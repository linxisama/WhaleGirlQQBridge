"""hello 示例：最小可用的 main 竞标插件（priority 20）。

load 阶段只做声明；真正的回复在 enable 之后由主体调用。
"""

from plugin_core.sdk import Action

DEFAULT_REPLY = "你好，我是 hello 插件。"


def setup(api) -> None:
    api.log.info("hello: load 阶段只登记声明，不做对外副作用")

    def generate(ctx):
        reply = ctx.cfg.get("reply") or DEFAULT_REPLY
        hits = int(ctx.store.get("hits", 0)) + 1
        ctx.store.set("hits", hits)
        ctx.log.info("hello 第 %d 次命中：%r" % (hits, ctx.message["text"]))
        return [Action.send_text(ctx.session["key"], reply)]

    def bid(ctx):
        if not ctx.can("read_message"):
            return None
        return api.bid("main", 20, generate=generate, reason="命中任意消息")

    api.declare_inbound(bid)
    api.on("plugin.enabled", lambda ctx, payload: [Action.noop()])
