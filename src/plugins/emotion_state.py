"""Small, decaying role-play state updated after a successful send."""
import time
import math
from . import PluginBase, PluginMeta, FieldSchema


class EmotionState(PluginBase):
    META = PluginMeta(id='emotion_state',name='鲸鱼娘情绪与精力',category='social',
        description='维护角色的心情、精力和压力，随时间恢复；只在消息发送成功后更新。/状态 可查看当前角色状态。',
        config_schema={'enabled':FieldSchema(type='bool',label='启用'),
            'injectPrompt':FieldSchema(type='bool',label='把角色状态用于回复语气'),
            'autoUpdate':FieldSchema(type='bool',label='根据互动更新状态')},
        default_settings={'enabled':True,'injectPrompt':True,'autoUpdate':True})

    def state(self,now=None):
        now = time.time() if now is None else now
        raw = self.load_state('state.json',{})
        raw = raw if isinstance(raw,dict) else {}
        def number(key,default):
            try:
                value = float(raw.get(key,default))
                return value if math.isfinite(value) else default
            except (TypeError,ValueError,OverflowError):
                return default
        hours = max(0,min(48,(now-number('updated',now))/3600))
        result = {'updated':now}
        for key,baseline,speed in [('mood',60,2),('energy',80,5),('stress',10,4)]:
            old = max(0,min(100,number(key,baseline)))
            delta = max(-hours*speed,min(hours*speed,baseline-old))
            result[key] = round(old+delta,1)
        return result

    def label(self):
        state = self.state()
        mood = '开心' if state['mood']>=70 else '平静' if state['mood']>=40 else '有点低落'
        return f"角色状态：心情{mood}（{state['mood']:g}），精力{state['energy']:g}，压力{state['stress']:g}"

    def on_ingest(self,message,runtime):
        if str(message.get('text','')).strip() == '/状态':
            return self.label()

    def on_pre_ai(self,message,conversation,runtime):
        if self.get('injectPrompt',True):
            # 心情数值每轮都在变 → 交给 volatile_hint：装了「缓存稳定性」时排到提示词最后
            return self.volatile_hint(runtime,self.label()+'。这是扮演语气参考，正常回答问题，不必每次报数值。')

    def on_sent(self,message,reply,runtime):
        if not self.get('autoUpdate',True):
            return
        state = self.state()
        text = str(message.get('text',''))
        positive = any(word in text for word in ('谢谢','喜欢你','可爱','辛苦','摸摸','抱抱'))
        negative = any(word in text for word in ('讨厌你','闭嘴','滚开','笨蛋'))
        state['mood'] = max(0,min(100,state['mood']+2*positive-2*negative))
        state['energy'] = max(0,state['energy']-.5)
        state['stress'] = max(0,min(100,state['stress']+negative-positive))
        self.save_state('state.json',state)
