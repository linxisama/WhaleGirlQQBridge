"""本地 DSH Agent 关联状态（只读检查，不执行 DSH 对话）。

除了看 cordis.patch.yml 里登记了什么，还静态解析 MCP 脚本本身：
- 脚本里的 TOOLS 列表有几个工具（不导入、不执行脚本）；
- 脚本是否自己把 stdio 钉成 UTF-8（缺这一步时 DSH 只会报 server unavailable）。
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

BOT_ENTRY_ID = 'mcp-whale-bot'
BOT_SERVER_NAME = 'whale_bot'
UTF8_FIX_FUNCTION = 'force_utf8_stdio'


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


def status(home=None, profile='desktop'):
    home = Path(home or os.getenv('DSH_HOME') or (Path.home() / '.dsh'))
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

    problems = []
    if not control:
        problems.append('cordis.patch.yml 里没有 ' + BOT_ENTRY_ID + ' 条目')
    else:
        if not bot_command or not Path(bot_command).is_file():
            problems.append('MCP command 不存在：' + (bot_command or '(空)'))
        if not bot_script or not Path(bot_script).is_file():
            problems.append('MCP 脚本不存在：' + (bot_script or '(空)'))
        if not names:
            problems.append('MCP 脚本里读不到 TOOLS 工具清单')
        if not (utf8_fixed or utf8_env):
            problems.append('MCP 未锁 UTF-8（中文 Windows 下 DSH 会报 server unavailable）')

    return {
        'dsh_home': str(home),
        'profile': str(profile),
        'profile_exists': profile_dir.is_dir(),
        'patch_exists': patch.is_file(),
        'memory_bridge': bool(memory),
        'bot_bridge': bool(control),
        'bot_registered': bool(control),
        'bot_server_name': str(control_config.get('serverName') or ''),
        'bot_url': str(control_env.get('WHALE_BOT_URL') or ''),
        'bot_command': bot_command,
        'bot_command_exists': bool(bot_command) and Path(bot_command).is_file(),
        'bot_script': bot_script,
        'bot_script_exists': bool(bot_script) and Path(bot_script).is_file(),
        'bot_tool_count': len(names),
        'bot_tool_names': names,
        'bot_stdio_utf8': utf8_fixed,
        'bot_utf8_env': utf8_env,
        'bot_ready': not problems,
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
