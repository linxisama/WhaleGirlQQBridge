# -*- coding: utf-8 -*-
"""Windows Sandbox 检测与启用/回滚的单测：全部 mock，不真的调用 DISM、不提权、不重启。

覆盖：已启用、未启用、启用失败、需要重启、权限不足（UAC 取消/740）、家庭版、
重复执行幂等、超时与 PowerShell 缺席、注册表读不到时的降级。
"""
import json
import subprocess as real_subprocess
import types
import unittest
from pathlib import Path
from unittest import mock

from src import windows_sandbox as ws

PRO_REGISTRY = {
    "ProductName": "Windows 11 Pro",
    "EditionID": "Professional",
    "CurrentBuildNumber": "22631",
    "DisplayVersion": "23H2",
    "ReleaseId": "2009",
}
HOME_REGISTRY = {
    "ProductName": "Windows 11 Home",
    "EditionID": "Core",
    "CurrentBuildNumber": "22631",
    "DisplayVersion": "23H2",
}
OLD_REGISTRY = {
    "ProductName": "Windows 10 Pro",
    "EditionID": "Professional",
    "CurrentBuildNumber": "17763",
}


def fake_platform(system="Windows", release="10", version="10.0.22631", machine="AMD64"):
    label = f"{system}-{release}-{version}"
    return types.SimpleNamespace(
        system=lambda: system,
        release=lambda: release,
        version=lambda: version,
        machine=lambda: machine,
        platform=lambda: label,
    )


class FakeWinreg:
    """假的 winreg：只有 OpenKey/QueryValueEx/CloseKey 三个入口被模块用到。"""

    HKEY_LOCAL_MACHINE = "HKLM"
    KEY_READ = 0x20019
    KEY_WOW64_64KEY = 0x0100

    def __init__(self, values=None, deny=False):
        self.values = dict(values or {})
        self.deny = deny
        self.closed = []

    def OpenKey(self, root, path, reserved=0, access=0):
        if self.deny or not self.values:
            raise OSError("拒绝访问（模拟普通用户读不到）")
        return {"root": root, "path": path}

    def QueryValueEx(self, key, name):
        if name in self.values:
            return (self.values[name], 1)
        raise FileNotFoundError(name)

    def CloseKey(self, key):
        self.closed.append(key)


def script_result(stdout="", returncode=0, stderr="", timed_out=False, error=""):
    return ws.ScriptResult(
        ok=(returncode == 0 and not timed_out and not error),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        error=error,
    )


def probe_json(state, source="optional-feature", detail=""):
    return json.dumps(
        {"schema": 1, "feature": {"name": ws.FEATURE_NAME, "state": state,
                                  "state_source": source, "detail": detail}},
        ensure_ascii=False,
    )


class Runner:
    """记录调用次数的假提权 runner；error 用于验证"根本不该被调用"。"""

    def __init__(self, result=None, error=None):
        self.result = result if result is not None else script_result(returncode=0)
        self.error = error
        self.calls = []

    def __call__(self, script, arguments, timeout):
        self.calls.append((script, list(arguments), timeout))
        if self.error is not None:
            raise self.error
        return self.result


class SandboxTestBase(unittest.TestCase):
    def setUp(self):
        # 兜底：任何没被显式 mock 的真实子进程调用都会走这里，绝不落到真 PowerShell 上。
        self.forbidden_calls = []

        real_run = ws._run_process

        def _forbid(argv, timeout=0):
            # RunProcessTests 会把 ws.subprocess 换成假模块，那种情况下要走真实逻辑。
            if not isinstance(ws.subprocess, types.ModuleType):
                return real_run(argv, timeout=timeout)
            self.forbidden_calls.append(list(argv))
            return script_result(error="测试环境禁止启动真实进程")

        self._guard = mock.patch.object(ws, "_run_process", side_effect=_forbid)
        self._guard.start()

    def tearDown(self):
        self._guard.stop()

    def detection(self, feature_state=ws.STATE_DISABLED, registry=None, exe=True,
                  platform_module=None):
        return ws.detect(
            registry=PRO_REGISTRY if registry is None else registry,
            feature=ws.FeatureState(feature_state),
            exe_checker=lambda path: exe,
            platform_module=platform_module or fake_platform(),
        )


