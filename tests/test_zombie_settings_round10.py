"""Round 10 第二轮僵尸设置修复的测试。

第二轮用 AST 精确检测又找出三个「写了但不读」的设置：

| 设置 | 问题 | 现在的行为 |
|---|---|---|
| `context_chime.judgeModel` | 从不读取（判定模型设置形同虚设） | 真的传给引擎做单次模型覆盖 |
| `affinity_proactive.scanIntervalSec` | 从不读取（调大间隔也还是每分钟扫） | 真的按间隔限流 |
| `speech_to_text_qq.budgetMs` | `parse_budget` 写了却**没有调用点** | 与 timeoutMs 取小值后真的传下去 |

`pixiv_illust` 与 `forward_summary` 里剩下的"僵尸设置"是**故意未接入**的
（取图、OCR），已在各自 description 里标注，不在本次修复范围。
"""

import asyncio
import pathlib
import tempfile
import unittest

from src.ai_engine import AIEngine
from src.plugins.affinity_proactive import AffinityProactive
from src.plugins.context_chime import ContextChime
from src.plugins.speech_to_text_qq import SpeechToTextQQ


class FakeManager:
    def __init__(self, plugin_cls, state_dir, settings=None):
        self.state_dir = pathlib.Path(state_dir)
        self._settings = dict(plugin_cls.META.default_settings)
        self._settings.update(settings or {})

    def get_settings(self, plugin_id):
        return dict(self._settings)


class FakeAI:
    """记录 chat 调用参数的假引擎。"""

    configured = True

    def __init__(self):
        self.calls = []

    async def chat(self, messages, system_prompt="", temperature=None, max_tokens=None, model=None):
        self.calls.append({"model": model, "max_tokens": max_tokens, "temperature": temperature})
        return "0"


class AIEngineModelOverrideTests(unittest.TestCase):
    def engine(self, model="main-model"):
        engine = AIEngine.__new__(AIEngine)
        engine.model = model
        engine.max_tokens = 500
        engine.temperature = 0.55
        engine.api_style = "openai"
        return engine

    def test_payload_uses_instance_model_by_default(self):
        payload = self.engine()._build_payload([], "", None)
        self.assertEqual(payload["model"], "main-model")

    def test_payload_model_can_be_overridden(self):
        payload = self.engine()._build_payload([], "", None, model="cheap-model")
        self.assertEqual(payload["model"], "cheap-model")

    def test_blank_override_falls_back_to_instance_model(self):
        for value in ("", None, "   "):
            with self.subTest(value=value):
                payload = self.engine()._build_payload([], "", None, model=value)
                self.assertEqual(payload["model"], "main-model")


class ContextChimeJudgeModelTests(unittest.IsolatedAsyncioTestCase):
    def make(self, **settings):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings.setdefault("enabled", True)
        return ContextChime(FakeManager(ContextChime, tmp.name, settings))

    async def test_judge_model_is_passed_when_set(self):
        plugin = self.make(judgeModel="cheap-model")
        ai = FakeAI()
        await plugin._judge_async(ai, [{"role": "system", "content": "判据"},
                                       {"role": "user", "content": "内容"}])
        self.assertEqual(ai.calls[0]["model"], "cheap-model")

    async def test_no_judge_model_uses_instance_default(self):
        plugin = self.make(judgeModel="")
        ai = FakeAI()
        await plugin._judge_async(ai, [{"role": "user", "content": "内容"}])
        self.assertIsNone(ai.calls[0]["model"])       # 不传 → 引擎用主模型

    async def test_judge_still_pins_temperature_zero(self):
        plugin = self.make(judgeModel="m")
        ai = FakeAI()
        await plugin._judge_async(ai, [{"role": "user", "content": "x"}])
        self.assertEqual(ai.calls[0]["temperature"], 0.0)


class AffinityScanIntervalTests(unittest.IsolatedAsyncioTestCase):
    def make(self, **settings):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings.setdefault("enabled", True)
        settings.setdefault("quietStartHour", 0)
        settings.setdefault("quietEndHour", 0)
        return AffinityProactive(FakeManager(AffinityProactive, tmp.name, settings))

    async def test_interval_throttles_second_tick(self):
        plugin = self.make(scanIntervalSec=3600, affinityScores="group:1=100",
                           allowAllWhenEmpty=True)
        plugin.state()["chats"]["group:1"] = {"nextCheckAt": 0, "lastSentAt": 0}
        runtime = type("RT", (), {"mode": "onebot", "ai": FakeAI(), "onebot": None})()
        await plugin.tick(runtime)
        first_scan = plugin._last_scan
        self.assertGreater(first_scan, 0)
        # 第二次立刻调用应被限流（不改 _last_scan）
        await plugin.tick(runtime)
        self.assertEqual(plugin._last_scan, first_scan)

    async def test_large_interval_never_scans_twice(self):
        plugin = self.make(scanIntervalSec=3600)
        plugin._last_scan = 0.0
        runtime = type("RT", (), {"mode": "onebot", "ai": FakeAI(), "onebot": None})()
        await plugin.tick(runtime)
        scanned = plugin._last_scan
        for _ in range(3):
            await plugin.tick(runtime)
        self.assertEqual(scanned, plugin._last_scan)


class SpeechBudgetTests(unittest.IsolatedAsyncioTestCase):
    def make(self, **settings):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings.setdefault("enabled", True)
        return SpeechToTextQQ(FakeManager(SpeechToTextQQ, tmp.name, settings))

    async def test_budget_shorter_than_timeout_wins(self):
        plugin = self.make(budgetMs=1000, timeoutMs=5000)
        onebot = _RecordingOneBot()
        runtime = type("RT", (), {"mode": "onebot", "onebot": onebot})()
        await plugin.transcribe(runtime, {"file": "a.amr"})
        self.assertEqual(onebot.calls[0][1]["timeout"], 1000)

    async def test_timeout_shorter_than_budget_wins(self):
        plugin = self.make(budgetMs=30000, timeoutMs=2000)
        onebot = _RecordingOneBot()
        runtime = type("RT", (), {"mode": "onebot", "onebot": onebot})()
        await plugin.transcribe(runtime, {"file": "a.amr"})
        self.assertEqual(onebot.calls[0][1]["timeout"], 2000)

    async def test_invalid_budget_falls_back_to_default(self):
        plugin = self.make(budgetMs="abc", timeoutMs=9000)
        onebot = _RecordingOneBot()
        runtime = type("RT", (), {"mode": "onebot", "onebot": onebot})()
        await plugin.transcribe(runtime, {"file": "a.amr"})
        self.assertEqual(onebot.calls[0][1]["timeout"], 8000)   # 预算默认 8000 < 9000

    async def test_debug_log_does_not_break_transcription(self):
        plugin = self.make(debugLog=True)
        onebot = _RecordingOneBot()
        runtime = type("RT", (), {"mode": "onebot", "onebot": onebot})()
        self.assertEqual(await plugin.transcribe(runtime, {"file": "a.amr"}), "转写结果")


class _RecordingOneBot:
    def __init__(self):
        self.calls = []

    async def call(self, action, params=None):
        self.calls.append((action, params))
        return "转写结果"


if __name__ == "__main__":
    unittest.main()
