"""官方 QQ 的引用消息 / 聊天记录 / 结构化卡片：解析入站，并支持出站引用回复。

依据官方文档（2026-10 核对）：

- 事件 `GROUP_AT_MESSAGE_CREATE` 的 `message_type`：
  0=普通文本, 3=结构化卡片, 101=并行消息, **102=聊天记录**, **103=引用消息**；
- 正文在 `msg_elements`（可递归）里，每项有 `content` / `attachments` / `ark_data` / `author`；
- 卡片数据在 `ark_data`：`{prompt, ark_type, ark_name, fields:{title, desc, jump_url, source, ...}}`；
- `message_scene.ext` 里 `msg_idx=REFIDX_...` 是**这条消息的引用索引**，
  `ref_msg_idx=...` 表示这条消息本身是"引用"；
- 出站引用：`POST /v2/groups/{openid}/messages` 的 `message_reference: {message_id: <REFIDX>}`。

用户现象（2026-10-17）：群里有人**引用/转发一条消息**再 @ 机器人，
机器人看不到被引用的内容，只能瞎答。原因就是我们的解析器只取了顶层 `content`，
把 `msg_elements` 与 `ark_data` 整个丢掉了。
"""
from __future__ import annotations

# 引用消息 / 聊天记录正文的预算：太长会把提示词撑爆，也不能让用户觉得"没读到"，
# 所以截断并明确标注。
ELEMENT_TEXT_BUDGET = 1500


def _clip(text: str, budget: int = ELEMENT_TEXT_BUDGET) -> str:
    text = str(text or "").strip()
    if len(text) <= budget:
        return text
    return text[:budget].rstrip() + f"…（后面还有 {len(text) - budget} 字，已省略）"


def _ark_text(ark: dict) -> str:
    """把结构化卡片翻成一行可读文本（标题/描述/来源/链接/操作提示）。"""
    if not isinstance(ark, dict):
        return ""
    fields = ark.get("fields") if isinstance(ark.get("fields"), dict) else {}
    name = str(ark.get("ark_name") or ark.get("ark_type") or "卡片").strip()
    pieces = [f"[卡片:{name}]" if name else "[卡片]"]
    for key, label in (("title", "标题"), ("desc", "描述"), ("source", "来源"),
                       ("jump_url", "链接"), ("address", "位置"), ("nickname", "昵称")):
        value = fields.get(key)
        if isinstance(value, (int, float)):
            value = str(value)
        if isinstance(value, str) and value.strip():
            pieces.append(f"{label}：{value.strip()[:300]}")
    prompt = ark.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        pieces.append(f"提示：{prompt.strip()[:120]}")
    # 兜底：卡片字段里还有别的键（tag/tags/preview/source_logo…）时，给个键名清单，
    # 免得"卡片里明明有内容但机器人说什么都没有"。
    if len(pieces) == 1:
        keys = [str(key) for key in fields][:8]
        if keys:
            pieces.append("（卡片字段：" + "、".join(keys) + "）")
    return " ".join(pieces)


def render_elements(elements, *, depth: int = 0, default_kind=None, _budget=None) -> tuple[str, list, dict]:
    """把官方 ``msg_elements`` 递归翻成 (文本, 媒体, 标记)。

    标记里：
    - ``quoted``：出现过 message_type=103（引用消息）；
    - ``record``：出现过 102（聊天记录）；
    - ``card``：出现过 3 或带 ark_data；
    - ``nested_texts``：被引用/聊天记录里的原文，供上层做"引用内容"提示。
    """
    if not isinstance(elements, list):elements=[]
    if _budget is None:_budget=[200]
    texts: list[str] = []
    media: list = []
    marks = {"quoted": False, "record": False, "card": False, "nested_texts": []}
    # 事件级的 message_type 描述**整条**消息的类型（引用/聊天记录/卡片），
    # 元素本身通常不带 message_type —— 早期只按元素判断，结果引用前缀全丢了。
    try:
        top_kind = int(default_kind) if default_kind is not None else None
    except (TypeError, ValueError):
        top_kind = None
    if depth == 0 and top_kind == 103:
        marks["quoted"] = True
    elif depth == 0 and top_kind == 102:
        marks["record"] = True
    elif depth == 0 and top_kind == 3:
        marks["card"] = True
    for element in elements:
        if _budget[0]<=0:break
        _budget[0]-=1
        if not isinstance(element, dict) or depth > 4:
            continue
        kind = element.get("message_type")
        try:
            kind = int(kind) if kind is not None else None
        except (TypeError, ValueError):
            kind = None
        content = element.get("content")
        content = content if isinstance(content, str) else ""
        ark = element.get("ark_data")
        if kind == 103:
            marks["quoted"] = True
        elif kind == 102:
            marks["record"] = True
        elif kind == 3 or isinstance(ark, dict):
            marks["card"] = True
        inner_text, inner_media, inner_marks = render_elements(
            element.get("msg_elements"), depth=depth + 1, _budget=_budget)
        media.extend(inner_media)
        for key in ("quoted", "record", "card"):
            marks[key] = marks[key] or inner_marks[key]
        marks["nested_texts"].extend(inner_marks["nested_texts"])
        # 元素自带的附件（图片/语音）
        for item in element.get("attachments") if isinstance(element.get("attachments"), list) else []:
            url = str(item.get("url") or "") if isinstance(item, dict) else ""
            if url:
                # 标记 quoted：被引用/聊天记录里的图片只是上下文，不能当成本条消息的图片去识图
                media.append({"type": "image" if "image" in str(item.get("content_type") or "") else "file",
                              "url": url, "name": str(item.get("filename") or ""), "quoted": True})
        body = content.strip()
        author=element.get('author')
        if body and isinstance(author,dict):
            name=author.get('username') or author.get('nickname')
            if isinstance(name,str) and name.strip():body=name.strip()[:80]+'：'+body
        if body:
            marks["nested_texts"].append(body)
        prefix = ""
        if kind == 103:
            prefix = "[引用] "
        elif kind == 102:
            prefix = "[聊天记录] "
        card_text = _ark_text(ark) if isinstance(ark, dict) else ""
        pieces = [piece for piece in (prefix + body if body else "", card_text, inner_text) if piece]
        if pieces:
            texts.append(" ".join(pieces))
    body_text = _clip("\n".join(texts).strip()) if depth == 0 else "\n".join(texts).strip()
    if depth == 0 and body_text:
        if top_kind == 103 and not body_text.startswith("[引用]"):
            body_text = "[引用] " + body_text
        elif top_kind == 102 and not body_text.startswith("[聊天记录]"):
            body_text = "[聊天记录] " + body_text
    return body_text, media, marks


def scene_refs(data: dict) -> dict:
    """从 ``message_scene.ext`` 取引用相关信息。

    返回 ``{"msg_idx": 本消息引用索引, "is_quote": 是否引用了别的消息, "ref_msg_idx": 被引用项}``。
    出站引用要的正是 ``msg_idx``（官方叫 REFIDX），不是消息 id。
    """
    scene = data.get("message_scene") if isinstance(data, dict) else None
    ext = scene.get("ext") if isinstance(scene, dict) else None
    result = {"msg_idx": "", "is_quote": False, "ref_msg_idx": ""}
    for item in ext if isinstance(ext, list) else []:
        if not isinstance(item, str) or "=" not in item:
            continue
        key, _, value = item.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "msg_idx":
            result["msg_idx"] = value
        elif key == "ref_msg_idx":
            result["ref_msg_idx"] = value
            result["is_quote"] = True
    return result
