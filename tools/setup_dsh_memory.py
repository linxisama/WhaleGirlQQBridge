#!/usr/bin/env python3
"""注册 / 卸载「鲸鱼娘 MCP」三个桥到本地 DSH：记忆桥 + 控制桥 + 工具桥。

用法::

    python tools/setup_dsh_memory.py                 # 一条命令登记三条（默认包含工具桥）
    python tools/setup_dsh_memory.py --status
    python tools/setup_dsh_memory.py --remove         # 三条一起撤
    python tools/setup_dsh_memory.py --remove-tools   # 只撤工具桥 whale_tools
    python tools/setup_dsh_memory.py --no-tools       # 不登记工具桥，只写记忆桥/控制桥
    python tools/setup_dsh_memory.py --profile web
    python tools/setup_dsh_memory.py --dry-run

安装后必须完全退出 DSH 桌面版并重新启动（至少新开一个会话），模型才会多出
``mcp__whale_memory__*``、``mcp__whale_bot__*`` 与 ``mcp__whale_tools__*`` 工具；
MCP client 只在会话创建时拉起一次，启动失败不重试、也没有热加载，老会话不会自己补上这些工具。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import bot_control_api as control_api
from src import dsh_memory_bridge as bridge


def default_bot_url() -> str:
    """此刻解析到的控制台地址（只用于展示）：WHALE_BOT_URL > WHALEGIRL_PORT > 端点文件 > 27499。

    与 src/bot_control_api.base_url() 同一口径。注意：它**不进** profile —— 写死地址
    等于把"当时算出来的端口"冻在登记里，之后改端口必失效。默认留空由运行期解析。
    """
    return control_api.base_url()


def written_bot_url(explicit=None) -> str:
    """要写进 profile env 的 WHALE_BOT_URL：只有用户显式 --bot-url 时才有值。

    留空字符串是有意的：MCP 子进程拿到空值会当"没设"，转去运行期解析
    （base_url() 里 "取值 or 下一级" 的短路），而 profile 里残留的旧地址
    会被这次重写整条替换掉。
    """
    return str(explicit or '').strip()


def default_bot_home() -> Path:
    explicit = os.environ.get('WHALE_QQ_HOME')
    if explicit:
        return Path(explicit)
    local = os.environ.get('LOCALAPPDATA', '')
    return Path(local) / 'WhaleGirlQQ' if local else ROOT


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='把鲸鱼娘记忆、控制、工具三个 MCP 桥注册到本地 DSH')
    parser.add_argument('--profile', default='desktop', help='DSH profile 名（默认 desktop）')
    parser.add_argument('--dsh-home', default=str(bridge.default_dsh_home()), help='DSH 数据目录')
    parser.add_argument('--bot-home', default=str(default_bot_home()), help='机器人数据目录')
    parser.add_argument('--python', default=sys.executable, help='MCP 使用的 Python')
    parser.add_argument('--script', default=str(ROOT / 'tools' / 'whale_memory_mcp.py'))
    parser.add_argument('--bot-script', default=str(ROOT / 'tools' / 'whale_bot_mcp.py'))
    parser.add_argument('--tools-script', default=str(ROOT / 'tools' / 'whale_tools_mcp.py'),
                        help='工具桥 MCP 脚本（默认 tools/whale_tools_mcp.py）')
    parser.add_argument('--bot-url', default=None,
                        help='把控制台地址写死进 profile env（默认不写：留空由 MCP 运行期'
                             '跟随控制台端点文件 / WHALEGIRL_PORT 解析，改端口不用重跑登记）')
    parser.add_argument('--no-bot', action='store_true', help='只装记忆桥，不装机器人控制')
    parser.add_argument('--bot-only', action='store_true', help='只装机器人控制，不装记忆桥')
    parser.add_argument('--no-tools', action='store_true', help='不登记工具桥 whale_tools')
    parser.add_argument('--status', action='store_true', help='只看当前状态')
    parser.add_argument('--dry-run', action='store_true', help='只打印将写入的内容')
    remove = parser.add_mutually_exclusive_group()
    remove.add_argument('--remove', action='store_true', help='三条登记一起撤')
    remove.add_argument('--remove-tools', action='store_true',
                        help='只撤工具桥（id=mcp-whale-tools），记忆桥/控制桥保留')
    args = parser.parse_args(argv)

    # 工具桥跟着记忆桥走：--no-tools 或 --bot-only 时不登记第三条。
    tools_on = not args.bot_only and not args.no_tools

    if args.status:
        print(json.dumps(bridge.status(args.dsh_home, args.profile), ensure_ascii=False, indent=2))
        return 0
    if args.remove_tools:
        print(json.dumps(bridge.remove(args.dsh_home, args.profile, only=bridge.TOOLS_ENTRY_ID),
                         ensure_ascii=False, indent=2))
        return 0
    if args.remove:
        print(json.dumps(bridge.remove(args.dsh_home, args.profile), ensure_ascii=False, indent=2))
        return 0
    bot_url = written_bot_url(args.bot_url)
    if args.dry_run:
        entries = []
        if not args.bot_only:
            entries.append(bridge.build_entry(args.python, args.script, args.bot_home))
        if not args.no_bot:
            entries.append(bridge.build_control_entry(args.python, args.bot_script, bot_url))
        if tools_on:
            entries.append(bridge.build_tools_entry(args.python, args.tools_script, args.bot_home))
        # 两行分开看：写进 profile 的 env（可能为空 = 运行期解析）和此刻会解析到的地址。
        print(json.dumps({'path': str(bridge.profile_path(args.dsh_home, args.profile)),
                          'bot_url': bot_url,
                          'resolved_bot_url': default_bot_url(),
                          'entries': entries}, ensure_ascii=False, indent=2))
        return 0
    want_memory = not args.bot_only
    want_control = not args.no_bot
    results = []
    if want_memory and want_control:
        # 记忆桥和控制桥都在时用 install_all：三条共用一次备份，重复运行不落新备份。
        results.append(bridge.install_all(args.dsh_home, args.python, args.script, args.bot_home,
                                          args.bot_script, bot_url, args.profile,
                                          tools_script=args.tools_script if tools_on else None))
    else:
        if want_memory:
            results.append(bridge.install(args.dsh_home, args.python, args.script,
                                          args.bot_home, args.profile))
        if want_control:
            results.append(bridge.install_control(args.dsh_home, args.python, args.bot_script,
                                                  bot_url, args.profile))
        if tools_on:
            results.append(bridge.install_tools(args.dsh_home, args.python, args.tools_script,
                                                args.bot_home, args.profile))
    print(json.dumps(results, ensure_ascii=False, indent=2))
    names = []
    if not args.bot_only:
        names.append('mcp__whale_memory__*')
    if not args.no_bot:
        names.append('mcp__whale_bot__*')
    if tools_on:
        names.append('mcp__whale_tools__*')
    print('\n已写入。要完全退出 DSH 桌面版再启动（至少新开一个会话）才能看到 '
          + ' 与 '.join(names) +
          ' 工具：MCP client 只在会话创建时拉起一次，失败不重试、没有热加载，'
          '老会话不会自己出现这些工具。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
