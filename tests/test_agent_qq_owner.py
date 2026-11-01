# -*- coding: utf-8 -*-
"""W6a：主人在 QQ 私聊里发话即授权后台操作（授权边界 + 真机证据）。

这里是**真 QQRuntime + 临时 root + 真文件系统**，走真实 ingest 入口
（不是自造 mock）。重点不是“功能能跑”，而是“谁不能跑”：

* 非主人私聊发同样的指令 → 明确拒绝，0 文件写入、0 agent_workspace 写审计；
* 主人在群聊里发 → 一样拒绝（群聊不作为授权证明）；
* 非主人拿到确认码 → 无效；
* 普通聊天（含没有待确认项时的 6 位数字）完全不受影响，照旧进 AI 队列。
"""
import asyncio
import json
import os
import pathlib
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from src import agent_workspace  # noqa: E402
from src.agent_admin import AgentAdmin  # noqa: E402
from src.agent_qq_ops import NOTE_NAME  # noqa: E402
from src.qq_app import QQRuntime  # noqa: E402

OWNER = '10001'
GUEST = '90009'          # 真 AgentAdmin 只认 5～15 位数字 QQ


def build_root(tmp):
    root = pathlib.Path(tmp)
    (root / 'styles' / 'custom').mkdir(parents=True, exist_ok=True)
    (root / 'styles' / 'default.yaml').write_text(
        'name: default\ndisplay_name: 默认\nsystem_prompt: 你是默认助手。\n', encoding='utf-8')
    (root / 'data' / 'qq-only').mkdir(parents=True, exist_ok=True)
    (root / 'config.qq.yaml').write_text(
        'ai:\n  api_url: https://example.test/v1\n  api_key: fake\n  model: test\n'
        'qq_only:\n  mode: official\n  listen_private: true\n', encoding='utf-8')
    return root


class Recorder:
    """真 transport 的网络边界替身：只记录真正被发出去的正文。"""

    def __init__(self):
        self.authenticated = True
        self.app_id = 'APP'
        self.self_id = 'BOT'
        self.sent = []

    async def send(self, message, text, meta=None):
        self.sent.append(str(text))
        return {'ok': True, 'message_id': 'm%d' % len(self.sent)}


class OwnerQqOpsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_root(self._tmp.name)
        AgentAdmin(self.root / 'agent_admin.json').bind(OWNER)
        self.bot = QQRuntime(root=self.root, config_path=self.root / 'config.qq.yaml',
                             db_path=self.root / 't.sqlite3')
        self.bot.running = True
        self.data = self.root / 'data'
        self.rec = Recorder()
        self.bot.official = self.rec

    def tearDown(self):
        try:
            self.bot.db.close()
        except Exception:
            pass
        self._tmp.cleanup()

    # ── 驱动真实入口 ───────────────────────────────────────────────────
    def say(self, text, mid, *, kind='private', who=OWNER, target=None, **extra):
        message = {'id': mid, 'kind': kind, 'text': text, 'sender_id': who,
                   'target': target or who}
        message.update(extra)
        self.bot.ingest(message, 'official')
        return message

    def deliver(self):
        """跑一小段真 worker：把队列里的预置回复真的发出去。"""
        async def run():
            task = asyncio.create_task(self.bot._work())
            for _ in range(40):
                await asyncio.sleep(0.05)
                if not self.bot.db.execute(
                        "SELECT 1 FROM inbox WHERE state='pending'").fetchone():
                    break
            self.bot.running = False
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        asyncio.run(run())
        self.bot.running = True
        return self.rec.sent[-1] if self.rec.sent else ''

    def inbox(self):
        return self.bot.db.execute(
            "SELECT state,reply,payload FROM inbox ORDER BY rowid").fetchall()

    def workspace_audits(self):
        path = agent_workspace.audit_path(self.data)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]

    def ops_audits(self):
        path = self.bot.qq_ops.audit_path()
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]

    def note(self):
        return agent_workspace.read_text(self.data, 'qq', NOTE_NAME)

    # ── 主人私聊：四类指令都通 ─────────────────────────────────────────
    def test_owner_private_write_reaches_disk(self):
        self.say('记到记事本：鲸鱼娘在水里记账', 'p-1')
        reply = self.deliver()
        self.assertIn('24 字节', reply)
        self.assertEqual(self.note()['text'], '鲸鱼娘在水里记账')
        rows = self.workspace_audits()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual((rows[0]['agent'], rows[0]['op'], rows[0]['path']), ('qq', 'write', NOTE_NAME))
        self.assertEqual(rows[0]['principal'], 'qq:' + OWNER)

    def test_owner_append_read_list_roundtrip(self):
        self.say('记到记事本：第一行', 'p-1')
        self.deliver()
        self.say('追加记事本：第二行', 'p-2')
        self.assertIn('已追加', self.deliver())
        self.say('记事本', 'p-3')
        self.assertIn('第一行第二行', self.deliver())
        self.say('列工作区', 'p-4')
        self.assertIn(NOTE_NAME, self.deliver())

    def test_owner_read_file_inside_workspace(self):
        agent_workspace.write_text(self.data, 'qq', 'doc/清单.txt', '牛奶\n鸡蛋')
        self.say('读文件 doc/清单.txt', 'p-1')
        reply = self.deliver()
        self.assertIn('牛奶', reply)
        self.assertIn('doc/清单.txt', reply)

    def test_owner_out_of_bounds_read_is_refused(self):
        self.say('读文件 ../../config.qq.yaml', 'p-1')
        reply = self.deliver()
        self.assertIn('拒', reply)
        self.assertFalse(self.note()['ok'])

    # ── 非主人 / 群聊：一律拒，且不留写痕迹 ────────────────────────────
    def test_non_owner_write_refused_and_changes_nothing(self):
        before = self.workspace_audits()
        self.say('记到记事本：入侵', 'g-1', who=GUEST, target=GUEST)
        reply = self.deliver()
        self.assertIn('不在主人名单', reply)
        self.assertIn('没有读取、也没有写入', reply)
        self.assertEqual(self.workspace_audits(), before, '非主人不能产生写审计行')
        self.assertFalse(self.note()['ok'], '非主人不能真的写进文件')

    def test_non_owner_refused_for_every_command(self):
        commands = ('记事本', '记到记事本：x', '追加记事本：x', '列工作区', '读文件 note.txt')
        for index, text in enumerate(commands):
            before = len(self.workspace_audits())
            self.say(text, 'g-%d' % index, who=GUEST, target=GUEST)
            reply = self.deliver()
            self.assertIn('不在主人名单', reply, text)
            self.assertEqual(len(self.workspace_audits()), before, text)
        self.assertFalse(self.note()['ok'])
        denied = [row for row in self.ops_audits() if row['kind'] == 'denied']
        self.assertEqual(len(denied), len(commands))
        self.assertEqual({row['code'] for row in denied}, {'not-owner'})
        self.assertEqual({row['principal'] for row in denied}, {'qq:' + GUEST})

    def test_owner_in_group_is_refused(self):
        self.say('记到记事本：群里发', 'grp-1', kind='group', target='group-7', at=True)
        reply = self.deliver()
        self.assertIn('群聊不作为授权证明', reply)
        self.assertFalse(self.note()['ok'])
        self.assertEqual(self.workspace_audits(), [])

    def test_owner_group_refused_even_without_at(self):
        self.say('列工作区', 'grp-2', kind='group', target='group-7')
        reply = self.deliver()
        self.assertIn('群聊不作为授权证明', reply)
        self.assertEqual(self.workspace_audits(), [])

    def test_non_owner_cannot_burn_owner_code(self):
        calls = []
        self.bot.qq_ops.set_executor(lambda action, detail: calls.append(action) or True)
        issued = self.bot.qq_ops.request('screenshot', '全屏')
        self.say(issued['confirm'], 'g-code', who=GUEST, target=GUEST)
        reply = self.deliver()
        self.assertIn('不在主人名单', reply)
        self.assertEqual(calls, [], '非主人拿到码也无效')
        self.assertIsNotNone(self.bot.qq_ops.pending(), '待确认项不能被他人的错误尝试消耗')

    # ── 主人判定本身 ───────────────────────────────────────────────────
    def test_owner_bound_by_agent_admin_json(self):
        self.assertIn(OWNER, self.bot.owner_ids())
        self.say('记事本', 'p-1')
        self.assertTrue(self.deliver().startswith('记事本'))

    def test_owner_ids_union_of_admin_and_pc_control(self):
        self.bot.plugins.update_settings('pc_control', {'enabled': True, 'ownerIds': '20002'})
        self.assertEqual(self.bot.owner_ids(), {OWNER, '20002'})
        self.say('记事本', 'p-2', who='20002', target='20002')
        self.assertNotIn('不在主人名单', self.deliver())

    # ── 普通聊天不受影响 ───────────────────────────────────────────────
    def test_plain_chat_still_goes_to_ai_queue(self):
        self.say('你好呀', 'p-chat')
        rows = self.inbox()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0][0], 'pending')
        self.assertEqual(rows[0][1], '', '普通消息不能被指令面预置回复')
        self.assertNotIn('_robot_admin', json.loads(rows[0][2]))

    def test_six_digits_without_pending_are_plain_chat(self):
        self.say('424242', 'p-num')
        rows = self.inbox()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], '')
        self.assertEqual(self.ops_audits(), [], '没有待确认项时不该有确认码审计')

    def test_ops_reply_is_marked_robot_admin(self):
        self.say('列工作区', 'p-1')
        payload = json.loads(self.inbox()[0][2])
        self.assertTrue(payload.get('_robot_admin'),
                        '主人指令回执不该被暂停开关或对外改写吞掉')

    # ── 真机证据 2：确认码全流程 ───────────────────────────────────────
    def test_code_flow_request_use_replay(self):
        calls = []

        def executor(action, detail):
            calls.append((action, detail))
            return {'ok': True, 'detail': '已模拟截图 %s' % detail}

        self.bot.qq_ops.set_executor(executor)
        issued = self.bot.qq_ops.request('screenshot', '全屏')
        code = issued['confirm']
        self.assertEqual(len(code), 6)
        self.assertIn('回复这 6 位数字', issued['text'])
        self.say(code, 'p-code-1')
        self.assertIn('已执行', self.deliver())
        self.assertEqual(calls, [('screenshot', '全屏')])
        self.say(code, 'p-code-2')
        self.assertIn('用过了', self.deliver())
        self.assertEqual(len(calls), 1)
        kinds = [row['kind'] for row in self.ops_audits()]
        self.assertEqual(kinds.count('code-used'), 1)
        self.assertEqual(kinds.count('code-replay'), 1)


if __name__ == '__main__':
    unittest.main()