class DetectWindowsTests(SandboxTestBase):
    def test_reads_registry_values_without_admin(self):
        fake = FakeWinreg(PRO_REGISTRY)
        with mock.patch.object(ws, "_winreg_module", lambda: fake):
            report = ws.detect_windows(platform_module=fake_platform())
        self.assertEqual(report.edition_id, "Professional")
        self.assertEqual(report.build, 22631)
        self.assertTrue(report.is_windows)
        self.assertFalse(report.is_home)
        self.assertTrue(fake.closed, "读完必须 CloseKey")

    def test_registry_denied_returns_empty_and_degrades(self):
        with mock.patch.object(ws, "_winreg_module", lambda: FakeWinreg(deny=True)):
            self.assertEqual(ws.read_current_version(), {})
            report = ws.detect_windows(platform_module=fake_platform(version="10.0.22631"))
        self.assertEqual(report.build, 22631, "注册表读不到时要用 platform.version() 兜底")
        self.assertTrue(report.supports_build)

    def test_no_winreg_module_is_not_an_error(self):
        with mock.patch.object(ws, "_winreg_module", lambda: None):
            self.assertEqual(ws.read_current_version(), {})

    def test_non_windows_is_unsupported(self):
        det = ws.detect(
            registry={},
            platform_module=fake_platform(system="Linux", release="6.5",
                                          version="6.5.0-15-generic"),
            exe_checker=lambda path: False,
        )
        self.assertEqual(det.support.code, "not_windows")
        self.assertFalse(det.support.supported)
        self.assertFalse(det.ready)
        self.assertEqual(det.feature.state, ws.STATE_UNKNOWN)
        self.assertTrue(det.warnings)

    def test_home_edition_is_unsupported_with_alternative(self):
        det = self.detection(registry=HOME_REGISTRY, exe=False)
        self.assertEqual(det.support.code, "home_edition")
        self.assertFalse(det.support.supported)
        self.assertIn("专业版", det.support.alternative)
        self.assertFalse(det.ready)

    def test_too_old_build_is_unsupported(self):
        det = self.detection(registry=OLD_REGISTRY, exe=False)
        self.assertEqual(det.support.code, "build_too_old")
        self.assertFalse(det.support.supported)

    def test_enabled_feature_marks_ready(self):
        det = self.detection(feature_state=ws.STATE_ENABLED)
        self.assertEqual(det.support.code, "supported")
        self.assertTrue(det.ready)
        self.assertTrue(det.windows.display_name.startswith("Windows 11"))
        self.assertFalse(det.warnings)

    def test_unknown_feature_state_warns_and_is_not_ready(self):
        det = self.detection(feature_state=ws.STATE_UNKNOWN)
        self.assertFalse(det.ready)
        self.assertTrue(any("无法确认" in item for item in det.warnings))

    def test_disabled_feature_with_exe_present_warns(self):
        det = self.detection(feature_state=ws.STATE_DISABLED, exe=True)
        self.assertFalse(det.ready)
        self.assertTrue(any("状态可能已过期" in item for item in det.warnings))

    def test_enabled_without_exe_warns(self):
        det = self.detection(feature_state=ws.STATE_ENABLED, exe=False)
        self.assertFalse(det.ready)
        self.assertTrue(any("WindowsSandbox.exe" in item for item in det.warnings))

    def test_detection_dict_is_json_serialisable(self):
        payload = self.detection(feature_state=ws.STATE_ENABLED).to_dict()
        self.assertEqual(json.loads(json.dumps(payload, ensure_ascii=False))["support"]["code"],
                         "supported")
        self.assertEqual(payload["schema"], ws.SCHEMA_VERSION)

    def test_detect_without_injected_feature_never_spawns_real_process(self):
        det = ws.detect(registry=PRO_REGISTRY, platform_module=fake_platform(),
                        exe_checker=lambda path: True)
        self.assertEqual(det.feature.state, ws.STATE_UNKNOWN)
        self.assertEqual(len(self.forbidden_calls), 1, "探测应走 mock 的 _run_process")


