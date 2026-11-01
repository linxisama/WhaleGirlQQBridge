"""reply_freshness.py - 回复时机与引用：让它知道"现在是什么时候、这句话还新不新鲜"。

移植自 QQ Agent 社区插件 ``reply-freshness`` v1.0.0（kondius.cn/qq-agent/plugin-market/）。
原版解决一个观感问题：**群里刷得快**，机器人思考期间又刷了一屏，等它开口时那句话在时间上
已经"脱节"。真人这时有两种自然做法，插件把这两种明确告诉模型：

① **晚回就说明**："我特意回你刚才那句"，必要时补一句"刚看到"；
② **冷场就别硬接**：群里已经安静下来，别再把没读的旧消息一条条回完（自说自话最像 AI）。

落地方式与原版一致：**只做加法**，把一小段时序提示追加到**当前这条 user 消息末尾**
（不动系统提示词，前缀缓存不受影响）；关掉插件行为与从前完全一致。

与原版的差异（写在文档里）：原版读内存里的最近消息、并能按 replyToMessageId 真的去引用；
本运行时改成从本地聊天记录表取最近几条算时序（发送、发出的回复都能看到），
"引用"这一步交给现成的「引用回复」插件，这里只做**措辞建议**。冷场时原版是建议模型自己
"finish"，我们额外给一个**可选的硬刹车** ``coldHold``（默认关）：真的不回那种"没人等你"的消息。
"""

from __future__ import annotations

import json
import re
import time

from . import PluginBase, PluginMeta, FieldSchema

TIME_BLOCK = '【本轮时序】'


def ago_text(seconds) -> str:
    """人话时长：8 秒 / 3 分 12 秒 / 1 小时 5 分（照搬原版）。"""
    try:
        total = max(0, int(round(float(seconds or 0))))
    except (TypeError, ValueError):
        return '0 秒'
    if total < 60:
        return f'{total} 秒'
    if total < 3600:
        minutes, rest = divmod(total, 60)
        return f'{minutes} 分 {rest} 秒' if rest else f'{minutes} 分'
    return f'{total // 3600} 小时 {(total % 3600) // 60} 分'


def clock(ts) -> str:
    return time.strftime('%H:%M:%S', time.localtime(float(ts or time.time())))


def who_of(entry: dict) -> str:
    name = str(entry.get('sender_name') or '').strip()
    uid = str(entry.get('sender_id') or '').strip()
    if name and uid and name != uid:
        return f'{name}(QQ:{uid})'
    return name or (f'QQ:{uid}' if uid else '某人')


def parse_row(payload: str) -> dict:
    try:
        data = json.loads(payload or '{}')
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def timing_of(rows, *, mid: str, now: float = None) -> dict:
    """从最近的记录里算出本轮做判断需要的时序事实。

    ``rows``：``[(payload, created)]``，按 created 从新到旧。``mid``：当前要回的那条消息 id。
    拿不到有效记录时返回空表（调用方据此不注入任何东西）。
    """
    now = float(now if now is not None else time.time())
    entries = []
    for payload, created in rows or []:
        data = parse_row(payload)
        if not data:
            continue
        data['_created'] = float(created or 0)
        entries.append(data)
    if not entries:
        return {}
    newest = entries[0]
    current = next((item for item in entries if str(item.get('id') or '') == str(mid or '')), newest)
    newer = [item for item in entries if item['_created'] > current['_created']]
    # 与原版同口径：others 里"最新的一条非机器人消息"就是临场感判断要看的那条
    others = [item for item in entries
              if str(item.get('id') or '') != str(current.get('id') or '')]
    last_other = others[0] if others else None
    return {
        'current': current,
        'newest': newest,
        'newer_count': len(newer),
        'is_latest': not newer,
        'since_current': max(0, now - current['_created']),
        'since_last_other': (max(0, now - last_other['_created']) if last_other else None),
        'last_other': last_other,
    }


def is_mention(message: dict, aliases) -> bool:
    if message.get('at'):
        return True
    text = str(message.get('text') or '')
    return any(alias and alias in text for alias in (aliases or []))


