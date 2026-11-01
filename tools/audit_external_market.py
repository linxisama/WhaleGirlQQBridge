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
import json
import re
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = 'https://kondius.cn/api/community/plugins'
OUT = Path(r'D:\聊天机器人\插件市场\_兼容性报告')
CACHE = ROOT / '.tmp' / 'external-market'
NODE = 'node'


def fetch_json(url, timeout=30):
    request = urllib.request.Request(url, headers={'User-Agent': 'WhaleGirl-Market-Audit/1.0'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def download(item: dict, retries=2) -> tuple[str, str]:
    identifier = item['id']
    target = CACHE / f'{identifier}.zip'
    if target.is_file() and target.stat().st_size > 0:
        return identifier, str(target)
    url = 'https://kondius.cn' + str(item.get('downloadUrl') or '')
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'WhaleGirl-Market-Audit/1.0'})
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return identifier, str(target)
        except Exception:
            if attempt >= retries:
                raise
            time.sleep(0.5 * (attempt + 1))
    return identifier, ''


def inspect_zip(path: str) -> dict:
    result = {'ok': False, 'problems': [], 'files': 0, 'js_bytes': 0,
              'manifest': {}, 'host_imports': [], 'node_imports': [],
              'external_imports': [], 'syntax': 'unknown'}
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
        if not js:
            result['problems'].append('缺少 JS 入口')
            return result
        try:
            manifest = json.loads(archive.read(manifests[0]).decode('utf-8'))
        except (ValueError, UnicodeError) as exc:
            result['problems'].append(f'plugin.json 无法解析：{exc}')
            manifest = {}
        result['manifest'] = manifest if isinstance(manifest, dict) else {}
        entry = next((n for n in js if Path(n).name == 'index.js'), js[0])
        code = archive.read(entry).decode('utf-8', 'replace')
        result['js_bytes'] = len(code.encode('utf-8'))
        result['host_imports'] = sorted(set(re.findall(r"from\s+['\"]([^'\"]*src/[^'\"]*)['\"]", code)))
        result['node_imports'] = sorted(set(re.findall(r"from\s+['\"](node:[^'\"]+)['\"]", code)))
        result['external_imports'] = sorted(set(
            module for module in re.findall(r"from\s+['\"]([^'\"]+)['\"]", code)
            if not module.startswith(('.', 'node:')) and module not in ('fs', 'path', 'os')))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'index.mjs'
            target.write_text(code, encoding='utf-8')
            process = subprocess.run([NODE, '--check', str(target)], capture_output=True, text=True)
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


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--workers', type=int, default=8)
    args = parser.parse_args(argv)
    data = fetch_json(API)
    items = [item for item in data.get('items', []) if item.get('status') == 'approved']
    if args.limit:
        items = items[:args.limit]
    CACHE.mkdir(parents=True, exist_ok=True)
    downloaded = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(download, item): item for item in items}
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            item = futures[future]
            try:
                identifier, path = future.result()
                downloaded.append((item, path))
            except Exception as exc:
                downloaded.append((item, ''))
            if index % 25 == 0:
                print(f'下载 {index}/{len(items)}', flush=True)
    results = []
    for item, path in downloaded:
        if not path:
            results.append({'item': item, 'result': {'ok': False, 'problems': ['下载失败'],
                                                     'manifest': {}}})
            continue
        results.append({'item': item, 'result': inspect_zip(path)})
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
        'source': API, 'tested': len(results), 'approved_total': len(items),
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
             'external_imports': row['result'].get('external_imports') or [],
             'js_bytes': row['result'].get('js_bytes')}
            for row in results]}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'external-market-audit.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 外部 QQ Agent 插件市场 · 静态兼容性审计', '',
             f'- 来源：{API}',
             f'- 测试条目：{len(results)}（approved {len(items)} / 全站 576）',
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
    (OUT / 'external-market-audit.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('tested', 'valid', 'syntax_ok',
                                             'host_dependent', 'external_dependent',
                                             'classification')}, ensure_ascii=False, indent=2))
    print('报告：', OUT)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
