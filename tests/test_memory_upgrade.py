"""记忆升级：模糊召回、冲突覆盖、手动记忆预算、整理接口。"""
import sqlite3
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from src.chat_state import ChatState, supersedes


def build_state():
    conn = sqlite3.connect(':memory:')
    return ChatState(conn), conn


class MemoryRecallTests(unittest.TestCase):
    def setUp(self):
        self.state, self.conn = build_state()

    def tearDown(self):
        self.conn.close()

    def confirm(self, text, source):
        self.state.remember('room', 'A', text, source, maximum=200)

    def test_fuzzy_recall_finds_related_fact(self):
        self.confirm('我喜欢蓝色', 's1')
        self.confirm('我喜欢蓝色', 's2')
        hits = self.state.recall('room', 'A', '颜色喜欢什么')
        self.assertTrue(hits)
        self.assertEqual(hits[0]['content'], '我喜欢蓝色')
        self.assertGreater(hits[0]['score'], 0)

    def test_empty_query_ranks_by_freshness_and_evidence(self):
        self.confirm('旧的偏好', 's1')
        self.confirm('旧的偏好', 's2')
        self.confirm('新的偏好', 's3')
        self.confirm('新的偏好', 's4')
        items = self.state.recall('room', 'A')
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['content'], '新的偏好')

    def test_relocation_supersedes_old_fact(self):
        for source in ('s1', 's2'):
            self.confirm('我住在杭州', source)
        for source in ('s3', 's4'):
            self.confirm('我搬到上海了', source)
        hits = self.state.recall('room', 'A', '我现在住哪')
        contents = [item['content'] for item in hits]
        self.assertIn('我搬到上海了', contents)
        self.assertNotIn('我住在杭州', contents)
        statuses = dict(self.conn.execute('SELECT content,status FROM chat_memories').fetchall())
        self.assertEqual(statuses['我住在杭州'], 'superseded')
        self.assertEqual(statuses['我搬到上海了'], 'confirmed')

    def test_unrelated_preferences_do_not_supersede(self):
        for source in ('s1', 's2'):
            self.confirm('我喜欢猫', source)
        for source in ('s3', 's4'):
            self.confirm('我喜欢狗', source)
        contents = [item['content'] for item in self.state.recall('room', 'A', '喜欢什么')]
        self.assertIn('我喜欢猫', contents)
        self.assertIn('我喜欢狗', contents)

    def test_supersedes_helper_is_conservative(self):
        self.assertTrue(supersedes('我不喜欢猫了', '我喜欢猫'))
        self.assertFalse(supersedes('我喜欢狗', '我喜欢猫'))
        self.assertFalse(supersedes('我住在杭州', '我住在上海'))  # 没有变化词，视为两条陈述

    def test_sensitive_fact_rejected(self):
        for text in ('我的银行卡是 6222', '身份证号 3301', 'api key 是 abc'):
            with self.assertRaises(ValueError):
                self.state.remember('room', 'A', text, 's1')


class ManualSelectionTests(unittest.TestCase):
    def test_relevant_manual_memory_first_and_budgeted(self):
        from src.plugins.conversation_memory import select_manual
        items = [f'第{i}条记忆内容' for i in range(20)] + ['谈到加班时先关心对方']
        picked = select_manual('他今天又加班了', items)
        self.assertLessEqual(len(picked), 8)
        self.assertIn('谈到加班时先关心对方', picked)
        self.assertEqual(picked[0], '谈到加班时先关心对方')
        self.assertLessEqual(sum(len(value) for value in picked), 1800 + 8)

    def test_manual_selection_without_text_keeps_first(self):
        from src.plugins.conversation_memory import select_manual
        self.assertEqual(select_manual('', ['a', 'b', 'c'], limit=2), ['a', 'b'])


class AutoFactTests(unittest.TestCase):
    def test_clear_self_statements_are_captured(self):
        from src.plugins.conversation_memory import auto_fact
        for text in ('我喜欢蓝色', '我是学生', '我住在杭州', '我养了一只猫',
                      '我的生日是五月一号', '我今年三十岁', '我有一个妹妹'):
            self.assertEqual(auto_fact(text), text)

    def test_questions_guesses_and_jokes_are_skipped(self):
        from src.plugins.conversation_memory import auto_fact
        for text in ('我喜欢猫吗', '如果我喜欢猫', '我是说这个', '我养猫吗？', '我今天很开心'):
            self.assertIsNone(auto_fact(text), text)