def build_block(timing: dict, *, late_seconds: int, quiet_seconds: int, bot_spoke_last: bool,
                mentions_bot: bool, is_private: bool = False) -> str:
    """组装给模型看的一小段时序提示（没有可说的就返回空串）。"""
    current = timing.get('current') or {}
    if not current:
        return ''
    lines = [f"{TIME_BLOCK}现在 {clock(time.time())}"]
    newest = timing.get('newest') or {}
    if newest and str(newest.get('id')) != str(current.get('id')):
        lines.append(f"- 群里最新一条：{who_of(newest)}（{ago_text(timing.get('since_current'))}之后）")
    head = f"- 你要看的是 {who_of(current)} 那条（{ago_text(timing.get('since_current'))}前）"
    if timing.get('is_latest'):
        lines.append(f'{head} —— 它就是最新的')
    else:
        lines.append(f"{head} —— 它之后群里又来了 {timing.get('newer_count', 0)} 条")
    cold = (not is_private and timing.get('since_last_other') is not None
            and timing['since_last_other'] >= quiet_seconds and (bot_spoke_last or not mentions_bot))
    late = timing.get('since_current', 0) >= late_seconds
    if cold:
        tail = '，之后只有你说过话' if bot_spoke_last else ''
        lines.append(f"- 群里上一条别人的消息在 {ago_text(timing['since_last_other'])}前{tail} —— 现在没人等你")
    if mentions_bot:
        lines.append('- 这一条里有人点名/直接问你')
    if cold:
        lines.append('→ 已经安静下来了：默认别为了清空未读继续回旧消息，最多轻轻应一句；'
                     '真有话要说就自然补一句衔接。')
    elif not timing.get('is_latest'):
        advice = ('你要回的不是最新那条：自然补一句衔接（「刚看到」「回一下上面」），别装作即时回应'
                  if late else '你要回的不是最新那条：必要时说一句「回你上面那句」')
        lines.append(f'→ {advice}。')
    elif late:
        lines.append(f"→ 这条已经过去 {ago_text(timing.get('since_current'))}：可以自然带一句「刚看到」，不用刻意解释。")
    return '\n'.join(lines)