class FeatureStateTests(SandboxTestBase):
    def test_enabled_from_probe_json(self):
        state = ws.detect_feature_state(
            probe_runner=lambda script, args, timeout: script_result(probe_json("Enabled")))
        self.assertEqual(state.state, ws.STATE_ENABLED)
        self.assertEqual(state.source, "optional-feature")

    def test_absent_from_probe_json(self):
        state = ws.detect_feature_state(
            probe_runner=lambda script, args, timeout: script_result(probe_json("Absent")))
        self.assertEqual(state.state, ws.STATE_ABSENT)

    def test_unrecognised_state_becomes_unknown_with_detail(self):
        state = ws.detect_feature_state(
            probe_runner=lambda script, args, timeout: script_result(probe_json("Enable Pending")))
        self.assertIn(state.state, (ws.STATE_UNKNOWN, ws.STATE_DISABLED))
        self.assertTrue(state.raw)

    def test_timeout_is_reported(self):
        state = ws.detect_feature_state(
            probe_runner=lambda script, args, timeout: script_result(
                timed_out=True, error="PowerShell 超时（20s）"))
        self.assertEqual(state.state, ws.STATE_UNKNOWN)
        self.assertEqual(state.source, "timeout")

    def test_missing_powershell_is_reported(self):
        state = ws.detect_feature_state(
            probe_runner=lambda script, args, timeout: script_result(
                error="找不到 PowerShell：FileNotFoundError"))
        self.assertEqual(state.state, ws.STATE_UNKNOWN)
        self.assertEqual(state.source, "no_powershell")

    def test_garbage_output_is_parse_error(self):
        state = ws.detect_feature_state(
            probe_runner=lambda script, args, timeout: script_result(stdout="not json at all"))
        self.assertEqual(state.state, ws.STATE_UNKNOWN)
        self.assertEqual(state.source, "parse_error")

    def test_script_arguments_use_feature_switch(self):
        seen = {}

        def runner(script, args, timeout):
            seen["script"] = script
            seen["args"] = list(args)
            return script_result(probe_json("Disabled"))

        probe = Path(r"C:\fake\probe_sandbox.ps1")
        ws.detect_feature_state(probe_runner=runner, script=probe)
        self.assertEqual(seen["script"], probe)
        self.assertEqual(seen["args"], ["-Feature", ws.FEATURE_NAME])

    def test_inline_branch_when_script_missing(self):
        seen = {}

        def runner(script, args, timeout):
            seen["script"] = script
            seen["args"] = list(args)
            return script_result(probe_json("Disabled"))

        with mock.patch.object(ws, "sandbox_script", lambda name, roots=None: None):
            state = ws.detect_feature_state(probe_runner=runner)
        self.assertIsNone(seen["script"])
        self.assertEqual(seen["args"], [ws.FEATURE_NAME])
        self.assertEqual(state.state, ws.STATE_DISABLED)


class RunProcessTests(SandboxTestBase):
    def _fake_subprocess(self, run):
        return types.SimpleNamespace(
            run=run,
            CREATE_NO_WINDOW=0x08000000,
            TimeoutExpired=real_subprocess.TimeoutExpired,
            SubprocessError=real_subprocess.SubprocessError,
            DETACHED_PROCESS=0x00000008,
            CREATE_NEW_PROCESS_GROUP=0x00000200,
            Popen=lambda *a, **k: self.fail("测试里不允许真的启动进程"),
        )

    def test_command_uses_no_profile_and_bypass(self):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

        with mock.patch.object(ws, "subprocess", self._fake_subprocess(run)):
            result = ws._run_powershell_command("Write-Output hi")
        self.assertTrue(result.ok)
        argv = calls[0][0]
        self.assertIn("-NoProfile", argv)
        self.assertIn("-NonInteractive", argv)
        self.assertEqual(argv[-2], "-Command")
        self.assertEqual(calls[0][1]["timeout"], ws.DETECT_TIMEOUT)

    def test_script_uses_file_switch(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return types.SimpleNamespace(returncode=0, stdout="{}", stderr="")

        with mock.patch.object(ws, "subprocess", self._fake_subprocess(run)):
            ws._run_powershell_script(Path(r"C:\fake\a.ps1"), ["-Feature", "X"])
        self.assertEqual(calls[0][-4:-2], ["-File", r"C:\fake\a.ps1"])
        self.assertEqual(calls[0][-2:], ["-Feature", "X"])

    def test_timeout_exception_is_swallowed(self):
        def run(argv, **kwargs):
            raise real_subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 1))

        with mock.patch.object(ws, "subprocess", self._fake_subprocess(run)):
            result = ws._run_powershell_command("x", timeout=5)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)

    def test_missing_powershell_exe_is_reported(self):
        def run(argv, **kwargs):
            raise FileNotFoundError("powershell.exe")

        with mock.patch.object(ws, "subprocess", self._fake_subprocess(run)):
            result = ws._run_powershell_command("x")
        self.assertFalse(result.ok)
        self.assertIn("找不到 PowerShell", result.error)


