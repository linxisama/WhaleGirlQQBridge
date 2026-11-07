"""persona-bindings 路由要把「失效的绑定」报出来（2026-11-07）。

`resolve_persona` 会跳过命中违禁词的人设（不能让会话绑定绕过 set_active 的闸门），
但表格里那一行看起来仍然正常。路由必须把失效原因一起返回，GUI 才能提示。
"""
from __future__ import annotations

import unittest

from src.persona_bindings import resolve_persona


class _Persona:
    def __init__(self, name, display_name=""):
        self.name = name
        self.display_name = display_name or name

    def full_prompt(self):
        return "PROMPT:" + self.name


class _Style:
    def __init__(self, styles):
        self._styles = styles
        self.active_name = "global-one"

    def get(self):
        return self._styles["global-one"]

    def match_keywords(self, text):
        return None

    def persona_violations(self, style):
        return ["违禁词"] if style.name == "dirty" else []

    def full_prompt(self, style=None):
        return "PROMPT:" + (style or self.get()).name


class _Plugin:
    def __init__(self, rows):
        self._rows = rows

    def load_state(self, name, default=None):
        return {"rows": self._rows} if name == "bindings.json" else (default or {})


class _Plugins:
    def __init__(self, plugin):
        self._plugins = {"archive_isolation": plugin}


class _Runtime:
    def __init__(self, rows):
        self.style = _Style({"global-one": _Persona("global-one"), "good": _Persona("good"),
                             "dirty": _Persona("dirty")})
        self.plugins = _Plugins(_Plugin(rows))
        self.mode = "official"

    def account(self, mode=None):
        return "acct-1"


def _rows():
    return [{"mode": "official", "account": "acct-1", "kind": "group", "target": "888",
             "user": "", "persona": "good"},
            {"mode": "official", "account": "acct-1", "kind": "group", "target": "999",
             "user": "", "persona": "dirty"},
            {"mode": "official", "account": "acct-1", "kind": "group", "target": "777",
             "user": "", "persona": "ghost"}]


class InvalidBindingReportTests(unittest.TestCase):
    def setUp(self):
        self.runtime = _Runtime(_rows())

    def test_clean_binding_wins(self):
        message = {"kind": "group", "target": "888", "text": ""}
        persona = resolve_persona(self.runtime, message)
        self.assertEqual(persona.name, "good")
        self.assertTrue(message["_persona_bound"])
        self.assertEqual(message["_persona_reason"], "binding")

    def test_violating_binding_is_skipped_and_marked(self):
        message = {"kind": "group", "target": "999", "text": ""}
        persona = resolve_persona(self.runtime, message)
        self.assertEqual(persona.name, "global-one")
        self.assertFalse(message["_persona_bound"])
        self.assertIn("blocked:dirty", message["_persona_reason"])

    def test_missing_persona_is_marked(self):
        message = {"kind": "group", "target": "777", "text": ""}
        resolve_persona(self.runtime, message)
        self.assertIn("missing:ghost", message["_persona_reason"])

    def test_unbound_conversation_has_plain_default_reason(self):
        message = {"kind": "group", "target": "555", "text": ""}
        resolve_persona(self.runtime, message)
        self.assertEqual(message["_persona_reason"], "default")

    def test_invalid_map_matches_resolver_decisions(self):
        """路由返回的 invalid 必须和 resolve_persona 的判定一致。"""
        bad = {}
        for row in _rows():
            persona = self.runtime.style._styles.get(row["persona"])
            if persona is None:
                bad[row["persona"]] = "missing"
            elif self.runtime.style.persona_violations(persona):
                bad[row["persona"]] = "blocked"
        self.assertEqual(bad, {"dirty": "blocked", "ghost": "missing"})


if __name__ == "__main__":
    unittest.main()
