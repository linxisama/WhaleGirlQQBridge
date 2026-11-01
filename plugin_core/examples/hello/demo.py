"""hello 示例最小复现：一条 mock 消息 → 一句固定回复。

在仓库根执行：python plugin_core/examples/hello/demo.py
"""

import json
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
        record = host.handle(MockSource().message("你好"))
        print("仲裁记录：", json.dumps(record, ensure_ascii=False))
        print("发出去：", host.transport.sent_texts())
        print("KV：", host.store.kv_data("hello"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