class EnableTests(SandboxTestBase):
    def test_already_enabled_is_idempotent(self):
        det = self.detection(feature_state=ws.STATE_ENABLED)
        runner = Runner(error=AssertionError("已启用时不该提权"))
        first = ws.enable_sandbox(detection=det, elevated_runner=runner)
        second = ws.enable_sandbox(detection=det, elevated_runner=runner)
        for result in (first, second):
            self.assertTrue(result.ok)
            self.assertEqual(result.status, "already_enabled")
            self.assertFalse(result.needs_reboot)
            self.assertEqual(result.reason, "")
        self.assertEqual(runner.calls, [])

    def test_plan_without_apply_returns_command(self):
        det = self.detection(feature_state=ws.STATE_DISABLED)
        runner = Runner(error=AssertionError("dry-run 不该提权"))
        result = ws.enable_sandbox(apply=False, detection=det, elevated_runner=runner)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "planned")
        self.assertFalse(result.needs_reboot)
        self.assertIn("/enable-feature", result.command)
        self.assertIn(ws.FEATURE_NAME, result.command)
        self.assertIn("Enable-WindowsOptionalFeature", result.powershell_command)
        self.assertEqual(runner.calls, [])

    def test_success_reports_needs_reboot(self):
        det = self.detection(feature_state=ws.STATE_DISABLED)
        runner = Runner(script_result(stdout='{"status":"enabled_pending_reboot"}'))
        result = ws.enable_sandbox(detection=det, elevated_runner=runner)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "enabled")
        self.assertTrue(result.needs_reboot)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(any("不会自动重启" in note for note in result.notes))
        self.assertEqual(len(runner.calls), 1)

    def test_runner_receives_script_and_feature_arguments(self):
        det = self.detection(feature_state=ws.STATE_DISABLED)
        runner = Runner()
        ws.enable_sandbox(detection=det, elevated_runner=runner, timeout=42)
        script, arguments, timeout = runner.calls[0]
        self.assertEqual(arguments[0], "-Feature")
        self.assertIn(ws.FEATURE_NAME, arguments)
        self.assertEqual(timeout, 42)
        self.assertIsNotNone(script, "仓库里应当能定位到 tools/sandbox/enable_sandbox.ps1")
        self.assertTrue(str(script).endswith("enable_sandbox.ps1"))

    def test_exit_3010_means_pending_reboot(self):
        det = self.detection(feature_state=ws.STATE_DISABLED)
        result = ws.enable_sandbox(detection=det,
                                   elevated_runner=Runner(script_result(returncode=3010)))
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "pending_reboot")
        self.assertTrue(result.needs_reboot)

    def test_uac_cancelled(self):
        det = self.detection(feature_state=ws.STATE_DISABLED)
        result = ws.enable_sandbox(
            detection=det,
            elevated_runner=Runner(script_result(returncode=1223, stderr="操作已被用户取消")))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "needs_elevation")
        self.assertEqual(result.reason, "uac_cancelled")
        self.assertIn("操作已被用户取消", result.detail)

    def test_elevation_required(self):
        det = self.detection(feature_state=ws.STATE_DISABLED)
        result = ws.enable_sandbox(detection=det,
                                   elevated_runner=Runner(script_result(returncode=740)))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "elevation_required")

    def test_access_denied_hresult_is_normalised(self):
        det = self.detection(feature_state=ws.STATE_DISABLED)
        result = ws.enable_sandbox(
            detection=det, elevated_runner=Runner(script_result(returncode=-2147024891)))
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 0x80070005)
        self.assertEqual(result.reason, "elevation_required")

    def test_source_files_missing(self):
        det = self.detection(feature_state=ws.STATE_DISABLED)
        result = ws.enable_sandbox(
            detection=det,
            elevated_runner=Runner(script_result(returncode=0x800F0906,
                                                 stderr="找不到源文件")))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "source_files_missing")
        self.assertIn("找不到源文件", result.detail)

    def test_timeout_reported(self):
        det = self.detection(feature_state=ws.STATE_DISABLED)
        result = ws.enable_sandbox(
            detection=det,
            elevated_runner=Runner(script_result(timed_out=True, error="PowerShell 超时（900s）")))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "timeout")
        self.assertTrue(result.detail)

    def test_unknown_exit_code_is_not_silent(self):
        det = self.detection(feature_state=ws.STATE_DISABLED)
        result = ws.enable_sandbox(
            detection=det,
            elevated_runner=Runner(script_result(returncode=1, stderr="拒绝访问")))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "exit_1")
        self.assertIn("拒绝访问", result.detail)
        self.assertTrue(result.command)

    def test_home_edition_never_elevates(self):
        det = self.detection(registry=HOME_REGISTRY, exe=False)
        runner = Runner(error=AssertionError("家庭版不该提权"))
        result = ws.enable_sandbox(detection=det, elevated_runner=runner)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.reason, "home_edition")
        self.assertIn("专业版", result.detail)
        self.assertEqual(runner.calls, [])

    def test_unknown_state_still_attempts_once(self):
        det = self.detection(feature_state=ws.STATE_UNKNOWN)
        runner = Runner()
        result = ws.enable_sandbox(detection=det, elevated_runner=runner)
        self.assertTrue(result.ok)
        self.assertEqual(len(runner.calls), 1)
        self.assertTrue(any("幂等" in note for note in result.notes))


