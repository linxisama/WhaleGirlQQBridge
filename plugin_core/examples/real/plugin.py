"""real 示例：从产品现有 keyword_reply 行为抽出来的关键词回复。

与 D:/qq机器人5.0/src/plugins/keyword_reply.py 对齐的行为：
1. 规则来自配置的多行文本，每行 `关键词=回复`；
2. 大小写不敏感的子串匹配，从上到下第一条命中即返回，不做多规则拼接；
3. 插件被禁用时直接不参与仲裁。
"""

from plugin_core.sdk import Action

DEFAULT_RULES = """在吗=在的，有事直接说。
价格=报价请看群公告第一条。
帮助=发送「关键词=回复」即可配置。"""


def parse_rules(text):
    """把多行文本解析成 [(keyword, reply)]；跳过空行与坏行。"""
    pairs = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        keyword, _, reply = line.partition("=")
        keyword = keyword.strip()
        reply = reply.strip()
        if keyword and reply:
            pairs.append((keyword, reply))
    return pairs


def setup(api) -> None:
    api.log.info("real: load 阶段只登记声明")

    def generate(ctx, reply):
        hits = int(ctx.store.get("hits", 0)) + 1
        ctx.store.set("hits", hits)
        return [Action.send_text(ctx.session["key"], reply)]

    def bid(ctx):
        if not ctx.cfg.get("enabled", True):
            return None
        rules = parse_rules(ctx.cfg.get("rules") or DEFAULT_RULES)
        if not rules:
            return None
        text = (ctx.message["text"] or "").lower()
        for keyword, reply in rules:
            if keyword.lower() in text:
                return api.bid(
                    "main",
                    90,
                    generate=lambda inner: generate(inner, reply),
                    reason="关键词命中 %r" % keyword,
                )
        return None

    api.declare_inbound(bid)
