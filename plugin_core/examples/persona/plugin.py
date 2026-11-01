"""persona 示例：main priority 80 + exclusive，用 ctx.model.chat 生成回复。

可弃标：消息里没有配置的触发词时返回 None，让位给其它插件（或什么都不做）。
"""

from plugin_core.sdk import Action

DEFAULT_PERSONA = "你是一位简短、克制的助手。"
DEFAULT_KEYWORDS = ("你好", "谢谢", "在吗", "人设")


def setup(api) -> None:
    api.log.info("persona: load 阶段只登记声明")

    def keywords(ctx):
        values = ctx.cfg.get("keywords") or DEFAULT_KEYWORDS
        return tuple(str(item) for item in values if str(item).strip())

    def generate(ctx):
        persona = ctx.cfg.get("persona") or DEFAULT_PERSONA
        messages = [
            {"role": "system", "content": persona},
            {"role": "user", "content": ctx.message["text"]},
        ]
        reply = ctx.model.chat(messages, max_chars=500)
        turns = int(ctx.store.get("turns", 0)) + 1
        ctx.store.set("turns", turns)
        return [Action.send_text(ctx.session["key"], reply)]

    def bid(ctx):
        if not ctx.can("read_message"):
            return None
        text = (ctx.message["text"] or "").lower()
        if not text:
            return None
        for keyword in keywords(ctx):
            if keyword.lower() in text:
                return api.bid("main", 80, generate=generate, reason="命中触发词 %r" % keyword)
        # 接不住的话题主动弃标
        ctx.log.info("persona 弃标：%r 不含任何触发词" % ctx.message["text"])
        return None

    api.declare_inbound(bid)
