"""affinity.py - 群友好感度：发言加分、@加分、接话加分，长期不发言缓慢衰减。

移植自 QQ Agent 社区插件 ``affinity`` v1.1.0（kondius.cn/qq-agent/plugin-market/）。
原版用 LLM 工具（check / rank / tune）+ 提示词片段让模型自己来查；本运行时没有工具调用，
所以映射成两件确定性的事：

1. **把当前说话人的真实分数与等级以"易变说明"注入提示词** —— 模型据此自然作答，
   问到"你我多熟/好感度多少"时不会编数字（这正是原版提示词片段想要的效果）；
2. **口令**：``好感度``（查自己）、``好感度 排行 [N]``、``好感度 重置 [QQ号]``（主人）、
   ``好感度 设置 <键>=<值>`` / ``好感度 设定``（主人）。全部在进模型之前处理，零消耗。

规则照搬原版：每条发言 ``perMessage`` 分、@机器人 ``atBotBonus``、接话 ``replyBonus``，
**每人每天加分上限 ``dailyCap``** 防刷屏；超过 ``decayAfterDays`` 天不发言起，每天扣
``decayPerDay``（不会扣成负数）；主人恒为 ``ownerScore`` 满级、不加分也不衰减；
等级门槛 ``levelThresholds``（默认 50,200,500,1000 → 陌生/眼熟/熟人/老友/挚友）。

与原版的两处差异写在文档里：**① 本运行时收不到"引用回复"信息**，所以"接话加分"用
"机器人刚在这条会话里回过话、且间隔小于 replyWindowSec"来判断；② 补扫用的是本地
聊天记录表（最近 24 小时、最多 scanLimit 条），并用 ``scored_upto`` 保证不重复计分。
"""

from __future__ import annotations

import json
import re
import time

from . import PluginBase, PluginMeta, FieldSchema

LEVEL_TITLES = ['陌生', '眼熟', '熟人', '老友', '挚友']
DEFAULT_THRESHOLDS = (50, 200, 500, 1000)
DAY = 86400
CST_OFFSET = 8 * 3600
MAX_MEMBERS_PER_SCOPE = 300
PENDING_KEEP = 5
TUNABLE = ('perMessage', 'atBotBonus', 'replyBonus', 'dailyCap', 'decayAfterDays',
           'decayPerDay', 'scanLimit', 'topN', 'announceLevelUp', 'allowReset',
           'replyWindowSec', 'ownerScore', 'levelThresholds')
COMMAND_RE = re.compile(r'^\s*[/\\]?\s*好感度\s*(?P<rest>.*)$')
RANK_WORDS = re.compile(r'^(排行|排名|榜单|榜)(\s+\d+)?$')
RESET_WORDS = re.compile(r'^(重置|清空|清零)(?:\s+(?P<who>\S+))?$')
TUNE_WORDS = re.compile(r'^(设置|设定|配置)(?:\s+(?P<body>.+))?$')


def parse_thresholds(raw) -> tuple:
    """把 ``"50,200,500,1000"`` 解析成 4 个递增整数；格式不对就回退默认值。"""
    if isinstance(raw, (list, tuple)):
        parts = [str(item).strip() for item in raw]
    else:
        parts = [item.strip() for item in re.split(r'[,，、;；\s]+', str(raw or '')) if item.strip()]
    try:
        numbers = [int(float(item)) for item in parts]
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLDS
    if len(numbers) != 4 or any(b <= a for a, b in zip(numbers, numbers[1:])):
        return DEFAULT_THRESHOLDS
    return tuple(numbers)


def level_of(score, thresholds=DEFAULT_THRESHOLDS) -> tuple:
    """返回 ``(等级 1-5, 称号)``。"""
    try:
        value = float(score or 0)
    except (TypeError, ValueError):
        value = 0.0
    level = 1
    for index, minimum in enumerate(thresholds):
        if value >= minimum:
            level = index + 2
    level = max(1, min(len(LEVEL_TITLES), level))
    return level, LEVEL_TITLES[level - 1]


