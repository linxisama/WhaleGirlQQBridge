"""W13a：src/app_log.py 的结构化日志内核测试（全部落在 tempfile，绝不碰仓库 data/）。"""
from __future__ import annotations

import json
import logging
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from src import app_log as app_log_mod
from src.app_log import (
    DEFAULT_LIMIT,
    LEVELS,
    LOG_DIR_PARTS,
    MAX_LIMIT,
    RING,
    AppLog,
    MirrorHandler,
    default_app_log,
    install_logging_handler,
    logs_dir,
    make_entry,
)

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 10, 26, 17, 5, 12, tzinfo=TZ)


class FakeClock:
    """可推进 / 可跳转的假时钟，用来造跨日与稳定时间戳。"""

    def __init__(self, start=None, step=timedelta(seconds=1)):
        self.now = start or T0
        self.step = step

    def __call__(self):
        current = self.now
        self.now = current + self.step
        return current

    def jump_to(self, moment):
        self.now = moment


class AppLogTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="app-log-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.dir = logs_dir(self.root)

    def make(self, **kw):
        return AppLog(self.root, **kw)

    def log_files(self):
        folder = logs_dir(self.root)
        if not folder.exists():
            return []
        return sorted(p.name for p in folder.glob("*.jsonl"))

    def read_file(self, name):
        text = (logs_dir(self.root) / name).read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]


class TestPathsAndEntries(AppLogTestCase):
    def test_logs_dir_uses_frozen_parts_and_creates_nothing(self):
        self.assertEqual(LOG_DIR_PARTS, ("qq-only", "logs"))
        resolved = logs_dir(str(self.root))
        self.assertEqual(resolved, self.root / "qq-only" / "logs")
        self.assertEqual(logs_dir(self.root), resolved)
        self.assertFalse(resolved.exists())

    def test_make_entry_shape_and_normalisation(self):
        entry = make_entry("ERROR", "qq", "boom\nsecond line", detail="d", code="E1",
                           extra={"k": "v"}, now=T0)
        self.assertEqual(
            tuple(entry), ("id", "ts", "level", "source", "message", "detail", "code", "extra")
        )
        self.assertEqual(entry["level"], "error")
        self.assertEqual(entry["ts"], "2026-10-26T17:05:12+08:00")
        self.assertEqual(entry["message"], "boom second line")
        self.assertEqual(len(entry["id"]), 16)
        int(entry["id"], 16)
        self.assertEqual(entry["extra"], {"k": "v"})
        self.assertNotEqual(entry["id"], make_entry("error", "qq", "boom", now=T0)["id"])


class TestRecordAndDisk(AppLogTestCase):
    def test_four_levels_land_in_ring_and_file(self):
        clock = FakeClock()
        app = self.make(clock=clock)
        for level in LEVELS:
            app.record(level, "qq", "level-" + level)
        self.assertEqual([e["level"] for e in app.tail()], list(reversed(LEVELS)))
        self.assertEqual(self.log_files(), ["app-20261026.jsonl"])
        rows = self.read_file("app-20261026.jsonl")
        self.assertEqual([r["level"] for r in rows], list(LEVELS))
        self.assertEqual(rows[0]["ts"], "2026-10-26T17:05:12+08:00")

    def test_invalid_level_falls_back_to_info(self):
        app = self.make()
        self.assertEqual(app.record("critical", "qq", "x")["level"], "info")
        self.assertEqual(app.record(None, "qq", "y")["level"], "info")

    def test_non_string_inputs_never_raise(self):
        app = self.make()
        entry = app.record(["bad"], None, 12345, detail=None, code=[1, 2], extra="nope")
        self.assertEqual(entry["level"], "info")
        self.assertEqual(entry["message"], "12345")
        self.assertEqual(entry["source"], "")
        self.assertEqual(entry["detail"], "")
        self.assertEqual(entry["code"], "[1, 2]")
        self.assertEqual(entry["extra"], {})

    def test_message_and_detail_are_capped(self):
        app = self.make()
        entry = app.record("error", "qq", "m" * 900, detail="d" * 9000)
        self.assertEqual(len(entry["message"]), 500)
        self.assertEqual(len(entry["detail"]), 8000)

    def test_record_exception_carries_traceback(self):
        app = self.make()
        try:
            raise ValueError("kaboom")
        except ValueError:
            entry = app.record_exception("qq", "发送失败", detail="超时")
        self.assertEqual(entry["level"], "error")
        self.assertIn("Traceback", entry["detail"])
        self.assertIn("ValueError: kaboom", entry["detail"])
        self.assertTrue(entry["detail"].startswith("超时"))
        explicit = app.record_exception("qq", "", exc=RuntimeError("直传"))
        self.assertIn("RuntimeError: 直传", explicit["detail"])
        self.assertIn("RuntimeError: 直传", explicit["message"])

    def test_record_exception_fills_code_from_exception(self):
        app = self.make()
        try:
            1 / 0
        except ZeroDivisionError as exc:
            entry = app.record_exception("qq", "算错了", exc=exc)
        self.assertEqual(entry["code"], "ZeroDivisionError")
        self.assertEqual(entry["message"], "算错了")
        self.assertIn("ZeroDivisionError: division by zero", entry["detail"])
        explicit = app.record_exception("qq", "算错了", ValueError("x"), code="E42")
        self.assertEqual(explicit["code"], "E42")
        self.assertEqual(app.record_exception("qq", "无异常上下文")["code"], "")

    def test_convenience_helpers_map_to_levels(self):
        app = self.make()
        self.assertEqual(app.error("qq", "e")["level"], "error")
        self.assertEqual(app.warning("qq", "w")["level"], "warning")
        self.assertEqual(app.info("qq", "i")["level"], "info")
        self.assertEqual(app.debug("qq", "d")["level"], "debug")


