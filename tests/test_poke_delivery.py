"""Poke protocol, real receive path and official fallback; no real QQ sends."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from test_conversation_suite import Fixture, event
from src.qq_app import create_app
from src.qq_transports import onebot_notice

def payload(**changes):
    p=dict(post_type='notice',notice_type='notify',sub_type='poke',self_id=42,
           user_id=7,target_id=42,group_id=123,time=100)
    p.update(changes)
    return p

class ProtocolTests(unittest.TestCase):
    def test_standard_notify_poke_has_correct_actor_target_and_identity(self):
        m=onebot_notice(payload())
        self.assertEqual((m['notice'],m['sender_id'],m['target'],m['self_id']),('poke','7','123','42'))
        self.assertEqual(m['notice_data'],{'poked':'42','poker':'7','group':'123'})
    def test_legacy_direct_type_is_accepted(self):
        self.assertEqual(onebot_notice(payload(notice_type='poke'))['notice'],'poke')
    def test_private_poke_routes_to_actor_even_with_group_zero(self):
        for group in (None,0,'0'):
            m=onebot_notice(payload(group_id=group))
            self.assertEqual((m['kind'],m['target']),('private','7'))
    def test_others_self_malformed_and_unknown_notifications_ignored(self):
        for changes in ({'target_id':8},{'user_id':42},{'target_id':None},
                        {'user_id':None},{'self_id':None},{'sub_type':'honor'}):
            self.assertIsNone(onebot_notice(payload(**changes)))
        self.assertIsNone(onebot_notice(None))
    def test_verified_bridge_identity_takes_precedence(self):
        self.assertIsNone(onebot_notice(payload(self_id=99,target_id=99),'42'))
        self.assertIsNotNone(onebot_notice(payload(self_id=None),'42'))
    def test_explicit_event_ids_keep_separate_events_in_same_second(self):
        self.assertNotEqual(onebot_notice(payload(event_id='a'))['id'],onebot_notice(payload(event_id='b'))['id'])
        self.assertNotEqual(onebot_notice(payload(group_id=1))['id'],onebot_notice(payload(group_id=2))['id'])

class DeliveryTests(Fixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup();self.bot.running=True
        self.bot.plugins.update_settings('poke_counter',{'enabled':True,'replyProbability':1.0,'cooldownMs':0,'replyTexts':'今天第 {count} 次，数值 {n}'})
        self.counter=self.bot.plugins._plugins['poke_counter']
        self.bot.onebot.self_id='42';self.bot.onebot.online=True
        self.bot.onebot.send=AsyncMock();self.bot.official.authenticated=True
        self.bot.official.send=AsyncMock();self.bot.ai.chat=AsyncMock(return_value='不应调用')
    async def asyncTearDown(self):await self.cleanup()
    def ingest_poke(self,**changes):
        self.bot.config.set('qq_only.mode','onebot')
        m=onebot_notice(payload(**changes))
        self.bot.ingest(m,'onebot')
        return m
    def count(self):return self.counter._load()['global']['total']
    async def settle(self):
        task=asyncio.create_task(self.bot._work())
        try:
            for _ in range(50):
                row=self.bot.db.execute('SELECT state FROM inbox ORDER BY created DESC LIMIT 1').fetchone()
                if row and row[0]=='sent':return
                await asyncio.sleep(.05)
            self.fail('Expected reply was not delivered')
        finally:task.cancel();await asyncio.gather(task,return_exceptions=True)
    async def test_real_poke_delivers_predefined_reply_without_ai(self):
        self.ingest_poke();await self.settle()
        self.assertEqual(self.count(),1)
        self.assertIn('今天第 1 次',self.bot.onebot.send.call_args.args[1])
        self.bot.onebot.send.assert_awaited_once();self.bot.ai.chat.assert_not_awaited()
    def test_replayed_event_does_not_count_twice(self):
        for _ in range(2):self.ingest_poke()
        self.assertEqual(self.count(),1)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM inbox').fetchone()[0],1)
    def test_zero_probability_consumes_notice_and_never_queues_ai(self):
        self.bot.plugins.update_settings('poke_counter',{'replyProbability':0.0})
        self.ingest_poke();self.ingest_poke(time=101)
        self.assertEqual(self.count(),2)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM inbox').fetchone()[0],0)
    def test_cooldown_still_counts_without_second_send(self):
        self.bot.plugins.update_settings('poke_counter',{'cooldownMs':60000})
        self.ingest_poke();self.ingest_poke(time=101)
        self.assertEqual(self.count(),2)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM inbox').fetchone()[0],1)
    def test_statistics_has_priority_over_probability_plugin(self):
        self.bot.plugins.update_settings('probability_reply',{'enabled':True,'ignorePokes':True})
        self.ingest_poke();self.assertEqual(self.count(),1)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM inbox').fetchone()[0],1)
    def test_blacklist_disabled_and_private_switch_do_not_count(self):
        self.bot.plugins.update_settings('chat_controls',{'blockedUsers':'7'})
        self.ingest_poke();self.assertEqual(self.count(),0)
        self.bot.plugins.update_settings('chat_controls',{'blockedUsers':''})
        self.bot.plugins.update_settings('poke_counter',{'enabled':False})
        self.ingest_poke(time=101);self.assertEqual(self.count(),0)
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM inbox').fetchone()[0],0)
        self.bot.plugins.update_settings('poke_counter',{'enabled':True})
        self.bot.config.set('qq_only.listen_private',False)
        self.ingest_poke(group_id=0,time=102);self.assertEqual(self.count(),0)
    def test_forged_or_wrong_target_notice_cannot_trigger_plugin(self):
        m=onebot_notice(payload())
        self.bot.ingest(m,'official');self.assertEqual(self.count(),0)
        self.bot.config.set('qq_only.mode','onebot');m['notice_data']['poked']='8'
        self.bot.ingest(m,'onebot');self.assertEqual(self.count(),0)
    async def test_official_command_works_even_with_zero_random_reply_probability(self):
        self.bot.plugins.update_settings('poke_counter',{'replyProbability':0.0})
        self.bot.ingest(event(text='/戳一戳'),'official');await self.settle()
        self.assertEqual(self.count(),1)
        self.bot.official.send.assert_awaited_once();self.bot.ai.chat.assert_not_awaited()
        self.assertEqual(self.bot.official.send.call_args.args[0]['id'],'1')
    def test_query_does_not_increment_and_normal_speech_is_not_a_command(self):
        reply=self.counter.on_ingest(event(text='/戳一戳统计'),self.bot)
        self.assertIn('累计 0 次',reply);self.assertEqual(self.count(),0)
        self.assertIsNone(self.counter.on_ingest(event(text='你会戳一戳吗'),self.bot))
    def test_disabled_command_has_explicit_feedback_no_count(self):
        self.bot.plugins.update_settings('poke_counter',{'enabled':False})
        self.bot.ingest(event(text='/戳一戳'),'official')
        reply=self.bot.db.execute('SELECT reply FROM inbox').fetchone()[0]
        self.assertIn('尚未启用',reply);self.assertEqual(self.count(),0)
    def test_command_cooldown_reports_updated_count(self):
        self.bot.plugins.update_settings('poke_counter',{'cooldownMs':60000})
        first=self.counter.on_ingest(event(text='/戳一戳'),self.bot)
        second=self.counter.on_ingest(event(text='/戳一戳'),self.bot)
        self.assertIn('今天第 1 次',first);self.assertIn('冷却中',second)
        self.assertIn('2 次',second);self.assertEqual(self.count(),2)
    async def test_capability_metadata_is_honest_in_both_modes(self):
        app=create_app(self.bot);app.state.bot=self.bot
        endpoint=next(r.endpoint for r in app.routes if r.path=='/api/plugins')
        item=next(p for p in await endpoint() if p['id']=='poke_counter')
        self.assertTrue(item['available']);self.assertFalse(item['gesture_available'])
        self.assertEqual(item['enabled_label'],'指令模式');self.assertIn('/戳一戳',item['status_note'])
        self.bot.config.set('qq_only.mode','onebot')
        item=next(p for p in await endpoint() if p['id']=='poke_counter')
        self.assertTrue(item['gesture_available']);self.assertIn('真实戳一戳',item['status_note'])

class WebSocketTests(Fixture,unittest.TestCase):
    def setUp(self):
        self.setup();self.bot.config.set('qq_only.mode','onebot');self.bot.running=True
        self.bot.secret=lambda key:'test-token'
        self.bot.plugins.update_settings('poke_counter',{'enabled':True,'replyProbability':0.0})
    def test_universal_connection_forwards_standard_notice_to_counter(self):
        from test_qq_only import _probe_request
        async def no_probe():pass
        with patch.object(self.bot.onebot,'probe',side_effect=no_probe):
            with TestClient(create_app(self.bot)) as client:
                with client.websocket_connect('/qq/onebot',headers={'authorization':'Bearer test-token','x-self-id':'42'}) as ws:
                    ws.send_json(payload())
                    with client.portal.wrap_async_context_manager(_probe_request(self.bot.onebot)):
                        req=ws.receive_json()
                        ws.send_json({'echo':req['echo'],'status':'ok','retcode':0,'data':{}})
                    self.assertEqual(self.bot.plugins._plugins['poke_counter']._load()['global']['total'],1)
                    self.assertEqual(self.bot.db.execute('SELECT count(*) FROM inbox').fetchone()[0],0)
    def tearDown(self):
        self.tmp.cleanup()

class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ['QT_QPA_PLATFORM']='offscreen'
        from PyQt6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def test_official_enabled_card_and_toggle_keep_command_mode_label(self):
        import qq_gui as gui
        panel=gui.PluginTab()
        item={'id':'poke_counter','name':'戳一戳统计','enabled':True,'available':True,
              'enabled_label':'指令模式','status_note':'官方接入：使用 /戳一戳；真实头像事件未接入'}
        row=panel._make_row(item)
        checkbox=panel._rows['poke_counter']['toggle']
        self.assertEqual(checkbox.text(),'指令模式');self.assertTrue(checkbox.isEnabled())
        self.assertIn(item['status_note'],[w.text() for w in row.findChildren(gui.QLabel)])
        with patch.object(gui,'api_post',return_value={'success':True}):
            checkbox.setChecked(False);self.assertEqual(checkbox.text(),'已停用')
            checkbox.setChecked(True);self.assertEqual(checkbox.text(),'指令模式')
        panel.close();row.close()
