"""独立桌宠：资源包校验、安装与进程启动。

不是把图片塞进主窗口，而是单独的 `--pet-window` 进程；主程序退出不影响它，
用户也可以单独关。资源包只允许图片 + pet.json，不执行代码。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PACK_FORMAT = 'whale-pet'
MAX_PACK = 24 * 1024 * 1024
MAX_FILES = 200
ALLOWED = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}


class PetPackError(ValueError):
    pass


def _safe(name):
    value = str(name or '').replace('\\', '/')
    if value.startswith('/') or '..' in value.split('/') or ':' in value.split('/')[0]:
        raise PetPackError(f'资源包路径不安全：{value[:80]}')
    return value


def inspect_pack(path):
    target = Path(str(path))
    if not target.is_file():
        raise PetPackError('找不到桌宠资源包')
    if target.stat().st_size > MAX_PACK:
        raise PetPackError('资源包过大（上限 24MB）')
    try:
        archive = zipfile.ZipFile(target)
    except (zipfile.BadZipFile, OSError):
        raise PetPackError('这不是有效的 zip 资源包') from None
    with archive:
        names = [n for n in archive.namelist() if not n.endswith('/')]
        if len(names) > MAX_FILES:
            raise PetPackError('资源包文件过多（上限 200）')
        for name in names:
            _safe(name)
        if 'pet.json' not in names:
            raise PetPackError('资源包缺少 pet.json')
        try:
            meta = json.loads(archive.read('pet.json').decode('utf-8'))
        except (ValueError, UnicodeError):
            raise PetPackError('pet.json 不是有效的 UTF-8 JSON') from None
        if not isinstance(meta, dict) or meta.get('format') != PACK_FORMAT:
            raise PetPackError(f'资源包格式不对，应为 {PACK_FORMAT}')
        if meta.get('version') != 1:
            raise PetPackError('只支持 version=1 的桌宠资源包')
        identifier = str(meta.get('id') or '').strip()
        if not identifier or not all(ch.isalnum() or ch in '_-' for ch in identifier):
            raise PetPackError('桌宠 id 只能包含字母、数字、下划线和短横线')
        frames = meta.get('frames')
        if not isinstance(frames, list) or not 1 <= len(frames) <= 120:
            raise PetPackError('frames 需要 1～120 张图片')
        clean = []
        total = 0
        for frame in frames:
            name = _safe(frame)
            if Path(name).suffix.lower() not in ALLOWED:
                raise PetPackError(f'桌宠只支持图片帧：{name}')
            if name not in names:
                raise PetPackError(f'资源包缺少帧：{name}')
            total += archive.getinfo(name).file_size
            if total > MAX_PACK:
                raise PetPackError('资源包解压后过大')
            clean.append(name)
        return {'id': identifier, 'name': str(meta.get('name') or identifier)[:60],
                'fps': max(1, min(30, int(meta.get('fps') or 8))),
                'scale': max(0.1, min(1.0, float(meta.get('scale') or 0.35))),
                'frames': clean, 'files': len(names), 'bytes': target.stat().st_size,
                'path': str(target)}


def packs_root(root):
    return Path(root) / 'data' / 'pet_packs'


def list_packs(root):
    base = packs_root(root)
    result = []
    if not base.is_dir():
        return result
    for folder in sorted(base.iterdir()):
        meta_path = folder / 'pet.json'
        if not folder.is_dir() or not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        result.append({'id': folder.name, 'name': str(meta.get('name') or folder.name),
                       'frames': len(meta.get('frames') or []),
                       'path': str(folder),
                       'pid': _pid(folder)})
    return result


def _pid(folder):
    try:
        return int((Path(folder) / '.pid').read_text(encoding='ascii').strip() or 0)
    except (OSError, ValueError):
        return 0


def install_pack(root, path, confirmed=False, overwrite=False):
    if not confirmed:
        raise PetPackError('未经确认：请先预检资源包')
    info = inspect_pack(path)
    base = packs_root(root)
    destination = base / info['id']
    if destination.exists() and not overwrite:
        raise PetPackError('已存在同名桌宠；要更新请选择覆盖')
    base.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(info['path']) as archive:
        for name in archive.namelist():
            if name.endswith('/'):
                continue
            target = destination / _safe(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
    (destination / '.pid').unlink(missing_ok=True)
    return {'id': info['id'], 'name': info['name'], 'path': str(destination)}


def launch_pack(root, identifier):
    base = packs_root(root) / str(identifier)
    if not (base / 'pet.json').is_file():
        raise PetPackError('桌宠不存在，请先导入资源包')
    pid = _pid(base)
    if pid:
        return {'id': identifier, 'pid': pid, 'already': True}
    if getattr(sys, 'frozen', False):
        command = [sys.executable, '--pet-window', str(base)]
    else:
        command = [sys.executable, '-m', 'src.pet_window', str(base)]
    creation = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    process = subprocess.Popen(command, cwd=str(root), stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=creation)
    (base / '.pid').write_text(str(process.pid), encoding='ascii')
    return {'id': identifier, 'pid': process.pid, 'already': False}


def stop_pack(root, identifier):
    base = packs_root(root) / str(identifier)
    pid = _pid(base)
    if not pid:
        return {'id': identifier, 'stopped': False}
    try:
        os.kill(pid, 15)
    except OSError:
        pass
    try:
        (base / '.pid').unlink(missing_ok=True)
    except OSError:
        pass
    return {'id': identifier, 'stopped': True, 'pid': pid}
