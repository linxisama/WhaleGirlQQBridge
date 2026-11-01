"""控制台笔记一律走 agent_workspace：真数据根、真文件、真撤销链、真审计。

这里的用例不再手工拼 job dict / 直接调 _apply_one：全部从 submit 进链路
（plan -> validate -> apply -> undo），只有模型那一次 HTTP 调用被脚本化。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import agent_workspace
from src.console_agent import NOTE_AGENT, ConsoleAgent, note_data_dir
from src.qq_app import QQRuntime
from tests.test_persona_files import build_root

NOTE = 'note.md'


class FakeEngine:
    """只替掉模型那次调用：返回预设的操作 JSON，其余链路都是真的。"""
    output = ''

    def __init__(self, *args, **kwargs):
        self.args = args

    async def chat(self, messages, **kwargs):
        return type(self).output

    async def close(self):
        pass


class NoteWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = build_root(self.temp.name)
        self.bot = QQRuntime(root=self.root, db_path=self.root / 'notes.sqlite')
        self.bot.ai.api_url = 'https://model.example.test/v1/chat/completions'
        self.bot.ai.api_key = 'private-fixture-key'
        self.bot.ai.model = 'running-model'
        self.service = ConsoleAgent(self.bot)
        self.data = Path(note_data_dir(self.bot))
        self.workspace = self.data / 'agent_workspace' / NOTE_AGENT
        self.outside = self.root / 'outside'
        self.outside.mkdir()
        FakeEngine.output = ''
        self.engine = patch('src.console_agent.AIEngine', FakeEngine)
        self.engine.start()

    async def asyncTearDown(self):
        self.engine.stop()
        await self.service.close()
        await self.bot.close()
        self.temp.cleanup()

    async def submit(self, action, prompt='把这段整理成笔记'):
        """真链路入口：submit -> plan -> validate -> apply，返回真实 job。"""
        FakeEngine.output = json.dumps({'action': action}, ensure_ascii=False)
        started = self.service.submit(prompt=prompt)
        job = self.service.get(started['id'])
        await job['_task']
        return job, self.service.view(job)

    async def undo(self, job):
        self.service.undo(job['id'])
        await job['_task']
        return job, self.service.view(job)

    def audit_rows(self):
        path = agent_workspace.audit_path(str(self.data))
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def named(self, root, name):
        return sorted(str(item) for item in Path(root).rglob(name))

    def note_text(self, name=NOTE):
        return (self.workspace / name).read_text(encoding='utf-8')

    async def test_write_note_goes_through_agent_workspace(self):
        job, view = await self.submit({'type': 'write_note', 'name': NOTE, 'text': '# 清单\n牛奶'})
        self.assertEqual(view['state'], 'applied', view['message'])
        result = view['result']['steps'][0]
        path = Path(result['path'])
        self.assertTrue(path.is_absolute(), result)
        self.assertEqual(path, self.workspace / NOTE)
        self.assertEqual(path.read_text(encoding='utf-8'), '# 清单\n牛奶')
        self.assertEqual(view['message'], '已写入工作区笔记：' + result['path'])
        self.assertTrue(result['audited'])
        self.assertTrue(view['can_undo'])

    async def test_validate_strips_trailing_newline(self):
        # 真链路里 validate 会 value.strip()，尾随换行到不了磁盘；旧用例绕过 validate 所以没抓到。
        job, view = await self.submit({'type': 'write_note', 'name': NOTE, 'text': '正文\n'})
        self.assertEqual(view['state'], 'applied', view['message'])
        self.assertEqual(self.note_text(), '正文')
        job, undone = await self.undo(job)
        self.assertEqual(undone['state'], 'undone', undone['message'])
        self.assertFalse((self.workspace / NOTE).exists())

    async def test_workspace_is_created_on_demand(self):
        self.assertFalse((self.data / 'agent_workspace').exists())
        job, view = await self.submit({'type': 'write_note', 'name': NOTE, 'text': '第一次'})
        self.assertEqual(view['state'], 'applied', view['message'])
        self.assertTrue((self.workspace / NOTE).is_file())

    async def test_append_note_keeps_earlier_text(self):
        await self.submit({'type': 'write_note', 'name': NOTE, 'text': 'a'}, prompt='先写一行')
        job, view = await self.submit({'type': 'write_note', 'name': NOTE, 'text': 'b', 'append': True},
                                      prompt='再接一行')
        self.assertEqual(view['state'], 'applied', view['message'])
        self.assertEqual(self.note_text(), 'ab')
        self.assertEqual(view['message'], '已追加工作区笔记：' + view['result']['steps'][0]['path'])

    async def test_read_note_returns_absolute_path(self):
        await self.submit({'type': 'write_note', 'name': NOTE, 'text': '内容 42'}, prompt='记一条')
        job, view = await self.submit({'type': 'read_note', 'name': NOTE}, prompt='读回来')
        self.assertEqual(view['state'], 'applied', view['message'])
        result = view['result']['steps'][0]
        self.assertEqual(Path(result['path']), self.workspace / NOTE)
        self.assertEqual(result['text'], '内容 42')
        self.assertIn('内容 42', view['message'])
        self.assertFalse(view['can_undo'])

    async def test_read_missing_note_is_friendly(self):
        job, view = await self.submit({'type': 'read_note', 'name': 'nope.md'})
        self.assertEqual(view['state'], 'applied', view['message'])
        self.assertEqual(view['message'], '工作区里没有这个笔记')
        self.assertEqual(view['result']['steps'][0]['refused'], 'not_found')
        self.assertFalse(view['can_undo'])
        self.assertFalse((self.workspace / 'nope.md').exists())

    async def test_validate_blocks_escaping_names_before_disk(self):
        for index, bad in enumerate(('../escape.md', 'sub/x.md', 'C:/x.md', '..\\y.md')):
            job, view = await self.submit({'type': 'write_note', 'name': bad, 'text': 'x'},
                                          prompt='越界笔记 %d' % index)
            self.assertEqual(view['state'], 'failed', view)
            self.assertIn('笔记名', view['message'])
            self.assertEqual(self.named(self.data, 'escape.md'), [])
            self.assertEqual(self.named(self.root, 'escape.md'), [])
            self.assertFalse((self.outside / 'y.md').exists())

    async def test_symlink_escape_refused_without_touching_disk(self):
        target = self.outside / 'abs.md'
        target.write_text('外人内容', encoding='utf-8')
        self.workspace.mkdir(parents=True, exist_ok=True)
        link = self.workspace / 'leak.md'
        try:
            os.symlink(str(target), str(link))
        except (OSError, NotImplementedError) as exc:
            self.skipTest('这台机器建不了文件符号链接：' + str(exc))
        self.addCleanup(lambda: link.unlink() if link.is_symlink() else None)
        job, view = await self.submit({'type': 'write_note', 'name': 'leak.md', 'text': 'x'})
        self.assertEqual(view['state'], 'applied', view['message'])
        self.assertIn('这个路径不在你的工作区里，我不能写', view['message'])
        self.assertIn('目标文件没有改动', view['message'])
        self.assertEqual(view['result']['steps'][0]['refused'], 'denied')
        self.assertFalse(view['can_undo'], view)
        self.assertEqual(target.read_text(encoding='utf-8'), '外人内容')

    async def test_undo_restores_previous_text(self):
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / NOTE).write_text('旧内容', encoding='utf-8')
        job, view = await self.submit({'type': 'write_note', 'name': NOTE, 'text': '新内容'})
        self.assertEqual(view['state'], 'applied', view['message'])
        self.assertEqual(self.note_text(), '新内容')
        self.assertEqual([item['type'] for item in job['_undo']], ['_note_restore'])
        self.assertTrue(view['can_undo'])
        done, undone = await self.undo(job)
        self.assertEqual(undone['state'], 'undone', undone['message'])
        self.assertEqual(self.note_text(), '旧内容')
        self.assertIn('已恢复笔记', undone['message'])
        self.assertFalse(undone['can_undo'])

    async def test_undo_removes_new_note(self):
        job, view = await self.submit({'type': 'write_note', 'name': NOTE, 'text': '新建'})
        self.assertEqual(view['state'], 'applied', view['message'])
        self.assertEqual([item['type'] for item in job['_undo']], ['_note_drop'])
        self.assertTrue((self.workspace / NOTE).is_file())
        done, undone = await self.undo(job)
        self.assertEqual(undone['state'], 'undone', undone['message'])
        self.assertFalse((self.workspace / NOTE).exists())
        self.assertIn('已删掉本次新建的笔记', undone['message'])

    async def test_undo_refused_after_another_job_changed_the_note(self):
        first, first_view = await self.submit({'type': 'write_note', 'name': NOTE, 'text': '第一版'},
                                              prompt='新建 race 笔记')
        self.assertEqual(first_view['state'], 'applied', first_view['message'])
        second, second_view = await self.submit({'type': 'write_note', 'name': NOTE, 'text': '第二版'},
                                                prompt='把 race 笔记改成第二版')
        self.assertEqual(second_view['state'], 'applied', second_view['message'])
        self.assertEqual(self.note_text(), '第二版')
        with self.assertRaises(ValueError) as caught:
            self.service.undo(first['id'])
        self.assertIn('又被改过', str(caught.exception))
        self.assertIn('放弃自动撤销', str(caught.exception))
        self.assertEqual(first['state'], 'applied')
        self.assertEqual(self.note_text(), '第二版')

    async def test_undo_after_note_deleted_says_so_without_traceback(self):
        job, view = await self.submit({'type': 'write_note', 'name': NOTE, 'text': '会被删掉'})
        self.assertEqual(view['state'], 'applied', view['message'])
        (self.workspace / NOTE).unlink()
        with self.assertRaises(ValueError) as caught:
            self.service.undo(job['id'])
        self.assertIn('已经不在了', str(caught.exception))
        self.assertFalse((self.workspace / NOTE).exists())

    async def test_note_drop_step_refuses_when_note_changed_underneath(self):
        job, view = await self.submit({'type': 'write_note', 'name': NOTE, 'text': '第一版'})
        self.assertEqual(view['state'], 'applied', view['message'])
        entry = job['_undo'][0]
        self.assertEqual(entry['type'], '_note_drop')
        self.assertTrue(entry.get('digest'))
        (self.workspace / NOTE).write_text('别人写的', encoding='utf-8')
        # 陈旧性校验挡住整次撤销；这里单独验证 _note_drop 自己的兜底判断。
        with self.assertRaises(ValueError) as caught:
            await self.service._undo_one(entry)
        self.assertIn('又被改过', str(caught.exception))
        self.assertEqual(self.note_text(), '别人写的')

    async def test_note_drop_step_is_friendly_when_file_is_gone(self):
        job, view = await self.submit({'type': 'write_note', 'name': NOTE, 'text': '第一版'})
        self.assertEqual(view['state'], 'applied', view['message'])
        (self.workspace / NOTE).unlink()
        with self.assertRaises(ValueError) as caught:
            await self.service._undo_one(job['_undo'][0])
        self.assertIn('已经不在了', str(caught.exception))

    async def test_note_restore_step_refuses_when_note_changed_underneath(self):
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / NOTE).write_text('旧内容', encoding='utf-8')
        job, view = await self.submit({'type': 'write_note', 'name': NOTE, 'text': '我写的内容'})
        self.assertEqual(view['state'], 'applied', view['message'])
        entry = job['_undo'][0]
        self.assertEqual(entry['type'], '_note_restore')
        (self.workspace / NOTE).write_text('别人改写的内容', encoding='utf-8')
        with self.assertRaises(ValueError) as caught:
            await self.service._undo_one(entry)
        self.assertIn('又被改过', str(caught.exception))
        self.assertEqual(self.note_text(), '别人改写的内容')
        row = self.audit_rows()[-1]
        self.assertEqual(row['principal'], 'console')

    async def test_undo_after_append_restores_original(self):
        await self.submit({'type': 'write_note', 'name': NOTE, 'text': 'a'}, prompt='先写一行')
        job, view = await self.submit({'type': 'write_note', 'name': NOTE, 'text': 'b', 'append': True},
                                      prompt='再接一行')
        self.assertEqual(self.note_text(), 'ab')
        job, undone = await self.undo(job)
        self.assertEqual(undone['state'], 'undone', undone['message'])
        self.assertEqual(self.note_text(), 'a')

    async def test_refused_write_leaves_no_undo_entry(self):
        self.workspace.mkdir(parents=True, exist_ok=True)
        big = self.workspace / NOTE
        big.write_bytes(b'a' * 1040000)
        job, view = await self.submit({'type': 'write_note', 'name': NOTE, 'text': 'b' * 20000,
                                       'append': True})
        self.assertEqual(view['state'], 'applied', view['message'])
        self.assertEqual(view['result']['steps'][0]['refused'], 'too_large')
        self.assertIn('目标文件没有改动', view['message'])
        self.assertFalse(view['can_undo'], view)
        self.assertEqual(job['_undo'], [])
        self.assertEqual(big.stat().st_size, 1040000)
        self.assertEqual(big.read_bytes(), b'a' * 1040000)
        with self.assertRaises(ValueError) as caught:
            self.service.undo(job['id'])
        self.assertIn('没有可撤销的改动', str(caught.exception))

    async def test_write_is_audited(self):
        job, view = await self.submit({'type': 'write_note', 'name': NOTE, 'text': 'audited'})
        self.assertEqual(view['state'], 'applied', view['message'])
        rows = self.audit_rows()
        self.assertTrue(rows, '审计文件没有落盘')
        last = rows[-1]
        self.assertEqual(last['agent'], NOTE_AGENT)
        self.assertEqual(last['op'], 'write')
        self.assertTrue(last['ok'], last)
        self.assertTrue(str(last['path']).endswith('note.md'), last)
        self.assertEqual(last['principal'], 'console')

    async def test_data_dir_follows_bot_data_root(self):
        self.assertEqual(self.data, self.root / 'data')
        account = self.root / 'accounts' / 'second'
        self.bot.data_root = account
        self.assertEqual(Path(note_data_dir(self.bot)), account)
        job, view = await self.submit({'type': 'write_note', 'name': NOTE, 'text': '第二个账号'})
        self.assertEqual(view['state'], 'applied', view['message'])
        path = Path(view['result']['steps'][0]['path'])
        self.assertEqual(path, account / 'agent_workspace' / NOTE_AGENT / NOTE)
        self.assertTrue(path.is_file())
        self.assertEqual(self.named(self.root / 'data', 'note.md'), [])


if __name__ == '__main__':
    unittest.main()
