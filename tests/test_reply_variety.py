"""防复读（reply_variety）与官方模式戳一戳提示的回归测试。

真机背景（2026-10-17）：
1. 用户在官方模式下反复戳机器人没有任何回应 —— 查他机器上的 inbound_audit，
   只有 GROUP_AT_MESSAGE_CREATE / C2C_MESSAGE_CREATE / READY / GROUP_ADD_ROBOT，
   **一条 poke 都没有**：QQ 开放平台不推送戳一戳。程序侧要做的是把这件事说清楚。
2. 控制台里 19 条回复读起来像复读（"哟，…本鲸…不服开帖"）。用用户真实数据量了一遍：
   字面最像的一对 0.52，其余 0.0~0.17 —— 所以阈值定在 45% 能抓住真复读、不误伤新回复。
"""
import asyncio
import os
import pathlib
import shutil
import sqlite3
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.plugins.reply_variety import ReplyVariety, similarity, too_similar  # noqa: E402

USER_ROOT = pathlib.Path.home() / "AppData" / "Local" / "WhaleGirlQQ"
GROUP = "A9FC46C6BC722A1B8BAADC3F8B2D65AD"

# 用户机器上真实出现过的两段（控制台截图 19:01:36 与 18:59:23）
REPEATED = "哟，又拿这个糊弄本鲸？我手撕过的键盘侠比你打过的字都多，不服开帖，我奉陪。"
REPEATED_2 = "笑死，就你？本鲸手撕过的键盘侠比你打过的字都多，不服滚去开单挑帖。"
FRESH = "今天楼下那家面馆终于开门了，要不要一起去？"


class SimilarityTests(unittest.TestCase):
    def test_real_repeat_pair_is_caught(self):
        self.assertGreater(similarity(REPEATED, REPEATED_2), 0.42)
        self.assertTrue(too_similar(REPEATED, [REPEATED_2]), "真机那对复读必须能抓到")

    def test_fresh_reply_is_not_flagged(self):
        self.assertEqual(too_similar(FRESH, [REPEATED, REPEATED_2]), "")

    def test_short_replies_are_ignored(self):
        self.assertEqual(too_similar("嗯", ["嗯嗯"]), "", "太短的回复不参与判定")

    def test_default_threshold_is_documented_value(self):
        self.assertEqual(ReplyVariety.META.default_settings["thresholdPercent"], 45)


class RefineTests(unittest.TestCase):
    """太像 → 带指令重生成一次；仍像 → 用新版并留痕；异常不影响正常回复。"""

    def make(self, recent, retry="换了个说法：你今天吃了吗？", settings=None):
        plugin = ReplyVariety.__new__(ReplyVariety)
        values = dict(ReplyVariety.META.default_settings)
        values.update(settings or {})
        plugin.get = lambda key, default=None: values.get(key, default)
        # enabled 是 PluginBase 的只读 property（读 manager 里的设置），测试里直接覆盖读取
        type(plugin).enabled = property(lambda self: values["enabled"])
        self.addCleanup(lambda: delattr(type(plugin), 'enabled'))
        plugin.recent_replies = lambda message, runtime: list(recent)
        self.prompts = []

        class Engine:
            async def chat(inner, conversation, **kwargs):
                self.prompts.append(conversation)
                return retry

        self.notes = []

        class Runtime:
            mode = "official"

            @staticmethod
            def note(text):
                self.notes.append(text)

        return plugin, Engine(), Runtime()

    def test_similar_reply_is_regenerated(self):
        plugin, engine, runtime = self.make([REPEATED_2])
        reply = asyncio.run(plugin.refine(engine, [{"role": "user", "content": "小杂鱼"}],
                                          REPEATED, {"kind": "group", "target": GROUP}, runtime))
        self.assertEqual(reply, "换了个说法：你今天吃了吗？")
        self.assertTrue(any("别再用同样的开场" in str(item) for item in self.prompts[-1]),
                        "重生成时要带上'别重复'的明确指令")
        self.assertTrue(any("已换一种说法重生成" in note for note in self.notes), self.notes)

    def test_still_similar_keeps_reply_and_notes(self):
        plugin, engine, runtime = self.make([REPEATED_2], retry=REPEATED)
        reply = asyncio.run(plugin.refine(engine, [], REPEATED, {"kind": "group", "target": GROUP},
                                          runtime))
        self.assertEqual(reply, REPEATED)
        self.assertTrue(any("仍然像" in note for note in self.notes), self.notes)

    def test_no_history_means_no_extra_call(self):
        plugin, engine, runtime = self.make([])
        reply = asyncio.run(plugin.refine(engine, [], REPEATED, {"kind": "group", "target": GROUP},
                                          runtime))
        self.assertEqual(reply, REPEATED)
        self.assertEqual(self.prompts, [], "第一次说话不该多发一次请求")

    def test_retry_disabled_only_notes(self):
        plugin, engine, runtime = self.make([REPEATED_2], settings={"retryOnce": False})
        reply = asyncio.run(plugin.refine(engine, [], REPEATED, {"kind": "group", "target": GROUP},
                                          runtime))
        self.assertEqual(reply, REPEATED)
        self.assertEqual(self.prompts, [])
        self.assertTrue(any("未重生成" in note for note in self.notes), self.notes)

    def test_engine_failure_keeps_original_reply(self):
        plugin, engine, runtime = self.make([REPEATED_2])

        class Broken:
            async def chat(inner, conversation, **kwargs):
                raise RuntimeError("模型挂了")

        reply = asyncio.run(plugin.refine(Broken(), [], REPEATED,
                                          {"kind": "group", "target": GROUP}, runtime))
        self.assertEqual(reply, REPEATED, "重生成失败不能把回复弄丢")
        self.assertTrue(any("检查失败已跳过" in note for note in self.notes), self.notes)


