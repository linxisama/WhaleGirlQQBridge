"""group_cost_report.py - 群友花费统计：群里 @ 一下，按人列出 token 与花费。

移植自 QQ Agent 社区插件 ``group-cost-report`` v1.0.0（kondius.cn/qq-agent/plugin-market/）。
原版把"一次运行的花费"归给**触发这次运行的人**，并支持在触发词后面写区间
（昨天 / 本周 / 上月 / 最近7天 / 9月1日到9月30日 / 全部…）。

本运行时的差异（都写在这里，避免误解）：

- 原版要靠模型调用一个工具来发报告；我们是**确定性立即回复** —— 命中触发词就把报表直接回出去，
  **完全不经过模型，零消耗**；
- 花的是"元"要有单价。社区版复用它们的 140 个模型价目表，我们没有这张表，所以提供
  ``priceIn`` / ``priceOut``（元 / 百万 token，默认 0 = **只报 token 不折算金额**）。
  真填了价格才算钱，绝不用猜的价格糊弄；
- 每次运行的 token 由核心在生成后写进该条消息的 ``_usage``（含本轮的识图、判定等附带调用），
  报表按发送者归集；没有 ``_usage`` 的老消息只计次数、不计 token。
"""

from __future__ import annotations

import json
import re
import time

from . import PluginBase, PluginMeta, FieldSchema

DAY = 86400
CST = 8 * 3600
DEFAULT_KEYWORDS = ('金额统计', '花费统计', '花费查询', 'token统计', '用量统计', '消费统计')
RANGE_KEYS = ('today', 'yesterday', 'thisweek', 'lastweek', 'thismonth', 'lastmonth',
              '7d', '30d', 'all')
_DATE_FULL = re.compile(r'(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*[日号]?')
_DATE_SHORT = re.compile(r'(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]')


def _day_start(ts: float) -> float:
    local = time.localtime(float(ts) + CST)
    return time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1)) - CST


def _fmt_day(ts: float, with_year: bool = False) -> str:
    fmt = '%Y-%m-%d' if with_year else '%m-%d'
    return time.strftime(fmt, time.localtime(float(ts) + CST))


def _span(key: str, start: float, end: float, now: float) -> dict:
    return {'key': key, 'start': float(start), 'end': float(end),
            'label': f'{_fmt_day(start)} 到 {_fmt_day(end)}'}


def parse_range(text: str, default_key: str = 'today', now: float = None) -> dict:
    """把"金额统计 后面的那段话"解析成统计区间。

    支持：今天/昨天/前天/本周/上周/本月/上月/最近7天/最近30天/全部，
    以及 ``9月1日到9月30日``、``2026-09-01 到 2026-09-30`` 这类显式区间；
    认不出来就用 ``default_key``。全部按北京时间算。
    """
    now = float(now if now is not None else time.time())
    body = str(text or '')
    today = _day_start(now)
    explicit = []
    for match in _DATE_FULL.finditer(body):
        year, month, day = (int(part) for part in match.groups())
        try:
            explicit.append(time.mktime((year, month, day, 0, 0, 0, 0, 0, -1)) - CST)
        except (ValueError, OverflowError):
            continue
    if not explicit:
        for match in _DATE_SHORT.finditer(body):
            month, day = int(match.group(1)), int(match.group(2))
            year = time.localtime(now + CST).tm_year
            try:
                explicit.append(time.mktime((year, month, day, 0, 0, 0, 0, 0, -1)) - CST)
            except (ValueError, OverflowError):
                continue
    if explicit:
        start, end = min(explicit), max(explicit)
        return _span('range', start, end + DAY - 1, now)
    if re.search(r'全部|所有|一直以来|历史', body):
        return _span('all', 0.0, now, now)
    if re.search(r'前天', body):
        return _span('beforeyesterday', today - 2 * DAY, today - 2 * DAY + DAY - 1, now)
    if re.search(r'昨天|昨日', body):
        return _span('yesterday', today - DAY, today - 1, now)
    if re.search(r'上周|上个?星期', body):
        monday = today - ((time.localtime(now + CST).tm_wday + 7) % 7) * DAY
        return _span('lastweek', monday - 7 * DAY, monday - 1, now)
    if re.search(r'本周|这周|这个?星期', body):
        monday = today - ((time.localtime(now + CST).tm_wday + 7) % 7) * DAY
        return _span('thisweek', monday, now, now)
    if re.search(r'上个?月', body):
        local = time.localtime(now + CST)
        year, month = (local.tm_year, local.tm_mon - 1) if local.tm_mon > 1 else (local.tm_year - 1, 12)
        start = time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1)) - CST
        end = time.mktime((local.tm_year, local.tm_mon, 1, 0, 0, 0, 0, 0, -1)) - CST - 1
        return _span('lastmonth', start, end, now)
    if re.search(r'本月|这个?月', body):
        local = time.localtime(now + CST)
        start = time.mktime((local.tm_year, local.tm_mon, 1, 0, 0, 0, 0, 0, -1)) - CST
        return _span('thismonth', start, now, now)
    days = re.search(r'(?:最近|近|过去)\s*(\d{1,3})\s*(?:天|日)', body)
    if days:
        count = max(1, min(365, int(days.group(1))))
        return _span(f'{count}d', today - (count - 1) * DAY, now, now)
    if re.search(r'最近7天|近7天|一周|7天', body):
        return _span('7d', today - 6 * DAY, now, now)
    if re.search(r'最近30天|近30天|一个月|30天', body):
        return _span('30d', today - 29 * DAY, now, now)
    if re.search(r'今天|今日|当天', body):
        return _span('today', today, now, now)
    key = default_key if default_key in RANGE_KEYS else 'today'
    if key == 'today':
        return _span('today', today, now, now)
    return parse_range({'yesterday': '昨天', 'thisweek': '本周', 'lastweek': '上周',
                        'thismonth': '本月', 'lastmonth': '上月', '7d': '最近7天',
                        '30d': '最近30天', 'all': '全部'}.get(key, '今天'), 'today', now)


