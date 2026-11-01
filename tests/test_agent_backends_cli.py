
"""W5a 真机测试：子进程执行、超时被杀、输出截断、取消、孤进程检查、并发、dsh。

孤儿进程检查不依赖 psutil（本机未安装），一律走 tasklist /FI "PID eq <pid>" /NH /FO CSV。
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from src import agent_backends as ab

IS_WINDOWS = os.name == 'nt'


def pid_alive(pid: int) -> bool:
    """tasklist 精确匹配：进程存在返回 True。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    try:
        done = subprocess.run(['tasklist', '/FI', 'PID eq %d' % pid, '/NH', '/FO', 'CSV'],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return ('"%d"' % pid).encode('ascii') in done.stdout


def child_pids(parent_pid: int) -> list:
    """用 CIM 查直接子进程（判断进程树有没有被一起收掉）。"""
    script = ('Get-CimInstance Win32_Process -Filter "ParentProcessId=%d" | '
              'Select-Object -ExpandProperty ProcessId' % int(parent_pid))
    try:
        done = subprocess.run(['powershell', '-NoProfile', '-NonInteractive',
                               '-Command', script],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(token) for token in done.stdout.decode('ascii', 'replace').split()
            if token.strip().isdigit()]


def wait_until(predicate, timeout: float = 20.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:      # noqa: BLE001 - 轮询期间的瞬时异常不致命
            pass
        time.sleep(interval)
    return False


class CliTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix='ab-cli-'))
        self.data = self.base / 'data'
        self.addCleanup(lambda: shutil.rmtree(str(self.base), ignore_errors=True))

    def backend(self, backend_id='b1', profile=None, **extra):
        payload = {'command': sys.executable, 'timeout': 60}
        payload.update(profile or {})
        payload = {key: value for key, value in payload.items() if value is not None}
        backend = ab.CliBackend(backend_id, payload, data_dir=self.data, **extra)
        self.addCleanup(backend.stop)
        return backend

    @contextlib.contextmanager
    def running(self, backend, prompt='', *, timeout=60):
        """在后台线程跑一次 start()，等到真进 running 后交出 box['pid']。"""
        box: dict = {}

        def work():
            box['result'] = backend.start(prompt, timeout=timeout)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self.addCleanup(self._join, thread)

        def alive():
            status = backend.status()
            box['pid'] = status.get('pid')
            return bool(status.get('running') and status.get('pid'))

        self.assertTrue(wait_until(alive), '后端没有进入 running：%r' % backend.status())
        try:
            yield box
        finally:
            backend.stop()
            thread.join(30)
            self.assertFalse(thread.is_alive(), 'start() 线程没有退出')

    def _join(self, thread):
        thread.join(30)

    def py(self, code: str, backend_id: str = 'py1', **profile):
        """args 带 -c，prompt 才走 stdin（start(prompt) 只喂 stdin，不进命令行）。"""
        profile.setdefault('args', ['-c', code])
        return self.backend(backend_id, profile)

    def shim(self, body: str, name: str = 'shim.cmd') -> Path:
        path = self.base / name
        payload = '@echo off\r\n' + body.replace('\n', '\r\n') + '\r\n'
        encoding = 'ascii'
        try:
            payload.encode('ascii')
        except UnicodeEncodeError:      # cmd.exe 按 OEM 代码页读 .cmd
            encoding = 'mbcs'
        path.write_text(payload, encoding=encoding)
        return path


# ── 正常跑通 ────────────────────────────────────────────────────────────────

