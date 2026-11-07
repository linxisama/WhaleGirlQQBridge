# -*- coding: utf-8 -*-
"""宿主脚本 tool.execute 参数位契约（dual-shape）测试。

0.4 的工具契约是 `execute(ctx, args)`（.tmp/qq-agent-run/src/tool-registry.js:14），
但 runtime/qqagent-host/host-one.mjs 过去写成了 `tool.execute(payload, ctx)` ——
顺序反了：新式扩展的 args 拿到的是 ctx 对象，`args.query` 恒 undefined，
wiki-query 一类 skill 因此永远回「缺少要查的名字」。

修法是 dual-shape：ctx 上摊平 payload（老式 `execute(payload)` 仍读得到字段），
第二参交真 payload（新式 `execute(ctx, args)` 拿得到参数）。本文件只读文本断言，
防止两份副本之一漏打补丁、或日后回退成单参写法。

摊平顺序还带信任边界（任务书 P1-2）：payload 在前，可信会话 ctx 与宿主回调在后覆盖。
模型实参因此伪造不了 chatKey/senderId/target，也换不掉 fetch/log/metadata；
业务参数仍从第二参和摊平后的 ctx 两头都读得到。
"""
from __future__ import annotations

import hashlib
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNTIME_HOST = REPO / "runtime" / "qqagent-host" / "host-one.mjs"
MARKET_HOST = REPO.parent / "插件市场" / "QQAgent兼容层" / "host-one.mjs"

# dual-shape 的两个判据：ctx 上摊平 payload，且第二参是真 payload。
CTX_FLATTENS_PAYLOAD = re.compile(r"^\s*const toolCtx\s*=[^\n]*\.\.\.payload[^\n]*$", re.M)
DUAL_SHAPE_CALL = re.compile(r"tool\.execute\(\s*toolCtx\s*,\s*payload\s*\)")
# 旧写法：第一参 payload、第二参 ctx 字面量。命中即回退。
OLD_SHAPE_CALL = re.compile(r"tool\.execute\(\s*payload\s*,\s*\{")
# 信任边界（P1-2）：toolCtx 里这些片段的先后顺序就是覆盖顺序，越靠后越不可被伪造。
TOOLCTX_ORDER = ("...payload", "...context", "signal: null", "log:", "metadata:", "fetch: toolFetch(")


def host_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class HostScriptContractMixin:
    """两份副本共用的三条断言；子类只需给 path。"""

    path: Path

    def test_flattens_payload_onto_ctx(self):
        match = CTX_FLATTENS_PAYLOAD.search(host_text(self.path))
        self.assertIsNotNone(
            match,
            "缺少 dual-shape 的 ctx 展开（要求 toolCtx = { ..., ...payload }）：%s" % self.path)

    def test_passes_real_payload_as_second_arg(self):
        self.assertTrue(
            DUAL_SHAPE_CALL.search(host_text(self.path)),
            "缺少 tool.execute(toolCtx, payload) 调用：%s" % self.path)

    def test_old_single_arg_shape_is_gone(self):
        self.assertFalse(
            OLD_SHAPE_CALL.search(host_text(self.path)),
            "仍是旧写法 tool.execute(payload, {ctx})，参数位错位会复现：%s" % self.path)

    def test_payload_is_reachable_from_both_positions(self):
        """args 与摊平后的 ctx 必须都能看到 payload 字段，否则老式/新式有一边坏掉。"""
        text = host_text(self.path)
        self.assertIn("...payload", text, "ctx 没有摊平 payload：%s" % self.path)
        self.assertIn("tool.execute(toolCtx, payload)", text,
                      "第二参不是 payload：%s" % self.path)

    def test_trusted_context_and_host_callbacks_win_over_payload(self):
        """P1-2：payload 先摊平，可信 ctx 与宿主回调在后覆盖，模型实参伪造不生效。"""
        line = [item for item in host_text(self.path).splitlines() if "const toolCtx =" in item][0]
        positions = []
        for token in TOOLCTX_ORDER:
            self.assertIn(token, line, "toolCtx 缺少 %s：%s" % (token, self.path))
            positions.append(line.index(token))
        self.assertEqual(
            positions, sorted(positions),
            "toolCtx 合并顺序不对，可信字段会被模型实参覆盖：%s\n%s" % (self.path, line))


class RuntimeHostScriptTest(HostScriptContractMixin, unittest.TestCase):
    path = RUNTIME_HOST

    def test_asset_exists(self):
        self.assertTrue(RUNTIME_HOST.is_file(), "宿主脚本资产缺失：%s" % RUNTIME_HOST)


class MarketCopyHostScriptTest(HostScriptContractMixin, unittest.TestCase):
    path = MARKET_HOST

    def setUp(self):
        if not MARKET_HOST.is_file():
            self.skipTest("插件市场副本不存在（%s），跳过" % MARKET_HOST)


class TwoCopiesConsistencyTest(unittest.TestCase):
    """两份 host-one.mjs 必须逐字节一致（WP9 的 compat_dir 会指向市场副本）。"""

    def setUp(self):
        if not MARKET_HOST.is_file():
            self.skipTest("插件市场副本不存在（%s），跳过" % MARKET_HOST)

    def test_same_sha256(self):
        runtime_bytes = RUNTIME_HOST.read_bytes()
        market_bytes = MARKET_HOST.read_bytes()
        self.assertEqual(
            hashlib.sha256(runtime_bytes).hexdigest(),
            hashlib.sha256(market_bytes).hexdigest(),
            "两份 host-one.mjs 内容不一致：%s vs %s" % (RUNTIME_HOST, MARKET_HOST))

    def test_same_line_count(self):
        self.assertEqual(
            len(RUNTIME_HOST.read_text(encoding="utf-8").splitlines()),
            len(MARKET_HOST.read_text(encoding="utf-8").splitlines()),
            "两份 host-one.mjs 行数不一致：%s vs %s" % (RUNTIME_HOST, MARKET_HOST))


if __name__ == "__main__":
    unittest.main()
