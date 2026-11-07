"""T08：自动语音插件（把“聊天模型回复”按策略朗读并显式发送）。

默认 explicit、默认关闭（1.6.3 / T08 验收）。本插件**不改变现有文字回复链路**，
只在“生成回复之后”判断要不要朗读：

  run_on_post_reply：拿到文字回复 → audio_reply_policy.decide_voice →
  若该朗读则交给音频网关（audio_gateway → audio_jobs + audio_scheduler）入队 TTS，
  发送目标由“可信消息事件”绑定（绝不能来自模型输出，1.6.1）。

群聊未明确点名/命令不生成；关闭/切号/撤销后不发送；语音失败按配置回文字并说明。
"""
from __future__ import annotations

from . import PluginBase, PluginMeta, FieldSchema
from ..audio_contracts import ContractError
from ..audio_reply_policy import decide_voice, fit_segments


class VoiceReply(PluginBase):
    META = PluginMeta(
        id='voice_reply', name='自动语音', category='voice',
        enabled_by_default=False,
        description='按 explicit/text/voice/both 策略把回复朗读成语音并发送。'
                    '默认 explicit 且关闭；群聊未点名不自动朗读；语音失败回文字并说明。',
        config_schema={
            'enabled': FieldSchema(type='bool', label='启用'),
            'mode': FieldSchema(type='select', label='朗读策略',
                                values=['explicit', 'text', 'voice', 'both']),
            'groupRequiresName': FieldSchema(type='bool', label='群聊须明确点名'),
            'fallbackToText': FieldSchema(type='bool', label='语音失败回文字'),
            'maxChars': FieldSchema(type='int', label='单段朗读上限', min=1, max=300),
        },
        default_settings={'enabled': False, 'mode': 'explicit', 'groupRequiresName': True,
                         'fallbackToText': True, 'maxChars': 120})

    def on_pre_ai(self, message, conversation, runtime):
        """标记本轮“是否明确请求了语音”，供 post_reply 判定（不放大权限）。"""
        if not self.enabled:
            return None
        text = str(message.get('text') or '').strip()
        wants = any(k in text for k in ('说出来', '读给我听', '念一下', '用语音', '发语音'))
        message['_voice_requested'] = wants
        return None

    async def on_post_reply(self, message, reply, runtime):
        """拿到文字回复后，按策略决定朗读；入队 TTS（发送由网关按原消息目标执行）。"""
        if not self.enabled:
            return reply
        if message.get('_voice_enqueued'):
            return reply
        kind = str(message.get('kind') or '')
        target = str(message.get('target') or '')
        is_group = kind == 'group'
        requested = bool(message.get('_voice_requested'))
        if is_group and not bool(self.get('groupRequiresName', True)):
            # 用户显式关掉“群聊须点名”时，才允许群聊自动朗读。
            requested = True
        decision = decide_voice(
            reply, mode=str(self.get('mode', 'explicit')), is_group=is_group,
            explicitly_requested=requested)
        if not decision.should_voice:
            return reply  # 不朗读，保留原文字回复
        if not kind or not target or not str(message.get('sender_id') or ''):
            return reply  # 目标不可信/不完整：宁可只回文字
        try:
            limit = max(1, min(int(self.get('maxChars', 120) or 120), 300))
        except (TypeError, ValueError):
            limit = 120
        text, truncated = fit_segments(decision.segments, limit)
        if not text:
            return reply
        gateway = getattr(runtime, 'audio_gateway', None)
        if gateway is None or not hasattr(gateway, 'enqueue_voice_reply'):
            return reply
        try:
            account = ''
            account_getter = getattr(runtime, 'account', None)
            if callable(account_getter):
                try:
                    account = str(account_getter() or '')
                except Exception:
                    account = ''
            await gateway.enqueue_voice_reply(
                text=text, message=message,
                subject_domain=f'{kind}:{target}',
                principal=str(message.get('sender_id') or ''),
                account_id=account)
            message['_voice_enqueued'] = True
        except ContractError as exc:
            if bool(self.get('fallbackToText', True)):
                return reply + f'\n（语音没有生成：{exc.code.value}；以上文字仍然有效。）'
        except Exception:
            if bool(self.get('fallbackToText', True)):
                return reply + '\n（语音没有生成；以上文字仍然有效。）'
            return reply
        if truncated:
            # 朗读被字数上限截短时必须说出来：用户听到的和看到的对不上，
            # 又没被告知，就会以为机器人少说了一半意思。
            return reply + '\n（语音按单段上限朗读了前 %d 字，完整内容见上。）' % len(text)
        return reply
