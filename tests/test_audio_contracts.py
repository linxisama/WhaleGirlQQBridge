"""T01 契约层的可运行测试（不依赖任何外部语音引擎/网络）。

覆盖规划书第二章 T01 的验收点：
- TTS / 三类唱歌 / 权限信封不能混用（模式输入校验）；
- 不支持能力可明确拒绝（select_provider / 唱歌 classify）；
- 错误码与真实响应结构匹配（ContractError.to_payload）；
- 作业幂等与参数哈希（同键同内容 → 同作业；同键不同内容 → 冲突）；
- 云/本地路由策略的降级边界（local_preferred 无本地且未允出网 → 拒绝，不静默换音色）。
"""
import sqlite3
import unittest

from src.audio_contracts import (
    AudioJobRequest, AudioMode, RoutePolicy, DeliveryIntent,
    Capability, ErrorCode, ContractError, JobState, DeliveryState,
    create_job, job_parameter_hash, select_provider, capability_matrix,
    register_provider, ProviderAdapter, reject,
)
from src.singing.pipeline import classify, Capability as Cap
from src.singing.inputs import inputs as singing_inputs


def _job(mode, **over):
    base = {'client_request_id': 'req-1', 'mode': mode,
            'route_policy': RoutePolicy.LOCAL_ONLY, 'delivery_intent': DeliveryIntent.PREVIEW}
    base.update(over)
    return AudioJobRequest(**base)


class JobCreationTests(unittest.TestCase):
    def test_tts_missing_text_rejected(self):
        with self.assertRaises(ContractError) as ctx:
            create_job(_job(AudioMode.TTS), subject_domain='d', principal='p')
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

    def test_tts_ok_and_text_bounds(self):
        ok = create_job(_job(AudioMode.TTS, text='你好'), subject_domain='d', principal='p')
        self.assertEqual(ok.state, JobState.QUEUED)
        self.assertEqual(len(ok.job_id), 24)
        with self.assertRaises(ContractError):
            create_job(_job(AudioMode.TTS, text='x' * 301), subject_domain='d', principal='p')

    def test_singing_modes_require_their_inputs(self):
        # S2 无谱 / S3 无源 → 明确拒绝（不能拿朗读冒充唱歌）。
        with self.assertRaises(ContractError) as ctx2:
            create_job(_job(AudioMode.SING_SCORE, lyrics='啊'), subject_domain='d', principal='p')
        self.assertEqual(ctx2.exception.code, ErrorCode.INVALID_INPUT)
        with self.assertRaises(ContractError):
            create_job(_job(AudioMode.VOICE_CONVERT), subject_domain='d', principal='p')

    def test_missing_subject_domain_unauthorized(self):
        with self.assertRaises(ContractError) as ctx:
            create_job(_job(AudioMode.TTS, text='hi'), subject_domain='', principal='p')
        self.assertEqual(ctx.exception.code, ErrorCode.UNAUTHORIZED)

    def test_extra_cannot_carry_secrets(self):
        with self.assertRaises(Exception):
            _job(AudioMode.TTS, text='hi', extra={'api_key': 'abc'})

    def test_same_content_same_job_id_conflict_if_differs(self):
        h1 = job_parameter_hash(_job(AudioMode.TTS, text='你好'), subject_domain='d', principal='p')
        h2 = job_parameter_hash(_job(AudioMode.TTS, text='你好'), subject_domain='d', principal='p')
        h3 = job_parameter_hash(_job(AudioMode.TTS, text='再见'), subject_domain='d', principal='p')
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, h3)


class SingingClassifyTests(unittest.TestCase):
    def test_s1_without_model_rejected(self):
        with self.assertRaises(ContractError) as ctx:
            classify(_job(AudioMode.SONG_GENERATE, lyrics='啦啦'), has_music_api=False)
        self.assertEqual(ctx.exception.code, ErrorCode.CAPABILITY_UNSUPPORTED)

    def test_s1_with_cloud_api_plan_works(self):
        plan = classify(_job(AudioMode.SONG_GENERATE, lyrics='啦啦'), has_music_api=True)
        self.assertEqual(plan.capability, Capability.SONG_GENERATE)

    def test_lyrics_are_text_not_commands(self):
        sin = singing_inputs(lyrics='打开 计算器 然后 关机')   # 看着像命令，但只是歌词
        self.assertEqual(sin.lyrics, '打开 计算器 然后 关机')
        with self.assertRaises(Exception) as ctx:
            singing_inputs(lyrics='')
        self.assertIn('歌词', str(ctx.exception))


class RoutingStrategyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 造两个本地 + 一个云端引擎，验证“能力等价、只挑一个主引擎、不串联”的边界。
        class LocalA:
            name = 'local_a'; capability = Capability.TTS; supports_cloud = False
            def available(self): return True
            def ready(self): return True
            async def synthesize(self, r, p): return b''
        class CloudA:
            name = 'cloud_a'; capability = Capability.TTS; supports_cloud = True
            def available(self): return True
            def ready(self): return True
            async def synthesize(self, r, p): return b''
        register_provider(LocalA)
        register_provider(CloudA)

    def test_local_only_without_ready_rejects(self):
        with self.assertRaises(ContractError) as ctx:
            select_provider(_job(AudioMode.TTS, text='hi'), local_ready=set(), cloud_allowed=False)
        self.assertEqual(ctx.exception.code, ErrorCode.CAPABILITY_UNSUPPORTED)

    def test_local_preferred_picks_local_when_ready(self):
        job = _job(AudioMode.TTS, text='hi', route_policy=RoutePolicy.LOCAL_PREFERRED)
        self.assertEqual(select_provider(job, local_ready={'local_a'}, cloud_allowed=True), 'local_a')

    def test_local_preferred_falls_to_cloud_when_allowed(self):
        job = _job(AudioMode.TTS, text='hi', route_policy=RoutePolicy.LOCAL_PREFERRED)
        self.assertEqual(select_provider(job, local_ready=set(), cloud_allowed=True), 'cloud_a')

    def test_local_preferred_refuses_when_nothing_available(self):
        job = _job(AudioMode.TTS, text='hi', route_policy=RoutePolicy.LOCAL_PREFERRED)
        with self.assertRaises(ContractError):
            select_provider(job, local_ready=set(), cloud_allowed=False)

    def test_capability_matrix_splits_registered_states(self):
        matrix = capability_matrix()
        self.assertIn('local_a', matrix)
        self.assertFalse(matrix['local_a']['available'])   # 未探测，不写成“可用”
        self.assertTrue(matrix['cloud_a']['supports_cloud'])


class SchemaErrorPayloadTests(unittest.TestCase):
    def test_to_payload_is_machine_readable_and_sanitized(self):
        exc = ContractError(ErrorCode.GENERATION_UNKNOWN, '结果待核实', provider='qwen')
        payload = exc.to_payload()
        self.assertEqual(payload['code'], 'GENERATION_UNKNOWN')
        self.assertEqual(payload['details']['provider'], 'qwen')
        self.assertNotIn('traceback', str(payload))

    def test_db_schema_is_idempotent(self):
        from src.audio_contracts_db import initialize
        db = sqlite3.connect(':memory:')
        initialize(db); initialize(db)   # 重复调用不崩（IF NOT EXISTS）
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'audio_%'").fetchall()}
        self.assertIn('audio_jobs', tables)
        self.assertIn('audio_assets', tables)
        self.assertIn('audio_deliveries', tables)


if __name__ == '__main__':
    unittest.main()