def fmt_tokens(value) -> str:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return '0'
    if number >= 1000000:
        return f'{number / 1000000:.2f}M'
    if number >= 1000:
        return f'{number / 1000:.1f}k'
    return str(number)


def cost_of(prompt: int, completion: int, price_in: float, price_out: float) -> float:
    """按"元 / 百万 token"折算；没填价格就是 0（报表只报 token）。"""
    try:
        return (max(0, int(prompt or 0)) * float(price_in or 0)
                + max(0, int(completion or 0)) * float(price_out or 0)) / 1000000
    except (TypeError, ValueError):
        return 0.0


def usage_of(payload: str) -> dict:
    """从一条消息的 payload 里取本轮用量（没有就返回空表）。"""
    try:
        data = json.loads(payload or '{}')
    except (TypeError, ValueError):
        return {}
    usage = data.get('_usage') if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return {}
    return {'prompt': int(usage.get('prompt_tokens') or 0),
            'completion': int(usage.get('completion_tokens') or 0),
            'cached': int(usage.get('cached_tokens') or 0),
            'total': int(usage.get('total_tokens') or 0),
            'sender': str(data.get('sender_id') or ''),
            'name': str(data.get('sender_name') or '') or f"QQ:{data.get('sender_id') or '?'}"}


def build_report(rows, *, label: str, top_n: int = 8, footer: bool = True,
                 price_in: float = 0.0, price_out: float = 0.0) -> str:
    """把 ``rows``（``[(payload, created)]``）汇总成给人看的报表。"""
    members, runs, tokens = {}, 0, {'prompt': 0, 'completion': 0, 'cached': 0, 'total': 0}
    for payload, _created in rows or []:
        usage = usage_of(payload)
        if not usage:
            continue
        runs += 1
        for key in tokens:
            tokens[key] += usage.get(key, 0)
        entry = members.setdefault(usage['sender'] or usage['name'],
                                   {'name': usage['name'], 'runs': 0, 'prompt': 0, 'completion': 0})
        entry['runs'] += 1
        entry['prompt'] += usage['prompt']
        entry['completion'] += usage['completion']
    lines = [f'💰 本群花费统计（{label}）']
    if not runs:
        lines.append('这段时间里没有可统计的运行记录。')
        return '\n'.join(lines)
    money = cost_of(tokens['prompt'], tokens['completion'], price_in, price_out)
    summary = (f'合计 {runs} 次运行 · {fmt_tokens(tokens["total"])} token'
               f'（输入 {fmt_tokens(tokens["prompt"])} / 输出 {fmt_tokens(tokens["completion"])}'
               + (f'，其中缓存命中 {fmt_tokens(tokens["cached"])}' if tokens['cached'] else '') + '）')
    if money > 0:
        summary += f' · 约 ¥{money:.4f}'
    lines.append(summary)
    ordered = sorted(members.values(), key=lambda item: -(item['prompt'] + item['completion']))
    limit = max(1, int(top_n or 8))
    for index, item in enumerate(ordered[:limit], 1):
        piece = (f'{index}. {item["name"]}：{item["runs"]} 次 · '
                 f'{fmt_tokens(item["prompt"] + item["completion"])} token'
                 f'（输入 {fmt_tokens(item["prompt"])} / 输出 {fmt_tokens(item["completion"])}）')
        if money > 0:
            piece += f' · 约 ¥{cost_of(item["prompt"], item["completion"], price_in, price_out):.4f}'
        lines.append(piece)
    rest = ordered[limit:]
    if rest:
        rest_tokens = sum(item['prompt'] + item['completion'] for item in rest)
        lines.append(f'其余 {len(rest)} 人合计：{fmt_tokens(rest_tokens)} token')
    if footer:
        lines.append('（口径：每次运行的花费归给触发它的人'
                     + ('；金额按你填的单价折算' if money > 0 else '；没填单价所以只报 token') + '）')
    return '\n'.join(lines)