class ReplyFreshness(PluginBase):
    META = PluginMeta(
        id='reply_freshness', name='回复时机与引用', category='social', enabled_by_default=False,
        description=('移植自社区插件 reply-freshness：把"现在几点、群里最新一条是谁、你要回的那条'
                     '还新不新鲜"算清楚，追加在当前这条消息末尾（不动系统提示词，不伤前缀缓存）。'
                     '晚回就自然补一句衔接；群里冷场时提醒她别为了清空未读继续回旧消息。'
                     '另有可选的硬刹车「冷场就不回」（默认关）：真的安静下来又没人点她时，本轮不发。'),
        config_schema={
            'enabled': FieldSchema('bool', '启用回复时机提示'),
            'quietSeconds': FieldSchema('int', '安静多少秒算冷场', min=20, max=3600),
            'lateReplySeconds': FieldSchema('int', '超过多少秒算晚回', min=5, max=1800),
            'coldAdvice': FieldSchema('bool', '冷场时提醒"别硬接"'),
            'coldHold': FieldSchema('bool', '冷场且没人点她时干脆不回（默认关）'),
            'announce': FieldSchema('bool', '注入时序说明'),
            'scanLimit': FieldSchema('int', '回看多少条记录算时序', min=5, max=200),
            'verbose': FieldSchema('bool', '在控制台记录时序判断'),
        },
        default_settings={'enabled': False, 'quietSeconds': 90, 'lateReplySeconds': 45,
                          'coldAdvice': True, 'coldHold': False, 'announce': True,
                          'scanLimit': 40, 'verbose': False})

    def last_reply_at(self, runtime, scope: str) -> float:
        data = self.load_state('reply_freshness.json', {})
        return float((data or {}).get('last_reply', {}).get(scope) or 0)

    def note_reply(self, scope: str) -> None:
        data = self.load_state('reply_freshness.json', {})
        if not isinstance(data, dict):
            data = {}
        last = data.setdefault('last_reply', {})
        last[scope] = time.time()
        if len(last) > 500:
            data['last_reply'] = dict(sorted(last.items(), key=lambda item: -float(item[1] or 0))[:500])
        self.save_state('reply_freshness.json', data)

    def recent_rows(self, runtime, message, limit: int):
        scope = f"{message.get('kind')}:{message.get('target')}"
        return runtime.db.execute(
            "SELECT payload,created FROM inbox WHERE mode=? AND account=? AND room=?"
            " AND state IN ('sent','partial','pending','sending') ORDER BY created DESC LIMIT ?",
            (runtime.mode, runtime.account(), scope, max(5, int(limit or 40)))).fetchall()

    def analyse(self, runtime, message):
        """算一次时序；返回 ``(timing, cold)``。"""
        timing = timing_of(self.recent_rows(runtime, message, self.get('scanLimit', 40)),
                           mid=str(message.get('id') or ''))
        if not timing:
            return {}, False
        scope = f"{message.get('kind')}:{message.get('target')}"
        bot_spoke_last = self.last_reply_at(runtime, scope) >= float(
            (timing.get('last_other') or {}).get('_created') or 0)
        control = getattr(getattr(runtime, 'plugins', None), '_plugins', {}).get('chat_controls')
        aliases = [item.strip() for item in re.split(
            r'[,，\s]+', str(control.get('aliases') or '') if control is not None else '') if len(item.strip()) > 1]
        mentions = is_mention(message, aliases)
        is_private = str(message.get('kind') or '') == 'private'
        cold = (not is_private and timing.get('since_last_other') is not None
                and timing['since_last_other'] >= int(self.get('quietSeconds') or 90)
                and (bot_spoke_last or not mentions))
        timing.update({'cold': cold, 'bot_spoke_last': bot_spoke_last, 'mentions_bot': mentions,
                       'is_private': is_private})
        return timing, cold

    def on_ingest(self, message, runtime):
        """可选硬刹车：冷场 + 没人点她 → 这一轮干脆不回（默认关）。"""
        if not self.get('enabled') or not self.get('coldHold') or message.get('notice'):
            return None
        try:
            timing, cold = self.analyse(runtime, message)
        except Exception:
            return None
        if not cold or timing.get('mentions_bot') or timing.get('is_private'):
            return None
        message['_reply_freshness_cold'] = True
        return None

    def on_block(self, message, runtime) -> bool:
        if not self.get('enabled') or not self.get('coldHold'):
            return False
        if message.get('_reply_freshness_cold'):
            runtime.note('回复时机：群里已安静且没人点名，本轮不发言（冷场刹车）')
            return True
        return False

    def on_pre_ai(self, message, conversation, runtime):
        if not self.get('enabled') or not isinstance(conversation, list) or not conversation:
            return None
        try:
            timing, cold = self.analyse(runtime, message)
        except Exception:
            return None
        if not timing:
            return None
        block = ''
        if self.get('announce', True):
            block = build_block(timing, late_seconds=int(self.get('lateReplySeconds') or 45),
                                quiet_seconds=int(self.get('quietSeconds') or 90),
                                bot_spoke_last=bool(timing.get('bot_spoke_last')),
                                mentions_bot=bool(timing.get('mentions_bot')),
                                is_private=bool(timing.get('is_private')))
        if cold and self.get('coldAdvice', True) and not block:
            block = (f"{TIME_BLOCK}群里已经安静 {ago_text(timing.get('since_last_other'))}，没人等你："
                     '默认别继续回旧消息，最多轻轻应一句。')
        if not block:
            return None
        # 只往"当前这条 user 消息"末尾追加，不动系统提示词（前缀缓存不受影响）
        target = None
        for item in reversed(conversation):
            if isinstance(item, dict) and item.get('role') == 'user' and isinstance(item.get('content'), str):
                target = item
                break
        if target is None:
            return None
        target['content'] = f"{target['content']}\n\n{block}"
        if self.get('verbose'):
            runtime.note(f"[回复时机] {timing.get('newer_count', 0)} 条更新 · "
                         f"本条 {ago_text(timing.get('since_current'))}前"
                         + ('（冷场）' if cold else ''))
        return None

    def on_sent(self, message, reply, runtime):
        if not self.get('enabled'):
            return
        try:
            self.note_reply(f"{message.get('kind')}:{message.get('target')}")
        except Exception:
            pass
