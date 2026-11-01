"""examples/mod-keyword-reply 目录 mod 样例：装 / 跑 / 禁 / 摘 四步端到端。

测试全程用临时 data_dir，不碰真实数据目录：
每个用例把样例目录整份拷进 <tmp>/custom_plugins/mod-keyword-reply，再走真管理器。
"""
import json
import pathlib
import shutil
import tempfile
import unittest

from src.plugins.manager import PluginManager, PLUGIN_MODULES

EXAMPLE_DIR = (pathlib.Path(__file__).resolve().parents[1]
               / "examples" / "mod-keyword-reply")
MOD_ID = "mod-keyword-reply"
RULES = "价格=100块，包邮\n发货=今晚就发"
HIT = "这个价格多少？"        # 命中「价格」
MISS = "今天天气不错"          # 谁都不命中


class _Runtime:
    """最小 runtime：mod 拿到的是 ModRuntime 门面，这里给内置插件与 note 兜底。"""

    def __init__(self):
        self.notes = []

    def note(self, text):
        self.notes.append(str(text))


class ModKeywordReplyExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 本机解释器没有 httpx 时，内置 quick_menu/pixiv_illust 等 7 个插件必然加载失败，
        # take_load_problems() 天生不空。这里取「一个 mod 都没装」的基线做对照，
        # 断言仍然成立：装了 mod 之后的问题集合与基线逐字相同、且没有一条提到 mod。
        with tempfile.TemporaryDirectory() as other:
            cls.baseline_problems = set(PluginManager(pathlib.Path(other)).take_load_problems())

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.plugins_dir = self.root / "custom_plugins"
        self.plugins_dir.mkdir()
        self.mod_dir = self.plugins_dir / MOD_ID
        self.runtime = _Runtime()

    def tearDown(self):
        self._tmp.cleanup()

    # ── 工具 ────────────────────────────────────────────────────────────────
    def install(self):
        """把样例目录整份拷到 <data_dir>/custom_plugins/<id>/，再起一个真管理器。"""
        shutil.copytree(EXAMPLE_DIR, self.mod_dir)
        return PluginManager(self.root)

    def send(self, manager, text):
        return manager.run_on_ingest({"text": text, "chat_id": "g1", "sender_id": "u1"},
                                     self.runtime)

    def plugin_ids(self, manager):
        return [row["id"] for row in manager.list_plugins()]

    def mod_problems(self, manager):
        return [p for p in manager.take_load_problems() if MOD_ID in p]

    def audit_events(self):
        path = self.root / "qq-only" / "plugin_audit.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text("utf-8").splitlines()
                if line.strip()]

    # ── ① 装 ────────────────────────────────────────────────────────────────
    def test_step1_install_mounts_from_directory(self):
        self.assertTrue((EXAMPLE_DIR / "plugin.json").is_file(), "样例目录缺 plugin.json")
        manager = self.install()

        self.assertIn(MOD_ID, manager._plugins)
        self.assertIn(MOD_ID, self.plugin_ids(manager))
        problems = manager.take_load_problems()
        self.assertEqual([p for p in problems if MOD_ID in p], [])
        self.assertEqual(set(problems), self.baseline_problems)
        # 目录本身就是挂载点：不需要 register_custom，也没进自定义清单。
        self.assertNotIn(MOD_ID, manager.custom_plugin_ids())
        self.assertNotIn(MOD_ID, PLUGIN_MODULES)
        mounted = [e for e in self.audit_events() if e["event"] == "mounted"]
        self.assertEqual([e["plugin"] for e in mounted], [MOD_ID])
        self.assertIn("entry=keyword_reply.py", mounted[0]["detail"])
        # 样例自己的默认值（manifest enabled_by_default=false）生效
        self.assertEqual(manager.get_settings(MOD_ID), {"enabled": False, "rules": ""})
        self.assertFalse(manager._plugins[MOD_ID].enabled)

    # ── ② 跑 ────────────────────────────────────────────────────────────────
    def test_step2_run_returns_reply_on_match_and_none_on_miss(self):
        manager = self.install()
        manager.update_settings(MOD_ID, {"enabled": True, "rules": RULES})

        self.assertEqual(self.send(manager, HIT), "100块，包邮")
        self.assertEqual(self.send(manager, "什么时候发货"), "今晚就发")
        self.assertIsNone(self.send(manager, MISS))
        self.assertIsNone(self.send(manager, ""))
        self.assertEqual(self.runtime.notes, [])

    # ── ③ 禁 ────────────────────────────────────────────────────────────────
    def test_step3_disable_stops_reply_but_keeps_plugin(self):
        manager = self.install()
        manager.update_settings(MOD_ID, {"enabled": True, "rules": RULES})
        self.assertEqual(self.send(manager, HIT), "100块，包邮")

        manager.update_settings(MOD_ID, {"enabled": False})
        self.assertIsNone(self.send(manager, HIT))
        self.assertIsNone(self.send(manager, "什么时候发货"))
        # 关掉只是不跑钩子：插件还在表里，规则也还在。
        self.assertIn(MOD_ID, self.plugin_ids(manager))
        self.assertFalse({r["id"]: r["enabled"] for r in manager.list_plugins()}[MOD_ID])
        self.assertEqual(manager.get_settings(MOD_ID)["rules"], RULES)

    # ── ④ 摘 ────────────────────────────────────────────────────────────────
    def test_step4_uninstall_removes_plugin_and_leaves_no_residue(self):
        manager = self.install()
        manager.update_settings(MOD_ID, {"enabled": True, "rules": RULES})
        self.assertEqual(self.send(manager, HIT), "100块，包邮")
        manager.take_load_problems()
        before = self.plugin_ids(manager)

        shutil.rmtree(self.mod_dir)
        self.assertFalse(self.mod_dir.exists())
        manager.reload()

        self.assertNotIn(MOD_ID, manager._plugins)
        self.assertNotIn(MOD_ID, self.plugin_ids(manager))
        self.assertEqual(sorted(set(before) - set(self.plugin_ids(manager))), [MOD_ID])
        self.assertEqual(len(self.plugin_ids(manager)), len(before) - 1)   # 其它插件一个不少
        self.assertIsNone(self.send(manager, HIT))                         # 不再回复
        self.assertEqual(set(manager.take_load_problems()), self.baseline_problems)
        self.assertEqual(self.runtime.notes, [])                           # 没有残留 note
        bad = [e for e in self.audit_events()
               if e["plugin"] == MOD_ID and e["event"] in ("load-refused", "hook-failed",
                                                           "auto-disabled", "hook-timeout")]
        self.assertEqual(bad, [])                                          # 没有异常审计
        manager.reload()                                                   # 再 reload 一次也不炸
        self.assertNotIn(MOD_ID, manager._plugins)
        self.assertEqual(set(manager.take_load_problems()), self.baseline_problems)

    # ── 与内置版行为一致性 ──────────────────────────────────────────────────
    def test_same_inputs_match_builtin_keyword_reply(self):
        manager = self.install()
        manager.update_settings(MOD_ID, {"enabled": True, "rules": RULES})
        manager.update_settings("keyword_reply", {"enabled": True, "rules": RULES})
        mod_plugin = manager._plugins[MOD_ID]
        builtin = manager._plugins["keyword_reply"]

        for text in (HIT, "什么时候发货", MISS, "", "价格和发货都想问", "PRICE 多少"):
            message = {"text": text, "chat_id": "g1", "sender_id": "u1"}
            mod_reply = manager._call_mod_hook(mod_plugin, "on_ingest", mod_plugin.on_ingest,
                                               self.runtime, message)
            self.assertEqual(mod_reply, builtin.on_ingest(message, self.runtime), text)

        # 版本化规则（v1，与 write_rule_rows 同样的紧凑分隔符）两边也要一致
        from src.keyword_rules import write_rule_rows
        v1 = write_rule_rows([("价格", "第一行\n第二行")])
        manager.update_settings(MOD_ID, {"rules": v1})
        manager.update_settings("keyword_reply", {"rules": v1})
        message = {"text": "价格"}
        self.assertEqual(
            manager._call_mod_hook(mod_plugin, "on_ingest", mod_plugin.on_ingest,
                                   self.runtime, message),
            builtin.on_ingest(message, self.runtime))
        self.assertEqual(self.send(manager, "价格"), "第一行\n第二行")

        # 损坏的 v1 规则：两边都在保存时报同一个错
        broken = '{"format":"keyword_reply/v1","rules":[{"keyword":1}]}'
        for pid in (MOD_ID, "keyword_reply"):
            with self.assertRaises(ValueError) as ctx:
                manager.update_settings(pid, {"rules": broken})
            self.assertIn("规则数据损坏", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
