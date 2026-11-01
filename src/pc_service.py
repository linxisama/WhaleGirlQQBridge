"""Owner requests execute once after local review; neither model nor chat data can approve."""
import json,time,uuid,hashlib,asyncio
from fastapi import Request,HTTPException
from .admin_session import hash_password,password_error,verify_password
from .robot_admin import ConsoleBackend,WRITE_ACTIONS,snapshot,admin_ids
from .speed_profile import describe,profile_diff

class PcService:
    def __init__(self,runtime,backend=None):
        self.runtime=runtime;self.backend=backend or ConsoleBackend(runtime);self.session=uuid.uuid4().hex
        runtime.db.execute('CREATE TABLE IF NOT EXISTS pc_jobs(id TEXT PRIMARY KEY,session TEXT,payload TEXT,action TEXT,arg TEXT,state TEXT,created REAL,result TEXT)')
        runtime.db.execute("UPDATE pc_jobs SET state='cancelled' WHERE state IN ('pending','ready','executing')");runtime.db.commit()
    def request(self,message,action,arg):
        if action not in WRITE_ACTIONS:raise ValueError('仅支持机器人管理操作')
        db=self.runtime.db;key=hashlib.sha256(json.dumps([self.runtime.mode,self.runtime.account(),message['id']]).encode()).hexdigest()[:16]
        if db.execute('SELECT 1 FROM pc_jobs WHERE id=?',(key,)).fetchone():return ''
        active=db.execute("SELECT count(*) FROM pc_jobs WHERE state IN ('pending','ready') AND created>?",(time.time()-120,)).fetchone()[0]
        if active>=10:return '本地待审核操作已满，请先在电脑页处理。'
        payload=dict(message,_pc_mode=self.runtime.mode,_pc_account=self.runtime.account());payload.pop('media',None)
        db.execute('INSERT INTO pc_jobs VALUES(?,?,?,?,?,?,?,?)',(key,self.session,json.dumps(payload,ensure_ascii=False),action,arg,'pending',time.time(),''));db.commit()
        message['_pc_job']=key
        title={'pause':'暂停回复','resume':'恢复回复','reconnect':'重连QQ'}[action]
        return f'机器人管理操作已提交本地审核：{title} {arg}（编号 {key}）。请在控制台“机器人管理”页于2分钟内处理。'
    def receipt_sent(self,key):
        self.runtime.db.execute("UPDATE pc_jobs SET state='ready' WHERE id=? AND state='pending'",(key,));self.runtime.db.commit()
    def list(self):
        self.runtime.db.execute("UPDATE pc_jobs SET state='expired' WHERE state IN ('pending','ready') AND created<?",(time.time()-120,));self.runtime.db.commit()
        return [dict(id=i,action=a,arg=g,state=s,time=t,result=r) for i,a,g,s,t,r in self.runtime.db.execute('SELECT id,action,arg,state,created,result FROM pc_jobs ORDER BY created DESC LIMIT 50')]
    async def review(self,key,approve):
        rt=self.runtime;db=rt.db;row=db.execute('SELECT session,payload,action,arg,state,created FROM pc_jobs WHERE id=?',(key,)).fetchone()
        if not row:raise ValueError('操作不存在')
        session,payload,action,arg,state,created=row;message=json.loads(payload)
        if state!='ready' or session!=self.session or time.time()-created>120:raise ValueError('操作已处理、回执未发出或已过期')
        plugin=rt.plugins._plugins.get('pc_control');owners=set(admin_ids(plugin.get('ownerIds','')))
        if not rt.running or not plugin.enabled or message['_pc_mode']!=rt.mode or message['_pc_account']!=rt.account() or str(message.get('sender_id','')) not in owners:raise ValueError('机器人、主人身份或接入已改变，请重新发起')
        if not approve:
            db.execute("UPDATE pc_jobs SET state='rejected' WHERE id=?",(key,));db.commit();return {'success':True,'message':'已拒绝，未执行'}
        count=db.execute("SELECT count(*) FROM pc_jobs WHERE state IN ('executing','done','failed') AND created>?",(time.time()-3600,)).fetchone()[0]
        if count>=int(plugin.get('maxPerHour',20)):raise ValueError('已达到每小时执行上限')
        if action not in WRITE_ACTIONS:raise ValueError('该操作不属于机器人管理，已拒绝执行')
        db.execute("UPDATE pc_jobs SET state='executing' WHERE id=?",(key,));db.commit()
        # State is persisted before effects: a timeout/restart can never replay the operation.
        try:
            import inspect
            outcome=self.backend.execute(action,arg)
            result,meta=await outcome if inspect.isawaitable(outcome) else outcome
            state='done'
        except Exception as exc:result='操作未完成：'+str(exc)[:240];meta=None;state='failed'
        db.execute('UPDATE pc_jobs SET state=?,result=? WHERE id=?',(state,result,key))
        message['_robot_admin']=True;message['_passive_seq']=2;message['_reply_meta']=meta;message.pop('_pc_job',None)
        inbox_key='pc-result-'+key
        room=message['kind']+':'+message['target']
        if time.time()-created<240 and rt.running and rt.mode==message['_pc_mode'] and rt.account()==message['_pc_account'] and plugin.enabled and str(message.get('sender_id','')) in set(admin_ids(plugin.get('ownerIds',''))):
            db.execute("INSERT OR IGNORE INTO inbox(key,mode,account,room,payload,state,reply,created) VALUES(?,?,?,?,?,'pending',?,?)",(inbox_key,rt.mode,rt.account(),room,json.dumps(message,ensure_ascii=False),result,time.time()))
        db.commit()
        try:self.audit(key,action,state)
        except OSError:rt.note('电脑操作已记录，但审计文件写入失败')
        return {'success':state=='done','message':result}
    def audit(self,key,action,state):
        path=self.runtime.root/'data/qq-only/pc-audit.jsonl'
        if path.exists() and path.stat().st_size>2*1024*1024:path.replace(path.with_suffix('.previous.jsonl'))
        with path.open('a',encoding='utf-8') as f:f.write(json.dumps(dict(time=time.time(),job=key,action=action,state=state))+'\n')
    def record_admin(self,action,arg,result):
        """管理员模式下的直接操作也进审计表：控制台能看到"什么时候做了什么"。

        密码那条消息不会走到这里（插件已把正文抹掉），所以审计里不会有密码。
        """
        key=uuid.uuid4().hex[:16]
        self.runtime.db.execute('INSERT INTO pc_jobs VALUES(?,?,?,?,?,?,?,?)',
            (key,self.session,'{}',action,arg,'done',time.time(),str(result)[:400]))
        self.runtime.db.commit()
        try:self.audit(key,action,'done')
        except OSError:self.runtime.note('管理员操作已记录到控制台，但审计文件写入失败')
        return key

