# -*- coding: utf-8 -*-
"""Windows Sandbox 检测、启用与隔离通道（.wsb）生成。

定位：控制台要能把不受信任的任务关进 Windows Sandbox 里跑，所以需要三件事——
先判断本机到底支不支持（不需要管理员），用户同意后再走 UAC 提权启用可选功能
Containers-DisposableClientVM，最后生成 .wsb 配置并启动沙盒。

几条硬约束（改之前先读）：
* 检测只读注册表和文件存在性；Get-WindowsOptionalFeature 需要管理员，拿不到就
  老实回 Unknown，不能把"没查到"当成"没启用"。
* 启用/禁用走 UAC 提权子进程，结局一律回结构化结果；失败必须带 reason + detail，
  不允许静默返回成功。
* 本模块从不重启系统、不改系统策略；禁用只给命令和人工步骤。
* .wsb 里所有宿主路径做 XML 转义；重复调用内容相同就不重写文件（幂等）。
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
import xml.sax.saxutils as _saxutils
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

__all__ = [
    "FEATURE_NAME",
    "MIN_BUILD",
    "STATE_ENABLED",
    "STATE_DISABLED",
    "STATE_ABSENT",
    "STATE_UNKNOWN",
    "ScriptResult",
    "WindowsReport",
    "SandboxSupport",
    "FeatureState",
    "SandboxDetection",
    "FeatureActionResult",
    "SandboxConfig",
    "IsolationChannel",
    "LaunchResult",
    "ChannelStatus",
    "detect_windows",
    "assess_support",
    "detect_feature_state",
    "detect",
    "enable_sandbox",
    "disable_sandbox",
    "ensure_ready",
    "build_wsb",
    "default_config",
    "create_isolation_channel",
    "launch_sandbox",
    "read_channel_status",
    "enable_command",
    "disable_command",
    "enable_powershell_command",
    "disable_powershell_command",
    "manual_instructions",
    "sandbox_script",
    "channel_dir",
]

SCHEMA_VERSION = 1
FEATURE_NAME = "Containers-DisposableClientVM"
SANDBOX_EXE_NAME = "WindowsSandbox.exe"
MIN_BUILD = 18362  # Windows 10 1903 起才有 Windows Sandbox
DETECT_TIMEOUT = 20
ENABLE_TIMEOUT = 900  # DISM 装可选功能偶尔要好几分钟
LAUNCH_TIMEOUT = 30

CURRENT_VERSION_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
CURRENT_VERSION_FIELDS = (
    "ProductName",
    "EditionID",
    "CurrentBuildNumber",
    "CurrentBuild",
    "DisplayVersion",
    "ReleaseId",
)

STATE_ENABLED = "Enabled"
STATE_DISABLED = "Disabled"
STATE_ABSENT = "Absent"
STATE_UNKNOWN = "Unknown"
FEATURE_STATES = (STATE_ENABLED, STATE_DISABLED, STATE_ABSENT, STATE_UNKNOWN)

#: 家庭版/中国版等 SKU：Windows Sandbox 不提供，也不装 Containers-DisposableClientVM。
HOME_EDITION_IDS = frozenset(
    {
        "core",
        "coren",
        "corecountryspecific",
        "coresinglelanguage",
        "corechina",
        "corearm",
        "coreconnected",
        "coreconnectedn",
        "starter",
        "cloud",
        "cloudn",
        "cloudedition",
        "iotcore",
        "home",
    }
)

EXIT_OK = 0
EXIT_REBOOT_REQUIRED = 3010
EXIT_UAC_CANCELLED = 1223
EXIT_ELEVATION_REQUIRED = 740
EXIT_ACCESS_DENIED = 5
EXIT_SOURCE_FILES_MISSING = 0x800F0906
EXIT_SOURCE_FILES_NOT_FOUND = 0x800F081F
EXIT_SOURCE_FILES_NOT_FOUND_ALT = 0x800F0907
EXIT_FEATURE_ABSENT = 0x80070490
EXIT_ACCESS_DENIED_HR = 0x80070005

CHANNEL_FOLDER_NAME = "sandbox"
WSB_FILE_NAME = "sandbox.wsb"
MANIFEST_FILE_NAME = "channel.json"
DEFAULT_CHANNEL_NAME = "default"
DEFAULT_SANDBOX_FOLDER = r"C:\WhaleGirl"
DEFAULT_READY_MARKER = "sandbox_ready.txt"

PROBE_SCRIPT = "probe_sandbox.ps1"
ENABLE_SCRIPT = "enable_sandbox.ps1"
DISABLE_SCRIPT = "disable_sandbox.ps1"

NO_REBOOT_NOTE = "本模块不会自动重启系统：需要用户在合适的时间自己重启。"


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _tail(text: Any, limit: int = 400) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else "..." + value[-limit:]


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _normalize_exit(returncode: Optional[int]) -> Optional[int]:
    """把负的 32 位退出码（如 -2147024891）折回无符号 HRESULT 口径。"""
    if returncode is None:
        return None
    value = int(returncode)
    return value & 0xFFFFFFFF if value < 0 else value


def _xml_escape(text: Any) -> str:
    return _saxutils.escape(str(text if text is not None else ""),
                            {'"': "&quot;", "'": "&apos;"})


def _ps_quote(text: Any) -> str:
    return "'" + str(text if text is not None else "").replace("'", "''") + "'"


def _safe_call(module: Any, name: str, default: Any = "") -> Any:
    func = getattr(module, name, None)
    if not callable(func):
        return default
    try:
        return func()
    except Exception:
        return default


def _winreg_module():
    try:
        import winreg  # noqa: WPS433 - 只在 Windows 存在
    except ImportError:
        return None
    return winreg


def _now_platform():
    return platform


def candidate_roots() -> List[Path]:
    """查找 tools/sandbox/*.ps1 的候选根目录（源码模式与冻结模式都要能找到）。"""
    roots: List[Path] = []
    try:
        roots.append(Path(__file__).resolve().parents[1])
    except (OSError, IndexError):
        pass
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    try:
        roots.append(Path(sys.executable).resolve().parent)
    except (OSError, ValueError):
        pass
    home = os.environ.get("WHALEGIRL_HOME") or ""
    if home.strip():
        roots.append(Path(home))
    return roots


def sandbox_script(name: str, *, roots: Optional[Sequence[Any]] = None) -> Optional[Path]:
    """返回 tools/sandbox/<name>.ps1 的绝对路径；找不到返回 None。"""
    filename = name if str(name).endswith(".ps1") else f"{name}.ps1"
    for root in (roots or candidate_roots()):
        try:
            path = Path(root) / "tools" / "sandbox" / filename
            if path.is_file():
                return path
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------- #
# PowerShell 调用
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ScriptResult:
    ok: bool
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "error": self.error,
        }


def _powershell_exe() -> str:
    return "powershell.exe"


def _run_process(argv: Sequence[str], *, timeout: int) -> ScriptResult:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=flags,
        )
    except subprocess.TimeoutExpired as exc:
        return ScriptResult(
            ok=False,
            returncode=None,
            stdout=_tail(getattr(exc, "stdout", "") or "", 2000),
            stderr=_tail(getattr(exc, "stderr", "") or "", 2000),
            timed_out=True,
            error=f"PowerShell 超时（{timeout}s）",
        )
    except FileNotFoundError as exc:
        return ScriptResult(ok=False, error=f"找不到 PowerShell：{exc}")
    except OSError as exc:
        return ScriptResult(ok=False, error=f"无法启动 PowerShell：{exc}")
    return ScriptResult(
        ok=completed.returncode == 0,
        returncode=completed.returncode,
        stdout=str(completed.stdout or ""),
        stderr=str(completed.stderr or ""),
        error="" if completed.returncode == 0 else _tail(completed.stderr or completed.stdout, 400),
    )


def _run_powershell_command(command: str, *, timeout: int = DETECT_TIMEOUT) -> ScriptResult:
    return _run_process(
        [_powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", command],
        timeout=timeout,
    )


def _run_powershell_script(script: Path, arguments: Sequence[str] = (), *,
                           timeout: int = DETECT_TIMEOUT) -> ScriptResult:
    argv = [_powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script), *[str(item) for item in arguments]]
    return _run_process(argv, timeout=timeout)


def _elevation_wrapper(file_path: str, arguments: Sequence[str]) -> str:
    """包一层 Start-Process -Verb RunAs：UAC 被取消时把 1223 传回来，而不是假装成功。"""
    joined = ",".join(_ps_quote(item) for item in arguments)
    return (
        "$ErrorActionPreference='Stop';"
        "try{ $p = Start-Process -FilePath " + _ps_quote(file_path) +
        " -ArgumentList @(" + joined + ")"
        " -Verb RunAs -Wait -PassThru -WindowStyle Hidden; exit $p.ExitCode }"
        "catch{ $code = " + str(EXIT_ELEVATION_REQUIRED) + ";"
        " try { if ($_.Exception.NativeErrorCode -eq " + str(EXIT_UAC_CANCELLED) + ")"
        " { $code = " + str(EXIT_UAC_CANCELLED) + " }"
        " elseif ($_.Exception.HResult -eq -2147023673)"
        " { $code = " + str(EXIT_UAC_CANCELLED) + " } } catch {};"
        " Write-Error $_.Exception.Message; exit $code }"
    )


def _run_elevated_script(script: Path, arguments: Sequence[str] = (), *,
                         timeout: int = ENABLE_TIMEOUT) -> ScriptResult:
    inner = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script), *[str(item) for item in arguments]]
    return _run_powershell_command(_elevation_wrapper(_powershell_exe(), inner), timeout=timeout)


def _run_elevated_command(command: str, *, timeout: int = ENABLE_TIMEOUT) -> ScriptResult:
    inner = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command]
    return _run_powershell_command(_elevation_wrapper(_powershell_exe(), inner), timeout=timeout)


def _dism_arguments(action: str, feature: str = FEATURE_NAME) -> List[str]:
    args = ["/online", f"/{action}-feature", f"/featurename:{feature}"]
    if action == "enable":
        args.append("/all")
    args.append("/norestart")
    return args


def _inline_probe_command(feature: str = FEATURE_NAME) -> str:
    exe_line = "$exe = Join-Path $env:SystemRoot 'System32\\WindowsSandbox.exe'"
    return (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "$state='Unknown'; $source='unavailable'; $detail='';"
        "try{ $f = Get-WindowsOptionalFeature -Online -FeatureName " + _ps_quote(feature) +
        " -ErrorAction Stop; $state=[string]$f.State; $source='optional-feature' }"
        "catch{ $detail=[string]$_.Exception.Message };"
        + exe_line + ";"
        "$el = ([Security.Principal.WindowsPrincipal]"
        "[Security.Principal.WindowsIdentity]::GetCurrent())"
        ".IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator);"
        "@{ feature=@{ name=" + _ps_quote(feature) + "; state=$state;"
        " state_source=$source; detail=$detail };"
        " os=@{}; elevated=$el;"
        " exe=@{ present=(Test-Path -LiteralPath $exe); path=$exe } }"
        " | ConvertTo-Json -Depth 5 -Compress"
    )


def _inline_dism_elevated(action: str, feature: str = FEATURE_NAME) -> str:
    return _elevation_wrapper("dism.exe", _dism_arguments(action, feature))


# --------------------------------------------------------------------------- #
# 检测
# --------------------------------------------------------------------------- #
def read_current_version() -> Dict[str, str]:
    """读 HKLM\\...\\CurrentVersion（普通用户可读，不需要管理员）。"""
    winreg = _winreg_module()
    if winreg is None:
        return {}
    key = None
    values: Dict[str, str] = {}
    try:
        access = getattr(winreg, "KEY_READ", 0) | getattr(winreg, "KEY_WOW64_64KEY", 0)
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, CURRENT_VERSION_KEY, 0, access)
        for name in CURRENT_VERSION_FIELDS:
            try:
                value, _ = winreg.QueryValueEx(key, name)
            except OSError:
                continue
            if value not in (None, ""):
                values[name] = str(value)
    except OSError:
        return {}
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except OSError:
                pass
    return values


def _build_from_version(version_string: str) -> int:
    parts = str(version_string or "").split(".")
    if len(parts) >= 3:
        return _to_int(parts[2])
    if len(parts) == 2:
        return _to_int(parts[1])
    return 0


def _display_name(product_name: str, edition_id: str, build: int) -> str:
    family = "Windows 11" if build >= 22000 else ("Windows 10" if build else "Windows")
    edition = (edition_id or "").strip()
    if not edition:
        # ProductName 形如 "Windows 11 Pro"：去掉前面的家族名，留下版本名
        cleaned = product_name.strip()
        for prefix in ("Windows 11", "Windows 10", "Windows"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()
                break
        edition = cleaned or product_name.strip()
    return f"{family} {edition}".strip()


@dataclass(frozen=True)
class WindowsReport:
    is_windows: bool
    product_name: str
    edition_id: str
    display_name: str
    display_version: str
    build: int
    version_string: str
    architecture: str
    platform_label: str

    @property
    def is_home(self) -> bool:
        edition = (self.edition_id or "").strip().lower()
        name = (self.product_name or "").lower()
        return edition in HOME_EDITION_IDS or "home" in name

    @property
    def supports_build(self) -> bool:
        return self.build >= MIN_BUILD

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_windows": self.is_windows,
            "product_name": self.product_name,
            "edition_id": self.edition_id,
            "display_name": self.display_name,
            "display_version": self.display_version,
            "build": self.build,
            "version": self.version_string,
            "architecture": self.architecture,
            "platform": self.platform_label,
            "is_home": self.is_home,
            "supports_build": self.supports_build,
        }


def detect_windows(*, registry: Optional[Dict[str, str]] = None,
                   platform_module: Any = None) -> WindowsReport:
    module = platform_module or platform
    values: Dict[str, str] = dict(registry) if registry is not None else read_current_version()
    system = str(_safe_call(module, "system", ""))
    release = str(_safe_call(module, "release", ""))
    version_string = str(_safe_call(module, "version", ""))
    machine = str(_safe_call(module, "machine", ""))
    label = str(_safe_call(module, "platform", ""))
    is_windows = system.lower().startswith("win") or label.lower().startswith("win")

    build = _to_int(values.get("CurrentBuildNumber") or values.get("CurrentBuild"))
    if build <= 0:
        build = _build_from_version(version_string)
    edition_id = str(values.get("EditionID") or "")
    product_name = str(values.get("ProductName") or "")
    display_version = str(
        values.get("DisplayVersion") or values.get("ReleaseId") or release or ""
    )
    return WindowsReport(
        is_windows=bool(is_windows),
        product_name=product_name,
        edition_id=edition_id,
        display_name=_display_name(product_name, edition_id, build),
        display_version=display_version,
        build=build,
        version_string=version_string,
        architecture=machine,
        platform_label=label,
    )


@dataclass(frozen=True)
class SandboxSupport:
    supported: bool
    code: str
    message: str
    alternative: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "supported": self.supported,
            "code": self.code,
            "message": self.message,
            "alternative": self.alternative,
        }


def assess_support(windows: WindowsReport) -> SandboxSupport:
    if not windows.is_windows:
        return SandboxSupport(
            supported=False,
            code="not_windows",
            message="当前系统不是 Windows，无法使用 Windows Sandbox。",
            alternative="Windows Sandbox 只在 Windows 10 1903+ / Windows 11 的专业版、企业版、教育版提供。",
        )
    if windows.is_home:
        return SandboxSupport(
            supported=False,
            code="home_edition",
            message=(
                f"{windows.display_name}（{windows.edition_id or '家庭版'}）不提供 Windows Sandbox，"
                "也不包含 Containers-DisposableClientVM 可选功能。"
            ),
            alternative=(
                "替代方案：升级到专业版/企业版/教育版后启用 Windows Sandbox；"
                "或改用独立虚拟机、受限本地账户运行不受信任的任务。本模块不会尝试强行安装。"
            ),
        )
    if not windows.supports_build:
        return SandboxSupport(
            supported=False,
            code="build_too_old",
            message=(
                f"系统版本过低（build {windows.build or '未知'}），"
                f"Windows Sandbox 需要 build {MIN_BUILD}（Windows 10 1903）或更高。"
            ),
            alternative="先升级 Windows 到 1903 或更高版本，再回到控制台启用 Windows Sandbox。",
        )
    return SandboxSupport(
        supported=True,
        code="supported",
        message=f"{windows.display_name}（build {windows.build}）支持 Windows Sandbox。",
        alternative="",
    )


@dataclass(frozen=True)
class FeatureState:
    state: str
    source: str = "unavailable"
    detail: str = ""
    raw: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "source": self.source,
            "detail": self.detail,
            "raw": self.raw,
        }


def _normalize_state(value: Any) -> str:
    text = str(value or "").strip().lower()
    mapping = {
        "enabled": STATE_ENABLED,
        "enable": STATE_ENABLED,
        "on": STATE_ENABLED,
        "installed": STATE_ENABLED,
        "disabled": STATE_DISABLED,
        "disable": STATE_DISABLED,
        "off": STATE_DISABLED,
        "disabledwithpayloadremoved": STATE_DISABLED,
        "absent": STATE_ABSENT,
        "notpresent": STATE_ABSENT,
        "removed": STATE_ABSENT,
        "unknown": STATE_UNKNOWN,
        "enablepending": STATE_DISABLED,
        "": STATE_UNKNOWN,
    }
    return mapping.get(text, STATE_UNKNOWN)


def _parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    value = str(text or "").strip()
    if not value:
        return None
    start = value.find("{")
    end = value.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(value[start:end + 1])
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _extract_feature_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    feature = payload.get("feature")
    return feature if isinstance(feature, dict) else payload


def _default_probe_runner(script: Optional[Path], arguments: Sequence[str],
                          timeout: int) -> ScriptResult:
    if script is not None:
        return _run_powershell_script(script, arguments, timeout=timeout)
    return _run_powershell_command(_inline_probe_command(str(arguments[0]) if arguments else FEATURE_NAME),
                                   timeout=timeout)


def detect_feature_state(*, timeout: int = DETECT_TIMEOUT, probe_runner=None,
                         script: Optional[Path] = None,
                         feature: str = FEATURE_NAME) -> FeatureState:
    """查可选功能状态；不假定管理员权限，查不到就是 Unknown。"""
    resolved = script if script is not None else sandbox_script(PROBE_SCRIPT)
    arguments = ["-Feature", feature] if resolved is not None else [feature]
    runner = probe_runner or _default_probe_runner
    result = runner(resolved, arguments, timeout)
    if result.timed_out:
        return FeatureState(STATE_UNKNOWN, "timeout",
                            f"可选功能探测超时（{timeout}s）：{_tail(result.error, 200)}")
    if result.error and not result.stdout.strip():
        source = "no_powershell" if "PowerShell" in result.error else "error"
        return FeatureState(STATE_UNKNOWN, source, _tail(result.error, 300))
    payload = _parse_json_object(result.stdout)
    if payload is None:
        detail = _tail(result.stderr or result.stdout, 300) or "探测脚本没有输出可解析的 JSON。"
        return FeatureState(STATE_UNKNOWN, "parse_error", detail)
    body = _extract_feature_payload(payload)
    raw = str(body.get("state") or body.get("State") or "")
    state = _normalize_state(raw)
    source = str(body.get("state_source") or body.get("source") or "optional-feature")
    detail = str(body.get("detail") or "")
    if state == STATE_UNKNOWN and not detail:
        detail = f"探测结果里的状态无法识别：{raw or '(空)'}"
    return FeatureState(state=state, source=source, detail=detail, raw=raw)


@dataclass(frozen=True)
class SandboxDetection:
    windows: WindowsReport
    support: SandboxSupport
    feature: FeatureState
    exe_path: str
    exe_present: bool
    ready: bool
    warnings: List[str] = field(default_factory=list)
    checked_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "checked_at": self.checked_at,
            "windows": self.windows.to_dict(),
            "support": self.support.to_dict(),
            "feature": self.feature.to_dict(),
            "exe": {"path": self.exe_path, "present": self.exe_present},
            "ready": self.ready,
            "warnings": list(self.warnings),
        }


def _sandbox_exe_path() -> Path:
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    return Path(system_root) / "System32" / SANDBOX_EXE_NAME


def _exists(path: Any) -> bool:
    try:
        return Path(path).exists()
    except OSError:
        return False


def detect(*, registry: Optional[Dict[str, str]] = None,
           feature: Optional[FeatureState] = None,
           exe_checker: Optional[Callable[[Any], bool]] = None,
           probe_runner=None,
           platform_module: Any = None,
           timeout: int = DETECT_TIMEOUT) -> SandboxDetection:
    windows = detect_windows(registry=registry, platform_module=platform_module)
    exe_path = _sandbox_exe_path()
    exe_present = bool((exe_checker or _exists)(exe_path))
    warnings: List[str] = []

    if not windows.is_windows:
        support = assess_support(windows)
        state = feature or FeatureState(STATE_UNKNOWN, "unavailable", "非 Windows 系统，跳过探测")
        return SandboxDetection(
            windows=windows, support=support, feature=state,
            exe_path=str(exe_path), exe_present=exe_present, ready=False,
            warnings=["当前系统不是 Windows，Windows Sandbox 不可用。"],
            checked_at=_now_iso(),
        )

    support = assess_support(windows)
    state = feature if feature is not None else detect_feature_state(
        timeout=timeout, probe_runner=probe_runner)
    if state.state == STATE_UNKNOWN:
        warnings.append(
            "无法确认可选功能 Containers-DisposableClientVM 的状态："
            + (state.detail or "探测没有返回结果")
            + "（可能需要管理员权限；启用流程仍可尝试，DISM 是幂等的）"
        )
    if state.state == STATE_ENABLED and not exe_present:
        warnings.append(f"可选功能显示已启用，但没找到 {exe_path}：请确认系统文件与功能安装是否完整。")
    if state.state in (STATE_DISABLED, STATE_ABSENT) and exe_present:
        warnings.append(f"探测到 {exe_path} 存在，但可选功能状态是 {state.state}：状态可能已过期，建议提权复核。")
    ready = bool(support.supported and state.state == STATE_ENABLED and exe_present)
    return SandboxDetection(
        windows=windows, support=support, feature=state,
        exe_path=str(exe_path), exe_present=exe_present, ready=ready,
        warnings=warnings, checked_at=_now_iso(),
    )


# --------------------------------------------------------------------------- #
# 启用 / 回滚
# --------------------------------------------------------------------------- #
def enable_command(feature: str = FEATURE_NAME) -> str:
    return "dism.exe " + " ".join(_dism_arguments("enable", feature))


def disable_command(feature: str = FEATURE_NAME) -> str:
    return "dism.exe " + " ".join(_dism_arguments("disable", feature))


def enable_powershell_command(feature: str = FEATURE_NAME) -> str:
    return f"Enable-WindowsOptionalFeature -Online -FeatureName {feature} -All -NoRestart"


def disable_powershell_command(feature: str = FEATURE_NAME) -> str:
    return f"Disable-WindowsOptionalFeature -Online -FeatureName {feature} -NoRestart"


def manual_instructions(action: str = "enable", feature: str = FEATURE_NAME) -> List[str]:
    if action == "disable":
        return [
            "以管理员身份打开 PowerShell。",
            f"执行：{disable_command(feature)}",
            f"或执行：{disable_powershell_command(feature)}",
            "看到“需要重新启动”后自己挑时间重启；本模块不会重启，也不会删除沙盒产物。",
            "如需清理隔离通道：删除 data/sandbox/<通道名> 目录与宿主映射目录里的就绪标记。",
        ]
    return [
        "以管理员身份打开 PowerShell。",
        f"执行：{enable_command(feature)}",
        f"或执行：{enable_powershell_command(feature)}",
        "命令返回成功后重启系统，WindowsSandbox.exe 才会出现。",
    ]


@dataclass(frozen=True)
class FeatureActionResult:
    action: str
    status: str
    ok: bool
    needs_reboot: bool
    reason: str
    detail: str
    command: str
    powershell_command: str
    exit_code: Optional[int] = None
    notes: List[str] = field(default_factory=list)
    detection: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "status": self.status,
            "ok": self.ok,
            "needs_reboot": self.needs_reboot,
            "reason": self.reason,
            "detail": self.detail,
            "command": self.command,
            "powershell_command": self.powershell_command,
            "exit_code": self.exit_code,
            "notes": list(self.notes),
            "detection": dict(self.detection),
        }


def _elevation_failure_reason(code: Optional[int]) -> Optional[str]:
    if code in (EXIT_UAC_CANCELLED,):
        return "uac_cancelled"
    if code in (EXIT_ELEVATION_REQUIRED, EXIT_ACCESS_DENIED, EXIT_ACCESS_DENIED_HR):
        return "elevation_required"
    if code in (EXIT_SOURCE_FILES_MISSING, EXIT_SOURCE_FILES_NOT_FOUND,
                EXIT_SOURCE_FILES_NOT_FOUND_ALT):
        return "source_files_missing"
    if code == EXIT_FEATURE_ABSENT:
        return "feature_absent"
    return None


def _outcome(action: str, result: ScriptResult) -> tuple:
    """把提权子进程的结果折成 (status, ok, needs_reboot, reason)。"""
    code = _normalize_exit(result.returncode)
    if result.timed_out:
        return ("failed", False, False, "timeout")
    if result.error and code is None:
        return ("failed", False, False, "powershell_unavailable")
    if code == EXIT_UAC_CANCELLED:
        return ("needs_elevation", False, False, "uac_cancelled")
    reason = _elevation_failure_reason(code)
    if reason in ("elevation_required", "source_files_missing", "feature_absent"):
        return ("failed", False, False, reason)
    if code == EXIT_OK:
        # DISM 返回 0 表示命令成功；功能真正生效前仍要重启。
        status = "enabled" if action == "enable" else "disabled"
        return (status, True, True, "")
    if code == EXIT_REBOOT_REQUIRED:
        return ("pending_reboot", True, True, "")
    return ("failed", False, False, f"exit_{code}")


def _default_elevated_runner(action: str, script: Optional[Path],
                             arguments: Sequence[str], timeout: int) -> ScriptResult:
    if script is not None:
        return _run_elevated_script(script, arguments, timeout=timeout)
    return _run_elevated_command(_inline_dism_elevated(action), timeout=timeout)


def _action_arguments(feature: str = FEATURE_NAME) -> List[str]:
    return ["-Feature", feature, "-Mode", "DISM"]


def enable_sandbox(*, apply: bool = True,
                   detection: Optional[SandboxDetection] = None,
                   timeout: int = ENABLE_TIMEOUT,
                   elevated_runner=None,
                   script: Optional[Path] = None,
                   feature: str = FEATURE_NAME) -> FeatureActionResult:
    """启用 Windows Sandbox。已启用时幂等返回，不需要管理员；未启用时走 UAC 提权。"""
    det = detection if detection is not None else detect(timeout=DETECT_TIMEOUT)
    command = enable_command(feature)
    ps_command = enable_powershell_command(feature)
    notes = [NO_REBOOT_NOTE]

    def _result(status: str, ok: bool, needs_reboot: bool, reason: str, detail: str,
                exit_code: Optional[int] = None) -> FeatureActionResult:
        return FeatureActionResult(
            action="enable", status=status, ok=ok, needs_reboot=needs_reboot,
            reason=reason, detail=detail, command=command, powershell_command=ps_command,
            exit_code=exit_code, notes=list(notes), detection=det.to_dict(),
        )

    if not det.support.supported:
        notes.append(manual_instructions("enable", feature)[0])
        return _result("unsupported", False, False, det.support.code,
                       det.support.message + (" " + det.support.alternative if det.support.alternative else ""))
    if det.feature.state == STATE_ENABLED:
        return _result("already_enabled", True, False, "", "可选功能已是 Enabled，无需重复启用。")
    if not apply:
        notes.append("dry-run：只生成命令，没有提权、没有调用 DISM。")
        return _result("planned", True, False, "", "未执行启用，仅返回计划与命令。")

    resolved = script if script is not None else sandbox_script(ENABLE_SCRIPT)
    runner = elevated_runner or (
        lambda target, args, wait: _default_elevated_runner("enable", target, args, wait))
    arguments = _action_arguments(feature)
    result = runner(resolved, arguments, timeout)
    status, ok, needs_reboot, reason = _outcome("enable", result)
    detail = _tail(result.stderr or result.stdout or result.error, 600)
    if det.feature.state == STATE_UNKNOWN:
        notes.append("探测时无法确认可选功能状态，已按幂等方式尝试启用。")
    if not ok and not detail:
        detail = "提权进程没有留下输出，请按下方命令手动执行确认原因。"
    if not ok:
        notes.extend(manual_instructions("enable", feature)[1:])
    return _result(status, ok, needs_reboot, reason, detail,
                   _normalize_exit(result.returncode))


def disable_sandbox(*, apply: bool = False,
                    detection: Optional[SandboxDetection] = None,
                    timeout: int = ENABLE_TIMEOUT,
                    elevated_runner=None,
                    script: Optional[Path] = None,
                    feature: str = FEATURE_NAME) -> FeatureActionResult:
    """回滚：默认只给禁用命令与人工步骤，apply=True 才提权执行；从不重启。"""
    det = detection if detection is not None else detect(timeout=DETECT_TIMEOUT)
    command = disable_command(feature)
    ps_command = disable_powershell_command(feature)
    notes = [NO_REBOOT_NOTE] + manual_instructions("disable", feature)

    def _result(status: str, ok: bool, needs_reboot: bool, reason: str, detail: str,
                exit_code: Optional[int] = None) -> FeatureActionResult:
        return FeatureActionResult(
            action="disable", status=status, ok=ok, needs_reboot=needs_reboot,
            reason=reason, detail=detail, command=command, powershell_command=ps_command,
            exit_code=exit_code, notes=list(notes), detection=det.to_dict(),
        )

    if not det.support.supported:
        return _result("unsupported", False, False, det.support.code,
                       det.support.message + (" " + det.support.alternative if det.support.alternative else ""))
    if det.feature.state in (STATE_DISABLED, STATE_ABSENT):
        return _result("already_disabled", True, False, "",
                       f"可选功能状态为 {det.feature.state}，不需要再禁用。")
    if not apply:
        return _result("planned", True, False, "", "未执行禁用，仅返回命令与人工步骤。")

    resolved = script if script is not None else sandbox_script(DISABLE_SCRIPT)
    runner = elevated_runner or (
        lambda target, args, wait: _default_elevated_runner("disable", target, args, wait))
    arguments = _action_arguments(feature)
    result = runner(resolved, arguments, timeout)
    status, ok, needs_reboot, reason = _outcome("disable", result)
    detail = _tail(result.stderr or result.stdout or result.error, 600)
    if not ok and not detail:
        detail = "提权进程没有留下输出，请按下方命令手动执行确认原因。"
    return _result(status, ok, needs_reboot, reason, detail, _normalize_exit(result.returncode))


def ensure_ready(*, apply: bool = True,
                 detection: Optional[SandboxDetection] = None,
                 timeout: int = ENABLE_TIMEOUT,
                 elevated_runner=None) -> Dict[str, Any]:
    """控制台用的一步入口：检测 → 需要时启用 → 汇报下一步。不创建 .wsb。"""
    det = detection if detection is not None else detect(timeout=DETECT_TIMEOUT)
    payload: Dict[str, Any] = {"detection": det.to_dict(), "ready": det.ready}
    if not det.support.supported:
        payload.update({"status": "unsupported", "ok": False, "reason": det.support.code,
                        "detail": det.support.message, "alternative": det.support.alternative})
        return payload
    if det.ready:
        payload.update({"status": "ready", "ok": True, "reason": "",
                        "detail": "Windows Sandbox 已启用，可直接创建隔离通道。"})
        return payload
    action = enable_sandbox(apply=apply, detection=det, timeout=timeout,
                            elevated_runner=elevated_runner)
    status = "ready" if det.feature.state == STATE_ENABLED else action.status
    payload.update({"status": status, "ok": action.ok, "reason": action.reason,
                    "detail": action.detail, "enable": action.to_dict()})
    return payload


# --------------------------------------------------------------------------- #
# 隔离通道（.wsb）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SandboxConfig:
    host_folder: str
    sandbox_folder: str = DEFAULT_SANDBOX_FOLDER
    networking: bool = False
    clipboard: bool = False
    read_only: bool = False
    memory_mb: int = 0
    logon_command: str = ""
    ready_marker: str = DEFAULT_READY_MARKER

    def to_dict(self) -> Dict[str, Any]:
        return {
            "host_folder": self.host_folder,
            "sandbox_folder": self.sandbox_folder,
            "networking": bool(self.networking),
            "clipboard": bool(self.clipboard),
            "read_only": bool(self.read_only),
            "memory_mb": int(self.memory_mb),
            "logon_command": self.logon_command,
            "ready_marker": self.ready_marker,
        }

    @property
    def ready_marker_path(self) -> str:
        return str(Path(self.host_folder) / self.ready_marker)


def default_config(host_folder: Any, **overrides: Any) -> SandboxConfig:
    return SandboxConfig(host_folder=str(host_folder), **overrides)


def _normalize_config(config: SandboxConfig) -> SandboxConfig:
    host = str(config.host_folder or "").strip()
    if not host:
        raise ValueError("宿主工作目录不能为空")
    host_abs = os.path.abspath(os.path.expanduser(host))
    sandbox_folder = str(config.sandbox_folder or "").strip()
    if not sandbox_folder:
        raise ValueError("沙盒内目录不能为空")
    if not (len(sandbox_folder) >= 3 and sandbox_folder[1] == ":" and sandbox_folder[2] in "\\/") \
            and not sandbox_folder.startswith("\\\\"):
        raise ValueError(f"沙盒内目录必须是 Windows 绝对路径（如 C:\\WhaleGirl）：{sandbox_folder}")
    for tail_name, tail_value in (("ready_marker", config.ready_marker),):
        if not str(tail_value or "").strip():
            raise ValueError(f"{tail_name} 不能为空")
        if any(sep in str(tail_value) for sep in ("/", "\\")):
            raise ValueError(f"{tail_name} 只能是文件名，不能带路径分隔符：{tail_value}")
    memory = max(0, _to_int(config.memory_mb))
    return SandboxConfig(
        host_folder=host_abs,
        sandbox_folder=sandbox_folder.rstrip("\\/") or sandbox_folder,
        networking=bool(config.networking),
        clipboard=bool(config.clipboard),
        read_only=bool(config.read_only),
        memory_mb=memory,
        logon_command=str(config.logon_command or ""),
        ready_marker=str(config.ready_marker),
    )


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def default_logon_command(config: SandboxConfig) -> str:
    sandbox = str(config.sandbox_folder).rstrip("\\/")
    marker = f"{sandbox}\\{config.ready_marker}"
    return f"cmd.exe /c echo WhaleGirl sandbox ready>{marker}"


def build_wsb(config: SandboxConfig) -> str:
    """生成 .wsb 文本。宿主路径与 LogonCommand 全部做 XML 转义。"""
    cfg = _normalize_config(config)
    esc = _xml_escape
    command = cfg.logon_command.strip() or default_logon_command(cfg)
    lines = [
        "<!-- WhaleGirl Windows Sandbox 配置（自动生成，勿手改） -->",
        "<Configuration>",
        "  <MappedFolders>",
        "    <MappedFolder>",
        f"      <HostFolder>{esc(cfg.host_folder)}</HostFolder>",
        f"      <SandboxFolder>{esc(cfg.sandbox_folder)}</SandboxFolder>",
        f"      <ReadOnly>{_bool_text(cfg.read_only)}</ReadOnly>",
        "    </MappedFolder>",
        "  </MappedFolders>",
        f"  <Networking>{'Enable' if cfg.networking else 'Disable'}</Networking>",
        f"  <ClipboardRedirection>{'Enable' if cfg.clipboard else 'Disable'}</ClipboardRedirection>",
    ]
    if cfg.memory_mb > 0:
        lines.append(f"  <MemoryInMB>{cfg.memory_mb}</MemoryInMB>")
    lines.extend([
        "  <LogonCommand>",
        f"    <Command>{esc(command)}</Command>",
        "  </LogonCommand>",
        "</Configuration>",
        "",
    ])
    return "\n".join(lines)


def channel_dir(root: Any = None, name: str = DEFAULT_CHANNEL_NAME) -> Path:
    safe_name = str(name or DEFAULT_CHANNEL_NAME).strip() or DEFAULT_CHANNEL_NAME
    if any(sep in safe_name for sep in ("/", "\\", "..")):
        raise ValueError(f"通道名不能包含路径分隔符：{name}")
    base = Path(root) if root else Path(_data_root()) / "data" / CHANNEL_FOLDER_NAME
    return base / safe_name


def _data_root() -> Path:
    try:
        from .data_root import resolve_data_root
    except ImportError:  # pragma: no cover - 只在包外直接跑脚本时出现
        return Path(__file__).resolve().parents[1]
    return resolve_data_root()


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _read_json(path: Path) -> Dict[str, Any]:
    payload = _parse_json_object(_read_text(path))
    return payload or {}


@dataclass(frozen=True)
class LaunchResult:
    status: str
    ok: bool
    command: str
    detail: str = ""
    pid: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "command": self.command,
            "detail": self.detail,
            "pid": self.pid,
        }


def _spawn_detached(exe: str, wsb_path: str) -> Optional[int]:
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen([exe, str(wsb_path)], close_fds=True, creationflags=flags)
    return getattr(process, "pid", None)


def launch_sandbox(wsb_path: Any, *, apply: bool = True, launcher=None,
                   exe_path: Optional[Any] = None) -> LaunchResult:
    """启动沙盒进程。apply=False 只回命令，方便 UI 先确认。"""
    target = Path(str(wsb_path))
    exe = Path(str(exe_path)) if exe_path else _sandbox_exe_path()
    command = f'"{exe}" "{target}"'
    if not target.is_file():
        return LaunchResult("missing_config", False, command,
                            f"找不到 .wsb 配置文件：{target}")
    if not _exists(exe):
        return LaunchResult("unavailable", False, command,
                            f"找不到 {exe}：Windows Sandbox 可能未启用或未安装。")
    if not apply:
        return LaunchResult("planned", True, command, "未启动，仅返回启动命令。")
    try:
        pid = (launcher or _spawn_detached)(str(exe), str(target))
    except OSError as exc:
        return LaunchResult("failed", False, command, f"启动失败：{exc}")
    return LaunchResult("launched", True, command, "已请求启动 Windows Sandbox。",
                        int(pid) if pid else None)


@dataclass(frozen=True)
class IsolationChannel:
    name: str
    wsb_path: str
    manifest_path: str
    host_folder: str
    sandbox_folder: str
    ready_marker_path: str
    changed: bool
    created: bool
    content_sha256: str
    launched: Optional[LaunchResult] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "wsb_path": self.wsb_path,
            "manifest_path": self.manifest_path,
            "host_folder": self.host_folder,
            "sandbox_folder": self.sandbox_folder,
            "ready_marker_path": self.ready_marker_path,
            "changed": self.changed,
            "created": self.created,
            "content_sha256": self.content_sha256,
            "launched": self.launched.to_dict() if self.launched else None,
            "notes": list(self.notes),
        }


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_MANIFEST_STABLE_FIELDS = (
    "name",
    "wsb_path",
    "host_folder",
    "sandbox_folder",
    "networking",
    "clipboard",
    "read_only",
    "memory_mb",
    "ready_marker",
    "ready_marker_path",
    "logon_command",
    "content_sha256",
)


def create_isolation_channel(config: SandboxConfig, *, name: str = DEFAULT_CHANNEL_NAME,
                             root: Any = None, launch: bool = False, apply: bool = True,
                             launcher=None, launch_exe: Optional[Any] = None) -> IsolationChannel:
    """落地 .wsb 与 channel.json。内容没变就不重写（幂等），返回 changed 供 UI 显示。"""
    cfg = _normalize_config(config)
    folder = channel_dir(root, name)
    folder.mkdir(parents=True, exist_ok=True)
    host = Path(cfg.host_folder)
    notes: List[str] = []
    if not host.is_dir():
        host.mkdir(parents=True, exist_ok=True)
        notes.append(f"宿主目录原本不存在，已创建：{host}")

    wsb_path = folder / WSB_FILE_NAME
    manifest_path = folder / MANIFEST_FILE_NAME
    text = build_wsb(cfg)
    digest = _sha256(text)
    previous = _read_text(wsb_path) if wsb_path.is_file() else ""
    created = not wsb_path.is_file()
    changed = previous != text
    if changed:
        _write_text_atomic(wsb_path, text)

    now = _now_iso()
    previous_manifest = _read_json(manifest_path) if manifest_path.is_file() else {}
    desired: Dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "name": str(name),
        "wsb_path": str(wsb_path),
        "host_folder": cfg.host_folder,
        "sandbox_folder": cfg.sandbox_folder,
        "networking": cfg.networking,
        "clipboard": cfg.clipboard,
        "read_only": cfg.read_only,
        "memory_mb": cfg.memory_mb,
        "ready_marker": cfg.ready_marker,
        "ready_marker_path": cfg.ready_marker_path,
        "logon_command": cfg.logon_command or default_logon_command(cfg),
        "content_sha256": digest,
    }
    if any(previous_manifest.get(key) != desired.get(key) for key in _MANIFEST_STABLE_FIELDS):
        desired["created_at"] = previous_manifest.get("created_at") or now
        desired["updated_at"] = now
        _write_text_atomic(manifest_path, json.dumps(desired, ensure_ascii=False, indent=2) + "\n")
    else:
        notes.append("配置与上次一致，未重写文件。")

    launched = None
    if launch:
        launched = launch_sandbox(wsb_path, apply=apply, launcher=launcher, exe_path=launch_exe)
    return IsolationChannel(
        name=str(name),
        wsb_path=str(wsb_path),
        manifest_path=str(manifest_path),
        host_folder=cfg.host_folder,
        sandbox_folder=cfg.sandbox_folder,
        ready_marker_path=cfg.ready_marker_path,
        changed=bool(changed),
        created=bool(created),
        content_sha256=digest,
        launched=launched,
        notes=notes,
    )


@dataclass(frozen=True)
class ChannelStatus:
    name: str
    exists: bool
    wsb_path: str
    manifest_path: str
    host_folder: str
    sandbox_folder: str
    ready: bool
    ready_marker_path: str
    marker_text: str
    created_at: str
    updated_at: str
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "exists": self.exists,
            "wsb_path": self.wsb_path,
            "manifest_path": self.manifest_path,
            "host_folder": self.host_folder,
            "sandbox_folder": self.sandbox_folder,
            "ready": self.ready,
            "ready_marker_path": self.ready_marker_path,
            "marker_text": self.marker_text,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
        }


def _wsb_host_folder(wsb_path: Path) -> str:
    """manifest 丢了也能从 .wsb 里把宿主目录读回来。"""
    import xml.etree.ElementTree as ElementTree

    try:
        tree = ElementTree.parse(str(wsb_path))
    except (OSError, ElementTree.ParseError):
        return ""
    node = tree.getroot().find("./MappedFolders/MappedFolder/HostFolder")
    if node is None or not (node.text or "").strip():
        return ""
    return node.text.strip()


def read_channel_status(*, name: str = DEFAULT_CHANNEL_NAME, root: Any = None) -> ChannelStatus:
    folder = channel_dir(root, name)
    wsb_path = folder / WSB_FILE_NAME
    manifest_path = folder / MANIFEST_FILE_NAME
    manifest = _read_json(manifest_path) if manifest_path.is_file() else {}
    host_folder = str(manifest.get("host_folder") or "")
    error = ""
    if wsb_path.is_file() and not manifest:
        host_folder = _wsb_host_folder(wsb_path)
        error = "channel.json 缺失或无法解析，已从 .wsb 反推宿主目录。"
    marker_name = str(manifest.get("ready_marker") or DEFAULT_READY_MARKER)
    marker_path = str(Path(host_folder) / marker_name) if host_folder else ""
    marker_text = _read_text(Path(marker_path)).strip() if marker_path and Path(marker_path).is_file() else ""
    return ChannelStatus(
        name=str(name),
        exists=wsb_path.is_file(),
        wsb_path=str(wsb_path),
        manifest_path=str(manifest_path),
        host_folder=host_folder,
        sandbox_folder=str(manifest.get("sandbox_folder") or ""),
        ready=bool(marker_text),
        ready_marker_path=marker_path,
        marker_text=marker_text,
        created_at=str(manifest.get("created_at") or ""),
        updated_at=str(manifest.get("updated_at") or ""),
        error=error,
    )