def day_string(now: float) -> str:
    """北京时间日期（原版按本地日期重置每日上限，这里固定用北京时间，和程序其它地方一致）。"""
    return time.strftime('%Y-%m-%d', time.localtime(float(now or 0) + CST_OFFSET))


def new_record(now: float, name: str = '') -> dict:
    return {'first_seen': float(now or 0), 'last_seen': float(now or 0), 'msgs': 0, 'score': 0,
            'level': 1, 'day_key': day_string(now), 'day_gain': 0, 'decay_anchor': 0.0,
            'name': str(name or ''), 'pending': []}


def apply_decay(rec: dict, now: float, settings: dict) -> int:
    """按"多少天没发言"惰性扣分；用 ``decay_anchor`` 保证同一段时间不重复扣。主人不扣。"""
    if not isinstance(rec, dict) or rec.get('pinned'):
        return 0
    per_day = int(settings.get('decayPerDay') or 0)
    if per_day <= 0:
        return 0
    after_days = max(0, int(settings.get('decayAfterDays') or 0))
    anchor = max(float(rec.get('last_seen') or 0), float(rec.get('decay_anchor') or 0))
    if not anchor:
        return 0
    days = int((float(now) - anchor) // DAY)
    rec['decay_anchor'] = float(now)
    over = days - after_days
    if over <= 0:
        return 0
    loss = min(int(rec.get('score') or 0), over * per_day)
    if loss > 0:
        rec['score'] = max(0, int(rec.get('score') or 0) - loss)
        rec['decayed'] = int(rec.get('decayed') or 0) + loss
    return loss


def touch_member(rec: dict, now: float, settings: dict, *, at_bot: bool = False,
                 reply_to_bot: bool = False, owner: bool = False, name: str = '') -> int:
    """给一条消息记账，返回实际加到的分（可能因每日上限而少于应加）。"""
    if not isinstance(rec, dict):
        return 0
    if not rec.get('first_seen'):
        rec['first_seen'] = float(now)
    if name:
        rec['name'] = str(name)
    rec['msgs'] = int(rec.get('msgs') or 0) + 1
    rec['last_seen'] = float(now)
    if owner:
        rec['score'] = int(settings.get('ownerScore') or 9999)
        rec['level'] = len(LEVEL_TITLES)
        rec['pinned'] = True
        return 0
    apply_decay(rec, now, settings)
    key = day_string(now)
    if rec.get('day_key') != key:
        rec['day_key'] = key
        rec['day_gain'] = 0
    gain = int(settings.get('perMessage') or 0)
    if at_bot:
        gain += int(settings.get('atBotBonus') or 0)
    if reply_to_bot:
        gain += int(settings.get('replyBonus') or 0)
    gain = max(0, gain)
    room = max(0, int(settings.get('dailyCap') or 0) - int(rec.get('day_gain') or 0))
    applied = min(gain, room)
    rec['score'] = max(0, int(rec.get('score') or 0) + applied)
    rec['day_gain'] = int(rec.get('day_gain') or 0) + applied
    level, title = level_of(rec['score'], parse_thresholds(settings.get('levelThresholds')))
    if level > int(rec.get('level') or 1):
        rec['level'] = level
        if settings.get('announceLevelUp', True):
            pending = rec.get('pending') if isinstance(rec.get('pending'), list) else []
            pending.append({'title': title, 'level': level, 'at': float(now)})
            rec['pending'] = pending[-PENDING_KEEP:]
    else:
        rec['level'] = level
    return applied


def rank_of(members: dict, subject: str) -> int:
    """某人在这个会话里的名次（按分数从高到低，1 开始）；不在表里返回 0。"""
    if not isinstance(members, dict) or subject not in members:
        return 0
    ordered = sorted(members.items(), key=lambda item: -int((item[1] or {}).get('score') or 0))
    for index, (key, _) in enumerate(ordered, 1):
        if key == subject:
            return index
    return 0


def top_members(members: dict, limit: int) -> list:
    rows = sorted((members or {}).items(),
                  key=lambda item: -int((item[1] or {}).get('score') or 0))
    return [(key, rec) for key, rec in rows[:max(1, int(limit or 10))]]


class Affinity(PluginBase):
    META = PluginMeta(
        id='affinity', name='群友好感度', category='social', enabled_by_default=False,
        description=('移植自社区插件 affinity：自动给每个群友累积好感度 —— 发言加分、@机器人加分、'
                     '接话加分，每人每天有上限防刷屏；长期不发言缓慢衰减；主人恒为满级。'
                     '等级：陌生/眼熟/熟人/老友/挚友。当前说话人的真实分数与等级会作为"易变说明"'
                     '注入提示词，问到"你我多熟"时不会编数字。口令：好感度 / 好感度 排行 [N] / '
                     '好感度 重置 [QQ号]（主人）/ 好感度 设置 <键>=<值>（主人），全部零消耗。'),
        config_schema={
            'enabled': FieldSchema('bool', '启用好感度'),
            'perMessage': FieldSchema('int', '每条发言加分', min=0, max=20),
            'atBotBonus': FieldSchema('int', '@机器人额外加分', min=0, max=50),
            'replyBonus': FieldSchema('int', '接话额外加分', min=0, max=50),
            'dailyCap': FieldSchema('int', '每人每天加分上限', min=1, max=500),
            'decayAfterDays': FieldSchema('int', '多少天不发言开始衰减', min=0, max=365),
            'decayPerDay': FieldSchema('int', '每天衰减多少分', min=0, max=50),
            'replyWindowSec': FieldSchema('int', '多久内算"接话"（秒）', min=10, max=3600),
            'scanLimit': FieldSchema('int', '每次补扫的聊天记录条数', min=20, max=1000),
            'backfillSeconds': FieldSchema('int', '同一会话最短多久补扫一次（秒）', min=0, max=3600),
            'topN': FieldSchema('int', '排行榜默认人数', min=1, max=50),
            'announceLevelUp': FieldSchema('bool', '升级时让她顺口提一句'),
            'allowReset': FieldSchema('bool', '允许任何人重置好感度'),
            'ownerQq': FieldSchema('string', '主人 QQ 号（逗号分隔；留空=沿用机器人管理里绑定的管理员）'),
            'ownerScore': FieldSchema('int', '主人显示的固定分数', min=0, max=999999),
            'levelThresholds': FieldSchema('string', '等级门槛（4 个递增数字，逗号分隔）'),
            'verbose': FieldSchema('bool', '在控制台记录每次加分'),
        },
        default_settings={'enabled': False, 'perMessage': 1, 'atBotBonus': 3, 'replyBonus': 2,
                          'dailyCap': 20, 'decayAfterDays': 7, 'decayPerDay': 1,
                          'replyWindowSec': 90, 'scanLimit': 200, 'backfillSeconds': 30, 'topN': 10,
                          'announceLevelUp': True, 'allowReset': False, 'ownerQq': '',
                          'ownerScore': 9999, 'levelThresholds': '50,200,500,1000',
                          'verbose': False})

    # ── 状态 ─────────────────────────────────────────────────────────────────
    def data(self) -> dict:
        state = self.load_state('affinity.json', {})
        if not isinstance(state, dict):
            state = {}
        state.setdefault('members', {})
        state.setdefault('last_reply', {})
        state.setdefault('scored_upto', {})
        return state

    def save(self, state: dict) -> None:
        members = state.get('members') or {}
        for scope, table in list(members.items()):
            if isinstance(table, dict) and len(table) > MAX_MEMBERS_PER_SCOPE:
                keep = sorted(table.items(), key=lambda item: -int((item[1] or {}).get('score') or 0))
                members[scope] = dict(keep[:MAX_MEMBERS_PER_SCOPE])
        self.save_state('affinity.json', state)

    def settings_now(self) -> dict:
        return {key: self.get(key) for key in TUNABLE}

    def owner_ids(self, runtime) -> set:
        owners = {v.strip() for v in re.split(r'[,，\s]+', str(self.get('ownerQq') or '')) if v.strip()}
        if owners:
            return owners
        control = getattr(getattr(runtime, 'plugins', None), '_plugins', {}).get('pc_control')
        if control is None:
            return set()
        return {v.strip() for v in re.split(r'[,，\s]+', str(control.get('ownerIds') or '')) if v.strip()}

    @staticmethod
    def scope_of(message) -> str:
        return f"{message.get('kind')}:{message.get('target')}"

    def record(self, state: dict, scope: str, subject: str) -> dict:
        table = state['members'].setdefault(scope, {})
        return table.setdefault(str(subject), new_record(time.time()))

    # ── 记账 ─────────────────────────────────────────────────────────────────
    def score_message(self, message, runtime, *, count_backfill=False) -> tuple:
        """给一条消息记账；返回 ``(加到的分, 结构化说明)``。

        用 ``seen``（每个会话最后一次计分的消息 id）挡住重复：补扫与实时计分走的是同一段
        代码，同一 mid 只会算一次。
        """
        state = self.data()
        scope = self.scope_of(message)
        subject = str(message.get('sender_id') or '')
        if not subject:
            return 0, {}
        mid = str(message.get('id') or '')
        if mid and (state.get('seen') or {}).get(scope) == mid:
            return 0, {}
        now = float(message.get('_affinity_ts') or time.time())
        last_reply = float(state['last_reply'].get(scope) or 0)
        window = max(10, int(self.get('replyWindowSec') or 90))
        text = str(message.get('text') or '')
        aliases = self._bot_aliases(runtime)
        at_bot = bool(message.get('at')) or any(alias and alias in text for alias in aliases)
        reply_to_bot = bool(last_reply) and 0 <= (now - last_reply) <= window
        rec = self.record(state, scope, subject)
        owner = subject in self.owner_ids(runtime)
        gained = touch_member(rec, now, self.settings_now(), at_bot=at_bot,
                              reply_to_bot=reply_to_bot, owner=owner,
                              name=str(message.get('sender_name') or ''))
        if not count_backfill:
            state['scored_upto'][scope] = max(float(state['scored_upto'].get(scope) or 0), now)
            if mid:
                state.setdefault('seen', {})[scope] = mid
        self.save(state)
        if gained and self.get('verbose') and not count_backfill:
            runtime.note(f'[好感度] {rec.get("name") or subject} +{gained} → {rec["score"]}'
                         f'（{level_of(rec["score"], parse_thresholds(self.get("levelThresholds")))[1]}）')
        return gained, {'scope': scope, 'subject': subject, 'record': rec}

    @staticmethod
    def _bot_aliases(runtime) -> list:
        control = getattr(getattr(runtime, 'plugins', None), '_plugins', {}).get('chat_controls')
        raw = str(control.get('aliases') or '') if control is not None else ''
        return [item.strip() for item in re.split(r'[,，\s]+', raw) if len(item.strip()) > 1]

    def maybe_backfill(self, message, runtime) -> int:
        """按 ``backfillSeconds`` 节流补扫：同一会话在窗口内不再重复扫库。

        群聊刷屏时每条消息都扫一次最近 200 条是白花的开销（索引能救一部分，但没必要扫），
        所以默认同一会话 30 秒最多补扫一次；窗口内新消息仍会被实时计分，不会漏。
        """
        interval = max(0, int(self.get('backfillSeconds') or 0))
        scope = self.scope_of(message)
        now = time.time()
        if interval > 0:
            state = self.data()
            last = float((state.get('last_scan') or {}).get(scope) or 0)
            if last and now - last < interval:
                return 0
            state.setdefault('last_scan', {})[scope] = now
            if len(state['last_scan']) > 500:
                state['last_scan'] = dict(sorted(state['last_scan'].items(),
                                                 key=lambda item: -float(item[1] or 0))[:500])
            self.save(state)
        return self.backfill(message, runtime)

    def backfill(self, message, runtime, since: float = 0.0) -> int:
        """补扫这个会话里最近 24 小时、``since`` 之后的消息（不重复计分）。"""
        limit = max(20, int(self.get('scanLimit') or 200))
        scope = self.scope_of(message)
        state = self.data()
        anchor = max(float(since or state['scored_upto'].get(scope) or 0), time.time() - DAY)
        rows = runtime.db.execute(
            "SELECT payload,created FROM inbox WHERE mode=? AND account=? AND room=?"
            " AND state IN ('sent','partial','pending','sending') AND created>?"
            " ORDER BY created ASC LIMIT ?",
            (runtime.mode, runtime.account(), scope, since, limit)).fetchall()
        scored = 0
        for payload, created in rows:
            try:
                data = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if str(data.get('sender_id') or '') in {'', '0'}:
                continue
            data['_affinity_ts'] = float(created or 0)
            data.setdefault('kind', message.get('kind'))
            data.setdefault('target', message.get('target'))
            gained, _ = self.score_message(data, runtime, count_backfill=True)
            scored += 1 if gained else 0
        if rows:
            state = self.data()
            state['scored_upto'][scope] = max(float(state['scored_upto'].get(scope) or 0),
                                              float(rows[-1][1] or 0))
            self.save(state)
        return scored

    # ── 钩子 ─────────────────────────────────────────────────────────────────
    def on_ingest(self, message, runtime):
        if not self.get('enabled') or message.get('notice'):
            return None
        command = self._command(message, runtime)
        if command is not None:
            return command
        try:
            # 先补扫历史（用上一次的进度，并按 backfillSeconds 节流），再记这一条 ——
            # 顺序反了会把旧消息当成"已扫过"
            self.maybe_backfill(message, runtime)
            self.score_message(message, runtime)
        except Exception as exc:
            runtime.note(f'好感度记账跳过本轮（{type(exc).__name__}）')
        return None

    def on_pre_ai(self, message, conversation, runtime):
        if not self.get('enabled'):
            return None
        scope = self.scope_of(message)
        subject = str(message.get('sender_id') or '')
        state = self.data()
        rec = (state['members'].get(scope) or {}).get(subject)
        if not isinstance(rec, dict):
            return None
        level, title = level_of(rec.get('score'), parse_thresholds(self.get('levelThresholds')))
        rank = rank_of(state['members'].get(scope) or {}, subject)
        note = (f"【好感度】{rec.get('name') or subject}：{int(rec.get('score') or 0)} 分·{title}"
                f"（群里第 {rank or '?'} 名，累计发言 {int(rec.get('msgs') or 0)} 条）")
        pending = rec.get('pending') if isinstance(rec.get('pending'), list) else []
        if pending:
            note += f"；TA 刚刚升到「{pending[-1].get('title')}」，可以顺口提一句"
            rec['pending'] = []
            self.save(state)
        return self.volatile_hint(runtime, note)

    def on_sent(self, message, reply, runtime):
        if not self.get('enabled'):
            return
        state = self.data()
        state['last_reply'][self.scope_of(message)] = time.time()
        if len(state['last_reply']) > 500:
            state['last_reply'] = dict(sorted(state['last_reply'].items(),
                                              key=lambda item: -float(item[1] or 0))[:500])
        self.save(state)

    # ── 口令 ─────────────────────────────────────────────────────────────────
    def _command(self, message, runtime):
        text = str(message.get('text') or '').strip()
        match = COMMAND_RE.match(text)
        if not match:
            return None
        rest = (match.group('rest') or '').strip()
        scope = self.scope_of(message)
        subject = str(message.get('sender_id') or '')
        state = self.data()
        table = state['members'].get(scope) or {}
        if not rest or rest in {'查', '我的', '我的好感度'}:
            rec = table.get(subject)
            if not isinstance(rec, dict):
                return '我还不认识你诶，多说几句话就有了。'
            level, title = level_of(rec.get('score'), parse_thresholds(self.get('levelThresholds')))
            rank = rank_of(table, subject)
            return (f"你的好感度：{int(rec.get('score') or 0)} 分 · {title}"
                    f"（群里第 {rank} 名，累计发言 {int(rec.get('msgs') or 0)} 条）")
        rank_match = RANK_WORDS.match(rest)
        if rank_match:
            limit = int(rank_match.group(2) or self.get('topN') or 10)
            rows = top_members(table, limit)
            if not rows:
                return '还没有任何记录，先聊几句吧。'
            lines = [f"{index}. {rec.get('name') or key}：{int(rec.get('score') or 0)} 分"
                     for index, (key, rec) in enumerate(rows, 1)]
            return '好感度排行：\n' + '\n'.join(lines)
        reset_match = RESET_WORDS.match(rest)
        if reset_match:
            owner = subject in self.owner_ids(runtime)
            if not owner and not self.get('allowReset'):
                return '重置好感度只有主人能做（可以在插件设置里放开）。'
            who = (reset_match.group('who') or '').strip()
            if not who:
                table.pop(subject, None)
                self.save(state)
                return '已经把你自己的好感度清零了。'
            target = next((key for key, rec in table.items()
                           if key == who or str(rec.get('name') or '') == who), '')
            if not target:
                return f'这个会话里没有「{who}」的记录。'
            table.pop(target, None)
            self.save(state)
            return f'已把 {who} 的好感度清零。'
        tune_match = TUNE_WORDS.match(rest)
        if tune_match:
            owner = subject in self.owner_ids(runtime)
            body = (tune_match.group('body') or '').strip()
            if not owner:
                return '只有主人能改好感度的设定。'
            if not body:
                now = self.settings_now()
                return '当前好感度设定：\n' + '\n'.join(f'{key}={now[key]}' for key in TUNABLE)
            changed, bad = [], []
            for piece in re.split(r'[,，;\s]+', body):
                if not piece:
                    continue
                key, _, value = piece.partition('=')
                key = key.strip()
                if key not in TUNABLE:
                    bad.append(key)
                    continue
                try:
                    self._apply_tune(key, value.strip())
                except Exception:
                    bad.append(key)
                    continue
                changed.append(f'{key}={self.get(key)}')
            parts = []
            if changed:
                parts.append('已更新：' + '、'.join(changed))
            if bad:
                parts.append('没认出来：' + '、'.join(bad))
            return '\n'.join(parts) or '没有要改的项。'
        return None

    def _apply_tune(self, key: str, value: str) -> None:
        """单个设置项的值转换与提交（布尔认开/关，其余走整数）。

        ``PluginBase`` 没有 update_settings，必须经管理器提交（那里会按 schema 校验）。
        """
        schema = self.META.config_schema.get(key)
        if schema is not None and getattr(schema, 'type', '') == 'bool':
            payload = {key: value.strip().lower() in {'1', 'true', 'on', '开', '是', 'yes'}}
        elif key == 'levelThresholds':
            payload = {key: value}
        else:
            payload = {key: int(float(value))}
        self._manager.update_settings(self.META.id, payload)

    # ── 给"好感度主动发言"用的分数表 ─────────────────────────────────────────
    def chat_scores(self) -> dict:
        """``{"group:123": 最高分, ...}``：会话级的亲密度（取该会话里最高的成员分）。"""
        state = self.data()
        result = {}
        for scope, table in (state.get('members') or {}).items():
            scores = [int((rec or {}).get('score') or 0) for rec in (table or {}).values()]
            if scores:
                result[scope] = max(scores)
        return result
