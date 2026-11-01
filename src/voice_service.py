"""Opt-in voice output. No automatic replies, model installation or QQ retries."""
import asyncio
import hashlib
import io
import json
import re
import time
import uuid
import wave
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit

import httpx

MAX_AUDIO_BYTES = 8 * 1024 * 1024


class VoiceDeliveryUncertain(RuntimeError):
    """Sending was attempted, but no conclusive acknowledgement was received."""


def validate_wav(audio):
    if not isinstance(audio, bytes) or not 44 <= len(audio) <= MAX_AUDIO_BYTES:
        raise ValueError('语音须为非空 WAV，最大 8MB')
    try:
        with wave.open(io.BytesIO(audio), 'rb') as stream:
            duration = stream.getnframes() / stream.getframerate()
            expected = stream.getnframes() * stream.getnchannels() * stream.getsampwidth()
            if not 0 < duration <= 120 or stream.getnchannels() not in (1, 2):
                raise ValueError('语音时长须为 0–120 秒，支持单声道或双声道')
            if len(stream.readframes(stream.getnframes())) != expected:
                raise ValueError('WAV 内容不完整')
    except (wave.Error, EOFError, ZeroDivisionError) as exc:
        raise ValueError('音频不是支持的 PCM WAV 格式') from exc
    return duration


