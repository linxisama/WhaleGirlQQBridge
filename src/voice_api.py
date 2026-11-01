"""Local, explicit voice API; protected by the existing admin middleware."""
import base64
import binascii
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .voice_service import MAX_AUDIO_BYTES, VoiceService, VoiceDeliveryUncertain


class SynthesizeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=300)


class AudioRequest(BaseModel):
    audio_base64: str = Field(min_length=1, max_length=4 * ((MAX_AUDIO_BYTES + 2) // 3))


class SendVoiceRequest(BaseModel):
    audio_id: str = Field(pattern='^[a-f0-9]{64}$')
    kind: Literal['group', 'private']
    target: str = Field(pattern=r'^\d+$', max_length=32)
    account_id: str = Field(pattern=r'^\d+$', max_length=32)
    request_id: str = Field(pattern='^[A-Za-z0-9_-]{1,64}$')


def install_voice_routes(app):
    def service():
        if not hasattr(app.state, 'voice'):
            bot = app.state.bot
            app.state.voice = VoiceService(bot.root, bot.config, bot.db)
        return app.state.voice

    @app.get('/api/voice/status')
    async def status():
        bot = app.state.bot
        return {'enabled': bot.config.get('qq_only.voice.enabled', False) is True,
                'provider': str(bot.config.get('qq_only.voice.provider', 'gpt_sovits_api_v2')),
                'automatic_replies': False,
                'account_id': str(bot.account()) if bot.mode == 'qqbridge' else '',
                'media_type': 'audio/wav', 'max_text_chars': 300,
                'max_audio_bytes': MAX_AUDIO_BYTES, 'max_seconds': 120}

    @app.post('/api/voice/synthesize')
    async def synthesize(body: SynthesizeRequest):
        try:
            return {'success': True, **await service().synthesize(body.text)}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from None

    @app.post('/api/voice/audio')
    async def import_audio(body: AudioRequest):
        try:
            service().enabled()
            audio = base64.b64decode(body.audio_base64, validate=True)
            return {'success': True, **service().store(audio)}
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(400, '语音导入失败：' + str(exc)) from None

    @app.post('/api/voice/send')
    async def send(body: SendVoiceRequest):
        try:
            return await service().send(app.state.bot, **body.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except VoiceDeliveryUncertain:
            # A failed acknowledgement is not proof that QQ didn't receive the record.
            return {'success': False, 'state': 'uncertain', 'request_id': body.request_id,
                    'message': '发送结果待核实；相同 request_id 不会自动重发'}
