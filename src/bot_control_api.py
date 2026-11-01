"""通过本机 HTTP 操作鲸鱼娘控制台（供 DSH 的 MCP 服务调用）。

控制台后端只监听 127.0.0.1，且校验 Host/Origin；本模块只走本机回环，
不读写配置文件、不碰密钥，所有动作都复用控制台已有的接口：

- 状态 / 启停：``/api/status`` ``/api/runtime`` ``/api/start`` ``/api/stop``
- 插件：``/api/plugins`` ``/api/plugins/{id}``
- 人设：``/api/personas`` ``/api/personas/{name}/activate``
- 发消息：``/api/onebot/action``（send_group_msg / send_private_msg）

环境变量 ``WHALE_BOT_URL`` 可覆盖地址；没设就跟控制台自己的端口走：
``WHALEGIRL_PORT`` > 控制台启动时写下的端点文件 ``<data>/qq-only/console_endpoint.json``
（端口 + 存活 pid）> ``http://127.0.0.1:27499``。

端点文件是给"用户改了控制台端口"兜底的：控制台不管用 ``--port`` 还是
``WHALEGIRL_PORT`` 起来，都把**真实监听的端口**记进这份文件，外部 MCP 运行期读它，
不需要重跑登记；控制台关掉后 pid 探活失败，自动退回默认端口。
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

_LOG = logging.getLogger(__name__)


class BotUnavailable(RuntimeError):
    pass


DEFAULT_PORT = 27499
# 端点文件的形状：版本号留给以后加字段时判断兼容性。
ENDPOINT_VERSION = 1
ENDPOINT_PARTS = ('qq-only', 'console_endpoint.json')
LOOPBACK_HOSTS = ('127.0.0.1', 'localhost', '::1')


def _valid_port(raw):
    """转成 1-65535 的端口；非法返回 None（调用方降级，绝不抛）。"""
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _pid_alive(pid) -> bool:
    """pid 还活在吗。Windows 走 OpenProcess 探活；其它平台退到 os.kill(pid, 0)。

    任何异常一律当"不存活"——探活失败还硬用旧端口，正是要消灭的那种静默打空。
    """
    try:
        value = int(str(pid).strip())
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    if os.name == 'nt':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, value)   # QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                # 系统 pid 复用会让"句柄拿到了"不等于"还是那个进程"，再看退出码兜一层。
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return int(code.value) == 259                     # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(value, 0)
    except PermissionError:
        return True          # 进程在，只是没权限发信号
    except OSError:
        return False
    except Exception:
        return False
    return True


def endpoint_path(data_dir) -> Path:
    """<data_dir>/qq-only/console_endpoint.json。"""
    return Path(data_dir).joinpath(*ENDPOINT_PARTS)


def data_dir_candidates() -> list:
    """控制台数据目录的候选（按可信度排序）。

    与 qq_gui.py 同一口径：非冻结运行是仓库根下的 data/，冻结运行是
    %LOCALAPPDATA%/WhaleGirlQQ/data。这里只是"猜位置"，采不采用由 pid 探活决定。
    """
    roots = []
    explicit = str(os.environ.get('WHALE_QQ_DATA_DIR') or '').strip()
    if explicit:
        roots.append(Path(explicit))
    home = str(os.environ.get('WHALE_QQ_HOME') or '').strip()
    if home:
        roots.append(Path(home) / 'data')
    roots.append(Path(__file__).resolve().parents[1] / 'data')
    local = str(os.environ.get('LOCALAPPDATA') or '').strip()
    if local:
        roots.append(Path(local) / 'WhaleGirlQQ' / 'data')
    out, seen = [], set()
    for root in roots:
        try:
            key = os.path.normcase(os.path.abspath(str(root)))
        except (OSError, ValueError):
            continue
        if key not in seen:
            seen.add(key)
            out.append(root)
    return out


def write_endpoint(port, data_dir, pid=None, started_at=None, host='127.0.0.1'):
    """把控制台真实监听的端口原子写进端点文件；失败只记日志并返回 None。

    先写 .tmp 再 os.replace：外部工具任何时刻读到的要么是旧文件、要么是完整新文件，
    不会读到写了一半的 JSON。这条路径绝不能挡住控制台启动，所以全部异常吞掉。
    """
    value = _valid_port(port)
    if value is None:
        _LOG.warning('控制台端口 %r 不合法，跳过端点文件写入', port)
        return None
    temp = None
    try:
        target = endpoint_path(data_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'version': ENDPOINT_VERSION,
            'host': str(host or '127.0.0.1'),
            'port': value,
            'pid': int(os.getpid() if pid is None else pid),
            'started_at': str(started_at or datetime.datetime.now().astimezone()
                              .isoformat(timespec='seconds')),
        }
        temp = target.with_name(target.name + '.tmp')
        with temp.open('w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp), str(target))
        return target
    except Exception as exc:
        _LOG.warning('控制台端点文件写入失败（%s）：%s', data_dir, exc)
        if temp is not None:
            try:
                temp.unlink()
            except OSError:
                pass
        return None


def _endpoint_url(data_dir=None, is_alive=None):
    """端点文件里记的控制台地址；缺失/损坏/端口非法/pid 已死都返回 None，绝不抛。

    data_dir 与 is_alive 是给单测注入的口子：不传时扫默认候选目录、用真实探活。
    """
    alive_check = _pid_alive if is_alive is None else is_alive
    directories = [data_dir] if data_dir is not None else data_dir_candidates()
    for directory in directories:
        try:
            target = endpoint_path(directory)
            if not target.is_file():
                continue
            item = json.loads(target.read_text(encoding='utf-8'))
        except Exception:
            continue
        if not isinstance(item, dict):
            continue
        port = _valid_port(item.get('port'))
        if port is None:
            continue
        try:
            pid = int(str(item.get('pid')).strip())
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        try:
            alive = bool(alive_check(pid))
        except Exception:
            alive = False
        if not alive:
            continue
        host = str(item.get('host') or '').strip() or '127.0.0.1'
        if host not in LOOPBACK_HOSTS:      # 只走本机回环，文件里写别的地址一律不认
            host = '127.0.0.1'
        return 'http://%s:%d' % (host, port)
    return None


def _console_url() -> str:
    """WHALEGIRL_PORT > 端点文件（pid 存活）> 默认端口；坏值一律降级，不抛异常。"""
    port = _valid_port(os.environ.get('WHALEGIRL_PORT'))
    if port is not None:
        return 'http://127.0.0.1:%d' % port
    return _endpoint_url() or 'http://127.0.0.1:%d' % DEFAULT_PORT


def base_url() -> str:
    """四级优先级：WHALE_BOT_URL > WHALEGIRL_PORT > 端点文件 > 27499。"""
    explicit = str(os.environ.get('WHALE_BOT_URL') or '').strip()
    return (explicit or _console_url()).rstrip('/')


def _detail(body) -> str:
    if isinstance(body, dict):
        return str(body.get('detail') or body.get('message') or '')
    return ''


def _request_raw(method: str, path: str, payload=None, timeout: float = 10.0):
    """发一次请求，返回 (HTTP 状态码, 解析后的 JSON)；解析不了时 JSON 为 None。"""
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = Request(base_url() + path, data=data, method=method,
                      headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, 'status', 200) or 200)
            raw = response.read()
    except HTTPError as exc:
        status = int(exc.code)
        try:
            raw = exc.read()
        except Exception:
            raw = b''
    except (URLError, TimeoutError, ConnectionError) as exc:
        raise BotUnavailable(
            f'连不上机器人控制台（{base_url()}）。请先打开控制台并确认后台已启动：{exc}') from None
    if not raw:
        return status, {}
    try:
        return status, json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        return status, None


def _request(method: str, path: str, payload=None, timeout: float = 10) -> dict:
    status, result = _request_raw(method, path, payload, timeout)
    if status >= 400:
        raise ValueError(_detail(result) or f'控制台返回 HTTP {status}')
    if result is None:
        raise ValueError('控制台返回的不是 JSON')
    if not isinstance(result, dict):
        raise ValueError('控制台返回格式不正确')
    return result


def status(include_recent: bool = False, recent_limit: int = 10) -> dict:
    runtime = _request('GET', '/api/runtime', timeout=5)
    state = _request('GET', '/api/status', timeout=5)
    onebot = state.get('onebot') if isinstance(state.get('onebot'), dict) else {}
    bridge = state.get('qqbridge') if isinstance(state.get('qqbridge'), dict) else {}
    result = {
        'app': runtime.get('app'), 'version': runtime.get('version'),
        'data_root': runtime.get('data_root'),
        'running': bool(state.get('running')),
        'reply_paused': bool(state.get('reply_paused')),
        'mode': state.get('mode'), 'account': state.get('account'),
        'model': state.get('model'), 'ai_configured': bool(state.get('ai_configured')),
        'billing_mode': state.get('billing_mode'),
        'connection': {'onebot': {'connected': bool(onebot.get('connected')),
                                  'online': bool(onebot.get('online')),
                                  'nickname': onebot.get('nickname') or '',
                                  'error': onebot.get('error') or ''},
                       'qqbridge': {'connected': bool(bridge.get('connected')),
                                    'online': bool(bridge.get('online')),
                                    'nickname': bridge.get('nickname') or '',
                                    'error': bridge.get('error') or ''}},
        'usage': state.get('usage') if isinstance(state.get('usage'), dict) else {},
    }
    if include_recent:
        limit = max(1, min(30, int(recent_limit or 10)))
        result['recent'] = [{'time': item.get('time'), 'sender': item.get('sender'),
                             'text': item.get('text'), 'state': item.get('state'),
                             'reply': item.get('reply'), 'kind': item.get('kind')}
                            for item in (state.get('messages') or [])[:limit]]
    return result


def start() -> dict:
    return _request('POST', '/api/start', {}, timeout=30)


def stop() -> dict:
    return _request('POST', '/api/stop', {}, timeout=30)


def plugins() -> dict:
    status, body = _request_raw('GET', '/api/plugins', timeout=10)
    if status >= 400:
        raise ValueError(_detail(body) or f'控制台返回 HTTP {status}')
    items = body.get('plugins') if isinstance(body, dict) else body
    if not isinstance(items, list):
        raise ValueError('插件列表格式不正确')
    return {'plugins': [{'id': item.get('id'), 'name': item.get('name'),
                         'enabled': bool(item.get('enabled')),
                         'category': item.get('category'), 'description': item.get('description')}
                        for item in items if isinstance(item, dict)]}


def set_plugin(plugin_id: str, enabled: bool) -> dict:
    plugin_id = str(plugin_id or '').strip()
    if not re.fullmatch(r'[a-z0-9_\-]{1,64}', plugin_id):
        raise ValueError('插件标识不合法')
    if type(enabled) is not bool:
        raise ValueError('enabled 必须是布尔值')
    return _request('POST', f'/api/plugins/{plugin_id}', {'enabled': enabled}, timeout=10)


def personas() -> dict:
    data = _request('GET', '/api/personas', timeout=5)
    return {'active': data.get('active'), 'locked': data.get('locked'),
            'personas': [{'name': item.get('name'), 'display_name': item.get('display_name')}
                         for item in (data.get('personas') or []) if isinstance(item, dict)]}


def activate_persona(name: str) -> dict:
    value = str(name or '').strip()
    if not value or len(value) > 48:
        raise ValueError('人设标识不合法')
    return _request('POST', '/api/personas/' + quote(value, safe='') + '/activate', {}, timeout=10)


def send_text(target_type: str, target_id: str, text: str) -> dict:
    """真实发一条 QQ 消息。只在用户明确要求时调用。"""
    target_type = str(target_type or '').strip()
    target_id = str(target_id or '').strip()
    text = str(text or '')
    if target_type not in ('group', 'private'):
        raise ValueError('target_type 只能是 group 或 private')
    if not target_id.isdigit():
        raise ValueError('target_id 必须是数字 QQ 号或群号')
    if not text.strip():
        raise ValueError('消息内容不能为空')
    if len(text) > 2000:
        raise ValueError('消息最多 2000 字')
    action = 'send_group_msg' if target_type == 'group' else 'send_private_msg'
    params = {'group_id': int(target_id), 'message': text} if target_type == 'group' \
        else {'user_id': int(target_id), 'message': text}
    result = _request('POST', '/api/onebot/action',
                      {'action': action, 'params': params}, timeout=20)
    return {'sent': bool(result.get('success')), 'target_type': target_type,
            'target_id': target_id, 'result': result}

# ---------------------------------------------------------------------------
# 扩展市场 / 已装扩展 / 能力桥 / 技能工具
#
# 这些接口由控制台后端提供，可能还没实现（并行开发中）。因此这里的每个函数都
# 返回结构化结果而不是抛栈：
#   成功      {'ok': True, ...}
#   端点缺失  {'ok': False, 'error': {'code': 'not_implemented', ...}}
# 传输层错误仍然抛 BotUnavailable（调用方要给出可读提示），参数错误抛
# ValueError，需要二次确认的操作抛 ConfirmationRequired。
# ---------------------------------------------------------------------------

MARKET_PATH = '/api/qqagent/market'
MARKET_INSTALL_PATH = '/api/qqagent/market/install'
EXTENSIONS_PATH = '/api/qqagent/extensions'
BRIDGE_STATUS_PATH = '/api/qqagent/bridge/status'
BRIDGE_CALLS_PATHS = ('/api/qqagent/bridge/calls', '/api/qqagent/bridge/stats')
EXTENSION_TOGGLE_PATHS = ('/api/qqagent/extensions/{id}', '/api/extensions/{id}')
MARKET_ZIP_PATHS = ('/api/qqagent/extensions/install-local',
                    '/api/qqagent/market/install-local',
                    '/api/extensions/install-local')
SKILL_TOOL_PATHS = ('/api/qqagent/skills', '/api/skills', '/api/agent/skills')
MISSING_STATUS = (404, 405, 501)
EXTENSION_ID_RE = re.compile(r'[A-Za-z0-9_.-]{1,64}')


class ConfirmationRequired(ValueError):
    """会真的改动磁盘/配置的操作缺少显式确认。"""


def structured_error(code: str, message: str, **extra) -> dict:
    payload = {'ok': False, 'error': {'code': code, 'message': str(message)}}
    payload['error'].update(extra)
    return payload


def structured_ok(**data) -> dict:
    payload = {'ok': True}
    payload.update(data)
    return payload


def _not_implemented(paths, feature: str) -> dict:
    candidates = [paths] if isinstance(paths, str) else list(paths)
    return structured_error(
        'not_implemented',
        '控制台还没有「' + feature + '」接口（试过 ' + '、'.join(candidates) + '）。'
        '等控制台后端补上端点后再调用；本次没有改动任何东西。',
        endpoints=candidates)


def _probe(method: str, paths, payload=None, timeout: float = 10.0):
    """按顺序试候选路径；全部 404/405/501 时返回 (None, None)。"""
    candidates = [paths] if isinstance(paths, str) else list(paths)
    for path in candidates:
        status, body = _request_raw(method, path, payload, timeout)
        if status in MISSING_STATUS:
            continue
        if status >= 400:
            raise ValueError(_detail(body) or '控制台返回 HTTP ' + str(status) + '（' + path + '）')
        if body is None:
            raise ValueError('控制台返回的不是 JSON（' + path + '）')
        return path, body
    return None, None


def _int_in_range(value, name: str, low: int, high: int, default: int) -> int:
    if value is None or value == '':
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(name + ' 必须是整数')
    if value < low or value > high:
        raise ValueError(name + ' 必须在 ' + str(low) + '..' + str(high) + ' 之间')
    return value


def _market_item(item: dict) -> dict:
    return {'id': item.get('id'), 'name': item.get('name'),
            'description': item.get('description'),
            'category': item.get('category'), 'category_name': item.get('categoryName'),
            'version': item.get('version'), 'author': item.get('author'),
            'downloads': item.get('downloads'), 'status': item.get('status'),
            'installable': bool(item.get('downloadUrl'))}


def market_list(category: str = '', keyword: str = '', limit: int = 20,
                refresh: bool = False) -> dict:
    """列出市场条目；分类/关键词在本地过滤，limit 上限 50。"""
    if type(refresh) is not bool:
        raise ValueError('refresh 必须是布尔值')
    limit = _int_in_range(limit, 'limit', 1, 50, 20)
    status, body = _request_raw('POST', MARKET_PATH, {'refresh': refresh}, timeout=60)
    if status in MISSING_STATUS:
        return _not_implemented(MARKET_PATH, '市场清单')
    if status >= 400:
        raise ValueError(_detail(body) or '控制台返回 HTTP ' + str(status))
    data = body if isinstance(body, dict) else {}
    raw_items = [item for item in (data.get('items') or []) if isinstance(item, dict)]
    items = raw_items
    wanted_category = str(category or '').strip().lower()
    if wanted_category:
        items = [item for item in items
                 if wanted_category in (str(item.get('category') or '').lower(),
                                        str(item.get('categoryName') or '').lower())]
    wanted_keyword = str(keyword or '').strip().lower()
    if wanted_keyword:
        items = [item for item in items
                 if wanted_keyword in ' '.join(str(item.get(key) or '')
                                               for key in ('id', 'name', 'description')
                                               ).lower()]
    page = items[:limit]
    return structured_ok(endpoint=MARKET_PATH, count=len(page), total=len(raw_items),
                         matched=len(items),
                         installable=sum(1 for item in page if item.get('downloadUrl')),
                         categories=data.get('categories') or {},
                         fetched_at=data.get('fetchedAt'),
                         items=[_market_item(item) for item in page])


def install_market(extension_id: str = '', zip_path: str = '', confirm: bool = False) -> dict:
    """安装一个市场扩展（id）或本地 zip 包。会真的写入磁盘，必须 confirm=True。"""
    if confirm is not True:
        raise ConfirmationRequired('安装扩展会真的下载并写入扩展目录；确认后请传 confirm:true')
    ident = str(extension_id or '').strip()
    archive = str(zip_path or '').strip()
    if ident and archive:
        raise ValueError('extension_id 与 zip_path 只能给一个')
    if not ident and not archive:
        raise ValueError('需要 extension_id 或 zip_path')
    if ident:
        if not EXTENSION_ID_RE.fullmatch(ident):
            raise ValueError('扩展标识不合法')
        status, body = _request_raw('POST', MARKET_INSTALL_PATH, {'id': ident}, timeout=120)
        if status in MISSING_STATUS:
            return _not_implemented(MARKET_INSTALL_PATH, '市场安装')
        if status >= 400:
            raise ValueError(_detail(body) or '控制台返回 HTTP ' + str(status))
        data = body if isinstance(body, dict) else {}
        return structured_ok(action='install', endpoint=MARKET_INSTALL_PATH,
                             extension_id=ident, installed=True, source='market',
                             extension=data.get('extension') or data)
    path = Path(archive)
    try:
        exists = path.is_file()
    except OSError:
        exists = False
    if not exists:
        raise ValueError('zip 文件不存在：' + archive)
    if path.suffix.lower() != '.zip':
        raise ValueError('本地扩展包必须是 .zip')
    resolved = str(path.resolve())
    found, body = _probe('POST', MARKET_ZIP_PATHS, {'zipPath': resolved}, timeout=120)
    if found is None:
        return _not_implemented(MARKET_ZIP_PATHS, '本地 zip 安装扩展')
    data = body if isinstance(body, dict) else {}
    return structured_ok(action='install', endpoint=found, installed=True, source='zip',
                         zip_path=resolved, extension=data.get('extension') or data)


def extensions() -> dict:
    """列出已装扩展及启用状态。"""
    status, body = _request_raw('GET', EXTENSIONS_PATH, timeout=20)
    if status in MISSING_STATUS:
        return _not_implemented(EXTENSIONS_PATH, '已装扩展列表')
    if status >= 400:
        raise ValueError(_detail(body) or '控制台返回 HTTP ' + str(status))
    raw = body.get('items') if isinstance(body, dict) else body
    raw = [item for item in (raw or []) if isinstance(item, dict)]
    items = []
    for item in raw:
        manifest = item.get('manifest') if isinstance(item.get('manifest'), dict) else {}
        enabled = item.get('enabled')
        items.append({
            'id': item.get('id') or manifest.get('id'),
            'name': item.get('name') or manifest.get('name'),
            'version': item.get('version') or manifest.get('version'),
            'description': item.get('description') or manifest.get('description'),
            'capabilities': item.get('capabilities') or manifest.get('capabilities') or [],
            'enabled': bool(enabled) if isinstance(enabled, bool) else None,
            'source': item.get('source') or item.get('origin') or '',
        })
    return structured_ok(endpoint=EXTENSIONS_PATH, count=len(items), items=items)


def set_extension(extension_id: str, enabled: bool) -> dict:
    """启用/停用已装扩展。"""
    ident = str(extension_id or '').strip()
    if not EXTENSION_ID_RE.fullmatch(ident):
        raise ValueError('扩展标识不合法')
    if type(enabled) is not bool:
        raise ValueError('enabled 必须是布尔值')
    quoted = quote(ident, safe='')
    candidates = [path.replace('{id}', quoted) for path in EXTENSION_TOGGLE_PATHS]
    found, body = _probe('POST', candidates, {'enabled': enabled}, timeout=30)
    if found is None:
        return _not_implemented(candidates, '扩展启停')
    data = body if isinstance(body, dict) else {}
    return structured_ok(action='enable' if enabled else 'disable', endpoint=found,
                         extension_id=ident,
                         enabled=bool(data.get('enabled', enabled)), result=data)


def _call_stats(data: dict) -> dict:
    keys = ('total', 'count', 'calls', 'success', 'failed', 'errors')
    stats = {key: data[key] for key in keys if key in data}
    recent = data.get('recent') or data.get('items') or []
    stats['recent'] = [item for item in recent if isinstance(item, dict)][:10]
    return stats


def bridge_status() -> dict:
    """能力桥只读状态：provider/hook 覆盖、缺什么、最近调用统计（有才返回）。"""
    found, body = _probe('GET', (BRIDGE_STATUS_PATH,), None, timeout=20)
    if found is None:
        return _not_implemented(BRIDGE_STATUS_PATH, '能力桥状态')
    data = body if isinstance(body, dict) else {}
    calls_path, calls = _probe('GET', BRIDGE_CALLS_PATHS, None, timeout=15)
    return structured_ok(
        endpoint=found,
        loaded=bool(data.get('loaded')), enabled=bool(data.get('enabled')),
        ready=bool(data.get('ready')), profiles=int(data.get('profiles') or 0),
        providers=[str(item) for item in (data.get('providers') or [])],
        hooks=[str(item) for item in (data.get('hooks') or [])],
        missing=[str(item) for item in (data.get('missing') or [])],
        extensions_dir=data.get('extensionsDir') or '',
        catalog_path=data.get('catalogPath') or '',
        calls_available=calls_path is not None,
        calls_endpoint=calls_path or '',
        calls=_call_stats(calls) if isinstance(calls, dict) else None)


def skill_tools() -> dict:
    """skill 提供的工具清单（控制台后端没有该接口时返回 not_implemented）。"""
    found, body = _probe('GET', SKILL_TOOL_PATHS, None, timeout=20)
    if found is None:
        return _not_implemented(SKILL_TOOL_PATHS, '技能工具清单')
    if isinstance(body, list):
        raw = body
    elif isinstance(body, dict):
        raw = body.get('tools') or body.get('items') or body.get('skills') or []
    else:
        raw = []
    tools = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        nested = entry.get('tools')
        if isinstance(nested, list):
            for item in nested:
                if isinstance(item, dict):
                    tools.append({'skill': entry.get('id') or entry.get('name'),
                                  'name': item.get('name'),
                                  'description': item.get('description') or ''})
            continue
        tools.append({'skill': entry.get('skill') or entry.get('source') or '',
                      'name': entry.get('name') or entry.get('id'),
                      'description': entry.get('description') or ''})
    return structured_ok(endpoint=found, count=len(tools), tools=tools)
