"""W13-d：日志三端点（前缀 /api/logs）+ 关键事件簿记入口。

能力全部来自 W13a 已验收的 app_log（**冻结**，这里只调用、不改它一行）：

* GET  /api/logs         —— 过滤查询（level / source / query / limit / since）；
* GET  /api/logs/export  —— 同一套过滤，附件下载一份 .json（HEAD 也能命中）；
* POST /api/logs/client  —— 前端把一条客户端侧日志塞进同一个库。

安全边界照抄 import_api.py:48 的双层口径（与宿主 qq_app.py 的 local_admin 中间件
同等或更严）：回环 client、拒转发头、Origin 同源、Host 白名单。中间件已经拦过一遍，
这里再显式拦一次 —— 单独装配本模块（测试或嵌入式）时也不裸奔。

返回约定：
* /api/logs → {"ok": true, "entries": [...最新在前...], "total": N, "truncated": bool}
* /api/logs/export → 同形 export() 载荷 + Content-Disposition 附件名
* /api/logs/client → {"ok": true, "id": "<16 位>"}；message 为空 → 400 {"ok": false,
  "code": "bad_request"}。

``log_event()`` 是本模块对外唯一的写入入口：它自己包了 try/except，调用点写日志
失败绝不会影响主流程，所以钩子处只要一行。
"""

from __future__ import annotations

import os
from datetime import datetime
from urllib.parse import urlsplit

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

from .app_log import DETAIL_MAX, MESSAGE_MAX, AppLog, default_app_log

PREFIX = '/api/logs'

DEFAULT_LIMIT = 200
EXPORT_LIMIT = 1000
MAX_LIMIT = 2000

LEVELS = ('debug', 'info', 'warning', 'error')
SOURCES = ('qq', 'console', 'agent', 'extension', 'skill', 'dsh', 'web', 'gui',
           'backend', 'import')
#: POST /api/logs/client 的归一化口径（冻结契约）：非法 level 归 error，非法 source 归 web。
CLIENT_LEVEL = 'error'
CLIENT_SOURCE = 'web'

_LOOPBACK_HOSTS = frozenset({'127.0.0.1', '::1', 'testclient', 'testserver'})
_LOCAL_HOSTNAMES = frozenset({'127.0.0.1', 'localhost', '::1', 'testserver'})
_FORWARD_HEADERS = ('forwarded', 'x-forwarded-for', 'x-real-ip')


class _Denied(Exception):
    """鉴权失败：由 exception_handler 落成 403 + auth_denied。"""


def _require_local(request: Request) -> None:
    """只允许本机直连：代理转发不能代表本机。"""
    client = getattr(request, 'client', None)
    host = str(getattr(client, 'host', '') or '')
    if host not in _LOOPBACK_HOSTS:
        raise _Denied('日志端点只允许本机访问（来源 %s）' % (host or 'unknown'))
    headers = request.headers
    for name in _FORWARD_HEADERS:
        if name in headers:
            raise _Denied('拒绝经由代理转发的请求（检测到 %s 头）' % name)
    origin = headers.get('origin')
    if origin and urlsplit(origin).netloc != (headers.get('host') or ''):
        raise _Denied('Origin 与 Host 不同源，拒绝')
    hostname = str(getattr(request.url, 'hostname', '') or '')
    if hostname not in _LOCAL_HOSTNAMES:
        raise _Denied('Host 必须是本机地址（收到 %s）' % (hostname or 'unknown'))


# ── 簿记：把关键事件写进同一个库（永不抛）──────────────────────────────────

def _as_bot(target):
    """既能传宿主 app，也能直接传 QQRuntime。"""
    inside = getattr(getattr(target, 'state', None), 'bot', None)
    return inside if inside is not None else target


def data_dir_of(target) -> str:
    """<data_dir>：bot.data_root 优先，退到 bot.root/'data'；都取不到返回空串。

    与 import_api._data_dir 同一个口径（Bot 的真实数据根，不猜路径）。
    """
    bot = _as_bot(target)
    value = getattr(bot, 'data_root', None)
    if not value:
        root = getattr(bot, 'root', None)
        value = os.path.join(str(root), 'data') if root else None
    if value is None or not str(value).strip():
        return ''
    try:
        return os.path.abspath(str(value))
    except (OSError, ValueError):
        return ''


def app_log_for(target) -> AppLog:
    """宿主同一个 data_dir 永远拿到同一个 AppLog（进程内单例）。"""
    return default_app_log(data_dir_of(target) or None)