class AutoRememberConfirmTests(unittest.TestCase):
    """私聊/@ 是当面自述，直接确认；群聊未点名仍先记待复证。"""

    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.state = ChatState(self.conn)
        from src.plugins.conversation_memory import ConversationMemory

        class Manager:
            _settings = {'conversation_memory': {
                'enabled': True, 'autoRemember': True, 'recallLimit': 5,
                'maxEntries': 500}}

            def get_settings(self, plugin_id):
                return dict(self._settings[plugin_id])

        self.plugin = ConversationMemory(Manager())

        class Runtime:
            mode = 'qq'
            chat_state = self.state

            def account(self):
                return '10000'

        self.runtime = Runtime()

    def tearDown(self):
        self.conn.close()

    def ingest(self, **message):
        payload = {'kind': 'private', 'target': '999', 'sender_id': '999',
                   'text': '我的生日是五月一号', 'id': 'm1'}
        payload.update(message)
        self.plugin.on_ingest(payload, self.runtime)

    def status(self):
        row = self.conn.execute('SELECT status FROM chat_memories').fetchone()
        return row[0] if row else ''

    def test_private_statement_is_confirmed(self):
        self.ingest()
        self.assertEqual(self.status(), 'confirmed')

    def test_group_without_at_stays_candidate(self):
        self.ingest(kind='group', target='12345', text='我今年三十岁', id='m2')
        self.assertEqual(self.status(), 'candidate')

    def test_group_at_is_confirmed(self):
        self.ingest(kind='group', target='12345', text='我有一个妹妹', id='m3', at=True)
        self.assertEqual(self.status(), 'confirmed')


class MemoryMaintainApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from src.qq_app import QQRuntime, create_app
        from tests.test_persona_files import build_root
        cls.tmp = tempfile.TemporaryDirectory()
        root = build_root(cls.tmp.name)
        cls.bot = QQRuntime(root=root, db_path=root / 'test.sqlite')
        cls.client = TestClient(create_app(cls.bot))
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.tmp.cleanup()

    def _unified(self, text, layer='fact'):
        """直接插一行统一记忆：remember 会去重/过滤，体检用例需要人为造脏数据。"""
        import time
        import uuid
        now = time.time()
        identifier = uuid.uuid4().hex
        self.bot.db.execute(
            'INSERT INTO unified_memories(id,source,content,importance,created,last_used,uses,'
            'expires_at,tags,persona,layer,keywords,project,status,confidence,updated,evidence,'
            'goal,channel) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (identifier, 'qq', text, 50, now, now, 0, 0, '[]', '', layer, '[]', '',
             'active', 60, now, '', '', 'qq'))
        self.bot.db.commit()
        self.addCleanup(lambda: (self.bot.db.execute(
            'DELETE FROM unified_memories WHERE id=?', (identifier,)), self.bot.db.commit()))
        return identifier

    def _content(self, identifier):
        row = self.bot.db.execute('SELECT content FROM unified_memories WHERE id=?',
                                  (identifier,)).fetchone()
        return row[0] if row else None

    def test_guard_report_route_is_read_only(self):
        identifier = self._unified('长' * 260)
        body = self.client.get('/api/memory/guard').json()
        self.assertTrue(body['success'])
        self.assertIn('fact', body['limits'])
        self.assertGreaterEqual(len(body['weak_keywords']), 1)
        dry = self.client.post('/api/memory/maintain', json={'apply': False}).json()
        self.assertFalse(dry['guard']['applied'])
        self.assertIn('关键词偏少', dry['message'])
        self.assertEqual(len(self._content(identifier)), 260)

    def test_maintain_reports_guard_counts_and_hides_secrets(self):
        long_id = self._unified('长' * 260)
        secret_id = self._unified('api_key: sk-abcdefghijklmnopqrst')
        response = self.client.post('/api/memory/maintain', json={'apply': True})
        body = response.json()
        self.assertGreaterEqual(body['guard']['truncated'], 1)
        self.assertGreaterEqual(body['guard']['masked'], 1)
        self.assertNotIn('sk-abcdefghijklmnopqrst', response.text)
        self.assertIn('密钥打码 1 条', body['message'])
        self.assertEqual(len(self._content(long_id)), 200)
        self.assertNotIn('sk-abcdefghijklmnopqrst', self._content(secret_id))

    def _inbox(self, text, sender='10001', mode=None, state='sent'):
        """造一条主人私聊：提取用例只看 inbox，不需要真连 QQ。"""
        import json
        import time
        account = self.bot.account()
        mode = mode or self.bot.mode
        self.bot.db.execute(
            'INSERT INTO inbox(key,mode,account,room,payload,state,reply,created) '
            "VALUES(?,?,?,?,?,?,'',?)",
            ('ingest-' + str(abs(hash((text, sender, time.time())))), mode, account,
             'private:' + sender, json.dumps({'sender_id': sender, 'text': text},
                                             ensure_ascii=False), state, time.time()))
        self.bot.db.commit()

    def test_ingest_preview_then_apply(self):
        from unittest import mock
        text = '以后统一用简体中文回复我'
        self._inbox(text)
        self._inbox('以后只给主人发文字', sender='10002')     # 不是主人，忽略
        self.addCleanup(lambda: (self.bot.db.execute(
            'DELETE FROM inbox WHERE json_extract(payload,\'$.sender_id\')=?', ('10001',)),
            self.bot.db.commit()))
        with mock.patch.object(self.bot, 'owner_ids', lambda: ['10001']):
            preview = self.client.get('/api/memory/ingest?days=1').json()
            self.assertTrue(preview['success'])
            self.assertEqual(preview['added'], 0)
            self.assertEqual([item['text'] for item in preview['candidates']], [text])
            self.assertIn('候选', preview['message'])
            before = self.bot.unified_memory.count()
            applied = self.client.post('/api/memory/ingest',
                                       json={'apply': True, 'days': 1}).json()
            self.assertEqual(applied['added'], 1)
            self.assertEqual(self.bot.unified_memory.count(), before + 1)
            self.assertIn('已写入 1 条', applied['message'])
        self.addCleanup(lambda: (self.bot.db.execute(
            "DELETE FROM unified_memories WHERE content=?", (text,)), self.bot.db.commit()))

    def test_ingest_without_owner_binding_reports_plainly(self):
        from unittest import mock
        with mock.patch.object(self.bot, 'owner_ids', lambda: []):
            body = self.client.get('/api/memory/ingest').json()
        self.assertTrue(body['success'])
        self.assertEqual(body['checked'], 0)
        self.assertIn('没有主人私聊记录', body['message'])

    def test_maintain_merges_duplicates_and_reports(self):
        state = self.bot.chat_state
        for source in ('s1', 's2'):
            state.remember('room', 'A', '我喜欢蓝色', source)
        state.remember('room', 'A', '我喜欢蓝色！', 's3')
        before = self.bot.db.execute('SELECT count(*) FROM chat_memories').fetchone()[0]
        response = self.client.post('/api/memory/maintain')
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['success'])
        self.assertGreaterEqual(body['merged'], 1)
        rows = self.bot.db.execute("SELECT content,hits FROM chat_memories").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(rows[0][1], 3)
        # 备份必须发生在旧的 chat_memories 清理之前，否则丢掉的重复行找不回来。
        backup = body['guard']['backup']
        self.assertTrue(backup['name'], backup)
        self.assertTrue(Path(backup['path']).exists())
        self.assertIn('备份 ' + backup['name'], body['message'])
        conn = sqlite3.connect(backup['path'])
        try:
            self.assertEqual(conn.execute('SELECT count(*) FROM chat_memories').fetchone()[0], before)
        finally:
            conn.close()



    def test_dream_and_maintain_back_up_first(self):
        for action in ('dream', 'maintain'):
            response = self.client.post('/api/memory/unified', json={'action': action})
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertTrue(body['success'], body)
            backup = body.get('backup') or {}
            self.assertTrue(backup.get('name'), body)
            self.assertTrue(Path(backup['path']).exists(), body)

    def test_backups_route_lists_generated_backups(self):
        self.client.post('/api/memory/maintain', json={'apply': True})
        response = self.client.get('/api/memory/backups')
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['success'])
        self.assertTrue(Path(body['dir']).is_dir(), body)
        self.assertGreaterEqual(body['count'], 1, body)
        self.assertTrue(all(item['name'].startswith('memory-') for item in body['items']))

    def test_report_route_writes_markdown(self):
        identifier = self._unified(r'资料在 D:\no-such-dir-zzz\plan.txt 里，api_key: sk-abcdefghijklmnop')
        response = self.client.post('/api/memory/report')
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['success'])
        target = Path(body['path'])
        self.assertTrue(target.exists())
        text = target.read_text(encoding='utf-8')
        self.assertIn('记忆体检报告', text)
        self.assertIn('二、疑似失效', text)
        self.assertIn('体检报告已写入', body['message'])
        self.assertEqual(self.bot.unified_memory.get(identifier)['status'], 'active')


if __name__ == '__main__':
    unittest.main()
