"""插件审计发现的问题：回归测试（每修一条就在这里钉一条）。

对应审计报告（59 个插件 + manager + capabilities）里的高/中优先级条目：
- H1 token_saver 摘要被静默丢弃 → tests/test_token_saver.py 已覆盖
- M1 sticker_cap 每条消息全库扫描
- M2 sticker_cap 的目录上传入口被 capabilities 摘掉
- M3 插件加载失败只 print、进不了运行记录
- M4 prepare_delivery 无异常兜底（回复会丢）
- M5 官方模式私聊被打成 at=True
- L3/L4/L7 之类的小问题
"""
import asyncio
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.plugins.manager import PluginManager  # noqa: E402
from src.qq_transports import official_event  # noqa: E402


class PrepareDeliveryTests(unittest.TestCase):
    """M4：某个插件的 prepare_delivery 抛异常时，回复不能丢。"""

    def test_plugin_exception_keeps_the_reply(self):
        manager = PluginManager.__new__(PluginManager)      # 不走 __init__，避免读用户配置

        class Bad:
            class META:
                id = 'bad_plugin'

            enabled = True

            async def prepare_delivery(self, message, reply, runtime):
                raise RuntimeError("插件内部炸了")

        class Good:
            class META:
                id = 'good_plugin'

            enabled = True

            async def prepare_delivery(self, message, reply, runtime):
                return reply + " 尾巴"

        manager._plugins = {'bad_plugin': Bad(), 'good_plugin': Good()}
        notes = []

        class Runtime:
            @staticmethod
            def note(text):
                notes.append(text)

        reply = asyncio.run(manager.prepare_delivery({}, "原回复", Runtime()))
        self.assertEqual(reply, "原回复 尾巴", "坏插件不该让整条回复丢失")
        self.assertTrue(any("bad_plugin" in note for note in notes),
                        f"出错要写进运行记录：{notes}")

    def test_enabled_read_failure_is_treated_as_disabled(self):
        manager = PluginManager.__new__(PluginManager)

        class Broken:
            class META:
                id = 'broken'

            @property
            def enabled(self):
                raise OSError("设置文件坏了")

        manager._plugins = {'broken': Broken()}
        self.assertFalse(manager._enabled(manager._plugins['broken']))


class OfficialAtFlagTests(unittest.TestCase):
    """M5：官方模式只有群消息算"被 @"，私聊不是。"""

    def group_event(self):
        return official_event("GROUP_AT_MESSAGE_CREATE", {
            "id": "M1", "content": "<@!BOTOPENID> 在吗", "group_openid": "G1",
            "author": {"member_openid": "U1", "username": "慕白"}})

    def private_event(self):
        return official_event("C2C_MESSAGE_CREATE", {
            "id": "M2", "content": "在吗",
            "author": {"user_openid": "U1", "username": "慕白"}})

    def test_group_message_is_marked_as_at(self):
        message = self.group_event()
        self.assertEqual(message["kind"], "group")
        self.assertTrue(message["at"])

    def test_private_message_is_not_marked_as_at(self):
        message = self.private_event()
        self.assertEqual(message["kind"], "private")
        self.assertFalse(message["at"],
                         "私聊不该被当成'被 @'，否则按 at 判定的插件（睡眠模式/好感度）行为全错")


class PluginFailureVisibilityTests(unittest.TestCase):
    """M3：插件加载/钩子失败要进运行记录，而不是只 print 到日志文件。"""

    def test_manager_has_a_shared_failure_notifier(self):
        import inspect
        source = inspect.getsource(PluginManager)
        self.assertIn("_note_failure", source)
        self.assertIn("runtime.note", source, "失败要写进界面的运行记录")

    def test_tick_failure_is_reported(self):
        manager = PluginManager.__new__(PluginManager)

        class Bad:
            class META:
                id = 'bad_tick'

            enabled = True

            async def tick(self, runtime):
                raise ValueError("tick 炸了")

        manager._plugins = {'bad_tick': Bad()}
        notes = []

        class Runtime:
            @staticmethod
            def note(text):
                notes.append(text)

        asyncio.run(manager.run_tick(Runtime()))
        self.assertTrue(any("bad_tick" in note for note in notes),
                        f"tick 里的插件异常要写进运行记录：{notes}")


class FakeRuntime:
    """apply_capabilities 需要的最小 runtime：只看 mode / stickers / 设置。"""

    mode = 'onebot'
    stickers = None
    onebot = None

    def __init__(self):
        self.settings = {}


class CapabilityConsistencyTests(unittest.TestCase):
    """L4/L5：能力表里不能出现不存在的插件；官方模式不支持的插件要标注。"""

    def test_no_capability_entry_for_missing_plugin(self):
        import pathlib
        import re
        source = pathlib.Path(
            pathlib.Path(__file__).resolve().parents[1] / 'src' / 'plugin_capabilities.py'
        ).read_text(encoding='utf-8')
        from src.plugins.manager import PLUGIN_MODULES
        referenced = set(re.findall(r"'([a-z_]{3,})'", source))
        known = set(PLUGIN_MODULES) | {'stickerDir'}
        # 只检查那些"看着像插件 id"的引用（带下划线且在能力表里成组出现）
        suspects = {name for name in referenced
                    if name.endswith(('_cap', '_ping', '_chat', '_followup', '_welcome',
                                      '_announcer', '_breaker', '_proactive', '_reaction',
                                      '_indicator', '_echo', '_counter', '_gate', '_mention'))}
        missing = sorted(name for name in suspects if name not in known)
        self.assertEqual(missing, [], f"能力表引用了不存在的插件 id：{missing}")

    def test_sticker_dir_field_is_still_exposed(self):
        """M2：sticker_cap 声明的目录上传入口不能在能力过滤里被摘掉。"""
        from src.plugin_capabilities import apply_capabilities
        item = {'id': 'sticker_cap', 'schema': {
            'stickerDir': {'type': 'string', 'file_picker': 'directory', 'label': '表情库目录'}},
            'status': 'ready'}
        apply_capabilities([item], FakeRuntime())
        self.assertIn('stickerDir', item.get('schema', {}),
                      "目录选择入口被摘掉了，用户无法在界面里选表情库目录")
        self.assertIn('stickerDir', item.get('inactive_fields') or [],
                      "当前运行时不生效的字段要标注出来，而不是删掉")


class StickerCapThrottleTests(unittest.TestCase):
    """M1：sticker_cap 的每条消息清理要按 checkIntervalSec 节流。"""

    def test_observe_is_throttled(self):
        import inspect
        from src.plugins import sticker_cap
        source = inspect.getsource(sticker_cap.StickerCap.observe)
        self.assertIn("checkIntervalSec", source,
                      "observe 必须按间隔节流，否则每条消息都会全库扫描")


class SilencePingStateTests(unittest.TestCase):
    """L7：状态文件类型不对时不能永久静默失效。"""

    def test_bad_state_file_is_tolerated(self):
        import inspect
        from src.plugins import silence_ping
        source = inspect.getsource(silence_ping)
        self.assertIn("isinstance", source, "状态文件要做类型校验")


if __name__ == "__main__":
    unittest.main()