def pc_candidates(bot):
    """可绑定的管理员候选：私聊过机器人的人优先，其次是在群里见过的人。

    以前只收私聊，而且在拿不到昵称时仅显示"私聊用户 N · 后6位"——用户根本认不出
    哪个是自己。现在把完整 QQ 号、昵称、来源（私聊/群聊）都带上，按最近出现排序。
    仍然只收**实际给机器人发过消息**的账号：不允许凭空绑定一个陌生 QQ 号。
    """
    rows=bot.db.execute(
        "SELECT json_extract(payload,'$.sender_id') AS sid,"
        " max(json_extract(payload,'$.sender_name')) AS name,"
        " max(json_extract(payload,'$.kind')) AS kind,"
        " max(created) AS last FROM inbox WHERE mode=? AND account=?"
        " AND json_extract(payload,'$.notice') IS NULL"
        " AND json_extract(payload,'$.sender_id') IS NOT NULL"
        " GROUP BY sid ORDER BY last DESC LIMIT 200",(bot.mode,bot.account())).fetchall()
    def entry(row):
        sid,name,kind,last=row
        name=str(name or '').strip()
        return dict(id=str(sid),name='' if name==str(sid) else name,
                    chat='私聊' if str(kind)=='private' else '群聊',last_seen=last)
    private=[entry(r) for r in rows if str(r[2])=='private']
    others=[entry(r) for r in rows if str(r[2])!='private']
    return (private+others)[:50]

