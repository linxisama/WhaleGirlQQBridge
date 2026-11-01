"""统一的"这句话有没有点名机器人"判定（唤醒词 / @ / 戳一戳）。

为什么要有这个模块：真机上出现过"设了唤醒词却不触发"——因为两个插件对"点名"的定义不一致：

- ``chat_controls`` 认为"昵称（唤醒词）出现也算点名"；
- ``mention_gate`` 只看 ``message['at']``，于是群名单里的消息在**前一道闸门**就被丢了，
  唤醒词根本没机会生效。

把判定收成一处之后，两边共享同一套规则，以后改匹配方式只需要改这里。

匹配规则（比原来的 `alias in text` 宽松，覆盖真机上的常见输入）：

- 全角/半角统一（``ＮＦＫＣ``），大小写不敏感（英文昵称）；
- 忽略空格与常见标点：``老 鲸！`` 与 ``老鲸`` 等价；
- 唤醒词支持中文逗号/顿号/分号/换行/空白分隔；
- 单字唤醒词**仍然忽略**（太容易误触发），但会返回"被忽略"的清单，界面要提示出来。
"""
from __future__ import annotations

import re
import unicodedata

# 分隔符：中英文逗号、顿号、分号、换行、空白
_SPLIT = re.compile(r"[,，、;；\s]+")
# 匹配时忽略的标点与空白（只影响"包含判定"，不改变用户填的内容）
_IGNORE = re.compile(r"[\s\u3000~!@#$%^&*()\-_=+\[\]{}|\\:;\"'<>,.?/·~！@#￥%……&*（）—\-—+【】{}｜、；：""''《》？，。／]+")


def normalize(text) -> str:
    """把一段文本归一成"用于包含判定"的形式：全角转半角、小写、去空格与标点。"""
    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    return _IGNORE.sub("", value)


def split_aliases(raw) -> tuple[list[str], list[str]]:
    """把用户填的唤醒词拆成 (有效, 被忽略)。

    - 有效：归一后长度 ≥ 2 的条目（去重、保留用户原始写法）；
    - 被忽略：空串、单字条目（界面要显示出来，否则用户会以为设置没生效）。

    特例：如果按分隔符拆完**只得到单字**（例如用户写了 ``老 鲸`` 想表达"老鲸"），
    但整串归一后有 ≥2 字，就把整串当成一个唤醒词 —— 中文昵称里手滑打个空格很常见。
    """
    valid: list[str] = []
    ignored: list[str] = []
    seen: set[str] = set()
    for piece in _SPLIT.split(str(raw or "")):
        for candidate in (piece, _IGNORE.sub("", piece)):
            if not candidate:
                continue
            key = normalize(candidate)
            if not key:
                continue
            if len(key) < 2:
                if candidate not in ignored:
                    ignored.append(candidate)
                continue
            if key in seen:
                continue
            seen.add(key)
            valid.append(candidate.strip())
    if not valid and ignored:
        whole = _IGNORE.sub("", str(raw or "")).strip()
        if len(normalize(whole)) >= 2:
            return [whole], []
    return valid, ignored


def addressed(text, aliases, *, at: bool = False) -> tuple[bool, str]:
    """这句话有没有点名机器人？返回 (是否点名, 命中原因)。

    原因字符串直接可以显示给用户：``@`` 表示通过 @ 段、否则是命中的唤醒词、
    ``未命中`` 表示没点到名。
    """
    if at:
        return True, "@"
    haystack = normalize(text)
    if not haystack:
        return False, "未命中"
    valid, _ = split_aliases(aliases)
    for alias in valid:
        needle = normalize(alias)
        if needle and needle in haystack:
            return True, alias
    return False, "未命中"


def explain(text, aliases, *, at: bool = False) -> str:
    """给界面用的中文解释（"触发测试"那一行直接显示这句）。"""
    hit, why = addressed(text, aliases, at=at)
    valid, ignored = split_aliases(aliases)
    if hit:
        detail = "通过 @ 点名" if why == "@" else f"命中唤醒词「{why}」"
        return f"会回复：{detail}"
    tail = f"（有效唤醒词 {len(valid)} 个" + (f"，已忽略单字/空：{'、'.join(ignored)}" if ignored else "") + "）"
    return f"不会回复：这句话没有 @ 机器人，也没命中唤醒词{tail}"
