# -*- coding: utf-8 -*-
"""静态自检：安装版「收集诊断」必须收集程序真正写日志的目录。

只读源文本做断言：不启动 PowerShell、不启动控制台、不写仓库。
跑法：python -m unittest tests.test_diagnostics_collector
"""
import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
PS1_ROOT = REPO / "收集诊断.ps1"
PS1_COPIES = (PS1_ROOT, REPO / "tools" / "收集诊断.ps1")
CMD_ROOT = REPO / "收集诊断.cmd"
CMD_COPIES = (CMD_ROOT, REPO / "tools" / "收集诊断.cmd")

CURRENT_LOG_DIR = r"WhaleGirlQQ\logs"
LEGACY_LOG_DIR = r"WhaleGirlQQBridge\logs"
NEW_LOG_FILES = ("gui-child.log", "qt-messages.log", "desktop-crash.json")


class DiagnosticsCollectorSourceTest(unittest.TestCase):
    def setUp(self):
        self.text = PS1_ROOT.read_text(encoding="utf-8-sig")

    def test_one_log_dir_candidates_cover_current_first_then_legacy(self):
        # 5.x 实际写 %LOCALAPPDATA%\WhaleGirlQQ\logs；旧版本用过 WhaleGirlQQBridge\logs
        self.assertIn(CURRENT_LOG_DIR, self.text)
        self.assertIn(LEGACY_LOG_DIR, self.text)
        self.assertLess(
            self.text.index(CURRENT_LOG_DIR),
            self.text.index(LEGACY_LOG_DIR),
            "WhaleGirlQQ\\logs 必须作为首选候选排在 WhaleGirlQQBridge\\logs 之前",
        )

    def test_two_log_dirs_report_real_path_and_drop_the_wrong_claim(self):
        self.assertIn("实际日志目录：", self.text)
        self.assertIn("两个候选目录都不存在", self.text)
        self.assertNotIn(
            "程序根本没启动起来",
            self.text,
            "日志目录缺失不能断言程序没启动（目录变了/日志被清理都会这样）",
        )

    def test_three_new_log_files_are_collected(self):
        for name in NEW_LOG_FILES:
            self.assertIn(name, self.text, "%s 没有被收集" % name)

    def test_dumps_are_listed_never_copied(self):
        self.assertIn("*.dmp", self.text)
        self.assertIn("如果有 dmp 文件，请单独把它发过来。", self.text)
        self.assertNotIn("Copy-Item $dump", self.text, "dmp 不许复制进 zip")

    def test_all_ps1_copies_are_byte_identical(self):
        base = PS1_ROOT.read_bytes()
        for copy in PS1_COPIES[1:]:
            self.assertTrue(copy.exists(), copy)
            self.assertEqual(base, copy.read_bytes(), "%s 与 %s 不一致" % (copy, PS1_ROOT))

    def test_ps1_keep_utf8_bom(self):
        # PowerShell 5.1 读无 BOM 的 .ps1 会按 ANSI 解码，中文路径直接乱码。
        for copy in PS1_COPIES:
            self.assertEqual(b"\xef\xbb\xbf", copy.read_bytes()[:3], "%s 缺少 UTF-8 BOM" % copy)

    def test_cmd_launcher_runs_the_ps1_next_to_itself(self):
        for cmd in CMD_COPIES:
            text = cmd.read_text(encoding="utf-8")
            self.assertIn('%~dp0收集诊断.ps1', text, "%s 没有调用同目录的收集诊断.ps1" % cmd)


if __name__ == "__main__":
    unittest.main()
