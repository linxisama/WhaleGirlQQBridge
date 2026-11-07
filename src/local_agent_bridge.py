"""本地 DSH Agent 关联状态（只读检查，不执行 DSH 对话）。

除了看 cordis.patch.yml 里登记了什么，还静态解析 MCP 脚本本身：
- 脚本里的 TOOLS 列表有几个工具（不导入、不执行脚本）；
- 脚本是否自己把 stdio 钉成 UTF-8（缺这一步时 DSH 只会报 server unavailable）。
另外对控制台做一次轻量探活（bot_alive）：登记好了但控制台没开时，配置仍是"能启动"的，
所以状态分 ready / degraded / broken 三级，并把整份结果按 TTL 缓存，避免每次刷新都扫盘+打网络。
"""
from __future__ import annotations

import ast
import os
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BOT_ENTRY_ID = 'mcp-whale-bot'
BOT_SERVER_NAME = 'whale_bot'
UTF8_FIX_FUNCTION = 'force_utf8_stdio'
STATUS_TTL_SECONDS = 5.0      # status() 结果缓存秒数
PROBE_TIMEOUT_SECONDS = 1.5   # 探活超时上限（status() 整体不超过 2 秒）

_CACHE = {}                   # (home, profile) -> (monotonic 时间戳, 结果)
_CACHE_LOCK = threading.Lock()


def _entries(path):
    """按 id 取出补丁里的登记行；新增登记在 - insert: 块里，旧版本写在顶层裸行。"""
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding='utf-8-sig')) or []
    except Exception:
        return {}
    if not isinstance(data, list):
        return {}
    found = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        nested = item.get('insert')
        if isinstance(nested, list):
            for row in nested:
                if isinstance(row, dict) and row.get('id'):
                    found.setdefault(str(row['id']), row)
        elif item.get('id'):
            found[str(item['id'])] = item
    return found


def _config(entry) -> dict:
    config = entry.get('config') if isinstance(entry, dict) else None
    return config if isinstance(config, dict) else {}


def _script_path(entry) -> str:
    args = _config(entry).get('args')
    if isinstance(args, list) and args:
        return str(args[0] or '')
    return ''


def _tree(script: str):
    if not script:
        return None
    try:
        return ast.parse(Path(script).read_text(encoding='utf-8', errors='replace'))
    except (OSError, SyntaxError, ValueError):
        return None


def tool_names(script: str) -> list:
    """静态读出 MCP 脚本里 TOOLS 的 name 列表（只解析语法树，不导入脚本）。"""
    tree = _tree(script)
    if tree is None:
        return []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id == 'TOOLS'):
            continue
        if not isinstance(node.value, ast.List):
            continue
        names = []
        for item in node.value.elts:
            if not isinstance(item, ast.Dict):
                continue
            for key, value in zip(item.keys, item.values):
                if isinstance(key, ast.Constant) and key.value == 'name' \
                        and isinstance(value, ast.Constant) and isinstance(value.value, str):
                    names.append(value.value)
        return names
    return []


def has_utf8_stdio(script: str) -> bool:
    tree = _tree(script)
    if tree is None:
        return False
    return any(isinstance(node, ast.FunctionDef) and node.name == UTF8_FIX_FUNCTION
               for node in tree.body)


def probe(url: str, timeout=None):
    """对控制台发一次 GET 探活：有响应 True、连不上 False、没配地址 None。永不抛异常。"""
    url = str(url or '').strip()
    if not url:
        return None
    if not url.lower().startswith(('http://', 'https://')):
        return False
    try:
        with urlopen(Request(url, method='GET'),
                     timeout=PROBE_TIMEOUT_SECONDS if timeout is None else timeout) as response:
            response.read(0)
        return True
    except HTTPError:
        return True    # 4xx/5xx 也说明端口有人听，控制台至少起来了
    except Exception:
        return False   # 超时 / 拒绝连接 / DNS 失败一律算没起来


def _copy(result: dict) -> dict:
    """缓存里的列表要拷贝出去，调用方改返回值不能污染缓存。"""
    out = dict(result)
    out['bot_problems'] = list(result['bot_problems'])
    out['bot_tool_names'] = list(result['bot_tool_names'])
    out['bot_probe'] = dict(result.get('bot_probe') or {})
    return out


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def invalidate(home=None, profile=None) -> None:
    """只作废指定范围的缓存；两个参数都不给时等于 clear_cache。

    重跑 setup_dsh_memory.py 之后需要立刻看到新状态，但没必要把所有 profile
    的缓存一起丢掉（别的 profile 的扫描结果仍然有效）。
    """
    if home is None and profile is None:
        clear_cache()
        return
    key_home = None if home is None else str(Path(home))
    with _CACHE_LOCK:
        for key in [item for item in _CACHE
                    if (key_home is None or item[0] == key_home)
                    and (profile is None or item[1] == str(profile))]:
            _CACHE.pop(key, None)


