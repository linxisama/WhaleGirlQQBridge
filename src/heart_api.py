"""心情面板路由：/heart（页面）+ /api/heart/state、/api/heart/action。

面板本体在插件包里（<custom_plugins>/dsh-heart-qq/web/heart.html），这里只负责发文件
和转发到插件实例的 snapshot()/apply_action()。插件没装、没启用或关了 panel 时，
页面会显示一行原因而不是白屏。

直接用 app.get / app.post 注册，不用 include_router：本机 FastAPI 会往 app.routes 里
塞没有 .path 的 _IncludedRouter 占位符，打断冒烟与既有测试对 app.routes 的遍历。
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

from fastapi import Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

MOD_ID = 'dsh-heart-qq'
PLUGIN_ID = 'dsh_heart'
PAGE_NAME = 'heart.html'
PAGE_PATH = '/heart'
API_PREFIX = '/api/heart'
CSRF_HEADER = 'x-dsh-heart'
MAX_BODY = 65536

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
        raise _Denied('心情面板只允许本机访问（来源 %s）' % (host or 'unknown'))
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


def _as_bot(target):
    """既能传宿主 app，也能直接传 QQRuntime。"""
    inside = getattr(getattr(target, 'state', None), 'bot', None)
    return inside if inside is not None else target


def _manager(target):
    return getattr(_as_bot(target), 'plugins', None)


def plugin_for(target):
    """已挂载的心情插件实例；没装 / 没加载返回 None。"""
    table = getattr(_manager(target), '_plugins', None)
    if not isinstance(table, dict):
        return None
    return table.get(PLUGIN_ID)


def page_file(target):
    """面板 HTML 的绝对路径；插件目录不在了返回 None。"""
    manager = _manager(target)
    locate = getattr(manager, 'custom_mod_path', None)
    if not callable(locate):
        return None
    try:
        root = locate(MOD_ID)
    except Exception:
        return None
    page = root / 'web' / PAGE_NAME
    try:
        return page if page.is_file() else None
    except OSError:
        return None


def _unavailable(target):
    """插件不可用时页面/接口统一给的原因；可用时返回空串。"""
    manager = _manager(target)
    if manager is None:
        return '宿主没有插件管理器（bot.plugins 为空）'
    plugin = plugin_for(target)
    if plugin is None:
        return '心情插件没加载：把 dsh-heart-qq 目录放进 data/qq-only/custom_plugins/ 后重启'
    if not getattr(plugin, 'enabled', False):
        return '心情插件被停用了：到「插件」页打开它'
    try:
        if not plugin.get('panel', True):
            return '面板开关关着：到「插件」页把「网页面板」打开'
    except Exception:
        pass
    return ''


def _message_page(reason: str) -> HTMLResponse:
    """插件不可用时的整页说明，免得用户看到白屏或 JSON。"""
    text = (reason or '心情面板暂时不可用').replace('&', '&amp;').replace('<', '&lt;')
    body = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<title>心情面板 · 不可用</title><style>'
        ':root{color-scheme:dark;font-family:"Microsoft YaHei",system-ui,sans-serif}'
        'body{margin:0;background:#0b1220;color:#eaf1ff;display:flex;min-height:100vh;'
        'align-items:center;justify-content:center}'
        '.box{max-width:520px;padding:28px 30px;background:#131f31;border:1px solid #273750;'
        'border-radius:16px;line-height:1.9;font-size:14px}'
        'a{color:#72ddd1}</style></head><body><div class="box">'
        '<h1 style="font-size:18px;margin:0 0 12px">心情面板暂时打不开</h1>'
        '<p style="color:#a9bad0;margin:0 0 16px">' + text + '</p>'
        '<p><a href="/">← 回控制台</a></p></div></body></html>'
    )
    return HTMLResponse(body, status_code=503)


async def _body(request: Request) -> dict:
    """坏 JSON / 空体一律当空 dict，交给后续校验报错。"""
    try:
        raw = await request.body()
    except Exception:
        return {}
    if not raw or len(raw) > MAX_BODY:
        return {}
    try:
        data = json.loads(raw.decode('utf-8', 'replace'))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _bad_request(message: str) -> JSONResponse:
    return JSONResponse({'ok': False, 'code': 'bad_request', 'message': message}, status_code=400)


def register(app) -> None:
    """挂上 /heart 与 /api/heart/*。重复注册同一 App 只挂一次。"""
    marker = '_heart_routes_installed'
    if getattr(app.state, marker, False):
        return
    setattr(app.state, marker, True)

    @app.exception_handler(_Denied)
    async def _denied(_request: Request, exc: _Denied):
        return JSONResponse(
            {'ok': False, 'code': 'auth_denied', 'message': str(exc)}, status_code=403)

    guard = {'dependencies': [Depends(_require_local)]}

    @app.get(PAGE_PATH, **guard)
    async def heart_page():
        reason = _unavailable(app)
        if reason:
            return _message_page(reason)
        page = page_file(app)
        if page is None:
            return _message_page('插件目录里找不到 web/%s（插件包不完整）' % PAGE_NAME)
        return FileResponse(page, media_type='text/html; charset=utf-8')

    @app.get(API_PREFIX + '/state', **guard)
    async def heart_state():
        reason = _unavailable(app)
        if reason:
            return JSONResponse({'ok': False, 'code': 'unavailable', 'message': reason},
                                status_code=503)
        plugin = plugin_for(app)
        try:
            state = await asyncio.to_thread(plugin.snapshot)
        except Exception as exc:
            return JSONResponse({'ok': False, 'code': 'state_failed',
                                 'message': '读取心情数值失败：%s' % exc}, status_code=500)
        return {'ok': True, 'state': state}

    @app.post(API_PREFIX + '/action', **guard)
    async def heart_action(request: Request):
        # 写请求要自定义头：跨站表单带不出这个头，local_admin 之外再加一层。
        if request.headers.get(CSRF_HEADER) != '1':
            return JSONResponse(
                {'ok': False, 'code': 'csrf', 'message': '缺少 %s 头（跨站写保护）' % CSRF_HEADER},
                status_code=403)
        reason = _unavailable(app)
        if reason:
            return JSONResponse({'ok': False, 'code': 'unavailable', 'message': reason},
                                status_code=503)
        body = await _body(request)
        action = str(body.get('action') or '').strip()
        if not action:
            return _bad_request('action 不能为空')
        plugin = plugin_for(app)
        try:
            ok, error, state = await asyncio.to_thread(plugin.apply_action, body)
        except Exception as exc:
            return JSONResponse({'ok': False, 'code': 'action_failed',
                                 'message': '动作执行失败：%s' % exc}, status_code=500)
        if not ok:
            return JSONResponse({'ok': False, 'code': 'rejected',
                                 'message': error or '没改成', 'state': state}, status_code=400)
        return {'ok': True, 'state': state}