class VoiceService:
    def __init__(self, root, config, db, http_transport=None):
        self.directory = Path(root) / 'data/voice/output'
        self.config = config
        self.db = db
        self.http_transport = http_transport
        self._synthesis_lock = asyncio.Lock()
        self._cache = OrderedDict()
        db.execute('CREATE TABLE IF NOT EXISTS voice_sends('
                   'request_id TEXT PRIMARY KEY,payload TEXT,state TEXT,result TEXT)')
        db.execute("UPDATE voice_sends SET state='uncertain' WHERE state='sending'")
        db.commit()

    def enabled(self):
        if self.config.get('qq_only.voice.enabled', False) is not True:
            raise ValueError('语音输出接口尚未启用')

    def store(self, audio):
        self.enabled()
        duration = validate_wav(audio)
        audio_id = hashlib.sha256(audio).hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path(audio_id)
        intact = (path.is_file() and path.stat().st_size == len(audio)
                  and hashlib.sha256(path.read_bytes()).hexdigest() == audio_id)
        if not intact:
            temporary = self.directory / (uuid.uuid4().hex + '.tmp')
            try:
                temporary.write_bytes(audio)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return {'audio_id': audio_id, 'media_type': 'audio/wav', 'bytes': len(audio),
                'duration_seconds': round(duration, 3)}

    def path(self, audio_id):
        if not re.fullmatch('[a-f0-9]{64}', str(audio_id)):
            raise ValueError('语音编号无效')
        path = (self.directory / (audio_id + '.wav')).resolve()
        if not path.is_relative_to(self.directory.resolve()):
            raise ValueError('语音文件不在本程序的数据目录内')
        return path

    def provider(self):
        provider = str(self.config.get('qq_only.voice.provider', 'gpt_sovits_api_v2'))
        if provider not in {'gpt_sovits_api_v2', 'audio_cpp'}:
            raise ValueError('语音引擎须为 audio_cpp 或 gpt_sovits_api_v2')
        return provider

    def _request_parameters(self, text):
        provider = self.provider()
        audio_cpp = provider == 'audio_cpp'
        prefix = 'qq_only.voice.' + ('audio_cpp' if audio_cpp else 'gpt_sovits')
        name = 'audio.cpp' if audio_cpp else 'GPT-SoVITS'
        path = '/v1/audio/speech' if audio_cpp else '/tts'
        default_endpoint = 'http://127.0.0.1:18100/v1/audio/speech' if audio_cpp else 'http://127.0.0.1:9880/tts'
        endpoint = str(self.config.get(prefix + '.endpoint', default_endpoint))
        try:
            url = urlsplit(endpoint)
            port = url.port
        except ValueError:
            raise ValueError(name + ' 地址无效') from None
        if (url.scheme not in {'http', 'https'} or url.hostname not in {'127.0.0.1', 'localhost', '::1'}
                or url.username or url.password or url.query or url.fragment or port == 0
                or url.path.rstrip('/') != path):
            raise ValueError('预留接口仅连接本机 ' + name + ' 的 ' + path + ' 地址')
        if audio_cpp:
            model = str(self.config.get(prefix + '.model', 'breeze-tts-2')).strip()
            voice = str(self.config.get(prefix + '.voice', 'demo_3_woman')).strip()
            if not model or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', voice):
                raise ValueError('请配置有效模型名称和音色库名称（不接受文件路径）')
            payload = {'model': model, 'input': text, 'voice': voice, 'response_format': 'wav'}
        else:
            reference = str(self.config.get(prefix + '.ref_audio_path', '')).strip()
            if not reference:
                raise ValueError('请先配置 GPT-SoVITS 的参考音频路径')
            payload = {'text': text,
                       'text_lang': str(self.config.get(prefix + '.text_lang', 'zh')),
                       'ref_audio_path': reference,
                       'prompt_lang': str(self.config.get(prefix + '.prompt_lang', 'zh')),
                       'prompt_text': str(self.config.get(prefix + '.prompt_text', '')),
                       'media_type': 'wav', 'streaming_mode': False}
        return provider, name, endpoint, payload

    async def synthesize(self, text):
        self.enabled()
        text = str(text).strip()
        if not 1 <= len(text) <= 300:
            raise ValueError('待合成文字须为 1–300 个字符')
        provider, name, endpoint, payload = self._request_parameters(text)
        # Cache only the new adapter. Existing GPT-SoVITS sampling stays unchanged.
        cache_enabled = provider == 'audio_cpp' and self.config.get('qq_only.voice.audio_cpp.cache', True) is True
        cache_key = json.dumps([endpoint, payload], ensure_ascii=False, sort_keys=True)
        async def request():
            async with httpx.AsyncClient(timeout=60, trust_env=False, follow_redirects=False,
                                         transport=self.http_transport) as client:
                async with client.stream('POST', endpoint, json=payload) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_AUDIO_BYTES:
                            raise ValueError('合成音频超过 8MB 上限')
                        chunks.append(chunk)
                    return b''.join(chunks)
        async with self._synthesis_lock:
            self.enabled()
            # Settings may have changed while waiting behind an earlier generation.
            if (provider, name, endpoint, payload) != self._request_parameters(text):
                raise ValueError('语音设置已变化，请重新合成')
            cache_enabled = provider == 'audio_cpp' and self.config.get('qq_only.voice.audio_cpp.cache', True) is True
            now = time.monotonic()
            for key, (expires, _) in list(self._cache.items()):
                if expires <= now:
                    del self._cache[key]
            if cache_enabled and cache_key in self._cache:
                _, metadata = self._cache[cache_key]
                path = self.path(metadata['audio_id'])
                try:
                    if path.stat().st_size <= MAX_AUDIO_BYTES:
                        content = path.read_bytes()
                        if hashlib.sha256(content).hexdigest() == metadata['audio_id']:
                            validate_wav(content)
                            self._cache.move_to_end(cache_key)
                            return {**metadata, 'cached': True}
                except (OSError, ValueError):
                    pass
                del self._cache[cache_key]
            try:
                audio = await asyncio.wait_for(request(), 65)
            except (httpx.HTTPError, asyncio.TimeoutError):
                raise RuntimeError(name + ' 合成失败或超时，请检查本机语音服务') from None
            self.enabled()  # Disabling during synthesis also prevents storing the result.
            result = self.store(audio)
            if cache_enabled:
                self._cache[cache_key] = (time.monotonic() + 300, result)
                while len(self._cache) > 64:
                    self._cache.popitem(last=False)
                return {**result, 'cached': False}
            return result

    async def send(self, bot, *, audio_id, kind, target, account_id, request_id):
        self.enabled()
        if not re.fullmatch('[A-Za-z0-9_-]{1,64}', request_id):
            raise ValueError('request_id 须为 1–64 位字母、数字、下划线或短横线')
        if kind not in {'group', 'private'} or not str(target).isdigit():
            raise ValueError('请选择有效的群聊或私聊目标')
        payload = json.dumps([str(account_id), kind, str(target), audio_id], ensure_ascii=False)
        # Same runtime lock as settings/start/stop: never switch account during sending.
        async with bot.lock:
            self.enabled()
            existing = self.db.execute('SELECT payload,state,result FROM voice_sends WHERE request_id=?',
                                       (request_id,)).fetchone()
            if existing:
                if existing[0] != payload:
                    raise ValueError('request_id 已用于其他语音或目标，请使用新的编号')
                return {'success': existing[1] == 'sent', 'state': existing[1], 'duplicate': True,
                        'result': json.loads(existing[2] or '{}')}
            if (bot.mode != 'qqbridge' or not bot.bridge.online
                    or str(bot.account()) != str(account_id) or not str(account_id).isdigit()
                    or not bot.can_send_proactive(kind, str(target))):
                raise ValueError('账号未就绪、账号已变化，或当前回复规则不允许发送到该会话')
            path = self.path(audio_id)
            if not path.is_file() or path.stat().st_size > MAX_AUDIO_BYTES:
                raise ValueError('语音文件不存在或超过上限，请重新合成或导入')
            audio = path.read_bytes()
            validate_wav(audio)
            if hashlib.sha256(audio).hexdigest() != audio_id:
                raise ValueError('语音文件已变化，请重新合成或导入')
            self.db.execute('INSERT INTO voice_sends VALUES(?,?,?,?)', (request_id, payload, 'sending', '{}'))
            self.db.commit()
            try:
                result = await bot.bridge.send_voice(kind, str(target), audio)
            except BaseException as exc:
                self.db.execute("UPDATE voice_sends SET state='uncertain' WHERE request_id=?", (request_id,))
                self.db.commit()
                if isinstance(exc, Exception):
                    raise VoiceDeliveryUncertain('语音发送结果待核实') from None
                raise
            result = {'message_id': str(result.get('message_id') or result.get('id') or '')} if isinstance(result, dict) else {}
            self.db.execute("UPDATE voice_sends SET state='sent',result=? WHERE request_id=?",
                            (json.dumps(result), request_id))
            self.db.commit()
            return {'success': True, 'state': 'sent', 'duplicate': False, 'result': result}
