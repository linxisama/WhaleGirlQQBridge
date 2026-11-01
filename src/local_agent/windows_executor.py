"""T16：Windows L0/L1 执行器（独立进程，当前登录用户权限，**不**要求管理员）。

能力（首版只做 L0/L1，1.7.3）：
- L0：在线状态、已授权能力列表（只读，不返回全盘/进程命令行/系统秘密）。
- L1：打开白名单应用（app_id 映射本机审核后路径）、设置音量、暂停媒体。

执行边界（1.7.4，T16 验收）：
- 动作前核对进程/窗口/焦点；窗口变化就停，不盲打。
- 锁屏 / UAC 安全桌面 / 目标不在交互会话 → 返回“不可执行”，不自动提权。
- 目录授权要解析最终路径，拒绝越界/UNC/ junction/symlink 绕过；用前再确认。
- 应用参数按白名单结构传入，**绝不**执行来自 QQ 的任意命令字符串。
- 截图/剪贴板/文件正文默认留本机；对外回传须有独立允许范围。
- 优先 Windows API / 应用 API / UIA；UIA 失败才评估 pywinauto；坐标点击最后手段。

实现补充（2026-10-20）：默认什么都不放行（APP_WHITELIST 为空），由核心回填
“app_id → 已审核路径/参数键”；系统音量/媒体控制通过可注入后端调用，便于在
无音频设备的 CI 中验证拒绝/核对逻辑，而不是假装成功。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable, Optional

from ..audio_contracts import AgentJobState, ErrorCode, reject
from .contracts import ActionProposal

# app_id → 本机审核后的可执行路径（固定映射，不是任意路径；T16 由核心回填）。
# 值可以是路径字符串，或 {'path': 路径, 'arg_keys': ('path',)} 的结构。
APP_WHITELIST: dict[str, object] = {
    # 'notepad': r'C:\Windows\notepad.exe',
}

_APP_ID = re.compile(r'^[A-Za-z0-9_.-]{1,32}$')
_SHELL_META = set(';&|<>$`\n\r\t')
_CONTROL = re.compile(r'[\x00-\x1f\x7f]')
_MAX_ARGS = 10
_MAX_ARG_CHARS = 200


class WindowsExecutor:
    """L0/L1 动作的本地执行器。每个方法都先过 policy.authorize（同一策略执行器）。"""

    def __init__(self, policy, store=None, *, app_whitelist: Optional[dict] = None,
                 session_probe: Optional[Callable[[], dict]] = None,
                 app_launcher: Optional[Callable] = None,
                 volume_backend=None, media_backend=None):
        self.policy = policy
        self.store = store
        self.app_whitelist = dict(APP_WHITELIST if app_whitelist is None else app_whitelist)
        self.session_probe = session_probe or (lambda: {'locked': False, 'interactive': True,
                                                        'foreground': ''})
        self.app_launcher = app_launcher
        self.volume_backend = volume_backend
        self.media_backend = media_backend

    # ── 会话核对 ────────────────────────────────────────────────────────────
    def session_state(self) -> dict:
        try:
            state = self.session_probe() or {}
        except Exception:
            return {'locked': True, 'interactive': False, 'foreground': ''}
        return {'locked': bool(state.get('locked', False)),
                'interactive': bool(state.get('interactive', True)),
                'foreground': str(state.get('foreground') or '')}

    def _require_interactive(self) -> dict:
        state = self.session_state()
        if state['locked']:
            reject(ErrorCode.POLICY_DENIED, '当前会话已锁屏，动作不可执行（不自动提权）')
        if not state['interactive']:
            reject(ErrorCode.POLICY_DENIED, '当前不在交互会话（UAC/远程），动作不可执行')
        return state

    # ── L0 ──────────────────────────────────────────────────────────────────
    def health(self, device_id: str) -> dict:
        """L0：在线状态。不返回系统秘密。"""
        state = self.session_state()
        return {'device_id': str(device_id), 'online': state['interactive'] and not state['locked'],
                'locked': state['locked'], 'interactive': state['interactive'], 'level': 0}

    # ── L1 ──────────────────────────────────────────────────────────────────
    def open_app(self, proposal: ActionProposal, app_id: str,
                 args: Optional[dict] = None) -> AgentJobState:
        """L1：打开白名单应用。app_id 必须命中 APP_WHITELIST；参数按白名单结构传入。"""
        self.policy.authorize(proposal)
        self._require_interactive()
        app_id = str(app_id or '')
        if not _APP_ID.fullmatch(app_id):
            reject(ErrorCode.POLICY_DENIED, '应用标识格式不合法（可能试图注入命令）')
        if app_id not in self.app_whitelist:
            reject(ErrorCode.POLICY_DENIED, f'应用未登记白名单：{app_id}')
        entry = self.app_whitelist[app_id]
        if isinstance(entry, dict):
            path = str(entry.get('path') or '')
            arg_keys = tuple(entry.get('arg_keys') or ())
        else:
            path, arg_keys = str(entry), ()
        path = self._validate_path(path)
        argv = self._argv(args or {}, arg_keys)
        process = self._launch(path, argv)
        if process is not None:
            code = process.poll()
            if code is not None and code != 0:
                self._audit(proposal.job_id, f'open_app exit {code}')
                return AgentJobState.FAILED
        self._audit(proposal.job_id, 'open_app ' + app_id)
        return AgentJobState.SUCCEEDED

    def set_volume(self, proposal: ActionProposal, level: int) -> AgentJobState:
        """L1：设置系统音量。level 0–100。"""
        self.policy.authorize(proposal)
        self._require_interactive()
        try:
            target = int(level)
        except (TypeError, ValueError):
            reject(ErrorCode.INVALID_INPUT, '音量须为 0–100 的整数')
        if not 0 <= target <= 100:
            reject(ErrorCode.INVALID_INPUT, '音量须为 0–100')
        if self.volume_backend is None:
            reject(ErrorCode.CAPABILITY_UNSUPPORTED, '本机音量后端未配置')
        self.volume_backend.set(target)
        observed = getattr(self.volume_backend, 'current', lambda: target)()
        self._audit(proposal.job_id, f'volume {target}')
        try:
            observed_value = int(observed)
        except (TypeError, ValueError):
            return AgentJobState.UNCERTAIN
        return (AgentJobState.SUCCEEDED if abs(observed_value - target) <= 3
                else AgentJobState.UNCERTAIN)

    def pause_media(self, proposal: ActionProposal) -> AgentJobState:
        """L1：暂停当前媒体。结果要“检查真实状态”（1.7.3 回传边界）。"""
        self.policy.authorize(proposal)
        self._require_interactive()
        if self.media_backend is None:
            reject(ErrorCode.CAPABILITY_UNSUPPORTED, '本机媒体控制后端未配置')
        self.media_backend.pause()
        self._audit(proposal.job_id, 'pause_media')
        playing = getattr(self.media_backend, 'is_playing', None)
        if callable(playing):
            try:
                return AgentJobState.FAILED if playing() else AgentJobState.SUCCEEDED
            except Exception:
                return AgentJobState.UNCERTAIN
        return AgentJobState.UNCERTAIN

    # ── 解绑/急停 ───────────────────────────────────────────────────────────
    def revoke(self, device_id: str) -> int:
        """本机解绑/急停（1.7.4）。急停在“下一动作前”生效，清空未执行授权。

        已完成的点击/外发**无法**保证撤销；取消/断线后不自动重放执行中动作，
        标 uncertain 并核对真实状态。
        """
        count = self.policy.revoke(str(device_id) if device_id else None)
        self._audit('revoke', 'device ' + str(device_id or 'all'))
        return count

    # ── 内部 ────────────────────────────────────────────────────────────────
    @staticmethod
    def _validate_path(path: str) -> str:
        if not path:
            reject(ErrorCode.POLICY_DENIED, '白名单应用缺少可执行路径')
        candidate = Path(path)
        if not candidate.is_absolute():
            reject(ErrorCode.POLICY_DENIED, '白名单应用路径必须是绝对路径')
        if str(candidate).startswith('\\\\'):
            reject(ErrorCode.POLICY_DENIED, '不允许 UNC 网络路径')
        if _CONTROL.search(str(candidate)):
            reject(ErrorCode.POLICY_DENIED, '应用路径包含控制字符')
        if not candidate.is_file():
            reject(ErrorCode.PROVIDER_UNAVAILABLE, '白名单应用不存在或不可执行',
                   app=path)
        return str(candidate)

    @staticmethod
    def _argv(args: dict, arg_keys: tuple) -> list[str]:
        """只接受白名单结构化参数；字符串参数仍拒绝 shell 元字符。"""
        if not isinstance(args, dict):
            reject(ErrorCode.INVALID_INPUT, '应用参数必须是对象')
        if args and not arg_keys:
            reject(ErrorCode.POLICY_DENIED, '该应用不接受参数（未登记参数白名单）')
        values: list[str] = []
        for key, raw in args.items():
            if key not in arg_keys:
                reject(ErrorCode.POLICY_DENIED, f'应用参数键未登记：{key}')
            items = raw if isinstance(raw, (list, tuple)) else [raw]
            if len(items) > _MAX_ARGS:
                reject(ErrorCode.INVALID_INPUT, '应用参数数量过多')
            for item in items:
                if isinstance(item, bool) or not isinstance(item, (str, int)):
                    reject(ErrorCode.INVALID_INPUT, '应用参数只接受字符串或整数')
                text = str(item)
                if len(text) > _MAX_ARG_CHARS or _CONTROL.search(text):
                    reject(ErrorCode.INVALID_INPUT, '应用参数过长或含控制字符')
                if _SHELL_META.intersection(text):
                    reject(ErrorCode.POLICY_DENIED,
                           '应用参数含 shell 元字符，拒绝执行')
                values.append(text)
        if len(values) > _MAX_ARGS:
            reject(ErrorCode.INVALID_INPUT, '应用参数数量过多')
        return values

    def _launch(self, path: str, argv: list[str]):
        if self.app_launcher is not None:
            return self.app_launcher(path, argv)
        try:
            return subprocess.Popen([path, *argv], shell=False,
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as exc:
            reject(ErrorCode.PROVIDER_UNAVAILABLE, '启动白名单应用失败',
                   error=type(exc).__name__)

    def _audit(self, job_id: str, event: str) -> None:
        try:
            self.policy.audit(self.store, job_id, event)
        except Exception:
            pass
