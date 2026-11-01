"""导入控制台即可用的真进程验收：把 tools/e2e_extension_probe.py 的断言固化成用例。

探针本身真起 node 子进程、真解压市场 zip、真读账本。本用例只做封装：跑一次探针，
断言硬性 7 项通过、报告落盘、反证里 call_tool 返回 disabled。前置（node / 0.4 运行副本 /
市场清单）缺失时 skipTest 并打印原因，绝不伪装通过。
"""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / 'tools' / 'e2e_extension_probe.py'
RUNTIME = ROOT / '.tmp' / 'qq-agent-run'
INVENTORY = ROOT / '.tmp' / 'inv' / 'plugin-tools.json'
REPORT = ROOT / '.tmp' / 'inv' / 'e2e-live-report.md'
TIMEOUT_S = 900


def missing_prereqs():
    """返回前置缺失原因列表；空列表表示可以真跑。"""
    reasons = []
    if not PROBE.is_file():
        reasons.append('缺探针 %s' % PROBE)
    if not (RUNTIME / 'src').is_dir():
        reasons.append('缺 0.4 运行副本 %s' % RUNTIME)
    if not INVENTORY.is_file():
        reasons.append('缺市场清单 %s' % INVENTORY)
    if reasons:
        return reasons
    try:
        done = subprocess.run(['node', '--version'], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as error:
        return ['node 不可用：%s' % error]
    if done.returncode != 0:
        return ['node --version 退出码 %s' % done.returncode]
    return []


class ExtensionLiveE2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skip_reason = '；'.join(missing_prereqs())
        cls.code = None
        cls.output = ''
        cls.report = ''
        if cls.skip_reason:
            print('\n[skip] 前置缺失：%s' % cls.skip_reason)
            return
        env = dict(os.environ)
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUTF8'] = '1'
        done = subprocess.run([sys.executable, str(PROBE)], cwd=str(ROOT), env=env,
                              capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=TIMEOUT_S)
        cls.code = done.returncode
        cls.output = (done.stdout or '') + (done.stderr or '')
        if REPORT.is_file():
            cls.report = REPORT.read_text(encoding='utf-8', errors='replace')

    def setUp(self):
        if self.skip_reason:
            self.skipTest(self.skip_reason)

    def tail(self, lines=40):
        return '\n'.join(self.output.strip().splitlines()[-lines:])

    def test_1_probe_exit_zero_with_all_hard_checks(self):
        self.assertEqual(self.code, 0, '探针退出码非 0：\n%s' % self.tail())
        self.assertIn('硬性断言 7/7', self.output, '硬性断言没全过：\n%s' % self.tail())

    def test_2_hook_really_spawned_a_process(self):
        """必须有真实 pid 与 duration_ms，不能只有 ok=True。"""
        pids = re.findall(r'pid=(\d+)', self.output)
        self.assertTrue(pids, '没有真实 pid：\n%s' % self.tail())
        durations = [int(x) for x in re.findall(r'duration_ms=(\d+)', self.output)]
        self.assertTrue(durations, '没有真实 duration_ms：\n%s' % self.tail())
        self.assertTrue(any(value > 0 for value in durations), 'duration_ms 全为 0')

    def test_3_tool_face_uses_full_name_and_object_schema(self):
        match = re.search(r'list_tools\(\) 返回 \d+ 个：(\S+)', self.output)
        self.assertIsNotNone(match, '没打印 list_tools() 结果：\n%s' % self.tail())
        full_name = match.group(1)
        self.assertRegex(full_name, r'^[A-Za-z0-9._-]+__[A-Za-z0-9._-]+$')
        self.assertIn('call_tool(%s)' % full_name, self.output)
        self.assertIn('| True | True |', self.report,
                      '报告里没有 parameters 合法 JSON Schema 的工具行')

    def test_4_disabled_extension_does_not_spawn(self):
        """反证：停用后 call_tool 必须 disabled 且不起进程。"""
        self.assertIn('kind=disabled', self.output, '停用后 call_tool 没返回 disabled：\n%s' % self.tail())
        self.assertRegex(self.output, r'call_tool\(\)[^\n]*verdict=clean')
        self.assertRegex(self.output, r'list_tools\(\) [^\n]*verdict=clean')

    def test_5_report_records_real_numbers(self):
        self.assertTrue(self.report, '没写出报告 %s' % REPORT)
        self.assertIn('真实端到端探针报告', self.report)
        for section in ('## 1 选候选', '## 2 宿主根制备', '## 3 装 + 启', '## 4 真跑钩子',
                        '## 5 工具面', '## 6 反证', '## 7 分层判定', '## 8 src 侧缺陷'):
            self.assertIn(section, self.report)
        pids = set(re.findall(r'pid=(\d+)', self.output))
        self.assertTrue(any(re.search(r'"pid": %s\b' % pid, self.report) for pid in pids),
                        '报告里没记下 stdout 里那个真实 pid')


if __name__ == '__main__':
    unittest.main()
