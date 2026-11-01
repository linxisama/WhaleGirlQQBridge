"""W6b：越界写的审计闸 —— 审计写不进去就必须拒绝这次写（全部落在 tempfile）。"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import agent_workspace as aw


class AuditGateTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix='aw-audit-test-'))
        self.outside = self.base / 'outside'
        self.outside.mkdir()
        self.addCleanup(self._cleanup)
        # 审计闸的连续失败计数是模块级的，用例之间必须清零，否则互相污染。
        aw._AUDIT_GATE['fails'] = 0
        aw._AUDIT_GATE['muted'] = False

    def _cleanup(self) -> None:
        shutil.rmtree(str(self.base), ignore_errors=True)

    def grant(self, relpath, content: str = 'hi', op: str = 'write',
              principal: str = 'owner') -> str:
        result = aw.grant_write(self.base, 'agent1', relpath, principal=principal,
                                ttl=120.0, content=content, op=op)
        self.assertTrue(result['ok'], result)
        return result['param_hash']

    def audit_lines(self) -> list:
        path = aw.audit_path(self.base)
        if not path.exists():
            return []
        raw = path.read_text(encoding='utf-8')
        return [json.loads(line) for line in raw.splitlines() if line.strip()]


class OutOfBoundsAuditGateTest(AuditGateTestBase):
    def test_audit_exception_refuses_outside_write(self) -> None:
        """审计抛异常：越界写被拒、目标文件一个字节都不落。"""
        target = self.outside / 'export.md'
        token = self.grant(str(target), content='hello')
        with mock.patch.object(aw, '_audit', side_effect=OSError('disk on fire')):
            out = aw.write_text(self.base, 'agent1', str(target), 'hello', grant=token,
                                principal='owner')
        self.assertFalse(out['ok'], out)
        self.assertEqual(out['code'], 'audit_failed')
        self.assertEqual(out['error']['code'], 'audit_failed')
        self.assertFalse(target.exists())          # 界外文件没有被创建
        self.assertEqual(self.audit_lines(), [])   # 审计也确实没落盘

    def test_audit_returning_false_refuses_outside_write(self) -> None:
        """审计返回 False（目录不可写）：同样拒绝，不是"记不上也照写"。"""
        target = self.outside / 'export2.md'
        token = self.grant(str(target), content='x')
        with mock.patch.object(aw, '_audit', return_value=False):
            out = aw.write_text(self.base, 'agent1', str(target), 'x', grant=token,
                                principal='owner')
        self.assertFalse(out['ok'], out)
        self.assertEqual(out['code'], 'audit_failed')
        self.assertFalse(target.exists())

    def test_refused_write_keeps_token_usable(self) -> None:
        """拒绝发生在落盘与 consume 之前：审计修好后同一枚令牌仍然可用一次。"""
        target = self.outside / 'export3.md'
        token = self.grant(str(target), content='y')
        with mock.patch.object(aw, '_audit', side_effect=OSError('boom')):
            refused = aw.write_text(self.base, 'agent1', str(target), 'y', grant=token,
                                    principal='owner')
        self.assertEqual(refused['code'], 'audit_failed')
        good = aw.write_text(self.base, 'agent1', str(target), 'y', grant=token,
                             principal='owner')
        self.assertTrue(good['ok'], good)
        self.assertTrue(good['audited'])
        self.assertEqual(target.read_text(encoding='utf-8'), 'y')

    def test_inside_write_keeps_old_semantics_when_audit_fails(self) -> None:
        """界内写沿用老语义：审计失败只记 audited=False，文件照写。"""
        with mock.patch.object(aw, '_audit', side_effect=OSError('boom')):
            out = aw.write_text(self.base, 'agent1', 'notes.md', 'inside text')
        self.assertTrue(out['ok'], out)
        self.assertTrue(out['inside'])
        self.assertFalse(out['audited'])
        written = aw.root(self.base, 'agent1') / 'notes.md'
        self.assertEqual(written.read_text(encoding='utf-8'), 'inside text')
        self.assertEqual(self.audit_lines(), [])

    def test_outside_write_audits_before_writing_once(self) -> None:
        """正常越界路径不回归：写成功、audited=True、只记一行 ok=True。"""
        target = self.outside / 'export4.md'
        token = self.grant(str(target), content='z')
        out = aw.write_text(self.base, 'agent1', str(target), 'z', grant=token,
                            principal='owner')
        self.assertTrue(out['ok'], out)
        self.assertTrue(out['audited'])
        lines = [line for line in self.audit_lines() if line.get('op') == 'write']
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0]['ok'])
        self.assertEqual(lines[0]['code'], '')
        self.assertEqual(target.read_text(encoding='utf-8'), 'z')

    def test_without_grant_is_still_denied_not_audit_failed(self) -> None:
        """没有令牌的越界写仍是 denied（审计闸不改变既有拒绝码）。"""
        target = self.outside / 'export5.md'
        out = aw.write_text(self.base, 'agent1', str(target), 'x')
        self.assertFalse(out['ok'], out)
        self.assertEqual(out['error']['code'], 'denied')
        self.assertFalse(target.exists())
        self.assertNotIn('code', out)   # denied 走的还是老的 error 载体

    def test_audit_gate_logs_stderr_and_mutes_repeats(self) -> None:
        """连续失败：stderr 有据可查，但不许无限刷（超过阈值后静默）。"""
        stderr = io.StringIO()
        with mock.patch.object(aw, '_audit', side_effect=OSError('boom')):
            with contextlib.redirect_stderr(stderr):
                for index in range(6):
                    target = self.outside / f'repeat{index}.md'
                    token = self.grant(str(target), content='v')
                    out = aw.write_text(self.base, 'agent1', str(target), 'v',
                                        grant=token, principal='owner')
                    self.assertEqual(out['code'], 'audit_failed')
                    self.assertFalse(target.exists())
        text = stderr.getvalue()
        self.assertIn('越界写被拒', text)
        self.assertIn('审计连续失败', text)
        self.assertLessEqual(text.count('越界写被拒'), aw.AUDIT_GATE_MUTE_AFTER)
        self.assertGreaterEqual(aw._AUDIT_GATE['fails'], 6)


if __name__ == '__main__':
    unittest.main()
