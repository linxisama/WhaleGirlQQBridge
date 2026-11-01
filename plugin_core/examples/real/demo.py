"""real 示例复现：命中的走回复，没命中的不说话。

在仓库根执行：python plugin_core/examples/real/demo.py
"""

import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from plugin_core import Host, MockModel, MockSource, MockTransport  # noqa: E402


def main() -> int:
    here = pathlib.Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory() as tmp:
        plugins = pathlib.Path(tmp) / "plugins"
        plugins.mkdir()
        shutil.copytree(here, plugins / here.name)
        host = Host(plugins, pathlib.Path(tmp) / "data", MockTransport(), MockModel())
        host.start()
        for text in ("在吗", "价格多少", "今天吃什么"):
            record = host.handle(MockSource().message(text))
            print("%-8s → %s" % (text, [item["text"] for item in record["sent"]] or "（不说话）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