class CliRunTests(CliTestBase):
    def test_start_returns_stdout_and_pid(self):
        backend = self.py("print('from-cli')")
        result = backend.start(None, timeout=30)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['exit_code'], 0)
        self.assertIn('from-cli', result['stdout'])
        self.assertGreater(result['pid'], 0)
        self.assertEqual(result['state'], 'idle')
        self.assertFalse(result['timed_out'])
        self.assertEqual(result['id'], 'py1')
        self.assertEqual(result['kind'], 'cli')
        self.assertEqual(result['argv'][-2:], ['-c', "print('from-cli')"])
        self.assertIsNone(backend.status()['pid'])

    def test_prompt_goes_to_stdin(self):
        backend = self.py('import sys; print(sys.stdin.read().strip().upper())', 'p-in')
        result = backend.start('hello-stdin', timeout=30)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['stdout'].strip(), 'HELLO-STDIN')

    def test_prompt_body_roundtrip_utf8(self):
        backend = self.py('import sys; print(sys.stdin.read().strip()[::-1])', 'p-rt',
                          env={'PYTHONIOENCODING': 'utf-8'})
        result = backend.start('中文提示', timeout=30)
        self.assertTrue(result['ok'], result)
        self.assertIn('示提文中', result['stdout'])

    def test_stdin_none_means_no_pipe(self):
        backend = self.py('import sys; print(repr(sys.stdin.read()))', 'p-none')
        result = backend.start(None, timeout=30)
        self.assertTrue(result['ok'], result)
        self.assertIn("''", result['stdout'])

    def test_start_rejects_non_str_prompt(self):
        backend = self.py('print(1)', 'p-bad')
        result = backend.start(b'bytes-prompt', timeout=30)
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'bad_profile')

    def test_workdir_applied(self):
        workdir = self.base / 'cwd-dir'
        workdir.mkdir()
        backend = self.backend('wd1', {'workdir': str(workdir)})
        result = backend.start('import os; print(os.getcwd())', timeout=30)
        self.assertTrue(result['ok'], result)
        self.assertEqual(os.path.normcase(result['stdout'].strip()),
                         os.path.normcase(str(workdir)))

    def test_env_passed_and_utf8_output_decoded(self):
        backend = self.backend('env1', {'env': {'AGENT_BACKEND_TEST_TAG': 'tag-42',
                                                'PYTHONIOENCODING': 'utf-8'}})
        result = backend.start(
            'import os; print(os.environ["AGENT_BACKEND_TEST_TAG"]); print("中文输出")',
            timeout=30)
        self.assertTrue(result['ok'], result)
        self.assertIn('tag-42', result['stdout'])
        self.assertIn('中文输出', result['stdout'])

    def test_runs_counter_and_rerun_after_stop(self):
        backend = self.backend('cnt')
        self.assertEqual(backend.status()['runs'], 0)
        self.assertTrue(backend.start("print(1)", timeout=30)['ok'])
        self.assertEqual(backend.status()['runs'], 1)
        self.assertTrue(backend.start("print(2)", timeout=30)['ok'])
        self.assertEqual(backend.status()['runs'], 2)
        backend.stop()
        third = backend.start("print(3)", timeout=30)
        self.assertTrue(third['ok'], third)
        self.assertIn('3', third['stdout'])

    def test_probe_ok_for_python(self):
        backend = self.backend('pr1')
        result = backend.probe()
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['version_ok'], result)
        self.assertTrue(result['runnable'])
        self.assertEqual(result['returncode'], 0)
        self.assertIn('Python', result['stdout'])
        self.assertTrue(os.path.isfile(result['resolved']))

    def test_command_not_found_probe_and_start(self):
        backend = ab.CliBackend('nf1', {'command': 'no-such-command-xyz-42'},
                                data_dir=self.data)
        probe = backend.probe()
        self.assertFalse(probe['ok'])
        self.assertEqual(probe['code'], 'command_not_found')
        run = backend.start('x', timeout=30)
        self.assertFalse(run['ok'])
        self.assertEqual(run['code'], 'command_not_found')
        self.assertEqual(backend.status()['state'], 'failed')

    def test_nonzero_exit_keeps_stderr_tail(self):
        backend = self.backend('nz1')
        result = backend.start(
            'import sys; sys.stderr.write("B"*5000 + "TAIL-MARK"); sys.exit(3)', timeout=30)
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['code'], 'nonzero_exit')
        self.assertEqual(result['exit_code'], 3)
        self.assertIn('退出码 3', result['message'])
        self.assertIn('TAIL-MARK', result['message'])
        self.assertEqual(result['stderr_bytes'], 5009)
        self.assertFalse(result['stderr_truncated'])
        self.assertTrue(result['stderr'].endswith('TAIL-MARK'))
        self.assertEqual(backend.status()['state'], 'failed')

    def test_stdout_truncation_keeps_tail_and_marks(self):
        backend = self.backend('tr1')
        result = backend.start(
            'import sys; sys.stdout.write("x"*200000); sys.stdout.write("END-MARK");'
            ' sys.stdout.flush()', timeout=30)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['stdout_truncated'])
        self.assertEqual(len(result['stdout']), ab.MAX_OUTPUT_BYTES)
        self.assertEqual(result['stdout_bytes'], 200008)
        self.assertEqual(result['stdout_dropped'], 200008 - ab.MAX_OUTPUT_BYTES)
        self.assertTrue(result['stdout'].endswith('END-MARK'))
        self.assertFalse(result['stderr_truncated'])

    def test_stderr_truncation_with_nonzero_exit(self):
        backend = self.backend('tr2')
        result = backend.start(
            'import sys; sys.stderr.write("y"*200000); sys.stderr.write("ERR-TAIL");'
            ' sys.stderr.flush(); sys.exit(4)', timeout=30)
        self.assertEqual(result['code'], 'nonzero_exit')
        self.assertTrue(result['stderr_truncated'])
        self.assertEqual(len(result['stderr']), ab.MAX_OUTPUT_BYTES)
        self.assertTrue(result['stderr'].endswith('ERR-TAIL'))
        self.assertIn('ERR-TAIL', result['message'])

    def test_status_reports_running_then_idle(self):
        backend = self.backend('st1')
        with self.running(backend, 'import time; time.sleep(30)') as box:
            status = backend.status()
            self.assertTrue(status['running'])
            self.assertEqual(status['state'], 'running')
            self.assertEqual(status['pid'], box['pid'])
            self.assertGreater(status['started_at'], 0)
            self.assertTrue(pid_alive(box['pid']))
        self.assertFalse(pid_alive(box['pid']))
        settled = backend.status()
        self.assertFalse(settled['running'])
        self.assertIsNone(settled['pid'])
        self.assertEqual(settled['state'], 'stopped')
        self.assertIsNotNone(settled['last'])

    def test_already_running_on_second_start(self):
        backend = self.backend('ar1')
        with self.running(backend, 'import time; time.sleep(30)'):
            second = backend.start('print(2)', timeout=5)
            self.assertFalse(second['ok'])
            self.assertEqual(second['code'], 'already_running')

    def test_different_backends_run_in_parallel(self):
        first = self.backend('par1')
        second = self.backend('par2')
        results: dict = {}

        def work(name, backend):
            results[name] = backend.start('import time; time.sleep(1.2); print("done")',
                                          timeout=30)

        started = time.monotonic()
        threads = [threading.Thread(target=work, args=('a', first)),
                   threading.Thread(target=work, args=('b', second))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(40)
        elapsed = time.monotonic() - started
        self.assertTrue(results['a']['ok'], results['a'])
        self.assertTrue(results['b']['ok'], results['b'])
        self.assertLess(elapsed, 2.2, '两个后端没有并行：%.2fs' % elapsed)


# ── 取消 / 停止 / 孤儿进程 ──────────────────────────────────────────────────

class CliLifecycleTests(CliTestBase):
    def test_timeout_kills_child_process(self):
        backend = self.backend('to1')
        started = time.monotonic()
        result = backend.start('import time; time.sleep(30)', timeout=1)
        elapsed = time.monotonic() - started
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['code'], 'timeout')
        self.assertTrue(result['timed_out'])
        self.assertIn('已终止', result['message'])
        self.assertLess(elapsed, 15, '超时没有按时收掉：%.1fs' % elapsed)
        self.assertTrue(wait_until(lambda: not pid_alive(result['pid']), 15),
                        '超时后子进程仍在：pid=%s' % result['pid'])
        self.assertIsNone(backend.status()['pid'])

    def test_cancel_then_status_reflects_exit(self):
        backend = self.backend('cx1')
        with self.running(backend, 'import time; time.sleep(30)') as box:
            cancelled = backend.cancel()
            self.assertTrue(cancelled['ok'], cancelled)
            self.assertTrue(cancelled['cancelled'])
            self.assertEqual(cancelled['pid'], box['pid'])
            self.assertTrue(wait_until(lambda: not pid_alive(box['pid']), 15),
                            'cancel 之后子进程还在：pid=%s' % box['pid'])
        result = box['result']
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['code'], 'cancelled')
        self.assertEqual(backend.status()['state'], 'stopped')

    def test_cancel_without_run_is_not_running(self):
        backend = self.backend('cx2')
        result = backend.cancel()
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'not_running')

    def test_stop_is_idempotent(self):
        backend = self.backend('sp1')
        first = backend.stop()
        self.assertTrue(first['ok'], first)
        self.assertTrue(first['already_stopped'])
        self.assertFalse(first['stopped'])
        second = backend.stop()
        self.assertTrue(second['ok'], second)
        self.assertTrue(second['already_stopped'])

        with self.running(backend, 'import time; time.sleep(30)') as box:
            stopped = backend.stop()
            self.assertTrue(stopped['stopped'], stopped)
            self.assertEqual(stopped['pid'], box['pid'])
            self.assertFalse(stopped['already_stopped'])
        again = backend.stop()
        self.assertTrue(again['already_stopped'], again)
        self.assertEqual(again['state'], 'stopped')
        self.assertTrue(wait_until(lambda: not pid_alive(stopped['pid']), 15))

    def test_stop_leaves_no_orphan(self):
        backend = self.backend('orph')
        with self.running(backend, 'import time; time.sleep(30)') as box:
            pid = box['pid']
            self.assertTrue(pid_alive(pid), '启动后进程应存在：pid=%s' % pid)
            stopped = backend.stop()
            self.assertTrue(stopped['stopped'], stopped)
        self.assertTrue(wait_until(lambda: not pid_alive(pid), 15),
                        'stop 之后仍是活进程：pid=%s' % pid)
        self.assertNotIn('orph', [item['id'] for item in ab.live_backends()])

    @unittest.skipUnless(IS_WINDOWS, '进程树检查依赖 Windows 的 taskkill /T 语义')
    def test_stop_kills_whole_process_tree(self):
        shim = self.shim('"{}" -c "import time;time.sleep(30)"'.format(sys.executable))
        backend = self.backend('tree1', {'command': str(shim), 'args': []})
        with self.running(backend, None) as box:
            shim_pid = box['pid']
            # 这里的等待只是前置条件（垫片要先真的拉起子进程），不是产品断言。
            # child_pids 每次都要起一次 PowerShell 查 WMI，整套 5000 个用例连跑、
            # 进程表很大时单次查询能拖到几十秒，20s 的窗口会把"查不到"误判成"没拉起"。
            self.assertTrue(wait_until(lambda: child_pids(shim_pid), 90),
                            '垫片没有拉起子进程，无法验证进程树（垫片 pid=%s 存活=%s）'
                            % (shim_pid, pid_alive(shim_pid)))
            kids = child_pids(shim_pid)
            self.assertTrue(backend.stop()['stopped'])
            self.assertTrue(wait_until(lambda: not pid_alive(shim_pid), 15))
            self.assertTrue(wait_until(lambda: not any(pid_alive(k) for k in kids), 15),
                            '进程树有孤儿残留：%r' % [k for k in kids if pid_alive(k)])
            self.assertEqual([k for k in child_pids(shim_pid) if pid_alive(k)], [])

    def test_live_backends_registry(self):
        backend = self.backend('live1')
        self.assertEqual([item['id'] for item in ab.live_backends() if item['id'] == 'live1'], [])
        with self.running(backend, 'import time; time.sleep(30)') as box:
            entries = {item['id']: item for item in ab.live_backends()}
            self.assertIn('live1', entries)
            self.assertEqual(entries['live1']['pid'], box['pid'])
            self.assertEqual(entries['live1']['kind'], 'cli')
        self.assertTrue(wait_until(
            lambda: 'live1' not in [item['id'] for item in ab.live_backends()], 15))
        self.assertEqual([item for item in ab.live_backends() if item['id'] == 'live1'], [])

    def test_duplicate_id_conflict_between_objects(self):
        first = self.backend('dup1')
        second = ab.CliBackend('dup1', {'command': sys.executable, 'timeout': 30},
                               data_dir=self.data)
        with self.running(first, 'import time; time.sleep(30)'):
            conflict = second.start('print(1)', timeout=5)
            self.assertFalse(conflict['ok'], conflict)
            self.assertEqual(conflict['code'], 'duplicate_id')
        self.assertTrue(second.start('print("free-again")', timeout=30)['ok'])