class RecentRepliesQueryTests(unittest.TestCase):
    def test_reads_last_sent_replies_for_the_room(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = os.path.join(tmp.name, "m.sqlite3")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE inbox(key TEXT PRIMARY KEY, mode TEXT, account TEXT, room TEXT,"
                     " payload TEXT, state TEXT, reply TEXT, error TEXT, created REAL)")
        for index, text in enumerate(["第一条", "第二条", "第三条"]):
            conn.execute("INSERT INTO inbox VALUES(?,?,?,?,?,?,?,?,?)",
                         (f"k{index}", "official", "appid", f"group:{GROUP}", "{}", "sent",
                          text, "", time.time() + index))
        conn.commit()
        conn.close()

        class DB:
            def execute(inner, sql, params):
                conn = sqlite3.connect(db_path)
                conn.row_factory = None
                rows = conn.execute(sql, params).fetchall()
                conn.close()
                return type("Cursor", (), {"fetchall": staticmethod(lambda: rows)})()

        class Runtime:
            db = DB()
            mode = "official"

            @staticmethod
            def account():
                return "appid"

        plugin = ReplyVariety.__new__(ReplyVariety)
        plugin.get = lambda key, default=None: (
            ReplyVariety.META.default_settings.get(key, default) if key != "lookback" else 3)
        rows = plugin.recent_replies({"kind": "group", "target": GROUP}, Runtime())
        self.assertEqual(rows, ["第三条", "第二条", "第一条"])


class OfficialModePokeHintTests(unittest.TestCase):
    def test_hint_is_added_when_official_mode_and_poke_plugins_enabled(self):
        from src.qq_app import QQRuntime

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
        shutil.copytree(USER_ROOT / "styles", root / "styles", dirs_exist_ok=True)
        (root / "config.qq.yaml").write_text(
            "ai:\n  api_url: https://example.test/v1/chat/completions\n  api_key: fake\n"
            "  model: test\nqq:\n  app_id: appid\n  client_secret: fake\n  sandbox: false\n"
            "qq_only:\n  mode: official\n", encoding="utf-8")
        bot = QQRuntime(root=root, config_path=root / "config.qq.yaml",
                        db_path=root / "t.sqlite3")
        notes = []
        bot.note = lambda text: notes.append(text)
        # 两个戳插件默认是关的：先启用，提示才有意义（用户机器上就是开着的）
        for plugin_id in ("poke_counter", "probability_reply"):
            try:
                bot.plugins.update_settings(plugin_id, {"enabled": True})
            except Exception:
                pass
        try:
            asyncio.run(bot.start())
        except Exception:
            pass
        try:
            joined = "\n".join(notes)
            self.assertIn("不推送戳一戳", joined,
                          f"官方模式要主动说明戳一戳收不到：{notes}")
            self.assertIn("普通 QQ", joined)
        finally:
            try:
                asyncio.run(bot.close())
            except Exception:
                pass
            try:
                bot.db.close()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
