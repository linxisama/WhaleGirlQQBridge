"""插件分层：控制台内置的「基础插件」与放进插件市场的「可选插件」。

构建安装包时按这里的清单排除可选插件；插件市场打包脚本也以它为准。
源码开发环境仍保留全部文件，方便测试与继续优化。
"""
from __future__ import annotations

# 只保留聊天主链路必需、或用户明确要求默认提供的能力。
BASIC_PLUGINS = (
    'addressee_guard', 'ambient_reply', 'anti_repeat', 'burst_merge',
    'chat_controls', 'conversation_memory', 'face_decode', 'forward_summary',
    'image_understanding', 'keyword_reply', 'mention_nudge_guard', 'pc_control', 'quick_menu',
    'quote_reply', 'reply_variety', 'send_pacing', 'short_followup',
    'sticker_admin', 'sticker_memory', 'token_saver', 'qqagent_bridge',
)

# 非插件模块，构建/打包时不要当成插件。
HELPERS = ('face_names',)

# 其余都进入插件市场；用 manager.PLUGIN_MODULES 计算，避免两处清单漂移。


def all_plugin_modules():
    from .plugins.manager import PLUGIN_MODULES
    return tuple(dict.fromkeys(PLUGIN_MODULES))


def market_plugins():
    basic = set(BASIC_PLUGINS)
    return tuple(name for name in all_plugin_modules()
                 if name not in basic and name not in HELPERS)
