"""本地语音组件的客户端：只访问 127.0.0.1，不发送 QQ 消息。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HOST = '127.0.0.1'
PORT = int(os.environ.get('WHALE_VOICE_PORT', '8765'))


class VoiceUnavailable(RuntimeError):
    pass


def _request(path, payload=None, timeout=4):
    url = f'http://{HOST}:{PORT}{path}'
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode('utf-8'))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise VoiceUnavailable('本地语音组件未启动：' + str(exc)[:120]) from None
    if not body.get('ok'):
        raise VoiceUnavailable(str(body.get('error') or '语音组件返回失败'))
    return body


def health(timeout=1.5):
    return _request('/health', timeout=timeout)


def voices():
    return _request('/voices').get('voices') or []


def speak(text, voice='', rate=0, volume=100):
    text = str(text or '').strip()
    if not text:
        raise ValueError('没有可朗读的文本')
    return _request('/speak', {'text': text[:2000], 'voice': voice,
                               'rate': rate, 'volume': volume}, timeout=70)


def component_path():
    candidates = [
        Path(os.getenv('LOCALAPPDATA') or '') / 'Programs' / 'WhaleVoice' / 'WhaleVoice.exe',
        Path(__file__).resolve().parents[1] / 'voice_component' / 'dist' / 'WhaleVoice' / 'WhaleVoice.exe',
    ]
    return next((path for path in candidates if path.is_file()), None)


def start_component():
    path = component_path()
    if path is None:
        raise VoiceUnavailable('未安装语音组件；请先运行 WhaleVoice-Setup-1.0.0.exe')
    subprocess.Popen([str(path)], cwd=str(path.parent), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return str(path)
