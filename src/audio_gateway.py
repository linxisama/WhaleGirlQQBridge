"""音频网关（T01 契约 → T03/T04/T05/T06/T07/T13 的运行时粘合层）。

职责边界：
- 把请求变成“持久化作业 + 调度任务”，把渲染结果变成受管理资产；
- 只做路由/预算/状态，不实现具体引擎（适配器）或 QQ 发送原语（发送回调注入）；
- 所有对外字段都来自可信主体（调用方显式传入 subject_domain/principal），
  绝不从模型输出推断。

发送原语通过 ``set_sender()`` 注入：没有注入时，交付计划只落“本机可发/需文件通道”
状态，不会假装已经发到 QQ（T13 验收）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from collections import OrderedDict
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx

from .audio_assets import AudioAssetStore
from .audio_billing import AudioBilling
from .audio_contracts import (
    AudioJobRequest, AudioMode, ContractError, DeliveryIntent, DeliveryPlanRequest,
    DeliveryState, ErrorCode, JobState, RoutePolicy, create_job, capability_matrix,
    job_parameter_hash, reject,
)
from .audio_contracts_db import initialize as initialize_schema
from .audio_delivery_plan import build_plan, mark_segment, next_pending
from .audio_jobs import AudioJobStore
from .audio_scheduler import AudioScheduler
from .audio_providers.cloud_tts import CloudTtsProvider
from .audio_providers.local_existing import AudioCppProvider, GptSovitsProvider
from .audio_providers.qwen_webui import QwenWebUIProvider
from .voice_profiles import VoiceProfileStore

Sender = Callable[..., Awaitable[dict]]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audio_delivery_plans (
    delivery_id    TEXT PRIMARY KEY,
    subject_domain TEXT NOT NULL,
    request_json   TEXT NOT NULL,
    plan_json      TEXT NOT NULL,
    created_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audio_reply_deliveries (
    job_id         TEXT PRIMARY KEY,
    request_id     TEXT NOT NULL,
    subject_domain TEXT NOT NULL,
    kind           TEXT NOT NULL,
    target         TEXT NOT NULL,
    account_id     TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'pending',
    fail_reason    TEXT DEFAULT '',
    created_at     REAL NOT NULL,
    UNIQUE(request_id)
);
"""


def _cfg(config, key: str, default=None):
    try:
        return config.get(key, default)
    except (TypeError, AttributeError):
        return default


class TtsCache:
    """短语音结果缓存：相同（引擎+音色+文本+语言）直接复用，省掉一次合成往返。

    只缓存内存、按 TTL + 总字节 + 条目数三重上限；关闭时清空。缓存命中仍然
    重新走“资产入库/鉴权”，不会绕过所有者和发送前检查。
    """

    def __init__(self, *, ttl: float = 300.0, max_bytes: int = 64 * 1024 * 1024,
                 max_entries: int = 128, now=None):
        self.ttl = float(ttl)
        self.max_bytes = int(max_bytes)
        self.max_entries = max(1, int(max_entries))
        self._clock = now or time.time
        self._items: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._bytes = 0
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[bytes]:
        entry = self._items.get(str(key))
        if entry is None:
            self.misses += 1
            return None
        expires, audio = entry
        if expires <= self._clock():
            self._items.pop(key, None)
            self._bytes -= len(audio)
            self.misses += 1
            return None
        self._items.move_to_end(key)
        self.hits += 1
        return audio

    def put(self, key: str, audio: bytes) -> None:
        if not audio:
            return
        if len(audio) > self.max_bytes:
            return
        existing = self._items.pop(str(key), None)
        if existing is not None:
            self._bytes -= len(existing[1])
        self._items[str(key)] = (self._clock() + self.ttl, bytes(audio))
        self._bytes += len(audio)
        while len(self._items) > self.max_entries or self._bytes > self.max_bytes:
            _old_key, (_, old_audio) = self._items.popitem(last=False)
            self._bytes -= len(old_audio)

    def clear(self) -> None:
        self._items.clear()
        self._bytes = 0

    def stats(self) -> dict:
        return {'entries': len(self._items), 'bytes': self._bytes,
                'hits': self.hits, 'misses': self.misses,
                'ttl_seconds': self.ttl, 'max_bytes': self.max_bytes,
                'max_entries': self.max_entries}


