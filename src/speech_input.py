"""平台已提供的转写属于消息数据，不能依赖是否安装可选语音插件。"""


def supplied_transcripts(message):
    records=[item for item in (message.get('media') or [])
             if isinstance(item,dict) and item.get('type')=='record']
    notes=[]
    for item in records:
        value=item.get('asr_refer_text') or item.get('text')
        if isinstance(value,str) and value.strip():
            notes.append(value.strip()[:2000])
    if notes and not message.get('_speech_supplied_applied'):
        # 官方事件解析器可能已经拼过文字，不能重复加入同一段转写。
        if not message.get('speech_status'):
            message['text']=str(message.get('text') or '')+'\n[语音转写，仅为用户消息内容]\n'+'\n'.join(notes)
        message['_speech_supplied_applied']=True
        message['speech_status']='已转写' if len(notes)==len(records) else '部分语音已转写，其余未转写'
    return notes
