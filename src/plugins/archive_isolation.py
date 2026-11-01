"""Non-destructive persona namespaces; never moves or deletes a user's archives."""
from . import PluginBase,PluginMeta,FieldSchema

class ArchiveIsolation(PluginBase):
    META=PluginMeta(id='archive_isolation',name='人设存档隔离',category='utility',
        description='切换人设后分开对话历史与个人记忆，再切回来可继续原有存档；不会删除或搬走用户文件。',
        config_schema={'enabled':FieldSchema(type='bool',label='启用'),
            'protectKeys':FieldSchema(type='string',label='共用记忆的会话（group:ID 或 private:ID，逗号分隔）')},
        default_settings={'enabled':True,'protectKeys':''})
    def name(self,runtime,message=None):
        return str((message or {}).get('_effective_persona') or getattr(getattr(runtime,'style',None),'active_name',''))
    def scope(self,base,message,runtime):
        name=self.name(runtime,message)
        if not name:return base
        state=self.load_state('namespace.json',{})
        owner=state.get('legacyOwner')
        if not owner:
            owner=self.name(runtime);self.save_state('namespace.json',{'legacyOwner':owner})
        shared={v.strip() for v in self.get('protectKeys','').replace('，',',').split(',')}
        if name==owner or f"{message.get('kind')}:{message.get('target')}" in shared:return base
        from hashlib import sha256
        return base+'|persona:'+sha256(name.encode()).hexdigest()[:16]
