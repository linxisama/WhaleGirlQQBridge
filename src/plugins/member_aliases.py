"""member_aliases.py - 群友外号对照：进提示词让模型认人。

移植自 QQ Agent 社区插件 ``member-aliases`` v1.0.0（kondius.cn/qq-agent/plugin-market/）。
原版是"纯 hooks"：谁在说话就告诉他这人还叫什么；消息里出现了某人的外号，就告诉他指的是谁。
**命中才加行**，没命中一个字都不加（不花 token）。

本运行时的对应做法：

- 注入位置用 ``volatile_hint()``（提示词**末尾**）—— 原版注释写得很清楚："别动 system 前缀，
  否则整条缓存作废"；装了「缓存稳定性」时它正好排在最后，没装就跟在稳定说明后面。
- 外号表存 ``member_aliases.json``（形状与原版一致：``{"aliases": {"<uid>": ["外号"]}}``）。
  原版在控制台「记忆 → 群成员列表 → 编辑外号」里改，本运行时改成**口令**：
  ``外号``（看表）、``外号 加 <QQ号> <外号>`、``外号 删 <QQ号> <外号>``（管理员/主人）；
  另有可选 ``autoLearn``：有人说"我叫X/叫我X"时自动记下来（默认关，免得把玩笑话存成外号）。
- 原版扫"本轮触发的一批消息"，我们只拿得到当前这条 → 说话人一行按当前发送者算，
  "别人提到外号"按当前消息文本算。这一点在文档里写明了。
"""

from __future__ import annotations

import re

from . import PluginBase, PluginMeta, FieldSchema

HEADER = '【称呼对照】群友的外号（只是帮你认人，别在回复里刻意解释这些对应关系）：'
MAX_ALIASES_PER_MEMBER = 12
MAX_ALIAS_LEN = 24
MAX_MEMBERS = 500
COMMAND_RE = re.compile(r'^\s*[/\\]?\s*外号\s*(?P<rest>.*)$')
LEARN_RE = re.compile(r'(?:我|咱|本?人)(?:就)?(?:叫|是|名叫|昵称是)\s*([^\s，,。！!？?、；;：:]{1,12})')


def text_has_alias(text, alias) -> bool:
    """文本里是否出现了这个外号。

    中文不用空格分词，所以分两档（照搬原版）：
    **纯中文或长度 ≥3** 的外号直接包含判断（"某某今天怎么没来"要能命中）；
    **含 ASCII 字母数字的短外号**（"99"）只在 ASCII 字母数字边界上判断，
    否则 "99" 会命中 "1990"；中文紧邻不算越界（"99来了"照样命中）。
    """
    body = str(text or '')
    word = str(alias or '').strip()
    if not body or not word:
        return False
    if not re.search(r'[0-9A-Za-z]', word) or len(word) >= 3:
        return word in body
    pattern = rf'(?<![0-9A-Za-z]){re.escape(word)}(?![0-9A-Za-z])'
    return re.search(pattern, body, re.IGNORECASE) is not None


def clean_aliases(value) -> list:
    """规整一个人的外号列表：去空、去重、限长限量。"""
    if isinstance(value, str):
        items = re.split(r'[,，、;；\s]+', value)
    elif isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    else:
        return []
    result = []
    for item in items:
        word = str(item or '').strip()
        if not word or len(word) > MAX_ALIAS_LEN or word in result:
            continue
        result.append(word)
        if len(result) >= MAX_ALIASES_PER_MEMBER:
            break
    return result


def build_note(aliases: dict, *, sender: str = '', sender_name: str = '', text: str = '',
               scan_mentions: bool = True, max_lines: int = 12) -> str:
    """按原版顺序拼"称呼对照"块：先本轮说话的人，再被提到的外号；没有命中就返回空串。"""
    table = {str(uid): clean_aliases(list_) for uid, list_ in (aliases or {}).items()}
    table = {uid: list_ for uid, list_ in table.items() if list_}
    if not table:
        return ''
    lines, seen = [], set()

    def label(uid: str) -> str:
        if uid == str(sender or '') and sender_name:
            return str(sender_name)
        return table.get(uid, [''])[0] or f'QQ:{uid}'

    if sender and table.get(str(sender)):
        seen.add(f's:{sender}')
        lines.append(f"· 正在说话的 {label(str(sender))}(QQ:{sender}) 也叫：{'、'.join(table[str(sender)])}")
    if scan_mentions:
        for uid, words in table.items():
            if f's:{uid}' in seen:
                continue
            hit = next((word for word in words if text_has_alias(text, word)), '')
            if not hit:
                continue
            lines.append(f"· 有人提到「{hit}」= {label(uid)}(QQ:{uid})（外号：{'、'.join(words)}）")
    if not lines:
        return ''
    limit = max(1, int(max_lines or 12))
    return '\n'.join([HEADER] + lines[:limit])


