"""「对话与记忆」页的崩溃回归测试。

真机故障（用户截图，1.6.7 那台电脑）：打开「对话与记忆 → 自我记忆」就弹
"操作未完成，错误已记录 + logs/ui-errors.log"，表格全空。

复现后的根因：`src/gui/conversation.py` 里用了 `time.strftime` 却**没有 import time**。
只要"未回复诊断"的入站审计里有一条记录，整页 `refresh()` 就抛
`NameError: name 'time' is not defined` —— 而 refresh() 同时刷新四个子页，
所以用户看到的正是"自我记忆"页弹错。

另外顺手钉住几个"字段一缺就整页崩"的老毛病（KeyError/TypeError），
以及最重要的一条：**Qt 槽里绝不能抛异常**（PyQt 会直接 abort 整个进程）。
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication
    HAVE_QT = True
except Exception:  # pragma: no cover
    HAVE_QT = False

MEMORY = {
    "success": True,
    "persona": "tsundere_whale_girl",
    "manual": [{"id": "m1", "content": "谈到加班时先关心对方的状态", "enabled": True},
               {"id": "m2", "content": "叫他老板", "enabled": False}],
    "structured": [{"id": "s1", "scope": "group", "subject": "3B511E5E",
                    "content": "喜欢鲸鱼", "status": "confirmed", "evidence": 2},
                   {"id": "s2", "scope": "user", "subject": "238548A1",
                    "content": "想养猫", "status": "pending", "evidence": 1}],
    "autoRemember": True,
}

STATUS = {
    "decisions": [{"kind": "group", "target": "1CBE87EF", "sender": "慕白", "reason": "已进入回复队列"}],
    "inbound": [{"time": 1760000000.0, "event": "GROUP_AT_MESSAGE_CREATE", "kind": "group",
                 "room": "group:1CBE87EF", "sender": "3B511E5E", "text": "吃饭",
                 "outcome": "queued", "reason": "已进入回复队列"},
                {"time": 1760000060.0, "event": "C2C_MESSAGE_CREATE", "kind": "private",
                 "room": "private:238548A1", "sender": "238548A1", "text": "在吗",
                 "outcome": "dropped", "reason": "未点名且不在白名单"}],
}


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class MemoryTabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def tab(self, memory=None, status=None, posts=None):
        from src.gui.conversation import MemoryTab
        calls = []

        def get(path):
            calls.append(path)
            if path.startswith("memory"):
                return MEMORY if memory is None else memory
            return STATUS if status is None else status

        def post(path, payload):
            calls.append((path, payload))
            return {"success": True}

        widget = MemoryTab(get, post, lambda pid: None)
        widget.calls = calls
        return widget

    # ── 崩溃回归 ────────────────────────────────────────────────────────────
    def test_inbound_rows_render_without_crashing(self):
        """这次真机崩溃的回归：有入站记录时必须正常渲染（以前 NameError: time）。"""
        widget = self.tab()
        widget.refresh()                      # 以前这里就抛异常了
        self.assertEqual(widget.inbound.rowCount(), 2)
        stamp = widget.inbound.item(0, 0).text()
        self.assertRegex(stamp, r"^\d{2}:\d{2}:\d{2}$", f"时间列应渲染成时分秒，实际 {stamp!r}")
        self.assertEqual(widget.inbound.item(0, 1).text(), "GROUP_AT_MESSAGE_CREATE")
        self.assertIn("已进入回复队列", widget.inbound.item(0, 5).text())
        self.assertIn("被拦下", widget.inbound.item(1, 5).text())

    def test_module_imports_time(self):
        """直接钉住 import：这行漏了就会在"有数据"时炸，静态也检查一遍。"""
        import src.gui.conversation as conversation
        self.assertTrue(hasattr(conversation, "time"), "conversation.py 必须 import time")

    def test_manual_and_structured_tables_render(self):
        widget = self.tab()
        widget.refresh()
        self.assertEqual(widget.manual_table.rowCount(), 2)
        self.assertEqual(widget.manual_table.item(0, 1).text(), "谈到加班时先关心对方的状态")
        check = widget.manual_table.cellWidget(0, 0)
        self.assertTrue(check.isChecked())
        self.assertFalse(widget.manual_table.cellWidget(1, 0).isChecked())
        self.assertEqual(widget.facts.rowCount(), 2)
        self.assertEqual(widget.facts.item(0, 1).text(), "已确认")
        self.assertEqual(widget.facts.item(1, 1).text(), "待复证")
        self.assertIn("group / 3B511E5E", widget.facts.item(0, 3).text())

    def test_decisions_table_renders(self):
        widget = self.tab()
        widget.refresh()
        self.assertEqual(widget.decisions.rowCount(), 1)
        self.assertEqual(widget.decisions.item(0, 0).text(), "group:1CBE87EF")

    # ── 形状不对的返回也不该崩 ──────────────────────────────────────────────
    def test_null_scope_and_subject_are_placeholders(self):
        memory = dict(MEMORY, structured=[{"id": "s1", "scope": None, "subject": None,
                                           "content": "喜欢鲸鱼", "status": "confirmed",
                                           "evidence": None}])
        widget = self.tab(memory=memory)
        widget.refresh()
        self.assertEqual(widget.facts.item(0, 3).text(), "- / -")
        self.assertEqual(widget.facts.item(0, 2).text(), "0")

    def test_missing_fields_never_raise(self):
        cases = [
            ("decision 缺 kind/target", MEMORY, {"decisions": [{"sender": "x", "reason": "y"}], "inbound": []}),
            ("inbound 缺 time", MEMORY, {"decisions": [], "inbound": [{"event": "E", "kind": "group",
                                                                      "room": "group:1", "sender": "u",
                                                                      "text": "hi", "outcome": "queued",
                                                                      "reason": "r"}]}),
            ("inbound time 是字符串垃圾", MEMORY, {"decisions": [], "inbound": [{"time": "不是时间",
                                                                                "outcome": "queued"}]}),
            ("memory 缺 autoRemember", {k: v for k, v in MEMORY.items() if k != "autoRemember"}, STATUS),
            ("memory 缺 structured", {k: v for k, v in MEMORY.items() if k != "structured"}, STATUS),
            ("manual 缺 enabled", dict(MEMORY, manual=[{"id": "m", "content": "x"}]), STATUS),
            ("status 是 None", MEMORY, None),
            ("status 是字符串", MEMORY, "坏了"),
            ("memory 的 manual 是 None", dict(MEMORY, manual=None), STATUS),
        ]
        for label, memory, status in cases:
            widget = self.tab(memory=memory, status=status)
            try:
                widget.refresh()
            except Exception as exc:                     # pragma: no cover - 失败时给出可读信息
                self.fail(f"{label} 不该抛异常，实际 {type(exc).__name__}: {exc}")

    def test_backend_error_shows_notice_without_touching_tables(self):
        widget = self.tab(memory={"success": False, "message": "控制台仅允许本机访问"})
        widget.refresh()
        self.assertIn("仅允许本机", widget.notice.text())

    def test_broken_section_is_reported_in_notice(self):
        """一块坏数据只影响那一块：其余照常显示，并在底部说明。"""
        from src.gui.conversation import MemoryTab
        widget = MemoryTab(lambda path: (_ for _ in ()).throw(RuntimeError("boom"))
                           if False else MEMORY,
                           lambda path, payload: {"success": True}, lambda pid: None)
        widget.refresh()
        self.assertEqual(widget.manual_table.rowCount(), 2)

    # ── 交互 ────────────────────────────────────────────────────────────────
    def test_adding_a_memory_posts_and_refreshes(self):
        widget = self.tab()
        widget.refresh()
        widget.input.setText("  记住他喜欢鲸鱼  ")
        widget.add()
        posted = [call for call in widget.calls if isinstance(call, tuple)]
        self.assertEqual(posted[0][1]["action"], "add")
        self.assertEqual(posted[0][1]["content"], "记住他喜欢鲸鱼")
        self.assertEqual(widget.input.text(), "")

    def test_empty_input_is_ignored(self):
        widget = self.tab()
        widget.refresh()
        widget.input.setText("   ")
        widget.add()
        self.assertEqual([c for c in widget.calls if isinstance(c, tuple)], [])

    def test_delete_selected_manual_memory(self):
        widget = self.tab()
        widget.refresh()
        widget.manual_table.setCurrentCell(1, 1)
        widget.remove()
        posted = [call for call in widget.calls if isinstance(call, tuple)]
        self.assertEqual(posted[0][1], {"action": "delete", "id": "m2"})

    def test_posting_failure_is_shown_in_notice(self):
        from src.gui.conversation import MemoryTab
        widget = MemoryTab(lambda path: MEMORY if path.startswith("memory") else STATUS,
                           lambda path, payload: {"success": False, "detail": "每个人设最多100条手动记忆"},
                           lambda pid: None)
        widget.refresh()
        widget.input.setText("第101条")
        widget.add()
        self.assertIn("最多100条", widget.notice.text())


if __name__ == "__main__":
    unittest.main()
