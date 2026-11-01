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
    candidates=[]
    for row in bindings(runtime):
        if row.get('mode')!=runtime.mode or row.get('account')!=runtime.account():continue
        if row.get('kind')!=kind or row.get('target')!=target:continue
        if row.get('user') and row['user']!=sender:continue
        persona=runtime.style._styles.get(row.get('persona'))
        if persona:candidates.append((bool(row.get('user')),persona))
    persona=max(candidates,key=lambda r:r[0])[1] if candidates else (
        runtime.style.match_keywords(str(message.get('text') or '')) or runtime.style.get())
    message['_effective_persona']=persona.name
    message['_persona_bound']=bool(candidates)
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
        return {'rows':[{k:r.get(k,'') for k in ('kind','target','user','persona')} for r in rows],
            'revision':revision(rows),'mode':bot.mode,'personas':[{'name':p.name,'display_name':p.display_name} for p in bot.style._styles.values()],
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
