"""Four-role team broker. Workers only receive staged packets and return proposals.

This is API/context isolation, not Windows ACL isolation for separately opened apps.
Legacy coordination.py records are retained but not used for the new workflow.
"""
import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.agent_apis import call_agent, provider

ROOT = Path(__file__).resolve().parents[1]
WORKERS = ('workbuddy','deepseek')
CORES = ('gpt','claude')
ROLES = CORES + WORKERS

def operating_core(root=None):
    """显式、可审计的运行核心覆盖。

    默认核心仍是 gpt/claude。当用户指定另一个运行核心（例如让本机 DeepSeek 会话
    代行核心职责）时，必须写下 .team-private/operating_core.json，记录角色、时间与
    原因，避免悄悄把开发角色永久提升为核心。文件不存在即返回 None。
    """
    path = (Path(root) if root else ROOT)/'.team-private/operating_core.json'
    if not path.is_file():
        return None
    try:
        # utf-8-sig：Windows 工具（记事本、PowerShell Set-Content）很容易写出带 BOM 的
        # JSON，用 utf-8 读会直接 JSONDecodeError，表现为"覆盖文件明明在、却静默不生效"。
        record = json.loads(path.read_text(encoding='utf-8-sig'))
    except Exception:
        return None
    actor = record.get('actor')
    if actor not in ROLES:
        return None
    if not str(record.get('reason','')).strip() or not str(record.get('since','')).strip():
        return None
    return actor

def core_report_dirs(root, actor):
    """核心审核报告的合法目录：固定核心只认自己的目录，运行核心覆盖同时接受 gpt/claude。"""
    names = {actor}
    if actor == operating_core(root) and actor in WORKERS:
        names |= set(CORES)
    return tuple((Path(root)/'agents'/name/'reports').resolve() for name in names)


def digest(data):
    return hashlib.sha256(data).hexdigest()

