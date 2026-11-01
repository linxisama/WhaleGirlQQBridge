# -*- coding: utf-8 -*-
"""W6a：QQ 主人指令面 + 工作区读写 + 一次性确认码（模块级）。

这里测的是 src/agent_qq_ops.py 本身：命令面解析、四类工作区指令、
确认码生命周期与审计。真机链路（真 QQRuntime + 真 ingest）在
tests/test_agent_qq_owner.py 里测，两边合起来覆盖授权边界。
"""
import json
import os
import pathlib
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from src import agent_qq_ops, agent_workspace  # noqa: E402
from src.agent_qq_ops import (AUDIT_FILE_NAME, CODE_DIGITS, CODE_TTL,  # noqa: E402
                              LIST_PREVIEW_ITEMS, NOTE_NAME, READ_PREVIEW_CHARS,
                              OwnerOps, parse_command)


class ParseTests(unittest.TestCase):
    """命令面只有固定的几个词——自由命令字符串进不来。"""

    def test_note_read_words(self):
        for text in ('记事本', '读记事本', '记事本。', ' 记事本 '):
            self.assertEqual(parse_command(text), {'kind': 'note-read'}, text)

    def test_note_read_is_not_a_prefix_match(self):
        # “记事本在哪啊”是闲聊，不能被当成指令，更不能替主人读文件。
        self.assertIsNone(parse_command('记事本在哪啊'))
        self.assertIsNone(parse_command('读记事本了吗'))

    def test_note_write_and_append(self):
        self.assertEqual(parse_command('记到记事本：买了牛奶'),
                         {'kind': 'note-write', 'content': '买了牛奶'})
        self.assertEqual(parse_command('追加记事本:第二行'),
                         {'kind': 'note-append', 'content': '第二行'})
        self.assertEqual(parse_command('记到记事本：'),
                         {'kind': 'note-write', 'content': ''})

    def test_list_and_read_file(self):
        self.assertEqual(parse_command('列工作区'), {'kind': 'list'})
        self.assertEqual(parse_command('读文件 note.txt'),
                         {'kind': 'read-file', 'relpath': 'note.txt'})
        self.assertEqual(parse_command('读文件'), {'kind': 'read-file', 'relpath': ''})

    def test_free_shell_and_bare_numbers_are_not_commands(self):
        for text in ('rm -rf /', 'cmd /c dir', '记事本; del note.txt',
                     '123456', '打开记事本'):
            self.assertIsNone(parse_command(text), text)

    def test_shell_syntax_is_a_literal_path_not_a_command(self):
        # “读文件 && whoami” 只会被当成一个叫 “&& whoami” 的相对路径去读，
        # 绝不解释成命令；读不到就报读不到。
        self.assertEqual(parse_command('读文件 && whoami'),
                         {'kind': 'read-file', 'relpath': '&& whoami'})
        ops = OwnerOps(None, owners=lambda: {'owner-1'})
        result = ops.handle({'kind': 'private', 'target': 'owner-1',
                             'sender_id': 'owner-1', 'text': '读文件 && whoami'})
        self.assertFalse(result['ok'])
        self.assertIn(result['status'], ('not_found', 'denied', 'invalid'))


class OwnerOpsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = pathlib.Path(self._tmp.name) / 'data'
        self.now = [1_000_000.0]
        self.ops = OwnerOps(self.data,
                            owners=lambda: {'owner-1'},
                            clock=lambda: self.now[0],
                            code_factory=lambda: '424242')

    def tearDown(self):
        self._tmp.cleanup()

    # ── 小工具 ─────────────────────────────────────────────────────────
    def owner(self, text, **extra):
        message = {'id': 'm-' + str(abs(hash(text)) % 99999), 'kind': 'private',
                   'target': 'owner-1', 'sender_id': 'owner-1', 'text': text}
        message.update(extra)
        return self.ops.handle(message)

    def note(self):
        return agent_workspace.read_text(self.data, 'qq', NOTE_NAME)

    def audits(self, kind=None):
        path = self.ops.audit_path()
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]
        return [row for row in rows if kind is None or row.get('kind') == kind]

    def workspace_audits(self):
        path = agent_workspace.audit_path(self.data)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]

    # ── 四类工作区指令 ─────────────────────────────────────────────────
    def test_write_creates_note_and_reports_bytes(self):
        result = self.owner('记到记事本：鲸鱼娘在水里记账')
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.note()['text'], '鲸鱼娘在水里记账')
        self.assertIn('24 字节', result['text'], '要回字节数，主人才知道写进去多少')
        self.assertNotIn(':', result['text'].replace('：', ''),
                         '回复只给工作区相对路径，不能泄露盘符')

    def test_append_keeps_previous_content(self):
        self.owner('记到记事本：第一行')
        result = self.owner('追加记事本：第二行')
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.note()['text'], '第一行第二行')

    def test_read_note_returns_content_and_size(self):
        self.owner('记到记事本：买牛奶')
        result = self.owner('记事本')
        self.assertTrue(result['ok'])
        self.assertIn('买牛奶', result['text'])
        self.assertIn('note.txt', result['text'])

    def test_read_missing_note_says_it_is_empty(self):
        result = self.owner('读记事本')
        self.assertTrue(result['ok'], '没有文件不算越权，要明确说“还是空的”')
        self.assertIn('空', result['text'])
        self.assertFalse(self.audits('denied'))

    def test_list_workspace_shows_entries(self):
        self.owner('记到记事本：x')
        result = self.owner('列工作区')
        self.assertTrue(result['ok'], result)
        self.assertIn(NOTE_NAME, result['text'])
        self.assertIn('1 项', result['text'])

    def test_list_workspace_empty(self):
        result = self.owner('列工作区')
        self.assertTrue(result['ok'])
        self.assertIn('空', result['text'])

    def test_read_file_returns_text(self):
        agent_workspace.write_text(self.data, 'qq', 'sub/a.txt', '内层文件')
        result = self.owner('读文件 sub/a.txt')
        self.assertTrue(result['ok'], result)
        self.assertIn('内层文件', result['text'])
        self.assertIn('sub/a.txt', result['text'])

    def test_read_file_out_of_bounds_is_denied(self):
        result = self.owner('读文件 ../../config.qq.yaml')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'denied')
        self.assertIn('拒绝', result['text'])
        self.assertIn('..', result['text'], '要说明为什么拒，而不是只说失败')

    def test_read_file_absolute_path_is_denied(self):
        for rel in ('C:/Windows/win.ini', '/etc/passwd', '\\\\server\\share\\x.txt'):
            result = self.owner('读文件 ' + rel)
            self.assertFalse(result['ok'], rel)
            self.assertEqual(result['status'], 'denied', rel)

    def test_read_preview_truncates_long_text(self):
        long_text = '记' * (READ_PREVIEW_CHARS + 500)
        self.owner('记到记事本：' + long_text)
        result = self.owner('记事本')
        self.assertIn('已截断', result['text'])
        self.assertLess(len(result['text']), READ_PREVIEW_CHARS + 120,
                        '摘要上限写进 docstring 了，不能整篇回灌给模型')
        self.assertIn(str(READ_PREVIEW_CHARS), result['text'])

    def test_empty_content_writes_nothing(self):
        result = self.owner('记到记事本：   ')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'empty')
        self.assertFalse(self.note()['ok'], '空内容不能把记事本清空')
        self.assertEqual(self.workspace_audits(), [], '没写成功就不该有写审计')

    # ── 确认码 ─────────────────────────────────────────────────────────
    def test_confirm_success_runs_executor_once(self):
        calls = []
        self.ops.set_executor(lambda action, detail: calls.append((action, detail)) or {'ok': True})
        issued = self.ops.request('screenshot', '全屏')
        self.assertEqual(len(issued['confirm']), CODE_DIGITS)
        self.assertTrue(issued['confirm'].isdigit())
        self.assertIn(issued['confirm'], issued['text'])
        self.assertEqual(calls, [], '生成确认码本身绝不能执行动作')
        result = self.owner(issued['confirm'])
        self.assertTrue(result['ok'], result)
        self.assertIn('已执行', result['text'])
        self.assertEqual(calls, [('screenshot', '全屏')])
        self.assertIsNone(self.ops.pending(), '用掉之后就没有待确认项了')

    def test_wrong_code_does_not_consume_pending(self):
        calls = []
        self.ops.set_executor(lambda action, detail: calls.append(action) or True)
        self.ops.request('service', '重启机器人')
        wrong = self.owner('000000')
        self.assertFalse(wrong['ok'])
        self.assertEqual(wrong['status'], 'mismatch')
        self.assertEqual(calls, [])
        self.assertIsNotNone(self.ops.pending(), '错码不该把待确认项烧掉')
        self.assertTrue(self.owner('424242')['ok'], '紧接着用对码仍然能成功')

    def test_expired_code_is_rejected_and_not_executed(self):
        calls = []
        self.ops.set_executor(lambda action, detail: calls.append(action) or True)
        self.ops.request('screenshot', ttl=CODE_TTL)
        self.now[0] += CODE_TTL + 1
        result = self.owner('424242')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'expired')
        self.assertIn('过期', result['text'])
        self.assertEqual(calls, [], '过期码不能执行')
        again = self.owner('424242')
        self.assertIn(again['status'], ('expired', 'replayed'))

    def test_code_is_one_time(self):
        calls = []
        self.ops.set_executor(lambda action, detail: calls.append(action) or True)
        self.ops.request('hotkey', 'ctrl+s')
        self.assertTrue(self.owner('424242')['ok'])
        replay = self.owner('424242')
        self.assertFalse(replay['ok'])
        self.assertEqual(replay['status'], 'replayed')
        self.assertIn('用过了', replay['text'])
        self.assertEqual(len(calls), 1, '重放不能第二次执行')

    def test_new_request_replaces_pending(self):
        self.ops.set_executor(lambda action, detail: True)
        self.ops.request('screenshot')
        second = self.ops.request('service', '重启')
        self.assertIn('上一个待确认项已作废', second['text'])
        self.assertEqual(second['confirm'], '424242')
        self.assertTrue(self.ops.audit_path().exists())
        kinds = [row['kind'] for row in self.audits()]
        self.assertIn('code-replaced', kinds)
        result = self.owner('424242')
        self.assertEqual(result['ok'], True)
        self.assertEqual(self.ops.pending(), None)

    def test_unknown_action_is_refused(self):
        issued = self.ops.request('format_disk')
        self.assertFalse(issued['ok'])
        self.assertEqual(issued['status'], 'unknown-action')
        self.assertEqual(issued['confirm'], '')
        self.assertIsNone(self.ops.pending(), '不认识的动作名不能生成码')

    def test_no_executor_means_nothing_happens(self):
        self.ops.request('open_url', 'https://example.test')
        result = self.owner('424242')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'no-executor')
        self.assertIn('没有被改动', result['text'])

    def test_code_without_pending_is_plain_chat(self):
        # 没有待确认项时的 6 位数字就是普通聊天，不能被指令面吃掉。
        self.assertIsNone(self.ops.handle({'kind': 'private', 'target': 'owner-1',
                                           'sender_id': 'owner-1', 'text': '424242'}))

    # ── 审计 ───────────────────────────────────────────────────────────
    def test_audit_file_lives_under_data_dir(self):
        path = self.ops.audit_path()
        self.assertEqual(path.name, AUDIT_FILE_NAME)
        self.assertEqual(path.parent, self.data / 'qq-only')
        self.owner('记事本')
        self.assertTrue(path.exists())

    def test_audit_rows_carry_required_fields(self):
        self.owner('记到记事本：审计用')
        self.owner('读文件 ../oops.txt')
        self.ops.request('screenshot')
        self.owner('424242')
        rows = self.audits()
        self.assertGreaterEqual(len(rows), 4)
        for row in rows:
            for key in ('ts', 'kind', 'principal', 'ok', 'code'):
                self.assertIn(key, row, row)
            if row['kind'] != 'code-issued':      # 生成码的是程序侧，principal 是发起者
                self.assertEqual(row['principal'][:3], 'qq:', row)
        kinds = {row['kind'] for row in rows}
        self.assertTrue({'command', 'code-issued', 'code-used'} <= kinds, kinds)
        denied = [row for row in rows if row['kind'] == 'command' and not row['ok']]
        self.assertTrue(denied, '越界读取要能查')
        self.assertEqual(denied[0]['code'], 'denied')

    def test_workspace_writes_are_audited_by_agent_workspace(self):
        self.owner('记到记事本：写审计')
        rows = self.workspace_audits()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]['op'], 'write')
        self.assertEqual(rows[0]['agent'], 'qq')
        self.assertEqual(rows[0]['path'], NOTE_NAME)
        self.assertEqual(rows[0]['principal'], 'qq:owner-1')


if __name__ == '__main__':
    unittest.main()