class DisableTests(SandboxTestBase):
    def test_plan_returns_commands_and_never_restarts(self):
        det = self.detection(feature_state=ws.STATE_ENABLED)
        runner = Runner(error=AssertionError("默认禁用不该提权"))
        result = ws.disable_sandbox(detection=det, elevated_runner=runner)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "planned")
        self.assertIn("/disable-feature", result.command)
        self.assertIn("Disable-WindowsOptionalFeature", result.powershell_command)
        self.assertGreater(len(result.notes), 3)
        self.assertFalse(result.needs_reboot)
        self.assertEqual(runner.calls, [])
        for note in result.notes:
            for forbidden in ("Restart-Computer", "shutdown", "/restart"):
                self.assertNotIn(forbidden, note)

    def test_apply_runs_elevated_but_does_not_reboot(self):
        det = self.detection(feature_state=ws.STATE_ENABLED)
        runner = Runner()
        result = ws.disable_sandbox(apply=True, detection=det, elevated_runner=runner)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "disabled")
        self.assertTrue(result.needs_reboot)
        self.assertEqual(len(runner.calls), 1)
        self.assertTrue(any("不会自动重启" in note for note in result.notes))

    def test_already_disabled_short_circuits(self):
        det = self.detection(feature_state=ws.STATE_DISABLED)
        runner = Runner(error=AssertionError("已禁用不该提权"))
        result = ws.disable_sandbox(apply=True, detection=det, elevated_runner=runner)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "already_disabled")
        self.assertEqual(runner.calls, [])

    def test_commands_never_contain_restart_triggers(self):
        for text in (ws.enable_command(), ws.disable_command(),
                     ws.enable_powershell_command(), ws.disable_powershell_command()):
            for forbidden in ("Restart-Computer", "shutdown", "/restart"):
                self.assertNotIn(forbidden, text)
        for step in ws.manual_instructions("enable") + ws.manual_instructions("disable"):
            for forbidden in ("Restart-Computer", "shutdown", "/restart"):
                self.assertNotIn(forbidden, step)


class EnsureReadyTests(SandboxTestBase):
    def test_home_edition_unsupported(self):
        det = self.detection(registry=HOME_REGISTRY, exe=False)
        payload = ws.ensure_ready(detection=det,
                                  elevated_runner=Runner(error=AssertionError("不该提权")))
        self.assertEqual(payload["status"], "unsupported")
        self.assertFalse(payload["ok"])
        self.assertIn("alternative", payload)

    def test_ready_when_enabled(self):
        payload = ws.ensure_ready(detection=self.detection(feature_state=ws.STATE_ENABLED))
        self.assertEqual(payload["status"], "ready")
        self.assertTrue(payload["ok"])

    def test_enables_when_disabled(self):
        payload = ws.ensure_ready(detection=self.detection(feature_state=ws.STATE_DISABLED),
                                  elevated_runner=Runner())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "enabled")
        self.assertTrue(payload["enable"]["needs_reboot"])


class ScriptAssetTests(unittest.TestCase):
    def test_helper_scripts_are_present_and_utf8(self):
        for name in (ws.PROBE_SCRIPT, ws.ENABLE_SCRIPT, ws.DISABLE_SCRIPT):
            path = ws.sandbox_script(name)
            self.assertIsNotNone(path, f"缺少 tools/sandbox/{name}")
            raw = Path(path).read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"),
                            f"{name} 需要 UTF-8 BOM，否则 Windows PowerShell 5.1 会读乱中文")
            text = raw.decode("utf-8-sig")
            self.assertIn(ws.FEATURE_NAME, text)
            self.assertNotIn("Restart-Computer", text)

    def test_channel_dir_rejects_separator(self):
        with self.assertRaises(ValueError):
            ws.channel_dir("C:/tmp", "a/b")


if __name__ == "__main__":
    unittest.main()
