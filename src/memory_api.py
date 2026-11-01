"""Loopback-only memory management, protected by the host middleware."""
import uuid,time
from pathlib import Path
from fastapi import Request,HTTPException
from .chat_state import safe_fact, maintain_memories
from . import agent_diary, memory_guard, memory_ingest

def _backup_before_change(bot):
    """改动记忆前先做一致性快照；失败只返回原因，不阻断原操作。"""
    diary = getattr(bot, 'diary_path', None)
    if not diary:
        return {'path': '', 'name': '', 'bytes': 0, 'kept': 0,
                'removed': 0, 'skipped': 'no_diary_path'}
    try:
        folder = Path(diary).parent / '记忆' / '备份'
        return memory_guard.backup(
            bot.unified_memory, folder,
            db_path=getattr(bot, 'db_path', '') or '')
    except Exception as exc:
        return {'path': '', 'name': '', 'bytes': 0, 'kept': 0,
                'removed': 0, 'skipped': type(exc).__name__}

def maintain_message(guard, apply=True):
    """把体检结果写成一句中文；只报数量，绝不回显记忆正文或密钥。"""
    guard = guard or {}
    if guard.get('error'):
        return '整理完成（写回体检没跑起来：' + str(guard['error']) + '）'
    counts = guard.get('counts') or {}
    backup = guard.get('backup') or {}
    parts = ['合并重复 ' + str(int(guard.get('merged') or 0)) + ' 条']
    if apply:
        parts += ['超长截断 ' + str(int(guard.get('truncated') or 0)) + ' 条',
                  '密钥打码 ' + str(int(guard.get('masked') or 0)) + ' 条',
                  '近似重复归档 ' + str(int(guard.get('archived_dup') or 0)) + ' 条',
                  '久未使用归档 ' + str(int(guard.get('decayed') or 0)) + ' 条',
                  '补关键词 ' + str(int(guard.get('fixed_keywords') or 0)) + ' 条',
                  '关键词偏少 ' + str(len(guard.get('weak_keywords') or [])) + ' 条（未改）']
    else:
        parts += ['待截断 ' + str(int(counts.get('too_long') or 0)) + ' 条',
                  '含密钥 ' + str(int(counts.get('secret') or 0)) + ' 条',
                  '待降权 ' + str(int(guard.get('decay_candidates') or 0)) + ' 条',
                  '失效路径 ' + str(len(guard.get('dead_paths') or [])) + ' 条',
                  '疑似冲突 ' + str(len(guard.get('conflicts') or [])) + ' 条',
                  '冷宫 ' + str(len(guard.get('cold') or [])) + ' 条',
                  '关键词偏少 ' + str(len(guard.get('weak_keywords') or [])) + ' 条（未改）']
    if apply and backup.get('name'):
        parts.append('备份 ' + str(backup.get('name')))
    parts.append('剩余 ' + str(int(guard.get('remaining') or 0)) + ' 条')
    return '；'.join(parts) + '。'


def ingest_message(result, apply=False, days=7):
    """提取结果写成一句中文：只报数量 + 前三条候选正文，不整段回显聊天记录。"""
    result = result or {}
    checked = int(result.get('checked') or 0)
    skipped = result.get('skipped') or {}
    if not checked:
        return '最近 ' + str(days) + ' 天没有主人私聊记录（或还没绑定主人 QQ）——先跟机器人私聊几句再来提取。'
    count = len(result.get('candidates') or [])
    extra = []
    for key, label in (('secret', '含密钥'), ('duplicate', '重复')):
        if int(skipped.get(key) or 0):
            extra.append(label + ' ' + str(int(skipped[key])) + ' 条已跳过')
    tail = ('（' + '、'.join(extra) + '）') if extra else ''
    if not apply:
        if not count:
            return '扫了 ' + str(checked) + ' 条私聊，没有值得记的新内容' + tail + '。'
        samples = '；'.join('「' + str(item.get('text') or '')[:30] + '」'
                            for item in (result.get('candidates') or [])[:3])
        return '扫了 ' + str(checked) + ' 条私聊，挑出 ' + str(count) + ' 条候选' + tail + '：' + samples
    return '已写入 ' + str(int(result.get('added') or 0)) + ' 条记忆（扫了 ' + str(checked) + ' 条私聊）' + tail + '。'


