"""功能包（feature pack）导入：把插件、Agent 技能、长期偏好打成一个 zip。

目标（用户要求：后续功能以导入为主）：以后加能力不再改程序，发一个
``.whalepack.zip`` 就能带走插件 + 说明 + 默认偏好。

包格式（``pack.json``）::

    {
      "format": "whale-feature-pack",
      "version": 1,
      "name": "表情增强包",
      "description": "…",
      "author": "…",
      "plugins": [{"file": "plugins/sticker_plus.py", "overwrite": false}],
      "skills": [{"file": "skills/sticker_help.json"}],
      "preferences": [{"key": "称呼", "value": "老鲸"}]
    }

安全边界：
- 只接受 zip，最多 200 个条目、总解压 24MB；
- 拒绝绝对路径、``..``、符号链接与非白名单后缀；
- 插件仍走 ``plugin_installer`` 的安全检查（危险用法、必须继承 PluginBase）；
- 技能包只当**数据**存进学习库，绝不执行；
- 安装必须 ``confirmed=True``（先预检给用户看）。
"""
from __future__ import annotations

import io
import json
import tempfile
import zipfile
from pathlib import Path

FORMAT = 'whale-feature-pack'
MAX_PACK_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 24 * 1024 * 1024
MAX_ENTRIES = 200
MAX_MANIFEST_BYTES = 64 * 1024
SECTIONS = ('plugins', 'skills', 'preferences')


class FeaturePackError(ValueError):
    pass


def _safe_name(name: str) -> str:
    value = str(name or '').replace('\\', '/')
    if value.startswith('/') or '..' in value.split('/') or ':' in value.split('/')[0]:
        raise FeaturePackError(f'包内路径不安全：{value[:80]}')
    return value


def _open_zip(path) -> zipfile.ZipFile:
    target = Path(str(path))
    if not target.is_file():
        raise FeaturePackError('找不到功能包文件')
    if target.stat().st_size > MAX_PACK_BYTES:
        raise FeaturePackError('功能包过大（上限 16MB）')
    try:
        archive = zipfile.ZipFile(target)
    except (zipfile.BadZipFile, OSError):
        raise FeaturePackError('这不是有效的 zip 功能包') from None
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise FeaturePackError(f'功能包条目过多（上限 {MAX_ENTRIES}）')
    total = 0
    for info in infos:
        _safe_name(info.filename)
        if info.is_dir():
            continue
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise FeaturePackError('功能包解压后过大（上限 24MB）')
    if 'pack.json' not in {info.filename for info in infos}:
        raise FeaturePackError('功能包缺少 pack.json')
    return archive


def _manifest(archive: zipfile.ZipFile) -> dict:
    info = archive.getinfo('pack.json')
    if info.file_size > MAX_MANIFEST_BYTES:
        raise FeaturePackError('pack.json 过大')
    try:
        data = json.loads(archive.read(info).decode('utf-8'))
    except (ValueError, UnicodeError, RecursionError):
        raise FeaturePackError('pack.json 不是有效的 UTF-8 JSON') from None
    if not isinstance(data, dict):
        raise FeaturePackError('pack.json 必须是对象')
    if data.get('format') != FORMAT:
        raise FeaturePackError(f'功能包格式不对，应为 {FORMAT}')
    if data.get('version') != 1:
        raise FeaturePackError('只支持 version=1 的功能包')
    if not str(data.get('name') or '').strip():
        raise FeaturePackError('功能包需要 name')
    return data


def _entries(manifest: dict) -> list[dict]:
    result = []
    for section in SECTIONS:
        items = manifest.get(section) or []
        if section == 'preferences':
            if not isinstance(items, list) or len(items) > 50:
                raise FeaturePackError('preferences 最多 50 条')
            for item in items:
                if not isinstance(item, dict) or set(item) - {'key', 'value'}:
                    raise FeaturePackError('preferences 条目格式不正确')
                key = str(item.get('key') or '').strip()
                value = str(item.get('value') or '').strip()
                if not key or not value:
                    raise FeaturePackError('偏好需要 key 与 value')
                result.append({'kind': 'preference', 'key': key[:40], 'value': value[:500]})
            continue
        if not isinstance(items, list) or len(items) > 30:
            raise FeaturePackError(f'{section} 最多 30 项')
        for item in items:
            if not isinstance(item, dict) or set(item) - {'file', 'overwrite'}:
                raise FeaturePackError(f'{section} 条目格式不正确')
            name = _safe_name(item.get('file'))
            suffix = Path(name).suffix.lower()
            expected = ('.py',) if section == 'plugins' else ('.json',)
            if suffix not in expected:
                raise FeaturePackError(f'{section} 文件必须是 {"/".join(expected)}：{name}')
            result.append({'kind': 'plugin' if section == 'plugins' else 'skill',
                           'file': name, 'overwrite': bool(item.get('overwrite'))})
    return result


