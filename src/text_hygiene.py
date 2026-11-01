"""出站文本卫生：别把"入站渲染出来的占位符"原样发回 QQ。

背景（保证 QQ 端功能完整）：为了让模型读懂媒体，入站消息会把图片/表情渲染成
``[图片]`` ``[表情:微笑]`` ``[语音]`` ``[卡片消息…]`` 这类占位符（见 qq_transports）。
模型很爱照抄这种格式，于是回复里就带上 ``[表情:微笑]`` —— 发到 QQ 里就是**一行方括号字**，
用户看到的是乱码感的东西。这里在发送前统一清掉，并顺手收拾留下的多余空格。

只处理"明确是渲染产物"的形状，不动正常正文：
- ``[表情:名字]`` / ``[表情12]`` / ``[表情]``
- ``[动图:名字]``
- 纯占位：``[图片]`` ``[语音]`` ``[视频]`` ``[文件]`` ``[动画表情]`` ``[未知消息]``
- ``[卡片消息...]`` / ``[卡片：...]``（带内容的卡片占位）
"""
from __future__ import annotations

import re

FACE = re.compile(r'\[表情[:：]?[^\]\n]{0,40}\]')
ANIMATION = re.compile(r'\[动图[:：][^\]\n]{1,40}\]')
CARD = re.compile(r'\[卡片[^\]]{0,80}\]')
PLAIN = re.compile(r'\[(?:图片|语音|视频|文件|动画表情|未知消息|表情)\d*\]')

_SPACE_RUN = re.compile(r'[ \t]{2,}')
_SPACE_BEFORE_PUNCT = re.compile(r'[ \t]+([，。！？、；：）)】」』…~～,.!?;:])')
_LEADING_PUNCT = re.compile(r'^[ \t]*([，。！？、；：])')


_PACING = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd\u25a1]*PACE:\d+'
                     r'[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd\u25a1]*')


def strip_pacing(text: str) -> str:
    """去掉内部节奏标记（含被替换成方框/问号的残骸），只用于展示与落库。"""
    return _PACING.sub('', str(text or '')).strip()


def tidy_outbound(text: str) -> str:
    """清掉入站占位符并收拾空格。对正常文本是恒等变换（幂等）。"""
    value = str(text or '')
    if not value:
        return value
    for pattern in (FACE, ANIMATION, CARD, PLAIN):
        value = pattern.sub('', value)
    value = _SPACE_RUN.sub(' ', value)
    value = _SPACE_BEFORE_PUNCT.sub(r'\1', value)
    value = _LEADING_PUNCT.sub(r'\1', value)
    return value.strip()