def _scan(home: Path, profile: str) -> dict:
    profile_dir = home / 'profiles' / str(profile)
    patch = profile_dir / 'cordis.patch.yml'
    entries = _entries(patch)
    memory = entries.get('mcp-whale-memory', {})
    control = entries.get(BOT_ENTRY_ID, {})
    qqbot = entries.get('im-qqbot', {})
    whalegirl = entries.get('whalegirl', {})
    qqbot_config = qqbot.get('config') if isinstance(qqbot, dict) else {}
    qqbot_plugin = profile_dir / 'node_modules/@tencent-connect/dsh-qqbot/package.json'
    whalegirl_plugin = profile_dir / 'node_modules/dsh-we-whalegirl/package.json'

    control_config = _config(control)
    control_env = control_config.get('env') if isinstance(control_config.get('env'), dict) else {}
    bot_command = str(control_config.get('command') or '')
    bot_script = _script_path(control) if control else ''
    names = tool_names(bot_script) if bot_script else []
    utf8_fixed = has_utf8_stdio(bot_script) if bot_script else False
    utf8_env = str(control_env.get('PYTHONUTF8') or '') == '1'
    bot_url = str(control_env.get('WHALE_BOT_URL') or '')

    problems = []
    if not patch.is_file():
        problems.append('cordis.patch.yml 不存在：' + str(patch)
                        + '（运行 tools/setup_dsh_memory.py --bot-only 生成）')
    elif not control:
        problems.append('cordis.patch.yml 里没有 ' + BOT_ENTRY_ID
                        + ' 条目（运行 tools/setup_dsh_memory.py --bot-only 重新登记）')
    else:
        if not bot_command or not Path(bot_command).is_file():
            problems.append('MCP command 不存在：' + (bot_command or '(空)')
                            + '（重跑 tools/setup_dsh_memory.py --bot-only --python <Python 可执行文件>）')
        if not bot_script or not Path(bot_script).is_file():
            problems.append('MCP 脚本不存在：' + (bot_script or '(空)')
                            + '（重跑 tools/setup_dsh_memory.py --bot-only --bot-script <whale_bot_mcp.py 路径>）')
        if not names:
            problems.append('MCP 脚本里读不到 TOOLS 工具清单'
                            '（确认 --bot-script 指向完整的 whale_bot_mcp.py 后重跑 setup_dsh_memory.py）')
        if not (utf8_fixed or utf8_env):
            problems.append('MCP 未锁 UTF-8（在 cordis.patch.yml 的 env 里加 PYTHONUTF8: "1"，'
                            '然后重启 DSH）')

    bot_alive = probe(bot_url) if control else None
    # 探活结果的细节：只给 True/False 时界面只能显示「连不上」，用户不知道是
    # 端口没人听还是配错了地址。error 保留原始异常类型，方便贴给用户看。
    bot_probe = {'url': bot_url, 'ok': bot_alive,
                 'detail': ('' if bot_alive else
                            ('未配置 WHALE_BOT_URL' if bot_alive is None else
                             '连接被拒绝或超时'))}
    if problems:
        bot_state = 'broken'          # 配置就不对，DSH 里拉不起来
    elif bot_alive is False:
        bot_state = 'degraded'        # 配置能启动，但控制台此刻探不通
    else:
        bot_state = 'ready'
    if bot_alive is False:
        alive_hint = ('控制台探活失败：' + bot_url + '（先启动控制台；端口不一致就改 '
                      'cordis.patch.yml 里的 WHALE_BOT_URL 后重跑 setup_dsh_memory.py）')
    elif bot_alive is None:
        alive_hint = ('未配置 WHALE_BOT_URL，无法探活（重跑 tools/setup_dsh_memory.py --bot-only '
                      '--bot-url http://127.0.0.1:27499）')
    else:
        alive_hint = ''

    return {
        'dsh_home': str(home),
        'profile': str(profile),
        'profile_exists': profile_dir.is_dir(),
        'patch_exists': patch.is_file(),
        'memory_bridge': bool(memory),
        'bot_bridge': bool(control),
        'bot_registered': bool(control),
        'bot_server_name': str(control_config.get('serverName') or ''),
        'bot_url': bot_url,
        'bot_command': bot_command,
        'bot_command_exists': bool(bot_command) and Path(bot_command).is_file(),
        'bot_script': bot_script,
        'bot_script_exists': bool(bot_script) and Path(bot_script).is_file(),
        'bot_tool_count': len(names),
        'bot_tool_names': names,
        'bot_stdio_utf8': utf8_fixed,
        'bot_utf8_env': utf8_env,
        'bot_ready': not problems,
        'bot_state': bot_state,
        'bot_alive': bot_alive,
        'bot_alive_hint': alive_hint,
        'bot_probe': bot_probe,
        'bot_cache_ttl': STATUS_TTL_SECONDS,
        'bot_problems': problems,
        'qqbot_entry': bool(qqbot),
        'qqbot_plugin_installed': qqbot_plugin.is_file(),
        'qqbot_credentials_configured': bool(
            (qqbot_config or {}).get('appId') and (qqbot_config or {}).get('appSecret')),
        'whalegirl_entry': bool(whalegirl),
        'whalegirl_plugin_installed': whalegirl_plugin.is_file(),
        'memory_server_name': (memory.get('config') or {}).get('serverName', ''),
        'hint': '改完 DSH 配置要退出并重启 DSH（MCP server 只在新建会话时拉起，不热加载）；'
                'MCP 工具只在新的 DSH 会话里出现。',
    }


def status(home=None, profile='desktop', refresh=False):
    """读一次状态；STATUS_TTL_SECONDS 内复用缓存，refresh=True 强制重扫重探。"""
    home = Path(home or os.getenv('DSH_HOME') or (Path.home() / '.dsh'))
    profile = str(profile)
    key = (str(home), profile)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if not refresh and cached and time.monotonic() - cached[0] < STATUS_TTL_SECONDS:
            return _copy(cached[1])
        result = _scan(home, profile)
        _CACHE[key] = (time.monotonic(), result)
        return _copy(result)
