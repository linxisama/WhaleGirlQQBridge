"""身份保护：不许把底层模型/平台的名字当成自己的身份。

真机（2026-10-17）：群里有人问"你是谁啊"，机器人回 **"我是WorkBuddy。"** ——
WorkBuddy 是这台机器上跑的协作程序 / 中转站的名字，属于**人设被模型自我认知顶掉**。
只加一句系统提示并不可靠（模型仍可能照抄），所以这里再加一层**确定性的本地清理**：

- 命中"我是/我叫/身为 + 外部产品名"或"作为一个AI/语言模型/大模型"这类句子；
- 先把整句去掉（保留同一条回复里其它正常的话）；
- 如果去掉后什么都不剩，用一句人设里的中性话兜底（不编造事实，也不自称模型）。

这一层不额外调用模型，零延迟；系统提示里同步加"身份约束"（见 qq_app）。
"""
from __future__ import annotations

import re

# 外部产品名：底层模型、平台、代理软件。命中这些名字就算泄漏。
FOREIGN_NAMES = (
    "workbuddy", "chatgpt", "gpt-4", "gpt-3", "gpt4", "gpt3", "openai", "claude",
    "deepseek", "copilot", "gemini", "llama", "qwen", "通义千问", "文心一言",
    "kimi", "智谱", "glm-4", "moon shot", "moonshot", "月之暗面",
)

# "我是一个 AI / 语言模型 / 大模型 / 人工智能助手"
_AI_CLAIM = re.compile(
    r"(作为一个?|身为一个?|我是一?个?)\s*(ai|人工智能|语言模型|大模型|智能助手|ai助手|语言大模型)"
    r"|(我是|我叫|我的名字是)\s*([A-Za-z][A-Za-z0-9 .\-]{1,20})",
    re.IGNORECASE)

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?~…\n])")


def _allowed(names) -> set:
    """把人设名归一成可比对的集合（小写、去空白）。"""
    return {str(item or "").strip().lower() for item in (names or ()) if str(item or "").strip()}


def find_leaks(text: str, personas=()) -> list[str]:
    """返回命中的句子（空列表表示干净）。

    personas 是本轮可用的人设名（含 display_name）。第二分支匹配的是
    「我是/我叫 + ASCII 名字」，它本来是用来抓 "我是ChatGPT" 的；但同一个句式
    也是人设自报家门（"我是Alice"）。注释早就写明「只在名字不是人设时才算泄漏」，
    实现却把所有不超过 12 字的 ASCII 名字一律判泄漏 —— 于是人设自称被整句删掉。
    这里按注释的原意落地：命中名字属于本轮人设就放过。
    """
    allowed = _allowed(personas)
    leaks: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(str(text or "")):
        probe = sentence.strip()
        if not probe:
            continue
        lowered = probe.lower()
        if any(name in lowered for name in FOREIGN_NAMES):
            leaks.append(probe)
            continue
        match = _AI_CLAIM.search(probe)
        if match:
            # 名字在第 4 组。原来取 group(3)，那是「我是/我叫/我的名字是」这个动词，
            # 长度永远不超过 12，于是每个匹配都被判泄漏 —— 人设自称被整句删掉。
            name = (match.group(4) or "").strip()
            if name and name.lower() in allowed:
                continue          # 自报的是本轮人设名，不是泄漏
            if not name or len(name) <= 12:
                leaks.append(probe)
    return leaks


def scrub(text: str, fallback: str = "", personas=()) -> tuple[str, bool]:
    """去掉泄漏句。返回 ``(清理后的文本, 是否发生过清理)``。

    personas 原样传给 find_leaks：人设自称（"我是Alice"）不算泄漏。
    """
    source = str(text or "").strip()
    leaks = find_leaks(source, personas)
    if not leaks:
        return source, False
    kept = [sentence for sentence in _SENTENCE_SPLIT.split(source)
            if sentence.strip() and sentence.strip() not in leaks]
    cleaned = "".join(kept).strip()
    if not cleaned:
        return (fallback or "问这个干嘛，我就是我。"), True
    return cleaned, True