# ── Windows 垫片 ────────────────────────────────────────────────────────────

@unittest.skipUnless(IS_WINDOWS, 'Windows 垫片语义')
class ShimTests(CliTestBase):
    def test_cmd_shim_runs(self):
        shim = self.shim('echo shim-ok')
        backend = self.backend('shim1', {'command': str(shim)})
        result = backend.start(None, timeout=30)
        self.assertTrue(result['ok'], result)
        self.assertIn('shim-ok', result['stdout'])
        self.assertTrue(result['command'].lower().endswith('shim.cmd'))

    def test_cmd_shim_rejects_metachar_args(self):
        shim = self.shim('echo nope')
        backend = self.backend('shim2', {'command': str(shim), 'args': ['a&b']})
        result = backend.start(None, timeout=30)
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['code'], 'bad_profile')
        self.assertIn('&', result['message'])
        probe = backend.probe()
        self.assertEqual(probe['code'], 'bad_profile')

    def test_probe_reports_nonzero_version_call(self):
        shim = self.shim('exit /b 3')
        backend = self.backend('shim3', {'command': str(shim)})
        result = backend.probe()
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['version_ok'])
        self.assertEqual(result['returncode'], 3)
        self.assertTrue(result['runnable'])


# ── DSH ─────────────────────────────────────────────────────────────────────

