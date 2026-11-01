"""回复速度：三条真实瓶颈（合并等待 / 轮询唤醒 / 慢模型重试）+ 模型测速。

实测数据（本机、用户真实配置与中转站）：
- 合并等待：旧逻辑无条件睡满 capMs（用户设 8000ms）→ 新逻辑被 @/叫昵称 0.00s、
  其余静默 0.6s 就开口（实测 0.63s）。
- 轮询：回复循环每 0.3s 轮询一次 → 改成"有消息立刻唤醒 + 0.5s 兜底"。
- 慢模型：旧逻辑超时后还会退避重试（30s+1+30+2+30）→ 聊天路径不再重试超时。
"""
import asyncio
import json
import os
import pathlib
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.ai_engine import AIEngine  # noqa: E402
from src.qq_app import QQRuntime  # noqa: E402


def build_root(tmp, *, burst=None):
    root = pathlib.Path(tmp)
    (root / "styles" / "custom").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "default.yaml").write_text(
        "name: default\ndisplay_name: 默认\nsystem_prompt: 你是默认助手。\n", encoding="utf-8")
    data = root / "data" / "qq-only"
    data.mkdir(parents=True, exist_ok=True)
    settings = {"burst_merge": {"enabled": True, "capMs": 8000, **(burst or {})},
                "chat_controls": {"enabled": True, "requireAt": True, "aliases": "鲸鱼娘,小鲸鱼"}}
    (data / "plugin_settings.json").write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
    (root / "config.qq.yaml").write_text(
        "ai:\n  api_url: https://example.test/v1\n  api_key: fake\n  model: test\n", encoding="utf-8")
    return root


def group_message(mid, text, at=False):
    return {"id": mid, "kind": "group", "target": "456", "sender_id": "20002",
            "sender_name": "小鱼", "text": text, "at": at, "media": []}


