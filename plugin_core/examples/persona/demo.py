"""persona 示例复现：命中触发词才竞标，胜出后调模型回答。

在仓库根执行：python plugin_core/examples/persona/demo.py
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
        model = MockModel("我在这里，你需要什么？")
        host = Host(plugins, pathlib.Path(tmp) / "data", MockTransport(), model)
        host.start()
        for text in ("你好", "今天天气如何"):
            record = host.handle(MockSource().message(text))
            print("%r → winner=%s sent=%s"
                  % (text, (record["winner"] or {}).get("plugin"),
                     [item["text"] for item in record["sent"]]))
        print("模型收到的 prompt：", json.dumps(model.last_prompt(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
