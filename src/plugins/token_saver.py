"""token_saver.py - Token 节流：只做减法的省 token 插件。

移植自 QQ Agent 社区插件 ``token-saver`` v1.0.0（来自 kondius.cn/qq-agent/plugin-market/）。
原版三件事：① 历史滑窗裁剪、超出部分压成一行摘要；② 工具描述瘦身；③ 截断过长工具结果。
本运行时**没有工具定义**，所以 ② 不适用；③ 换成等价物：**截断历史里过长的单条内容**
（转发摘要、图片描述、网页正文这些每一轮都要重发的长文本）。

规则逐条照搬，包括几个刻意保留的细节：

- 本来就不长（``len <= keep + 2``）就不折腾；
- 摘要**只截取原文**，不生成新内容（window 每条取前若干字；aggressive 只留条数）；
- 摘要按 **user** 身份插在保留的旧消息之后、最近若干条之前；
- 算下来**没省到字符就退化成"只删不留摘要"**（原版踩过这个坑：预算太小、单条又短时，
  插摘要反而更费，白白丢了上下文）；
- 任何一步出错都静默放行 —— 省 token 不能导致回复发不出去。
"""

from __future__ import annotations

import re

from . import PluginBase, PluginMeta, FieldSchema

MODES = ("off", "window", "aggressive")
PROTECTED_ROLES = {"system", "tool"}      # 系统提示词与工具结果不参与压缩（原版同款保护）
DIGEST_PREFIX = "【较早对话摘要】"
DIGEST_OMIT = "【较早对话已压缩】省略了 {count} 条更早的消息。"
TRUNCATE_NOTE = "\n…（内容过长，已省略 {omitted} 字符）"
_WS_RE = re.compile(r"\s+")


