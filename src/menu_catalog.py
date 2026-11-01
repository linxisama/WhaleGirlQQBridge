"""控制台和QQ消息共用的指令目录；管理端配置只导出，不伪称已发布。"""
import re


def official_menu_payload(menu,text):
    commands=menu.get('commands') or []
    buttons=[]
    for index,item in enumerate(commands[:6]):
        if not isinstance(item,(list,tuple)) or len(item)!=2:continue
        command,label=map(str,item)
        if not command.startswith('/') or len(command)>80:continue
        buttons.append({'id':f'menu-{index}','render_data':{'label':label[:20],'visited_label':label[:20],'style':1},
            'action':{'type':2,'permission':{'type':2},'data':command,'enter':False,'reply':False,
                      'unsupport_tips':'请手动发送 '+command}})
    rows=[{'buttons':buttons[n:n+2]} for n in range(0,len(buttons),2)]
    content=str(text or '')[:3000]
    cover=menu.get('cover_url','')
    if isinstance(cover,str) and re.fullmatch(r'https://[^\s()<>]{1,1800}',cover):content='![封面]('+cover+')\n'+content
    return {'msg_type':2,'content':' ','markdown':{'content':content},'keyboard':{'content':{'rows':rows}}}


def install_routes(app):
    @app.get('/api/command-catalog')
    async def catalog():
        # 旧包体可能没有功能菜单插件；目录接口降级为空，不能让 QQ 消息主循环崩掉。
        try:
            from .plugins.quick_menu import COMMANDS
        except ImportError:
            COMMANDS = (('/帮助','查看可用功能（以实际安装的插件为准）'),)
        return {'commands':[dict(command=c,description=d,trigger='发送指令') for c,d in COMMANDS],
            'note':'在 QQ 官方管理端「高级设置 → 菜单与指令」选择好友或群聊场景，逐项添加发送指令。发布与场景权限由官方管理端处理。'}

    @app.get('/api/group-diagnostics')
    async def diagnostics():
        import time
        bot=app.state.bot
        rows=bot.db.execute("SELECT at,event,kind,room,outcome,reason FROM inbound_audit WHERE mode=? ORDER BY at DESC LIMIT 100",(bot.mode,)).fetchall()
        groups=[dict(at=a,event=e,room=r,outcome=o,reason=s) for a,e,k,r,o,s in rows if k=='group' or e=='GROUP_AT_MESSAGE_CREATE'][:20]
        latest=bot.db.execute("SELECT state,error,created FROM inbox WHERE mode=? AND account=? AND room LIKE 'group:%' ORDER BY created DESC LIMIT 1",(bot.mode,bot.account())).fetchone()
        return {'mode':bot.mode,'running':bot.running,'paused':bot.reply_paused,'worker_alive':bool(bot.worker and not bot.worker.done()) if hasattr(bot,'worker') else None,
            'official_connected':bot.official.ws_connected if bot.mode=='official' else None,'groups':groups,
            'last_heartbeat_ack':getattr(bot.official,'last_heartbeat_ack',0),'heartbeat_pending':getattr(bot.official,'_heartbeat_pending',False),
            'last_group_delivery':dict(state=latest[0],error=latest[1],at=latest[2]) if latest else None,
            'checked_at':time.time(),'hint':'官方群聊需要点选真正的 @机器人，且机器人已加入并有该群场景权限。无新入站事件时，检查QQ官方场景、发布/沙箱白名单及群内机器人状态；已入队则查看发送结果或具体拦截原因。'}
