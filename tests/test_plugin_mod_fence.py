"""mod 运行时围栏：只读门面、钩子超时隔离、连续失败自愈、on_ready 通道管控。"""
import asyncio
import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from src.plugins.manager import PluginManager

LOG_SRC = '''import json
import pathlib

_LOG = pathlib.Path(__file__).with_name("hook_log.jsonl")


def _log(**fields):
    with _LOG.open("a", encoding="utf-8") as handle:
        print(json.dumps(fields, ensure_ascii=False), file=handle)
'''

FACADE_SRC = LOG_SRC + '''
from . import PluginBase, PluginMeta


class Facade(PluginBase):
    META = PluginMeta(id="__PID__", name="门面探针", category="utility",
                      description="x", default_settings={"greeting": "hi"})

    def on_ingest(self, message, runtime):
        blocked = []
        for name in ("bot", "plugins", "config", "db", "_plugins", "send", "call_ai"):
            try:
                getattr(runtime, name)
            except AttributeError:
                blocked.append(name)
        assigned = False
        try:
            runtime.settings = {}
        except AttributeError:
            assigned = True
        _log(kind="facade", type=type(runtime).__name__, blocked=blocked,
             assigned_rejected=assigned, mod_id=runtime.mod_id,
             api=runtime.api_version, settings=runtime.settings,
             greeting=runtime.get("greeting"), missing=runtime.get("missing", "fallback"),
             network=runtime.has_permission("network"),
             manage=runtime.has_permission("manage_settings"),
             now=type(runtime.now()).__name__)
        runtime.log("门面日志")
        runtime.note("门面备注")
        return None
'''

SLOW_SRC = '''
import time
from . import PluginBase, PluginMeta


class Slow(PluginBase):
    META = PluginMeta(id="__PID__", name="慢钩子", category="utility", description="x")

    def on_ingest(self, message, runtime):
        time.sleep(3)
        return "慢钩子的回复不该到达"
'''

SLOW_ASYNC_SRC = '''
import asyncio
from . import PluginBase, PluginMeta


class SlowAsync(PluginBase):
    META = PluginMeta(id="__PID__", name="慢异步钩子", category="utility", description="x")

    async def on_prepare(self, message, runtime):
        await asyncio.sleep(3)
'''

FAST_SRC = LOG_SRC + '''
from . import PluginBase, PluginMeta


class Fast(PluginBase):
    META = PluginMeta(id="__PID__", name="快插件", category="utility", description="x")

    def on_ingest(self, message, runtime):
        _log(kind="fast")
        return None

    async def on_prepare(self, message, runtime):
        _log(kind="prepared")
'''

BOOM_SRC = '''
from . import PluginBase, PluginMeta


class Boom(PluginBase):
    META = PluginMeta(id="__PID__", name="炸钩子", category="utility",
                      description="x", default_settings={"mode": "fail"})

    def on_ingest(self, message, runtime):
        if self.get("mode") == "ok":
            return None
        raise RuntimeError("boom")
'''

READY_SRC = '''
from . import PluginBase, PluginMeta


class Ready(PluginBase):
    META = PluginMeta(id="__PID__", name="代执行探针", category="utility", description="x")

    def on_pre_ai(self, message, conversation, runtime):
        return {"extra_system": "MOD_EXTRA", "on_ready": lambda: None}
'''

LEGACY_SRC = '''
from . import PluginBase, PluginMeta


class Legacy(PluginBase):
    META = PluginMeta(id="__PID__", name="老式插件", category="utility", description="x")
    seen = None

    def on_ingest(self, message, runtime):
        type(self).seen = runtime
        return None

    def on_pre_ai(self, message, conversation, runtime):
        return {"extra_system": "LEGACY_EXTRA", "on_ready": lambda: None}
'''


class _FakeRuntime:
    """假 runtime：有 note，也有 mod 不该摸到的内部成员（用来验证门面确实挡住了）。"""

    def __init__(self):
        self.notes = []
        self.bot = object()
        self.plugins = object()
        self.config = object()
        self.db = object()

    def note(self, text):
        self.notes.append(text)

    def send(self, *args, **kwargs):
        raise AssertionError("mod 不该拿到能发消息的 runtime")


class ModFenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.plugins_dir = self.root / "custom_plugins"
        self.plugins_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def add_mod(self, mod_id, source, manifest=None):
        folder = self.plugins_dir / mod_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "mod.py").write_text(source.replace("__PID__", mod_id), encoding="utf-8")
        data = {"id": mod_id, "api_version": 1, "entry": "mod.py"}
        data.update(manifest or {})
        (folder / "plugin.json").write_text(json.dumps(data), encoding="utf-8")
        return folder

    def add_bare(self, pid, source):
        (self.plugins_dir / (pid + ".py")).write_text(
            source.replace("__PID__", pid), encoding="utf-8")
        (self.root / "custom_plugins.json").write_text(
            json.dumps({"plugins": [pid]}), encoding="utf-8")
        return pid

    def hook_log(self, mod_id):
        path = self.plugins_dir / mod_id / "hook_log.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text("utf-8").splitlines()
                if line.strip()]

    def audit(self):
        path = self.root / "qq-only" / "plugin_audit.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text("utf-8").splitlines()
                if line.strip()]

    def audit_of(self, event):
        return [e for e in self.audit() if e["event"] == event]

    def probe_facade(self, pid="facade_mod", permissions=("network", "manage_settings")):
        self.add_mod(pid, FACADE_SRC, {"permissions": list(permissions)})
        manager = PluginManager(self.root)
        runtime = _FakeRuntime()
        manager.run_on_ingest({"content": "hi"}, runtime)
        entries = self.hook_log(pid)
        self.assertEqual(len(entries), 1, self.hook_log(pid))
        return manager, runtime, entries[0]

    # ── B. 只读门面 ────────────────────────────────────────────────────────
    def test_mod_hook_receives_facade_not_live_runtime(self):
        manager, runtime, entry = self.probe_facade()
        self.assertEqual(entry["type"], "ModRuntime")
        self.assertEqual(entry["mod_id"], "facade_mod")
        self.assertEqual(entry["api"], 1)
        self.assertIn("门面备注", runtime.notes)

    def test_facade_blocks_internal_members(self):
        manager, runtime, entry = self.probe_facade()
        self.assertEqual(entry["blocked"],
                         ["bot", "plugins", "config", "db", "_plugins", "send", "call_ai"])

    def test_facade_allows_documented_members(self):
        manager, runtime, entry = self.probe_facade()
        self.assertEqual(entry["settings"], {"greeting": "hi"})
        self.assertEqual(entry["greeting"], "hi")
        self.assertEqual(entry["missing"], "fallback")
        self.assertTrue(entry["network"])
        self.assertTrue(entry["manage"])
        self.assertEqual(entry["now"], "float")

    def test_facade_rejects_assignment(self):
        manager, runtime, entry = self.probe_facade()
        self.assertTrue(entry["assigned_rejected"])

    # ── B. 钩子超时隔离 ─────────────────────────────────────────────────────
    def test_sync_hook_timeout_is_isolated(self):
        self.add_mod("aaa_slow", SLOW_SRC)
        self.add_mod("zzz_fast", FAST_SRC)
        manager = PluginManager(self.root)
        runtime = _FakeRuntime()
        with mock.patch("src.plugins.manager.MOD_HOOK_TIMEOUT", 0.2):
            started = time.time()
            reply = manager.run_on_ingest({"content": "hi"}, runtime)
            elapsed = time.time() - started
        self.assertIsNone(reply)
        self.assertLess(elapsed, 1.5)
        self.assertEqual([e["kind"] for e in self.hook_log("zzz_fast")], ["fast"])
        timeouts = self.audit_of("hook-timeout")
        self.assertEqual([e["plugin"] for e in timeouts], ["aaa_slow"])
        self.assertEqual(timeouts[0]["hook"], "on_ingest")

    def test_async_hook_timeout_is_isolated(self):
        self.add_mod("aaa_slow_async", SLOW_ASYNC_SRC)
        self.add_mod("zzz_fast_async", FAST_SRC)
        manager = PluginManager(self.root)
        runtime = _FakeRuntime()
        with mock.patch("src.plugins.manager.MOD_HOOK_TIMEOUT", 0.2):
            started = time.time()
            asyncio.run(manager.run_on_prepare({"content": "hi"}, runtime))
            elapsed = time.time() - started
        self.assertLess(elapsed, 1.5)
        self.assertEqual([e["kind"] for e in self.hook_log("zzz_fast_async")], ["prepared"])
        self.assertEqual([e["plugin"] for e in self.audit_of("hook-timeout")],
                         ["aaa_slow_async"])

    # ── C. 坏插件自愈 ───────────────────────────────────────────────────────
    def test_three_consecutive_failures_auto_disable_and_persist(self):
        self.add_mod("boom_mod", BOOM_SRC)
        manager = PluginManager(self.root)
        runtime = _FakeRuntime()
        for _ in range(3):
            manager.run_on_ingest({"content": "hi"}, runtime)
        self.assertFalse(manager._plugins["boom_mod"].enabled)
        stored = json.loads((self.root / "plugin_settings.json").read_text("utf-8"))
        self.assertFalse(stored["boom_mod"]["enabled"])
        self.assertEqual([e["plugin"] for e in self.audit_of("auto-disabled")], ["boom_mod"])
        self.assertIn("自动禁用", "\n".join(manager.take_load_problems()))

    def test_success_resets_failure_counter(self):
        self.add_mod("boom_mod", BOOM_SRC)
        manager = PluginManager(self.root)
        runtime = _FakeRuntime()
        plugin = manager._plugins["boom_mod"]
        manager.run_on_ingest({"content": "hi"}, runtime)
        manager.run_on_ingest({"content": "hi"}, runtime)
        self.assertEqual(manager._hook_failures.get("boom_mod"), 2)
        manager._settings["boom_mod"]["mode"] = "ok"
        manager.run_on_ingest({"content": "hi"}, runtime)
        self.assertNotIn("boom_mod", manager._hook_failures)
        manager._settings["boom_mod"]["mode"] = "fail"
        manager.run_on_ingest({"content": "hi"}, runtime)
        manager.run_on_ingest({"content": "hi"}, runtime)
        self.assertEqual(manager._hook_failures.get("boom_mod"), 2)
        self.assertTrue(plugin.enabled)
        self.assertEqual(self.audit_of("auto-disabled"), [])

    def test_mod_failure_writes_hook_failed_audit_fields(self):
        self.add_mod("boom_mod", BOOM_SRC)
        manager = PluginManager(self.root)
        manager.run_on_ingest({"content": "hi"}, _FakeRuntime())
        failed = self.audit_of("hook-failed")
        self.assertEqual(len(failed), 1)
        record = failed[0]
        self.assertEqual(record["plugin"], "boom_mod")
        self.assertEqual(record["hook"], "on_ingest")
        self.assertEqual(record["code"], "exception")
        self.assertIn("RuntimeError: boom", record["detail"])
        for field in ("ts", "plugin", "event", "hook", "code", "detail"):
            self.assertIn(field, record)

    def test_mod_failure_does_not_stop_other_plugins(self):
        self.add_mod("aaa_boom", BOOM_SRC)
        self.add_mod("zzz_fast", FAST_SRC)
        manager = PluginManager(self.root)
        reply = manager.run_on_ingest({"content": "hi"}, _FakeRuntime())
        self.assertIsNone(reply)
        self.assertEqual([e["kind"] for e in self.hook_log("zzz_fast")], ["fast"])

    # ── B. on_ready 通道 + 内置插件不受影响 ─────────────────────────────────
    def test_on_ready_is_stripped_from_mod_hint(self):
        self.add_mod("ready_mod", READY_SRC)
        manager = PluginManager(self.root)
        hint = manager.run_on_pre_ai({"content": "hi"}, [], _FakeRuntime())
        self.assertIn("MOD_EXTRA", hint["extra_system"])
        self.assertIsNone(hint["on_ready"])
        self.assertEqual([e["plugin"] for e in self.audit_of("on-ready-stripped")],
                         ["ready_mod"])

    def test_builtin_plugins_keep_live_runtime(self):
        manager = PluginManager(self.root)
        runtime = _FakeRuntime()
        checked = 0
        for pid, plugin in manager._plugins.items():
            if manager._is_mod(plugin):
                continue
            self.assertIs(manager._hook_runtime(plugin, runtime), runtime)
            checked += 1
        self.assertGreaterEqual(checked, 60)

    def test_bare_py_plugin_keeps_live_runtime_and_on_ready(self):
        self.add_bare("legacy_live", LEGACY_SRC)
        manager = PluginManager(self.root)
        runtime = _FakeRuntime()
        manager.run_on_ingest({"content": "hi"}, runtime)
        self.assertIs(manager._plugins["legacy_live"].seen, runtime)
        hint = manager.run_on_pre_ai({"content": "hi"}, [], runtime)
        self.assertIn("LEGACY_EXTRA", hint["extra_system"])
        self.assertTrue(callable(hint["on_ready"]))
        self.assertEqual(self.audit_of("on-ready-stripped"), [])


if __name__ == "__main__":
    unittest.main()
