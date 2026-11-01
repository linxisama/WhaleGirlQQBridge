"""Robot-only administration. Never imports or invokes host computer control APIs."""
import time,re,asyncio
from .runtime_identity import APP_VERSION

def admin_ids(raw):return list(dict.fromkeys(v.strip() for v in re.split('[,，\n]',str(raw or '')) if v.strip()))

READ_ACTIONS={'status','connections','queue','logs','personas','plugins','speed'}
WRITE_ACTIONS={'pause','resume','reconnect'}

def clean_log(text):
    text=re.sub(r'sk-[A-Za-z0-9_-]{8,}','[已隐藏密钥]',str(text))
    text=re.sub(r'(?i)(api[_ -]?key|password|secret|token)\s*[:=]\s*[^\s,，;；]+',r'\1=[已隐藏]',text)
    return text[:180]

def snapshot(bot):
    counts=dict(bot.db.execute('SELECT state,count(*) FROM inbox WHERE mode=? AND account=? GROUP BY state',(bot.mode,bot.account())).fetchall())
    plugins=[dict(id=p.META.id,name=p.META.name,enabled=p.enabled) for p in bot.plugins._plugins.values()]
    return {'version':APP_VERSION,'running':bool(bot.running),'reply_paused':bool(getattr(bot,'reply_paused',False)),
        'worker_alive':bool(bot.worker and not bot.worker.done()),'model':bot.ai.model,
        'mode':bot.mode,'qq_connected':bool(getattr(bot.official,'ws_connected',False)) if bot.mode=='official' else bool(bot.onebot.online),
        'authenticated':bool(bot.official.authenticated) if bot.mode=='official' else bool(bot.onebot.online),
        'queue':{k:counts.get(k,0) for k in ('pending','sending','paused','failed','partial','uncertain','expired')},
        'plugins':plugins,'persona':bot.style.active_name,'personas':[{'name':v.get('name',''),'display_name':v.get('display_name','')} for v in bot.style.list_styles()],
        'logs':[dict(time=v['time'],text=clean_log(v['text'])) for v in bot.events[-8:]]}

def read_status(bot,action):
    s=snapshot(bot)
    if action=='status':return f"鲸鱼娘 {s['version']}\n机器人：{'运行中' if s['running'] else '未启动'}；回复：{'已暂停' if s['reply_paused'] else '正常'}\nQQ：{'已连接' if s['qq_connected'] else '未连接'}；模型：{s['model']}\n当前人设：{s['persona']}\n插件：{sum(v['enabled'] for v in s['plugins'])}/{len(s['plugins'])} 已启用\n队列待处理：{s['queue']['pending']}；待核实：{s['queue']['uncertain']}"
    if action=='connections':return f"QQ 接入：{'官方' if s['mode']=='official' else '普通 QQ'}\n鉴权：{'正常' if s['authenticated'] else '未就绪'}\n消息连接：{'正常' if s['qq_connected'] else '未连接'}\n聊天模型：{s['model']}\n回复任务：{'运行中' if s['worker_alive'] else '未运行'}"
    if action=='queue':return '机器人消息队列：\n'+'\n'.join(f'{label}：{s["queue"][key]}' for key,label in [('pending','等待处理'),('sending','发送中'),('paused','暂停的回复'),('failed','失败'),('partial','文字已发、图片未完成'),('uncertain','发送结果待核实'),('expired','已过期')])
    if action=='logs':return '机器人最近运行记录：\n'+('\n'.join(v['text'] for v in s['logs']) or '暂无运行记录')
    if action=='personas':return '当前人设：'+s['persona']+'\n可用人设：'+('、'.join(v['display_name'] or v['name'] for v in s['personas']) or '默认人设')
    if action=='plugins':return '机器人插件：\n'+'\n'.join(('✓ ' if v['enabled'] else '○ ')+v['name'] for v in s['plugins'])[:1800]
    if action=='speed':
        data=bot.latency_summary() if hasattr(bot,'latency_summary') else {'count':0}
        if not data.get('count'):return '还没有回复记录，先聊两句再看速度。'
        def secs(key):return f"{float(data.get(key,0))/1000:.2f}"
        return (f"最近 {data['count']} 条回复的平均耗时：{secs('total_ms')} 秒"
                f"（最快 {secs('fastest_ms')} / 最慢 {secs('slowest_ms')}）\n"
                f"拆解：排队 {secs('queue_ms')}s + 等锁 {secs('lock_ms')}s + 插件 {secs('plugin_ms')}s"
                f" + 模型 {secs('model_ms')}s + 发送 {secs('send_ms')}s\n"
                f"模型：{data.get('model') or '未知'}\n"
                "哪一段大就治哪一段：「模型」大 → 设置页「模型测速」换最快的；"
                "「插件」大 → 看「连发合并」窗口和插件的联网读取；"
                "「排队」大 → 前面还有消息没处理完。")
    raise ValueError('不支持的机器人查询')

def set_paused(bot, paused: bool) -> str:
    """暂停/恢复自动回复（本地按钮与管理员模式共用同一份实现）。

    暂停状态会写进 pc_control 的状态文件，所以重启后仍然保持——这是"暂停回复"
    这个显式操作的语义；违禁词那种"警告式暂停"则只留在内存里。
    """
    plugin = bot.plugins._plugins.get('pc_control')
    if plugin is not None:
        plugin.save_state('robot_admin.json', {'paused': bool(paused)})
    bot.reply_paused = bool(paused)
    if not paused:
        bot.db.execute("UPDATE inbox SET state='pending' WHERE state='paused' AND mode=? AND account=?",
                       (bot.mode, bot.account()))
        bot.db.commit()
    bot.note('机器人自动回复已暂停；管理员通道保留' if paused else '机器人自动回复已恢复')
    return ('已暂停机器人自动回复；QQ 连接和管理员指令仍可使用。' if paused else '已恢复机器人自动回复。')


class ConsoleBackend:
    def __init__(self, runtime): self.runtime = runtime
    async def execute(self, action, arg=''):
        bot = self.runtime
        if action not in WRITE_ACTIONS: raise ValueError('仅能管理机器人回复和 QQ 连接，不能操作宿主机器')
        if action in ('pause', 'resume'):
            return set_paused(bot, action == 'pause'), None
        if not bot.running:raise ValueError('机器人尚未启动，请先在控制台启动')
        if bot.mode=='official':
            await bot.official.authorize(force=True)
            task=getattr(bot,'official_ws',None)
            if task:task.cancel();await asyncio.gather(task,return_exceptions=True)
            bot.official_ws=asyncio.create_task(bot._official_receiver());bot.note('管理员请求重新连接官方 QQ')
            return '已启动 QQ 消息连接重连，实际连接状态请用“机器人 连接”查询。',None
        await bot.onebot.probe()
        return '已重新检查普通 QQ 接入状态；连接由普通 QQ 接入端维持。',None