class MemberAliases(PluginBase):
    META = PluginMeta(
        id='member_aliases', name='群友外号对照', category='knowledge', enabled_by_default=False,
        description=('移植自社区插件 member-aliases：谁在说话就告诉模型这人还叫什么；消息里出现'
                     '某人的外号时，告诉模型指的是谁 —— 命中才加几行，没命中一个字不加。'
                     '外号表可用口令维护：外号 / 外号 加 <QQ号> <外号> / 外号 删 <QQ号> <外号>'
                     '（管理员或主人）；可选自动从"我叫X"里学（默认关）。'),
        config_schema={
            'enabled': FieldSchema('bool', '启用外号对照'),
            'scanMentions': FieldSchema('bool', '识别「别人提到外号」'),
            'maxLines': FieldSchema('int', '每轮最多几行', min=1, max=50),
            'autoLearn': FieldSchema('bool', '自动记下"我叫X"（默认关）'),
            'verbose': FieldSchema('bool', '在控制台记录加了哪几行'),
        },
        default_settings={'enabled': False, 'scanMentions': True, 'maxLines': 12,
                          'autoLearn': False, 'verbose': False})

    # ── 外号表 ───────────────────────────────────────────────────────────────
    def table(self) -> dict:
        data = self.load_state('member_aliases.json', {})
        raw = (data or {}).get('aliases') if isinstance(data, dict) else {}
        if not isinstance(raw, dict):
            return {}
        cleaned = {}
        for uid, words in list(raw.items())[:MAX_MEMBERS]:
            aliases = clean_aliases(words)
            if aliases:
                cleaned[str(uid)] = aliases
        return cleaned

    def save_table(self, table: dict) -> None:
        self.save_state('member_aliases.json', {'version': 1, 'aliases': table})

    def add_alias(self, uid: str, alias: str) -> bool:
        uid, alias = str(uid or '').strip(), str(alias or '').strip()
        if not uid or not alias or len(alias) > MAX_ALIAS_LEN:
            return False
        table = self.table()
        words = table.setdefault(uid, [])
        if alias in words:
            return False
        words.append(alias)
        table[uid] = clean_aliases(words)
        self.save_table(table)
        return True

    def remove_alias(self, uid: str, alias: str) -> bool:
        table = self.table()
        words = table.get(str(uid or '').strip())
        if not words or alias not in words:
            return False
        words.remove(alias)
        if words:
            table[str(uid).strip()] = words
        else:
            table.pop(str(uid).strip(), None)
        self.save_table(table)
        return True

    # ── 钩子 ─────────────────────────────────────────────────────────────────
    def on_pre_ai(self, message, conversation, runtime):
        if not self.get('enabled'):
            return None
        table = self.table()
        sender = str(message.get('sender_id') or '')
        text = str(message.get('text') or '')
        # 先学再判空：否则空表永远学不到第一句"我叫X"（自动学习就永远启动不了）
        if self.get('autoLearn'):
            self._learn(sender, text, runtime)
            table = self.table()
        if not table:
            return None
        note = build_note(table, sender=sender, sender_name=str(message.get('sender_name') or ''),
                          text=text, scan_mentions=bool(self.get('scanMentions', True)),
                          max_lines=int(self.get('maxLines', 12) or 12))
        if not note:
            return None
        if self.get('verbose'):
            runtime.note(f'[外号对照] 本轮加了 {note.count("·")} 行')
        return self.volatile_hint(runtime, note)

    def _learn(self, sender: str, text: str, runtime) -> None:
        """可选：从"我叫X"里自动记外号（默认关，免得把玩笑话存成外号）。"""
        if not sender or not text:
            return
        for match in LEARN_RE.finditer(text):
            alias = match.group(1).strip()
            if alias and alias.isdigit():
                continue
            if self.add_alias(sender, alias) and self.get('verbose'):
                runtime.note(f'[外号对照] 记下 {sender} 的外号：{alias}')

    # ── 口令（管理员/主人维护外号表）─────────────────────────────────────────
    def on_ingest(self, message, runtime):
        if not self.get('enabled') or message.get('notice'):
            return None
        match = COMMAND_RE.match(str(message.get('text') or ''))
        if not match:
            return None
        if message.get('kind') != 'private':
            # 口令只在私聊生效：群里有人打这两个字不该让机器人接话
            return None
        if not self._is_keeper(message, runtime):
            return '外号表只有管理员能改。'
        rest = (match.group('rest') or '').strip()
        table = self.table()
        if not rest or rest in {'列表', '看', '表'}:
            if not table:
                return '外号表还是空的。可以发「外号 加 123456 某某大王」。'
            lines = [f"QQ:{uid} → {'、'.join(words)}" for uid, words in list(table.items())[:20]]
            return '外号对照表：\n' + '\n'.join(lines)
        action, _, body = rest.partition(' ')
        parts = body.split()
        if action in {'加', '添加', '记'}:
            if len(parts) < 2:
                return '要按 QQ 号记外号，例如「外号 加 123456 某某大王」。'
            uid, alias = parts[0], ' '.join(parts[1:])
            if not uid.isdigit():
                return '要按 QQ 号记外号，例如「外号 加 123456 某某大王」。'
            return (f'已记下：QQ:{uid} 也叫「{alias}」' if self.add_alias(uid, alias)
                    else f'没记（{alias} 可能已经在了，或者格式不对）')
        if action in {'删', '删除', '去掉'} and len(parts) >= 1:
            uid = parts[0]
            alias = ' '.join(parts[1:]) if len(parts) > 1 else ''
            if alias:
                return (f'已删掉：QQ:{uid} 的「{alias}」' if self.remove_alias(uid, alias)
                        else '没找到这条外号。')
            if uid in table:
                table.pop(uid, None)
                self.save_table(table)
                return f'已删掉 QQ:{uid} 的全部外号。'
            return '没找到这个 QQ 号的外号。'
        return '用法：外号 / 外号 加 <QQ号> <外号> / 外号 删 <QQ号> [外号]'

    def _is_keeper(self, message, runtime) -> bool:
        if message.get('kind') != 'private':
            return False
        sender = str(message.get('sender_id') or '')
        control = getattr(getattr(runtime, 'plugins', None), '_plugins', {}).get('pc_control')
        if control is None:
            return True
        return bool(control.is_admin(message))