def register_memory_api(app):
    @app.get('/api/memory/unified')
    async def read_unified(q: str = '', limit: int = 8, layer: str = '',
                           project: str = '', days: int = 0):
        bot = app.state.bot
        return {'success': True,
                'items': bot.unified_memory.search(q, limit, layer=layer,
                                                   project=project, days=days),
                'count': bot.unified_memory.count(),
                'layers': bot.unified_memory.layers(),
                'diaries': bot.unified_memory.diaries(7),
                'diaryPath': str(getattr(bot, 'diary_path', '') or '')}

    @app.post('/api/memory/unified')
    async def write_unified(request: Request):
        body = await request.json(); bot = app.state.bot
        try:
            action = body.get('action')
            if action == 'add':
                item = bot.unified_memory.remember(
                    body.get('content'), source=body.get('source', 'desktop'),
                    importance=body.get('importance', 50), ttl_days=body.get('ttl_days', 0),
                    tags=body.get('tags') or [], persona=bot.style.active_name,
                    layer=body.get('layer', 'fact'), keywords=body.get('keywords') or [],
                    project=body.get('project', ''), confidence=body.get('confidence'),
                    evidence=body.get('evidence', ''), goal=body.get('goal', ''))
                return {'success': True, 'item': item}
            if action == 'update':
                item = bot.unified_memory.update(
                    str(body.get('id') or ''), content=body.get('content'),
                    layer=body.get('layer'), keywords=body.get('keywords'),
                    project=body.get('project'), status=body.get('status'),
                    confidence=body.get('confidence'), importance=body.get('importance'),
                    evidence=body.get('evidence'), goal=body.get('goal'))
                return {'success': True, 'item': item}
            if action == 'reflect':
                item = bot.unified_memory.reflect(
                    body.get('summary') or body.get('content'), lesson=body.get('lesson', ''),
                    project=body.get('project', ''), source=body.get('source', 'chat'),
                    evidence=body.get('evidence', ''))
                return {'success': True, 'item': item}
            if action == 'dream':
                backup = _backup_before_change(bot)
                return {'success': True, 'backup': backup, **bot.unified_memory.dream()}
            if action == 'forget':
                return {'success': True, 'removed': bot.unified_memory.forget(str(body.get('id') or ''))}
            if action == 'maintain':
                backup = _backup_before_change(bot)
                return {'success': True, 'backup': backup, **bot.unified_memory.maintain()}
            raise ValueError('未知记忆操作')
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get('/api/memory/diary')
    async def read_diary(day: str = '', limit: int = 30):
        bot = app.state.bot
        return {'success': True, 'diary': bot.unified_memory.diary(day) if day else None,
                'items': bot.unified_memory.diaries(limit),
                'files': agent_diary.list_days(bot, 90),
                'dir': str(agent_diary.diary_dir_for(bot))}

    @app.post('/api/memory/diary/auto')
    async def auto_diary(request: Request):
        body = await request.json(); bot = app.state.bot
        if body.get('confirmed') is not True:
            raise HTTPException(400, '需要确认后才能让 AI 写日记')
        try:
            day = agent_diary.normalize_day(body.get('day'))
            force = bool(body.get('force'))
            if agent_diary.has_day(bot, day) and not force:
                return {'success': True, 'skipped': True, 'day': day,
                        'path': str(agent_diary.diary_file(bot, day)),
                        'message': f'{day} 的日记已经写过了；要重写请勾选覆盖。'}
            maker = getattr(bot, '_make_ai', None)
            engine = maker() if callable(maker) else None
            if engine is None:
                raise ValueError('没有可用的模型配置，无法让 AI 写日记')
            try:
                text = await agent_diary.compose(bot, engine, day)
            finally:
                try:
                    closer = getattr(engine, 'close', None)
                    if callable(closer):
                        await closer()
                except Exception:
                    pass
            info = agent_diary.save(bot, day, text)
            return {'success': True, 'skipped': False, **info,
                    'message': f'AI 已写完 {day} 的日记：{info["chars"]} 字'}
        except HTTPException:
            raise
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from None
        except Exception as exc:
            raise HTTPException(500, '让 AI 写日记失败：' + type(exc).__name__ + ': '
                                + str(exc)[:200]) from None

    @app.post('/api/memory/diary')
    async def write_diary(request: Request):
        body = await request.json(); bot = app.state.bot
        try:
            item = bot.unified_memory.write_diary(body.get('day'), body.get('content'),
                                                   meta=body.get('meta') or {})
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from None
        return {'success': True, 'diary': item}
    @app.get('/api/memory')
    async def read_memory(q:str=''):
        bot=app.state.bot;name=bot.style.active_name
        bot.db.execute('CREATE TABLE IF NOT EXISTS self_memories(id TEXT PRIMARY KEY,persona TEXT,content TEXT,enabled INTEGER,created REAL)')
        manual=[dict(id=i,content=c,enabled=bool(e)) for i,c,e in bot.db.execute('SELECT id,content,enabled FROM self_memories WHERE persona=? ORDER BY created',(name,))]
        rows=bot.db.execute("SELECT id,scope,subject,content,status,hits FROM chat_memories "
                            "WHERE content LIKE ? AND status<>'superseded' ORDER BY updated DESC LIMIT 200",
                            ('%'+q[:100]+'%',)).fetchall()
        return {'success':True,'persona':name,'manual':manual,'structured':[dict(id=i,scope=s,subject=u,content=c,status=t,evidence=h) for i,s,u,c,t,h in rows],
            'autoRemember':bot.plugins.get_settings('conversation_memory')['autoRemember']}
    @app.post('/api/memory/maintain')
    async def maintain_memory(request:Request):
        """整理记忆：合并重复 + 写回体检 + 按半衰期归档久未用的条目。

        写回体检（超长截断/密钥打码/近似重复归档）默认就执行 —— 用户点按钮就是要它动手；
        带 {"apply": false} 时只报告，界面拿来做「先看看有什么问题」。
        """
        try:
            body=await request.json()
        except Exception:
            body={}
        apply=bool(body.get('apply', True)) if isinstance(body,dict) else True
        bot=app.state.bot
        # 先让守卫备份并整理 unified_memories，再跑旧的 chat_memories 清理；
        # 备份必须在任何删改之前完成。
        try:
            guard=memory_guard.maintain(bot.unified_memory,apply=apply,
                                          backup_dir=bot.diary_path.parent/'记忆'/'备份',
                                          db_path=getattr(bot,'db_path','') or '')
        except Exception as exc:
            guard={'error':type(exc).__name__}
        legacy=maintain_memories(bot.db)
        return {'success':True,**legacy,'guard':guard,'apply':apply,
                'message':maintain_message(guard,apply)}

    @app.get('/api/memory/guard')
    async def read_guard():
        """只读体检报告：哪些记忆超长/含密钥/像流水账/近似重复/该降权，不动数据。"""
        return {'success':True,**memory_guard.report(app.state.bot.unified_memory)}

    @app.post('/api/memory/report')
    async def export_report():
        """只读体检导出 Markdown：报告写到日记同级的 记忆/ 目录，不改任何记忆行。"""
        bot=app.state.bot
        try:
            result=memory_guard.write_report(
                bot.unified_memory, out_dir=bot.diary_path.parent / '记忆',
                db_path=getattr(bot, 'db_path', '') or '')
        except Exception as exc:
            raise HTTPException(500,'生成体检报告失败：'+type(exc).__name__)
        result['message']='体检报告已写入：'+str(result.get('path') or '')
        return {'success':True,**result}

    @app.get('/api/memory/backups')
    async def list_backups(limit: int = 20):
        """列出记忆库备份文件；只读，最新在前。"""
        bot=app.state.bot
        folder=bot.diary_path.parent / '记忆' / '备份'
        try:
            items=memory_guard.list_backups(folder, limit=max(1,min(100,int(limit or 20))))
        except Exception as exc:
            raise HTTPException(500,'读取备份列表失败：'+type(exc).__name__)
        return {'success':True,'dir':str(folder),'count':len(items),'items':items}

    @app.get('/api/memory/ingest')
    async def preview_ingest(days: int = 7, limit: int = 20):
        """零 token 提取预演：扫主人最近私聊，只列候选，不写库。"""
        bot=app.state.bot
        days=max(1,min(90,int(days or 7)));limit=max(1,min(50,int(limit or 20)))
        try:
            messages=memory_ingest.inbox_messages(bot,days=days,limit=400)
            result=memory_ingest.run(bot.unified_memory,messages,apply=False,limit=limit,
                                     persona=bot.style.active_name)
        except Exception as exc:
            raise HTTPException(500,'提取记忆失败：'+type(exc).__name__)
        return {'success':True,'days':days,**result,
                'message':ingest_message(result,False,days)}

    @app.post('/api/memory/ingest')
    async def write_ingest(request:Request):
        """确认后才落库：只写 fact 层，默认 apply=false 仍然只预览。"""
        try:
            body=await request.json()
        except Exception:
            body={}
        body=body if isinstance(body,dict) else {}
        bot=app.state.bot
        days=max(1,min(90,int(body.get('days') or 7)))
        limit=max(1,min(50,int(body.get('limit') or 20)))
        apply=bool(body.get('apply',False))
        try:
            messages=memory_ingest.inbox_messages(bot,days=days,limit=400)
            result=memory_ingest.run(bot.unified_memory,messages,apply=apply,limit=limit,
                                     persona=bot.style.active_name)
        except Exception as exc:
            raise HTTPException(500,'提取记忆失败：'+type(exc).__name__)
        return {'success':True,'days':days,**result,
                'message':ingest_message(result,apply,days)}

    @app.post('/api/memory')
    async def write_memory(request:Request):
        b=await request.json();bot=app.state.bot
        try:
            action=b.get('action');mid=str(b.get('id',''))
            if action=='add':
                text=safe_fact(b.get('content'))
                if bot.db.execute('SELECT count(*) FROM self_memories WHERE persona=?',(bot.style.active_name,)).fetchone()[0]>=100:raise ValueError('每个人设最多100条手动记忆')
                bot.db.execute('INSERT INTO self_memories VALUES(?,?,?,?,?)',(uuid.uuid4().hex,bot.style.active_name,text,1,time.time()))
            elif action in ('enable','delete'):
                if action=='enable':
                    if type(b.get('enabled')) is not bool:raise ValueError('开关格式错误')
                    bot.db.execute('UPDATE self_memories SET enabled=? WHERE id=? AND persona=?',(int(b['enabled']),mid,bot.style.active_name))
                else:bot.db.execute('DELETE FROM self_memories WHERE id=? AND persona=?',(mid,bot.style.active_name))
            elif action=='forget':bot.db.execute('DELETE FROM chat_memories WHERE id=?',(mid,))
            elif action=='auto':bot.plugins.update_settings('conversation_memory',{'autoRemember':b['enabled']})
            else:raise ValueError('未知记忆操作')
            bot.db.commit();return {'success':True}
        except (ValueError,KeyError) as e:raise HTTPException(400,str(e))