class TestTailFilters(AppLogTestCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.app = self.make(clock=self.clock)
        self.app.error("qq", "连接失败", detail="socket reset", code="E42")
        self.app.info("web", "启动完成", detail="boot ok")
        self.app.warning("extension", "插件版本过旧", code="W7")
        self.app.info("qq", "导入成功", detail="3 个包")
        self.app.debug("gui", "刷新列表")

    def messages(self, entries):
        return [e["message"] for e in entries]

    def test_tail_newest_first_and_default_limit(self):
        entries = self.app.tail()
        self.assertEqual(DEFAULT_LIMIT, 200)
        self.assertEqual(
            self.messages(entries),
            ["刷新列表", "导入成功", "插件版本过旧", "启动完成", "连接失败"],
        )

    def test_tail_level_single(self):
        entries = self.app.tail(level="error")
        self.assertEqual(self.messages(entries), ["连接失败"])

    def test_tail_level_multi(self):
        entries = self.app.tail(level="error,warning")
        self.assertEqual(self.messages(entries), ["插件版本过旧", "连接失败"])
        self.assertEqual(len(self.app.tail(level="ERROR, Warning")), 2)

    def test_tail_source(self):
        entries = self.app.tail(source="QQ")
        self.assertEqual(self.messages(entries), ["导入成功", "连接失败"])

    def test_tail_query_is_case_insensitive_over_message_detail_code_source(self):
        self.assertEqual(self.messages(self.app.tail(query="SOCKET")), ["连接失败"])
        self.assertEqual(self.messages(self.app.tail(query="e42")), ["连接失败"])
        self.assertEqual(self.messages(self.app.tail(query="EXTENSION")), ["插件版本过旧"])
        self.assertEqual(self.messages(self.app.tail(query="web")), ["启动完成"])

    def test_tail_limit(self):
        self.assertEqual(len(self.app.tail(limit=2)), 2)
        self.assertEqual(self.messages(self.app.tail(limit=2)), ["刷新列表", "导入成功"])
        self.assertEqual(len(self.app.tail(limit=0)), 1)
        self.assertEqual(len(self.app.tail(limit="nope")), 5)
        self.assertEqual(len(self.app.tail(limit=10 ** 6)), 5)

    def test_tail_since(self):
        since = "2026-10-26T17:05:15+08:00"
        self.assertEqual(self.messages(self.app.tail(since=since)), ["刷新列表", "导入成功"])
        self.assertEqual(len(self.app.tail(since="bad-input")), 5)

    def test_max_limit_constant(self):
        self.assertEqual(MAX_LIMIT, 2000)
        self.assertEqual(RING, 1000)


class TestRingAndRotation(AppLogTestCase):
    def test_ring_evicts_oldest_beyond_ring_size(self):
        app = self.make()
        for index in range(RING + 5):
            app.info("qq", "m" + str(index))
        kept = app.tail(limit=RING)
        self.assertEqual(len(kept), RING)
        messages = {e["message"] for e in kept}
        self.assertNotIn("m0", messages)
        self.assertNotIn("m4", messages)
        self.assertIn("m5", messages)
        self.assertIn("m" + str(RING + 4), messages)
        self.assertEqual(app.stats()["entries"], RING)

    def test_roll_creates_three_archives_and_drops_oldest(self):
        clock = FakeClock()
        app = self.make(roll_bytes=1, roll_keep=3, clock=clock)
        for index in range(5):
            app.info("qq", "m" + str(index))
        self.assertEqual(
            self.log_files(),
            [
                "app-20261026.1.jsonl",
                "app-20261026.2.jsonl",
                "app-20261026.3.jsonl",
                "app-20261026.jsonl",
            ],
        )
        everything = []
        for name in self.log_files():
            everything.extend(row["message"] for row in self.read_file(name))
        self.assertNotIn("m0", everything)
        self.assertEqual(self.read_file("app-20261026.jsonl")[0]["message"], "m4")

    def test_roll_keeps_single_entry_per_archive(self):
        app = self.make(roll_bytes=1, roll_keep=3, clock=FakeClock())
        app.info("qq", "a")
        app.info("qq", "b")
        self.assertEqual(self.read_file("app-20261026.1.jsonl")[0]["message"], "a")
        self.assertEqual(self.read_file("app-20261026.jsonl")[0]["message"], "b")

    def test_cross_day_starts_a_new_file(self):
        clock = FakeClock()
        app = self.make(clock=clock)
        app.info("qq", "day-one-1")
        app.info("qq", "day-one-2")
        clock.jump_to(datetime(2026, 10, 27, 0, 0, 5, tzinfo=TZ))
        app.info("qq", "day-two")
        self.assertEqual(
            self.log_files(), ["app-20261026.jsonl", "app-20261027.jsonl"]
        )
        self.assertEqual(len(self.read_file("app-20261026.jsonl")), 2)
        self.assertEqual(self.read_file("app-20261027.jsonl")[0]["message"], "day-two")

    def test_unwritable_directory_degrades_without_raising(self):
        app = self.make()
        with mock.patch.object(Path, "mkdir", side_effect=PermissionError("denied")):
            entry = app.record("error", "qq", "写盘失败也要留内存")
        self.assertEqual(entry["message"], "写盘失败也要留内存")
        self.assertEqual(app.stats()["entries"], 1)
        self.assertEqual(app.tail(limit=1)[0]["message"], "写盘失败也要留内存")
        self.assertGreaterEqual(app.write_failures, 1)
        self.assertEqual(self.log_files(), [])

    def test_bad_lines_on_disk_are_skipped(self):
        app = self.make(clock=FakeClock())
        app.error("qq", "good-one")
        app.error("qq", "good-two")
        path = logs_dir(self.root) / "app-20261026.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{broken json" + chr(10))
            handle.write(chr(10))
        entries = self.make().tail()  # 新实例：空环 → 走文件补齐这条路
        self.assertEqual([e["message"] for e in entries], ["good-two", "good-one"])

    def test_restart_reads_entries_back_from_file(self):
        first = self.make(clock=FakeClock())
        first.error("qq", "before-restart")
        second = self.make()
        self.assertEqual(second.stats()["entries"], 0)
        self.assertEqual([e["message"] for e in second.tail()], ["before-restart"])

    def test_thread_safety_smoke(self):
        app = self.make()

        def worker(tag):
            for index in range(50):
                app.info("qq", tag + "-" + str(index))

        threads = [threading.Thread(target=worker, args=("t" + str(i),)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(app.stats()["entries"], 200)
        self.assertEqual(len(app.tail(limit=MAX_LIMIT)), 200)


class TestExportClearStats(AppLogTestCase):
    def test_export_shape(self):
        app = self.make(clock=FakeClock())
        app.error("qq", "e1", code="E1")
        app.info("web", "i1")
        app.warning("qq", "w1")
        payload = app.export()
        self.assertEqual(
            sorted(payload), ["count", "entries", "exported_at", "filters", "version"]
        )
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["count"], 3)
        self.assertEqual(
            payload["filters"], {"level": "", "source": "", "query": "", "limit": 1000}
        )
        self.assertEqual([e["message"] for e in payload["entries"]], ["w1", "i1", "e1"])
        datetime.fromisoformat(payload["exported_at"])
        json.dumps(payload, ensure_ascii=False)

    def test_export_applies_filters(self):
        app = self.make(clock=FakeClock())
        app.error("qq", "e1")
        app.info("web", "i1")
        payload = app.export(level="error", source="qq", query="e1", limit=10)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(
            payload["filters"], {"level": "error", "source": "qq", "query": "e1", "limit": 10}
        )

    def test_clear_only_drops_memory(self):
        app = self.make(clock=FakeClock())
        app.error("qq", "keep-on-disk")
        app.error("qq", "keep-on-disk-2")
        self.assertEqual(app.clear(), 2)
        self.assertEqual(app.stats()["entries"], 0)
        self.assertEqual(self.log_files(), ["app-20261026.jsonl"])
        self.assertEqual(len(self.read_file("app-20261026.jsonl")), 2)
        self.assertEqual(app.clear(), 0)

    def test_stats_numbers(self):
        app = self.make(clock=FakeClock())
        app.error("qq", "e1")
        app.error("qq", "e2")
        app.info("web", "i1")
        stats = app.stats()
        self.assertEqual(
            sorted(stats), ["bytes", "entries", "files", "levels", "sources"]
        )
        self.assertEqual(stats["entries"], 3)
        self.assertEqual(stats["files"], 1)
        self.assertEqual(
            stats["levels"], {"debug": 0, "info": 1, "warning": 0, "error": 2}
        )
        self.assertEqual(stats["sources"], {"qq": 2, "web": 1})
        self.assertEqual(stats["bytes"], (logs_dir(self.root) / "app-20261026.jsonl").stat().st_size)
        self.assertGreater(stats["bytes"], 0)


class TestClearMarker(AppLogTestCase):
    """返工 1：clear() 之后 tail()/export() 不许把刚清掉的条目从磁盘读回来。"""

    def test_clear_then_tail_and_export_are_empty(self):
        app = self.make(clock=FakeClock())
        for index in range(3):
            app.error("qq", "gone-" + str(index))
        self.assertEqual(app.clear(), 3)
        self.assertEqual(app.tail(), [])
        self.assertEqual(app.export()["count"], 0)
        self.assertEqual(app.stats()["entries"], 0)

    def test_clear_then_tail_is_empty_in_same_second(self):
        app = self.make()  # 真实时钟：清空与写入同一秒,靠 id 过滤这条路（探针复现的那条）
        app.error("qq", "same-second-1")
        app.error("qq", "same-second-2")
        self.assertEqual(app.clear(), 2)
        self.assertEqual(app.tail(), [])

    def test_clear_keeps_later_entries_visible(self):
        app = self.make()
        app.error("qq", "before-clear")
        app.clear()
        after = app.error("qq", "after-clear")
        tailed = app.tail()
        self.assertEqual([e["message"] for e in tailed], ["after-clear"])
        self.assertEqual([e["id"] for e in tailed], [after["id"]])
        self.assertEqual(app.export()["count"], 1)

    def test_clear_does_not_touch_entries_on_disk(self):
        app = self.make(clock=FakeClock())
        app.error("qq", "keep-1")
        app.error("qq", "keep-2")
        app.clear()
        self.assertEqual(self.log_files(), ["app-20261026.jsonl"])
        self.assertEqual(len(self.read_file("app-20261026.jsonl")), 2)
        self.assertEqual(app.stats()["files"], 1)
        self.assertEqual(app.tail(), [])


class TestDoubleClear(AppLogTestCase):
    """返工 2：黑名单只增不减 —— 重复 clear() 不许把上一次清掉的条目放回来。"""

    def _first_batch(self):
        app = self.make()  # 真实时钟：同一秒内写入，只能靠 id 黑名单区分先后
        for index in range(3):
            app.error("qq", "e" + str(index))
        self.assertEqual(app.clear(), 3)
        return app

    def test_second_clear_keeps_first_batch_hidden(self):
        app = self._first_batch()
        app.info("qq", "i1")
        app.record_exception("qq", "x1", ValueError("x"))
        app.record_exception("qq", "x2", ValueError("x"))
        self.assertEqual(app.clear(), 3)
        self.assertEqual(app.tail(), [])
        messages = [e["message"] for e in app.tail(level="error")]
        for gone in ("e0", "e1", "e2"):
            self.assertNotIn(gone, messages)
        self.assertEqual(app.export()["count"], 0)

    def test_second_clear_keeps_later_entries_visible(self):
        app = self._first_batch()
        app.info("qq", "i1")
        app.clear()
        after = app.error("qq", "after-second-clear")
        self.assertEqual([e["message"] for e in app.tail()], ["after-second-clear"])
        self.assertEqual([e["id"] for e in app.tail(level="error")], [after["id"]])
        self.assertEqual(app.export()["count"], 1)

    def test_three_clears_never_resurrect_first_batch(self):
        app = self._first_batch()
        for round_index in range(2):
            app.warning("qq", "round-" + str(round_index))
            app.clear()
        self.assertEqual(app.tail(), [])
        everything = [e["message"] for e in app.tail(limit=MAX_LIMIT)]
        for gone in ("e0", "e1", "e2", "round-0", "round-1"):
            self.assertNotIn(gone, everything)
        self.assertGreaterEqual(len(app._cleared_ids), 3)


class TestMirrorHandler(AppLogTestCase):
    def test_captures_warning_and_error(self):
        app = self.make()
        logger = logging.getLogger("tests.app_log.mirror")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        handler = MirrorHandler(app)
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        logger.debug("noise")
        logger.info("noise")
        logger.warning("warn-once")
        logger.error("error-once")
        entries = app.tail()
        self.assertEqual([e["level"] for e in entries], ["error", "warning"])
        self.assertEqual([e["message"] for e in entries], ["error-once", "warn-once"])
        self.assertEqual({e["source"] for e in entries}, {"backend"})
        self.assertEqual({e["code"] for e in entries}, {"tests.app_log.mirror"})
        self.assertEqual(entries[0]["extra"]["levelname"], "ERROR")

    def test_does_not_recurse(self):
        app = self.make()
        logger = logging.getLogger("tests.app_log.recursion")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        handler = MirrorHandler(app)
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        seen = []
        real_record = app.record

        def fake_record(level, source, message, **kw):
            seen.append(message)
            logger.warning("nested")  # 镜像链路自己再写日志，必须被挡掉
            return real_record(level, source, message, **kw)

        app.record = fake_record
        try:
            logger.warning("outer")
        finally:
            app.record = real_record
        self.assertEqual(seen, ["outer"])

    def test_captures_traceback_of_logged_exception(self):
        app = self.make()
        logger = logging.getLogger("tests.app_log.exc")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        handler = MirrorHandler(app)
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        try:
            raise KeyError("missing")
        except KeyError:
            logger.exception("出错了")
        entry = app.tail()[0]
        self.assertEqual(entry["level"], "error")
        self.assertIn("KeyError", entry["detail"])

    def test_install_logging_handler_is_idempotent(self):
        app = self.make()
        root = logging.getLogger()
        before = list(root.handlers)
        first = install_logging_handler(app)
        self.addCleanup(root.removeHandler, first)
        second = install_logging_handler(app)
        self.assertIs(first, second)
        self.assertEqual(first.level, logging.WARNING)
        self.assertEqual(len(root.handlers), len(before) + 1)
        install_logging_handler(app, level="ERROR")
        self.assertEqual(first.level, logging.ERROR)


class TestDefaultAppLog(AppLogTestCase):
    def test_singleton_reuse_per_data_dir(self):
        other = Path(tempfile.mkdtemp(prefix="app-log-other-"))
        self.addCleanup(shutil.rmtree, other, True)
        self.addCleanup(app_log_mod._DEFAULTS.clear)
        first = default_app_log(self.root)
        second = default_app_log(str(self.root))
        self.assertIs(first, second)
        self.assertIsNot(first, default_app_log(other))
        first.error("qq", "singleton")
        self.assertEqual(len(first.tail()), 1)


if __name__ == "__main__":
    unittest.main()
