"""minimal_mode.py - 极简模式：给"免费/便宜模型"用的快档，按档位砍上下文与输出。

移植自 QQ Agent 社区插件 ``minimal-mode`` v1.3.1（作者 雨夜流汐，MIT，来自
kondius.cn/qq-agent/plugin-market/）。**落地方式必须说清楚**：那边的极简模式砍三样东西
——技能说明段、工具定义（function calling 的 schema）、聊天记录条数；本运行时
**每条消息只调一次聊天模型、没有工具定义**，所以能砍的是另外三样：

1. **聊天记录条数**：`on_pre_ai` 里就地裁剪 conversation（省的是每轮都要重发的历史）；
2. **输出上限**：给本轮 `max_tokens` 封顶（极端档只给 1~2 句的位置）；
3. **追加一小段固定纪律**（"能不说就不说、说了就直给"）——固定文本，不破坏提示词前缀缓存。

三档递进（和原版一致，越往后越省、越不像人）：

- **极简**：历史收到 ``minimalMsgs`` 条 + 一段纪律；
- **极限**：历史更少、再给输出封顶；
- **敢死**：只留最短的历史与最小的输出上限，纪律最硬（"能不说就不说；说就 1~6 个字"）。

口令（只有绑定管理员能用，全程不经过模型）：``/极简 开``、``/极简 关``、``/极简 极限``、
``/极简 敢死``、``/极简``（看状态）。档位与统计写进插件状态，重启后仍在；关掉就完全恢复原样。
"""

from __future__ import annotations

import re
import time

from . import PluginBase, PluginMeta, FieldSchema

TIERS = ("off", "minimal", "ultra", "kamikaze")
TIER_NAMES = {"off": "已关", "minimal": "极简", "ultra": "极限", "kamikaze": "敢死"}

# 原版的纪律文本不可照抄（那边面向工具协议），这里按本运行时的能力重写，效果一致：短、直、少。
NOTES = {
    "minimal": ("【极简模式·已开】这轮尽量省：有话直说，一到两句就够，不要复述对方的话，"
                "不要解释自己为什么这么说，不要列点。没什么可说的就不说。"),
    "ultra": ("【极简模式·极限档】这轮用最省的方式回：一句话说清，能六个字解决就别写一行，"
              "不铺垫、不追问、不加表情符号。"),
    "kamikaze": ("【极简模式·敢死档】这轮最省：能不说就不说；要说就 1~6 个字，直给结论，"
                 "不解释、不铺垫、不反问。"),
}

# 口令：只在短句里认，避免把闲聊当成开关
KEY_RE = re.compile(r"极简|精简|省字|快模式")
OFF_RE = re.compile(r"(别|不要|不用|取消|关掉?|关闭|停用|退出|算了)")
ASK_RE = re.compile(r"(状态|开没开|开了吗|关了吗|怎么样|怎么用|怎么开|有什么用)")
ON_RE = re.compile(r"(开|开启|打开|启用|切到|进入|用)")
ULTRA_RE = re.compile(r"极限|压到?最低|压到底|最省|最低档|超省")
KAMIKAZE_RE = re.compile(r"敢死|玉碎|kamikaze", re.I)
MAX_COMMAND_LEN = 40


def history_limit(messages, limit: int):
    """只留最近 ``limit`` 条消息里"用户/助手"成对的部分，且必须保留最后一条。

    规则和原版一致：**永远保留当前这一条**（它就是要回应的内容），从后往前留。
    """
    if not isinstance(messages, list) or limit <= 0 or len(messages) <= limit:
        return messages, 0
    head, tail = messages[:-1], messages[-1:]
    kept = head[-(max(1, limit) - 1):] if limit > 1 else []
    dropped = len(head) - len(kept)
    return kept + tail, dropped


