"""Local engine setup and bounded, read-only connection checks for the native console."""
from __future__ import annotations

import copy
import json
import time
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from .audio_contracts import ErrorCode, reject
from .audio_providers._http import request_bytes
from .audio_providers.local_existing import AudioCppProvider, GptSovitsProvider
from .audio_providers.qwen_webui import QwenWebUIProvider

LOCAL_PROVIDERS = ('audio_cpp', 'gpt_sovits_api_v2', 'qwen_webui')
FIELDS = {
    'audio_cpp': ('audio_cpp', {'endpoint': 'http://127.0.0.1:18100/v1/audio/speech',
                              'model': 'breeze-tts-2', 'voice': 'demo_3_woman'}),
    'gpt_sovits_api_v2': ('gpt_sovits', {'endpoint': 'http://127.0.0.1:9880/tts',
                        'ref_audio_path': '', 'prompt_text': '',
                        'prompt_lang': 'zh', 'text_lang': 'zh'}),
    'qwen_webui': ('qwen', {'endpoint': 'http://127.0.0.1:7860', 'mode': 'custom_voice',
                  'model': '', 'speaker': '', 'ref_audio_path': '', 'ref_text': '',
                  'instruct': ''}),
}


class EngineSettingsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: Literal['audio_cpp', 'gpt_sovits_api_v2', 'qwen_webui']
    params: dict[str, str] = Field(default_factory=dict)
    route_policy: Literal['local_only'] | None = None


class _Overlay:
    def __init__(self, config, changes):
        self.config, self.changes = config, changes

    def get(self, key, default=None):
        return self.changes.get(key, self.config.get(key, default))


def settings(config) -> dict:
    providers = {}
    for name, (prefix, defaults) in FIELDS.items():
        providers[name] = {key: str(config.get(f'qq_only.voice.{prefix}.{key}', value) or '')
                           for key, value in defaults.items()}
    return {'provider': config.get('qq_only.voice.provider', 'gpt_sovits_api_v2'),
            'route_policy': config.get('qq_only.voice.route_policy', 'local_only'),
            'providers': providers,
            'singing_connected': False, 'automatic_replies_configured_separately': True}


def _changes(config, body: EngineSettingsRequest) -> tuple[dict, object]:
    prefix, defaults = FIELDS[body.provider]
    if set(body.params) - set(defaults):
        reject(ErrorCode.INVALID_INPUT, '包含不支持的引擎字段')
    if any(len(value) > 4096 or '\x00' in value for value in body.params.values()):
        reject(ErrorCode.INVALID_INPUT, '配置字段过长或包含无效字符')
    changes = {f'qq_only.voice.{prefix}.{key}': value.strip()
               for key, value in body.params.items()}
    overlay = _Overlay(config, changes)
    cls = {'audio_cpp': AudioCppProvider, 'gpt_sovits_api_v2': GptSovitsProvider,
           'qwen_webui': QwenWebUIProvider}[body.provider]
    adapter = cls(overlay)
    # Endpoint validation is mandatory; an incomplete reference/model may be saved
    # so the user can import assets before attempting synthesis.
    if body.provider == 'qwen_webui':
        if urlsplit(adapter._base()).path.rstrip('/'):
            reject(ErrorCode.INVALID_INPUT, 'Qwen 服务地址请填写根地址，不含 API 路径')
        if overlay.get('qq_only.voice.qwen.mode', 'custom_voice') not in (
                'custom_voice', 'voice_clone', 'voice_design'):
            reject(ErrorCode.INVALID_INPUT, 'Qwen 模式无效')
    else:
        adapter._endpoint()
        if body.provider == 'audio_cpp':
            adapter._payload('连接配置验证', {})
    changes['qq_only.voice.provider'] = body.provider
    if body.route_policy is not None:
        changes['qq_only.voice.route_policy'] = body.route_policy
    return changes, adapter


def save_settings(config, body: EngineSettingsRequest) -> dict:
    changes, _adapter = _changes(config, body)
    storage_name = '_config' if hasattr(config, '_config') else 'values'
    storage = getattr(config, storage_name, None)
    snapshot = copy.deepcopy(storage) if isinstance(storage, dict) else None
    old = {key: config.get(key) for key in changes}
    try:
        for key, value in changes.items():
            config.set(key, value)
        config.save_atomic()
    except Exception:
        if snapshot is not None:
            storage.clear()
            storage.update(snapshot)
        else:
            for key, value in old.items():
                config.set(key, value)
        raise
    return {'success': True, **settings(config)}


async def probe(config, body: EngineSettingsRequest, *, transport=None) -> dict:
    _patch, adapter = _changes(config, body)
    endpoint = adapter._base() if body.provider == 'qwen_webui' else adapter._endpoint()
    parsed = urlsplit(endpoint)
    path = {'audio_cpp': '/health', 'gpt_sovits_api_v2': '/openapi.json',
            'qwen_webui': '/qwenapi/v1/models'}[body.provider]
    url = f'{parsed.scheme}://{parsed.netloc}{path}'
    started = time.monotonic()
    result = {'success': True, 'provider': body.provider, 'reachable': False,
              'synthesis_verified': False, 'models': [],
              'message': '未检测；连接成功也不代表模型已加载或语音合成成功。'}
    try:
        response = await request_bytes('GET', url, transport=transport,
                                       timeout=2.0, max_bytes=256 * 1024)
        result['reachable'] = 200 <= response.status < 300
        if result['reachable']:
            data = json.loads(response.content)
            if not isinstance(data, dict):
                raise ValueError('invalid discovery response')
            if body.provider == 'qwen_webui':
                models = data.get('models')
                if not isinstance(models, list):
                    raise ValueError('missing models')
                result['models'] = [str(item.get('name') or '')[:200]
                                    for item in models[:100] if isinstance(item, dict)]
            elif body.provider == 'gpt_sovits_api_v2' and '/tts' not in data.get('paths', {}):
                raise ValueError('missing tts route')
            result['message'] = '本机接口可达；请生成一句语音验证模型与音色，尚未验证 QQ 播放。'
        else:
            result['message'] = f'本机检测端点返回 HTTP {response.status}；请检查服务版本和地址。'
    except Exception:
        result['reachable'] = False
        result['message'] = '未连接到匹配的本机语音接口；请先启动已有服务并检查端口。'
    result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
    return result
