"""11292「沙箱环境不能访问此资源」的回归测试（真机 2026-10-16）。

现象（用户截图，1.6.19）：群里 @ 机器人 → 控制台「消息与回复」里那条是**待核实**，
「运行记录」写 `QQ 拒绝了这次发送（11292）：沙箱环境不能访问此资源` —— 回复生成了却发不出去，
用户看到的就是"没有输出"。

根因：设置了「使用沙箱环境」时 base 是 `sandbox.api.sgroup.qq.com`，而沙箱接口只能发到
开放平台里配置的沙箱群/测试用户；真实群会被拒（11292）。以前这条码没有中文提示，
也不会自动纠正。
"""
import asyncio
import json
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx  # noqa: E402

from src.qq_transports import (QQ_ERROR_HINTS, SANDBOX_REJECT_CODES, OfficialQQ,  # noqa: E402
                               QQSendError, describe_qq_error)

MESSAGE = {"kind": "group", "target": "1CBE87EF553193840AFFDCC788043580", "id": "MSG1"}


class SandboxHintTests(unittest.TestCase):
    def test_11292_has_an_actionable_hint(self):
        self.assertIn(11292, QQ_ERROR_HINTS)
        text = describe_qq_error(500, {"code": 11292, "message": "沙箱环境不能访问此资源"})
        self.assertIn("11292", text)
        self.assertIn("沙箱", text)
        self.assertIn("使用沙箱环境", text, "要告诉用户去设置里关掉沙箱勾选")
        self.assertIn(str(11292), str(SANDBOX_REJECT_CODES))

    def test_unknown_code_still_shows_the_original_message(self):
        text = describe_qq_error(500, {"code": 99999, "message": "未知原因"})
        self.assertIn("99999", text)
        self.assertIn("未知原因", text)


class SandboxFallbackTests(unittest.TestCase):
    """沙箱被拒时自动改用正式环境重发一次；正式环境也失败时两边原因都给出。"""

    def transport(self, sandbox=True, prod_ok=True):
        """构造一个"沙箱/正式"两套主机共用同一个假客户端"的传输。

        注意：回退时内部会 new 一个正式环境的 OfficialQQ，它自己会创建 httpx 客户端，
        所以这里 patch 模块里的 AsyncClient 工厂，让两个实例共用同一个 MockTransport。
        """
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if "sandbox.api.sgroup.qq.com" in str(request.url):
                return httpx.Response(500, json={"code": 11292,
                                                 "message": "沙箱环境不能访问此资源"})
            if prod_ok:
                return httpx.Response(200, json={"id": "ok"})
            return httpx.Response(500, json={"code": 11255, "message": "机器人不在该群"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        qq = OfficialQQ("appid", "secret", sandbox=sandbox)
        qq.token, qq.expires = "TOKEN", 10 ** 12         # 免去鉴权请求
        qq.http = client
        qq._calls = calls
        qq._factory = mock.patch("src.qq_transports.httpx.AsyncClient", return_value=client)
        qq._factory.start()
        self.addCleanup(qq._factory.stop)
        return qq

    def test_sandbox_rejection_is_retried_against_production(self):
        qq = self.transport(sandbox=True, prod_ok=True)
        result = asyncio.run(qq.send(MESSAGE, "在的"))
        self.assertEqual(result["id"], "1")
        self.assertTrue(any("sandbox.api.sgroup.qq.com" in url for url in qq._calls),
                        "第一次应当走沙箱环境")
        self.assertTrue(any("api.sgroup.qq.com" in url and "sandbox" not in url
                            for url in qq._calls), f"应当自动改用正式环境重发：{qq._calls}")
        self.assertTrue(getattr(qq, "sandbox_fallback_at", 0),
                        "要留下「曾经降级」的痕迹，供运行记录提示")

    def test_production_failure_reports_both_reasons(self):
        qq = self.transport(sandbox=True, prod_ok=False)
        with self.assertRaises(QQSendError) as caught:
            asyncio.run(qq.send(MESSAGE, "在的"))
        text = str(caught.exception)
        self.assertIn("沙箱环境拒绝了发送", text)
        self.assertIn("正式环境重试也失败", text)
        self.assertIn("11255", text)

    def test_production_mode_does_not_retry(self):
        qq = self.transport(sandbox=False, prod_ok=False)
        with self.assertRaises(QQSendError) as caught:
            asyncio.run(qq.send(MESSAGE, "在的"))
        # 正式环境本身没被沙箱拒绝：不该出现"重试"字样
        self.assertNotIn("正式环境重试也失败", str(caught.exception))
        self.assertEqual(len(qq._calls), 1, "正式模式下不该重复发送")

    def test_active_message_also_falls_back(self):
        qq = self.transport(sandbox=True, prod_ok=True)
        result = asyncio.run(qq.send_active("group", MESSAGE["target"], "主动一句"))
        self.assertEqual(result["id"], "active")
        self.assertTrue(any("api.sgroup.qq.com" in url and "sandbox" not in url
                            for url in qq._calls))

    def test_fallback_note_is_available_for_the_run_log(self):
        """走了降级要让用户在「运行记录」里看到，并给出可操作建议。"""
        qq = self.transport(sandbox=True, prod_ok=True)
        asyncio.run(qq.send(MESSAGE, "在的"))
        notes = qq.drain_notes()
        self.assertTrue(any("11292" in note and "取消勾选" in note for note in notes),
                        f"要给用户一条可操作的提示：{notes}")
        self.assertEqual(qq.drain_notes(), [], "提示只应取走一次")


if __name__ == "__main__":
    unittest.main()
