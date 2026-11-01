"""Overlapping, bounded long-image tiles. Never implies omitted areas were read."""
import base64,math
from urllib.parse import urlsplit
from . import PluginBase,PluginMeta,FieldSchema
from ..sticker_library import fetch_image

def plan_tiles(width,height,maximum=12):
    if width<1 or height<1 or width*height>32_000_000:raise ValueError('图片尺寸超限')
    scale=min(1,1200/width);crop=math.floor(1800/scale);overlap=math.ceil(120/scale)
    result=[];top=0
    while top<height and len(result)<maximum:
        size=min(crop,height-top);result.append((top,size))
        if top+size>=height:break
        top+=crop-overlap
    return result,result[-1][0]+result[-1][1]<height

def tile_data(data,maximum=12):
    from PyQt6.QtCore import QBuffer,QByteArray,QIODevice
    from PyQt6.QtGui import QImageReader
    buf=QBuffer();buf.setData(QByteArray(data));buf.open(QIODevice.OpenModeFlag.ReadOnly)
    reader=QImageReader(buf);size=reader.size();w,h=size.width(),size.height()
    plan,partial=plan_tiles(w,h,maximum)
    if h<=2048 or h/max(w,1)<3:return None
    image=reader.read()
    if image.isNull():raise ValueError('长图无法解码')
    parts=[];total=0
    for top,height in plan:
        tile=image.copy(0,top,w,height)
        if tile.width()>1200:tile=tile.scaledToWidth(1200)
        out=QBuffer();out.open(QIODevice.OpenModeFlag.WriteOnly);tile.save(out,'JPEG',85)
        raw=bytes(out.data());total+=len(raw)
        if total>12*1024*1024:partial=True;break
        parts.append('data:image/jpeg;base64,'+base64.b64encode(raw).decode())
    return parts,partial

class LongImageReader(PluginBase):
    META=PluginMeta(id='long_image_reader',name='长图分段阅读',category='media',
        description='长截图按从上到下分段并留重叠区，由现有视觉模型读取；超出预算或某段失败会明确标注未读。',
        config_schema={'enabled':FieldSchema(type='bool',label='启用'),
            'maxTiles':FieldSchema(type='int',label='每张长图最多分段数',min=2,max=24)},
        default_settings={'enabled':True,'maxTiles':12})
    def tiles(self,data):return tile_data(data,int(self.get('maxTiles',12)))

    async def describe(self,message,runtime,model):
        images=[m for m in message.get('media',[]) if isinstance(m,dict) and m.get('type')=='image']
        if len(images)!=1:return False
        item=images[0];source=item.get('url') or item.get('file','')
        try:
            store=getattr(runtime,'stickers',None)
            if store:
                data=(await store.resolve(item,runtime))['data']
            else:
                if not str(source).startswith(('https://','http://','base64://','data:')) and runtime.mode in ('onebot','qqbridge'):
                    peer=runtime.bridge if runtime.mode=='qqbridge' else runtime.onebot
                    info=await peer.call('get_image',{'file':str(source)})
                    source=info.get('url','') if isinstance(info,dict) else ''
                data=await fetch_image(source)
            tiles=tile_data(data,int(self.get('maxTiles',12)))
        except Exception:return False
        if not tiles:return False
        return await self.describe_parts(message,runtime,tiles,model)

    async def describe_parts(self,message,runtime,tiles,model):
        import asyncio
        parts,partial=tiles;descriptions=[];failed=0
        for start in range(0,len(parts),3):
            try:result=await asyncio.wait_for(runtime.ai.describe_images(parts[start:start+3],model=model,text_only=True),25)
            except Exception:result=''
            if result:descriptions.append(f'同一长图从上到下第{start+1}–{min(start+3,len(parts))}段（段间有重叠）：'+str(result)[:1800])
            else:failed+=min(3,len(parts)-start)
            # 分批保留确认读到的结果，总预算耗尽时仍能回复已读部分。
            if descriptions:
                message['vision_description']='\n'.join(descriptions)[:12000]
                message['vision_status']='部分缺失：其余分段尚未读取，不能推断未读内容'
        message['vision_description']='\n'.join(descriptions)[:12000]
        message['vision_status']=('部分缺失：分段预算已用尽或有分段失败，未读区域不能推断' if partial or failed else 'ok') if descriptions else '失败：长图分段识别未返回内容'
        return True
