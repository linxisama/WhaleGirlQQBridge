"""Collect received sticker bytes into the bot's own bounded local library."""
from . import PluginBase,PluginMeta,FieldSchema
from .sticker_memory import looks_like_sticker

class StickerSteal(PluginBase):
    META=PluginMeta(id='sticker_steal',name='偷表情包与本地收藏',category='media',
        description='自动保存实际收到的表情图片，按内容去重，加入机器人自己的图库。支持官方QQ与普通QQ；不会写入或删除你的QQ收藏。默认只收表情/动图及小尺寸图片，不收普通大照片。官方QQ只能收平台实际推送的消息。',
        enabled_by_default=False,
        config_schema={'enabled':FieldSchema('bool','启用自动收集'),
            'stickerOnly':FieldSchema('bool','只收表情包和小图'),
            'maxLibrarySize':FieldSchema('int','最多收藏张数',min=10,max=2000),
            'maxLibraryMb':FieldSchema('int','图库容量上限（MB）',min=6,max=512),
            'blockedSenders':FieldSchema('string','不收这些发送者（逗号分隔）'),
            'allowPrivate':FieldSchema('bool','收集私聊发送的表情')},
        default_settings={'enabled':False,'stickerOnly':True,'maxLibrarySize':800,'maxLibraryMb':96,'blockedSenders':'','allowPrivate':True})

    async def observe(self,message,runtime):
        if message.get('notice') or (message.get('kind')=='private' and not self.get('allowPrivate',True)):
            return
        if str(message.get('sender_id','')) in str(self.get('blockedSenders','')).replace('，',',').split(','):
            return
        cap=runtime.plugins._plugins.get('sticker_cap') if hasattr(runtime,'plugins') else None
        if cap and cap.enabled and cap.blocked(message.get('sender_id')):return
        store=getattr(runtime,'stickers',None)
        if not store:
            return
        for item in message.get('media',[])[:3]:
            if not isinstance(item,dict) or item.get('type')!='image':
                continue
            try:
                image=await store.resolve(item,runtime)
                explicit=str(item.get('sub_type',''))=='1' or looks_like_sticker(tag=item.get('summary',''))
                small=image['width']<=400 and image['height']<=400 and len(image['data'])<=512*1024
                if self.get('stickerOnly',True) and not (explicit or image['animated'] or small):
                    continue
                entry,new=store.put(image['data'],max_items=int(self.get('maxLibrarySize',800)),max_mb=int(self.get('maxLibraryMb',96)))
                store.bind(item,entry['id'])
                if new:
                    runtime.note('已收藏一张表情图片，可在“表情包管理”中改标签和说明')
            except Exception:
                runtime.note('表情收集未完成：图片过期、损坏、超限或无法下载；正常聊天继续')

    def on_ingest(self,message,runtime):
        if str(message.get('text','')).strip()=='/表情库':
            entries=getattr(runtime,'stickers',None)
            return f'机器人本地收藏了 {len(entries.entries()) if entries else 0} 张表情。可在插件中心打开“表情包管理”。用 /表情 查看全部可发送标签。'
