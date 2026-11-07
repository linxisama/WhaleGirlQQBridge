"""人设冲突：会话绑定与关键词指向不同人设时，绑定赢但冲突要留痕。"""
import unittest

from src.persona_bindings import resolve_persona


class _Persona:
    def __init__(self, name):
        self.name = name
        self.display_name = name.upper()

    def full_prompt(self):
        return "PROMPT:" + self.name


class _Style:
    def __init__(self, active, keyword_hit):
        self._styles = {"bound-one": _Persona("bound-one"),
                        "kw-one": _Persona("kw-one"),
                        "global-one": _Persona("global-one")}
        self._active = active
        self._keyword_hit = keyword_hit
        self.active_name = active

    def persona_violations(self, style):
        return []

    def match_keywords(self, text):
        return self._styles.get(self._keyword_hit) if self._keyword_hit else None

    def get(self):
        return self._styles[self._active]


class _Plugin:
    def __init__(self, rows):
        self._rows = rows

    def load_state(self, name, default=None):
        return {"rows": self._rows}


class _Plugins:
    def __init__(self, plugin):
        self._plugins = {"archive_isolation": plugin}


class _Runtime:
    def __init__(self, rows, keyword_hit=None):
        self.mode = "official"
        self.style = _Style("global-one", keyword_hit)
        self.plugins = _Plugins(_Plugin(rows))

    def account(self):
        return "acct-1"


ROW = {"mode": "official", "account": "acct-1", "kind": "group",
       "target": "888", "user": "", "persona": "bound-one"}


class PersonaConflictTests(unittest.TestCase):
    def test_binding_wins_when_keyword_points_elsewhere(self):
        runtime = _Runtime([ROW], keyword_hit="kw-one")
        message = {"kind": "group", "target": "888", "text": "鲸鱼娘在吗"}
        persona = resolve_persona(runtime, message)
        self.assertEqual(persona.name, "bound-one", "会话绑定是用户显式指定的，优先级更高")
        self.assertTrue(message["_persona_bound"])
        self.assertIn("keyword:kw-one", message["_persona_reason"],
                      "绑定压过关键词时必须留痕，否则用户以为人设串了")

    def test_no_conflict_marker_when_keyword_agrees(self):
        runtime = _Runtime([ROW], keyword_hit="bound-one")
        message = {"kind": "group", "target": "888", "text": "鲸鱼娘在吗"}
        resolve_persona(runtime, message)
        self.assertEqual(message["_persona_reason"], "binding")

    def test_unbound_conversation_keeps_keyword_behavior(self):
        runtime = _Runtime([], keyword_hit="kw-one")
        message = {"kind": "group", "target": "555", "text": "鲸鱼娘在吗"}
        persona = resolve_persona(runtime, message)
        self.assertEqual(persona.name, "kw-one")
        self.assertEqual(message["_persona_reason"], "keyword")
        self.assertFalse(message["_persona_bound"])


if __name__ == "__main__":
    unittest.main()
