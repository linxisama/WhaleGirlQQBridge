"""管理员模式：密码只在哈希里、会话只在内存里、密码消息不进库也不进模型。

用户要求：管理界面能直接选管理员 QQ；管理员可以在页面设密码，在 QQ 里发
“进入管理员模式”再发密码进入；进入后能查状态/回复记录、暂停回复、换人设、
开关插件等。这里把安全边界也一起钉住：
- 配置里只有 PBKDF2 哈希 + 盐，**文件里绝不出现明文密码**；
- 密码那条 QQ 消息会被抹掉正文后才落库（否则它会原样存进 inbox 的 payload）；
- 会话与失败锁定只在内存里，重启即失效；连续错 3 次锁 10 分钟。
"""
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from fastapi.testclient import TestClient

    from src.qq_app import QQRuntime, create_app

    HAS_CLIENT = True
except Exception:
    HAS_CLIENT = False

from src.admin_session import (  # noqa: E402
    ADMIN_HELP, AdminSessions, entry_request, exit_request, hash_password,
    password_error, verify_password,
)
from src.plugins.pc_control import PcControl, parse_command, session_command  # noqa: E402

BUILTIN = """name: tsundere_whale_girl
display_name: 傲娇鲸鱼娘
description: 默认人设
system_prompt: 你是傲娇鲸鱼娘。
"""

ADMIN = "10001"
OTHER = "10002"


def build_root(tmp):
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "tsundere_whale_girl.yaml").write_text(BUILTIN, encoding="utf-8")
    return root


class PasswordCryptoTests(unittest.TestCase):
    def test_hash_is_salted_and_verifies(self):
        digest, salt = hash_password("hunter2!")
        self.assertNotIn("hunter2", digest)
        self.assertTrue(verify_password("hunter2!", digest, salt))
        self.assertFalse(verify_password("hunter2", digest, salt))
        self.assertFalse(verify_password("", digest, salt))

    def test_same_password_gets_different_hashes(self):
        first, _ = hash_password("hunter2!")
        second, _ = hash_password("hunter2!")
        self.assertNotEqual(first, second, "每次都要用新盐")

    def test_password_rules(self):
        self.assertIn("至少", password_error("123"))
        self.assertIn("空格", password_error(" hunter2"))
        self.assertEqual(password_error("hunter2!"), "")


class SessionClockTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.sessions = AdminSessions(minutes=30, clock=lambda: self.now)
        self.digest, self.salt = hash_password("hunter2!")

    def test_attempt_success_starts_session(self):
        self.sessions.begin(ADMIN)
        self.assertTrue(self.sessions.awaiting(ADMIN))
        self.assertEqual(self.sessions.attempt(ADMIN, "hunter2!", self.digest, self.salt), "ok")
        self.assertTrue(self.sessions.active(ADMIN))
        self.assertFalse(self.sessions.awaiting(ADMIN))
        self.assertEqual(self.sessions.seconds_left(ADMIN), 1800)

    def test_wrong_password_counts_then_locks(self):
        self.assertEqual(self.sessions.attempt(ADMIN, "nope", self.digest, self.salt), "wrong")
        self.assertEqual(self.sessions.attempts_left(ADMIN), 2)
        self.sessions.attempt(ADMIN, "nope", self.digest, self.salt)
        self.assertEqual(self.sessions.attempt(ADMIN, "nope", self.digest, self.salt), "locked")
        self.assertFalse(self.sessions.active(ADMIN))
        self.assertGreater(self.sessions.locked_for(ADMIN), 0)
        # 锁定期间即使密码正确也不放行
        self.assertEqual(self.sessions.attempt(ADMIN, "hunter2!", self.digest, self.salt), "locked")
        self.now += 601
        self.assertEqual(self.sessions.locked_for(ADMIN), 0)
        self.assertEqual(self.sessions.attempt(ADMIN, "hunter2!", self.digest, self.salt), "ok")

    def test_session_expires(self):
        self.sessions.start(ADMIN)
        self.now += 1801
        self.assertFalse(self.sessions.active(ADMIN))

    def test_end_and_end_all(self):
        self.sessions.start(ADMIN)
        self.sessions.start(OTHER)
        self.assertEqual(self.sessions.count(), 2)
        self.sessions.end(ADMIN)
        self.assertFalse(self.sessions.active(ADMIN))
        self.assertEqual(self.sessions.end_all(), 1)
        self.assertEqual(self.sessions.count(), 0)

    def test_begin_cancel(self):
        self.sessions.begin(ADMIN)
        self.sessions.cancel(ADMIN)
        self.assertFalse(self.sessions.awaiting(ADMIN))


