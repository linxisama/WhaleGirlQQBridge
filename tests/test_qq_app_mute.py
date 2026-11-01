import time
import unittest
from types import SimpleNamespace

from src.qq_app import QQRuntime


class MutedRoomTests(unittest.TestCase):
    def runtime(self, **config):
        rt = QQRuntime.__new__(QQRuntime)
        rt.bridge = SimpleNamespace(self_id='323485551')
        rt.events = []
        rt._muted_rooms = {}
        rt._mute_note_times = {}
        rt.config = {'qq_only.mode': 'qqbridge', 'qq.mute_cooldown_sec': 600, **config}
        return rt

    def test_1400_mutes_group_and_only_notes_once(self):
        rt = self.runtime()
        message = {'kind': 'group', 'target': 343973197}
        error = ('QQ 直连调用 send_group_msg 失败：错误码 1400（账号连接不可用或动作未确认，'
                 '请查看统一框架状态；发送失败不要自动重试。）')
        self.assertTrue(rt.mark_room_muted(message, error))
        self.assertTrue(rt.is_room_muted('group', 343973197))
        self.assertEqual(len(rt.events), 1)
        self.assertIn('343973197', rt.events[0]['text'])
        self.assertTrue(rt.mark_room_muted(message, error))
        self.assertEqual(len(rt.events), 1)

    def test_mute_can_be_cleared_after_a_successful_send(self):
        rt = self.runtime()
        rt.mark_room_muted({'kind': 'group', 'target': 1}, '错误码 11255 机器人已被禁言')
        self.assertTrue(rt.is_room_muted('group', 1))
        rt.clear_room_mute('group', 1)
        self.assertFalse(rt.is_room_muted('group', 1))

    def test_unrelated_error_does_not_mute_a_group(self):
        rt = self.runtime()
        self.assertFalse(rt.mark_room_muted({'kind': 'group', 'target': 1}, 'HTTP 500 服务不可用'))
        self.assertFalse(rt.is_room_muted('group', 1))
        self.assertEqual(rt.events, [])

    def test_private_and_expired_scopes_are_not_muted(self):
        rt = self.runtime()
        self.assertFalse(rt.mark_room_muted({'kind': 'private', 'target': 1}, '错误码 1400'))
        key = rt._mute_key('group', 2)
        rt._muted_rooms[key] = time.time() - 1
        self.assertFalse(rt.is_room_muted('group', 2))
        self.assertNotIn(key, rt._muted_rooms)

    def test_ingest_drops_new_group_message_without_touching_the_queue(self):
        rt = self.runtime()
        rt.running = True
        rt.qq_ops = SimpleNamespace(handle=lambda message: None)
        rt.plugins = SimpleNamespace(_plugins={})
        rt.conversation_allowed = lambda kind, target, sender: True
        rt.db = SimpleNamespace(execute=lambda *args, **kwargs: self.fail('muted message reached the queue'))
        rt.mark_room_muted({'kind': 'group', 'target': 343973197}, '错误码 11255 机器人已被禁言')
        rt.ingest({'kind': 'group', 'target': '343973197', 'text': '你好', 'sender_id': '1', 'id': 'x'},
                  'qqbridge')


if __name__ == '__main__':
    unittest.main()
