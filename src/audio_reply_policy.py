"""T08：自动语音与会话策略（规划书 1.6.3）。

策略：explicit（默认，仅明确请求）/ text / voice / both，按账号、群/私聊、角色配置。
群聊初版仅在“明确点名/命令”时生成，不把整个群所有消息自动朗读（T08 验收）。

流程（1.6.3）：
  已有聊天模型生成回复 → 按策略选定文本段并清理不适合朗读的内容 → 入队 TTS
  → 权限/账号复核 → 发送。代码块/长链接/超长回复按可见规则摘要或保留文字；
  禁止截断后悄悄改变意思。短语音失败可回退文字且**明确说明**；
  不自动改用不同音色或上传参考音频（T08 验收）。

人设只影响表达，**不**改变权限（VoicePolicy，1.2）。
真正的“选段+入队+发送”由插件 voice_reply.py 调 audio_jobs/scheduler；
本模块只做“这段该不该/以什么策略朗读”的纯策略判定，便于单测。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

REPLY_MODES = {'explicit', 'text', 'voice', 'both'}


@dataclass
class VoiceDecision:
    should_voice: bool
    reason: str
    segments: tuple = ()       # 选定要朗读的文本段（已清理）
    fallback_text: str = ''    # 语音失败时回退的文字（必须可解释，不静默改意思）


def decide_voice(reply: str, *, mode: str = 'explicit', is_group: bool = False,
                 explicitly_requested: bool = False, persona_text: Optional[str] = None) -> VoiceDecision:
    """按策略决定“这条回复要不要/以什么方式朗读”。纯函数，可单测。

    - explicit：只有 explicitly_requested 才朗读；否则不生成语音（T08 默认）。
    - text/voice/both：受会话规则约束；群聊未明确点名 → 不自动朗读。
    - persona_text 只影响“怎么说”，不改权限（本函数不读它做放行判断）。
    """
    if mode not in REPLY_MODES:
        raise ValueError(f'mode 须为 {sorted(REPLY_MODES)} 之一')
    reply = str(reply or '').strip()

    # 群聊初版：未明确点名/命令一律不自动朗读（1.6.3 / T08 验收）。
    if is_group and not explicitly_requested:
        return VoiceDecision(False, '群聊未明确点名，不自动朗读')

    if mode == 'explicit':
        if not explicitly_requested:
            return VoiceDecision(False, 'explicit：仅明确请求才朗读')
        segments = _readable_segments(reply)
        return VoiceDecision(True, 'explicit 明确请求', segments, fallback_text=reply)

    # text / voice / both：在允许自动的会话里，挑“适合朗读”的段。
    segments = _readable_segments(reply)
    if not segments:
        return VoiceDecision(False, '没有适合朗读的文本段（代码块/长链接已按规则处理）',
                             fallback_text=reply)
    return VoiceDecision(True, mode, segments, fallback_text=reply)


def _readable_segments(reply: str) -> tuple[str, ...]:
    """清理“不适合朗读”的内容（1.6.3）：代码块/长链接按可见规则摘要或保留文字。

    初版保守：按行保留可读中文/英文短句，跳过围栏代码块与超长 URL。
    TODO(T08): 按配置的字数上限切段，切分时**明确说明**而非静默截断。
    """
    out: list[str] = []
    in_code = False
    for line in str(reply).splitlines():
        s = line.strip()
        if s.startswith('```'):
            in_code = not in_code
            continue
        if in_code or not s or len(s) > 200 or s.startswith('http') :
            continue
        out.append(s)
    return tuple(out)
