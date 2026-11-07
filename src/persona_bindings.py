"""按账号、群和发送者选择人设；每条消息独立解析，不切换全局当前人设。"""
import hashlib
import json
import re


def binding_plugin(runtime):
    return getattr(getattr(runtime, 'plugins', None), '_plugins', {}).get('archive_isolation')


def bindings(runtime):
    plugin=binding_plugin(runtime)
    state=plugin.load_state('bindings.json',{}) if plugin else {}
    rows=state.get('rows',[]) if isinstance(state,dict) else []
    return [r for r in rows if isinstance(r,dict)]


def resolve_persona(runtime,message):
    kind=str(message.get('kind') or '')
    target=str(message.get('target') or '')
    sender=str(message.get('sender_id') or (target if kind=='private' else ''))
    candidates=[];missing=[];blocked=[]
    for row in bindings(runtime):
        if row.get('mode')!=runtime.mode or row.get('account')!=runtime.account():continue
        if row.get('kind')!=kind or row.get('target')!=target:continue
        if row.get('user') and row['user']!=sender:continue
        persona=runtime.style._styles.get(row.get('persona'))
        if not persona:
            missing.append(str(row.get('persona') or ''));continue
        # 绑定不能绕过违禁词闸门：set_active 会拒绝切换的人设，经会话绑定直接取用
        # 就等于绕过去了 —— 违禁词人设被绑到某个群，那个群就成了唯一不受管的地方。
        if runtime.style.persona_violations(persona):
            blocked.append(str(persona.name));continue
        candidates.append((bool(row.get('user')),persona))
    # 绑定里写的人设名不存在时不能静默回落：那会让整段对话悄悄换成人设，
    # 用户看不出是绑定坏了还是关键词没命中。原因记进 message 供上层提示。
    reason=''
    if candidates:
        persona=max(candidates,key=lambda r:r[0])[1];reason='binding'
        # 绑定与关键词打架时，绑定赢（会话绑定是用户显式指定的），但冲突要留痕：
        # 不然用户会奇怪「我在这个群设了人设，为什么她偶尔像另一个人」—— 那句话里
        # 恰好有人设关键词，而绑定只在有绑定的会话里生效，别处仍按关键词走。
        _by_key= runtime.style.match_keywords(str(message.get('text') or ''))
        if _by_key is not None and _by_key.name != persona.name:
            reason += '+keyword:' + str(_by_key.name)
    else:
        persona=runtime.style.match_keywords(str(message.get('text') or ''))
        if persona:reason='keyword'
        else:persona=runtime.style.get();reason='default'
        if missing:reason+='+missing:'+','.join(sorted(set(missing))[:3])
        if blocked:reason+='+blocked:'+','.join(sorted(set(blocked))[:3])
    message['_effective_persona']=persona.name
    message['_persona_bound']=bool(candidates)
    message['_persona_reason']=reason
    return persona


def revision(rows):
    return hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def validate_rows(rows,runtime):
    if not isinstance(rows,list) or len(rows)>500:raise ValueError('最多设置500条绑定')
    result=[];seen=set()
    for row in rows:
        if not isinstance(row,dict) or set(row)-{'kind','target','user','persona'}:raise ValueError('绑定格式错误')
        kind=row.get('kind');target=row.get('target');user=row.get('user','');persona=row.get('persona')
        if kind not in ('group','private'):raise ValueError('请选择群聊或私聊')
        for value in (target,user):
            if not isinstance(value,str) or (value and not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value)):
                raise ValueError('会话与用户ID只能包含字母、数字、下划线和短横线')
        if not target:raise ValueError('请选择或填写会话ID')
        if persona not in runtime.style._styles:raise ValueError('人设不存在，请刷新后重新选择')
        key=(kind,target,user)
        if key in seen:raise ValueError('同一会话和用户不能重复绑定')
        seen.add(key)
        result.append(dict(kind=kind,target=target,user=user,persona=persona,mode=runtime.mode,account=runtime.account()))
    return result


def install_routes(app):
    from fastapi import Request,HTTPException
    @app.get('/api/persona-bindings')
    async def read():
        bot=app.state.bot
        rows=[r for r in bindings(bot) if r.get('mode')==bot.mode and r.get('account')==bot.account()]
        recent=bot.db.execute("SELECT kind,room,sender,max(at) FROM inbound_audit WHERE mode=? AND kind IN ('group','private') GROUP BY kind,room,sender ORDER BY max(at) DESC LIMIT 100",(bot.mode,)).fetchall()
        # 绑定不能绕过违禁词闸门（见 resolve_persona），所以这里把「失效的绑定」也报给前端：
        # 否则用户看到一条绑定写得好好的，实际那个群一直用默认人设，没有任何地方提示。
        bad={}
        for row in rows:
            persona=bot.style._styles.get(row.get('persona'))
            if persona is None:
                bad[row.get('persona')]='missing'
            elif bot.style.persona_violations(persona):
                bad[row.get('persona')]='blocked'
        return {'rows':[{k:r.get(k,'') for k in ('kind','target','user','persona')} for r in rows],
            'revision':revision(rows),'mode':bot.mode,'personas':[{'name':p.name,'display_name':p.display_name} for p in bot.style._styles.values()],
            'invalid':bad,
            'recent':[dict(kind=k,target=str(room).removeprefix(k+':'),user=u or '') for k,room,u,_ in recent]}

    @app.post('/api/persona-bindings')
    async def write(request:Request):
        try:
            raw=await request.body()
            if len(raw)>200000:raise ValueError('绑定内容过大')
            body=json.loads(raw)
            if not isinstance(body,dict) or set(body)-{'rows','revision'}:raise ValueError('请求格式错误')
            bot=app.state.bot
            rows=bindings(bot);current=[r for r in rows if r.get('mode')==bot.mode and r.get('account')==bot.account()]
            if body.get('revision')!=revision(current):raise ValueError('绑定已变化，请刷新后重新编辑')
            updated=validate_rows(body.get('rows'),bot)
            other=[r for r in rows if r not in current]
            plugin=binding_plugin(bot)
            if not plugin:raise ValueError('人设存档服务未加载')
            path=plugin.state_file('bindings.json');tmp=path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps({'rows':other+updated},ensure_ascii=False,indent=2),encoding='utf-8')
            tmp.replace(path)
            return {'success':True,'message':'绑定已保存，下一条消息生效。已有历史与当前全局人设保留。'}
        except (ValueError,TypeError) as exc:raise HTTPException(400,str(exc)) from None
