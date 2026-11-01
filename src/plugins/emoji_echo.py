"""Follow reactions on the original message over the existing OneBot link."""
import asyncio
import time
from . import PluginBase,PluginMeta,FieldSchema

class EmojiEcho(PluginBase):
    META=PluginMeta(id='emoji_echo',name='跟着贴表情',category='social',
        description='普通QQ/OneBot专用：群友给消息贴表情回应时，跟贴同一条消息的同一个表情，全程不调用模型、不发文字。当前官方QQ群聊接入不支持此动作，会保持不执行；图片表情不受影响。',
        enabled_by_default=False,
        config_schema={'enabled':FieldSchema('bool','启用跟贴'),
            'delayMs':FieldSchema('int','跟贴延迟（毫秒）',min=0,max=5000),
            'minIntervalMs':FieldSchema('int','最小间隔（毫秒）',min=0,max=60000),
            'maxPerMinute':FieldSchema('int','每分钟最多次数',min=1,max=60),
            'groupWhitelist':FieldSchema('string','只在这些群生效（逗号分隔）'),
            'emojiBlacklist':FieldSchema('string','不跟贴的表情编号（逗号分隔）'),
            'followRemove':FieldSchema('bool','跟随取消表情')},
        default_settings={'enabled':False,'delayMs':900,'minIntervalMs':1500,'maxPerMinute':20,
                          'groupWhitelist':'','emojiBlacklist':'','followRemove':False})

    def __init__(self,manager):
        super().__init__(manager);self._times=[]
        self._done=self.load_state('echo.json',{})
        if not isinstance(self._done,dict):self._done={}

    async def observe(self,message,runtime):
        if runtime.mode!='onebot' or message.get('notice')!='emoji_reaction':return
        data=message.get('notice_data') or {}
        if str(data.get('user',''))==str(runtime.onebot.self_id):return
        groups={s.strip() for s in str(self.get('groupWhitelist','')).replace('，',',').split(',') if s.strip()}
        if groups and str(message['target']) not in groups:return
        add=bool(data.get('set',True))
        if not add and not self.get('followRemove',False):return
        black={s.strip() for s in str(self.get('emojiBlacklist','')).replace('，',',').split(',')}
        for emoji in data.get('emojis',[])[:2]:
            if emoji in black:continue
            key=f'{runtime.account()}:{message["target"]}:{data["message_id"]}:{emoji}:{int(add)}'
            if key in self._done:continue
            now=time.time();self._times=[t for t in self._times if now-t<60]
            if len(self._times)>=self.get('maxPerMinute',20):return
            delay=max(self.get('delayMs',900)/1000,
                      (self._times[-1]+self.get('minIntervalMs',1500)/1000-now) if self._times else 0)
            self._times.append(now+delay) # reserve quota before awaiting
            self._done[key]=now
            self._done=dict(list(self._done.items())[-2000:]);self.save_state('echo.json',self._done)
            await asyncio.sleep(delay)
            if not runtime.running or not self.enabled:return
            try:
                await runtime.onebot.call('set_msg_emoji_like',{'message_id':int(data['message_id']),'emoji_id':str(emoji),'set':add})
                # A follow-remove must allow a later add on the same message.
                self._done.pop(key[:-1]+str(int(not add)),None)
                self.save_state('echo.json',self._done)
            except Exception:
                runtime.note('跟贴表情未成功：请检查普通QQ协议端的消息表情回应支持')
