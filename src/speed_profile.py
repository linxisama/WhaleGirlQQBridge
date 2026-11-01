"""speed_profile.py - 「快速回复」档位：把"会拖慢回复"的设置一次性调到快档。

用户要求：回复要 3 秒左右。实测后确认**本地固定延迟**才是主因（连发合并无条件睡满 8 秒），
代码侧已修掉大部分；但用户自己的插件设置里仍可能留着大窗口（例如 `capMs: 8000`）。
这个模块只做一件事：算出一份"改哪些设置、改成什么"的清单，并在**用户确认后**才落盘。

设计约束（照用户要求）：

- **不改用户现有配置**，除非显式 ``apply=True``；
- 只碰"与等待/额外调用有关"的插件设置，**不碰**模型、密钥、人设、图片库、记忆等任何内容；
- 已经是快档的项不写进清单（不给用户看无意义的 diff）。
"""

from __future__ import annotations

# 目标值：本地机械耗时压到 1 秒内，且不引入额外模型调用
FAST_VALUES = {
    # 连发合并：等对方说完再回，但窗口要短（被 @ 时本来就不等）
    'burst_merge': {'capMs': 1200, 'quietMs': 500},
    # 句间/气泡节奏：群聊里逐条发会显得慢
    'send_pacing': {'enabled': False},
    # 轮次收尾判定（等对方说完的另一种实现）：窗口与延迟都压小
    'turn_complete': {'quietMs': 1500, 'maxDelayMs': 2000},
    # 表情回应：延迟贴表情不拖慢回复，但把等待清零更跟手
    'msg_reaction': {'delaySec': 1},
    # 正在输入状态对官方 QQ 是额外一次请求，群聊里没必要
    'typing_indicator': {'enabled': False},
    # "该不该接话"的判定是**额外一次模型调用**，每轮多花一次往返
    'context_chime': {'enabled': False},
    # 分享卡片读正文发生在**回复之前**：单页超时与总预算都压紧（并发已在代码里做了）
    'share_card': {'readTimeoutMs': 2000, 'readBudgetMs': 2500},
    # /搜索 也是回复前联网，超时同样压紧
    'search_query_hygiene': {'timeoutMs': 4000},
    # 跟聊窗口本身不加延迟，但窗口太大会让回复变得啰嗦，保持默认即可，不动它
}

# 明确禁止触碰的设置（写在这里是为了让"不改什么"也看得见）
NEVER_TOUCH = ('ai.api_url', 'ai.api_key', 'ai.model', 'qq.app_id', 'qq.client_secret',
               'style.*', 'qq_only.*')


def profile_diff(settings: dict) -> dict:
    """算出"快速回复"档位要改的设置（只列真正不同的项）。"""
    plan = {}
    for plugin_id, wanted in FAST_VALUES.items():
        current = settings.get(plugin_id)
        if not isinstance(current, dict):
            continue
        changes = {key: value for key, value in wanted.items()
                   if key in current and current.get(key) != value}
        if changes:
            plan[plugin_id] = changes
    return plan


def describe(plan: dict) -> str:
    """给用户看的中文清单（确认弹窗里逐条列出）。"""
    labels = {
        'burst_merge': ('连发合并窗口(ms)', '对方静默多久就开口(ms)'),
        'send_pacing': ('启用逐句发送',),
        'turn_complete': ('等对方说完的静默(ms)', '最长等待(ms)'),
        'msg_reaction': ('贴表情延迟(秒)',),
        'typing_indicator': ('发送"正在输入"',),
        'context_chime': ('每条消息都做一次接话判定（额外模型调用）',),
        'share_card': ('单个网页读取超时(ms)', '整条消息读网页总预算(ms)'),
        'search_query_hygiene': ('搜索超时(ms)',),
    }
    names = {'burst_merge': '连发合并', 'send_pacing': '发送节奏', 'turn_complete': '轮次收尾',
             'msg_reaction': '消息表情回应', 'typing_indicator': '正在输入状态',
             'context_chime': '上下文插话判定', 'share_card': '分享卡片解读',
             'search_query_hygiene': '搜索词清理与联网查找'}
    lines = []
    for plugin_id, changes in sorted(plan.items()):
        pieces = []
        for index, (key, value) in enumerate(changes.items()):
            label = labels.get(plugin_id, ())
            text = label[index] if index < len(label) else key
            if isinstance(value, bool):
                shown = '开启' if value else '关闭'
            else:
                shown = value
            pieces.append(f'{text} → {shown}')
        lines.append(f"· {names.get(plugin_id, plugin_id)}：{'；'.join(pieces)}")
    return '\n'.join(lines)
