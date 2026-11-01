"""Attach an actual registered image every N successful replies, per room."""
from . import PluginBase,PluginMeta,FieldSchema
from ..chat_state import scope_of

class StickerCadence(PluginBase):
    META=PluginMeta(id='sticker_cadence',name='每三次表情',category='personality',
        description='每个会话中，机器人每成功回复N次附带一张真实图库图片。发送失败不计数，已有图片不再追加，避免官方QQ一条回复多图。没有可用图片时只回文字，不伪装成颜文字。',
        enabled_by_default=False,
        config_schema={'enabled':FieldSchema('bool','启用定期发表情图片'),
                       'sendEveryN':FieldSchema('int','每成功回复N次附图',min=1,max=100),
                       'skipIfMismatch':FieldSchema('bool','对不上语境就不发')},
        default_settings={'enabled':False,'sendEveryN':3,'skipIfMismatch':False})

    def __init__(self,manager):
        super().__init__(manager)
        value=self.load_state('counts.json',{})
        self._counts={k:v for k,v in value.items() if isinstance(v,int) and v>=0} if isinstance(value,dict) else {}

    def on_pre_ai(self,message,conversation,runtime):
        count=self._counts.get(scope_of(message,runtime),0)
        if (count+1)%int(self.get('sendEveryN',3))==0:
            return {'extra_system':'本轮达到定期配图次数，有可用图库图片时发送链会附一张真实图片；不要用颜文字冒充图片。'}

    @staticmethod
    def best_label(query, labels, notes=None):
        """从标签里挑与当前聊天最贴切的一个；全都对不上返回 None。

        以前是按下标轮转，发出去的表情和语境完全无关（用户反馈"没贴合语境就发了"）。
        这里用字符二元组重合度做轻量匹配（不花 token，也不调用模型）。
        """
        text = "".join(str(query or "").split())
        if not text:
            return None
        grams = {text[i:i + 2] for i in range(max(0, len(text) - 1))}
        if not grams:
            return None
        notes = notes or {}
        best, best_score = None, 0
        for label in labels:
            haystack = str(label) + str(notes.get(label, ""))
            score = sum(1 for gram in grams if gram in haystack)
            if score > best_score:
                best, best_score = label, score
        return best if best_score >= 1 else None

    async def prepare_delivery(self,message,reply,runtime):
        room=scope_of(message,runtime);count=self._counts.get(room,0)
        if (count+1)%int(self.get('sendEveryN',3)) or message.get('notice'):return reply
        meta=dict(getattr(runtime,'reply_meta',None) or {})
        if any(m.get('type')=='image' for m in meta.get('media',[]) if isinstance(m,dict)):return reply
        library=self._manager._plugins.get('image_library')
        if not library or not library.enabled:return reply
        labels=list(library.entries(runtime))
        if not labels:return reply
        # 先按当前语境挑标签：对不上就不发，宁可不配图也不发一张不搭的表情。
        notes={}
        store=getattr(runtime,'stickers',None)
        if store:
            try:
                notes={item['label']:str(item.get('description') or '') for item in store.entries()}
            except Exception:
                notes={}
        query=str(message.get('text') or '') + ' ' + str(reply or '')
        best=self.best_label(query,labels,notes)
        if best is None:
            if self.get('skipIfMismatch',False):
                return reply          # 对不上就不发，宁缺毋滥
            start=(count//int(self.get('sendEveryN',3)))%len(labels)
            ordered=labels[start:]+labels[:start]
        else:
            ordered=[best]+[label for label in labels if label!=best]
        for label in ordered:
            try:media,ids=library.selection(label,runtime)
            except (ValueError,OSError):continue
            meta['media']=list(meta.get('media',[]))+[media]
            meta['library_ids']=ids;runtime.reply_meta=meta
            break
        return reply

    def on_sent(self,message,reply,runtime):
        if message.get('notice'):return
        room=scope_of(message,runtime)
        self._counts[room]=self._counts.get(room,0)+1
        self._counts=dict(list(self._counts.items())[-2000:])
        # 审计建议"每条回复都落盘太费 I/O"，但计数是用户可见的发图节奏状态：
        # 攒批落盘会让"重启后计数仍在"失效（既有测试正是这么钉的），所以保持立即落盘。
        # 真要省 I/O 应当由 manager 在关闭/定时时统一 flush，那是另一件事。
        self.save_state('counts.json',self._counts)
