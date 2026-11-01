"""DSH 记忆桥：stdio MCP 协议、搜索、写入手动记忆与偏好。"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'tools' / 'whale_memory_mcp.py'


class MemoryMcpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        (home / 'data' / 'qq-only').mkdir(parents=True)
        self.db = home / 'data' / 'qq-only' / 'messages.sqlite3'
        conn = sqlite3.connect(self.db)
        conn.executescript('''
        CREATE TABLE self_memories(id TEXT PRIMARY KEY,persona TEXT,content TEXT,enabled INTEGER,created REAL);
        CREATE TABLE chat_memories(id TEXT PRIMARY KEY,scope TEXT,subject TEXT,content TEXT,status TEXT,
          source TEXT,hits INTEGER,created REAL,updated REAL);
        ''')
        conn.execute("INSERT INTO self_memories VALUES('m1','whale','用户喜欢鲸鱼',1,1)")
        conn.execute("INSERT INTO chat_memories VALUES('c1','group:1','u1','用户住在杭州','confirmed','chat',2,1,1)")
        conn.commit(); conn.close()
        self.learning = home / 'agent_learning.json'
        self.learning.write_text(json.dumps({'version': 1, 'preferences': {'称呼': {'value': '老鲸'}},
                                             'lessons': [{'id': 'l1', 'intent': '把回复调快',
                                                          'summary': '等待 15 秒', 'ok': 2, 'fail': 0,
                                                          'actions': []}],
                                             'skills': []}, ensure_ascii=False), encoding='utf-8')
        (home / 'config.qq.yaml').write_text('style:\n  active: whale\n', encoding='utf-8')
        self.env = dict(os.environ, WHALE_QQ_HOME=str(home), PYTHONIOENCODING='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def session(self, messages):
        payload = '\n'.join(json.dumps(message, ensure_ascii=False) for message in messages) + '\n'
        result = subprocess.run([sys.executable, str(SCRIPT)], input=payload.encode('utf-8'),
                                capture_output=True, env=self.env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        return [json.loads(line) for line in result.stdout.decode('utf-8').splitlines() if line.strip()]

    def call(self, name, arguments=None, identifier=9):
        return self.session([{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                              'params': {'protocolVersion': '2025-06-18'}},
                             {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                             {'jsonrpc': '2.0', 'id': identifier, 'method': 'tools/call',
                              'params': {'name': name, 'arguments': arguments or {}}}])[-1]

    def test_initialize_and_tool_list(self):
        replies = self.session([{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                 'params': {'protocolVersion': '2025-06-18'}},
                                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}])
        self.assertEqual(replies[0]['result']['protocolVersion'], '2025-06-18')
        self.assertEqual(replies[0]['result']['serverInfo']['name'], 'whale_memory')
        names = {tool['name'] for tool in replies[1]['result']['tools']}
        self.assertEqual(names, {'search_memory', 'remember', 'save_preference',
                                 'list_preferences', 'forget_memory', 'memory_status',
                                 'maintain_memory', 'remember_unified', 'read_diary', 'write_diary'})

    def test_search_returns_manual_structured_and_learning(self):
        reply = self.call('search_memory', {'query': ''})
        payload = json.loads(reply['result']['content'][0]['text'])
        self.assertEqual(payload['manual'][0]['content'], '用户喜欢鲸鱼')
        self.assertEqual(payload['structured'][0]['content'], '用户住在杭州')
        self.assertEqual(payload['lessons'][0]['intent'], '把回复调快')
        self.assertEqual(payload['preferences'][0]['key'], '称呼')

    def test_remember_writes_manual_memory_and_dedupes(self):
        reply = self.call('remember', {'text': '用户养了一只猫'})
        payload = json.loads(reply['result']['content'][0]['text'])
        self.assertFalse(payload['duplicate'])
        again = json.loads(self.call('remember', {'text': '用户养了一只猫'})['result']['content'][0]['text'])
        self.assertTrue(again['duplicate'])
        conn = sqlite3.connect(self.db)
        rows = conn.execute("SELECT content FROM self_memories WHERE persona='whale'").fetchall()
        conn.close()
        self.assertEqual([row[0] for row in rows], ['用户喜欢鲸鱼', '用户养了一只猫'])

    def test_save_preference_updates_learning_file(self):
        self.call('save_preference', {'key': '语气', 'value': '简短'})
        data = json.loads(self.learning.read_text(encoding='utf-8'))
        self.assertEqual(data['preferences']['语气']['value'], '简短')
        listed = json.loads(self.call('list_preferences')['result']['content'][0]['text'])
        self.assertEqual({item['key'] for item in listed['preferences']}, {'称呼', '语气'})

    def test_status_and_forget(self):
        status = json.loads(self.call('memory_status')['result']['content'][0]['text'])
        self.assertEqual(status['manual'], 1)
        self.assertEqual(status['structured'], 1)
        self.assertEqual(status['persona'], 'whale')
        removed = json.loads(self.call('forget_memory', {'id': 'm1'})['result']['content'][0]['text'])
        self.assertEqual(removed['removed'], 1)

    def test_tool_errors_are_structured_not_crash(self):
        reply = self.call('remember', {'text': ''})
        self.assertTrue(reply['result']['isError'])
        reply = self.call('nope')
        self.assertTrue(reply['result']['isError'])

    def test_unknown_method_returns_jsonrpc_error(self):
        reply = self.session([{'jsonrpc': '2.0', 'id': 5, 'method': 'nope'}])[0]
        self.assertEqual(reply['error']['code'], -32601)


if __name__ == '__main__':
    unittest.main()