def log_event(target, level, source, message, *, detail='', code='') -> None:
    """记一条关键事件；写日志失败一律吞掉，绝不影响主流程。"""
    try:
        app_log_for(target).record(level, source, message, detail=detail, code=code)
    except Exception:
        pass


# ── 参数整形（全部不抛）────────────────────────────────────────────────────

def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ''


def _limit(raw, default: int) -> int:
    """limit 解析：缺省/非法/非正数回落 default，封顶 MAX_LIMIT。"""
    text = _text(raw)
    if not text:
        return default
    try:
        number = int(text)
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    return min(number, MAX_LIMIT)


def _clip(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:cap]


def _norm_level(value) -> str:
    level = _text(value).lower()
    return level if level in LEVELS else CLIENT_LEVEL


def _norm_source(value) -> str:
    source = _text(value).lower()
    return source if source in SOURCES else CLIENT_SOURCE


async def _body(request: Request) -> dict:
    """坏 JSON 当空 body：缺 message 的路径后面自然落成 bad_request，不抛 500。"""
    try:
        raw = await request.json()
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _bad_request(message: str) -> JSONResponse:
    return JSONResponse({'ok': False, 'code': 'bad_request', 'message': message},
                        status_code=400)


# ── 装配 ────────────────────────────────────────────────────────────────────

def register(app, *, data_dir=None) -> None:
    """把 /api/logs* 三条端点挂到宿主 app 上（由 qq_app.create_app 调用）。

    直接用 app.get / app.post 注册，**不用 include_router**：本机 FastAPI 会往
    app.routes 里塞没有 .path 的 _IncludedRouter 占位符，打断冒烟与既有测试对
    app.routes 的遍历。
    """

    @app.exception_handler(_Denied)
    async def _on_denied(request: Request, exc: _Denied):
        return JSONResponse({'ok': False, 'code': 'auth_denied', 'message': str(exc)},
                            status_code=403)

    guard = {'dependencies': [Depends(_require_local)]}

    def store() -> AppLog:
        """Bot 的真实数据根优先（端点与关键事件因此共用同一个 AppLog）。"""
        return default_app_log(data_dir_of(app) or data_dir or None)

    def filters(request: Request) -> dict:
        params = request.query_params
        return {'level': params.get('level') or '',
                'source': params.get('source') or '',
                'query': params.get('query') or ''}

    @app.get(PREFIX, **guard)
    async def logs_tail(request: Request):
        """最近 N 条（最新在前）；total 是同一套过滤下的总数，用于判断是否被截断。"""
        app_log = store()
        wanted = filters(request)
        limit = _limit(request.query_params.get('limit'), DEFAULT_LIMIT)
        since = request.query_params.get('since') or ''
        entries = app_log.tail(limit=limit, since=since, **wanted)
        total = len(app_log.tail(limit=MAX_LIMIT, since=since, **wanted))
        return {'ok': True, 'entries': entries, 'total': total,
                'truncated': total > limit}

    # 本机 FastAPI 0.143 的 APIRoute 不会替 app.get 补 HEAD（实测 405），所以按
    # 显式口径写 methods=['GET', 'HEAD']：浏览器/下载器预检打 HEAD 要能命中。
    @app.api_route(PREFIX + '/export', methods=['GET', 'HEAD'], **guard)
    async def logs_export(request: Request):
        """导出成一份 .json 附件（GET 下载，HEAD 预检）。"""
        app_log = store()
        wanted = filters(request)
        limit = _limit(request.query_params.get('limit'), EXPORT_LIMIT)
        payload = app_log.export(limit=limit, **wanted)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        return JSONResponse(payload, headers={
            'Content-Disposition': 'attachment; filename="app-log-%s.json"' % stamp})

    @app.post(PREFIX + '/client', **guard)
    async def logs_client(request: Request):
        """前端侧日志入口：message 为空一律 400，其余字段按冻结口径归一。"""
        body = await _body(request)
        message = _text(body.get('message'))
        if not message:
            return _bad_request('message 不能为空：客户端日志至少要有一行摘要')
        entry = store().record(
            _norm_level(body.get('level')),
            _norm_source(body.get('source')),
            _clip(message, MESSAGE_MAX),
            detail=_clip(_text(body.get('detail')), DETAIL_MAX),
            code=_text(body.get('code'))[:80])
        return {'ok': True, 'id': entry.get('id', '')}