DSH = shutil.which('dsh')


class DshBackendTests(CliTestBase):
    @unittest.skipUnless(DSH, '本机 PATH 里没有 dsh')
    def test_probe_finds_dsh(self):
        backend = ab.DshBackend('dsh1', data_dir=self.data)
        self.addCleanup(backend.stop)
        result = backend.probe()
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['runnable'], result)
        self.assertTrue(result['version_ok'], result)
        self.assertEqual(result['returncode'], 0)
        self.assertTrue(result['stdout'].strip(), 'dsh --version 没有输出')
        self.assertIn('dsh', os.path.basename(result['resolved']).lower())
        self.assertGreater(result['duration'], 0.0)

    @unittest.skipUnless(DSH, '本机 PATH 里没有 dsh')
    def test_real_version_run(self):
        backend = ab.DshBackend('dsh2', data_dir=self.data)
        self.addCleanup(backend.stop)
        result = backend.version()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['exit_code'], 0)
        self.assertTrue(result['stdout'].strip())
        self.assertEqual(result['kind'], 'dsh')
        self.assertEqual(backend.status()['state'], 'idle')

    def test_missing_dsh_is_command_not_found(self):
        backend = ab.DshBackend('dsh3', {'command': 'no-such-dsh-xyz-9'}, data_dir=self.data)
        self.addCleanup(backend.stop)
        probe = backend.probe()
        self.assertFalse(probe['ok'])
        self.assertEqual(probe['code'], 'command_not_found')
        run = backend.start(None, timeout=10)
        self.assertFalse(run['ok'])
        self.assertEqual(run['code'], 'command_not_found')
        self.assertEqual(backend.kind, 'dsh')


if __name__ == '__main__':
    unittest.main()
