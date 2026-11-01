"""T04：进程内 asyncio 调度器（初版不引入 Redis/Celery）。

队列限额（1.5.3 起始配置，基准后再调）：
- 短语音：总队列 20、单会话待处理 2。
- 唱歌：队列 5、同 GPU 推理并发 1。

约束（T04 验收）：
- 短语音优先；已开始的长歌曲不可强行抢占而破坏 GPU 状态。
- 取消尽力停止排队/生成，但不能承诺外部服务停止计费。
- 歌曲与 QQ 消息线程解耦（QQ 线程立即释放，后台任务完成再回传）。

需要多机多进程时，再把“领取”升级为事务租约/外部队列（1.2），
不要简单起多个消费者造成重复合成。

实现补充（2026-10-20）：
- 每个作业在独立 ``asyncio.Task`` 里生成，取消只取消这一条，不杀工作循环；
- 单会话上限按数据库里的“非终态作业数”判定，跨请求也生效；
- 关闭调度器时把未开始的作业留在 queued，重启由 ``recover_on_restart`` 决定。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from .audio_contracts import (
    AudioJobRequest, ErrorCode, JobState, reject,
)

Runner = Callable[[AudioJobRequest, object, dict], Awaitable[bytes]]
Resolver = Callable[[AudioJobRequest], object]


@dataclass
class _Scheduled:
    job_id: str
    request: AudioJobRequest
    runner: Optional[Runner] = None
    song: bool = False
    session: str = ''


class AudioScheduler:
    SHORT_TTS_QUEUE = 20
    SHORT_PER_SESSION = 2
    SONG_QUEUE = 5
    SONG_GPU_CONCURRENT = 1

    def __init__(self, store, provider_resolver: Optional[Resolver] = None, *,
                 short_timeout: float = 90.0, song_timeout: float = 1200.0,
                 cold_timeout: float = 300.0, short_workers: int = 2,
                 song_workers: int = SONG_GPU_CONCURRENT, clock=None):
        self.store = store
        self.provider_resolver = provider_resolver
        self._short_q: asyncio.Queue = asyncio.Queue(maxsize=self.SHORT_TTS_QUEUE)
        self._song_q: asyncio.Queue = asyncio.Queue(maxsize=self.SONG_QUEUE)
        self._short_timeout = float(short_timeout)
        self._song_timeout = float(song_timeout)
        self._cold_timeout = float(cold_timeout)
        self._short_workers = max(1, int(short_workers))
        self._song_workers = max(1, int(song_workers))
        self._clock = clock or time.time
        self._cancels: set[str] = set()
        self._queued_ids: set[str] = set()
        self._running: dict[str, asyncio.Task] = {}
        self._workers: list[asyncio.Task] = []
        self._closed = False
        self._results: dict[str, bytes] = {}
        self._waiters: dict[str, list[asyncio.Future]] = {}
        self._lock = asyncio.Lock()

    # ── 提交 ────────────────────────────────────────────────────────────────
    async def submit_short(self, job_id: str, request: AudioJobRequest,
                           runner: Optional[Runner] = None) -> dict:
        """短语音入队；队列满 → ContractError(QUEUE_FULL)（不阻塞 QQ 线程）。"""
        return await self._submit(_Scheduled(str(job_id), request, runner, False))

    async def submit_song(self, job_id: str, request: AudioJobRequest,
                          runner: Optional[Runner] = None) -> dict:
        """歌曲入独立队列（并发 1，不抢 GPU）。取消丢弃后到结果。"""
        return await self._submit(_Scheduled(str(job_id), request, runner, True))

    async def _submit(self, item: _Scheduled) -> dict:
        if self._closed:
            reject(ErrorCode.PROVIDER_UNAVAILABLE, '调度器已关闭，不再接收新作业')
        if self._queued_ids.__contains__(item.job_id):
            return {'job_id': item.job_id, 'state': JobState.QUEUED.value,
                    'queued': True, 'duplicate': True}
        row = self.store.get(item.job_id)
        if row is None:
            reject(ErrorCode.INVALID_INPUT, '作业未登记，拒绝调度', job_id=item.job_id)
        if row.get('state') in {s.value for s in JobState if s.terminal}:
            return {'job_id': item.job_id, 'state': row['state'], 'queued': False,
                    'duplicate': True}
        item.session = str(row.get('subject_domain') or '')
        if item.song:
            queue = self._song_q
            timeout = self._song_timeout
        else:
            queue = self._short_q
            timeout = self._short_timeout
            if item.session and self.store.active_count(item.session) > self.SHORT_PER_SESSION:
                self.store.mark(item.job_id, JobState.FAILED,
                                error_code=ErrorCode.QUEUE_FULL.value,
                                phase_started='queue_full')
                reject(ErrorCode.QUEUE_FULL,
                       f'该会话待处理语音已达 {self.SHORT_PER_SESSION} 条，请稍后再试',
                       session=item.session)
        self._ensure_workers()
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            self.store.mark(item.job_id, JobState.FAILED,
                            error_code=ErrorCode.QUEUE_FULL.value, phase_started='queue_full')
            reject(ErrorCode.QUEUE_FULL,
                   '短语音队列已满' if not item.song else '唱歌队列已满，请稍后再试',
                   capacity=queue.maxsize)
        self._queued_ids.add(item.job_id)
        self.store.mark(item.job_id, JobState.QUEUED, phase_started='queued')
        return {'job_id': item.job_id, 'state': JobState.QUEUED.value,
                'queued': True, 'duplicate': False, 'timeout': timeout}

    # ── 工作循环 ────────────────────────────────────────────────────────────
    def _ensure_workers(self) -> None:
        if self._workers:
            return
        for _ in range(self._short_workers):
            self._workers.append(asyncio.ensure_future(self._worker(False)))
        for _ in range(self._song_workers):
            self._workers.append(asyncio.ensure_future(self._worker(True)))

    async def _worker(self, song: bool) -> None:
        queue = self._song_q if song else self._short_q
        timeout = self._song_timeout if song else self._short_timeout
        while not self._closed:
            try:
                item = await queue.get()
            except asyncio.CancelledError:
                return
            try:
                await self._execute(item, timeout)
            finally:
                self._notify(item.job_id)
                queue.task_done()

    async def _execute(self, item: _Scheduled, timeout: float) -> None:
        self._queued_ids.discard(item.job_id)
        if item.job_id in self._cancels:
            self._cancels.discard(item.job_id)
            self.store.mark(item.job_id, JobState.CANCELLED, phase_started='cancelled')
            return
        incoming = self.store.get(item.job_id) or {}
        if incoming.get('state') in {s.value for s in JobState if s.terminal}:
            return
        self.store.mark(item.job_id, JobState.PREPARING, phase_started='preparing')
        task = asyncio.ensure_future(self._generate(item, timeout))
        self._running[item.job_id] = task
        try:
            audio = await task
        except asyncio.CancelledError:
            # 只取消这一条；工作循环继续。已在外部提交的计费无法承诺停止。
            self._running.pop(item.job_id, None)
            if self._closed:
                return
            self._cancels.discard(item.job_id)
            self.store.mark(item.job_id, JobState.CANCELLED, phase_started='cancelled')
            return
        except Exception as exc:  # noqa: BLE001 —— 任何引擎异常都要落成可解释状态
            state, code = self._classify(exc)
            self.store.mark(item.job_id, state, error_code=code,
                            phase_started='failed' if state is JobState.FAILED else 'unknown')
            return
        finally:
            self._running.pop(item.job_id, None)
        if item.job_id in self._cancels:
            self._cancels.discard(item.job_id)
            self.store.mark(item.job_id, JobState.CANCELLED, phase_started='cancelled')
            return
        self.store.mark(item.job_id, JobState.READY, phase_started='ready')
        self._results[item.job_id] = audio
        while len(self._results) > 32:
            self._results.pop(next(iter(self._results)))

    async def _generate(self, item: _Scheduled, timeout: float) -> bytes:
        self.store.mark(item.job_id, JobState.RUNNING, phase_started='running')

        async def produce() -> bytes:
            resolver = self.provider_resolver
            if item.runner is not None:
                adapter, profile = (None, {})
                if resolver is not None:
                    adapter, profile = _split(await _maybe_await(resolver(item.request)))
                return await _maybe_await(item.runner(item.request, adapter, profile))
            if resolver is None:
                reject(ErrorCode.CAPABILITY_UNSUPPORTED, '没有可用的语音引擎')
            adapter, profile = _split(await _maybe_await(resolver(item.request)))
            return await _maybe_await(adapter.synthesize(item.request, profile))

        try:
            audio = await asyncio.wait_for(produce(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError('生成超时') from None
        if not isinstance(audio, (bytes, bytearray)) or not audio:
            raise ValueError('引擎返回空音频')
        return bytes(audio)

    @staticmethod
    def _classify(exc: Exception) -> tuple[JobState, str]:
        code = getattr(exc, 'code', None)
        if code is not None:
            value = getattr(code, 'value', str(code))
            if value == ErrorCode.GENERATION_UNKNOWN.value:
                return JobState.UNKNOWN, value
            return JobState.FAILED, value
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return JobState.FAILED, ErrorCode.DEADLINE_EXCEEDED.value
        if isinstance(exc, (ValueError, OSError)):
            return JobState.FAILED, ErrorCode.INVALID_INPUT.value
        return JobState.FAILED, ErrorCode.PROVIDER_UNAVAILABLE.value

    # ── 取消 ────────────────────────────────────────────────────────────────
    async def cancel(self, job_id: str) -> dict:
        """尽力停止排队/生成；外部计费不承诺停止（1.5.3）。"""
        job_id = str(job_id)
        row = self.store.get(job_id)
        if row is None:
            reject(ErrorCode.INVALID_INPUT, '作业不存在', job_id=job_id)
        if row.get('state') in {s.value for s in JobState if s.terminal}:
            return {'job_id': job_id, 'state': row['state'], 'cancelled': False,
                    'duplicate': True}
        self._cancels.add(job_id)
        running = self._running.get(job_id)
        state = row.get('state')
        if state in (JobState.QUEUED.value, ''):
            self.store.mark(job_id, JobState.CANCELLED, phase_started='cancelled')
            self._notify(job_id)
            return {'job_id': job_id, 'state': JobState.CANCELLED.value, 'cancelled': True,
                    'billing_note': '排队中已取消，未提交生成'}
        self.store.mark(job_id, JobState.CANCEL_REQUESTED, phase_started='cancel_requested')
        if running is not None and not running.done():
            running.cancel()
        return {'job_id': job_id, 'state': JobState.CANCEL_REQUESTED.value, 'cancelled': True,
                'billing_note': '已尽力停止；外部服务可能继续生成或计费'}

    def is_cancelled(self, job_id: str) -> bool:
        return str(job_id) in self._cancels

    # ── 完成通知（替代轮询，缩短端到端延迟） ───────────────────────────────
    def _notify(self, job_id: str) -> None:
        waiters = self._waiters.pop(str(job_id), [])
        row = self.store.get(str(job_id))
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(row)

    async def wait_for(self, job_id: str, timeout: Optional[float] = None) -> Optional[dict]:
        """等作业到终态；已完成立即返回，不 sleep 轮询。"""
        job_id = str(job_id)
        row = self.store.get(job_id)
        if row is not None and row.get('terminal'):
            return row
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(job_id, []).append(waiter)
        try:
            if timeout is None:
                return await waiter
            return await asyncio.wait_for(waiter, timeout)
        except asyncio.TimeoutError:
            return self.store.get(job_id)
        finally:
            bucket = self._waiters.get(job_id)
            if bucket is not None:
                try:
                    bucket.remove(waiter)
                except ValueError:
                    pass
                if not bucket:
                    self._waiters.pop(job_id, None)

    # ── 结果 ────────────────────────────────────────────────────────────────
    def peek_result(self, job_id: str) -> Optional[bytes]:
        return self._results.get(str(job_id))

    def take_result(self, job_id: str) -> Optional[bytes]:
        return self._results.pop(str(job_id), None)

    # ── 重启恢复 ────────────────────────────────────────────────────────────
    async def recover_on_restart(self) -> int:
        """把“已提交但结果未知”的作业标待核实；只重新入队确定未执行的 queued。"""
        affected = self.store.reconcile_restart()
        if self.provider_resolver is None:
            return affected
        requeued = 0
        for row, request in self.store.queued_requests():
            try:
                await self.submit_short(row['job_id'], request)
                requeued += 1
            except Exception:
                continue
        return affected + requeued

    # ── 关闭 ────────────────────────────────────────────────────────────────
    async def close(self) -> None:
        """停止工作循环；未开始作业保持 queued，等待下次启动恢复。"""
        self._closed = True
        for job_id, task in list(self._running.items()):
            if not task.done():
                task.cancel()
        self._running.clear()
        for worker in self._workers:
            worker.cancel()
        for worker in self._workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._workers.clear()
        self._queued_ids.clear()
        for job_id in list(self._waiters):
            self._notify(job_id)


async def _maybe_await(value):
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


def _split(resolved) -> tuple[object, dict]:
    """provider_resolver 可返回 adapter 或 (adapter, profile)。"""
    if isinstance(resolved, tuple) and len(resolved) == 2:
        adapter, profile = resolved
        return adapter, dict(profile or {})
    return resolved, {}