class CommandParsingTests(unittest.TestCase):
    def test_entry_and_exit_words(self):
        for text in ("进入管理员模式", "管理员模式", "admin", "进入管理模式"):
            self.assertTrue(entry_request(text), text)
        for text in ("退出管理员模式", "退出管理员", "结束管理员模式"):
            self.assertTrue(exit_request(text), text)
        self.assertFalse(entry_request("你好"))

    def test_session_commands(self):
        self.assertEqual(session_command("状态"), ("status", ""))
        self.assertEqual(session_command("/帮助"), ("help", ""))
        self.assertEqual(session_command("回复记录 8"), ("replies", "8"))
        self.assertEqual(session_command("人设 傲娇鲸鱼娘"), ("persona", "傲娇鲸鱼娘"))
        self.assertEqual(session_command("插件 关 keyword_reply"), ("plugin", "关 keyword_reply"))
        self.assertEqual(session_command("重试失败"), ("retry_failed", ""))
        self.assertIsNone(session_command("今天天气不错"))

    def test_legacy_prefix_still_works(self):
        self.assertEqual(parse_command("机器人 状态"), ("status", ""))
        self.assertEqual(parse_command("机器人 暂停"), ("pause", ""))
        self.assertIsNone(parse_command("状态"))


@unittest.skipUnless(HAS_CLIENT, "需要 fastapi.testclient")
class AdminModeEndToEndTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self._tmp.name)
        self.config = self.root / "config.qq.yaml"
        self.config.write_text(yaml.safe_dump({
            "ai": {"api_url": "https://example.test/v1", "api_key": "fake-key", "model": "test-model"},
            "qq": {"app_id": "123", "client_secret": "fake-secret"},
        }), encoding="utf-8")
        self.bot = QQRuntime(root=self.root, config_path=self.config, db_path=self.root / "test.sqlite")
        self.client = TestClient(create_app(self.bot))
        self.client.__enter__()
        self.plugin = self.bot.plugins._plugins["pc_control"]
        self.bot.plugins.update_settings("pc_control", {"enabled": True, "ownerIds": ADMIN})

    def tearDown(self):
        self.client.__exit__(None, None, None)
        try:
            self.bot.db.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def message(self, text, sender=ADMIN):
        return {"id": f"m-{abs(hash((text, sender))) % 10**8}", "kind": "private", "target": "admin",
                "sender_id": sender, "sender_name": "主人", "text": text, "at": False}

    def send(self, text, sender=ADMIN):
        message = self.message(text, sender)
        reply = self.plugin.on_ingest(message, self.bot)
        return reply, message

    def set_password(self, password, old=""):
        return self.client.post("/api/pc/admin-password", json={"new": password, "old": old})

    # ── 密码设置（控制台侧）──────────────────────────────────────────────
    def test_password_is_stored_hashed_only(self):
        self.assertEqual(self.set_password("hunter2!").status_code, 200)
        raw = self.config.read_text(encoding="utf-8")
        self.assertNotIn("hunter2", raw, "配置文件里绝不能出现明文密码")
        self.assertTrue(self.bot.config.get("qq.admin_password_hash"))
        self.assertTrue(self.bot.config.get("qq.admin_password_salt"))
        admin = self.client.get("/api/pc").json()["admin"]
        self.assertTrue(admin["password_set"])

    def test_change_needs_old_password_but_clear_does_not(self):
        self.set_password("hunter2!")
        self.assertEqual(self.set_password("newpass!", old="wrong").status_code, 400)
        self.assertEqual(self.set_password("newpass!", old="hunter2!").status_code, 200)
        self.assertEqual(self.set_password("").status_code, 200)
        self.assertFalse(self.client.get("/api/pc").json()["admin"]["password_set"])

    def test_short_password_is_rejected(self):
        response = self.set_password("123")
        self.assertEqual(response.status_code, 400)
        self.assertIn("至少", response.json()["detail"])

    def test_clear_ends_sessions(self):
        self.set_password("hunter2!")
        self.send("进入管理员模式")
        self.send("hunter2!")
        self.assertTrue(self.bot.admin.active(ADMIN))
        self.set_password("")
        self.assertFalse(self.bot.admin.active(ADMIN))

    # ── QQ 侧流程 ─────────────────────────────────────────────────────────
    def test_enter_without_password_explains(self):
        reply, _ = self.send("进入管理员模式")
        self.assertIn("还没有设置管理员密码", reply)
        self.assertFalse(self.bot.admin.awaiting(ADMIN))

    def test_full_enter_flow_and_password_never_stored(self):
        self.set_password("hunter2!")
        reply, _ = self.send("进入管理员模式")
        self.assertIn("请输入管理员密码", reply)
        self.assertTrue(self.bot.admin.awaiting(ADMIN))

        reply, message = self.send("hunter2!")
        self.assertIn("已进入管理员模式", reply)
        self.assertTrue(self.bot.admin.active(ADMIN))
        self.assertNotIn("hunter2", str(message), "密码消息必须在返回前被抹掉正文")
        rows = self.bot.db.execute("SELECT payload FROM inbox").fetchall()
        self.assertFalse(any("hunter2" in (row[0] or "") for row in rows), "密码不得落进 inbox")

    def test_wrong_password_counts_and_locks(self):
        self.set_password("hunter2!")
        self.send("进入管理员模式")
        reply, _ = self.send("nope")
        self.assertIn("还可以尝试 2 次", reply)
        reply, _ = self.send("nope")
        self.assertIn("还可以尝试 1 次", reply)
        reply, _ = self.send("nope")
        self.assertIn("锁定", reply)
        reply, _ = self.send("进入管理员模式")
        self.assertIn("锁定", reply)
        # 锁定期间即使密码正确也不放行
        self.send("进入管理员模式")
        self.assertFalse(self.bot.admin.active(ADMIN))

    def test_repeated_entry_word_does_not_burn_an_attempt(self):
        self.set_password("hunter2!")
        self.send("进入管理员模式")
        reply, _ = self.send("进入管理员模式")
        self.assertIn("请输入管理员密码", reply)
        self.assertEqual(self.bot.admin.attempts_left(ADMIN), 3)

    def test_cancel_password_prompt(self):
        self.set_password("hunter2!")
        self.send("进入管理员模式")
        reply, _ = self.send("取消")
        self.assertIn("已取消", reply)
        self.assertFalse(self.bot.admin.awaiting(ADMIN))

    def test_non_admin_cannot_enter(self):
        self.set_password("hunter2!")
        reply, _ = self.send("进入管理员模式", sender=OTHER)
        self.assertIsNone(reply, "不是绑定管理员的私聊，不回答也不进入流程")
        self.assertFalse(self.bot.admin.awaiting(OTHER))

    def test_session_commands_work_and_are_audited(self):
        self.set_password("hunter2!")
        self.send("进入管理员模式")
        self.send("hunter2!")

        reply, _ = self.send("状态")
        self.assertIn("鲸鱼娘", reply)
        reply, _ = self.send("回复记录 3")
        self.assertIn("回复", reply)
        reply, _ = self.send("插件")
        self.assertIn("插件", reply)

        reply, _ = self.send("插件 关 keyword_reply")
        self.assertIn("已关闭", reply)
        self.assertFalse(self.bot.plugins.get_settings("keyword_reply")["enabled"])
        reply, _ = self.send("插件 开 keyword_reply")
        self.assertIn("已开启", reply)

        reply, _ = self.send("暂停")
        self.assertIn("已暂停", reply)
        self.assertTrue(self.bot.reply_paused)
        reply, _ = self.send("恢复")
        self.assertIn("已恢复", reply)
        self.assertFalse(self.bot.reply_paused)

        actions = [row[0] for row in self.bot.db.execute("SELECT action FROM pc_jobs ORDER BY created").fetchall()]
        for expected in ("status", "replies", "plugin", "pause", "resume"):
            self.assertIn(expected, actions, f"{expected} 应写进审计表")

    def test_persona_switch_in_session_and_banned_word_guard(self):
        self.set_password("hunter2!")
        self.send("进入管理员模式")
        self.send("hunter2!")
        reply, _ = self.send("人设")
        self.assertIn("tsundere_whale_girl", reply)
        reply, _ = self.send("人设 不存在的名字")
        self.assertIn("不存在", reply)

        self.client.post("/api/personas", json={"name": "risky", "display_name": "危险",
                                                "system_prompt": "里面写着 badword"})
        self.client.post("/api/personas/banned-words", json={"words": "badword"})
        self.send("恢复")
        reply, _ = self.send("人设 risky")
        self.assertIn("违禁词", reply)
        self.assertTrue(self.bot.reply_paused, "命中违禁词要暂停自动回复")

    def test_exit_admin_mode(self):
        self.set_password("hunter2!")
        self.send("进入管理员模式")
        self.send("hunter2!")
        reply, _ = self.send("退出管理员模式")
        self.assertIn("已退出", reply)
        self.assertFalse(self.bot.admin.active(ADMIN))
        # 退出后简写命令不再被管理员通道吃掉（回落到普通聊天）
        self.assertIsNone(self.send("状态")[0])

    def test_legacy_commands_still_go_through_review(self):
        reply, _ = self.send("机器人 暂停")
        self.assertIn("本地审核", reply)
        self.assertTrue(self.bot.pc.list(), "旧通道要留下待审核记录")

    def test_wants_routes_only_admin_messages(self):
        self.assertTrue(self.plugin.wants(self.message("进入管理员模式"), self.bot))
        self.assertTrue(self.plugin.wants(self.message("机器人 状态"), self.bot))
        self.assertFalse(self.plugin.wants(self.message("状态"), self.bot), "会话外不认简写命令")
        self.assertFalse(self.plugin.wants(self.message("今天天气不错"), self.bot))
        self.set_password("hunter2!")
        self.send("进入管理员模式")
        self.assertTrue(self.plugin.wants(self.message("hunter2!"), self.bot), "等密码时任何消息都要被吃掉")
        self.assertTrue(self.plugin.wants(self.message("状态"), self.bot))

    def test_help_lists_admin_commands(self):
        self.set_password("hunter2!")
        self.send("进入管理员模式")
        reply, _ = self.send("hunter2!")
        self.assertIn("回复记录", reply)
        self.assertIn(ADMIN_HELP.splitlines()[0], reply)


if __name__ == "__main__":
    unittest.main()
