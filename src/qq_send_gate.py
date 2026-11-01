"""QQ 发送闸门：全局最小发送间隔 + 429/11253/11254 自动冷却（纯标准库，无新依赖）。

设计前提：本项目的 QQ 发送（官方 HTTP / OneBot WS / QQBridge WS）全部运行在
同一事件循环里，因此用 asyncio.Lock 串行化 acquire；时间一律用 time.monotonic()，
不依赖系统墙钟（改系统时间不会影响冷却与间隔判断）。

冷却规则：
- 触发码：429 / 11253 / 11254（int 或 str 均可）。
- 首次触发冷却 cooldown_base_s（默认 30s）；距上次触发 5 分钟内再次触发，冷却
  时长翻倍，封顶 cooldown_max_s（默认 120s）；距上次触发超过 5 分钟才复位计数。
- 冷却期内正常 code 不会清零冷却（只有到期自然结束，或超 5 分钟未再触发才复位）。
- acquire() 在冷却期内会一直等到冷却结束（官方/outbox 路径靠这个等待，消息留在
  队列不丢）；/api 直发路径在调用前自己检查 in_cooldown() 并拒绝。
"""
import asyncio
import logging
import time

log = logging.getLogger("qq.send_gate")

RATE_LIMIT_CODES = frozenset({429, 11253, 11254})
_RESET_AFTER_S = 300.0          # 距上次触发超过 5 分钟才把连续触发计数复位


class SendGate:
    """每个 QQRuntime 实例一个：三条发送路径共用同一把闸门。"""

    def __init__(self, min_gap_ms=800, cooldown_base_s=30, cooldown_max_s=120):
        try:
            gap = float(min_gap_ms)
        except (TypeError, ValueError):
            gap = 800.0
        if gap < 0:
            gap = 800.0
        self.min_gap_s = gap / 1000.0
        self.cooldown_base_s = float(cooldown_base_s)
        self.cooldown_max_s = float(cooldown_max_s)
        self.last_send_ts = None       # time.monotonic()，上一次发送开始时刻
        self.cooldown_until = None     # time.monotonic()，冷却截止时刻
        self.cooldown_count = 0        # 当前连续触发次数（决定冷却翻倍）
        self.consecutive_hits = 0      # 与 cooldown_count 同源，供状态输出
        self.last_trigger_ts = None    # 上次触发冷却的时刻
        self._lock = asyncio.Lock()

    @staticmethod
    def _code_of(status_or_code):
        if isinstance(status_or_code, bool):
            return None
        try:
            return int(status_or_code)
        except (TypeError, ValueError):
            try:
                return int(str(status_or_code).strip())
            except (TypeError, ValueError):
                return None

    def in_cooldown(self, now=None):
        now = time.monotonic() if now is None else now
        return bool(self.cooldown_until is not None and self.cooldown_until > now)

    def cooldown_left_s(self, now=None):
        now = time.monotonic() if now is None else now
        if not self.in_cooldown(now):
            return 0.0
        return max(0.0, self.cooldown_until - now)

    async def acquire(self):
        """取得一次发送许可；返回距上一次发送开始的真实间隔（秒，首次为 0）。

        冷却期内的发送会在这里等到冷却结束（不丢消息的路径用这个行为）。
        """
        async with self._lock:
            now = time.monotonic()
            if self.cooldown_until is not None and self.cooldown_until <= now:
                self.cooldown_until = None
            prev = self.last_send_ts
            if prev is None:
                ready_at = now
            else:
                ready_at = prev + self.min_gap_s
            if self.cooldown_until is not None and self.cooldown_until > ready_at:
                ready_at = self.cooldown_until
            wait = ready_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self.last_send_ts = now
            return 0.0 if prev is None else now - prev

    async def report_code(self, status_or_code):
        """识别 429/11253/11254 并触发冷却；返回是否真的触发了冷却。"""
        code = self._code_of(status_or_code)
        if code not in RATE_LIMIT_CODES:
            return False
        async with self._lock:
            now = time.monotonic()
            if self.last_trigger_ts is None or now - self.last_trigger_ts > _RESET_AFTER_S:
                self.cooldown_count = 0
            self.cooldown_count += 1
            self.consecutive_hits = self.cooldown_count
            duration = min(self.cooldown_max_s,
                           self.cooldown_base_s * (2 ** (self.cooldown_count - 1)))
            self.cooldown_until = now + duration
            self.last_trigger_ts = now
            log.info("QQ 限流冷却触发：code=%s，冷却 %.0f 秒（第 %s 次连续触发），"
                     "冷却至 %.3f（monotonic）", code, duration, self.cooldown_count,
                     self.cooldown_until)
        return True

    def status(self, now=None):
        now = time.monotonic() if now is None else now
        return {
            "last_send_ts": self.last_send_ts,
            "cooldown_until": self.cooldown_until,
            "cooldown_count": self.cooldown_count,
            "consecutive_hits": self.consecutive_hits,
            "in_cooldown": self.in_cooldown(now),
            "cooldown_left_s": round(self.cooldown_left_s(now), 3),
            "min_gap_ms": round(self.min_gap_s * 1000.0, 1),
        }
