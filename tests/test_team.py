import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from tools.team import Team, allocate, idle_at, digest, write_json, core_report_dirs, write_source, operating_core

class TeamTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).resolve().parents[1]/'.tmp'
        base.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=base)
        self.root=Path(self.tmp.name)
        self.team=Team(self.root)
        (self.root/'src').mkdir()
        (self.root/'src/example.py').write_text('value = 1\n',encoding='utf-8')

    def tearDown(self):
        self.team.close()
        self.tmp.cleanup()

    def make_task(self,owner='workbuddy',**extra):
        return self.team.create('gpt',{'title':'isolated task','owner':owner,'level':'advanced','weight':1,'scope':['src/example.py'],'acceptance':'prove behavior',**extra})

    def test_idle_windows_and_boundaries(self):
        tz=timezone(timedelta(hours=8))
        for hour,minute,expected in [(0,0,True),(7,59,True),(8,0,False),(11,59,False),(12,0,True),(13,59,True),(14,0,False),(17,59,False),(18,0,True),(23,59,True)]:
            with self.subTest(hour=hour,minute=minute):
                self.assertEqual(idle_at(datetime(2026,10,13,hour,minute,tzinfo=tz)),expected)

    def test_busy_half_advanced_and_simple_workbuddy(self):
        items=[{'title':str(i),'level':'advanced','weight':2} for i in range(4)]+[{'title':'simple','level':'simple'}]
        result=allocate(items,datetime(2026,10,13,2,tzinfo=timezone.utc))
        self.assertEqual([r['owner'] for r in result].count('workbuddy'),3)
        idle=allocate(items,datetime(2026,10,13,5,tzinfo=timezone.utc))
        self.assertTrue(all(r['owner']=='deepseek' for r in idle[:4]))

    def test_worker_cannot_assign_or_complete(self):
        with self.assertRaises(PermissionError):
            self.team.create('workbuddy',{'owner':'workbuddy','scope':['src/example.py']})
        task=self.make_task()
        with self.assertRaises(PermissionError):
            self.team.complete('deepseek',task['id'],'claimed success')

    def test_scope_escape_and_sensitive_paths_rejected(self):
        for name in ['../secret.py','.team-private/providers.json','tools/agent_apis.py','agents/claude/reports/file.py']:
            with self.subTest(name=name),self.assertRaises(PermissionError):
                self.team.source(name)

    def test_overlapping_tasks_rejected(self):
        self.make_task()
        with self.assertRaises(ValueError):
            self.make_task('deepseek')

    def test_packet_only_has_assigned_files(self):
        (self.root/'src/other.py').write_text('not shared',encoding='utf-8')
        task=self.make_task()
        packet=self.team.packet(task['id'])
        self.assertEqual(list(packet['files']),['src/example.py'])
        self.assertFalse((self.root/'agents/deepseek/workspace'/task['id']).exists())

    def test_dependency_waits_for_acceptance(self):
        first=self.make_task()
        second=self.team.create('gpt',{'title':'advanced','owner':'deepseek','scope':['src/new.py'],'depends_on':[first['id']]})
        with self.assertRaises(ValueError):
            self.team.packet(second['id'])

    def test_secret_in_source_not_sent(self):
        (self.root/'src/example.py').write_text("key='sk-"+'z'*24+"'",encoding='utf-8')
        task=self.make_task()
        with self.assertRaises(ValueError):
            self.team.packet(task['id'])

    def test_integration_requires_fresh_source(self):
        task=self.make_task()
        packet=self.team.packet(task['id'])
        task['state']='submitted';self.team.save_task(task)
        write_json(self.root/'agents/workbuddy/reports'/(task['id']+'-implement.json'),{'summary':'change','files':[{'path':'src/example.py','content':'value = 2\n'}],'base_sha256':packet['base_sha256']})
        review=self.root/'agents/gpt/reports/review.md';review.write_text('reviewed diff and scope',encoding='utf-8')
        (self.root/'src/example.py').write_text('concurrent change',encoding='utf-8')
        with self.assertRaises(ValueError):
            self.team.integrate('gpt',task['id'],str(review.relative_to(self.root)))
        self.assertEqual((self.root/'src/example.py').read_text(),'concurrent change')

    def test_handoff_requires_real_configuration_and_ack(self):
        with patch('tools.team.provider',side_effect=ValueError('not configured')):
            with self.assertRaises(ValueError):
                self.team.request_handoff('gpt','claude','quota nearly full')
        self.assertEqual(self.team.control()['leader'],'gpt')
        with patch('tools.team.provider',return_value={'model':'test'}):
            self.team.request_handoff('gpt','claude','quota nearly full')
        with self.assertRaises(PermissionError):
            self.make_task()
        self.team.accept_handoff('claude')
        self.assertEqual(self.team.control()['leader'],'claude')
        with self.assertRaises(PermissionError):
            self.team.require_core('gpt')
        self.team.request_handoff('claude','gpt','quota recovered; checkpoint ready')
        self.team.accept_handoff('gpt')
        self.assertEqual(self.team.control()['epoch'],3)

    def test_quota_requires_fresh_real_five_hour_window(self):
        record={'observed_at':100,'windowDurationMins':300,'usedPercent':95}
        self.assertEqual(self.team.quota(record,100)['action'],'handoff')
        self.assertEqual(self.team.quota(record,300)['action'],'unknown')
        self.assertEqual(self.team.quota({**record,'windowDurationMins':10080},100)['action'],'unknown')
        self.assertEqual(self.team.quota({**record,'usedPercent':None},100)['action'],'unknown')

    def test_operating_core_override_is_explicit_and_audited(self):
        private=self.root/'.team-private';private.mkdir()
        override=private/'operating_core.json'
        spec={'title':'override task','owner':'workbuddy','level':'simple','weight':1,'scope':['src/new_override.py']}
        # 未写覆盖文件：开发角色仍不能当核心
        with self.assertRaises(PermissionError):
            self.team.create('deepseek',spec)
        # 只有同时满足“文件声明该角色”且“该角色就是当前 leader”才放行
        override.write_text(json.dumps({'actor':'deepseek','since':'2026-10-13T12:20:00+08:00','reason':'用户指定本机核心'},ensure_ascii=False),encoding='utf-8')
        with self.assertRaises(PermissionError):
            self.team.create('deepseek',spec)
        control=self.team.control();control['leader']='deepseek'
        with self.team.db:
            self.team.db.execute('UPDATE control SET body=? WHERE id=1',(json.dumps(control),))
        task=self.team.create('deepseek',spec)
        self.assertEqual(task['owner'],'workbuddy')
        # 审核报告目录必须与实际核心一致：运行核心同时接受 gpt/claude 的报告目录
        dirs={str(d) for d in core_report_dirs(self.root,'deepseek')}
        self.assertIn(str((self.root/'agents/deepseek/reports').resolve()),dirs)
        self.assertIn(str((self.root/'agents/gpt/reports').resolve()),dirs)
        self.assertNotIn(str((self.root/'agents/workbuddy/reports').resolve()),dirs)
        # 缺少原因或时间的声明无效
        override.write_text(json.dumps({'actor':'deepseek','since':''},ensure_ascii=False),encoding='utf-8')
        with self.assertRaises(PermissionError):
            self.team.require_core('deepseek')

    def test_source_writes_keep_lf_line_endings(self):
        # 集成写盘必须与仓库既有文件一致（UTF-8 + LF），否则同一文件再次集成会被误判为被改动。
        target=self.root/'src/lf_probe.py'
        write_source(target,'a = 1\nb = 2\n')
        raw=target.read_bytes()
        self.assertEqual(raw,b'a = 1\nb = 2\n')
        self.assertNotIn(b'\r\n',raw)

    def test_operating_core_tolerates_utf8_bom(self):
        # 记事本/PowerShell 写出的 JSON 常带 BOM；带 BOM 时不能静默失效
        private=self.root/'.team-private';private.mkdir()
        override=private/'operating_core.json'
        payload={'actor':'deepseek','since':'2026-10-13T13:20:00+08:00','reason':'用户指定'}
        override.write_bytes(b'\xef\xbb\xbf'+json.dumps(payload,ensure_ascii=False).encode('utf-8'))
        self.assertEqual(operating_core(self.root),'deepseek')

    def test_operating_core_rejects_incomplete_record(self):
        private=self.root/'.team-private';private.mkdir()
        override=private/'operating_core.json'
        # 合法的 actor 是四个角色之一（worker 也可代行核心，另由 leader 约束）；
        # 这里只验证"缺 actor / 缺 reason / 缺 since"必须被判为无效。
        for payload in ({'actor':'deepseek'}, {'actor':'nobody','since':'x','reason':'y'},
                        {'actor':'deepseek','since':'','reason':'y'},
                        {'actor':'deepseek','since':'x','reason':'   '}):
            with self.subTest(payload=payload):
                override.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
                self.assertIsNone(operating_core(self.root))

    def test_integration_rejects_missing_baseline_digest(self):
        task=self.make_task()
        packet=self.team.packet(task['id'])
        task['state']='submitted';self.team.save_task(task)
        write_json(self.root/'agents/workbuddy/reports'/(task['id']+'-implement.json'),{'summary':'change','files':[{'path':'src/example.py','content':'value = 2\n'}],'base_sha256':{}})
        review=self.root/'agents/gpt/reports/review.md';review.write_text('reviewed diff and scope',encoding='utf-8')
        with self.assertRaises(ValueError):
            self.team.integrate('gpt',task['id'],str(review.relative_to(self.root)))
        self.assertEqual((self.root/'src/example.py').read_text(),'value = 1\n')

    def test_events_are_delivered_to_other_roles(self):
        task=self.make_task()
        for role in ('claude','workbuddy','deepseek'):
            events=list((self.root/'agents'/role/'inbox').glob('*.json'))
            self.assertTrue(events)
            self.assertEqual(json.loads(events[0].read_text(encoding='utf-8'))['task_id'],task['id'])

if __name__=='__main__':
    unittest.main()
