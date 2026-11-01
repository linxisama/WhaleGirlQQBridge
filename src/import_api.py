"""W11-4b：统一导入端点（前缀 /api/import）。

两条端点，能力全部来自 W11-4a 已验收的 pack_import（**冻结**，这里只 import 调用
它的 detect/inspect/install，不改它一行）：

* POST /api/import/inspect —— 预检，body {"path": "<本机绝对路径>"}；
* POST /api/import/install —— 落盘，body {path, confirmed, kind, overwrite, enable}。

为什么要这一层：控制台里原本有 5 个各自为政的导入对话框（qq_gui.py:3128/3545/3667/3910、
src/gui/assistant.py:941）和 24 条按家族分家的路由，用户不知道该点哪个。收敛成
"一个路径 + 两条端点"，家族由 pack_import 按**包内容**判定，GUI 不猜。

安全边界照抄 agent_backend_api.py:63 的双层口径（与宿主 qq_app.py 的 local_admin 中间件
同等或更严）：回环 client、拒转发头、Origin 同源、Host 白名单。中间件已经拦过一遍，
这里再显式拦一次 —— 单独装配本模块（测试或嵌入式）时也不裸奔。

确认闸门：install 的 confirmed 必须是 True，否则 400 + unconfirmed 且**一次都不落盘**。
PackImportError 一律 400，绝不变成 500。

返回约定：永远是与 pack_import 同形的 19 键 schema（失败时 problems 里带 code/message/hint），
并统一带 duration_ms；HTTP 状态码 400（鉴权失败 403）。
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

from . import pack_import
from .log_api import log_event as _log_event

PREFIX = '/api/import'

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
        raise _Denied('统一导入只允许本机访问（来源 %s）' % (host or 'unknown'))
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


# ── 统一 schema 小工具 ──────────────────────────────────────────────────────

#: 与 pack_import._new_result() 严格对齐的 19 个键：GUI 只渲染一套。
SCHEMA_KEYS = ('ok', 'kind', 'id', 'name', 'version', 'entry', 'files', 'bytes',
               'landed', 'target_dir', 'tools', 'hooks', 'providers', 'problems',
               'notices', 'load', 'installed_but_disabled', 'actions', 'duration_ms')


def _blank() -> dict:
    """失败响应用的空壳：键与 inspect() 完全一致，前端不用写第二套渲染。"""
    return {
        'ok': False,
        'kind': 'unknown',
        'id': '',
        'name': '',
        'version': '',
        'entry': '',
        'files': 0,
        'bytes': 0,
        'landed': '',
        'target_dir': '',
        'tools': [],
        'hooks': [],
        'providers': [],
        'problems': [],
        'notices': [],
        'load': {'ok': False, 'error': '', 'code': ''},
        'installed_but_disabled': False,
        'actions': [],
        'duration_ms': 0,
    }


def _problem(code: str, message: str, hint: str = '') -> dict:
    return {'code': str(code), 'message': str(message), 'hint': str(hint)}


def _ms(started) -> int:
    return int((time.perf_counter() - started) * 1000) if started is not None else 0


def _failure(code: str, message: str, hint: str = '', *,
             started=None, status: int = 400) -> JSONResponse:
    payload = _blank()
    payload['problems'] = [_problem(code, message, hint)]
    payload['duration_ms'] = _ms(started)
    return JSONResponse(payload, status_code=status)


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ''


async def _body(request: Request) -> dict:
    """坏 JSON 当空 body：缺字段的路径后面自然落成 bad-path，不抛 500。"""
    try:
        raw = await request.json()
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


# ── 宿主真实取值（data_dir / plugins_dir / skills_runtime）───────────────────

def _bot(app):
    return getattr(getattr(app, 'state', None), 'bot', None)


def _data_dir(app, override) -> str:
    """<data_dir>：显式传值优先，否则取 Bot 的真实数据根，不猜。"""
    value = override if override else getattr(_bot(app), 'root', None)
    if value is None or not str(value).strip():
        return ''
    try:
        return os.path.abspath(str(value))
    except (OSError, ValueError):
        return ''


def _plugins_dir(app, override) -> str:
    """<plugins_dir>：显式传值优先，否则取宿主插件管理器的自定义插件目录。"""
    value = override
    if not value:
        manager = getattr(_bot(app), 'plugins', None)
        value = getattr(manager, 'custom_plugins_dir', None)
    if value is None or not str(value).strip():
        return ''
    try:
        return os.path.abspath(str(value))
    except (OSError, ValueError):
        return ''


def _skills_runtime(app):
    """install() 要运行时对象：它自己会用 _runtime_path 取出数据根。"""
    return _bot(app)


def _skills_root(app) -> str:
    """inspect() 只吃路径（pack_import.py:621 直接 Path(skills_runtime) 拼计划落点）。

    取值顺序与 pack_import._runtime_path 一致：先 data_root，再 root。
    """
    bot = _bot(app)
    for name in ('data_root', 'root'):
        value = getattr(bot, name, None)
        if value:
            return str(value)
    return ''


# ── 装配 ────────────────────────────────────────────────────────────────────

# ── 目录 mod 装完立刻挂载（F1）───────────────────────────────────────────────


def _mod_sync(app):
    """调一次运行中宿主的 PluginManager.sync_mods()，返回 (report, 失败原因)。

    安装本身已经成功：这里任何问题都只写进 notices，绝不把 ok 改成 False。
    """
    manager = getattr(_bot(app), 'plugins', None)
    sync = getattr(manager, 'sync_mods', None)
    if not callable(sync):
        return None, '宿主里没有可用的 PluginManager.sync_mods'
    try:
        report = sync()
    except Exception as exc:      # 同步炸了不算安装失败，原因照样要看得见
        return None, 'sync_mods() 抛异常：%s: %s' % (type(exc).__name__, exc)
    if not isinstance(report, dict):
        return None, 'sync_mods() 返回 %s，不是 dict' % type(report).__name__
    return report, ''


def _sync_counts(report: dict) -> str:
    """把 sync_mods() 的报告压成一行 added=… removed=… 这种，原样进 notice。"""
    parts = []
    for key in ('added', 'removed', 'reloaded', 'unchanged', 'rejected', 'failed'):
        value = report.get(key) or []
        parts.append('%s=%s' % (key, ','.join(str(one) for one in value) or '-'))
    return ' '.join(parts)


def _mod_mounted(payload: dict, report: dict, error: str) -> bool:
    """按 sync_mods() 的真实结果改写 notices，返回「这个 mod 现在真的挂上了」。

    同步成功就不许再说「下一次启动才会挂载」：安装期那条 mod-mount-pending 一律摘掉，
    换成 mod-mounted / mod-mount-failed / mod-mount-pending（这一轮没扫到）三者之一。
    """
    ident = str(payload.get('id') or '')
    notices = payload.get('notices')
    if not isinstance(notices, list):
        notices = []
        payload['notices'] = notices
    notices[:] = [one for one in notices
                  if not (isinstance(one, dict) and one.get('code') == 'mod-mount-pending')]
    if error:
        notices.append({'code': 'mod-mount-pending',
                        'message': '已落盘，但这次没能让运行中的宿主挂上它（%s）：下一次 '
                                   'PluginManager 扫描（宿主启动或手工 sync_mods()）才会挂载'
                                   % error})
        return False
    mounted = [key for key in ('added', 'reloaded', 'unchanged')
               if ident in [str(one) for one in (report.get(key) or [])]]
    counts = _sync_counts(report)
    if mounted:
        notices.append({'code': 'mod-mounted',
                        'message': '已落盘并同步进运行中的宿主：%s（sync_mods: %s）'
                                   % (ident, counts)})
        return True
    refused = [key for key in ('rejected', 'failed')
               if ident in [str(one) for one in (report.get(key) or [])]]
    if refused:
        notices.append({'code': 'mod-mount-failed',
                        'message': '已落盘，但 sync_mods() 拒载或挂载失败：%s（%s）；'
                                   '按 plugin.json 的拒载原因改好后重新导入或手工 sync_mods()'
                                   % (ident, counts)})
        return False
    notices.append({'code': 'mod-mount-pending',
                    'message': '已落盘，但这一轮 sync_mods() 没扫到 %s（%s）' % (ident, counts)})
    return False


def register(app, *, data_dir=None, plugins_dir=None) -> None:
    """把 /api/import* 两条端点挂到宿主 app 上（由 qq_app.create_app 调用）。

    直接用 app.post 注册，**不用 include_router**：本机 FastAPI 会往 app.routes 里塞
    没有 .path 的 _IncludedRouter 占位符，打断冒烟与既有测试对 app.routes 的遍历。
    """

    @app.exception_handler(_Denied)
    async def _on_denied(request: Request, exc: _Denied):
        return JSONResponse({'ok': False, 'code': 'auth_denied', 'message': str(exc)},
                            status_code=403)

    guard = {'dependencies': [Depends(_require_local)]}

    def roots() -> dict:
        # custom_plugins_dir 与 plugins_dir 同源（都是 manager.custom_plugins_dir）：
        # 目录 mod 的落点是 <custom_plugins_dir>/<id>/，.py 插件是 <plugins_dir>/<id>.py。
        return {'data_dir': _data_dir(app, data_dir),
                'plugins_dir': _plugins_dir(app, plugins_dir),
                'custom_plugins_dir': _plugins_dir(app, plugins_dir),
                'skills_runtime': _skills_runtime(app),
                'skills_root': _skills_root(app)}

    def custom_root(body: dict, kwargs: dict) -> str:
        """可选请求体字段 custom_plugins_dir：显式给了就按它算（仍走同一套校验）。"""
        override = _text(body.get('custom_plugins_dir'))
        if override:
            return _plugins_dir(app, override)
        return kwargs['custom_plugins_dir']

    def bad_path(path: str, started) -> JSONResponse:
        if not path:
            return _failure('bad-path', '没有给包的路径：预检和安装都需要本机绝对路径',
                            '在对话框里先「选择文件…」或「选择文件夹…」', started=started)
        if not os.path.exists(path):
            return _failure('bad-path', '路径不存在：%s' % path,
                            '重新选择要导入的包', started=started)
        if not os.access(path, os.R_OK):
            return _failure('bad-path', '路径读不了（权限不足）：%s' % path,
                            '换一个有读权限的位置再试', started=started)
        return None

    @app.post(PREFIX + '/inspect', **guard)
    async def import_inspect(request: Request):
        """预检：只读，绝不落盘（inspect 全语料一轮约 55 秒，必须离开事件循环）。"""
        started = time.perf_counter()
        body = await _body(request)
        path = _text(body.get('path'))
        rejected = bad_path(path, started)
        if rejected is not None:
            return rejected
        kwargs = roots()
        result = await asyncio.to_thread(
            pack_import.inspect, path,
            plugins_dir=kwargs['plugins_dir'] or None,
            skills_runtime=kwargs['skills_root'] or None,
            data_dir=kwargs['data_dir'] or None,
            custom_plugins_dir=custom_root(body, kwargs) or None)
        payload = dict(result) if isinstance(result, dict) else _blank()
        payload.setdefault('duration_ms', _ms(started))
        return JSONResponse(payload, status_code=200 if payload.get('ok') else 400)

    @app.post(PREFIX + '/install', **guard)
    async def import_install(request: Request):
        """安装：confirmed 必须是 True；未确认一次都不落盘。"""
        started = time.perf_counter()
        body = await _body(request)
        if body.get('confirmed') is not True:
            return _failure('unconfirmed',
                            '未经确认：请先预检并把结果展示给用户，确认后再安装',
                            '先调 /api/import/inspect，用户点「安装」时再带 confirmed=true',
                            started=started)
        path = _text(body.get('path'))
        rejected = bad_path(path, started)
        if rejected is not None:
            return rejected
        enable = body.get('enable')
        kwargs = roots()
        try:
            result = await asyncio.to_thread(
                pack_import.install, path,
                confirmed=True,
                kind=_text(body.get('kind')),
                overwrite=bool(body.get('overwrite')),
                enable=enable if isinstance(enable, bool) else None,
                plugins_dir=kwargs['plugins_dir'] or None,
                skills_runtime=kwargs['skills_runtime'],
                data_dir=kwargs['data_dir'] or None,
                custom_plugins_dir=custom_root(body, kwargs) or None)
        except pack_import.PackImportError as exc:  # 未确认竞态：400，不是 500
            return _failure('unconfirmed', str(exc),
                            '先预检并把结果展示给用户', started=started)
        except Exception as exc:  # install() 契约上不抛，兜底也不许 500
            return _failure('install-failed',
                            '安装异常：%s: %s' % (type(exc).__name__, exc),
                            '把包和这段报错一起发给维护者', started=started)
        payload = dict(result) if isinstance(result, dict) else _blank()
        payload.setdefault('duration_ms', _ms(started))
        if payload.get('ok') and payload.get('kind') == 'mod-py':
            # 目录 mod 落盘后不会自己出现：装完立刻让运行中的宿主同步一次。
            # 放线程里跑（sync_mods 会 import mod 代码），失败只写 notice，不改 ok。
            report, error = await asyncio.to_thread(_mod_sync, app)
            if _mod_mounted(payload, report or {}, error):
                payload['load'] = {'ok': True, 'code': 'mounted', 'error': ''}
        if payload.get('ok'):
            _log_event(_bot(app), 'info', 'import',
                       '导入成功：%s %s' % (payload.get('kind') or '', payload.get('id') or ''))
        return JSONResponse(payload, status_code=200 if payload.get('ok') else 400)
