"""Local-only sticker administration routes (mounted behind console guards)."""
import base64
from pathlib import Path
from fastapi import HTTPException,Request
from .sticker_library import decode_image,frames_for_vision,MAX_BYTES

def install_routes(app):
    def service():
        bot=app.state.bot
        plugin=bot.plugins._plugins.get('sticker_admin')
        if not plugin or not plugin.enabled:raise HTTPException(403,'表情包管理已关闭')
        if not bot.stickers:raise HTTPException(503,bot.sticker_error)
        return bot,bot.stickers

    @app.get('/api/stickers')
    async def library(q:str='',page:int=1):
        bot,store=service()
        if len(q)>100 or page<1:raise HTTPException(400,'搜索词或页码无效')
        items=[e for e in store.entries() if q.lower() in (e['label']+' '+e.get('description','')).lower()]
        items.sort(key=lambda e:e['created'],reverse=True)
        total=len(items);items=items[(page-1)*12:page*12]
        for entry in items:
            try:
                from PyQt6.QtGui import QImage
                from PyQt6.QtCore import QBuffer,QIODevice,Qt
                image=QImage.fromData(store.path(entry['id']).read_bytes())
                image=image.scaled(96,96,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation)
                out=QBuffer();out.open(QIODevice.OpenModeFlag.WriteOnly);image.save(out,'PNG')
                entry['thumbnail']=base64.b64encode(bytes(out.data())).decode()
            except Exception:entry['thumbnail']=''
        return {'items':items,'total':total,'page':page,'pages':max(1,(total+11)//12),
                'official_reactions_supported':False,'reaction_available':bot.mode=='onebot'}

    @app.post('/api/stickers/import')
    async def import_images(request:Request):
        bot,store=service()
        body=await request.json();items=body.get('items') if isinstance(body,dict) else None
        if not isinstance(items,list) or not 1<=len(items)<=20:raise HTTPException(400,'每次请选择1～20张图片')
        managed=(bot.root/'data/plugin_uploads/sticker_admin').resolve();pending=[];total=0
        try:
            for item in items:
                if not isinstance(item,dict):raise ValueError('上传记录无效')
                path=Path(item.get('path','')).resolve()
                if not path.is_relative_to(managed) or not path.is_file():raise ValueError('请使用管理窗的上传按钮选择图片')
                if path.stat().st_size>MAX_BYTES:raise ValueError('单张图片不能超过6MB')
                data=path.read_bytes();total+=len(data)
                if total>64*1024*1024:raise ValueError('本次导入不能超过64MB')
                _,_,buffer=decode_image(data);buffer.close()
                label=str(item.get('label',''))[:40].strip()
                for char in '[]|\r\n':label=label.replace(char,'_')
                pending.append((data,label))
            added=sum(store.put(data,label)[1] for data,label in pending)
        except (ValueError,OSError) as exc:raise HTTPException(400,str(exc)) from None
        return {'success':True,'message':f'已导入{added}张；重复图片已合并','added':added}

    @app.post('/api/stickers/{key}')
    async def edit(key:str,request:Request):
        _,store=service();body=await request.json()
        if not isinstance(body,dict):raise HTTPException(400,'设置格式无效')
        try:store.update(key,body.get('label'),body.get('description',''))
        except KeyError:raise HTTPException(404,'表情不存在') from None
        except (ValueError,OSError) as exc:raise HTTPException(400,str(exc)) from None
        return {'success':True,'message':'标签和图片说明已保存'}

    @app.delete('/api/stickers/{key}')
    async def remove(key:str):
        _,store=service()
        try:store.remove(key)
        except KeyError:raise HTTPException(404,'表情不存在') from None
        return {'success':True,'message':'已删除机器人本地收藏，不影响QQ收藏和手动图库'}

    @app.post('/api/stickers/{key}/recognize')
    async def recognize(key:str):
        bot,store=service()
        try:data=store.path(key).read_bytes()
        except (KeyError,OSError):raise HTTPException(404,'表情图片不存在') from None
        plugin=bot.plugins._plugins.get('image_understanding')
        if not plugin or not plugin.enabled:raise HTTPException(400,'请先启用图片与表情包识别')
        try:
            from urllib.parse import urlsplit
            model=str(plugin.get('visionModel','')).strip() or ('deepseek-flash' if urlsplit(bot.ai.api_url).hostname=='api.deepseek.com' else bot.ai.model)
            sources=frames_for_vision(data,int(bot.plugins._plugins['sticker_admin'].get('gifFrames',2)))
            async with bot.ai_lock:description=await bot.ai.describe_images(sources,model=model)
            if not description:raise ValueError('视觉模型没有返回内容，请检查识图模型和接口状态')
            store.update(key,store._entries[key]['label'],str(description)[:1000])
        except KeyError:raise HTTPException(404,'图片已被删除，请刷新列表') from None
        except (ValueError,OSError) as exc:raise HTTPException(400,str(exc)) from None
        return {'success':True,'message':'识别完成，已更新图片说明'}
