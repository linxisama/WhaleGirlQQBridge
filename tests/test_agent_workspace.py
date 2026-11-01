
"""agent_workspace 的边界与令牌测试（全部落在 tempfile，绝不碰仓库 data/）。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import agent_workspace as aw

REPO = Path(__file__).resolve().parents[1]


class WorkspaceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix='aw-test-'))
        self.outside = self.base / 'outside'
        self.outside.mkdir()
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for junk in self.base.rglob('*'):
            if junk.is_symlink():
                try:
                    os.unlink(str(junk))
                except OSError:
                    pass
        shutil.rmtree(str(self.base), ignore_errors=True)

    def ws(self, agent: str = 'agent1') -> Path:
        return aw.root(self.base, agent)

    def audit_lines(self, path: Path) -> list:
        if not path.exists():
            return []
        raw = path.read_text(encoding='utf-8')
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def all_audit(self) -> list:
        audit = aw.audit_path(self.base)
        return self.audit_lines(audit.with_name(aw.AUDIT_PREVIOUS_NAME)) + self.audit_lines(audit)

    def grant(self, relpath, content: str = 'hi', op: str = 'write', ttl: float = 120.0,
              principal: str = 'owner') -> str:
        result = aw.grant_write(self.base, 'agent1', relpath, principal=principal, ttl=ttl,
                                content=content, op=op)
        self.assertTrue(result['ok'], result)
        return result['param_hash']


class LayoutTest(WorkspaceTestBase):
    def test_root_is_pure_path(self) -> None:
        root = self.ws()
        self.assertEqual(root, self.base / 'agent_workspace' / 'agent1')
        self.assertFalse((self.base / 'agent_workspace').exists())

    def test_agent_id_whitelist(self) -> None:
        self.assertEqual(aw.root(self.base, 'Agent_01-x'), self.base / 'agent_workspace' / 'Agent_01-x')
        for bad in ('', '../escape', 'a' * 65, 'bad/id', 'bad id', '中文'):
            with self.assertRaises(aw.WorkspaceError) as ctx:
                aw.root(self.base, bad)
            self.assertEqual(ctx.exception.code, 'invalid')
            self.assertEqual(aw.write_text(self.base, bad, 'notes.md', 'x')['error']['code'],
                             'invalid')

    def test_import_creates_nothing(self) -> None:
        code = ('import os\n'
                "before = set(os.listdir('.'))\n"
                'import src.agent_workspace as mod\n'
                "print(sorted(set(os.listdir('.')) - before))\n"
                "print(mod.MAX_FILE_BYTES)\n")
        env = dict(os.environ)
        env['PYTHONIOENCODING'] = 'utf-8'
        done = subprocess.run([sys.executable, '-c', code], cwd=str(REPO), env=env,
                              capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, '')
        self.assertEqual(done.stdout.splitlines()[0], '[]')

    def test_pure_reads_do_not_create_dirs(self) -> None:
        self.ws()
        aw.stats(self.base, 'agent1')
        aw.audit_path(self.base)
        self.assertEqual(aw.audit_tail(self.base), [])
        self.assertEqual(aw.read_text(self.base, 'agent1', 'notes.md')['error']['code'], 'not_found')
        self.assertEqual(aw.list_dir(self.base, 'agent1')['error']['code'], 'not_found')
        self.assertFalse((self.base / 'agent_workspace').exists())
        self.assertFalse((self.base / 'qq-only').exists())


class ReadWriteTest(WorkspaceTestBase):
    def test_write_then_read_roundtrip(self) -> None:
        payload = '# 人设\n我是鲸鱼娘。\n'
        written = aw.write_text(self.base, 'agent1', 'notes.md', payload)
        self.assertTrue(written['ok'], written)
        self.assertEqual(written['bytes'], len(payload.encode('utf-8')))
        self.assertEqual(written['sha256'], written['sha256'].lower())
        self.assertEqual(Path(written['path']).read_text(encoding='utf-8'), payload)
        got = aw.read_text(self.base, 'agent1', 'notes.md')
        self.assertTrue(got['ok'])
        self.assertEqual(got['text'], payload)
        self.assertEqual(got['sha256'], written['sha256'])

    def test_nested_paths_mkdir_on_demand(self) -> None:
        result = aw.write_text(self.base, 'agent1', 'notes/2026/round1.md', 'a')
        self.assertTrue(result['ok'], result)
        self.assertTrue((self.ws() / 'notes' / '2026' / 'round1.md').is_file())
        self.assertEqual(aw.read_text(self.base, 'agent1', 'notes/2026/round1.md')['text'], 'a')

    def test_append_accumulates(self) -> None:
        aw.append_text(self.base, 'agent1', 'transcripts/session.md', '第一行\n')
        aw.append_text(self.base, 'agent1', 'transcripts/session.md', '第二行\n')
        self.assertEqual(aw.read_text(self.base, 'agent1', 'transcripts/session.md')['text'],
                         '第一行\n第二行\n')
        info = aw.stats(self.base, 'agent1')
        self.assertTrue(info['ok'])
        self.assertEqual((info['files'], info['dirs']), (1, 1))
        self.assertEqual(info['areas']['transcripts'],
                         {'files': 1, 'bytes': 20, 'exists': True})

    def test_stats_reports_areas(self) -> None:
        aw.write_text(self.base, 'agent1', 'notes.md', 'abc')
        aw.write_text(self.base, 'agent1', 'drafts/d.md', 'de')
        info = aw.stats(self.base, 'agent1')
        self.assertEqual(info['bytes'], 5)
        self.assertEqual(info['areas']['notes.md'], {'files': 1, 'bytes': 3, 'exists': True})
        self.assertEqual(info['areas']['drafts'], {'files': 1, 'bytes': 2, 'exists': True})
        self.assertFalse(info['areas']['persona']['exists'])
        self.assertEqual(info['limits']['max_file_bytes'], 1024 * 1024)

    def test_list_dir_marks_kinds(self) -> None:
        aw.write_text(self.base, 'agent1', 'notes.md', 'x')
        (self.ws() / 'persona').mkdir()
        listing = aw.list_dir(self.base, 'agent1')
        self.assertTrue(listing['ok'], listing)
        kinds = {entry['name']: entry['type'] for entry in listing['entries']}
        self.assertEqual(kinds, {'notes.md': 'file', 'persona': 'dir'})

    def test_resolve_stays_inside(self) -> None:
        self.assertEqual(aw.resolve(self.base, 'agent1', 'notes/x.md'),
                         self.ws() / 'notes' / 'x.md')
        self.assertEqual(aw.resolve(self.base, 'agent1', ''),
                         self.ws())
        for bad in ('../x.md', str(self.outside / 'x.md'), 'C:rel.md', '//srv/share/x.md'):
            with self.assertRaises(aw.WorkspaceError) as ctx:
                aw.resolve(self.base, 'agent1', bad)
            self.assertEqual(ctx.exception.code, 'denied')

    def test_write_over_directory_rejected(self) -> None:
        (self.ws() / 'notes').mkdir(parents=True)
        out = aw.write_text(self.base, 'agent1', 'notes', 'x')
        self.assertEqual(out['error']['code'], 'invalid')



class EscapeTest(WorkspaceTestBase):
    def test_dotdot_traversal_rejected(self) -> None:
        out = aw.write_text(self.base, 'agent1', '../escape.md', 'x')
        self.assertFalse(out['ok'])
        self.assertEqual(out['error']['code'], 'denied')
        self.assertIn('..', out['error']['message'])
        self.assertFalse((self.base / 'escape.md').exists())
        self.assertFalse((self.base / 'agent_workspace' / 'escape.md').exists())
        self.assertEqual(aw.write_text(self.base, 'agent1', 'notes/../../x.md', 'x')['error']['code'],
                         'denied')

    def test_absolute_path_denied_without_grant(self) -> None:
        target = self.outside / 'abs.md'
        out = aw.write_text(self.base, 'agent1', str(target), 'x')
        self.assertEqual(out['error']['code'], 'denied')
        self.assertFalse(target.exists())
        self.assertEqual(aw.read_text(self.base, 'agent1', str(target))['error']['code'], 'denied')

    def test_drive_relative_rejected(self) -> None:
        out = aw.write_text(self.base, 'agent1', 'C:rel.md', 'x')
        self.assertEqual(out['error']['code'], 'denied')
        self.assertIn('盘符', out['error']['message'])

    def test_unc_paths_rejected(self) -> None:
        for unc in ('//srv/share/x.md', '\\\\srv\\share\\x.md'):
            out = aw.write_text(self.base, 'agent1', unc, 'x')
            self.assertEqual(out['error']['code'], 'denied', unc)
            self.assertIn('UNC', out['error']['message'])

    def test_symlink_denied(self) -> None:
        ws = self.ws()
        (ws / 'drafts').mkdir(parents=True)
        evil = self.outside
        link = ws / 'drafts' / 'sneaky'
        real = False
        try:
            os.symlink(str(evil), str(link), target_is_directory=True)
            real = True
        except (OSError, NotImplementedError):
            (link / 'sub').mkdir(parents=True)
        try:
            if real:
                out = aw.write_text(self.base, 'agent1', 'drafts/sneaky/sub/x.md', 'x')
            else:
                with mock.patch.object(aw, '_is_reparse',
                                       side_effect=lambda p: Path(p).name == 'sneaky'):
                    out = aw.write_text(self.base, 'agent1', 'drafts/sneaky/sub/x.md', 'x')
            self.assertFalse(out['ok'], out)
            self.assertEqual(out['error']['code'], 'denied')
            self.assertIn('符号链接', out['error']['message'])
            self.assertFalse((evil / 'sub').exists())      # 连目录都没在外面建
            self.assertFalse((evil / 'sub' / 'x.md').exists())
            if real:
                listing = aw.list_dir(self.base, 'agent1', 'drafts')
                self.assertEqual({e['name']: e['type'] for e in listing['entries']},
                                 {'sneaky': 'link'})
        finally:
            if real:
                try:
                    os.unlink(str(link))
                except OSError:
                    pass

    def test_junction_denied(self) -> None:
        ws = self.ws()
        (ws / 'drafts').mkdir(parents=True)
        evil = self.outside / 'evil'
        evil.mkdir()
        link = ws / 'drafts' / 'junc'
        done = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(evil)],
                              capture_output=True, text=True)
        real = done.returncode == 0
        if not real:
            (link / 'sub').mkdir(parents=True)
        try:
            if real:
                out = aw.write_text(self.base, 'agent1', 'drafts/junc/sub/x.md', 'x')
                read = aw.read_text(self.base, 'agent1', 'drafts/junc/x.md')
            else:
                with mock.patch.object(aw, '_is_reparse',
                                       side_effect=lambda p: Path(p).name == 'junc'):
                    out = aw.write_text(self.base, 'agent1', 'drafts/junc/sub/x.md', 'x')
                    read = aw.read_text(self.base, 'agent1', 'drafts/junc/x.md')
            self.assertFalse(out['ok'], out)
            self.assertEqual(out['error']['code'], 'denied')
            self.assertFalse(read['ok'])
            self.assertEqual(read['error']['code'], 'denied')
            self.assertFalse((evil / 'sub').exists())
            self.assertFalse((evil / 'x.md').exists())
        finally:
            if real:
                try:
                    os.rmdir(str(link))
                except OSError:
                    pass

    def test_single_file_size_limit(self) -> None:
        huge = 'a' * (aw.MAX_FILE_BYTES + 1)
        out = aw.write_text(self.base, 'agent1', 'notes.md', huge)
        self.assertEqual(out['error']['code'], 'too_large')
        self.assertFalse((self.ws() / 'notes.md').exists())

    def test_append_size_limit(self) -> None:
        ok = aw.write_text(self.base, 'agent1', 'notes.md', 'a' * aw.MAX_FILE_BYTES)
        self.assertTrue(ok['ok'])
        out = aw.append_text(self.base, 'agent1', 'notes.md', 'b')
        self.assertEqual(out['error']['code'], 'too_large')

    def test_oversized_file_read_rejected(self) -> None:
        (self.ws() / 'notes').mkdir(parents=True)
        (self.ws() / 'notes' / 'big.md').write_bytes(b'a' * (aw.MAX_FILE_BYTES + 1))
        out = aw.read_text(self.base, 'agent1', 'notes/big.md')
        self.assertEqual(out['error']['code'], 'too_large')

    def test_non_utf8_read_is_structured(self) -> None:
        (self.ws() / 'notes').mkdir(parents=True)
        (self.ws() / 'notes' / 'bin.md').write_bytes(b'\xff\xfe\x00not utf8')
        out = aw.read_text(self.base, 'agent1', 'notes/bin.md')
        self.assertFalse(out['ok'])
        self.assertEqual(out['error']['code'], 'encoding')
        self.assertEqual(aw.write_text(self.base, 'agent1', 'notes/bin2.md', b'\xff')['error']['code'],
                         'invalid')

    def test_control_characters_rejected(self) -> None:
        out = aw.write_text(self.base, 'agent1', 'notes/\x01x.md', 'x')
        self.assertEqual(out['error']['code'], 'denied')



class GrantTest(WorkspaceTestBase):
    def test_grant_shape(self) -> None:
        result = aw.grant_write(self.base, 'agent1', str(self.outside / 'a.md'),
                                principal='owner', ttl=60, content='hi')
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(result['param_hash']), 32)
        int(result['param_hash'], 16)                       # 十六进制
        self.assertFalse(result['inside'])
        self.assertGreater(result['expires_at'], 0)
        self.assertEqual(result['principal'], 'owner')
        self.assertFalse((self.outside / 'a.md').exists())   # 发令牌不写文件

    def test_grant_enables_one_outside_write(self) -> None:
        target = self.outside / 'export.md'
        token = self.grant(str(target), content='hello')
        out = aw.write_text(self.base, 'agent1', str(target), 'hello', grant=token,
                            principal='owner')
        self.assertTrue(out['ok'], out)
        self.assertFalse(out['inside'])
        self.assertEqual(target.read_text(encoding='utf-8'), 'hello')
        self.assertTrue(out['audited'])

    def test_grant_expired(self) -> None:
        target = self.outside / 'late.md'
        token = self.grant(str(target), content='x', ttl=-1)
        out = aw.write_text(self.base, 'agent1', str(target), 'x', grant=token, principal='owner')
        self.assertEqual(out['error']['code'], 'expired')
        self.assertFalse(target.exists())

    def test_grant_replay_rejected(self) -> None:
        target = self.outside / 'once.md'
        token = self.grant(str(target), content='x')
        first = aw.write_text(self.base, 'agent1', str(target), 'x', grant=token, principal='owner')
        self.assertTrue(first['ok'], first)
        second = aw.write_text(self.base, 'agent1', str(target), 'x', grant=token, principal='owner')
        self.assertEqual(second['error']['code'], 'replayed')
        self.assertEqual(target.read_text(encoding='utf-8'), 'x')

    def test_grant_tied_to_content(self) -> None:
        target = self.outside / 'content.md'
        token = self.grant(str(target), content='AAA')
        changed = aw.write_text(self.base, 'agent1', str(target), 'BBB', grant=token,
                                principal='owner')
        self.assertEqual(changed['error']['code'], 'denied')
        self.assertIn('参数不匹配', changed['error']['message'])
        self.assertFalse(target.exists())
        still = aw.write_text(self.base, 'agent1', str(target), 'AAA', grant=token,
                              principal='owner')
        self.assertTrue(still['ok'], still)                 # 参数不符不消耗令牌

    def test_grant_tied_to_path(self) -> None:
        token = self.grant(str(self.outside / 'a.md'), content='AAA')
        out = aw.write_text(self.base, 'agent1', str(self.outside / 'b.md'), 'AAA',
                            grant=token, principal='owner')
        self.assertEqual(out['error']['code'], 'denied')
        self.assertFalse((self.outside / 'b.md').exists())

    def test_grant_tied_to_op(self) -> None:
        target = self.outside / 'op.md'
        token = self.grant(str(target), content='AAA', op='write')
        out = aw.append_text(self.base, 'agent1', str(target), 'AAA', grant=token,
                             principal='owner')
        self.assertEqual(out['error']['code'], 'denied')
        self.assertFalse(target.exists())
        out2 = aw.write_text(self.base, 'agent1', str(target), 'AAA', grant=token,
                             principal='owner')
        self.assertTrue(out2['ok'], out2)

    def test_grant_tied_to_principal(self) -> None:
        target = self.outside / 'who.md'
        token = self.grant(str(target), content='x', principal='owner')
        bad = aw.write_text(self.base, 'agent1', str(target), 'x', grant=token,
                            principal='intruder')
        self.assertEqual(bad['error']['code'], 'unauthorized')
        self.assertFalse(target.exists())
        good = aw.write_text(self.base, 'agent1', str(target), 'x', grant=token,
                             principal='owner')
        self.assertTrue(good['ok'], good)

    def test_grant_rejects_unknown_op(self) -> None:
        out = aw.grant_write(self.base, 'agent1', 'notes.md', principal='owner', op='chmod')
        self.assertEqual(out['error']['code'], 'invalid')

    def test_grant_only_needed_outside(self) -> None:
        inside = aw.write_text(self.base, 'agent1', 'notes.md', 'x')
        self.assertTrue(inside['ok'])
        self.assertTrue(inside['inside'])
        self.assertEqual(inside['param_hash'], '')
        self.assertEqual(aw.write_text(self.base, 'agent1', str(self.ws() / 'notes.md'), 'x')['ok'],
                         True)                                   # 工作区绝对路径算界内

    def test_garbage_token_rejected(self) -> None:
        out = aw.write_text(self.base, 'agent1', str(self.outside / 'g.md'), 'x',
                            grant='deadbeef' * 4, principal='owner')
        self.assertEqual(out['error']['code'], 'unauthorized')
        self.assertFalse((self.outside / 'g.md').exists())



class AuditTest(WorkspaceTestBase):
    FIELDS = {'ts', 'agent', 'op', 'path', 'ok', 'code', 'principal', 'param_hash'}

    def test_audit_path_layout(self) -> None:
        self.assertEqual(aw.audit_path(self.base),
                         self.base / 'qq-only' / 'agent_workspace_audit.jsonl')
        self.assertFalse((self.base / 'qq-only').exists())

    def test_audit_line_has_all_fields(self) -> None:
        aw.write_text(self.base, 'agent1', 'notes.md', 'x')
        lines = aw.audit_tail(self.base)
        self.assertEqual(len(lines), 1)
        self.assertEqual(set(lines[0]), self.FIELDS)
        self.assertEqual(lines[0]['agent'], 'agent1')
        self.assertEqual(lines[0]['op'], 'write')
        self.assertEqual(lines[0]['path'], 'notes.md')
        self.assertTrue(lines[0]['ok'])
        self.assertEqual(lines[0]['code'], '')

    def test_audit_records_denied_writes(self) -> None:
        aw.write_text(self.base, 'agent1', '../escape.md', 'x')
        aw.write_text(self.base, 'agent1', 'C:rel.md', 'x')
        aw.write_text(self.base, 'agent1', str(self.outside / 'abs.md'), 'x')
        lines = aw.audit_tail(self.base)
        self.assertEqual(len(lines), 3)
        for entry in lines:
            self.assertFalse(entry['ok'])
            self.assertEqual(entry['code'], 'denied')
            self.assertEqual(entry['agent'], 'agent1')

    def test_audit_keeps_no_body_and_no_secret(self) -> None:
        aw.write_text(self.base, 'agent1', 'notes.md', 'SECRET-BODY-文字')
        raw = aw.audit_path(self.base).read_text(encoding='utf-8')
        self.assertNotIn('SECRET-BODY', raw)
        self.assertNotIn('文字', raw)
        self.assertEqual(len(raw.splitlines()), 1)

    def test_audit_tail_limit_and_order(self) -> None:
        for index in range(5):
            aw.write_text(self.base, 'agent1', 'notes.md', str(index))
        tail = aw.audit_tail(self.base, 2)
        self.assertEqual(len(tail), 2)
        stamps = [entry['ts'] for entry in tail]
        self.assertEqual(stamps, sorted(stamps))

    def test_audit_rotation_keeps_lines(self) -> None:
        first = aw.write_text(self.base, 'agent1', 'notes.md', 'x')
        self.assertTrue(first['audited'])
        audit = aw.audit_path(self.base)
        prev = audit.with_name(aw.AUDIT_PREVIOUS_NAME)
        size = audit.stat().st_size
        stamps = [entry['ts'] for entry in self.audit_lines(audit)]
        with mock.patch.object(aw, 'AGENT_WORKSPACE_AUDIT_MAX_BYTES', size + 1):
            for _ in range(6):
                result = aw.write_text(self.base, 'agent1', 'notes.md', 'x')
                self.assertTrue(result['audited'], result)
                stamps.append(self.audit_lines(audit)[-1]['ts'])
                combined = [entry['ts'] for entry in self.all_audit()]
                self.assertGreaterEqual(len(combined), 2)
                self.assertEqual(combined, stamps[-len(combined):])   # 连续后缀，不丢行不乱序
        self.assertTrue(prev.exists(), '应当发生过轮转')
        self.assertLess(len(self.all_audit()), len(stamps))            # 只保留最近两代


if __name__ == '__main__':
    unittest.main()