def inspect(path) -> dict:
    """只读取和校验，不写任何文件。"""
    archive = _open_zip(path)
    try:
        manifest = _manifest(archive)
        entries = _entries(manifest)
        present = {info.filename for info in archive.infolist()}
        items = []
        for entry in entries:
            if entry['kind'] == 'preference':
                items.append({**entry, 'ok': True})
                continue
            if entry['file'] not in present:
                items.append({**entry, 'ok': False, 'error': '包内缺少这个文件'})
                continue
            size = archive.getinfo(entry['file']).file_size
            items.append({**entry, 'ok': True, 'bytes': size,
                          'name': Path(entry['file']).stem})
        return {'ok': bool(items) and all(item.get('ok') for item in items),
                'name': str(manifest.get('name'))[:80],
                'description': str(manifest.get('description') or '')[:500],
                'author': str(manifest.get('author') or '')[:80],
                'version': 1, 'items': items,
                'count': len(items),
                'plugins': sum(1 for item in items if item['kind'] == 'plugin'),
                'skills': sum(1 for item in items if item['kind'] == 'skill'),
                'preferences': sum(1 for item in items if item['kind'] == 'preference')}
    finally:
        archive.close()


def install(path, plugins_dir, learning, confirmed=False, overwrite=False) -> dict:
    """安装功能包。插件仍需通过安装器的安全检查；单个失败不影响其它条目。"""
    if not confirmed:
        raise FeaturePackError('未经确认：请先预检并把内容展示给用户')
    report = inspect(path)
    if not report['ok']:
        raise FeaturePackError('功能包检查未通过，不能安装')
    archive = _open_zip(path)
    results = []
    try:
        manifest = _manifest(archive)
        entries = _entries(manifest)
        with tempfile.TemporaryDirectory(prefix='whalepack_') as temp:
            temp = Path(temp)
            for index, entry in enumerate(entries):
                if entry['kind'] == 'preference':
                    try:
                        learning.set_preference(entry['key'], entry['value'], note='功能包：' + report['name'])
                        results.append({'kind': 'preference', 'key': entry['key'], 'success': True})
                    except ValueError as exc:
                        results.append({'kind': 'preference', 'key': entry['key'],
                                        'success': False, 'message': str(exc)})
                    continue
                target = temp / f'{index}_{Path(entry["file"]).name}'
                target.write_bytes(archive.read(entry['file']))
                if entry['kind'] == 'plugin':
                    from . import plugin_installer
                    try:
                        installed = plugin_installer.install_from_file(
                            str(target), Path(plugins_dir), Path(entry['file']).stem,
                            True, overwrite or entry.get('overwrite'))
                        results.append({'kind': 'plugin', 'file': entry['file'],
                                        'success': True, **installed})
                    except plugin_installer.InstallError as exc:
                        results.append({'kind': 'plugin', 'file': entry['file'],
                                        'success': False, 'message': str(exc)})
                    except Exception as exc:
                        results.append({'kind': 'plugin', 'file': entry['file'],
                                        'success': False, 'message': f'{type(exc).__name__}: {exc}'})
                else:
                    try:
                        package = json.loads(target.read_text(encoding='utf-8'))
                        record = learning.import_skill(package, source=f'功能包：{report["name"]}')
                        results.append({'kind': 'skill', 'file': entry['file'],
                                        'success': True, 'skill': record})
                    except (ValueError, OSError, UnicodeError) as exc:
                        results.append({'kind': 'skill', 'file': entry['file'],
                                        'success': False, 'message': str(exc)})
        failed = [item for item in results if not item.get('success')]
        return {'success': not failed, 'name': report['name'], 'results': results,
                'failed': len(failed), 'total': len(results),
                'plugins': [item['plugin_id'] for item in results
                            if item.get('success') and item.get('kind') == 'plugin']}
    finally:
        archive.close()
