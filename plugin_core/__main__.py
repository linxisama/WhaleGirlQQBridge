"""支持 python -m plugin_core <子命令>，等价于 python -m plugin_core.cli。"""

import sys

from .cli import main as cli_main

if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
