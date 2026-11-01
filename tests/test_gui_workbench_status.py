"""智能中心状态：端口必须来自在线账号/qq-bridge 配置，不能写死 3072/3000。"""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication
    HAVE_QT = True
except Exception:                    # pragma: no cover - 环境缺 PyQt6 时跳过
    HAVE_QT = False

from src.gui import workbench

_APP = None


def _app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def _spin(predicate, timeout=8.0):
    app = _app()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


def _settle(tab):
    _spin(lambda: not tab._probe_running and not workbench._PROBES)
    tab.close()
    tab.deleteLater()
    _app().processEvents()


class StatusTargetTests(unittest.TestCase):
    def test_url_port_reads_ws_http_and_bare_host(self):
        self.assertEqual(workbench._url_port("ws://127.0.0.1:2947"), 2947)
        self.assertEqual(workbench._url_port("http://127.0.0.1:3000/"), 3000)
        self.assertEqual(workbench._url_port("127.0.0.1:3100"), 3100)
        self.assertEqual(workbench._url_port(10529), 10529)
        self.assertEqual(workbench._url_port("http://127.0.0.1"), 0)
        self.assertEqual(workbench._url_port(""), 0)

    def test_targets_use_the_online_owner_account_ports(self):
        config = {
            "ownerQQ": 2019117802,
            "dsh": {"baseUrl": "http://127.0.0.1:10529"},
            "consolePort": 3100,
        }
        accounts = [
            {"uin": "323485551", "online": True, "ws_port": 14025, "http_port": 0},
            {"uin": "2019117802", "online": True, "ws_port": 2947, "http_port": 3001},
        ]
        targets = {item["key"]: item for item in workbench._status_targets(config, accounts)}
        self.assertEqual(targets["onebot"]["port"], 2947)
        self.assertEqual(targets["onebot"]["label"], "OneBot 2947")
        self.assertEqual(targets["http"]["port"], 3001)
        self.assertEqual(targets["http"]["label"], "OneBot HTTP 3001")
        self.assertEqual(targets["dsh"]["label"], "DSH 10529")
        self.assertEqual(targets["bridge"]["label"], "qqbridge 3100")

    def test_offline_accounts_are_unconfigured_not_offline(self):
        config = {
            "ownerQQ": 2019117802,
            "dsh": {"baseUrl": "http://127.0.0.1:10529"},
            "consolePort": 3100,
        }
        accounts = [
            {"uin": "2019117802", "online": False, "ws_port": 2947, "http_port": 3001},
            {"uin": "323485551", "online": False, "ws_port": 14025, "http_port": 0},
        ]
        targets = {item["key"]: item for item in workbench._status_targets(config, accounts)}
        self.assertEqual(targets["onebot"]["port"], 0)
        self.assertEqual(targets["onebot"]["label"], "OneBot")
        self.assertEqual(targets["http"]["port"], 0)
        self.assertEqual(targets["http"]["label"], "OneBot HTTP")
        self.assertEqual(targets["dsh"]["port"], 10529)
        self.assertEqual(targets["bridge"]["port"], 3100)

    def test_missing_bridge_config_marks_dsh_and_bridge_unconfigured(self):
        targets = {item["key"]: item for item in workbench._status_targets({}, [])}
        self.assertEqual(targets["dsh"]["port"], 0)
        self.assertEqual(targets["dsh"]["label"], "DSH")
        self.assertEqual(targets["bridge"]["port"], 0)
        self.assertEqual(targets["bridge"]["label"], "qqbridge")

    def test_manager_reader_uses_account_engine_ports(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = root / "gateway.sqlite3"
            con = sqlite3.connect(db)
            try:
                con.execute("create table accounts(id text primary key, config text)")
                con.execute(
                    "insert into accounts values (?, ?)",
                    ("acc-1", json.dumps({
                        "label": "我的 QQ（4）",
                        "expected_uin": "2019117802",
                        "engine": "snowluma",
                        "injected_pid": 12345,
                    }, ensure_ascii=False)))
                con.commit()
            finally:
                con.close()
            engine = root / "accounts" / "acc-1" / "engine"
            engine.mkdir(parents=True)
            (engine / "connection.json").write_text(
                json.dumps({"ws_port": 2947}), encoding="utf-8")
            config_dir = engine / "runtime" / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "onebot.json").write_text(json.dumps({
                "networks": {
                    "httpServers": [{"port": 3000, "enable": True}],
                    "wsServers": [{"port": 2947, "enable": True}],
                }
            }), encoding="utf-8")
            with mock.patch.object(workbench, "_process_alive",
                                   side_effect=lambda pid: pid == 12345):
                rows = workbench._manager_accounts(db)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["label"], "我的 QQ（4）")
        self.assertEqual(row["uin"], "2019117802")
        self.assertEqual(row["ws_port"], 2947)
        self.assertEqual(row["http_port"], 3000)
        self.assertTrue(row["online"])

    def test_manager_reader_returns_empty_for_missing_db(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(workbench._manager_accounts(Path(folder) / "nope.sqlite3"), [])


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class DynamicLabelTests(unittest.TestCase):
    def setUp(self):
        self.app = _app()

    def test_unconfigured_onebot_shows_unconfigured_and_keeps_dsh_online(self):
        targets = [
            {"key": "onebot", "label": "OneBot", "port": 0},
            {"key": "http", "label": "OneBot HTTP", "port": 0},
            {"key": "dsh", "label": "DSH 10529", "port": 10529},
            {"key": "bridge", "label": "qqbridge 3100", "port": 3100},
        ]

        def probe(port):
            return port in (10529, 3100)

        with mock.patch.object(workbench, "_build_status_targets", return_value=targets), \
             mock.patch.object(workbench, "_port_open", probe):
            tab = workbench.WorkbenchTab()
            tab._status_timer.stop()
            try:
                self.assertTrue(_spin(lambda: not tab._probe_running
                                      and tab.labels["dsh"].text().endswith("在线")))
                self.assertEqual(tab.labels["onebot"].text(), "OneBot：未配置")
                self.assertEqual(tab.labels["http"].text(), "OneBot HTTP：未配置")
                self.assertEqual(tab.labels["dsh"].text(), "DSH 10529：在线")
                self.assertEqual(tab.labels["bridge"].text(), "qqbridge 3100：在线")
            finally:
                _settle(tab)

    def test_bridge_warning_no_longer_mentions_hardcoded_ports(self):
        targets = [
            {"key": "onebot", "label": "OneBot", "port": 0},
            {"key": "http", "label": "OneBot HTTP", "port": 0},
            {"key": "dsh", "label": "DSH", "port": 0},
            {"key": "bridge", "label": "qqbridge", "port": 0},
        ]
        with mock.patch.object(workbench, "_build_status_targets", return_value=targets), \
             mock.patch.object(workbench, "_port_open", lambda port: False):
            tab = workbench.WorkbenchTab()
            tab._status_timer.stop()
            try:
                self.assertTrue(_spin(lambda: not tab._probe_running))
                with mock.patch.object(workbench.dialogs, "tell") as tell:
                    tab._report_bridge_started()
                message = tell.call_args.args[2]
                self.assertNotIn("3072", message)
                self.assertNotIn("3000", message)
                self.assertIn("还没有可用账号", message)
            finally:
                _settle(tab)


if __name__ == "__main__":
    unittest.main()
