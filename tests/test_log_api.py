"""W13-d 日志三端点用例：/api/logs、/api/logs/export、/api/logs/client。

真的起 ASGI（TestClient 而不是直接调函数），所以鉴权、状态码、JSON 形状都是端到端
看到的；数据根一律指向 tempfile，绝不碰仓库真实 data/。
"""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src import log_api  # noqa: E402
from src.app_log import MESSAGE_MAX, DETAIL_MAX, MirrorHandler  # noqa: E402

ENTRY_KEYS = {"id", "ts", "level", "source", "message", "detail", "code", "extra"}
ATTACHMENT = re.compile(r'^attachment; filename="app-log-\d{8}-\d{6}\.json"$')
PAST = "2000-01-01T00:00:00+08:00"
FUTURE = "2099-01-01T00:00:00+08:00"


class LogApiHarness(unittest.TestCase):
    """每个用例一个 tempfile 数据根 + 一个挂了 /api/logs 的裸 FastAPI。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="w13d-api-")
        self.tmp = Path(self._tmp.name)
        self.data = self.tmp / "data"
        self.app = FastAPI()
        self.app.state.bot = SimpleNamespace(data_root=str(self.data), root=str(self.tmp))
        log_api.register(self.app)
        self.client = TestClient(self.app)
        self.log = log_api.app_log_for(self.app)

    def tearDown(self):
        self._tmp.cleanup()

    def record(self, level, source, message, **kw):
        return self.log.record(level, source, message, **kw)

    def three(self):
        self.record("info", "qq", "连接已建立")
        self.record("warning", "extension", "插件加载慢了一点点")
        self.record("error", "import", "导入失败：access-control", code="bad-pack")


class TailEndpointTest(LogApiHarness):
    def test_entries_are_newest_first_and_keep_the_frozen_shape(self):
        self.three()
        body = self.client.get("/api/logs").json()
        self.assertTrue(body["ok"])
        self.assertEqual([e["message"] for e in body["entries"]],
                         ["导入失败：access-control", "插件加载慢了一点点", "连接已建立"])
        for entry in body["entries"]:
            self.assertEqual(set(entry), ENTRY_KEYS)

    def test_level_accepts_multiple_values(self):
        self.three()
        body = self.client.get("/api/logs", params={"level": "error,warning"}).json()
        self.assertEqual({e["level"] for e in body["entries"]}, {"error", "warning"})
        self.assertEqual(body["total"], 2)

    def test_source_filter(self):
        self.three()
        body = self.client.get("/api/logs", params={"source": "qq"}).json()
        self.assertEqual([e["source"] for e in body["entries"]], ["qq"])

    def test_query_matches_message_detail_and_code(self):
        self.record("error", "qq", "第一条", detail="第二段里有关键词 needle")
        self.record("error", "qq", "第三条 needle 在摘要里")
        self.record("error", "qq", "第四条", code="needle")
        self.record("info", "qq", "无关的一条")
        body = self.client.get("/api/logs", params={"query": "needle"}).json()
        self.assertEqual(body["total"], 3)
        self.assertTrue(all("needle" in json.dumps(e, ensure_ascii=False)
                            for e in body["entries"]))

    def test_since_keeps_only_newer_entries(self):
        self.three()
        self.assertEqual(self.client.get("/api/logs", params={"since": PAST}
                                         ).json()["total"], 3)
        self.assertEqual(self.client.get("/api/logs", params={"since": FUTURE}
                                         ).json()["total"], 0)

    def test_limit_caps_payload_and_reports_total(self):
        for index in range(5):
            self.record("info", "qq", "第 %d 条" % index)
        body = self.client.get("/api/logs", params={"limit": 2}).json()
        self.assertEqual(len(body["entries"]), 2)
        self.assertEqual(body["total"], 5)
        self.assertTrue(body["truncated"])
        full = self.client.get("/api/logs", params={"limit": 50}).json()
        self.assertEqual(len(full["entries"]), 5)
        self.assertFalse(full["truncated"])

    def test_illegal_limit_falls_back_and_cap_is_2000(self):
        self.assertEqual(log_api._limit("abc", log_api.DEFAULT_LIMIT), 200)
        self.assertEqual(log_api._limit("", log_api.DEFAULT_LIMIT), 200)
        self.assertEqual(log_api._limit("0", log_api.DEFAULT_LIMIT), 200)
        self.assertEqual(log_api._limit("-9", log_api.EXPORT_LIMIT), 1000)
        self.assertEqual(log_api._limit("99999", log_api.DEFAULT_LIMIT), 2000)
        self.three()
        self.assertEqual(len(self.client.get("/api/logs", params={"limit": "abc"}
                                            ).json()["entries"]), 3)
        self.assertEqual(len(self.client.get("/api/logs", params={"limit": "99999"}
                                            ).json()["entries"]), 3)


class ExportEndpointTest(LogApiHarness):
    def test_content_disposition_filename(self):
        self.three()
        response = self.client.get("/api/logs/export")
        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.headers["content-disposition"], ATTACHMENT)

    def test_export_payload_shape_and_order(self):
        self.three()
        body = self.client.get("/api/logs/export").json()
        self.assertEqual(set(body), {"version", "exported_at", "filters", "count",
                                     "entries"})
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["count"], len(body["entries"]))
        self.assertEqual(body["count"], 3)
        self.assertEqual(body["entries"][0]["message"], "导入失败：access-control")
        self.assertIn("limit", body["filters"])

    def test_export_applies_filters_and_default_limit(self):
        self.three()
        body = self.client.get("/api/logs/export", params={"level": "error"}).json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["filters"]["level"], "error")
        self.assertEqual(body["filters"]["limit"], 1000)

    def test_export_limit_is_honoured_and_capped(self):
        for index in range(4):
            self.record("info", "qq", "第 %d 条" % index)
        self.assertEqual(self.client.get("/api/logs/export", params={"limit": 2}
                                         ).json()["count"], 2)
        body = self.client.get("/api/logs/export", params={"limit": "99999"}).json()
        self.assertEqual(body["filters"]["limit"], 2000)
        self.assertEqual(body["count"], 4)

    def test_head_is_answered(self):
        self.three()
        response = self.client.head("/api/logs/export")
        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.headers["content-disposition"], ATTACHMENT)


class ClientEndpointTest(LogApiHarness):
    def test_post_writes_into_the_same_store(self):
        response = self.client.post("/api/logs/client", json={
            "level": "error", "source": "gui", "message": "前端自己报的一条",
            "detail": "多行细节", "code": "ui-crash"})
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["id"]), 16)
        tail = self.client.get("/api/logs", params={"source": "gui"}).json()
        self.assertEqual(tail["entries"][0]["id"], body["id"])
        self.assertEqual(tail["entries"][0]["detail"], "多行细节")
        self.assertEqual(tail["entries"][0]["code"], "ui-crash")

    def test_empty_message_is_400(self):
        for payload in ({"level": "error", "message": ""}, {"level": "error"},
                        {"message": "   "}):
            response = self.client.post("/api/logs/client", json=payload)
            self.assertEqual(response.status_code, 400, payload)
            self.assertEqual(response.json()["ok"], False)
            self.assertEqual(response.json()["code"], "bad_request")
        self.assertEqual(self.client.get("/api/logs").json()["total"], 0)

    def test_broken_json_body_is_400_not_500(self):
        response = self.client.post("/api/logs/client", content="不是 JSON",
                                    headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "bad_request")

    def test_illegal_level_falls_back_to_error_and_source_to_web(self):
        for payload, level, source in (
                ({"level": "nope", "source": "garbage", "message": "归一化"}, "error", "web"),
                ({"message": "缺字段"}, "error", "web"),
                ({"level": "DEBUG", "source": "QQ", "message": "大小写"}, "debug", "qq")):
            body = self.client.post("/api/logs/client", json=payload).json()
            stored = [e for e in self.client.get("/api/logs").json()["entries"]
                      if e["id"] == body["id"]][0]
            self.assertEqual((stored["level"], stored["source"]), (level, source), payload)

    def test_overlong_message_and_detail_are_clipped(self):
        body = self.client.post("/api/logs/client", json={
            "level": "info", "source": "web", "message": "长" * 900,
            "detail": "细" * 9000}).json()
        self.assertTrue(body["ok"])
        stored = [e for e in self.client.get("/api/logs").json()["entries"]
                  if e["id"] == body["id"]][0]
        self.assertLessEqual(len(stored["message"]), MESSAGE_MAX)
        self.assertLessEqual(len(stored["detail"]), DETAIL_MAX)


class AuthTest(LogApiHarness):
    def test_forwarded_header_rejected_on_all_three(self):
        for method, path, kwargs in (
                ("get", "/api/logs", {}),
                ("get", "/api/logs/export", {}),
                ("post", "/api/logs/client", {"json": {"message": "x"}})):
            response = getattr(self.client, method)(
                path, headers={"x-forwarded-for": "10.0.0.9"}, **kwargs)
            self.assertEqual(response.status_code, 403, path)
            self.assertEqual(response.json()["code"], "auth_denied", path)

    def test_non_loopback_client_rejected(self):
        remote = TestClient(self.app, client=("10.1.2.3", 5000))
        self.assertEqual(remote.get("/api/logs").status_code, 403)
        self.assertEqual(remote.get("/api/logs/export").status_code, 403)
        self.assertEqual(remote.post("/api/logs/client", json={"message": "x"}
                                     ).status_code, 403)

    def test_cross_origin_rejected_and_same_origin_allowed(self):
        evil = self.client.get("/api/logs", headers={"origin": "http://evil.example"})
        self.assertEqual(evil.status_code, 403)
        self.assertEqual(evil.json()["code"], "auth_denied")
        same = self.client.get("/api/logs", headers={"origin": "http://testserver"})
        self.assertEqual(same.status_code, 200)

    def test_rejected_client_post_writes_nothing(self):
        self.client.post("/api/logs/client", json={"message": "不该落库"},
                         headers={"forwarded": "for=10.0.0.9"})
        self.assertEqual(self.client.get("/api/logs").json()["total"], 0)


class WiringTest(LogApiHarness):
    def test_routes_are_registered_without_included_router(self):
        paths = [getattr(route, "path", None) for route in self.app.routes]
        self.assertNotIn(None, paths)
        for path in ("/api/logs", "/api/logs/export", "/api/logs/client"):
            self.assertIn(path, paths)

    def test_data_dir_override_works_without_a_bot(self):
        bare = FastAPI()
        log_api.register(bare, data_dir=str(self.tmp / "override"))
        client = TestClient(bare)
        self.assertEqual(client.get("/api/logs").json()["ok"], True)
        log_api.log_event(SimpleNamespace(data_root=str(self.tmp / "override")),
                          "info", "backend", "走 override 数据根")
        self.assertEqual(client.get("/api/logs").json()["total"], 1)

    def test_data_dir_falls_back_to_root_data(self):
        bot = SimpleNamespace(root=str(self.tmp / "rt"), data_root=None)
        self.assertEqual(log_api.data_dir_of(bot), str((self.tmp / "rt" / "data").resolve()))
        self.assertEqual(log_api.data_dir_of(SimpleNamespace(root=None, data_root=None)), "")

    def test_log_event_never_raises(self):
        with mock.patch.object(self.log, "record", side_effect=RuntimeError("磁盘炸了")):
            log_api.log_event(self.app, "error", "qq", "写不进去也不能影响主流程")
        with mock.patch.object(log_api, "app_log_for", side_effect=RuntimeError("没有数据根")):
            log_api.log_event(None, "error", "qq", "连 data_dir 都没有")
        self.assertTrue(True)


class CreateAppWiringTest(unittest.TestCase):
    """接线回归：真 create_app() 装配出来的客户端，GET /api/logs 必须 ok=true。"""

    def setUp(self):
        from src.qq_app import QQRuntime, create_app
        from tests.test_persona_files import build_root

        self._tmp = tempfile.TemporaryDirectory(prefix="w13d-wire-")
        self.root = build_root(self._tmp.name)
        self.bot = QQRuntime(root=self.root, db_path=self.root / "w13d.sqlite")
        self.app = create_app(self.bot)
        self.app.state.bot = self.bot
        self.client = TestClient(self.app)

    def tearDown(self):
        try:
            self.bot.db.close()
        except Exception:
            pass
        try:
            self._tmp.cleanup()
        except OSError:
            pass

    def test_create_app_serves_three_log_routes(self):
        paths = {getattr(route, "path", None) for route in self.app.routes}
        for path in ("/api/logs", "/api/logs/export", "/api/logs/client"):
            self.assertIn(path, paths)

    def test_key_events_land_in_the_same_store_the_endpoint_reads(self):
        log_api.log_event(self.bot, "error", "qq", "接线回归：一条错误事件")
        body = self.client.get("/api/logs").json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["entries"][0]["source"], "qq")
        self.assertTrue(self.client.head("/api/logs/export").status_code == 200)

    def test_lifespan_records_startup_and_shutdown(self):
        with TestClient(self.app) as client:
            live = client.get("/api/logs").json()
        self.assertEqual([e["message"] for e in live["entries"]][0], "控制台已启动")
        after = log_api.app_log_for(self.bot).tail(limit=2)
        self.assertEqual([e["message"] for e in after], ["控制台正在关闭", "控制台已启动"])

    def test_logging_handler_is_installed_once_per_app_log(self):
        from src.app_log import default_app_log, install_logging_handler
        app_log = default_app_log(log_api.data_dir_of(self.bot) or None)
        handlers = [h for h in __import__("logging").getLogger().handlers
                    if isinstance(h, MirrorHandler) and h.app_log is app_log]
        self.assertEqual(len(handlers), 1)
        install_logging_handler(app_log, level="WARNING")
        again = [h for h in __import__("logging").getLogger().handlers
                 if isinstance(h, MirrorHandler) and h.app_log is app_log]
        self.assertEqual(len(again), 1)


if __name__ == "__main__":
    unittest.main()
