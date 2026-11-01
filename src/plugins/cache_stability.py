"""cache_stability.py - 缓存稳定性：让服务商的前缀缓存真正命中。

移植自 QQ Agent 社区插件 ``cache-stability`` v1.0.0（kondius.cn/qq-agent/plugin-market/）。
原版做两件事：① 把系统提示词里"位置会漂移"的段落（视频理解规则）固定到末尾；
② 把用户消息里易变的部分（可用表情包、记忆）挪到稳定部分之后 —— 目标都是让
**发送前缀尽量逐字节相同**，因为服务商按前缀缓存计费，命中部分便宜一个数量级。

本运行时的形态不同（没有"已读/未读信息"那种大块拼接），对应的做法是：

1. **稳定在前、易变在后**：插件用 ``volatile_hint()`` 标注"每轮都会变"的说明
   （心情数值、本轮发言人摘要、跟聊提醒、语音处理状态…），核心把它们排到系统提示词最后；
   关掉本插件就退回原来的顺序（行为与未安装时一致）。
2. **真实命中率**：从服务商返回的 usage 里读命中 token（DeepSeek 报
   ``prompt_cache_hit_tokens``、OpenAI 报 ``prompt_tokens_details.cached_tokens``…），
   按轮记进控制台运行记录，不用猜。
3. **前缀变化提醒**：稳定部分（人设 + 稳定说明）的指纹变了就说明缓存要重新预热，
   这时记一条日志，方便对照"改完人设之后是不是变贵了"。
"""

from __future__ import annotations

import hashlib

from . import PluginBase, PluginMeta, FieldSchema

REPORT_EVERY = 5          # 每多少轮报一次命中率（避免刷屏）


def prefix_fingerprint(text: str) -> str:
    """稳定前缀的指纹（只用来判断"变了没有"，不保存原文）。"""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


def hit_rate(cached: int, prompt: int) -> float:
    """单次请求的命中率（0~1）；没有输入 token 时算 0。"""
    try:
        cached, prompt = int(cached or 0), int(prompt or 0)
    except (TypeError, ValueError):
        return 0.0
    if prompt <= 0:
        return 0.0
    return max(0.0, min(1.0, cached / prompt))


class CacheStability(PluginBase):
    META = PluginMeta(
        id='cache_stability', name='缓存稳定性', category='utility', enabled_by_default=False,
        description=('移植自社区插件 cache-stability。把"每轮都会变"的说明排到系统提示词最后，'
                     '让发送前缀尽量逐字节相同 —— 服务商按前缀缓存计费，命中部分便宜一个数量级。'
                     '同时把真实的缓存命中 token 记进运行记录，并在稳定前缀发生变化（改了人设或稳定说明）'
                     '时提醒你"缓存会重新预热"。关掉它就回到原来的顺序，行为与未安装时一致。'),
        config_schema={
            'enabled': FieldSchema('bool', '启用缓存稳定性'),
            'stableOrder': FieldSchema('bool', '稳定说明在前、易变说明在后'),
            'reportEvery': FieldSchema('int', '每多少轮记录一次命中率', min=1, max=100),
            'verbose': FieldSchema('bool', '每次前缀变化都记录'),
        },
        default_settings={'enabled': False, 'stableOrder': True, 'reportEvery': REPORT_EVERY,
                          'verbose': False})

    def __init__(self, manager):
        super().__init__(manager)
        self._last_prefix = ''
        self._turns = 0

    # ── 记账 ─────────────────────────────────────────────────────────────────
    def state(self) -> dict:
        data = self.load_state('cache_stability.json', {})
        return data if isinstance(data, dict) else {}

    def note_prefix(self, runtime, fingerprint: str) -> None:
        """稳定前缀变了就记一条（第一次只是记住，不算"变化"）。"""
        previous = self._last_prefix or str(self.state().get('prefix') or '')
        self._last_prefix = fingerprint
        if previous and previous != fingerprint:
            self.save_state('cache_stability.json', {'prefix': fingerprint})
            runtime.note('缓存稳定性：稳定说明变了，前缀缓存会重新预热一轮')
        elif not previous:
            self.save_state('cache_stability.json', {'prefix': fingerprint})

    def report(self, runtime, usage: dict) -> None:
        """每 N 轮把这一段的命中率写进运行记录。"""
        interval = max(1, int(self.get('reportEvery', REPORT_EVERY) or REPORT_EVERY))
        self._turns += 1
        if self._turns % interval:
            return
        cached = int(usage.get('cached_tokens') or 0)
        prompt = int(usage.get('prompt_tokens') or 0)
        if prompt <= 0:
            return
        rate = hit_rate(cached, prompt)
        runtime.note(f'缓存命中率（累计）：{rate * 100:.0f}%（命中 {cached} / 输入 {prompt} token）')

    # ── 钩子 ─────────────────────────────────────────────────────────────────
    def on_pre_ai(self, message, conversation, runtime):
        if not self.get('enabled'):
            return None
        # 稳定前缀 = 人设提示词 + 本轮稳定说明；易变说明由核心排在它后面。
        persona = getattr(runtime, 'style', None)
        try:
            base = runtime.style.full_prompt(None) if persona is not None else ''
        except Exception:
            base = ''
        fingerprint = prefix_fingerprint(base)
        self.note_prefix(runtime, fingerprint)
        if self.get('verbose'):
            runtime.note(f'缓存稳定性：稳定前缀 {fingerprint}（{len(base)} 字符）')
        return None

    def on_sent(self, message, reply, runtime):
        if not self.get('enabled'):
            return
        try:
            usage = getattr(getattr(runtime, 'ai', None), 'usage', None)
            summary = usage.summary if usage is not None else {}
        except Exception:
            summary = {}
        if summary:
            self.report(runtime, summary)

    def status_text(self) -> str:
        data = self.state()
        return (f"缓存稳定性：{'已开' if self.get('enabled') else '已关'}"
                f"（稳定说明在前：{'是' if self.get('stableOrder', True) else '否'}）\n"
                f"上次记录的稳定前缀：{data.get('prefix') or '（还没记过）'}")