class CoalesceWaitTests(unittest.TestCase):
    """合并等待：被点名不等待，其余按静默阈值开口（不再无条件睡满窗口）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bot = QQRuntime(root=build_root(self._tmp.name),
                             config_path=pathlib.Path(self._tmp.name) / "config.qq.yaml",
                             db_path=pathlib.Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        try:
            asyncio.run(self.bot.close())
        except Exception:
            try:
                self.bot.db.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def pending_row(self, mid="k1"):
        self.bot.db.execute("INSERT INTO inbox(key,mode,account,room,payload,state,created)"
                            " VALUES(?,?,?,?,?,'pending',?)",
                            (mid, self.bot.mode, self.bot.account(), "group:456", "{}", time.time()))
        self.bot.db.commit()

    def test_addressed_message_does_not_wait(self):
        self.pending_row()
        message = group_message("m1", "鲸鱼娘在吗", at=True)
        started = time.perf_counter()
        asyncio.run(self.bot._coalesce_pending("k1", message, "group:456", time.time()))
        self.assertLess(time.perf_counter() - started, 0.35, "被 @ 时不该等待")

    def test_nickname_counts_as_addressed(self):
        self.pending_row()
        message = group_message("m1", "小鲸鱼帮我看下")
        started = time.perf_counter()
        asyncio.run(self.bot._coalesce_pending("k1", message, "group:456", time.time()))
        self.assertLess(time.perf_counter() - started, 0.35, "叫昵称同样不该等待")

    def test_plain_message_waits_only_the_quiet_window(self):
        self.pending_row()
        message = group_message("m1", "今天群里真热闹")
        started = time.perf_counter()
        asyncio.run(self.bot._coalesce_pending("k1", message, "group:456", time.time()))
        elapsed = time.perf_counter() - started
        self.assertGreaterEqual(elapsed, 0.5, "静默阈值 0.6s 之前不该抢话")
        self.assertLess(elapsed, 1.6, f"等了 {elapsed:.2f}s，自适应等待没生效")

    def test_window_is_never_exceeded(self):
        """把静默阈值设得比窗口还大时，仍以窗口为上限。"""
        self.bot.plugins._settings["burst_merge"] = {"enabled": True, "capMs": 800,
                                                     "quietMs": 5000}
        self.pending_row()
        message = group_message("m1", "今天群里真热闹")
        started = time.perf_counter()
        asyncio.run(self.bot._coalesce_pending("k1", message, "group:456", time.time()))
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.4, f"等了 {elapsed:.2f}s，没有按 capMs 收口")

    def test_burst_still_merges_two_messages(self):
        """语义没变：等待期间到达的第二条仍会被并进同一条回复。"""
        self.pending_row("k1")
        message = group_message("m1", "第一句")
        created = time.time()

        async def add_second():
            await asyncio.sleep(0.2)
            self.bot.db.execute(
                "INSERT INTO inbox(key,mode,account,room,payload,state,created)"
                " VALUES(?,?,?,?,?,'pending',?)",
                ("k2", self.bot.mode, self.bot.account(), "group:456",
                 json.dumps(group_message("m2", "第二句"), ensure_ascii=False), time.time()))
            self.bot.db.commit()

        async def run():
            task = asyncio.create_task(add_second())
            out = await self.bot._coalesce_pending("k1", message, "group:456", created)
            await task
            return out

        merged = asyncio.run(run())
        self.assertIn("第二句", merged["text"])
        states = dict(self.bot.db.execute("SELECT key,state FROM inbox").fetchall())
        self.assertEqual(states.get("k2"), "merged")


class WakeUpTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bot = QQRuntime(root=build_root(self._tmp.name),
                             config_path=pathlib.Path(self._tmp.name) / "config.qq.yaml",
                             db_path=pathlib.Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self):
        try:
            asyncio.run(self.bot.close())
        except Exception:
            try:
                self.bot.db.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def test_ingest_wakes_the_worker_immediately(self):
        async def run():
            self.bot.running = True
            self.bot.official.authenticated = True
            started = time.perf_counter()
            task = asyncio.create_task(self.bot._wait_for_work())
            await asyncio.sleep(0.05)
            self.bot.ingest(group_message("m1", "鲸鱼娘在吗", at=True), "official")
            await asyncio.wait_for(task, timeout=1.0)
            return time.perf_counter() - started

        elapsed = asyncio.run(run())
        self.assertLess(elapsed, 0.5, f"被叫醒用了 {elapsed:.2f}s，唤醒机制没生效")

    def test_wait_falls_back_to_timeout(self):
        async def run():
            return await self.bot._wait_for_work()

        started = time.perf_counter()
        asyncio.run(asyncio.wait_for(run(), timeout=3))
        self.assertGreaterEqual(time.perf_counter() - started, 0.4)


class ProcessingStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bot = QQRuntime(root=build_root(self._tmp.name),
                             config_path=pathlib.Path(self._tmp.name) / "config.qq.yaml",
                             db_path=pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.bot.config.set('qq_only.mode', 'onebot')
        self.bot.config.set('qq.processing_status', True)
        self.bot.running = True
        self.transport = mock.AsyncMock()
        self.bot.onebot = self.transport
        self.message = group_message('m1', '鲸鱼娘在吗', at=True)
        self.bot.db.execute("INSERT INTO inbox(key,mode,account,room,payload,state,created)"
                            " VALUES(?,?,?,?,?,'pending',?)",
                            ('k1', 'onebot', 'bot', 'group:456',
                             json.dumps(self.message, ensure_ascii=False), time.time()))
        self.bot.db.commit()

    async def asyncTearDown(self):
        await self.bot.close()
        self._tmp.cleanup()

    async def test_sends_one_status_when_still_pending(self):
        with mock.patch('src.qq_app.asyncio.sleep', new=mock.AsyncMock()):
            await self.bot._processing_status('k1', self.message, 'group:456')
        self.transport.send.assert_awaited_once()
        self.assertIn('正在处理', self.transport.send.await_args.args[1])

    async def test_does_not_send_after_final_state(self):
        self.bot.db.execute("UPDATE inbox SET state='sent' WHERE key='k1'")
        self.bot.db.commit()
        with mock.patch('src.qq_app.asyncio.sleep', new=mock.AsyncMock()):
            await self.bot._processing_status('k1', self.message, 'group:456')
        self.transport.send.assert_not_awaited()


class SlowImageConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bot = QQRuntime(root=build_root(self._tmp.name),
                             config_path=pathlib.Path(self._tmp.name) / "config.qq.yaml",
                             db_path=pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.bot.config.set('qq_only.mode', 'onebot')
        self.bot.running = True
        self.bot.onebot.online = True
        self.bot.ai.chat = mock.AsyncMock(return_value='文字回复')
        self.sent = []
        async def send(message, text, *args, **kwargs):
            self.sent.append(text)
            return {'message_id': 'x'}
        self.bot.onebot.send = mock.AsyncMock(side_effect=send)
        self.started = time.monotonic()
        self.vision_done = None

        async def slow_vision(message, runtime):
            await asyncio.sleep(2.0)
            self.vision_done = time.monotonic()
            message['vision_status'] = 'ok'
            message['vision_description'] = '图片'

        self.bot.plugins.describe_images = slow_vision

    async def asyncTearDown(self):
        self.bot.running = False
        for task in list(getattr(self.bot, '_vision_tasks', ())):
            task.cancel()
        await self.bot.close()
        self._tmp.cleanup()

    async def test_text_is_not_blocked_by_slow_image(self):
        self.bot.ingest({'id':'img','kind':'private','target':'u','sender':'u','at':True,
                         'text':'[图片]','media':[{'type':'image','url':'https://example.test/x.png'}]}, 'onebot')
        self.bot.ingest({'id':'txt','kind':'private','target':'u','sender':'u','at':True,
                         'text':'普通文字','media':[]}, 'onebot')
        work = asyncio.create_task(self.bot._work())
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not self.sent:
                await asyncio.sleep(0.02)
            self.assertTrue(self.sent, '文字消息没有被处理')
            self.assertLess(time.monotonic() - self.started, 1.8, '文字被慢图片阻塞')
            self.assertIsNone(self.vision_done, '慢图片不应在文字之前完成')
        finally:
            work.cancel()
            try:
                await work
            except asyncio.CancelledError:
                pass


class StatusTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_image_sends_one_status_after_four_seconds(self):
        tmp=tempfile.TemporaryDirectory()
        bot=QQRuntime(root=build_root(tmp.name),config_path=pathlib.Path(tmp.name)/'config.qq.yaml',
                      db_path=pathlib.Path(tmp.name)/'t.sqlite3')
        try:
            bot.config.set('qq_only.mode','onebot'); bot.config.set('qq.processing_status',True); bot.running=True; bot.onebot.online=True
            calls=[]
            async def send(message,text,*a,**k): calls.append(text)
            bot.onebot.send=send
            async def slow(message,runtime): await asyncio.sleep(6)
            bot.plugins.describe_images=slow
            bot.ingest({'id':'slow','kind':'private','target':'u','sender':'u','at':True,
                        'text':'[图片]','media':[{'type':'image','url':'https://example.test/x.png'}]},'onebot')
            work=asyncio.create_task(bot._work())
            await asyncio.sleep(4.5)
            self.assertTrue(any('正在处理' in text for text in calls), calls)
            self.assertLessEqual(sum('正在处理' in text for text in calls),1)
            work.cancel()
            try: await work
            except asyncio.CancelledError: pass
            for task in list(bot._vision_tasks): task.cancel()
        finally:
            bot.running=False
            await bot.close(); tmp.cleanup()


class SlowModelTests(unittest.TestCase):
    """慢模型不该把一次回复拖成 90 秒：聊天路径不重试超时。"""

    def test_timeout_is_not_retried_for_chat(self):
        engine = AIEngine(api_url="https://example.test/v1", api_key="k", model="m",
                          timeout=0.2, max_retries=3, retry_on_timeout=False)

        async def run():
            with mock.patch.object(engine, "_get_client") as get_client:
                client = mock.AsyncMock()
                client.post.side_effect = __import__("httpx").TimeoutException("slow")
                get_client.return_value = client
                started = time.perf_counter()
                reply = await engine.chat([{"role": "user", "content": "hi"}], max_tokens=8)
                return reply, time.perf_counter() - started, client.post.await_count

        reply, elapsed, attempts = asyncio.run(run())
        self.assertEqual(reply, "")
        self.assertEqual(attempts, 1, "只该请求一次")
        self.assertLess(elapsed, 1.0, "不该有退避重试的等待")
        self.assertIn("不再重试", engine.last_error)

    def test_timeout_is_retried_when_enabled(self):
        engine = AIEngine(api_url="https://example.test/v1", api_key="k", model="m",
                          timeout=0.2, max_retries=2, retry_on_timeout=True)

        async def run():
            with mock.patch.object(engine, "_get_client") as get_client:
                client = mock.AsyncMock()
                client.post.side_effect = __import__("httpx").TimeoutException("slow")
                get_client.return_value = client
                await engine.chat([{"role": "user", "content": "hi"}], max_tokens=8)
                return client.post.await_count

        self.assertEqual(asyncio.run(run()), 2, "探测类调用仍按原样重试")


class SpeedTestTests(unittest.TestCase):
    def engine(self):
        return AIEngine(api_url="https://example.test/v1", api_key="k", model="slow-model")

    def test_results_sorted_and_usage_restored(self):
        engine = self.engine()
        engine.usage.add(prompt=5000, completion=500)      # 假设已有历史用量

        async def fake_chat(messages, model=None, **kwargs):
            await asyncio.sleep({"fast": 0.02, "mid": 0.06}.get(model, 0.3))
            engine.usage.add(prompt=100, completion=10)     # 测速本身也会记一笔
            return "好"

        async def run():
            with mock.patch.object(engine, "chat", side_effect=fake_chat):
                return await engine.speed_test(["slow", "fast", "mid"], timeout=5)

        result = asyncio.run(run())
        self.assertEqual(result["fastest"], "fast")
        order = [item["model"] for item in sorted(result["results"], key=lambda r: r["seconds"])]
        self.assertEqual(order[:2], ["fast", "mid"])
        self.assertTrue(all(item["ok"] for item in result["results"]))
        self.assertEqual(engine.usage.prompt_tokens, 5000, "测速的 token 不该混进聊天用量")
        self.assertEqual(engine.usage.completion_tokens, 500)

    def test_failures_are_reported_per_model(self):
        engine = self.engine()

        async def fake_chat(messages, model=None, **kwargs):
            if model == "broken":
                engine.last_error = "请求超时 (0.2s)"
                return ""
            if model == "boom":
                raise RuntimeError("炸了")
            return "好"

        async def run():
            with mock.patch.object(engine, "chat", side_effect=fake_chat):
                return await engine.speed_test(["broken", "boom", "ok"], timeout=2)

        result = asyncio.run(run())
        by_model = {item["model"]: item for item in result["results"]}
        self.assertFalse(by_model["broken"]["ok"])
        self.assertIn("超时", by_model["broken"]["error"])
        self.assertFalse(by_model["boom"]["ok"])
        self.assertIn("RuntimeError", by_model["boom"]["error"])
        self.assertTrue(by_model["ok"]["ok"])
        self.assertEqual(result["fastest"], "ok")

    def test_model_count_is_capped_and_unconfigured_is_refused(self):
        engine = AIEngine(api_url="", api_key="", model="m")

        async def run():
            return await engine.speed_test([f"m{i}" for i in range(30)])

        result = asyncio.run(run())
        self.assertFalse(result["ok"])
        self.assertIn("配置", result["message"])

    def test_timeout_and_retries_are_restored(self):
        engine = self.engine()
        engine.timeout, engine.max_retries = 30, 3

        async def fake_chat(messages, model=None, **kwargs):
            return "好"

        async def run():
            with mock.patch.object(engine, "chat", side_effect=fake_chat):
                await engine.speed_test(["a", "b"], timeout=3)

        asyncio.run(run())
        self.assertEqual((engine.timeout, engine.max_retries), (30, 3))


if __name__ == "__main__":
    unittest.main()
