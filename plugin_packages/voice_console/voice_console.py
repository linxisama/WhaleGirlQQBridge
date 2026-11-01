"""Voice Console 1.0.2 for QQBridge 5.1.2+; local engines and host-managed delivery."""
from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path

from src.plugins import PluginBase, PluginMeta, FieldSchema


_LOCAL = ('audio_cpp', 'gpt_sovits_api_v2', 'qwen_webui')
_QUOTES = ('quote', 'quoted', 'reply', 'reference', 'message_reference', 'forward', 'forwarded')
_HELP = ('语音控制台：语音 状态 / 语音 检测 / 语音 朗读 <文字> / 语音 任务 <编号> / 语音 取消 <编号>。'
         '主人原始私聊可用：语音 应用配置 / 语音 电脑状态 / 语音 急停。'
         '完整设置、试听和任务列表在本机“音频与语音”页。唱歌和设备配对尚未接线。')


def parse_command(text):
    raw = str(text or '').strip()
    if raw == '语音':
        return '帮助', ''
    match = re.fullmatch(r'语音\s+(\S+)(?:\s+([\s\S]*))?', raw)
    return (match[1], (match[2] or '').strip()) if match else None


class VoiceConsole(PluginBase):
    META = PluginMeta(
        id='voice_console', name='语音控制台', category='voice',
        enabled_by_default=False,
        description='本地TTS配置、检测、朗读发送及Agent状态/急停入口；'
                    '需要QQBridge5.1.2及其音频网关，默认关闭，不自动出网或开启电脑权限。',
        config_schema={
            'enabled': FieldSchema(type='bool', label='启用插件', group='使用范围'),
            'ownerIds': FieldSchema(type='string', label='允许使用的主人QQ号', group='使用范围',
                                    description='多个QQ号用逗号分隔；空值不允许任何人操作。'),
            'allowGroups': FieldSchema(type='bool', label='允许主人在群里朗读', group='使用范围'),
            'speakReplies': FieldSchema(type='bool', label='主人聊天回复同时转语音', group='使用范围',
                                        description='默认关闭；只作用于允许的原始会话，文字仍正常发送。'),
            'maxChars': FieldSchema(type='int', label='单次朗读上限', min=1, max=300, group='使用范围'),
            'voiceProfileId': FieldSchema(type='string', label='音色ID', group='使用范围',
                                         description='persona-default跟随控制台默认音色；也可填已导入音色ID。'),
            'engine': FieldSchema(type='select', label='本地主引擎', values=list(_LOCAL), group='引擎设置'),
            'audioEndpoint': FieldSchema(type='string', label='audio.cpp接口', group='引擎设置'),
            'audioModel': FieldSchema(type='string', label='audio.cpp模型', group='引擎设置'),
            'audioVoice': FieldSchema(type='string', label='audio.cpp已登记音色', group='引擎设置'),
            'sovitsEndpoint': FieldSchema(type='string', label='GPT-SoVITS接口', group='引擎设置'),
            'qwenEndpoint': FieldSchema(type='string', label='Qwen服务根地址', group='引擎设置'),
            'qwenMode': FieldSchema(type='select', label='Qwen模式', group='引擎设置',
                                    values=['custom_voice', 'voice_clone', 'voice_design']),
            'qwenModel': FieldSchema(type='string', label='Qwen本机模型名称', group='引擎设置'),
            'qwenSpeaker': FieldSchema(type='string', label='Qwen说话人', group='引擎设置'),
            'qwenInstruct': FieldSchema(type='string', label='Qwen音色/语气描述', multiline=True,
                                        group='引擎设置',
                                        description='描述式音色使用此项；固定说话人模式可填写语气要求。'),
            'refAudio': FieldSchema(type='string', label='上传参考音频', group='参考素材',
                                    file_picker='file', file_extensions=['.wav', '.flac', '.mp3'],
                                    file_max_mb=8,
                                    description='复制到data/plugin_uploads；只留本机，不上传公网。'),
            'refText': FieldSchema(type='string', label='参考音频实际台词', multiline=True, group='参考素材'),
            'language': FieldSchema(type='select', label='GPT-SoVITS参考/合成语言',
                                    values=['zh', 'en', 'ja', 'ko', 'yue'], group='参考素材'),
        },
        default_settings={
            'enabled': False, 'ownerIds': '', 'allowGroups': False, 'speakReplies': False,
            'maxChars': 300, 'voiceProfileId': 'persona-default', 'engine': 'audio_cpp',
            'audioEndpoint': 'http://127.0.0.1:18100/v1/audio/speech',
            'audioModel': 'breeze-tts-2', 'audioVoice': 'demo_3_woman',
            'sovitsEndpoint': 'http://127.0.0.1:9880/tts',
            'qwenEndpoint': 'http://127.0.0.1:7860', 'qwenMode': 'custom_voice',
            'qwenModel': '', 'qwenSpeaker': '', 'qwenInstruct': '',
            'refAudio': '', 'refText': '', 'language': 'zh'})

    def __init__(self, manager):
        super().__init__(manager)
        self._tasks = {}

    def _owners(self):
        return {part for part in re.split(r'[,，\s]+', str(self.get('ownerIds', '')))
                if part.isdigit()}

    def _authorized(self, message, *, private=False):
        if not self.enabled or any(message.get(key) for key in _QUOTES):
            return False
        who, target = str(message.get('sender_id') or ''), str(message.get('target') or '')
        if who not in self._owners() or not target.isdigit():
            return False
        kind = message.get('kind')
        return ((kind == 'private' and target == who) or (not private and kind == 'group'
                                      and self.get('allowGroups', False)))

    @staticmethod
    def _gateway(runtime):
        gateway = getattr(runtime, 'audio_gateway', None)
        if gateway is None:
            raise ValueError('需要QQBridge5.1.2及已初始化的音频网关；请先升级宿主。')
        return gateway

    def _parameters(self, runtime):
        from src.audio_console import EngineSettingsRequest
        name = str(self.get('engine', 'audio_cpp'))
        reference = str(self.get('refAudio', '')).strip()
        if reference and name != 'audio_cpp':
            path = Path(reference).resolve()
            uploads = (Path(runtime.root) / 'data' / 'plugin_uploads').resolve()
            if (not path.is_relative_to(uploads) or not path.is_file()
                    or path.suffix.lower() not in {'.wav', '.flac', '.mp3'}
                    or not 44 <= path.stat().st_size <= 8 * 1024 * 1024):
                raise ValueError('参考音频须通过插件配置的上传按钮导入，最大8MB。')
            reference = str(path)
        if name == 'audio_cpp':
            params = {'endpoint': self.get('audioEndpoint'), 'model': self.get('audioModel'),
                      'voice': self.get('audioVoice')}
        elif name == 'gpt_sovits_api_v2':
            params = {'endpoint': self.get('sovitsEndpoint'), 'ref_audio_path': reference,
                      'prompt_text': self.get('refText', ''),
                      'prompt_lang': self.get('language', 'zh'), 'text_lang': self.get('language', 'zh')}
        elif name == 'qwen_webui':
            params = {'endpoint': self.get('qwenEndpoint'), 'mode': self.get('qwenMode'),
                      'model': self.get('qwenModel', ''), 'speaker': self.get('qwenSpeaker', ''),
                      'instruct': self.get('qwenInstruct', ''),
                      'ref_audio_path': reference, 'ref_text': self.get('refText', '')}
        else:
            raise ValueError('只支持已列出的三个本地引擎。')
        return EngineSettingsRequest(provider=name, params=params, route_policy='local_only')

    @staticmethod
    def _principal(message, account):
        return ':'.join(('market.voice_console', account, str(message.get('kind')),
                         str(message.get('target')), str(message.get('sender_id'))))

    def _owned_job(self, gateway, message, runtime, job_id):
        if not re.fullmatch(r'[a-f0-9]{24}', job_id):
            raise ValueError('请填写完整的24位任务编号。')
        row = gateway.jobs.get(job_id)
        if (row is None or row.get('subject_domain') != 'console:local'
                or row.get('principal') != self._principal(message, str(runtime.account()))):
            raise ValueError('该任务不存在或不属于当前账号与会话。')
        return row

    @staticmethod
    def _delivery(gateway, row):
        from src.audio_contracts import ContractError, ErrorCode
        request_id = str(row.get('client_request_id') or '')
        if not re.fullmatch(r'vp-[a-f0-9]{24}', request_id):
            return None
        try:
            return gateway.delivery_status('vc-' + request_id[3:], subject_domain='console:local')
        except ContractError as exc:
            if exc.code is not ErrorCode.INVALID_INPUT:
                raise
            return None

    def _job_summary(self, gateway, row):
        labels = {'queued': '排队中', 'preparing': '准备中', 'running': '生成中',
                  'postprocessing': '音频处理中', 'ready': '已生成', 'failed': '失败',
                  'cancel_requested': '正在取消', 'cancelled': '已取消',
                  'expired': '已过期', 'unknown': '结果待核实'}
        delivery = self._delivery(gateway, row)
        state = str(row.get('state') or 'unknown')
        summary = '生成：' + labels.get(state, '结果待核实') + '；交付：'
        if delivery is None:
            summary += '尚无交付记录（需文字回执成功且生成完成）。'
        else:
            summary += {'sent': '平台已回执，收件播放需验证。',
                        'uncertain': '结果待核实，不自动重发。',
                        'partial': '部分已交付，不自动重发。',
                        'pending': '已创建交付记录，尚无成功回执。'}.get(
                            delivery.get('state'), '结果待核实，不自动重发。')
        code = str(row.get('error_code') or '')
        if code:
            summary += '错误码：' + code
        return summary

    def _can_send(self, runtime, message, account):
        plugins = getattr(runtime, 'plugins', None)
        if plugins is not None and getattr(plugins, '_plugins', {}).get(self.META.id) is not self:
            return False
        return (self._authorized(message) and getattr(runtime, 'running', False)
                and getattr(runtime, 'mode', '') == 'qqbridge'
                and str(runtime.account()) == account and account.isdigit()
                and bool(getattr(getattr(runtime, 'bridge', None), 'online', False))
                and runtime.can_send_proactive(message['kind'], str(message['target'])))

    async def _queue(self, runtime, message, text):
        gateway = self._gateway(runtime)
        account = str(runtime.account())
        if not self._can_send(runtime, message, account) or not gateway.sender_ready:
            raise ValueError('当前账号、会话权限或QQ语音发送通道尚未就绪。')
        if runtime.config.get('qq_only.voice.provider', '') not in _LOCAL:
            raise ValueError('请先在本机音频页或通过“语音 应用配置”选定本地引擎。')
        if not str(message.get('id') or '').strip():
            raise ValueError('消息缺少编号，无法保证幂等，未提交语音。')
        limit = int(self.get('maxChars', 300))
        if not 1 <= len(text.strip()) <= limit:
            raise ValueError(f'朗读文本须为1–{limit}字，不会自动截断。')
        principal = self._principal(message, account)
        digest = hashlib.sha256((principal + ':' + str(message['id'])).encode()).hexdigest()[:24]
        created = await gateway.create_tts(text=text.strip(), subject_domain='console:local',
            principal=principal, client_request_id='vp-' + digest, route_policy='local_only',
            delivery_intent='preview', voice_profile_id=str(self.get('voiceProfileId', 'persona-default')))
        message['_voice_console_submission'] = {
            'job_id': created['job_id'], 'account': account, 'principal': principal,
            'message': {key: message[key] for key in ('id', 'kind', 'target', 'sender_id')},
            'delivery_id': 'vc-' + digest}
        return created

    async def on_prepare(self, message, runtime):
        command = parse_command(message.get('text'))
        if not self.enabled or command is None:
            return
        message['_voice_console_command'] = True
        message['_voice_enqueued'] = True  # Do not let another voice plugin read command receipts.
        action, value = command
        if action in ('帮助', 'help'):
            message['_direct_reply'] = _HELP
            return
        if not self._authorized(message):
            message['_direct_reply'] = '仅接受插件配置中主人的原始私聊；群聊需单独允许，引用/转发不执行。'
            return
        try:
            gateway = self._gateway(runtime)
            if action == '状态':
                caps = gateway.capabilities()
                message['_direct_reply'] = (f"当前引擎：{caps.get('provider', '未配置')}；"
                    '连接、合成、QQ播放分别验证。任务与试听请在本机“音频与语音”页查看。')
            elif action == '检测':
                from src.audio_console import probe
                result = await probe(runtime.config, self._parameters(runtime))
                message['_direct_reply'] = str(result.get('message') or '检测没有结果')
            elif action == '应用配置':
                if not self._authorized(message, private=True):
                    raise ValueError('应用引擎配置只接受主人原始私聊。')
                if gateway.jobs.active_count():
                    raise ValueError('仍有音频任务，完成或取消后再应用配置。')
                from src.audio_console import save_settings
                save_settings(runtime.config, self._parameters(runtime))
                gateway._adapters.clear()
                gateway.cache.clear()
                message['_direct_reply'] = '已增量应用插件的本地语音引擎配置；其他设置未替换，自动回复未开启。'
            elif action == '朗读':
                created = await self._queue(runtime, message, value)
                message['_direct_reply'] = ('语音任务 ' + created['job_id'] +
                    ' 已提交；本条回执发送成功后才会尝试语音交付。')
            elif action == '任务':
                row = self._owned_job(gateway, message, runtime, value)
                message['_direct_reply'] = self._job_summary(gateway, row)
            elif action == '取消':
                self._owned_job(gateway, message, runtime, value)
                result = await gateway.cancel(value, subject_domain='console:local')
                if result.get('cancelled'):
                    message['_direct_reply'] = ('已请求取消排队/生成，不会交付取消后的任务。' +
                        self._job_summary(gateway, self._owned_job(gateway, message, runtime, value)))
                else:
                    message['_direct_reply'] = ('任务已结束，本指令不能撤回已生成音频或已交给QQ的发送请求。' +
                        self._job_summary(gateway, self._owned_job(gateway, message, runtime, value)))
            elif action in ('电脑状态', '急停'):
                if not self._authorized(message, private=True):
                    raise ValueError('电脑状态/急停只接受主人原始私聊。')
                agent = getattr(runtime, 'local_agent_console', None)
                if agent is None:
                    raise ValueError('宿主尚未初始化本地Agent；本插件不会自动配对。')
                if action == '急停':
                    agent.emergency_stop()
                    message['_direct_reply'] = '已请求急停；恢复与批准请在本机控制台操作。'
                else:
                    status = agent.status()
                    count = sum(bool(device.get('paired')) for device in status.get('devices', []))
                    message['_direct_reply'] = f"已配对设备：{count}；急停：{'是' if status.get('stopped') else '否'}。未配对不能控制电脑。"
            elif action in ('唱歌', '配对', '批准', '解除急停'):
                message['_direct_reply'] = '唱歌与配对尚未接线；批准和解除急停只在本机控制台操作。'
            else:
                message['_direct_reply'] = _HELP
        except ImportError:
            message['_direct_reply'] = '需要QQBridge5.1.2完整音频组件；单独安装插件不会升级旧宿主。'
        except Exception as exc:
            message['_direct_reply'] = '语音操作未完成：' + self._public_error(exc)

    async def on_post_reply(self, message, reply, runtime):
        if (message.get('_voice_console_command') or not self.get('speakReplies', False)
                or not self._authorized(message) or message.get('_voice_console_submission')):
            return reply
        existing = getattr(getattr(runtime, 'plugins', None), '_plugins', {}).get('voice_reply')
        if existing is not None and existing.enabled:
            return reply  # One speech producer per reply.
        try:
            await self._queue(runtime, message, str(reply or ''))
        except Exception as exc:
            self._note(runtime, '自动语音未提交：' + self._public_error(exc))
        return reply

    def on_sent(self, message, reply, runtime):
        submission = message.pop('_voice_console_submission', None)
        if not submission or submission['job_id'] in self._tasks:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._note(runtime, '未找到宿主任务循环，没有发送语音。')
            return
        task = loop.create_task(self._finish(runtime, submission))
        self._tasks[submission['job_id']] = task
        task.add_done_callback(lambda _task, key=submission['job_id']: self._tasks.pop(key, None))

    async def _finish(self, runtime, submission):
        try:
            from src.audio_contracts import DeliveryPlanRequest, ContractError, ErrorCode
            gateway = self._gateway(runtime)
            await gateway.scheduler.wait_for(submission['job_id'], timeout=100)
            if not self._can_send(runtime, submission['message'], submission['account']):
                self._note(runtime, '语音已保留本机；插件、账号或会话授权已变化，未发送。')
                return
            row = gateway.get_job(submission['job_id'], subject_domain='console:local')
            if not row or row.get('state') != 'ready' or not row.get('audio'):
                self._note(runtime, '语音任务未完成，请在音频页查看状态。')
                return
            # Any existing delivery may have been submitted before a timeout or
            # restart. Do not repeat it, even when a previous plan looks pending.
            try:
                previous = gateway.delivery_status(submission['delivery_id'], subject_domain='console:local')
            except ContractError as exc:
                if exc.code is not ErrorCode.INVALID_INPUT:
                    raise
            else:
                self._note(runtime, '语音交付已有记录：' + str(previous.get('state')) + '；没有重发。')
                return
            message = submission['message']
            gateway.submit_delivery(DeliveryPlanRequest(
                delivery_id=submission['delivery_id'], audio_asset_ids=[row['audio']['audio_id']],
                kind=message['kind'], target=str(message['target']), account_id=submission['account'],
                request_id=submission['delivery_id']), subject_domain='console:local')
            result = await gateway.send_delivery(submission['delivery_id'], subject_domain='console:local')
            self._note(runtime, '语音交付状态：' + str(result.get('state')) +
                       '（结果未知不自动重发；QQ收件播放仍需验证）。')
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self._note(runtime, '语音交付未完成：' + self._public_error(exc))

    @staticmethod
    def _public_error(exc):
        code = getattr(getattr(exc, 'code', None), 'value', '')
        if code:
            return str(code) + '，请在本机音频页检查配置和任务。'
        return str(exc)[:180] if isinstance(exc, ValueError) else '请在本机控制台检查服务和配置。'

    @staticmethod
    def _note(runtime, text):
        note = getattr(runtime, 'note', None)
        if callable(note):
            note('[语音控制台] ' + text)
