"""Install the distributable single-file plugin through the actual custom loader."""
import asyncio
import json
import sqlite3
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.audio_gateway import AudioGateway
from src.plugin_installer import inspect_local, install_from_file
from src.plugins.manager import PluginManager
from tools.build_release import build_plugin
import tests.test_audio_api as fixtures


SOURCE = Path(__file__).resolve().parents[1] / 'plugin_packages/voice_console/voice_console.py'


class PluginPackageTests(unittest.TestCase):
    def test_whitelist_build_ignores_unapproved_files_and_zip_installs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            shutil.copytree(SOURCE.parent, source)
            (source / 'private.txt').write_text('not distributed', encoding='utf-8')
            destination, archive = root / 'market/voice_console', root / 'market/voice_console.zip'
            built = build_plugin(source, destination, archive)
            self.assertNotIn('private.txt', built['files'])
            self.assertEqual(len(built['files']), 5)
            self.assertTrue(inspect_local(archive)['ok'])
            installed = install_from_file(archive, root / 'installed', confirmed=True)
            self.assertEqual(installed['plugin_id'], 'voice_console')
            with self.assertRaises(FileExistsError):
                build_plugin(source, destination, archive)

    def test_bad_entry_never_writes_market(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            source.mkdir()
            (source / 'plugin.json').write_text(json.dumps({
                'id': 'voice_console', 'entry': '../outside.py', 'version': '1.0.0'}), encoding='utf-8')
            destination = root / 'market/voice_console'
            with self.assertRaises(ValueError):
                build_plugin(source, destination, root / 'market/voice_console.zip')
            self.assertFalse(destination.exists())


class MarketVoiceConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        custom = self.data / 'custom_plugins'
        inspection = inspect_local(SOURCE)
        self.assertTrue(inspection['ok'], inspection['problems'])
        install_from_file(SOURCE, custom, confirmed=True)
        (self.data / 'custom_plugins.json').write_text(
            json.dumps({'plugins': ['voice_console']}), encoding='utf-8')
        with patch('src.plugins.manager.PLUGIN_MODULES', []):
            self.manager = PluginManager(self.data, custom_plugins_dir=custom)
        self.plugin = self.manager._plugins['voice_console']
        self.manager.update_settings('voice_console', {'enabled': True, 'ownerIds': '70001'})
        self.db = sqlite3.connect(':memory:')
        self.config = fixtures.Config({'qq_only.voice.provider': 'audio_cpp',
                                      'ai.api_key': 'test-protected', 'qq.account': '10001'})
        self.gateway = AudioGateway(self.root, self.config, self.db)
        self.adapter = fixtures.FakeAdapter(fixtures.make_wav(0.1))
        self.gateway._adapters['audio_cpp'] = self.adapter
        self.sent = []
        self.notes = []
        self.account = '10001'
        async def sender(**kwargs):
            self.sent.append(kwargs)
            return {'message_id': 'test-message'}
        self.gateway.set_sender(sender)
        self.runtime = SimpleNamespace(
            root=self.root, config=self.config, db=self.db, audio_gateway=self.gateway,
            plugins=self.manager, running=True, mode='qqbridge',
            bridge=SimpleNamespace(online=True), account=lambda: self.account,
            can_send_proactive=lambda kind, target: True, note=self.notes.append)

    async def asyncTearDown(self):
        tasks = list(self.plugin._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.gateway.close()
        self.db.close()
        self.temp.cleanup()

    def message(self, text='语音 朗读 你好', **overrides):
        result = {'id': 'msg-1', 'kind': 'private', 'target': '70001',
                  'sender_id': '70001', 'text': text}
        result.update(overrides)
        return result

    async def finish(self):
        tasks = list(self.plugin._tasks.values())
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)

    async def prepare(self, message):
        await self.manager.run_on_prepare(message, self.runtime)
        return message.get('_direct_reply', '')

    async def test_actual_loader_metadata_default_closed_and_upload_button(self):
        self.assertEqual(self.manager.custom_plugin_ids(), ['voice_console'])
        self.assertFalse(self.plugin.META.enabled_by_default)
        schema = self.plugin.META.schema_dict()['refAudio']
        self.assertEqual(schema['file_picker'], 'file')
        self.assertEqual(schema['file_max_mb'], 8)
        self.assertIn('.wav', schema['file_extensions'])
        self.assertEqual(self.manager.take_load_problems(), [])

    async def test_queued_audio_is_visible_to_console_and_sent_only_after_receipt(self):
        message = self.message()
        reply = await self.prepare(message)
        self.assertIn('已提交', reply)
        self.assertEqual(self.sent, [])
        job_id = message['_voice_console_submission']['job_id']
        self.plugin.on_sent(message, reply, self.runtime)
        await self.finish()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]['target'], '70001')
        self.assertEqual(self.sent[0]['account_id'], '10001')
        self.assertTrue(self.sent[0]['path'].is_file())
        self.assertIn(job_id, [row['job_id'] for row in self.gateway.recent_jobs(subject_domain='console:local')])

    async def test_duplicate_input_never_resends_after_completion(self):
        for _ in range(2):
            message = self.message()
            reply = await self.prepare(message)
            self.plugin.on_sent(message, reply, self.runtime)
            await self.finish()
        self.assertEqual(self.adapter.calls, 1)
        self.assertEqual(len(self.sent), 1)

    async def test_unknown_delivery_is_not_retried(self):
        async def uncertain(**kwargs):
            self.sent.append(kwargs)
            raise RuntimeError('acknowledgement lost')
        self.gateway.set_sender(uncertain)
        for _ in range(2):
            message = self.message()
            await self.prepare(message)
            self.plugin.on_sent(message, '', self.runtime)
            await self.finish()
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(any('没有重发' in note for note in self.notes))

    async def test_disable_switch_account_or_revoke_prevents_delivery(self):
        for change in ('disabled', 'account', 'owners', 'reload', 'permission'):
            with self.subTest(change=change):
                self.manager._plugins['voice_console'] = self.plugin
                self.manager.update_settings('voice_console', {'enabled': True, 'ownerIds': '70001'})
                self.account = '10001'
                self.runtime.can_send_proactive = lambda kind, target: True
                message = self.message(id='change-' + change)
                await self.prepare(message)
                if change == 'disabled':
                    self.manager.update_settings('voice_console', {'enabled': False})
                elif change == 'account':
                    self.account = '10002'
                elif change == 'owners':
                    self.manager.update_settings('voice_console', {'ownerIds': '70002'})
                elif change == 'reload':
                    self.manager._plugins['voice_console'] = object()
                else:
                    self.runtime.can_send_proactive = lambda kind, target: False
                self.plugin.on_sent(message, '', self.runtime)
                await self.finish()
        self.assertEqual(self.sent, [])

    async def test_nonowner_group_quote_and_forged_private_target_are_denied(self):
        variants = [{'sender_id': '80001', 'target': '80001'}, {'kind': 'group', 'target': '90001'},
                    {'quote': {'text': '语音 朗读'}}, {'forward': True}, {'target': '90001'}]
        for item in variants:
            reply = await self.prepare(self.message(**item))
            self.assertIn('仅接受', reply)
        self.assertEqual(self.adapter.calls, 0)

    async def test_explicit_group_setting_preserves_origin(self):
        self.manager.update_settings('voice_console', {'allowGroups': True})
        message = self.message(kind='group', target='90001')
        await self.prepare(message)
        self.plugin.on_sent(message, '', self.runtime)
        await self.finish()
        self.assertEqual(self.sent[0]['kind'], 'group')
        self.assertEqual(self.sent[0]['target'], '90001')

    async def test_probe_uses_form_without_saving_or_synthesizing(self):
        before = dict(self.config.values)
        with patch('src.audio_console.probe', new_callable=AsyncMock,
                   return_value={'message': '接口可达，未合成'}) as checked:
            reply = await self.prepare(self.message('语音 检测'))
        self.assertIn('未合成', reply)
        self.assertEqual(checked.await_count, 1)
        self.assertEqual(self.config.values, before)
        self.assertEqual(self.adapter.calls, 0)

    async def test_apply_changes_only_voice_and_keeps_other_settings(self):
        self.manager.update_settings('voice_console', {'audioVoice': 'alice'})
        reply = await self.prepare(self.message('语音 应用配置'))
        self.assertIn('已增量应用', reply)
        self.assertEqual(self.config.get('qq_only.voice.audio_cpp.voice'), 'alice')
        self.assertEqual(self.config.get('ai.api_key'), 'test-protected')
        self.assertEqual(self.config.get('qq.account'), '10001')

    async def test_external_endpoint_rejected_without_config_write(self):
        self.manager.update_settings('voice_console', {'audioEndpoint': 'https://example.invalid/v1/audio/speech'})
        before = dict(self.config.values)
        reply = await self.prepare(self.message('语音 应用配置'))
        self.assertIn('INVALID_INPUT', reply)
        self.assertEqual(self.config.values, before)

    async def test_reference_requires_managed_upload_and_transcript_is_preserved(self):
        self.manager.update_settings('voice_console', {'engine': 'gpt_sovits_api_v2',
                                                      'refAudio': str(self.root / 'outside.wav')})
        (self.root / 'outside.wav').write_bytes(fixtures.make_wav(0.1))
        reply = await self.prepare(self.message('语音 应用配置'))
        self.assertIn('上传按钮', reply)
        upload = self.root / 'data/plugin_uploads/voice_console/refAudio/test.wav'
        upload.parent.mkdir(parents=True)
        upload.write_bytes(fixtures.make_wav(0.1))
        self.manager.update_settings('voice_console', {'refAudio': str(upload), 'refText': '参考真实台词'})
        reply = await self.prepare(self.message('语音 应用配置'))
        self.assertIn('已增量应用', reply)
        self.assertEqual(self.config.get('qq_only.voice.gpt_sovits.prompt_text'), '参考真实台词')

    async def test_qwen_form_maps_to_existing_contract(self):
        self.manager.update_settings('voice_console', {'engine': 'qwen_webui',
            'qwenModel': 'prepared-custom-model', 'qwenSpeaker': 'SpeakerA'})
        reply = await self.prepare(self.message('语音 应用配置'))
        self.assertIn('已增量应用', reply)
        self.assertEqual(self.config.get('qq_only.voice.qwen.model'), 'prepared-custom-model')
        self.assertEqual(self.config.get('qq_only.voice.qwen.mode'), 'custom_voice')

    async def test_long_input_rejected_without_truncation(self):
        reply = await self.prepare(self.message('语音 朗读 ' + '长' * 301))
        self.assertIn('不会自动截断', reply)
        self.assertEqual(self.adapter.calls, 0)

    async def test_qwen_description_reaches_real_provider_payload(self):
        from src.audio_contracts import AudioJobRequest
        from src.audio_providers.qwen_webui import QwenWebUIProvider
        for mode in ('custom_voice', 'voice_design'):
            self.manager.update_settings('voice_console', {
                'engine': 'qwen_webui', 'qwenMode': mode, 'qwenModel': 'local-model',
                'qwenSpeaker': 'local-speaker', 'qwenInstruct': '温柔、平静的声音'})
            reply = await self.prepare(self.message('语音 应用配置'))
            self.assertIn('已增量应用', reply)
            adapter = QwenWebUIProvider(self.config)
            request = AudioJobRequest(client_request_id='test-description', mode='tts', text='你好')
            payload = adapter._payload(request, {}, mode)
            self.assertEqual(payload['instruct'], '温柔、平静的声音')
            self.assertEqual(payload['model_name'], 'local-model')

    async def test_missing_gateway_loads_and_gives_upgrade_instruction(self):
        self.runtime.audio_gateway = None
        reply = await self.prepare(self.message('语音 状态'))
        self.assertIn('5.1.2', reply)

    async def test_task_query_is_bound_to_principal_account_and_room(self):
        message = self.message()
        await self.prepare(message)
        job_id = message['_voice_console_submission']['job_id']
        self.manager.update_settings('voice_console', {'ownerIds': '70001,70002'})
        reply = await self.prepare(self.message('语音 任务 ' + job_id,
                                                sender_id='70002', target='70002'))
        self.assertIn('不属于', reply)

    async def test_task_query_separates_generation_from_platform_receipt(self):
        message = self.message()
        await self.prepare(message)
        job_id = message['_voice_console_submission']['job_id']
        await self.gateway.scheduler.wait_for(job_id, timeout=3)
        reply = await self.prepare(self.message('语音 任务 ' + job_id))
        self.assertIn('生成：已生成', reply)
        self.assertIn('尚无交付记录', reply)
        self.plugin.on_sent(message, '', self.runtime)
        await self.finish()
        reply = await self.prepare(self.message('语音 任务 ' + job_id))
        self.assertIn('平台已回执，收件播放需验证', reply)

    async def test_uncertain_delivery_query_never_claims_received(self):
        async def uncertain(**kwargs):
            raise RuntimeError('acknowledgement lost')
        self.gateway.set_sender(uncertain)
        message = self.message()
        await self.prepare(message)
        job_id = message['_voice_console_submission']['job_id']
        self.plugin.on_sent(message, '', self.runtime)
        await self.finish()
        reply = await self.prepare(self.message('语音 任务 ' + job_id))
        self.assertIn('结果待核实，不自动重发', reply)
        self.assertNotIn('平台已回执', reply)

    async def test_delivery_query_survives_plugin_reload_without_private_state(self):
        message = self.message()
        await self.prepare(message)
        job_id = message['_voice_console_submission']['job_id']
        self.plugin.on_sent(message, '', self.runtime)
        await self.finish()
        reloaded = type(self.plugin)(self.manager)
        self.manager._plugins['voice_console'] = self.plugin = reloaded
        reply = await self.prepare(self.message('语音 任务 ' + job_id))
        self.assertIn('平台已回执', reply)

    async def test_cancel_queued_generation_prevents_later_receipt_delivery(self):
        message = self.message()
        await self.prepare(message)
        job_id = message['_voice_console_submission']['job_id']
        reply = await self.prepare(self.message('语音 取消 ' + job_id))
        self.assertIn('已请求取消', reply)
        self.plugin.on_sent(message, '', self.runtime)
        await self.finish()
        self.assertEqual(self.gateway.jobs.get(job_id)['state'], 'cancelled')
        self.assertEqual(self.sent, [])

    async def test_cancel_running_generation_stops_automatic_delivery(self):
        entered = asyncio.Event()
        async def slow(request, profile):
            entered.set()
            await asyncio.Event().wait()
        self.adapter.synthesize = slow
        message = self.message()
        await self.prepare(message)
        job_id = message['_voice_console_submission']['job_id']
        self.plugin.on_sent(message, '', self.runtime)
        await asyncio.wait_for(entered.wait(), timeout=3)
        reply = await self.prepare(self.message('语音 取消 ' + job_id))
        self.assertIn('已请求取消', reply)
        await self.finish()
        self.assertEqual(self.gateway.jobs.get(job_id)['state'], 'cancelled')
        self.assertEqual(self.sent, [])

    async def test_cancel_rejects_other_owner_and_account_jobs(self):
        message = self.message()
        await self.prepare(message)
        job_id = message['_voice_console_submission']['job_id']
        self.manager.update_settings('voice_console', {'ownerIds': '70001,70002'})
        with patch.object(self.gateway, 'cancel', new_callable=AsyncMock) as cancelled:
            reply = await self.prepare(self.message('语音 取消 ' + job_id,
                                                    sender_id='70002', target='70002'))
            self.assertIn('不属于', reply)
            self.account = '10002'
            reply = await self.prepare(self.message('语音 取消 ' + job_id))
            self.assertIn('不属于', reply)
            self.assertEqual(cancelled.await_count, 0)

    async def test_cancel_completed_task_does_not_claim_recall(self):
        message = self.message()
        await self.prepare(message)
        job_id = message['_voice_console_submission']['job_id']
        self.plugin.on_sent(message, '', self.runtime)
        await self.finish()
        reply = await self.prepare(self.message('语音 取消 ' + job_id))
        self.assertIn('不能撤回', reply)
        self.assertIn('平台已回执', reply)
        self.assertEqual(len(self.sent), 1)

    async def test_auto_reply_does_not_duplicate_builtin_voice(self):
        self.manager.update_settings('voice_console', {'speakReplies': True})
        self.manager._plugins['voice_reply'] = SimpleNamespace(enabled=True)
        message = self.message('你好')
        self.assertEqual(await self.plugin.on_post_reply(message, '你好呀', self.runtime), '你好呀')
        self.assertNotIn('_voice_console_submission', message)

    async def test_auto_reply_sends_only_after_text_receipt(self):
        self.manager.update_settings('voice_console', {'speakReplies': True})
        message = self.message('你好')
        reply = await self.plugin.on_post_reply(message, '你好呀', self.runtime)
        self.assertEqual(self.sent, [])
        self.plugin.on_sent(message, reply, self.runtime)
        await self.finish()
        self.assertEqual(len(self.sent), 1)

    async def test_commands_prevent_builtin_reading_receipt(self):
        message = self.message('语音 帮助')
        await self.prepare(message)
        self.assertTrue(message['_voice_enqueued'])
        self.assertNotIn('_voice_console_submission', message)

    async def test_agent_status_and_stop_do_not_approve_or_resume(self):
        class Agent:
            stopped = False
            def status(self):
                return {'devices': [], 'stopped': self.stopped}
            def emergency_stop(self):
                self.stopped = True
                return {'stopped': True}
        agent = self.runtime.local_agent_console = Agent()
        reply = await self.prepare(self.message('语音 电脑状态'))
        self.assertIn('未配对不能控制电脑', reply)
        await self.prepare(self.message('语音 急停'))
        self.assertTrue(agent.stopped)
        reply = await self.prepare(self.message('语音 解除急停'))
        self.assertIn('只在本机', reply)
        self.assertTrue(agent.stopped)

    async def test_singing_rejected_instead_of_tts_lyrics(self):
        reply = await self.prepare(self.message('语音 唱歌 一首歌'))
        self.assertIn('尚未接线', reply)
        self.assertEqual(self.adapter.calls, 0)


if __name__ == '__main__':
    unittest.main()