class AudioGateway:
    def __init__(self, root, config, db: sqlite3.Connection, *,
                 http_transport=None, clock=None,
                 scheduler: Optional[AudioScheduler] = None):
        self.root = Path(root)
        self.config = config
        self.db = db
        self._clock = clock or time.time
        initialize_schema(db)
        db.executescript(_SCHEMA)
        db.commit()
        self.jobs = AudioJobStore(db)
        self.assets = AudioAssetStore(self.root, db)
        self.billing = AudioBilling(
            daily_limit=float(_cfg(config, 'qq_only.voice.cloud.daily_limit', 5.0)),
            monthly_limit=float(_cfg(config, 'qq_only.voice.cloud.monthly_limit', 50.0)),
            per_request_limit=float(_cfg(config, 'qq_only.voice.cloud.per_request_limit', 0.5)),
            price_per_1k_chars=float(_cfg(config, 'qq_only.voice.cloud.price_per_1k_chars', 0.01)),
            price_per_second=float(_cfg(config, 'qq_only.voice.cloud.price_per_second', 0.0)),
            db=db, now=self._clock)
        self.http_transport = http_transport
        self.voices = VoiceProfileStore(self.root, db)
        self.cache = TtsCache(
            ttl=float(_cfg(config, 'qq_only.voice.cache_ttl', 300.0)),
            max_bytes=int(float(_cfg(config, 'qq_only.voice.cache_max_mb', 64.0))
                          * 1024 * 1024),
            max_entries=int(_cfg(config, 'qq_only.voice.cache_entries', 128)),
            now=self._clock)
        self._http_client: Optional[httpx.AsyncClient] = None
        self._adapters: dict[str, object] = {}
        self._sender: Optional[Sender] = None
        self._watch_tasks: dict[str, asyncio.Task] = {}
        self.scheduler = scheduler or AudioScheduler(self.jobs, self.resolve_provider,
                                                     short_timeout=90.0, song_timeout=1200.0)

    # ── 发送原语注入 ────────────────────────────────────────────────────────
    def set_sender(self, sender: Optional[Sender]) -> None:
        self._sender = sender

    @property
    def sender_ready(self) -> bool:
        return self._sender is not None

    # ── 引擎路由 ────────────────────────────────────────────────────────────
    def _cloud_credentials(self):
        key = os.environ.get('DASHSCOPE_API_KEY') or os.environ.get('ELEVENLABS_API_KEY')
        return {'api_key': key} if key else None

    def _cloud_music_credentials(self):
        key = os.environ.get('ELEVENLABS_API_KEY')
        return {'api_key': key} if key else None

    def _client(self):
        """共享 HTTP 连接池（省掉每次请求的连接/TLS 握手）。测试注入 transport 时禁用。"""
        if self.http_transport is not None:
            return None
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=120.0, trust_env=False,
                                                  follow_redirects=False)
        return self._http_client

    def adapter(self, name: str):
        if name in self._adapters:
            return self._adapters[name]
        client = self._client()
        if name == 'audio_cpp':
            adapter = AudioCppProvider(self.config, self.http_transport,
                                       http_client=client)
        elif name == 'gpt_sovits_api_v2':
            adapter = GptSovitsProvider(self.config, self.http_transport,
                                        http_client=client)
        elif name == 'qwen_webui':
            adapter = QwenWebUIProvider(self.config, self.http_transport,
                                        http_client=client)
        elif name == 'cloud_tts':
            adapter = CloudTtsProvider(self.config, credentials=self._cloud_credentials(),
                                       billing=self.billing,
                                       http_transport=self.http_transport,
                                       http_client=client)
        else:
            reject(ErrorCode.CAPABILITY_UNSUPPORTED, f'未知语音引擎：{name}')
        self._adapters[name] = adapter
        return adapter

    def resolve_provider(self, request: AudioJobRequest):
        if request.mode is not AudioMode.TTS:
            reject(ErrorCode.CAPABILITY_UNSUPPORTED,
                   f'{request.mode.value} 尚未接入该网关（S1/S2/S3 使用独立管线）',
                   mode=request.mode.value)
        profile = self._profile_for(request)
        policy = request.route_policy
        if policy is RoutePolicy.CLOUD_ONLY:
            candidates = ['cloud_tts']
        elif policy is RoutePolicy.LOCAL_PREFERRED:
            candidates = [str(_cfg(self.config, 'qq_only.voice.provider', 'gpt_sovits_api_v2')),
                          'cloud_tts']
        else:
            candidates = [str(_cfg(self.config, 'qq_only.voice.provider', 'gpt_sovits_api_v2'))]
        failures = []
        for name in candidates:
            try:
                adapter = self.adapter(name)
            except ContractError as exc:
                failures.append(exc.code.value)
                continue
            if getattr(adapter, 'supports_cloud', False) and not profile['allow_cloud']:
                failures.append('cloud_not_allowed_for_profile')
                continue
            if not adapter.available():
                failures.append('not_available:' + name)
                continue
            if not adapter.ready():
                failures.append('not_ready:' + name)
                continue
            return adapter, profile
        reject(ErrorCode.MODEL_NOT_READY,
               '没有已就绪的语音引擎；请先在音频设置里配置并预热',
               tried=','.join(candidates), failures=','.join(failures))

    def _profile_for(self, request: AudioJobRequest) -> dict:
        """按 voice_profile_id 取音色参数；默认音色可被全局 default_profile 覆盖。"""
        profile_id = str(getattr(request, 'voice_profile_id', '') or '')
        if profile_id in ('', 'persona-default', 'default'):
            configured = str(_cfg(self.config, 'qq_only.voice.default_profile', '')).strip()
            if configured:
                stored = self.voices.get(configured)
                if stored is not None:
                    return self._stored_profile(configured, stored, request)
        stored = self.voices.get(profile_id) if profile_id else None
        if stored is not None:
            return self._stored_profile(profile_id, stored, request)
        if profile_id not in ('', 'persona-default', 'default'):
            reject(ErrorCode.INVALID_INPUT,
                   '音色不存在或已删除；请在音频页重新导入', voice=profile_id)
        return {'profile_id': profile_id or 'persona-default',
                'profile_name': '默认音色', 'provider_map': {},
                'language': request.language,
                'allow_cloud': bool(_cfg(self.config,
                                         'qq_only.voice.cloud.allow_outbound', False))}

    @staticmethod
    def _stored_profile(profile_id: str, stored: dict,
                        request: AudioJobRequest) -> dict:
        return {'profile_id': profile_id,
                'profile_name': stored.get('name', profile_id),
                'provider_map': stored.get('provider_map') or {},
                'language': stored.get('language') or request.language,
                'allow_cloud': bool(stored.get('allow_cloud'))}

    # ── 能力 ────────────────────────────────────────────────────────────────
    def capabilities(self) -> dict:
        engines = {}
        for name in ('audio_cpp', 'gpt_sovits_api_v2', 'qwen_webui', 'cloud_tts'):
            try:
                adapter = self.adapter(name)
                engines[name] = {'capability': getattr(adapter.capability, 'value', ''),
                                 'supports_cloud': bool(adapter.supports_cloud),
                                 'available': bool(adapter.available()),
                                 'ready': bool(adapter.ready()),
                                 # Config/credentials are not proof of a real synthesis.
                                 'verified': False,
                                 'configuration_valid': bool(adapter.available())}
            except Exception:
                engines[name] = {'available': False, 'ready': False}
        for name, info in capability_matrix().items():
            engines.setdefault(name, info)
        return {'engines': engines,
                'route_policy': str(_cfg(self.config, 'qq_only.voice.route_policy', 'local_only')),
                'provider': str(_cfg(self.config, 'qq_only.voice.provider', 'gpt_sovits_api_v2')),
                'budget': self.billing.snapshot().to_dict(),
                'cache': self.cache.stats(),
                'voices': len(self.voices.list()),
                'sender_ready': self.sender_ready,
                'automatic_replies': False,
                'schema_version': 'audio-contracts-v1'}

    # ── 作业 ────────────────────────────────────────────────────────────────
    async def create_tts(self, *, text: str, subject_domain: str, principal: str,
                         delivery_intent: str = 'preview',
                         route_policy: Optional[str] = None,
                         client_request_id: Optional[str] = None,
                         voice_profile_id: str = 'persona-default') -> dict:
        text = str(text or '').strip()
        if not 1 <= len(text) <= 300:
            reject(ErrorCode.INVALID_INPUT, 'tts 文本须为 1–300 字符')
        request_id = client_request_id or ('tts-' + hashlib.sha256(
            f'{subject_domain}|{principal}|{text}|{self._clock()}'.encode()).hexdigest()[:20])
        resolved_voice = str(voice_profile_id or 'persona-default')
        if resolved_voice in ('', 'persona-default', 'default'):
            configured = str(_cfg(self.config, 'qq_only.voice.default_profile', '')).strip()
            if configured and self.voices.get(configured) is not None:
                resolved_voice = configured
        request = AudioJobRequest(
            client_request_id=request_id, mode=AudioMode.TTS, text=text,
            voice_profile_id=resolved_voice,
            route_policy=RoutePolicy(route_policy or _cfg(
                self.config, 'qq_only.voice.route_policy', 'local_only')),
            delivery_intent=DeliveryIntent(delivery_intent),
            language=str(_cfg(self.config, 'qq_only.voice.language', 'zh')))
        # 先校验音色存在，避免“入队成功、后台才报音色不存在”的延迟反馈。
        self._profile_for(request)
        created = create_job(request, subject_domain=subject_domain, principal=principal)
        param_hash = job_parameter_hash(request, subject_domain=subject_domain,
                                        principal=principal)
        queued = self.jobs.enqueue(request, subject_domain=subject_domain,
                                   principal=principal, job_id=created.job_id,
                                   param_hash=param_hash,
                                   cost_reserved=self.billing.estimate(text))
        if queued['duplicate']:
            return {**queued, 'queued': False}
        job_id = queued['job_id']
        owner = str(subject_domain)

        async def runner(req, adapter, profile):
            key = self._cache_key(adapter, profile, req.text or '', req.language)
            audio = self.cache.get(key)
            if audio is None:
                audio = await adapter.synthesize(req, profile)
                self.cache.put(key, audio)
            info = self.assets.store(audio, owner_domain=owner, source_job_id=job_id,
                                     media_type='audio/wav',
                                     licensed=True)
            self.jobs.set_asset(job_id, info['audio_id'],
                                provider=getattr(adapter, 'name', ''))
            return audio

        submitted = await self.scheduler.submit_short(job_id, request, runner)
        return {**submitted, 'duplicate': False}

    @staticmethod
    def _cache_key(adapter, profile: dict, text: str, language: str) -> str:
        raw = json.dumps({'engine': getattr(adapter, 'name', ''),
                          'profile': (profile or {}).get('provider_map') or {},
                          'text': text, 'language': language},
                         sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def get_job(self, job_id: str, *, subject_domain: str) -> Optional[dict]:
        row = self.jobs.get(str(job_id))
        if row is None:
            return None
        if str(row.get('subject_domain')) != str(subject_domain):
            reject(ErrorCode.POLICY_DENIED, '无权查看该作业（不属于当前主体）')
        result = {key: row.get(key) for key in (
            'job_id', 'mode', 'state', 'provider', 'phase_started', 'error_code',
            'cost_reserved', 'cost_actual', 'created_at', 'updated_at', 'asset_id')}
        result['terminal'] = bool(row.get('terminal'))
        asset_id = row.get('asset_id')
        if asset_id:
            try:
                meta = self.assets.meta(asset_id, owner_domain=str(subject_domain))
                result['audio'] = {key: meta[key] for key in (
                    'audio_id', 'media_type', 'sample_rate', 'channels',
                    'duration_seconds', 'size_bytes')}
            except ContractError:
                result['audio'] = None
        return result

    async def cancel(self, job_id: str, *, subject_domain: str) -> dict:
        row = self.jobs.get(str(job_id))
        if row is None:
            reject(ErrorCode.INVALID_INPUT, '作业不存在', job_id=str(job_id))
        if str(row.get('subject_domain')) != str(subject_domain):
            reject(ErrorCode.POLICY_DENIED, '无权取消该作业')
        return await self.scheduler.cancel(str(job_id))

    def recent_jobs(self, *, subject_domain: str, limit: int = 50) -> list[dict]:
        rows = self.jobs.list_recent(limit=max(1, min(int(limit), 200)),
                                     subject_domain=str(subject_domain))
        out = []
        for row in rows:
            payload = self.jobs.get(row['job_id']) or {}
            out.append({**row, 'asset_id': payload.get('asset_id', '')})
        return out

    def asset_meta(self, audio_id: str, *, subject_domain: str) -> dict:
        return self.assets.meta(str(audio_id), owner_domain=str(subject_domain))

    # ── 音色库（导入/列表/删除） ────────────────────────────────────────────
    def list_voices(self) -> list[dict]:
        return self.voices.list()

    def import_voices(self, *, paths: Optional[list[str]] = None,
                      table_path: str = '', provider: str = '',
                      language: str = 'zh', licensed: bool = False,
                      allow_cloud: bool = False) -> list[dict]:
        paths = [str(item) for item in (paths or []) if str(item).strip()]
        table_path = str(table_path or '').strip()
        if bool(paths) == bool(table_path):
            reject(ErrorCode.INVALID_INPUT,
                   '音色导入二选一：多个参考音频，或一张标签表')
        if table_path:
            imported = self.voices.import_table(table_path, language=language)
        else:
            imported = self.voices.import_audio(paths, provider=provider,
                                                language=language,
                                                licensed=licensed,
                                                allow_cloud=allow_cloud)
        self.cache.clear()
        return imported

    def delete_voice(self, profile_id: str) -> bool:
        deleted = self.voices.delete(profile_id)
        if deleted:
            self.cache.clear()
        return deleted

    # ── 自动语音回执（T08） ─────────────────────────────────────────────────
    async def enqueue_voice_reply(self, *, text: str, message: dict,
                                  subject_domain: str, principal: str,
                                  account_id: str = '') -> dict:
        """把“该朗读的一段文字”入队；发送用已注入的 sender，不阻塞 QQ 线程。"""
        result = await self.create_tts(text=text, subject_domain=subject_domain,
                                       principal=principal, delivery_intent='send')
        job_id = result['job_id']
        kind, target = str(message.get('kind') or ''), str(message.get('target') or '')
        account_id = str(account_id or message.get('account_id')
                         or message.get('account') or '')
        request_id = 'voice-' + job_id
        self.db.execute(
            'INSERT OR IGNORE INTO audio_reply_deliveries (job_id,request_id,'
            'subject_domain,kind,target,account_id,state,created_at) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (job_id, request_id, str(subject_domain), kind, target, account_id,
             'pending', self._clock()))
        self.db.commit()
        if self._sender is not None:
            task = asyncio.ensure_future(self._watch_and_send(job_id))
            self._watch_tasks[job_id] = task
            task.add_done_callback(lambda _t, key=job_id: self._watch_tasks.pop(key, None))
        return {**result, 'delivery_request_id': request_id,
                'send_scheduled': self._sender is not None}

    async def _watch_and_send(self, job_id: str, *, timeout: float = 120.0) -> None:
        # 由调度器在完成时唤醒，不再 0.2s 轮询（少一次固定延迟）。
        await self.scheduler.wait_for(job_id, timeout=timeout)
        try:
            await self.deliver_job(job_id)
        except Exception:
            pass

    async def deliver_job(self, job_id: str) -> dict:
        """把已就绪的语音按原消息目标发送；失败不自动重发（T13/uncertain 口径）。"""
        row = self.jobs.get(str(job_id))
        if row is None:
            reject(ErrorCode.INVALID_INPUT, '作业不存在')
        delivery = self.db.execute(
            'SELECT kind,target,account_id,request_id,state FROM audio_reply_deliveries '
            'WHERE job_id=?', (str(job_id),)).fetchone()
        if delivery is None:
            reject(ErrorCode.INVALID_INPUT, '该作业没有发送记录')
        kind, target, account_id, request_id, state = delivery
        if state == 'sent':
            return {'job_id': job_id, 'state': 'sent', 'duplicate': True}
        if state == 'sending':
            return {'job_id': job_id, 'state': 'sending', 'duplicate': True}
        if row.get('state') != JobState.READY.value:
            return {'job_id': job_id, 'state': 'not_ready', 'job_state': row.get('state')}
        asset_id = row.get('asset_id') or ''
        if not asset_id:
            reject(ErrorCode.ASSET_INVALID, '作业没有关联音频资产')
        if self._sender is None:
            self.db.execute("UPDATE audio_reply_deliveries SET state='local_only',"
                            "fail_reason=? WHERE job_id=?",
                            ('未注入 QQ 发送原语，语音保留在本机', str(job_id)))
            self.db.commit()
            return {'job_id': job_id, 'state': 'local_only'}
        path = self.assets.path(asset_id, owner_domain=str(row.get('subject_domain')))
        self.db.execute("UPDATE audio_reply_deliveries SET state='sending' WHERE job_id=?",
                        (str(job_id),))
        self.db.commit()
        try:
            result = await self._sender(kind=kind, target=target, account_id=account_id,
                                        path=path, request_id=request_id)
        except Exception as exc:
            self.db.execute("UPDATE audio_reply_deliveries SET state='uncertain',"
                            "fail_reason=? WHERE job_id=?",
                            (type(exc).__name__, str(job_id)))
            self.db.commit()
            return {'job_id': job_id, 'state': 'uncertain',
                    'note': '发送结果待核实，相同 request_id 不会自动重发'}
        self.db.execute("UPDATE audio_reply_deliveries SET state='sent',fail_reason=? "
                        "WHERE job_id=?", (json.dumps(result or {}, ensure_ascii=False)[:200],
                                           str(job_id)))
        self.db.commit()
        return {'job_id': job_id, 'state': 'sent', 'result': result or {}}

    # ── 交付计划（T13） ─────────────────────────────────────────────────────
    def submit_delivery(self, body: DeliveryPlanRequest, *, subject_domain: str) -> dict:
        metas = []
        for audio_id in body.audio_asset_ids:
            metas.append(self.assets.meta(audio_id, owner_domain=str(subject_domain)))
        total = max((meta.get('duration_seconds') or 0.0 for meta in metas), default=0.0)
        if total <= 0:
            reject(ErrorCode.ASSET_INVALID, '交付资产没有可用时长')
        plan = build_plan(
            audio_asset_ids=list(body.audio_asset_ids), total_seconds=total,
            kind=body.kind, target=body.target, account_id=body.account_id,
            request_id=body.request_id, preview_only=True)
        self.db.execute(
            'INSERT OR REPLACE INTO audio_delivery_plans (delivery_id,subject_domain,'
            'request_json,plan_json,created_at) VALUES (?,?,?,?,?)',
            (body.delivery_id, str(subject_domain),
             json.dumps(body.model_dump(), ensure_ascii=False),
             json.dumps(plan.to_payload(), ensure_ascii=False), self._clock()))
        for segment in plan.segments:
            self.db.execute(
                'INSERT OR IGNORE INTO audio_deliveries (delivery_id,job_id,'
                'audio_asset_ids,account_id,kind,target,segment_index,request_id,state) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (f'{body.delivery_id}-s{segment.index}', '', 
                 json.dumps(plan.audio_asset_ids, ensure_ascii=False),
                 body.account_id, body.kind, body.target, segment.index,
                 segment.request_id, segment.state.value))
        self.db.commit()
        return {'delivery_id': body.delivery_id, 'plan': plan.to_payload(),
                'total_seconds': round(total, 3), 'send_available': self.sender_ready}

    async def send_delivery(self, delivery_id: str, *, subject_domain: str,
                            segments: Optional[list[int]] = None) -> dict:
        """按段发送；某段 uncertain 即暂停，后续段不自动续发。"""
        row = self.db.execute(
            'SELECT plan_json FROM audio_delivery_plans WHERE delivery_id=? AND subject_domain=?',
            (str(delivery_id), str(subject_domain))).fetchone()
        if row is None:
            reject(ErrorCode.INVALID_INPUT, '交付计划不存在')
        plan = _plan_from_json(json.loads(row[0]))
        if self._sender is None:
            return {'delivery_id': delivery_id, 'state': 'local_only',
                    'note': '未注入 QQ 发送原语，交付计划未发送'}
        wanted = set(segments) if segments is not None else None
        sent = []
        while True:
            segment = next_pending(plan)
            if segment is None:
                break
            if wanted is not None and segment.index not in wanted:
                mark_segment(plan, segment.index, DeliveryState.SKIPPED)
                continue
            try:
                audio_id = plan.audio_asset_ids[
                    min(segment.index, len(plan.audio_asset_ids) - 1)]
                path = self.assets.path(audio_id, owner_domain=str(subject_domain))
                result = await self._sender(kind=plan.kind, target=plan.target,
                                            account_id=plan.account_id, path=path,
                                            request_id=segment.request_id)
            except Exception as exc:
                mark_segment(plan, segment.index, DeliveryState.UNCERTAIN)
                self._persist_segment(delivery_id, plan, segment)
                break
            mark_segment(plan, segment.index, DeliveryState.SENT,
                         message_id=str((result or {}).get('message_id', '')))
            self._persist_segment(delivery_id, plan, segment)
            sent.append(segment.index)
        state = 'paused' if plan.paused else (
            'sent' if all(segment.state is DeliveryState.SENT for segment in plan.segments)
            else 'partial')
        return {'delivery_id': delivery_id, 'state': state,
                'segments': [segment.to_payload() for segment in plan.segments],
                'sent': sent}

    def _persist_segment(self, delivery_id: str, plan, segment) -> None:
        self.db.execute(
            'UPDATE audio_deliveries SET state=?,platform_message_id=?,fail_reason=? '
            'WHERE delivery_id=?',
            (segment.state.value, segment.message_id,
             plan.aborted_reason if segment.state is DeliveryState.UNCERTAIN else '',
             f'{delivery_id}-s{segment.index}'))
        self.db.commit()

    def delivery_status(self, delivery_id: str, *, subject_domain: str) -> dict:
        plan_row = self.db.execute(
            'SELECT plan_json FROM audio_delivery_plans WHERE delivery_id=? AND subject_domain=?',
            (str(delivery_id), str(subject_domain))).fetchone()
        if plan_row is None:
            reject(ErrorCode.INVALID_INPUT, '交付计划不存在')
        plan = json.loads(plan_row[0])
        rows = self.db.execute(
            "SELECT segment_index,state,platform_message_id,fail_reason FROM audio_deliveries "
            "WHERE delivery_id LIKE ? ORDER BY segment_index",
            (f'{delivery_id}-s%',)).fetchall()
        segments = [{'index': row[0], 'state': row[1],
                     'message_id': row[2] or '', 'reason': row[3] or ''} for row in rows]
        states = {segment['state'] for segment in segments}
        if 'uncertain' in states:
            overall = 'uncertain'
        elif states and states <= {'sent'}:
            overall = 'sent'
        elif 'sent' in states:
            overall = 'partial'
        else:
            overall = 'pending'
        return {'delivery_id': delivery_id, 'state': overall,
                'segments': segments,
                'confirmed': [segment['index'] for segment in segments
                              if segment['state'] == 'sent'],
                'uncertain_reason': next((segment['reason'] for segment in segments
                                          if segment['reason']), ''),
                'plan': plan}

    async def close(self) -> None:
        for task in list(self._watch_tasks.values()):
            task.cancel()
        self._watch_tasks.clear()
        await self.scheduler.close()
        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None
        self.cache.clear()


def _plan_from_json(payload: dict):
    from .audio_delivery_plan import DeliveryPlan, Segment

    plan = DeliveryPlan(audio_asset_ids=list(payload.get('audio_asset_ids') or []),
                        kind=payload.get('kind', 'private'),
                        target=payload.get('target', ''),
                        account_id=payload.get('account_id', ''),
                        preview_only=bool(payload.get('preview_only', True)),
                        paused=bool(payload.get('paused', False)),
                        aborted_reason=payload.get('aborted_reason', ''))
    for item in payload.get('segments') or []:
        plan.segments.append(Segment(
            index=int(item.get('index', 0)), start=float(item.get('start', 0.0)),
            end=float(item.get('end', 0.0)), request_id=str(item.get('request_id', '')),
            state=DeliveryState(item.get('state', 'pending')),
            message_id=str(item.get('message_id', ''))))
    return plan
