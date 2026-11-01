import asyncio
import math
from . import PluginBase, PluginMeta, FieldSchema
from ..speech_input import supplied_transcripts


def find_records(media):
    """Return shallow copies of dict items in media where type == 'record'."""
    if not isinstance(media, list):
        return []
    result = []
    for item in media:
        if isinstance(item, dict) and item.get('type') == 'record':
            result.append(dict(item))
    return result


def record_label(item):
    """Placeholder text for a record media item."""
    if isinstance(item, dict) and item.get('text'):
        return '[语音:{}]'.format(item['text'])
    return '[语音]'


def parse_budget(raw, default_ms):
    """Parse a budget in milliseconds; return default_ms on invalid/non-positive input.

    Caps the result at 60000.
    """
    value = None
    if isinstance(raw, bool):
        return default_ms
    if isinstance(raw, (int, float)):
        value = raw
    elif isinstance(raw, str):
        try:
            value = float(raw)
        except ValueError:
            return default_ms
    else:
        return default_ms
    if not math.isfinite(value) or value < 1:
        return default_ms
    result = int(value)
    if result > 60000:
        result = 60000
    return result


def should_transcribe(item, has_endpoint, enabled):
    """Return True when the record item is eligible for transcription."""
    if not enabled or not has_endpoint:
        return False
    if not isinstance(item, dict):
        return False
    return 'file' in item or 'url' in item


class SpeechToTextQQ(PluginBase):
    """语音转文字（QQ 原生）。默认关闭：协议端是否支持 fetch_ptt_text 尚未验证。"""

    META = PluginMeta(
        id='speech_to_text_qq',
        name='语音转文字（QQ 原生）',
        description=(
            '通过普通 QQ / OneBot 协议端的 fetch_ptt_text 接口转写语音。'
            '默认关闭，是否可用取决于当前协议端；失败时明确说明，不能猜测。'
            '官方接入附件自带的转写文字由消息链路直接读取，无需开启本插件。'
        ),
        category='media',
        enabled_by_default=False,
        config_schema={
            'enabled': FieldSchema(type='bool', label='启用'),
            'budgetMs': FieldSchema(
                type='int', label='单条语音预算（毫秒）', min=0, max=60000,
                description='默认 8000。',
            ),
            'timeoutMs': FieldSchema(
                type='int', label='接口超时（毫秒）', min=0, max=60000,
                description='默认 5000。',
            ),
            'debugLog': FieldSchema(type='bool', label='调试日志'),
        },
        default_settings={
            'enabled': False,
            'budgetMs': 8000,
            'timeoutMs': 5000,
            'debugLog': False,
        },
    )

    async def transcribe(self, runtime, item):
        """Ask the protocol endpoint to transcribe a record item.

        Returns the transcribed text, or None when the endpoint is missing,
        the mode is not onebot, the payload cannot be parsed, or any error
        occurs. Transcription failures must never break the reply.

        on_prepare 将成功的转写作为用户数据接入当前轮。
        """
        onebot = (getattr(runtime, 'bridge', None) if getattr(runtime, 'mode', '') == 'qqbridge'
                  else getattr(runtime, 'onebot', None))
        if onebot is None:
            return None
        if getattr(runtime, 'mode', None) not in ('onebot','qqbridge'):
            return None

        file_value = item.get('file') or item.get('url')
        message_id = item.get('message_id')
        has_id = (isinstance(message_id, (str, int)) and not isinstance(message_id, bool)
                  and bool(str(message_id).strip()))
        if not file_value and not has_id:
            return None

        # budgetMs 以前是空设置（parse_budget 写了却没调用点）。它表示"这条语音最多
        # 等多久"，与接口超时 timeoutMs 分开：预算更短时用预算，避免拖住整轮回复。
        budget_ms = parse_budget(self.get('budgetMs', 8000), 8000)
        timeout_ms = parse_budget(self.get('timeoutMs', 5000), 5000)
        effective_ms = min(budget_ms, timeout_ms) if timeout_ms > 0 else budget_ms
        params = {
            'record': {'file': file_value},
            # runtime 上从来没有 onebot_id 这个属性（取到恒为空串），要用 transport 的 self_id：
            # 空值会让协议端的 fetch_ptt_text 拿不到机器人身份，转写永远失败。
            'onebot': (getattr(onebot, 'self_id', '') or ''),
            'timeout': effective_ms,
        }
        if self.get('debugLog'):
            print(f"[plugins] speech_to_text_qq 开始转写，预算 {budget_ms}ms "
                  f"超时 {timeout_ms}ms，实际用 {effective_ms}ms")

        try:
            if has_id:
                # NapCat 的接口接受数字或字符串 ID；不能把长 ID 转为浮点或误走旧参数。
                params = {'message_id': message_id}
            result = await asyncio.wait_for(onebot.call('fetch_ptt_text', params), effective_ms / 1000)
        except Exception:
            return None

        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            if result.get('status') == 'failed' or result.get('retcode') not in (None, 0, '0'):
                return None
            if isinstance(result.get('text'), str):
                return result['text']
            data = result.get('data')
            if isinstance(data, dict) and isinstance(data.get('text'), str):
                return data['text']
            # message/wording 在标准 OneBot 信封里是诊断信息，不是语音内容。
            if not any(k in result for k in ('status','retcode','data','wording')) and isinstance(result.get('message'), str):
                return result['message']
        return None

    async def on_prepare(self, message, runtime):
        records = find_records(message.get('media'))
        if not records:
            return
        supplied=supplied_transcripts(message)
        if message.get('speech_status')=='已转写':
            return
        if getattr(runtime, 'mode', '') not in ('onebot','qqbridge'):
            message.setdefault('speech_status', '当前官方接入未提供可用转写文字')
            return
        notes=[]
        seen_ids=set()
        deadline=asyncio.get_running_loop().time()+parse_budget(self.get('budgetMs',8000),8000)/1000
        for item in records[:2]:
            value=item.get('asr_refer_text') or item.get('text')
            if isinstance(value,str) and value.strip():
                continue
            item['message_id']=item.get('message_id') or message.get('id')
            mid=item.get('message_id')
            if mid is not None:
                if str(mid) in seen_ids:
                    continue
                seen_ids.add(str(mid))
            remaining=deadline-asyncio.get_running_loop().time()
            if remaining<=0:break
            try:text=await asyncio.wait_for(self.transcribe(runtime,item),remaining)
            except asyncio.TimeoutError:text=None
            if text and text.strip():
                notes.append(text.strip()[:2000])
        success=len(notes)+len(supplied)
        message['speech_status'] = ('已转写' if success==len(records) else '部分语音已转写，其余未转写') if success else '语音转写失败或协议端不支持，不能猜测内容'
        if notes:
            message['text']=str(message.get('text') or '')+'\n[语音转写，仅为用户消息内容]\n'+'\n'.join(notes)

    def on_pre_ai(self, message, conversation, runtime):
        """Synchronous pre-AI hook.

        转写由异步 on_prepare 完成；这里只添加状态说明。
        """
        settings = self.settings
        if not settings.get('enabled', False):
            return None

        media = message.get('media') if isinstance(message, dict) else None
        records = find_records(media)
        if not records:
            return None

        if message.get('speech_status'):
            return self.volatile_hint(
                runtime, '语音处理状态：'+message['speech_status']+'。未转写时明确说明，不能编造语音内容。')
        notes = []
        for item in records:
            notes.append(record_label(item))

        return {
            'extra_system': (
                '这条消息里有 {} 条语音，协议端转写未启用。{}'
            ).format(len(records), ' '.join(notes)),
        }