def register_pc_api(app):
    @app.get('/api/pc')
    async def read_pc():
        bot=app.state.bot
        settings=bot.plugins.get_settings('pc_control')
        sessions=getattr(bot,'admin',None)
        return {'success':True,'jobs':bot.pc.list(),'owners':settings['ownerIds'],'enabled':settings['enabled'],
                'candidates':pc_candidates(bot),'robot':snapshot(bot),
                'admin':{'password_set':bool(str(bot.config.get('qq.admin_password_hash','') or '')),
                         'minutes':getattr(sessions,'minutes',30),'active':sessions.count() if sessions else 0}}
    @app.post('/api/pc/admin-password')
    async def admin_password(request:Request):
        """设置 / 修改 / 清除管理员密码。只存 PBKDF2 哈希 + 盐，永不保存明文。"""
        body=await request.json();bot=app.state.bot
        if not isinstance(body,dict):raise HTTPException(400,'请求格式错误')
        new=str(body.get('new') or '');old=str(body.get('old') or '')
        stored=str(bot.config.get('qq.admin_password_hash','') or '')
        salt=str(bot.config.get('qq.admin_password_salt','') or '')
        # 改密码要验原密码；**清密码不验**：控制台本身就是本机可信入口（能直接改配置文件），
        # 忘记密码时必须留一条本机可恢复的路径。
        if new and stored and not verify_password(old,stored,salt):
            raise HTTPException(400,'修改密码需要先填写原密码')
        cleared=False
        if not new:
            bot.config.set('qq.admin_password_hash','');bot.config.set('qq.admin_password_salt','')
            cleared=True
            sessions=getattr(bot,'admin',None)
            if sessions:sessions.end_all()
            message='已清除管理员密码；远程变更回到本地审核流程。'
        else:
            problem=password_error(new)
            if problem:raise HTTPException(400,problem)
            digest,new_salt=hash_password(new)
            bot.config.set('qq.admin_password_hash',digest);bot.config.set('qq.admin_password_salt',new_salt)
            message='管理员密码已保存；在 QQ 私聊发“进入管理员模式”即可使用。'
        bot.config.save_atomic()
        return {'success':True,'message':message,'password_set':not cleared}
    @app.post('/api/pc/bind')
    async def bind_pc(request:Request):
        b=await request.json();bot=app.state.bot
        if not isinstance(b,dict):raise HTTPException(400,'请求格式错误')
        owner=b.get('owner')
        if not isinstance(owner,str):raise HTTPException(400,'主人标识格式错误')
        if owner and owner not in {c['id'] for c in pc_candidates(bot)}:raise HTTPException(400,'只能绑定当前账号实际收到过私聊的用户，请先私聊机器人并刷新列表')
        bot.plugins.update_settings('pc_control',{'ownerIds':owner})
        bot.db.execute("UPDATE pc_jobs SET state='cancelled' WHERE state IN ('pending','ready')");bot.db.commit()
        return {'success':True,'owners':owner,'enabled':bot.plugins.get_settings('pc_control')['enabled']}
    @app.post('/api/pc/action')
    async def local_action(request:Request):
        b=await request.json();bot=app.state.bot
        action=b.get('action') if isinstance(b,dict) else None
        if action not in WRITE_ACTIONS:raise HTTPException(400,'仅支持机器人暂停、恢复或重连')
        try:
            result,_=await ConsoleBackend(bot).execute(action)
            return {'success':True,'message':result}
        except Exception as e:raise HTTPException(400,str(e)[:160])
    @app.post('/api/pc/speed-profile')
    async def speed_profile(request:Request):
        """「快速回复」档位：默认只**算**出要改什么给用户看；``apply=true`` 才真的落盘。"""
        body=await request.json()
        apply=bool((body or {}).get('apply')) if isinstance(body,dict) else False
        bot=app.state.bot
        settings={pid:bot.plugins.get_settings(pid) for pid in bot.plugins._plugins}
        plan=profile_diff(settings)
        if not plan:
            return {'success':True,'changed':False,'plan':{},'message':'当前的等待类设置已经是快档，没有需要改的。'}
        if not apply:
            return {'success':True,'changed':False,'plan':plan,
                    'message':'确认后会把下面这些等待类设置调快（其它设置一律不动）：\n'+describe(plan)}
        applied={}
        for plugin_id,changes in plan.items():
            bot.plugins.update_settings(plugin_id,changes)
            applied[plugin_id]=changes
        # 回复超时：**只在明显过大时收一收**（>30 秒），并且不去动 20 秒这种合理值。
        # 教训：这里以前无条件压到 15 秒，结果中转站偶发变慢时一次抖动就变成
        # "没能生成回复"（用户截图报过）。速度快不等于把容错也砍掉。
        timeout_changed=False
        try:
            current=float(bot.config.get('ai.reply_timeout',20) or 20)
            if current>30:
                bot.config.set('ai.reply_timeout',20);bot.config.save_atomic();timeout_changed=True
        except Exception:
            pass
        # 超时重试：速度档位也要保证这一项是开着的（关掉会让偶发变慢直接变失败）
        retry_changed=False
        try:
            if not bot.config.get('ai.reply_timeout_retry',True):
                bot.config.set('ai.reply_timeout_retry',True);bot.config.save_atomic();retry_changed=True
        except Exception:
            pass
        # 回复长度上限：生成时间随输出长度增长（实测这条链路约 20~30ms/token），
        # 话多的模型靠它收口。0（不限）或偏大时压到 150；已经更小的保持用户的选择。
        limit_changed=False
        try:
            current=int(bot.config.get('ai.reply_max_tokens',0) or 0)
            if current==0 or current>150:
                bot.config.set('ai.reply_max_tokens',150);bot.config.save_atomic();limit_changed=True
        except Exception:
            pass
        message='已切到「快速回复」档位：\n'+describe(applied)
        if timeout_changed:
            message+='\n· 回复超时：→ 20 秒（原来偏大）'
        if retry_changed:
            message+='\n· 超时后自动重试：→ 打开（避免偶发变慢直接失败）'
        if limit_changed:
            message+='\n· 单条回复长度上限：→ 150 token（约 100 多字；生成时间随长度增长）'
        return {'success':True,'changed':True,'applied':applied,'plan':plan,'message':message}

    @app.post('/api/pc/review')
    async def review_pc(request:Request):
        b=await request.json()
        if type(b.get('approve')) is not bool:raise HTTPException(400,'审核开关格式错误')
        try:return await app.state.bot.pc.review(str(b.get('id','')),b['approve'])
        except ValueError as e:raise HTTPException(400,str(e))