def write_source(path, text):
    """源码一律按 UTF-8 + LF 写盘，与仓库既有文件一致。

    Path.write_text 默认走文本模式，在 Windows 上会把 \\n 变成 \\r\\n，
    导致写回字节与核心审核时记录的摘要不一致（同一文件再次集成会被误判为被改动）。
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(text)

def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(path)

def json_reply(text):
    text = text.strip()
    if text.startswith('```'):
        text = text.split('\n',1)[1].rsplit('```',1)[0]
    return json.loads(text)

def idle_at(now):
    local = now.astimezone(timezone(timedelta(hours=8)))
    minute = local.hour*60+local.minute
    return minute < 480 or 720 <= minute < 840 or minute >= 1080

def allocate(items, now):
    """Simple tasks go to WB. Busy advanced tasks balance estimated effort."""
    totals = {r:0 for r in WORKERS}
    result = []
    idle = idle_at(now)
    for item in items:
        weight = float(item.get('weight',1))
        if weight <= 0:
            raise ValueError('预计工作量必须大于0')
        owner = item.get('owner')
        if owner not in WORKERS:
            owner = 'workbuddy' if item['level']=='simple' else ('deepseek' if idle else min(totals,key=totals.get))
        if item['level']=='advanced':
            totals[owner] += weight
        result.append({**item,'owner':owner})
    return result

class Team:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        for role in ROLES:
            for part in ('workspace','instructions','tasks','inbox','outbox','reports'):
                (self.root/'agents'/role/part).mkdir(parents=True,exist_ok=True)
        for part in ('core/events','developers'):
            (self.root/'shared'/part).mkdir(parents=True,exist_ok=True)
        (self.root/'collaboration').mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root/'collaboration/team.sqlite3',timeout=30)
        self.db.execute('CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, body TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS control(id INTEGER PRIMARY KEY CHECK(id=1), body TEXT)')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO control VALUES(1,?)',(json.dumps({'leader':'gpt','epoch':1,'pending':None}),))
        policy = self.root/'collaboration/team_policy.json'
        self.policy = json.loads(policy.read_text(encoding='utf-8')) if policy.exists() else {'check_interval_seconds':30,'request_deadline_seconds':600,'usage_max_age_seconds':120,'prepare_handoff_used_percent':80,'handoff_used_percent':90}

    def close(self):
        self.db.close()

    def control(self):
        return json.loads(self.db.execute('SELECT body FROM control WHERE id=1').fetchone()[0])

    def require_core(self, actor):
        control = self.control()
        allowed = actor in CORES or (actor == operating_core(self.root) and actor == control['leader'])
        if not allowed or actor != control['leader'] or control['pending']:
            raise PermissionError('只有当前活动核心可以修改任务和集成；交接中暂停写入')

    def task(self, task_id):
        row = self.db.execute('SELECT body FROM tasks WHERE id=?',(task_id,)).fetchone()
        if not row:
            raise ValueError('任务不存在')
        return json.loads(row[0])

    def tasks(self):
        return [json.loads(r[0]) for r in self.db.execute('SELECT body FROM tasks ORDER BY rowid')]

    def source(self,name):
        path = (self.root/name).resolve()
        allowed = path.is_relative_to(self.root) and Path(name).parts[0] in {'src','tests','tools'}
        if not allowed or path.suffix not in {'.py','.js','.html','.css'} or path.name in {'agent_apis.py','team.py','coordination.py'}:
            raise PermissionError('不允许把此路径交给开发模型')
        return path

    def save_task(self,task):
        task['updated_at'] = time.time()
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO tasks VALUES(?,?)',(task['id'],json.dumps(task,ensure_ascii=False)))
        write_json(self.root/'agents'/task['owner']/'tasks'/(task['id']+'.json'),task)

    def event(self,actor,task_id,state,note):
        event = {'id':uuid.uuid4().hex,'time':time.time(),'actor':actor,'task_id':task_id,'state':state,'note':note}
        # Peers see only event summaries, never private task packets or hidden reasoning.
        write_json(self.root/'shared/core/events'/(event['id']+'.json'),event)
        for role in ROLES:
            write_json(self.root/'agents'/role/('outbox' if role==actor else 'inbox')/(event['id']+'.json'),event)

    def create(self,actor,spec):
        self.require_core(actor)
        if spec['owner'] not in WORKERS:
            raise ValueError('开发任务必须分配给 WorkBuddy 或 DeepSeek')
        scope = spec.get('scope',[])
        if not scope or len(scope)!=len(set(scope)):
            raise ValueError('写入范围不能为空或重复')
        for name in scope + spec.get('read_files',[]):
            self.source(name)
        for other in self.tasks():
            if other['state'] not in ('complete','cancelled') and set(scope)&set(other['scope']):
                raise ValueError('任务写入范围与未完成任务重叠')
        for dependency in spec.get('depends_on',[]):
            self.task(dependency)
        task = {**spec,'id':'T-'+uuid.uuid4().hex[:10],'state':'assigned','created_at':time.time()}
        self.save_task(task)
        self.event(actor,task['id'],'assigned',task['title'])
        return task

    def packet(self,task_id):
        task = self.task(task_id)
        for dep in task.get('depends_on',[]):
            if self.task(dep)['state']!='complete':
                raise ValueError('前置任务尚未由核心验收，应等待')
        directory = self.root/'agents'/task['owner']/'workspace'/task_id
        files,hashes = {},{}
        for name in sorted(set(task['scope']+task.get('read_files',[]))):
            path = self.source(name)
            raw = path.read_bytes() if path.exists() else b''
            text = raw.decode('utf-8')
            if re.search(r'sk-[A-Za-z0-9_-]{16,}',text):
                raise ValueError('任务源码疑似含密钥，已拒绝发送')
            files[name],hashes[name] = text,digest(raw)
            copy = directory/name
            copy.parent.mkdir(parents=True,exist_ok=True)
            write_source(copy,text)
        handoffs = []
        for dep in task.get('depends_on',[]):
            path = self.root/'shared/developers'/(dep+'.json')
            if path.exists():
                handoffs.append(json.loads(path.read_text(encoding='utf-8')))
        packet = {'task':task,'files':files,'base_sha256':hashes,'handoffs':handoffs}
        write_json(directory/'packet.json',packet)
        return packet

    async def run(self,task_id,phase):
        task = self.task(task_id)
        self.require_core(self.control()['leader'])
        expected = ('assigned','changes_requested','blocked') if phase=='plan' else ('plan_approved',)
        if task['state'] not in expected:
            raise ValueError('此阶段尚未获得批准或已经运行')
        packet = self.packet(task_id)
        role = task['owner']
        system = (self.root/'agents'/role/'instructions/SYSTEM.md').read_text(encoding='utf-8')
        if phase=='plan':
            system += '\n只返回JSON: {"summary":"目标及方向","steps":["步骤"],"tests":["验收"],"risks":["风险"]}。此阶段不输出代码。'
        else:
            plan = json.loads((self.root/'agents'/role/'reports'/(task_id+'-plan.json')).read_text(encoding='utf-8'))
            if digest(json.dumps(plan,ensure_ascii=False,sort_keys=True).encode()) != task['plan_hash']:
                raise ValueError('计划在批准后发生变化')
            packet['approved_plan'] = plan
            system += '\n只返回JSON: {"summary":"变更摘要","files":[{"path":"允许的路径","content":"完整文件内容"}],"tests":["测试命令和验收点"],"handoff":"给后续开发角色的接口说明"}。不要输出markdown围栏。只能修改scope内文件，read_files只读。完整输出修改后的文件，未修改文件不要重复返回。'
        task['state']=phase+'_running'
        self.save_task(task)
        self.event(role,task_id,task['state'],'已通过真实 API 开始阶段工作')
        started = time.monotonic()
        # Reasoning providers count reasoning and final text in the output budget.
        budget = (16384 if phase=='plan' else 32768) if role=='deepseek' else (4096 if phase=='plan' else 24000)
        pending = asyncio.create_task(call_agent(role,system,json.dumps(packet,ensure_ascii=False),budget))
        try:
            while not pending.done():
                done,_ = await asyncio.wait({pending},timeout=self.policy['check_interval_seconds'])
                if not done:
                    elapsed = round(time.monotonic()-started)
                    self.event(self.control()['leader'],task_id,'waiting',f'请求已运行{elapsed}秒，尚无可供方向审核的完整阶段输出')
                    print(f'{task_id} {role}: waiting {elapsed}s',flush=True)
                    if elapsed >= self.policy['request_deadline_seconds']:
                        raise TimeoutError('超过本阶段时限，停止请求并交由核心重新拆分')
            response = await pending
            result = json_reply(response['text'])
            if not isinstance(result,dict) or not isinstance(result.get('summary'),str):
                raise ValueError('模型交付格式无效')
            if phase=='implement':
                files = result.get('files')
                if not isinstance(files,list) or not files:
                    raise ValueError('实现没有提供代码')
                paths=[]
                for entry in files:
                    name=entry.get('path')
                    if name not in task['scope'] or not isinstance(entry.get('content'),str) or name in paths:
                        raise PermissionError('模型试图越过写入范围')
                    paths.append(name)
                    target = self.root/'agents'/role/'workspace'/task_id/'output'/name
                    target.parent.mkdir(parents=True,exist_ok=True)
                    write_source(target,entry['content'])
                result['base_sha256']=packet['base_sha256']
            result['provenance']={k:response[k] for k in ('model','configured_model','usage')}
            result['tests_run']=False
            report=self.root/'agents'/role/'reports'/(task_id+'-'+phase+'.json')
            write_json(report,result)
            task['state']='plan_submitted' if phase=='plan' else 'submitted'
            self.save_task(task)
            self.event(role,task_id,task['state'],result['summary'])
            return {'task':task_id,'state':task['state'],'report':str(report.relative_to(self.root))}
        except Exception as exc:
            task['state']='blocked'
            task['error']=f'{type(exc).__name__}: {exc}'
            self.save_task(task)
            self.event(role,task_id,'blocked',task['error'])
            return {'task':task_id,'state':'blocked','error':task['error']}
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending,return_exceptions=True)

    def approve_plan(self,actor,task_id,note):
        self.require_core(actor)
        task=self.task(task_id)
        if task['state']!='plan_submitted' or not note.strip():
            raise ValueError('需提交计划并填写方向审核结论')
        path=self.root/'agents'/task['owner']/'reports'/(task_id+'-plan.json')
        plan=json.loads(path.read_text(encoding='utf-8'))
        task.update(state='plan_approved',plan_hash=digest(json.dumps(plan,ensure_ascii=False,sort_keys=True).encode()),plan_review=note)
        self.save_task(task)
        self.event(actor,task_id,'plan_approved',note)

    def integrate(self,actor,task_id,review_file):
        self.require_core(actor)
        task=self.task(task_id)
        review=(self.root/review_file).resolve()
        if not any(review.is_relative_to(directory) for directory in core_report_dirs(self.root,actor)) or not review.is_file() or not review.read_text(encoding='utf-8').strip():
            raise ValueError('必须提供当前核心的实质审核报告')
        if task['state']!='submitted':
            raise ValueError('未提交代码，不可集成')
        report=self.root/'agents'/task['owner']/'reports'/(task_id+'-implement.json')
        data=json.loads(report.read_text(encoding='utf-8'))
        changes=[]
        for entry in data['files']:
            name=entry['path']
            if name not in task['scope']:
                raise PermissionError('超出写入范围')
            if name not in data['base_sha256']:
                # 任务包未发送过该路径的基线；基础版实现真的发送了文件，不写入空值。
                raise ValueError(f'交付报告缺少 {name} 的基线摘要，无法确认集成前状态')
            target=self.source(name)
            old=target.read_bytes() if target.exists() else None
            if digest(old or b'')!=data['base_sha256'][name]:
                raise ValueError('源码已变化，必须重新审核')
            changes.append((target,old,entry['content']))
        try:
            for target,old,content in changes:
                backup=self.root/'agents'/actor/'reports'/('backup-'+task_id)/target.relative_to(self.root)
                backup.parent.mkdir(parents=True,exist_ok=True)
                if old is not None:
                    backup.write_bytes(old)
                target.parent.mkdir(parents=True,exist_ok=True)
                write_source(target,content)
        except Exception:
            for target,old,_ in changes:
                if old is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_bytes(old)
            raise
        task.update(state='integrated',review=review_file)
        self.save_task(task)
        self.event(actor,task_id,'integrated','审核后已集成，等待真实测试验收')

    def complete(self,actor,task_id,evidence):
        self.require_core(actor)
        task=self.task(task_id)
        if task['state']!='integrated' or not evidence.strip():
            raise ValueError('必须完成集成并提供测试证据')
        task.update(state='complete',test_evidence=evidence)
        self.save_task(task)
        data=json.loads((self.root/'agents'/task['owner']/'reports'/(task_id+'-implement.json')).read_text(encoding='utf-8'))
        write_json(self.root/'shared/developers'/(task_id+'.json'),{'task_id':task_id,'owner':task['owner'],'summary':data['summary'],'handoff':data.get('handoff',''),'scope':task['scope'],'test_evidence':evidence})
        self.event(actor,task_id,'complete',evidence)

    def checkpoint(self,actor,summary,next_steps):
        control=self.control()
        if actor!=control['leader']:
            raise PermissionError('仅当前核心写检查点')
        record={'time':time.time(),'author':actor,'leader_epoch':control['epoch'],'summary':summary,'next_steps':next_steps,'tasks':self.tasks(),'control':control}
        write_json(self.root/'shared/core/CHECKPOINT.json',record)
        return record

    def request_handoff(self,actor,target,reason):
        self.require_core(actor)
        if target not in CORES or target==actor:
            raise ValueError('目标必须是另一位核心')
        self.checkpoint(actor,reason,['读取所有未完成任务及审核记录后确认接管'])
        if target=='claude':
            provider('claude')  # No simulated takeover when credentials are absent.
        control=self.control()
        control['pending']={'from':actor,'to':target,'reason':reason,'id':uuid.uuid4().hex,'time':time.time()}
        with self.db:
            self.db.execute('UPDATE control SET body=? WHERE id=1',(json.dumps(control),))
        self.event(actor,'core','handoff_requested',reason)
        return control

    def accept_handoff(self,actor):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            control=self.control()
            if not control['pending'] or control['pending']['to']!=actor:
                raise PermissionError('没有发给当前角色的交接请求')
            if not (self.root/'shared/core/CHECKPOINT.json').is_file():
                raise ValueError('缺少交接检查点')
            control.update(leader=actor,epoch=control['epoch']+1,pending=None)
            self.db.execute('UPDATE control SET body=? WHERE id=1',(json.dumps(control),))
        self.event(actor,'core','handoff_accepted','已读取检查点并确认接管')
        return control

    def quota(self,snapshot,now=None):
        now=time.time() if now is None else now
        if not 0<=now-snapshot['observed_at']<=self.policy['usage_max_age_seconds'] or snapshot.get('windowDurationMins')!=300:
            return {'action':'unknown','reason':'额度样本过期或不是5小时窗口，不进行自动切换'}
        used=snapshot.get('usedPercent')
        if not isinstance(used,(float,int)) or isinstance(used,bool) or not 0<=used<=100:
            return {'action':'unknown','reason':'额度数据无效'}
        write_json(self.root/'shared/core/usage.json',snapshot)
        if self.control()['leader']=='claude':
            return {'action':'request_return' if used<self.policy['prepare_handoff_used_percent'] else 'continue_claude'}
        action='handoff' if used>=self.policy['handoff_used_percent'] else ('prepare' if used>=self.policy['prepare_handoff_used_percent'] else 'continue_gpt')
        return {'action':action,'usedPercent':used}

async def cli(args):
    team=Team()
    try:
        if args.command=='status':
            output={'control':team.control(),'tasks':team.tasks(),'idle_now':idle_at(datetime.now(timezone.utc))}
        elif args.command=='create-batch':
            specs=json.loads(Path(args.file).read_text(encoding='utf-8'))
            output=[team.create(args.actor,s) for s in allocate(specs,datetime.now(timezone.utc))]
        elif args.command=='run':
            output=await asyncio.gather(*(team.run(t,args.phase) for t in args.tasks))
        elif args.command=='approve-plan':
            output=team.approve_plan(args.actor,args.task,args.note)
        elif args.command=='integrate':
            output=team.integrate(args.actor,args.task,args.review)
        elif args.command=='complete':
            output=team.complete(args.actor,args.task,args.note)
        elif args.command=='handoff':
            output=team.request_handoff(args.actor,args.target,args.note)
        elif args.command=='accept':
            output=team.accept_handoff(args.actor)
        elif args.command=='quota':
            output=team.quota(json.loads(Path(args.file).read_text(encoding='utf-8')))
        print(json.dumps(output,ensure_ascii=False,indent=2))
    finally:
        team.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('status')
    for name in ('create-batch','quota'):
        p=sub.add_parser(name); p.add_argument('file'); p.add_argument('--actor',default='gpt')
    p=sub.add_parser('run');p.add_argument('phase',choices=('plan','implement'));p.add_argument('tasks',nargs='+')
    for name in ('approve-plan','integrate','complete'):
        p=sub.add_parser(name);p.add_argument('task');p.add_argument('--actor',default='gpt')
        p.add_argument('--review' if name=='integrate' else '--note',required=True)
    p=sub.add_parser('handoff');p.add_argument('target',choices=CORES);p.add_argument('--actor',default='gpt');p.add_argument('--note',required=True)
    p=sub.add_parser('accept');p.add_argument('--actor',required=True,choices=CORES)
    asyncio.run(cli(parser.parse_args()))
