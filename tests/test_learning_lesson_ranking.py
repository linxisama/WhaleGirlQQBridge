"""学习系统：match_lessons 的筛选顺序与可解释性（2026-11-07）。

背景：`ranked = score × 置信度系数 × 新鲜度系数`，两个系数都小于 1，所以
「相似度高但反复失败」的经验 ranked 会低于「相似度一般但从没失败」的。
原实现是 `for ... in scored[:limit]` 再 `if score < .28: continue` —— 先切片后筛，
前 limit 条被阈值筛掉就返回空，明明还有合格经验。
"""
import tempfile
import unittest
from pathlib import Path

from src.agent_learning import AgentLearning


def _actions(tag):
    return [{"type": "plugin", "id": tag, "enabled": True}]


class MatchLessonsOrderTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = AgentLearning(Path(self.dir.name) / "learn.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_failed_high_similarity_does_not_hide_the_rest(self):
        """强相关但反复失败的经验 ranked 低，不能因此让合格经验一条都取不到。"""
        failed = self.store.record("打开表情插件", _actions("memes"))
        for _ in range(8):
            self.store.feedback(lesson_id=failed["id"], score=-1)
        for index, text in enumerate(("打开表情插件的设置", "打开表情插件的开关",
                                      "打开表情插件的地方", "打开表情插件的方式")):
            self.store.record(text, _actions("q%d" % index))
        hits = self.store.match_lessons("打开表情插件", limit=4)
        self.assertEqual(len(hits), 4)
        for hit in hits:
            self.assertGreaterEqual(hit["score"], 0.28)
        self.assertNotIn("打开表情插件", [hit["intent"] for hit in hits],
                         "反复失败的那条相似度最高，但 ranked 已被压到合格线之外")

    def test_threshold_applies_before_limit(self):
        """阈值必须先筛再截断：低分条目不能占掉 limit 名额。"""
        self.store.record("完全无关的甲", _actions("a"))
        self.store.record("完全无关的乙", _actions("b"))
        strong = self.store.record("把回复调快一点", _actions("c"))
        hits = self.store.match_lessons("把回复调快一点", limit=1)
        self.assertEqual([hit["id"] for hit in hits], [strong["id"]])
        self.assertGreaterEqual(hits[0]["score"], 0.9)

    def test_limit_zero_returns_nothing(self):
        self.store.record("打开表情插件", _actions("memes"))
        self.assertEqual(self.store.match_lessons("打开表情插件", limit=0), [])

    def test_empty_store_returns_nothing(self):
        self.assertEqual(self.store.match_lessons("随便说点什么"), [])


if __name__ == "__main__":
    unittest.main()
