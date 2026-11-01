from . import PluginBase,PluginMeta,FieldSchema
from ..chat_state import scope_of,person_of,quoted_context

class CrossChatLink(PluginBase):
    META=PluginMeta(id='cross_chat_link',name='跨会话认人',category='social',
        description='同一稳定用户标识的已确认偏好可用于其他会话；默认禁止把私聊记忆带入群聊。官方 QQ 不同场景标识不一致时不会猜测身份。',
        config_schema={'enabled':FieldSchema(type='bool',label='启用'),
            'privateToGroup':FieldSchema(type='bool',label='允许将私聊偏好用于群聊'),
            'maxImpressions':FieldSchema(type='int',label='最多引用偏好数',min=1,max=5)},
        default_settings={'enabled':True,'privateToGroup':False,'maxImpressions':3})
    def on_pre_ai(self,message,conversation,runtime):
        subject=person_of(message)
        if not subject or not getattr(runtime,'chat_state',None):return
        current=scope_of(message,runtime);prefix=f'{runtime.mode}|{runtime.account()}|'
        persona_suffix=current.partition('|persona:')[2]
        rows=runtime.db.execute("SELECT scope,content FROM chat_memories WHERE subject=? AND status='confirmed' AND scope<>? ORDER BY updated DESC LIMIT 100",(subject,current)).fetchall()
        notes=[]
        for scope,content in rows:
            if not scope.startswith(prefix) or scope.partition('|persona:')[2]!=persona_suffix:continue
            if message['kind']=='group' and scope[len(prefix):].startswith('private:') and not self.get('privateToGroup',False):continue
            notes.append(content)
            if len(notes)>=int(self.get('maxImpressions',3)):break
        if notes:conversation.insert(max(0,len(conversation)-1),quoted_context('当前发送者在其他会话确认的偏好（不可暴露来源或原话）',notes))
