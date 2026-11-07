#!/usr/bin/env python3
"""审计外部 QQ Agent 插件市场：下载、静态校验、可移植性分类。

只做静态检查，不执行第三方代码：
- 下载 approved 插件 zip（缓存到 .tmp/external-market）；
- 校验 zip 路径、plugin.json 字段、index.js 存在；
- 用 Node `--check` 验证 ES module 语法；
- 统计宿主 API 依赖、第三方依赖、能力声明；
- 输出 Markdown + JSON 报告到 D:\\聊天机器人\\插件市场\\_兼容性报告。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import subprocess
import tempfile
import time
import urllib.request
import uuid
import zipfile
import zlib
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]
API = 'https://kondius.cn/api/community/plugins'
OUT = Path(r'D:\聊天机器人\插件市场\_兼容性报告')
CACHE = ROOT / '.tmp' / 'external-market'
NODE = 'node'


def fetch_json(url, timeout=30):
    request = urllib.request.Request(url, headers={'User-Agent': 'WhaleGirl-Market-Audit/1.0'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def cache_path(item: dict) -> Path:
    identifier = item.get('id')
    if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,199}', identifier):
        raise ValueError('市场条目id不能用作缓存文件名')
    return CACHE / f'{identifier}.zip'


def valid_zip(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.testzip() is None
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, EOFError,
            NotImplementedError, zlib.error):
        return False


def atomic_write(path: Path, data: bytes):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.part')
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def download(item: dict, retries=2, *, force=False) -> tuple[str, str]:
    target = cache_path(item)
    identifier = item['id']
    url = urljoin('https://kondius.cn', str(item.get('downloadUrl') or ''))
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc != 'kondius.cn' or not parsed.path.startswith('/api/community/plugins/'):
        raise ValueError('市场下载地址不是本站插件下载接口')
    stamp = target.with_suffix('.cache.json')
    provenance = {'version': item.get('version'), 'url': url}
    if not force and valid_zip(target):
        try:
            metadata = json.loads(stamp.read_text(encoding='utf-8'))
            if (all(metadata.get(k) == v for k, v in provenance.items())
                    and metadata.get('sha256') == hashlib.sha256(target.read_bytes()).hexdigest()):
                return identifier, str(target)
        except (OSError, ValueError, AttributeError):
            pass  # A legacy or unverified cache is refreshed once online.
    for attempt in range(retries + 1):
        temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.part')
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'WhaleGirl-Market-Audit/1.0'})
            target.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(request, timeout=30) as response:
                with temporary.open('wb') as stream:
                    while chunk := response.read(64 * 1024):
                        stream.write(chunk)
            if not valid_zip(temporary):
                raise ValueError('下载内容不是完整有效的ZIP，原缓存未替换')
            checksum = hashlib.sha256(temporary.read_bytes()).hexdigest()
            temporary.replace(target)
            atomic_write(stamp, json.dumps({**provenance, 'sha256': checksum},
                                          ensure_ascii=False).encode('utf-8'))
            return identifier, str(target)
        except Exception:
            if attempt >= retries:
                raise
            time.sleep(0.5 * (attempt + 1))
        finally:
            temporary.unlink(missing_ok=True)
    return identifier, ''


def inspect_zip(path: str, *, check_syntax=True) -> dict:
    result = {'ok': False, 'problems': [], 'files': 0, 'js_bytes': 0,
              'manifest': {}, 'host_imports': [], 'node_imports': [],
              'external_imports': [], 'syntax': 'unknown', 'permissions_array': False,
              'permissions': [], 'archive_sha256': '', 'manifest_path': '',
              'manifest_readable': False}
    result['archive_sha256'] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        result['problems'].append(f'不是有效 zip：{exc}')
        return result
    with archive:
        names = [n for n in archive.namelist() if not n.endswith('/')]
        result['files'] = len(names)
        for name in names:
            if '..' in Path(name).parts or Path(name).is_absolute():
                result['problems'].append('包内路径不安全')
                return result
        manifests = [n for n in names if n.lower().endswith('plugin.json')]
        js = [n for n in names if n.lower().endswith(('.js', '.mjs', '.cjs'))]
        if not manifests:
            result['problems'].append('缺少 plugin.json')
            return result
        try:
            manifest = json.loads(archive.read(manifests[0]).decode('utf-8'))
        except (ValueError, UnicodeError) as exc:
            result['problems'].append(f'plugin.json 无法解析：{exc}')
            manifest = None
        result['manifest'] = manifest if isinstance(manifest, dict) else {}
        result['manifest_readable'] = isinstance(manifest, dict)
        if not isinstance(manifest, dict):
            result['problems'].append('plugin.json 必须是对象')
        result['manifest_path'] = manifests[0]
        permissions = result['manifest'].get('permissions')
        result['permissions_array'] = isinstance(permissions, list)
        result['permissions'] = permissions if isinstance(permissions, list) else []
        if not js:
            result['problems'].append('缺少 JS 入口')
            return result
        entry = next((n for n in js if Path(n).name == 'index.js'), js[0])
        code = archive.read(entry).decode('utf-8', 'replace')
        result['js_bytes'] = len(code.encode('utf-8'))
        result['host_imports'] = sorted(set(re.findall(r"from\s+['\"]([^'\"]*src/[^'\"]*)['\"]", code)))
        result['node_imports'] = sorted(set(re.findall(r"from\s+['\"](node:[^'\"]+)['\"]", code)))
        result['external_imports'] = sorted(set(
            module for module in re.findall(r"from\s+['\"]([^'\"]+)['\"]", code)
            if not module.startswith(('.', 'node:')) and module not in ('fs', 'path', 'os')))
        if not check_syntax:
            result['syntax'] = 'not-checked'
        else:
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / 'index.mjs'
                target.write_text(code, encoding='utf-8')
                process = subprocess.run([NODE, '--check', str(target)], capture_output=True, text=True,
                                         timeout=20)
                result['syntax'] = 'ok' if process.returncode == 0 else 'error'
                if process.returncode != 0:
                    result['problems'].append('JS 语法错误：' + (process.stderr or '')[:200])
        manifest_id = str(result['manifest'].get('id') or '')
        if manifest_id and manifest_id != Path(entry).parts[0]:
            result['problems'].append(f"manifest id 与目录不一致：{manifest_id}")
        result['ok'] = not result['problems']
        return result


def classify(result: dict) -> str:
    if not result.get('ok'):
        return '不可用/需修复'
    host = result.get('host_imports') or []
    external = result.get('external_imports') or []
    if not host and not external:
        return '可直接参考（无宿主依赖）'
    if host and not external:
        return '需移植到本机 API（宿主依赖）'
    return '需移植且补依赖'


def permission_summary(results: list) -> dict:
    """Count declarations, not network success; malformed permissions fail closed."""
    arrays = [row['result'] for row in results if row['result'].get('permissions_array')]
    words = Counter(word for result in arrays for word in result['permissions']
                    if isinstance(word, str))
    return {'packages': len(results), 'permissions_arrays': len(arrays),
            'web_fetch_packages': sum('web_fetch' in result['permissions'] for result in arrays),
            'permission_word_occurrences': dict(sorted(words.items())),
            'unreadable_manifests': sum(not row['result'].get('manifest_readable') for row in results),
            'download_failures': sum(not row.get('path') for row in results)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--output-dir', type=Path, default=OUT)
    parser.add_argument('--permissions-only', action='store_true', help='只做静态权限统计，不运行Node')
    parser.add_argument('--offline', action='store_true', help='使用输出目录的市场快照和本地ZIP，不联网')
    parser.add_argument('--refresh', action='store_true', help='忽略已验证缓存，重新下载；不能与--offline合用')
    args = parser.parse_args(argv)
    if args.workers < 1 or args.limit < 0:
        parser.error('workers须为正整数，limit须为非负整数')
    if args.offline and args.refresh:
        parser.error('--offline与--refresh不能同时使用')
    output = args.output_dir
    snapshot = output / 'market-list.json'
    baseline_file = output / 'external-market-audit.json'
    baseline = {}
    if args.offline and baseline_file.is_file():
        previous = json.loads(baseline_file.read_text(encoding='utf-8'))
        expected_snapshot = previous.get('market_snapshot_sha256')
        if expected_snapshot and hashlib.sha256(snapshot.read_bytes()).hexdigest() != expected_snapshot:
            parser.error('离线市场清单快照与历史报告基线不一致；请使用匹配的快照或新的输出目录')
        baseline = {row['id']: row for row in previous['items'] if row.get('archive_sha256')}
    data = json.loads(snapshot.read_text(encoding='utf-8')) if args.offline else fetch_json(API)
    items = [item for item in data.get('items', []) if item.get('status') == 'approved']
    approved_total = len(items)
    if args.limit:
        items = items[:args.limit]
    CACHE.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    if not args.offline:
        atomic_write(snapshot, json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))
    downloaded = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        def retrieve(item):
            if args.offline:
                path = cache_path(item)
                if not path.is_file():
                    raise FileNotFoundError('离线缓存缺失：' + item['id'])
                if not valid_zip(path):
                    raise ValueError('离线缓存ZIP损坏：' + item['id'])
                return item['id'], str(path)
            return download(item, force=args.refresh)
        futures = {pool.submit(retrieve, item): item for item in items}
        failures = {}
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            item = futures[future]
            try:
                identifier, path = future.result()
                downloaded.append((item, path))
            except Exception as exc:
                downloaded.append((item, ''))
                failures[item['id']] = str(exc)
            if index % 25 == 0:
                print(f'下载 {index}/{len(items)}', flush=True)
    results = []
    for item, path in downloaded:
        if not path:
            results.append({'item': item, 'path': '', 'result': {'ok': False, 'problems': [failures[item['id']]],
                                                     'manifest': {}}})
            continue
        try:
            inspected = inspect_zip(path, check_syntax=not args.permissions_only)
            expected = baseline.get(item['id'])
            if expected:
                if (expected['archive_sha256'] != inspected.get('archive_sha256')
                        or expected.get('version') != item.get('version')):
                    raise ValueError('离线缓存内容或版本与历史报告基线不一致：' + item['id'])
                inspected['baseline_verified'] = True
        except Exception as exc:
            inspected = {'ok': False, 'manifest': {}, 'problems': [str(exc)]}
        results.append({'item': item, 'path': path, 'result': inspected})
    results.sort(key=lambda row: row['item']['id'])
    cats = Counter()
    for row in results:
        row['category'] = classify(row['result'])
        if row['result'].get('manifest'):
            cats[str(row['result']['manifest'].get('category') or row['item'].get('category'))] += 1
    syntax_ok = sum(1 for row in results if row['result'].get('syntax') == 'ok')
    ok = sum(1 for row in results if row['result'].get('ok'))
    host = sum(1 for row in results if row['result'].get('host_imports'))
    external = sum(1 for row in results if row['result'].get('external_imports'))
    report = {
        'source': API, 'tested': len(results), 'approved_total': approved_total,
        'permissions_summary': permission_summary(results),
        'permissions_only': args.permissions_only,
        'market_snapshot_sha256': hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        'baseline_verified_packages': sum(bool(row['result'].get('baseline_verified')) for row in results),
        'valid': ok, 'syntax_ok': syntax_ok, 'host_dependent': host,
        'external_dependent': external, 'categories': dict(cats),
        'classification': dict(Counter(row['category'] for row in results)),
        'items': [
            {'id': row['item']['id'], 'name': row['item'].get('name'),
             'version': row['item'].get('version'), 'status': row['item'].get('status'),
             'category': row['item'].get('category'),
             'ok': row['result'].get('ok'), 'syntax': row['result'].get('syntax'),
             'class': row['category'], 'problems': row['result'].get('problems') or [],
             'host_imports': row['result'].get('host_imports') or [],
             'permissions_array': row['result'].get('permissions_array', False),
             'manifest_readable': row['result'].get('manifest_readable', False),
             'permissions': row['result'].get('permissions', []),
             'archive_sha256': row['result'].get('archive_sha256', ''),
             'manifest_path': row['result'].get('manifest_path', ''),
             'external_imports': row['result'].get('external_imports') or [],
             'js_bytes': row['result'].get('js_bytes')}
            for row in results]}
    incomplete = bool(report['permissions_summary']['download_failures']
                      or report['permissions_summary']['unreadable_manifests'])
    report['complete'] = not incomplete
    report_name = 'external-market-audit.failed' if args.offline and baseline_file.is_file() and incomplete else 'external-market-audit'
    atomic_write(output / (report_name + '.json'), json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8'))
    lines = ['# 外部 QQ Agent 插件市场 · 静态兼容性审计', '',
             f'- 来源：{API}',
             f'- 扫描完整：{report["complete"]}；离线基线验证：{report["baseline_verified_packages"]}包',
             f'- 测试条目：{len(results)}（市场 approved {approved_total} / 清单 {len(data.get("items", []))}）',
             '- 权限声明统计（不代表真实联网成功）：' + json.dumps(report['permissions_summary'], ensure_ascii=False),
             f'- zip 与 manifest 通过：{ok}',
             f'- JS 语法通过：{syntax_ok}',
             f'- 依赖宿主 API：{host}；依赖第三方模块：{external}',
             f'- 分类：{report["classification"]}', '',
             '| 插件 | 名称 | 版本 | 分类 | 语法 | 判定 | 问题/宿主依赖 |',
             '|---|---|---|---|---|---|---|']
    for row in report['items']:
        detail = '；'.join((row['problems'] or []) + (row['host_imports'][:2] or []))[:120]
        lines.append(f"| `{row['id']}` | {row['name']} | {row['version']} | {row['category']} | "
                     f"{row['syntax']} | {row['class']} | {detail} |")
    atomic_write(output / (report_name + '.md'), ('\n'.join(lines) + '\n').encode('utf-8'))
    print(json.dumps({k: report[k] for k in ('tested', 'valid', 'syntax_ok',
                                             'host_dependent', 'external_dependent',
                                             'classification')}, ensure_ascii=False, indent=2))
    print('权限：', json.dumps(report['permissions_summary'], ensure_ascii=False))
    print('报告：', output / (report_name + '.json'))
    return 1 if incomplete else 0


if __name__ == '__main__':
    raise SystemExit(main())