class GroupCostReport(PluginBase):
    META = PluginMeta(
        id='group_cost_report', name='群友花费统计', category='utility', enabled_by_default=False,
        description=('移植自社区插件 group-cost-report：群里 @机器人 说「金额统计」（可跟区间：'
                     '昨天 / 本周 / 上月 / 最近7天 / 9月1日到9月30日 / 全部…），就把这段时间本群的 '
                     'token 用量按**触发这次运行的人**逐人列出来。规则触发、立即回复，**不经过模型、零消耗**。'
                     '填了单价（元/百万 token）才折算金额，没填就只报 token。'),
        config_schema={
            'enabled': FieldSchema('bool', '启用花费统计'),
            'triggerWords': FieldSchema('string', '触发关键词（逗号分隔）'),
            'allowWithoutAt': FieldSchema('bool', '不需要 @ 也触发（容易误触发）'),
            'defaultRange': FieldSchema('select', '默认统计区间', options=[
                {'label': '今天', 'value': 'today'}, {'label': '昨天', 'value': 'yesterday'},
                {'label': '本周', 'value': 'thisweek'}, {'label': '上周', 'value': 'lastweek'},
                {'label': '本月', 'value': 'thismonth'}, {'label': '上月', 'value': 'lastmonth'},
                {'label': '最近 7 天', 'value': '7d'}, {'label': '最近 30 天', 'value': '30d'},
                {'label': '全部', 'value': 'all'}]),
            'topN': FieldSchema('int', '最多列出几人', min=1, max=50),
            'maxRows': FieldSchema('int', '单次最多扫描多少条运行记录', min=20, max=5000),
            'footer': FieldSchema('bool', '报告末尾附统计口径'),
            'priceIn': FieldSchema('float', '输入单价（元/百万 token，0=不折算）', min=0, max=100000),
            'priceOut': FieldSchema('float', '输出单价（元/百万 token，0=不折算）', min=0, max=100000),
        },
        default_settings={'enabled': False, 'triggerWords': '金额统计,花费统计,花费查询,token统计,用量统计,消费统计',
                          'allowWithoutAt': False, 'defaultRange': 'today', 'topN': 8,
                          'maxRows': 800, 'footer': True, 'priceIn': 0.0, 'priceOut': 0.0})

    def keywords(self) -> list:
        return [word.strip() for word in re.split(r'[,，、;；\s]+', str(self.get('triggerWords') or ''))
                if word.strip()]

    def triggered(self, message) -> str:
        """命中就返回触发词（附带后面的区间文字），否则返回空串。"""
        text = str(message.get('text') or '')
        if not text:
            return ''
        if not message.get('at') and not self.get('allowWithoutAt'):
            return ''
        for word in self.keywords():
            index = text.find(word)
            if index >= 0:
                return text[index:]
        return ''

    def on_ingest(self, message, runtime):
        if not self.get('enabled') or message.get('notice') or message.get('kind') != 'group':
            return None
        asked = self.triggered(message)
        if not asked:
            return None
        message['_robot_admin'] = True
        scope = f"{message.get('kind')}:{message.get('target')}"
        span = parse_range(asked, str(self.get('defaultRange') or 'today'))
        limit = max(20, int(self.get('maxRows') or 800))
        rows = runtime.db.execute(
            "SELECT payload,created FROM inbox WHERE mode=? AND account=? AND room=?"
            " AND state IN ('sent','partial') AND created>=? AND created<=?"
            " ORDER BY created DESC LIMIT ?",
            (runtime.mode, runtime.account(), scope, span['start'], span['end'], limit)).fetchall()
        report = build_report(rows, label=span['label'], top_n=int(self.get('topN') or 8),
                              footer=bool(self.get('footer', True)),
                              price_in=float(self.get('priceIn') or 0),
                              price_out=float(self.get('priceOut') or 0))
        if self.get('footer', True):
            report += f"\n（统计范围：{span['label']}，共扫到 {len(rows)} 条记录）"
        return report
