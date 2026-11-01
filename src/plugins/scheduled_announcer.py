"""Persisted, deduplicated scheduled text jobs through the core outbox."""
import datetime as dt
import re
from . import PluginBase, PluginMeta, FieldSchema


def schedule_values(settings):
    days = [v.strip() for v in re.split(r'[,，\s]+',settings.get('weekdays','')) if v.strip()]
    times = [v.strip() for v in re.split(r'[,，\s]+',settings.get('times','')) if v.strip()]
    groups = [v.strip() for v in re.split(r'[,，\s]+',settings.get('groupIds','')) if v.strip()]
    if any(v not in {'1','2','3','4','5','6','7'} for v in days):
        raise ValueError('星期请填写1–7，1为周一；用逗号分隔')
    if any(not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',v) for v in times):
        raise ValueError('时刻请填写 HH:MM，例如08:30，用逗号分隔')
    if any(not v.isdigit() or len(v)>20 for v in groups):
        raise ValueError('目标群号请填写数字，用逗号分隔')
    if len(times)>12 or len(groups)>10:
        raise ValueError('最多12个时刻、10个目标群')
    return set(map(int,days)),list(dict.fromkeys(times)),list(dict.fromkeys(groups))


class ScheduledAnnouncer(PluginBase):
    META = PluginMeta(id='scheduled_announcer',name='定时消息与文案推送',category='social',enabled_by_default=False,
        description='在指定星期和时刻给指定群发送固定文字，或用现有模型生成文案。普通QQ接入可用；官方QQ的主动消息权限不在本功能支持范围内。每次任务独立去重，超时发送不会自动重发。',
        config_schema={'enabled':FieldSchema(type='bool',label='启用'),
            'weekdays':FieldSchema(type='string',label='星期（1=周一，7=周日，逗号分隔）'),
            'times':FieldSchema(type='string',label='时刻（HH:MM，逗号分隔）'),
            'groupIds':FieldSchema(type='string',label='目标群号（逗号分隔）'),
            'content':FieldSchema(type='string',label='发送内容或文案主题',multiline=True),
            'useAI':FieldSchema(type='bool',label='按主题调用现有模型生成文案'),
            'maxChars':FieldSchema(type='int',label='文案字数上限',min=20,max=2000)},
        default_settings={'enabled':False,'weekdays':'1,2,3,4,5,6,7','times':'08:30',
                          'groupIds':'','content':'早上好，今天也要照顾好自己。','useAI':False,'maxChars':220})

    def validate_settings(self,settings):
        schedule_values(settings)

    async def tick(self,runtime):
        if runtime.mode != 'onebot' or not runtime.running:
            return
        days,times,groups = schedule_values(self.settings)
        now = dt.datetime.now()
        if now.isoweekday() not in days:
            return
        for clock in times:
            hour,minute = map(int,clock.split(':'))
            due = now.replace(hour=hour,minute=minute,second=0,microsecond=0)
            if not 0 <= (now-due).total_seconds() < 180:
                continue
            pending = []
            for group in groups:
                key = f"schedule:{now.date()}:{clock}:{group}"
                if not runtime.outgoing_exists(key) and runtime.can_send_proactive('group',group):
                    pending.append((key,group))
            if not pending:
                continue
            text = str(self.get('content','')).strip()
            if self.get('useAI',False):
                async with runtime.ai_lock:
                    text = await runtime.ai.chat([{'role':'user','content':text}],
                        system_prompt=f"写一段简短中文群聊文案，最多{self.get('maxChars',220)}字，只输出正文。",max_tokens=600)
            if not text:
                continue
            for key,group in pending:
                runtime.queue_outgoing(key,'group',group,text[:int(self.get('maxChars',220))])
