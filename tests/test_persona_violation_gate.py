"""人设绑定的违禁词闸门（2026-11-07）。

set_active 会拒绝切换到命中违禁词的人设，但 resolve_persona 原本直接按绑定取用，
等于给违禁词人设留了一条绕过闸门的旁路：绑到某个群，那个群就成了唯一不受管的地方。
"""
import unittest

from src.persona_bindings import resolve_persona


class _Persona:
    def __init__(self, name, prompt):
        self.name = name
        self.display_name = name
        self._prompt = prompt

    def full_prompt(self):
        return self._prompt


class _Style:
    def __init__(self):
        self.active_name = "global-one"
        self.blocked_persona = ""
        self._styles = {
            "global-one": _Persona("global-one", "GLOBAL"),
            "good": _Persona("good", "GOOD"),
            "dirty": _Persona("dirty", "DIRTY"),
        }

    def get(self):
        return self._styles["global-one"]

    def match_keywords(self, text):
        return None

    def persona_violations(self, style):
        return ["违禁词"] if style.name == "dirty" else []


class _Plugin:
    def __init__(self, rows):
        self.rows = rows

    def load_state(self, name, default):
        return {"rows": self.rows}


class _Runtime:
    mode = "official"

    def __init__(self, rows):
        self.style = _Style()
        self.plugins = type("PL", (), {"_plugins": {"archive_isolation": _Plugin(rows)}})()

    def account(self):
        return "acct-1"


def _row(target, persona):
    return {"mode": "official", "account": "acct-1", "kind": "group",
            "target": target, "user": "", "persona": persona}


class PersonaViolationGateTests(unittest.TestCase):
    def setUp(self):
        self.runtime = _Runtime([_row("888", "good"),
                                 _row("999", "dirty"),
                                 _row("777", "ghost")])

    def _resolve(self, target):
        message = {"kind": "group", "target": target, "text": ""}
        return resolve_persona(self.runtime, message), message

    def test_clean_binding_still_wins(self):
        persona, message = self._resolve("888")
        self.assertEqual(persona.name, "good")
        self.assertTrue(message["_persona_bound"])
        self.assertEqual(message["_persona_reason"], "binding")

    def test_violating_binding_falls_back_to_default(self):
        persona, message = self._resolve("999")
        self.assertEqual(persona.name, "global-one",
                         "命中违禁词的人设不能经绑定被用到前台")
        self.assertFalse(message["_persona_bound"])
        self.assertIn("blocked:dirty", message["_persona_reason"])

    def test_missing_persona_is_reported(self):
        persona, message = self._resolve("777")
        self.assertEqual(persona.name, "global-one")
        self.assertIn("missing:ghost", message["_persona_reason"])

    def test_unbound_conversation_has_no_blocked_marker(self):
        persona, message = self._resolve("555")
        self.assertEqual(persona.name, "global-one")
        self.assertEqual(message["_persona_reason"], "default")


if __name__ == "__main__":
    unittest.main()
