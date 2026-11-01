"""测试夹具：造插件目录、造 Host、造消息。"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from plugin_core import Host
from plugin_core.mock import MockModel, MockSource, MockTransport

BASE_MANIFEST = {
    "id": "inline",
    "name": "内联插件",
    "version": "1.0.0",
    "api_version": 1,
    "author": {"name": "unittest", "contact": "unittest@example.invalid"},
    "description": "unittest 内联插件",
    "entry": "plugin.py:setup",
    "permissions": ["read_message", "send_message", "store"],
    "capabilities": [{"stage": "inbound", "produces": "main", "priority": 50}],
    "side": "both",
    "data_policy": "retain",
    "upstream": {"protocol": "mock", "kernel": "plugin_core", "range": ">=1.0.0 <2.0.0"},
    "native": True,
    "storage": {},
    "config_schema": {},
}


def manifest(plugin_id, **overrides):
    data = dict(BASE_MANIFEST)
    data.update(overrides)
    data["id"] = plugin_id
    data["name"] = overrides.get("name", plugin_id)
    return data


def write_plugin(plugins_dir, plugin_id, source, **overrides):
    """造一个插件目录；source 为 None 表示只写 manifest。"""
    target = Path(plugins_dir) / plugin_id
    target.mkdir(parents=True, exist_ok=True)
    payload = manifest(plugin_id, **overrides)
    (target / "plugin.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if source is not None:
        (target / "plugin.py").write_text(source, encoding="utf-8")
    return target


MAIN_SOURCE = '''from plugin_core.sdk import Action


def setup(api) -> None:
    """最小主体插件：任意消息都竞标 main。"""

    def generate(ctx):
        return [Action.send_text(ctx.session["key"], "@TEXT@")]

    def bid(ctx):
        return api.bid("main", @PRIORITY@, generate=generate, reason="测试主体")

    api.declare_inbound(bid)
'''

ATTACH_SOURCE = '''from plugin_core.sdk import Action


def setup(api) -> None:
    """附加插件：只在主体胜出后追加一条消息。"""

    def generate(ctx):
        return [Action.send_text(ctx.session["key"], "@TEXT@")]

    def bid(ctx):
        return api.bid("attach", @PRIORITY@, generate=generate, reason="附加")

    api.declare_inbound(bid)
'''

FAILING_ATTACH_SOURCE = '''def setup(api) -> None:
    """附加插件：生成阶段必炸，用于验证失败隔离。"""

    def generate(ctx):
        raise RuntimeError("附加插件故意失败")

    def bid(ctx):
        return api.bid("attach", @PRIORITY@, generate=generate, reason="会失败的附加")

    api.declare_inbound(bid)
'''

SILENT_SOURCE = '''def setup(api) -> None:
    """沉默插件：抢到最高优先级后本轮什么都不发。"""

    def bid(ctx):
        return api.bid("silent", @PRIORITY@, reason="我在场就不该回")

    api.declare_inbound(bid)
'''

OUTBOUND_SOURCE = '''from plugin_core.sdk import Draft


def setup(api) -> None:
    """出站中间件：在正文后追加标记。"""

    def middleware(ctx, draft):
        return Draft(draft.conversation, draft.text + "@MARK@", draft.media, "mw")

    api.declare_outbound(middleware)
'''

OUTBOUND_CANCEL_SOURCE = '''def setup(api) -> None:
    """出站中间件：无条件取消，用于验证短路。"""

    def middleware(ctx, draft):
        return None

    api.declare_outbound(middleware)
'''

OUTBOUND_FAIL_SOURCE = '''def setup(api) -> None:
    """出站中间件：抛异常，用于验证隔离。"""

    def middleware(ctx, draft):
        raise RuntimeError("中间件故意失败")

    api.declare_outbound(middleware)
'''


def main_source(text, priority):
    return MAIN_SOURCE.replace("@TEXT@", text).replace("@PRIORITY@", str(priority))


def attach_source(text, priority):
    return ATTACH_SOURCE.replace("@TEXT@", text).replace("@PRIORITY@", str(priority))


def failing_attach_source(priority):
    return FAILING_ATTACH_SOURCE.replace("@PRIORITY@", str(priority))


def silent_source(priority):
    return SILENT_SOURCE.replace("@PRIORITY@", str(priority))


def outbound_source(mark):
    return OUTBOUND_SOURCE.replace("@MARK@", mark)


def main_capability(priority, **extra):
    capability = {"stage": "inbound", "produces": "main", "priority": priority}
    capability.update(extra)
    return capability


def attach_capability(priority):
    return {
        "stage": "inbound",
        "produces": "attach",
        "priority": priority,
        "requires_main": True,
    }


def silent_capability(priority):
    return {"stage": "inbound", "produces": "silent", "priority": priority}


def outbound_capability(priority, produces="rewrite"):
    return {"stage": "outbound", "produces": produces, "priority": priority}


class KernelTestCase(unittest.TestCase):
    """每个测试一份独立临时目录：plugins/ 与 data/。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="plugin-core-test-"))
        self.plugins = self.tmp / "plugins"
        self.plugins.mkdir()
        self.data = self.tmp / "data"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build_host(self, *, transport=None, model=None, **kwargs):
        transport = MockTransport() if transport is None else transport
        model = MockModel("模型回复") if model is None else model
        host = Host(self.plugins, self.data, transport, model, **kwargs)
        host.start()
        return host

    def message(self, text="测试消息"):
        return MockSource().message(text)
