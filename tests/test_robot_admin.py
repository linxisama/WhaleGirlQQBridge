import asyncio,json,unittest
from unittest.mock import Mock,AsyncMock,patch
from test_conversation_suite import Fixture,event
from src.robot_admin import snapshot,ConsoleBackend
from src.plugins.pc_control import parse_command
from src.pc_backend import WindowsBackend

class RobotAdminTests(Fixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup();self.bot.plugins.update_settings('pc_control',{'enabled':True,'ownerIds':'owner'});self.bot.running=True;self.bot.official.authenticated=True
        self.bot.official.send=AsyncMock();self.bot.ai.chat=AsyncMock(return_value='普通回复')
    async def asyncTearDown(self):await self.cleanup()
    async def settle(self,condition,seconds=4):
        task=asyncio.create_task(self.bot._work())
        try:
            for _ in range(int(seconds/.05)):
                if condition():return
                await asyncio.sleep(.05)
            self.fail('队列未按预期处理')
        finally:task.cancel();await asyncio.gather(task,return_exceptions=True)
    def test_host_capabilities_are_removed_even_for_admin(self):
        p=self.bot.plugins._plugins['pc_control']
        for text in ('电脑 截图','电脑 音量30','电脑 打开记事本','电脑 清空回收站'):
            self.assertEqual(parse_command(text),('help',''));self.assertIn('不能',p.on_ingest(event(text=text),self.bot))
        with self.assertRaises(RuntimeError):WindowsBackend().execute('status')
        self.assertFalse(self.bot.pc.list());self.assertNotIn('programAliases',p.META.config_schema)
    def test_snapshot_omits_config_secrets_chat_and_prompt(self):
        self.bot.note('token=supersecret 发生错误');value=snapshot(self.bot);text=json.dumps(value)
        self.assertNotIn('supersecret',text);self.assertNotIn('fake',text);self.assertNotIn('api_key',text);self.assertNotIn('prompt_text',text)
        self.assertEqual(value['model'],'mock');self.assertIn('queue',value)
    async def test_status_query_is_direct_admin_private_only_and_never_calls_ai(self):
        self.bot.ingest(event(text='机器人 状态'),'official')
        await self.settle(lambda:self.bot.official.send.await_count==1)
        self.assertIn('机器人：运行中',self.bot.official.send.call_args.args[1]);self.bot.ai.chat.assert_not_awaited()
        self.assertFalse(self.bot.pc.list());self.assertFalse(self.bot.plugins._plugins['short_followup']._last_replied)
        p=self.bot.plugins._plugins['pc_control']
        self.assertIn('仅接受',p.on_ingest(event(text='机器人 日志',subject='stranger'),self.bot))
        self.assertIn('仅接受',p.on_ingest(event(text='机器人 日志',kind='group'),self.bot))
    async def test_pause_keeps_receiver_and_admin_channel_resume_is_possible(self):
        backend=ConsoleBackend(self.bot);await backend.execute('pause')
        self.assertTrue(self.bot.running);self.assertTrue(self.bot.reply_paused);self.assertTrue(self.bot.official.authenticated)
        self.bot.ingest(event(mid='ordinary',text='你好'),'official');self.assertEqual(self.bot.db.execute('SELECT count(*) FROM inbox').fetchone()[0],0)
        self.bot.ingest(event(mid='admin',text='机器人 队列'),'official');await self.settle(lambda:self.bot.official.send.await_count==1)
        await backend.execute('resume');self.bot.ingest(event(mid='after',text='你好'),'official');await self.settle(lambda:self.bot.official.send.await_count==2)
        self.bot.ai.chat.assert_awaited_once()
    async def test_existing_pending_pauses_and_resumes_without_touching_uncertain(self):
        self.bot.ingest(event(mid='pending'),'official');await ConsoleBackend(self.bot).execute('pause')
        await self.settle(lambda:self.bot.db.execute("SELECT count(*) FROM inbox WHERE state='paused'").fetchone()[0]==1)
        self.bot.official.send.assert_not_awaited();await ConsoleBackend(self.bot).execute('resume')
        await self.settle(lambda:self.bot.official.send.await_count==1)
    async def test_pause_during_generation_prevents_new_send_until_resume(self):
        async def generate(*args,**kwargs):await ConsoleBackend(self.bot).execute('pause');return '已生成'
        self.bot.ai.chat=AsyncMock(side_effect=generate);self.bot.ingest(event(),'official')
        await self.settle(lambda:self.bot.db.execute("SELECT count(*) FROM inbox WHERE state='paused'").fetchone()[0]==1)
        self.bot.official.send.assert_not_awaited();await ConsoleBackend(self.bot).execute('resume')
        await self.settle(lambda:self.bot.official.send.await_count==1);self.bot.ai.chat.assert_awaited_once()
    async def test_remote_mutation_needs_receipt_local_review_and_executes_once(self):
        m=event(text='机器人 暂停');reply=self.bot.plugins._plugins['pc_control'].on_ingest(m,self.bot);self.assertIn('审核',reply)
        key=m['_pc_job']
        with self.assertRaises(ValueError):await self.bot.pc.review(key,True)
        self.bot.pc.receipt_sent(key);await self.bot.pc.review(key,True);self.assertTrue(self.bot.reply_paused)
        with self.assertRaises(ValueError):await self.bot.pc.review(key,True)
        payload=json.loads(self.bot.db.execute("SELECT payload FROM inbox WHERE key=?",('pc-result-'+key,)).fetchone()[0]);self.assertTrue(payload['_robot_admin']);self.assertEqual(payload['_passive_seq'],2)
    async def test_old_whitespace_and_newline_admin_lists_match_review(self):
        self.bot.plugins.update_settings('pc_control',{'ownerIds':'other， owner\nthird'})
        m=event(text='机器人 暂停');self.bot.plugins._plugins['pc_control'].on_ingest(m,self.bot)
        self.bot.pc.receipt_sent(m['_pc_job']);await self.bot.pc.review(m['_pc_job'],True)
        self.assertTrue(self.bot.reply_paused)

    async def test_reconnect_only_calls_robot_transport_and_restarts_receiver(self):
        self.bot.official.authorize=AsyncMock()
        self.bot._official_receiver=AsyncMock()
        await ConsoleBackend(self.bot).execute('reconnect');self.bot.official.authorize.assert_awaited_once_with(force=True)
        await asyncio.sleep(0);self.bot._official_receiver.assert_awaited_once()
