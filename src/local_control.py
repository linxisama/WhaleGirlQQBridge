"""本机电脑动作：打开路径/网址、向焦点窗口输入、快捷键、截图。

只由控制台 Agent 方案在用户点「应用方案」后调用；聊天侧不直接调用。
"""
from __future__ import annotations

import os
import re
import subprocess
import time
import webbrowser
from pathlib import Path


def _powershell(script: str, timeout: int = 15):
    return subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
         '-Command', script], capture_output=True, text=True, encoding='utf-8',
        errors='replace', timeout=timeout,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def open_path(path: str):
    value = str(path or '').strip()
    if not value:
        raise ValueError('路径不能为空')
    if not Path(value).exists():
        raise ValueError('路径不存在')
    os.startfile(value)  # type: ignore[attr-defined]


def open_url(url: str):
    value = str(url or '').strip()
    if not value.startswith(('http://', 'https://')):
        raise ValueError('只允许打开 http/https 网址')
    if not webbrowser.open(value):
        raise RuntimeError('系统没有打开浏览器')


def _send_keys(sequence: str):
    result = _powershell(
        "Add-Type -AssemblyName System.Windows.Forms; "
        "[System.Windows.Forms.SendKeys]::SendWait('" + sequence.replace("'", "''") + "')")
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or 'SendKeys 失败')[-300:])


def type_text(text: str):
    value = str(text or '')
    if not value:
        raise ValueError('输入文字不能为空')
    # SendKeys 把 + ^ % ~ ( ) { } [ ] 当控制符；逐字转义后发送。
    escaped = ''.join('{' + ch + '}' if ch in '+^%~(){}[]' else ch for ch in value)
    _send_keys(escaped)


_HOTKEYS = {'ctrl': '^', 'control': '^', 'alt': '%', 'shift': '+', 'win': '^', 'cmd': '^'}


def hotkey(keys: str):
    raw = str(keys or '').strip().lower()
    if not raw:
        raise ValueError('快捷键不能为空')
    parts = [part for part in re.split(r'[\s+\-]+', raw) if part]
    if not parts:
        raise ValueError('快捷键格式不正确')
    prefix = ''.join(_HOTKEYS.get(part, '') for part in parts[:-1])
    key = parts[-1]
    aliases = {'enter': '{ENTER}', 'tab': '{TAB}', 'esc': '{ESC}', 'escape': '{ESC}',
               'space': ' ', 'backspace': '{BACKSPACE}', 'delete': '{DELETE}',
               'up': '{UP}', 'down': '{DOWN}', 'left': '{LEFT}', 'right': '{RIGHT}'}
    key = aliases.get(key, key)
    if len(key) > 20 or not re.fullmatch(r'[a-z0-9{} ]+', key, re.IGNORECASE):
        raise ValueError('快捷键主键不支持')
    _send_keys(prefix + key)


def screenshot() -> str:
    folder = Path(os.getenv('LOCALAPPDATA') or Path.home()) / 'WhaleGirlQQ' / 'screenshots'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f'agent-{time.strftime("%Y%m%d-%H%M%S")}.png'
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
        "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen; "
        "$bmp=New-Object System.Drawing.Bitmap($b.Width,$b.Height); "
        "$g=[System.Drawing.Graphics]::FromImage($bmp); "
        "$g.CopyFromScreen($b.Left,$b.Top,0,0,$bmp.Size); "
        f"$bmp.Save('{str(target).replace(chr(39), chr(39)*2)}',[System.Drawing.Imaging.ImageFormat]::Png); "
        "$g.Dispose(); $bmp.Dispose()")
    result = _powershell(script, timeout=20)
    if result.returncode != 0 or not target.is_file():
        raise RuntimeError((result.stderr or result.stdout or '截图失败')[-300:])
    return str(target)
