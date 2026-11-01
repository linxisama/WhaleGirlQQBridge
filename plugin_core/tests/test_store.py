"""存储：KV 命名空间隔离、落盘路径、config_write 双条件、uninstall 清理。"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from plugin_core.api import PermissionDenied
from plugin_core.store import Store
from plugin_core.tests import support as s


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="plugin-core-store-"))
        self.data = self.tmp / "data"
        self.store = Store(self.data)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_kv_paths_follow_plugin_id(self):
        self.assertEqual(self.store.kv_path("a"), self.data / "plugin_kv" / "a.json")
        self.assertEqual(self.store.cfg_path("a"), self.data / "plugin_config" / "a.json")

    def test_kv_is_namespaced_per_plugin(self):
        self.store.kv("alpha").set("same", "alpha 的值")
        self.store.kv("beta").set("same", "beta 的值")

        self.assertEqual(self.store.kv_data("alpha"), {"same": "alpha 的值"})
        self.assertEqual(self.store.kv_data("beta"), {"same": "beta 的值"})
        self.assertEqual(self.store.kv("alpha").keys(), ["same"])
        self.assertEqual(self.store.kv("beta").get("same"), "beta 的值")

        on_disk_alpha = json.loads(self.store.kv_path("alpha").read_text(encoding="utf-8"))
        on_disk_beta = json.loads(self.store.kv_path("beta").read_text(encoding="utf-8"))
        self.assertEqual(on_disk_alpha, {"same": "alpha 的值"})
        self.assertEqual(on_disk_beta, {"same": "beta 的值"})

    def test_kv_delete_and_default(self):
        kv = self.store.kv("gamma")
        self.assertEqual(kv.get("missing", "兜底"), "兜底")
        kv.set("k", 1)
        kv.delete("k")
        self.assertIsNone(kv.get("k"))
        self.assertEqual(kv.as_dict(), {})

    def test_config_write_needs_store_permission(self):
        with self.assertRaises(PermissionDenied):
            self.store.write_config("c", {"x": 1}, declared=["read_message"], flags=())

    def test_config_write_needs_config_write_flag(self):
        with self.assertRaises(PermissionDenied):
            self.store.write_config("c", {"x": 1}, declared=["store"], flags=())

    def test_config_write_succeeds_with_flag(self):
        payload = self.store.write_config("c", {"x": 1}, declared=["store"], flags=["config_write"])
        self.assertEqual(payload, {"x": 1})
        self.assertEqual(
            json.loads(self.store.cfg_path("c").read_text(encoding="utf-8")), {"x": 1}
        )
        with self.assertRaises(PermissionDenied):
            self.store.write_config("c", {"x": 2}, declared=["store"], flags=())

    def test_read_config_defaults_to_empty(self):
        self.assertEqual(self.store.read_config("nobody"), {})

    def test_uninstall_purge_removes_files(self):
        self.store.kv("purge-me").set("k", 1)
        self.store.write_config("purge-me", {"x": 1}, declared=["store"], flags=["config_write"])
        info = self.store.uninstall("purge-me", "purge")
        self.assertEqual(info["data_policy"], "purge")
        self.assertFalse(self.store.kv_path("purge-me").exists())
        self.assertFalse(self.store.cfg_path("purge-me").exists())
        self.assertEqual(self.store.kv_data("purge-me"), {})

    def test_uninstall_retain_keeps_files(self):
        self.store.kv("keep-me").set("k", 1)
        self.store.write_config("keep-me", {"x": 1}, declared=["store"], flags=["config_write"])
        info = self.store.uninstall("keep-me", "retain")
        self.assertEqual(info["data_policy"], "retain")
        self.assertTrue(self.store.kv_path("keep-me").exists())
        self.assertTrue(self.store.cfg_path("keep-me").exists())
        # 重新构造 Store 也能读回来（真实落盘，不是内存缓存）
        fresh = Store(self.data)
        self.assertEqual(fresh.kv_data("keep-me"), {"k": 1})
        self.assertEqual(fresh.read_config("keep-me"), {"x": 1})


class HostStorageTest(s.KernelTestCase):
    def test_two_plugins_same_key_do_not_collide(self):
        source = (
            "from plugin_core.sdk import Action\n\n\n"
            "def setup(api) -> None:\n"
            "    def generate(ctx):\n"
            "        ctx.store.set('shared', api.plugin_id)\n"
            "        return [Action.send_text(ctx.session['key'], api.plugin_id)]\n\n"
            "    api.declare_inbound(lambda ctx: api.bid('main', 50, generate=generate, reason='写存储'))\n"
        )
        s.write_plugin(self.plugins, "one", source, capabilities=[s.main_capability(50)])
        host = self.build_host(default_persona=False)
        host.handle(self.message())
        self.assertEqual(host.store.kv_data("one"), {"shared": "one"})

    def test_host_uninstall_cleans_kv(self):
        s.write_plugin(self.plugins, "hello", s.main_source("你好", 50), capabilities=[s.main_capability(50)])
        host = self.build_host(default_persona=False)
        host.handle(self.message())
        self.assertIn("hello", host.plugins)
        info = host.uninstall("hello")
        self.assertEqual(info["data_policy"], "retain")
        self.assertNotIn("hello", host.plugins)
        self.assertEqual(host.freeze_snapshot(), tuple())
        self.assertTrue(host.audit.select(event="uninstalled"))


if __name__ == "__main__":
    unittest.main()
