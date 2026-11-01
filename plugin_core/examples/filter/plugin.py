"""filter 示例：出站中间件，priority 10 最先执行。

命中 cancel_words → 返回 None 取消发送并短路后续中间件；
命中 rewrite_map → 返回新 Draft 改写文本（改写结果不会再进管道一次）。
"""

from plugin_core.sdk import Draft

DEFAULT_CANCEL = ("广告",)
DEFAULT_REWRITE = {"傻": "聪明", "内测": "公测"}


def setup(api) -> None:
    api.log.info("filter: load 阶段只登记声明")

    def middleware(ctx, draft):
        text = draft.text or ""
        for word in ctx.cfg.get("cancel_words") or DEFAULT_CANCEL:
            if word and word in text:
                ctx.log.info("filter 取消发送：命中 %r" % word)
                return None
        mapping = ctx.cfg.get("rewrite_map") or DEFAULT_REWRITE
        new_text = text
        for bad, good in mapping.items():
            if bad and bad in new_text:
                new_text = new_text.replace(bad, str(good))
        if new_text != text:
            ctx.log.info("filter 改写：%r → %r" % (text, new_text))
            return Draft(draft.conversation, new_text, draft.media, "filter")
        return draft

    api.declare_outbound(middleware)
