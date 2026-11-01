"""filter 示例复现：改写与取消两条路径。

在仓库根执行：python plugin_core/examples/filter/demo.py
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
        # 让主体自己回话，再看管道把话改成什么
        host = Host(plugins, pathlib.Path(tmp) / "data", MockTransport(), MockModel("这条包含内测字样"))
        host.start()
        record = host.handle(MockSource().message("说点什么"))
        print("改写后：", host.transport.sent_texts())
        print("管道执行次数：", host.outbound.passes)
        print("仲裁记录 sent：", record["sent"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
