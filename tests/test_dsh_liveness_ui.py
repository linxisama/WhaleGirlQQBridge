"""DSH 联动对话框要显示探活结果（2026-11-07）。

背景：`local_agent_bridge.status()` 早就会返回 `bot_alive` / `bot_alive_hint` /
`bot_state` / `bot_problems`，但 `src/gui/assistant.py` 的 `_show_local_agent`
只渲染「已登记/未登记」—— 配置登记齐全不等于 DSH 里此刻真能连上，控制台没开时
用户看到的仍是「控制 MCP：已登记」，会以为接好了。
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSISTANT = ROOT / "src" / "gui" / "assistant.py"
BRIDGE = ROOT / "src" / "local_agent_bridge.py"


def _source(path):
    return path.read_text(encoding="utf-8")


class LocalAgentDialogTests(unittest.TestCase):
    def setUp(self):
        self.text = _source(ASSISTANT)

    def test_bridge_still_reports_liveness_fields(self):
        """对话框依赖的字段必须在 status() 的返回里，否则界面只能瞎猜。"""
        source = _source(BRIDGE)
        for field in ("bot_alive", "bot_alive_hint", "bot_state", "bot_problems"):
            self.assertIn("'%s'" % field, source, "status() 少了 %s" % field)

    def test_dialog_renders_liveness_line(self):
        self.assertIn("控制台探活", self.text)
        self.assertIn("data.get('bot_alive')", self.text)

    def test_dialog_shows_hint_and_problems(self):
        self.assertIn("bot_alive_hint", self.text)
        self.assertIn("bot_problems", self.text)

    def test_probe_target_is_named(self):
        """探不通时要报出探的地址：改了端口却没生效，是这类问题的第一大成因。"""
        self.assertIn("bot_probe", self.text)
        self.assertIn("探的地址", self.text)

    def test_three_states_are_distinguished(self):
        """配置坏 / 探活失败 / 没配地址 三种情况文案必须不同，不能都说「未登记」。"""
        for phrase in ("配置不完整", "探不通", "无法探活"):
            self.assertIn(phrase, self.text)

    def test_module_still_parses(self):
        ast.parse(self.text)


if __name__ == "__main__":
    unittest.main()
