"""人设作用域归一：所有按人设隔离的存储统一用 '' 表示默认人设。

StyleManager.active_name 返回的是配置里的人设名（默认配置是 tsundere_whale_girl，
文件缺失时退化成 default），永远不是空串；而隔离逻辑一律把 '' 当成「默认人设，
走老路径/老文件」。不归一就会出现：默认人设的学习库落到
agent_learning.tsundere_whale_girl.json，老文件里的记录读不到，用户看起来像丢了数据。
"""
from __future__ import annotations

# 与 style_manager.DEFAULT_PERSONA 同名的兜底值：延迟导入失败时仍能归一。
DEFAULT_ALIASES = frozenset({'', 'default', 'tsundere_whale_girl'})

PERSONA_MAX = 40


def _default_names() -> frozenset:
    """style_manager.DEFAULT_PERSONA 也算默认人设；延迟导入避免循环依赖。"""
    try:
        from .style_manager import DEFAULT_PERSONA
    except Exception:
        return frozenset()
    return frozenset({DEFAULT_PERSONA}) if DEFAULT_PERSONA else frozenset()


def scope(persona) -> str:
    """归一一个人设名：默认人设返回 ''，其它返回去空白的原名（截到 40 字符）。"""
    name = str(persona or '').strip()
    if not name:
        return ''
    if name.lower() in DEFAULT_ALIASES:
        return ''
    if name in _default_names():
        return ''
    return name[:PERSONA_MAX]


def active_scope(bot) -> str:
    """当前激活人设的归一结果；bot 没有 style 时返回 ''（即默认人设）。"""
    style = getattr(bot, 'style', None)
    if style is None:
        return ''
    return scope(getattr(style, 'active_name', ''))


def is_default(persona) -> bool:
    """是不是默认人设（归一后为空）。"""
    return not scope(persona)