def match_command(text):
    """把用户/管理员的一句话解析成动作：``on``/``off``/``ultra``/``kamikaze``/``status``/None。"""
    raw = str(text or "").strip()
    if not raw or len(raw) > MAX_COMMAND_LEN:
        return None
    probe = raw.lstrip("/\\").replace("极简主义", "")
    if not KEY_RE.search(probe):
        return None
    if KAMIKAZE_RE.search(probe):
        return "kamikaze-off" if OFF_RE.search(probe) else "kamikaze"
    if ULTRA_RE.search(probe):
        if ASK_RE.search(probe):
            return "status"
        if OFF_RE.search(probe):
            return "ultra-off"
        return "ultra"
    if OFF_RE.search(probe):
        return "off"
    if ASK_RE.search(probe):
        return "status"
    if ON_RE.search(probe):
        return "on"
    return "status"


class MinimalMode(PluginBase):
    META = PluginMeta(
        id='minimal_mode', name='极简模式（省 token）', category='utility', enabled_by_default=False,
        description=('移植自社区插件 minimal-mode。按档位砍掉每轮都要重发的聊天记录、给输出封顶、'
                     '并追加一小段"说短话"的纪律：极简 / 极限 / 敢死三档，越往后越省、越不像人。'
                     '管理员私聊发「/极简 开 / 关 / 极限 / 敢死」即可切换（不经过模型，零消耗），'
                     '「/极简」看状态。关掉立刻恢复原样。'),
        config_schema={
            'enabled': FieldSchema('bool', '启用极简模式插件'),
            'tier': FieldSchema('select', '当前档位', options=[
                {'label': '关闭（不省）', 'value': 'off'},
                {'label': '极简', 'value': 'minimal'},
                {'label': '极限', 'value': 'ultra'},
                {'label': '敢死（最省，人味最淡）', 'value': 'kamikaze'}]),
            'ownerIds': FieldSchema('string', '允许开关口令的管理员（留空=沿用机器人管理里绑定的管理员）'),
            'minimalMsgs': FieldSchema('int', '极简档保留的历史消息条数', min=2, max=50),
            'ultraMsgs': FieldSchema('int', '极限/敢死档保留的历史消息条数', min=2, max=30),
            'minimalMaxTokens': FieldSchema('int', '极简档输出上限（0=不限）', min=0, max=8000),
            'ultraMaxTokens': FieldSchema('int', '极限档输出上限', min=16, max=4000),
            'kamikazeMaxTokens': FieldSchema('int', '敢死档输出上限', min=8, max=1000),
            'verbose': FieldSchema('bool', '在控制台记录每次省下的内容'),
        },
        default_settings={'enabled': False, 'tier': 'off', 'ownerIds': '',
                          'minimalMsgs': 12, 'ultraMsgs': 6,
                          'minimalMaxTokens': 0, 'ultraMaxTokens': 300, 'kamikazeMaxTokens': 80,
                          'verbose': True})

    # ── 档位 ─────────────────────────────────────────────────────────────────
    def tier(self) -> str:
        """当前档位：插件设置与运行期状态取"更靠后"的那个（口令切换写状态）。"""
        state = self.load_state('minimal_mode.json', {})
        tier = str(state.get('tier') or self.get('tier', 'off') or 'off')
        return tier if tier in TIERS else 'off'

    def set_tier(self, tier: str, by: str = '') -> str:
        tier = tier if tier in TIERS else 'off'
        state = self.load_state('minimal_mode.json', {})
        if state.get('tier') != tier:
            state['tier'] = tier
            state['since'] = time.time()
            state['by'] = str(by or '')
            state['stats'] = state.get('stats') or {}
            self.save_state('minimal_mode.json', state)
        return tier

    def stats(self) -> dict:
        return dict(self.load_state('minimal_mode.json', {}).get('stats') or {})

    def bump(self, **deltas):
        state = self.load_state('minimal_mode.json', {})
        stats = state.get('stats') or {}
        for key, value in deltas.items():
            stats[key] = int(stats.get(key, 0)) + int(value)
        state['stats'] = stats
        self.save_state('minimal_mode.json', state)

    def limits(self) -> dict:
        """当前档位生效的参数：历史条数 / 输出上限 / 纪律文本。"""
        tier = self.tier()
        if tier == 'off':
            return {'tier': tier, 'messages': 0, 'max_tokens': 0, 'note': ''}
        if tier == 'minimal':
            return {'tier': tier, 'messages': int(self.get('minimalMsgs', 12) or 12),
                    'max_tokens': int(self.get('minimalMaxTokens', 0) or 0), 'note': NOTES['minimal']}
        if tier == 'ultra':
            return {'tier': tier, 'messages': int(self.get('ultraMsgs', 6) or 6),
                    'max_tokens': int(self.get('ultraMaxTokens', 300) or 300), 'note': NOTES['ultra']}
        return {'tier': tier, 'messages': int(self.get('ultraMsgs', 6) or 6),
                'max_tokens': int(self.get('kamikazeMaxTokens', 80) or 80), 'note': NOTES['kamikaze']}

    # ── 生成前：裁历史 + 加纪律 + 封顶输出 ────────────────────────────────────
    def on_pre_ai(self, message, conversation, runtime):
        if not self.get('enabled'):
            return None
        plan = self.limits()
        if plan['tier'] == 'off':
            return None
        hints = {'extra_system': plan['note']}
        if isinstance(conversation, list):
            trimmed, dropped = history_limit(conversation, plan['messages'])
            if dropped:
                conversation[:] = trimmed
                self.bump(messages=dropped)
                if self.get('verbose', True):
                    runtime.note(f"[极简模式] {TIER_NAMES[plan['tier']]}档：本轮只带最近 "
                                 f"{plan['messages']} 条记录（裁掉 {dropped} 条）")
        if plan['max_tokens']:
            hints['max_tokens'] = plan['max_tokens']
            if self.get('verbose', True):
                runtime.note(f"[极简模式] {TIER_NAMES[plan['tier']]}档：本轮输出上限 {plan['max_tokens']} token")
        return hints

    # ── 口令（只认绑定管理员，不经过模型）────────────────────────────────────
    def is_owner(self, message, runtime) -> bool:
        if message.get('kind') != 'private':
            return False
        sender = str(message.get('sender_id') or '')
        configured = [v.strip() for v in re.split(r'[,，\s]+', str(self.get('ownerIds') or '')) if v.strip()]
        if not configured:
            control = runtime.plugins._plugins.get('pc_control')
            if control is None:
                return False
            return bool(control.is_admin(message))
        return sender in configured

    def on_ingest(self, message, runtime):
        if not self.get('enabled') or message.get('notice'):
            return None
        action = match_command(message.get('text', ''))
        if not action:
            return None
        if message.get('kind') != 'private':
            # 群聊里有人打这几个字（或闲聊里凑巧出现）不该让机器人接话：
            # 口令只在私聊生效，群里静默处理。
            return None
        if not self.is_owner(message, runtime):
            return '极简模式只有绑定管理员能开关。'
        message['_robot_admin'] = True
        by = str(message.get('sender_id') or '')
        if action == 'status':
            return self.status_text(runtime)
        tier = {'on': 'minimal', 'off': 'off', 'ultra': 'ultra', 'kamikaze': 'kamikaze',
                'ultra-off': 'minimal' if self.tier() in ('ultra', 'kamikaze') else self.tier(),
                'kamikaze-off': 'ultra' if self.tier() == 'kamikaze' else self.tier()}[action]
        self.set_tier(tier, by)
        runtime.note(f'极简模式切换为：{TIER_NAMES[tier]}（由 {by}）')
        return f'极简模式：{TIER_NAMES[tier]}\n{self.status_text(runtime)}'

    def status_text(self, runtime=None) -> str:
        plan = self.limits()
        stats = self.stats()
        lines = [f"当前档位：{TIER_NAMES[plan['tier']]}"]
        if plan['tier'] != 'off':
            lines.append(f"每轮只带最近 {plan['messages']} 条聊天记录"
                         + (f"，输出上限 {plan['max_tokens']} token" if plan['max_tokens'] else '，输出不限'))
        if stats:
            lines.append(f"累计省下：聊天记录 {stats.get('messages', 0)} 条")
        lines.append('切换：/极简 开、/极简 关、/极简 极限、/极简 敢死')
        return '\n'.join(lines)
