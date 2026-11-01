"""安装版内置的 stdio MCP 宿主分派。

冻结产物里没有源码仓库，也没有外部 Python；同一个控制台 exe 用
--mcp memory / bot / tools 分别冒充三个桥。这里只做 import 与分派，
Qt、单实例和启动诊断都还没进来。
"""

import sys


USAGE = 'usage: WhaleGirlQQBridge.exe --mcp memory|bot|tools|check'


def run(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != '--mcp':
        print(USAGE, file=sys.stderr)
        return 2
    kind = args[1] if len(args) > 1 else ''
    rest = args[2:]
    if kind == 'memory':
        from tools import whale_memory_mcp
        return whale_memory_mcp.main()
    if kind == 'bot':
        from tools import whale_bot_mcp
        return whale_bot_mcp.main()
    if kind == 'tools':
        from tools import whale_tools_mcp
        return whale_tools_mcp.main(rest)
    if kind == 'check':
        # 自检只 import 三个模块：它们能进来，DSH 拉起时就不会缺依赖。
        from tools import whale_memory_mcp, whale_bot_mcp, whale_tools_mcp
        _ = (whale_memory_mcp, whale_bot_mcp, whale_tools_mcp)
        print('ok')
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(run())