def clamp(value, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def text_of(message) -> str:
    """取一条消息的可读文本（只读，不改）。``content`` 可能是字符串或分段列表。"""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [str(part.get("text") or "") for part in content
                 if isinstance(part, dict) and part.get("type") in (None, "text")]
        return "\n".join(p for p in parts if p)
    return "" if content is None else str(content)


def truncate_history_content(messages, limit: int) -> int:
    """把历史里过长的单条截断（最近一条 = 当前这轮，永不截断）。返回省下的字符数。"""
    if limit <= 0 or not isinstance(messages, list):
        return 0
    saved = 0
    for message in messages[:-1]:
        if not isinstance(message,dict) or message.get('role') in PROTECTED_ROLES:
            continue
        text = text_of(message)
        if len(text) <= limit:
            continue
        omitted = len(text) - limit
        note = TRUNCATE_NOTE.format(omitted=omitted)
        if isinstance(message.get("content"), str):
            message["content"] = text[:limit] + note
        elif isinstance(message.get("content"), list):
            message["content"] = [{"type": "text", "text": text[:limit] + note}]
        else:
            continue
        saved += omitted - len(note)
    return max(0, saved)


def trim_history(messages, *, mode: str, keep: int, budget: int) -> tuple[int, str]:
    """就地压缩历史，返回 ``(省下的字符数, 做了什么)``。

    与 JS 原版一致：window 生成摘要；aggressive 只留条数；省不到字符就退化成只删。
    """
    if mode not in MODES or mode == "off" or not isinstance(messages, list):
        return 0, ""
    if len(messages) <= keep + 2:
        return 0, ""
    cut = len(messages) - keep
    head = messages[:cut]
    deletable = [m for m in head if isinstance(m, dict) and m.get("role") not in PROTECTED_ROLES]
    must_keep = [m for m in head if isinstance(m, dict) and m.get("role") in PROTECTED_ROLES]
    if not deletable:
        return 0, ""

    before = sum(len(text_of(m)) for m in head)
    if mode == "aggressive":
        digest = DIGEST_OMIT.format(count=len(deletable))
    else:
        per_line = max(20, budget // max(1, len(deletable)))
        lines = []
        for message in deletable:
            body = _WS_RE.sub(" ", text_of(message)).strip()
            if not body:
                continue
            who = "机器人" if message.get("role") == "assistant" else "群友"
            clipped = f"{body[:per_line]}…" if len(body) > per_line else body
            lines.append(f"{who}: {clipped}")
        digest = (DIGEST_PREFIX + " / ".join(lines)) if lines else DIGEST_OMIT.format(count=len(deletable))
    if len(digest) > budget:
        digest = digest[:budget] + "…"

    rebuilt = must_keep + [{"role": "user", "content": digest}] + messages[cut:]
    # 省下的字符数只能比较**被替换掉的那一段**：保留的最近 N 条在前后都在，
    # 把它们算进来会让 saved 常为负 → 默认档（滑窗 + 一行摘要）永远退化成"只删不留摘要"，
    # 用户看到长对话早期上下文被直接删掉、说好的一行摘要却没出现。
    kept_chars = sum(len(text_of(m)) for m in must_keep)
    saved = before - (kept_chars + len(digest))
    if saved > 0:
        messages[:] = rebuilt
        return saved, f"{len(head)} 条 → 1 行摘要（保留最近 {keep} 条）"

    # 退化路径：摘要本身太占地方时就只删不留（原版实测踩过的坑）
    fallback = must_keep + messages[cut:]
    fb_saved = sum(len(text_of(m)) for m in messages) - sum(len(text_of(m)) for m in fallback)
    if fb_saved > 0:
        messages[:] = fallback
        return fb_saved, f"省略 {len(deletable)} 条更早消息（没插摘要）"
    return 0, ""


class TokenSaver(PluginBase):
    META = PluginMeta(
        id='token_saver', name='Token 节流', category='utility', enabled_by_default=True,
        description=('移植自社区插件 token-saver，并按本项目的真机数据加强：① 历史滑窗裁剪，更早的内容压成'
                     '一行摘要（只截取原文，不生成新内容）；② 截断历史里过长的单条内容（转发摘要/图片描述/'
                     '网页正文这类每轮都要重发的长文本）；③ 删掉内容完全重复的轮次；④ 整段对话超过字数上限时'
                     '从最早的轮次开始删（system 与当前这轮永不删除）。实测单轮 prompt 平均 1897 token、'
                     '输出只有 25 token，所以这里省的每一字都直接省钱。全程只做减法，出错静默放行。'),
        config_schema={
            'enabled': FieldSchema('bool', '启用节省'),
            'historyMode': FieldSchema('select', '历史消息处理', options=[
                {'label': '不处理', 'value': 'off'},
                {'label': '滑窗 + 一行摘要（推荐）', 'value': 'window'},
                {'label': '更激进（只留条数与总字数）', 'value': 'aggressive'}]),
            'keepRecentMessages': FieldSchema('int', '保留最近消息条数', min=4, max=60),
            'maxDigestChars': FieldSchema('int', '历史摘要字数上限', min=100, max=4000),
            'maxContentChars': FieldSchema('int', '单条历史内容字数上限（0=不截断）', min=0, max=40000),
            'maxPromptChars': FieldSchema('int', '整段对话字数上限（0=不限制）', min=0, max=60000,
                                          description='从最早的轮次开始删；人设（system）与当前这轮永不删除。'
                                                      '默认 4500 字，约 3000 token。'),
            'dedupeDuplicates': FieldSchema('bool', '删掉完全重复的对话轮次',
                                            description='群里连发同一句、或背景与历史重复时很常见。'),
            'verbose': FieldSchema('bool', '在控制台记录每次省下多少'),
            'historyMediaChars':FieldSchema('int','历史图片 / 转发描述最多字数',min=80,max=2000,description='只压缩再次发送的旧媒体描述。当前图片和转发正文、人设、磁盘原始记录保留。'),
        },
        default_settings={'enabled': True, 'historyMode': 'window', 'keepRecentMessages': 10,
                          'maxDigestChars': 400, 'maxContentChars': 1200, 'maxPromptChars': 4500,
                          'dedupeDuplicates': True, 'verbose': True,'historyMediaChars':300})

    def _record(self, saved_chars: int, what: str) -> None:
        """把省下的字数累计到今天（插件状态文件），便于用户看到实际效果。"""
        if saved_chars <= 0:
            return
        try:
            from datetime import date
            stats = self.load_state('token_stats.json', {})
            if not isinstance(stats, dict):
                stats = {}
            today = date.today().isoformat()
            row = stats.get(today) if isinstance(stats.get(today), dict) else {}
            row['chars'] = int(row.get('chars') or 0) + int(saved_chars)
            row['rounds'] = int(row.get('rounds') or 0) + 1
            row['last'] = str(what)[:80]
            stats[today] = row
            self.save_state('token_stats.json', stats)
        except Exception:
            pass

    def stats(self) -> dict:
        """今天省下的字数（供控制台/插件卡片展示）。"""
        try:
            from datetime import date
            stats = self.load_state('token_stats.json', {})
            row = stats.get(date.today().isoformat()) if isinstance(stats, dict) else None
            return row if isinstance(row, dict) else {}
        except Exception:
            return {}

    def on_pre_ai(self, message, conversation, runtime):
        if not self.get('enabled') or not isinstance(conversation, list):
            return None
        verbose = bool(self.get('verbose'))
        notes = []
        total = int((message or {}).get('_context_saved_chars',0))
        if total:notes.append(f'旧媒体描述压缩 {total} 字')
        try:
            cut = truncate_history_content(
                conversation, clamp(self.get('maxContentChars', 1200), 0, 40000, 1200))
            if cut:
                total += cut
                notes.append(f'截断过长内容 {cut} 字')
            saved, what = trim_history(
                conversation,
                mode=str(self.get('historyMode', 'window') or 'window'),
                keep=clamp(self.get('keepRecentMessages', 10), 4, 60, 10),
                budget=clamp(self.get('maxDigestChars', 400), 100, 4000, 400),
            )
            if saved:
                total += saved
                notes.append(f'{what}（{saved} 字）')
            if self.get('dedupeDuplicates', True):
                from ..token_saver_util import dedupe_conversation
                removed = dedupe_conversation(conversation)
                if removed:
                    notes.append(f'删掉 {removed} 条重复轮次')
            budget_chars = clamp(self.get('maxPromptChars', 4500), 0, 60000, 4500)
            if budget_chars:
                from ..token_saver_util import cap_total_chars
                capped = cap_total_chars(conversation, budget_chars)
                if capped:
                    total += capped
                    notes.append(f'总量裁剪 {capped} 字')
            if total or notes:
                self._record(total, '；'.join(notes))
            if verbose and notes:
                runtime.note('[Token 节流] ' + '；'.join(notes))
        except Exception as exc:                       # 省 token 不能挡住回复
            if verbose:
                runtime.note(f'[Token 节流] 本轮跳过（{type(exc).__name__}）')
        return None
