"""管理员模式：在 QQ 里用密码进入，进入后可以直接执行管理操作。

和原来的"远程变更必须本地审核"相比，这里是**密码认证后的直通**，所以安全边界要写清楚：

- 密码只保存 PBKDF2-HMAC-SHA256 的哈希 + 随机盐（配置里的 ``qq.admin_password_hash`` /
  ``qq.admin_password_salt``），**从不保存明文**，也不写日志、不回显到 QQ；
- 会话、失败次数、锁定都只在内存里：**重启程序即全部失效**（和"暂停只对本次运行有效"
  同一口径），不会留下一个关不掉的开关；
- 连续失败 ``MAX_FAILURES`` 次锁定 ``LOCK_SECONDS`` 秒；没设密码时管理员模式不可用，
  远程变更仍旧走本地审核流程（不降级安全性）。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

ITERATIONS = 120_000
DEFAULT_MINUTES = 30
MAX_FAILURES = 3
LOCK_SECONDS = 600
MIN_PASSWORD_LEN = 6


def hash_password(password: str, salt: str = "") -> tuple[str, str]:
    """返回 (哈希, 盐)。盐留空则随机生成。"""
    salt = salt or os.urandom(16).hex()
    try:
        salt_bytes = bytes.fromhex(salt)
    except ValueError:
        salt_bytes = os.urandom(16)
        salt = salt_bytes.hex()
    digest = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt_bytes, ITERATIONS)
    return digest.hex(), salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    """常量时间比较；空密码/空哈希一律不通过。"""
    if not str(password or "") or not stored_hash or not salt:
        return False
    digest, _ = hash_password(password, salt)
    return hmac.compare_digest(digest, str(stored_hash))


def password_error(password: str) -> str:
    """设置密码前的校验，返回空串表示可用。"""
    value = str(password or "")
    if len(value) < MIN_PASSWORD_LEN:
        return f"管理员密码至少 {MIN_PASSWORD_LEN} 位"
    if len(value) > 64:
        return "管理员密码最长 64 位"
    if value.strip() != value:
        return "管理员密码首尾不能有空格"
    return ""


class AdminSessions:
    """管理员会话：进入/退出、等待输入密码、失败锁定。时钟可注入，便于测试。"""

    def __init__(self, minutes: int = DEFAULT_MINUTES, clock=time.time):
        self.minutes = max(1, int(minutes or DEFAULT_MINUTES))
        self._clock = clock
        self._sessions: dict[str, float] = {}
        self._pending: set[str] = set()
        self._failures: dict[str, int] = {}
        self._locked: dict[str, float] = {}

    # ── 会话 ──────────────────────────────────────────────────────────────────
    def active(self, who) -> bool:
        expiry = self._sessions.get(str(who))
        if expiry is None:
            return False
        if expiry <= self._clock():
            self._sessions.pop(str(who), None)
            return False
        return True

    def start(self, who) -> None:
        self._sessions[str(who)] = self._clock() + self.minutes * 60
        self._pending.discard(str(who))
        self._failures.pop(str(who), None)

    def end(self, who) -> None:
        self._sessions.pop(str(who), None)
        self._pending.discard(str(who))

    def seconds_left(self, who) -> int:
        if not self.active(who):
            return 0
        return max(0, int(self._sessions[str(who)] - self._clock()))

    def end_all(self) -> int:
        """清掉所有会话（改/清密码时用）。返回被清掉的会话数。"""
        count = len(self._sessions)
        self._sessions.clear()
        self._pending.clear()
        self._failures.clear()
        self._locked.clear()
        return count

    def count(self) -> int:
        return sum(1 for who in list(self._sessions) if self.active(who))

    # ── 输入密码流程 ──────────────────────────────────────────────────────────
    def begin(self, who) -> None:
        self._pending.add(str(who))

    def awaiting(self, who) -> bool:
        return str(who) in self._pending

    def cancel(self, who) -> None:
        self._pending.discard(str(who))

    def locked_for(self, who) -> int:
        until = self._locked.get(str(who))
        if until is None:
            return 0
        left = int(until - self._clock())
        if left <= 0:
            self._locked.pop(str(who), None)
            self._failures.pop(str(who), None)
            return 0
        return left

    def attempt(self, who, password, stored_hash, salt) -> str:
        """校验一次密码，返回 ``ok`` / ``wrong`` / ``locked``。"""
        who = str(who)
        if self.locked_for(who):
            return "locked"
        if verify_password(password, stored_hash, salt):
            self.start(who)
            return "ok"
        count = self._failures.get(who, 0) + 1
        self._failures[who] = count
        if count >= MAX_FAILURES:
            self._locked[who] = self._clock() + LOCK_SECONDS
            self._failures.pop(who, None)
            self._pending.discard(who)
            return "locked"
        return "wrong"

    def attempts_left(self, who) -> int:
        return max(0, MAX_FAILURES - self._failures.get(str(who), 0))


ENTRY_WORDS = ("进入管理员模式", "管理员模式", "admin", "/admin", "进入管理模式")
EXIT_WORDS = ("退出管理员模式", "退出管理员", "退出管理模式", "结束管理员模式")


def entry_request(text) -> bool:
    return str(text or "").strip().lower() in {w.lower() for w in ENTRY_WORDS}


def exit_request(text) -> bool:
    return str(text or "").strip().lower() in {w.lower() for w in EXIT_WORDS}


ADMIN_HELP = (
    "管理员模式命令：\n"
    "状态 / 连接 / 队列 / 日志 / 用量\n"
    "回复记录 [条数]：最近谁发了什么、机器人回了什么\n"
    "人设 [名字] / 切换人设 [名字]：查看或切换人设（人格/角色同义）\n"
    "插件 [开|关 插件标识]：查看或开关插件\n"
    "市场 [关键词]：查看插件市场里跟机器人相关的插件（只展示，不自动安装）\n"
    "暂停 / 恢复 / 重连 / 重试失败\n"
    "退出管理员模式\n"
    "（管理指令只在本会话内生效，且只管理机器人本身，不操作这台电脑。）"
)
