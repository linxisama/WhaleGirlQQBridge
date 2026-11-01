#!/usr/bin/env python3
"""对市场 512 个 QQ Agent 扩展跑批：装进 0.4 宿主布局，再真加载一次看能枚举出什么。

为什么要有这个跑批：市场清单把 338 个扩展判成 P3-host-source（结构性不可用），
理由是 index.js 里 `../../src/config.js` 换宿主就 ERR_MODULE_NOT_FOUND。这个结论
只在"扩展没装在 <host_root>/plugins/<id>/"时成立。本脚本把扩展装到该布局下，
用真 node 进程加载一次，用实测数字回答"到底多少个能真正加载并枚举出工具"。

用法：
  python tools/sweep_market_extensions.py --limit 30
  python tools/sweep_market_extensions.py --copy --limit 30
  python tools/sweep_market_extensions.py --ids access-control,account-appearance --copy
  python tools/sweep_market_extensions.py                     # 全量 512

产物：.tmp/inv/sweep-report.json 与 .tmp/inv/sweep-report.md
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import qqagent_host  # noqa: E402

MARKET = ROOT / '.tmp' / 'external-market'
INV = ROOT / '.tmp' / 'inv'
WORK = ROOT / '.tmp' / 'sweep-work'
INVENTORY = INV / 'plugin-tools.json'
REPORT_JSON = INV / 'sweep-report.json'
REPORT_MD = INV / 'sweep-report.md'
SKILLS_DIRNAME = 'skills'

# 失败原因归桶：报告要看的是"为什么"，不是 512 条各不相同的 stderr。
REASON_BUCKETS = (
    ('ERR_MODULE_NOT_FOUND', '宿主源码/依赖找不到（相对导入落点不对或缺包）'),
    ('\\skills\\', '要宿主根的 skills/ 树（可选目录，--with-skills 可补）'),
    ('/skills/', '要宿主根的 skills/ 树（可选目录，--with-skills 可补）'),
    ('electron', '需要 electron 运行时'),
    ('Cannot find package', '缺第三方 npm 包'),
    ('ERR_UNSUPPORTED_DIR_IMPORT', '目录导入（缺 index.js）'),
    ('SyntaxError', '语法错误（非 ES module / 语法过新）'),
    ('timeout', '加载超时（被强杀）'),
    ('no-json', '宿主脚本没吐出 JSON（进程崩溃或提前退出）'),
    ('not loadable', '清单缺失或 loader 报 loadError'),
    ('load-failed', 'loader 报 loadError'),
)


def _read_tiers() -> dict:
    """从市场清单取每个扩展的分层（P1..P6），用于分层统计。"""
    try:
        payload = json.loads(INVENTORY.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    tiers = {}
    for item in payload.get('plugins') or []:
        if isinstance(item, dict) and item.get('id'):
            tiers[str(item['id'])] = str(item.get('tier') or '?')
    return tiers


def _select(paths, limit: int, wanted: list) -> list:
    selected = sorted(paths)
    names = {str(item).strip() for item in (wanted or []) if str(item).strip()}
    if names:
        selected = [path for path in selected if path.stem in names]
    if limit and limit > 0:
        selected = selected[:limit]
    return selected


def _seed_from(origin: Path, host: Path) -> int:
    """把已装好的扩展搬进跑批 host（复用），只补缺、不覆盖。"""
    source = origin / 'plugins'
    if not source.is_dir():
        return 0
    target = host / 'plugins'
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for folder in sorted(source.iterdir()):
        if not folder.is_dir() or folder.name.startswith('.') or folder.name.startswith('install-'):
            continue
        if (target / folder.name).exists():
            continue
        try:
            shutil.copytree(folder, target / folder.name, dirs_exist_ok=True)
        except OSError:
            continue
        copied += 1
    return copied


def _prepare_host(args) -> tuple:
    """返回 (host_root, 说明 dict, 错误)。host_root 永远是可写副本，不是只读基线。"""
    info = {'mode': 'copy' if args.copy else 'reuse', 'copied': 0, 'skipped': 0,
            'seeded': 0, 'smokeMs': 0}
    explicit = args.host_root
    resolved = qqagent_host.resolve_host_root(explicit=explicit)
    if not args.copy:
        if resolved is None:
            return None, info, '找不到可用的 host_root；改用 --copy 让脚本自己制备一个'
        if qqagent_host.is_protected(resolved):
            # 只读基线：host-one 会往 source 下建 plugins-host-<pid>/，所以基线只当源用。
            host = WORK / 'host'
            info['reuseFrom'] = str(resolved)
        else:
            host = resolved
        if not qqagent_host.is_host_root(host):
            prepared = qqagent_host.ensure_host_root(target=host, source=resolved)
            info.update(copied=prepared['copied'], skipped=prepared['skipped'],
                        smokeMs=(prepared.get('smoke') or {}).get('duration_ms', 0))
            if not prepared['ok']:
                return None, info, 'host_root 制备失败：%s' % (prepared.get('error') or {})
        seed_origin = resolved if qqagent_host.is_protected(resolved) else \
            qqagent_host.resolve_host_root(explicit=None)
        if seed_origin is not None and qqagent_host.is_protected(seed_origin):
            info['seeded'] = _seed_from(seed_origin, host)
        info['host'] = str(host)
        return host, info, ''
    host = Path(args.work_dir).resolve() / 'host'
    source = resolved
    if qqagent_host.is_host_root(host):
        info['reusedHost'] = True
        info['host'] = str(host)
        return host, info, ''
    prepared = qqagent_host.ensure_host_root(target=host, source=source)
    info.update(copied=prepared['copied'], skipped=prepared['skipped'],
                smokeMs=(prepared.get('smoke') or {}).get('duration_ms', 0),
                host=str(host))
    if not prepared['ok']:
        return None, info, '宿主根制备失败：%s' % (prepared.get('error') or {})
    return host, info, ''


def _add_skills(host: Path, args) -> dict:
    """可选：把 0.4 的 skills/ 树也搬进宿主根（75MB 量级）。

    绝大多数扩展不需要它；bili-autopilot 这类 index.js 里 import '../../skills/<x>/...'
    的扩展要。默认不复制，免得把"宿主根约 4MB"这个前提搞坏。
    """
    source = Path(args.source).resolve() if args.source else qqagent_host.resolve_host_root(explicit=None)
    if source is None or not (source / SKILLS_DIRNAME).is_dir():
        return {'copied': 0, 'mb': 0.0, 'skipped': True}
    target = host / SKILLS_DIRNAME
    if target.is_dir() and any(target.iterdir()):
        return {'copied': 0, 'mb': 0.0, 'skipped': True}
    files = 0
    size = 0
    for item in (source / SKILLS_DIRNAME).rglob('*'):
        if not item.is_file():
            continue
        dest = target / item.relative_to(source / SKILLS_DIRNAME)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(item), str(dest))
        files += 1
        size += item.stat().st_size
    return {'copied': files, 'mb': round(size / 1048576, 1), 'skipped': False}


def _bucket(message: str) -> str:
    text = str(message or '')
    for needle, label in REASON_BUCKETS:
        if needle in text:
            return label
    if not text:
        return '无错误信息'
    return '其它：' + text.replace(chr(10), ' ')[:80]


def _run_one(zip_path: Path, host: Path, timeout: float, tiers: dict) -> dict:
    peek = qqagent_host.read_archive_manifest(zip_path)
    ident = peek.get('id') or zip_path.stem
    record = {'id': ident, 'zip': zip_path.name, 'tier': tiers.get(ident, '?'),
              'installed': 'reused', 'installedOk': True, 'probe_ok': False, 'tools': [],
              'toolCount': 0, 'error': '', 'errorCode': '', 'reason': '',
              'duration_ms': 0, 'install_ms': 0}
    folder = host / 'plugins' / ident
    if not folder.is_dir():
        started = time.perf_counter()
        install = qqagent_host.install_extension(zip_path, host)
        record['install_ms'] = int((time.perf_counter() - started) * 1000)
        if not install.get('ok'):
            record['installed'] = 'failed'
            record['installedOk'] = False
            record['error'] = str((install.get('error') or {}).get('message') or '')[:300]
            record['errorCode'] = str((install.get('error') or {}).get('code') or 'install-failed')
            record['reason'] = _bucket(record['error'])
            return record
        record['installed'] = 'installed'
    probe = qqagent_host.import_probe(folder, host, timeout=timeout)
    record['probe_ok'] = bool(probe.get('ok'))
    record['tools'] = probe.get('tools') or []
    record['toolCount'] = len(record['tools'])
    record['duration_ms'] = int(probe.get('duration_ms') or 0)
    record['hostImports'] = probe.get('hostImports') or []
    if not record['probe_ok']:
        error = probe.get('error') or {}
        record['error'] = str(error.get('message') or '')[:300]
        record['errorCode'] = str(error.get('code') or 'probe-failed')
        record['reason'] = _bucket(record['error'])
    return record


def _stats(items: list, elapsed: float) -> dict:
    installed = sum(1 for item in items if item['installed'] == 'installed')
    reused = sum(1 for item in items if item['installed'] == 'reused')
    install_failed = sum(1 for item in items if item['installed'] == 'failed')
    probe_ok = [item for item in items if item['probe_ok']]
    with_tools = [item for item in items if item['toolCount']]
    slowest = max(items, key=lambda item: item['duration_ms']) if items else {}
    per_tier: dict = {}
    for item in items:
        tier = per_tier.setdefault(item['tier'], {'total': 0, 'ok': 0, 'tools': 0,
                                                  'withTools': 0})
        tier['total'] += 1
        tier['ok'] += 1 if item['probe_ok'] else 0
        tier['tools'] += item['toolCount']
        tier['withTools'] += 1 if item['toolCount'] else 0
    reasons = Counter(item['reason'] for item in items if item['reason'])
    return {
        'selected': len(items), 'installedNow': installed, 'reusedInstalled': reused,
        'installFailed': install_failed,
        'probeOk': len(probe_ok), 'probeFailed': len(items) - len(probe_ok),
        'toolsTotal': sum(item['toolCount'] for item in items),
        'withTools': len(with_tools), 'withSchema': sum(
            1 for item in items for tool in item['tools'] if tool.get('hasSchema')),
        'perTier': dict(sorted(per_tier.items())),
        'reasons': reasons.most_common(10),
        'totalMs': int(elapsed * 1000),
        'avgMs': int(sum(item['duration_ms'] for item in items) / len(items)) if items else 0,
        'slowest': {'id': slowest.get('id', ''), 'ms': slowest.get('duration_ms', 0),
                    'error': slowest.get('error', '')[:120]},
    }


def _write_markdown(report: dict, path: Path) -> None:
    stats = report['stats']
    lines = ['# 市场扩展跑批报告（装进 0.4 宿主布局 + 真加载探测）', '',
             '- 生成时间：%s' % report['meta']['generatedAt'],
             '- 宿主根：`%s`（模式 %s）' % (report['host']['path'], report['host']['mode']),
             '- 复现命令：`%s`' % report['meta']['command'], '',
             '## 0 一页结论', '',
             '| 维度 | 数字 |', '|---|---|',
             '| 选中扩展 | %d |' % stats['selected'],
             '| 本次新装 | %d |' % stats['installedNow'],
             '| 复用已装 | %d |' % stats['reusedInstalled'],
             '| 装入失败 | %d |' % stats['installFailed'],
             '| import_probe 成功 | %d |' % stats['probeOk'],
             '| import_probe 失败 | %d |' % stats['probeFailed'],
             '| 枚举出工具的扩展 | %d |' % stats['withTools'],
             '| 工具总数 | %d |' % stats['toolsTotal'],
             '| 带 inputSchema 的工具 | %d |' % stats['withSchema'],
             '| 总耗时 | %.1fs |' % (stats['totalMs'] / 1000.0),
             '| 单次最长 | %dms（%s） |' % (stats['slowest']['ms'], stats['slowest']['id']), '',
             '## 1 分层（市场清单 P1..P6 口径）', '',
             '| 层 | 选中 | probe 成功 | 成功率 | 带工具 | 工具数 |', '|---|---|---|---|---|---|']
    for tier, row in stats['perTier'].items():
        rate = (row['ok'] / row['total'] * 100.0) if row['total'] else 0.0
        lines.append('| %s | %d | %d | %.0f%% | %d | %d |'
                     % (tier, row['total'], row['ok'], rate, row['withTools'], row['tools']))
    lines += ['', '## 2 失败原因 Top-N', '']
    if stats['reasons']:
        lines += ['| 原因 | 次数 |', '|---|---|']
        lines += ['| %s | %d |' % (reason, count) for reason, count in stats['reasons']]
    else:
        lines.append('无失败。')
    lines += ['', '## 3 工具明细（有工具的扩展）', '']
    detailed = [item for item in report['items'] if item['toolCount']]
    if detailed:
        lines += ['| 扩展 | 层 | 工具数 | 工具名 |', '|---|---|---|---|']
        for item in detailed:
            names = '、'.join(tool.get('name') or tool.get('id') or '' for tool in item['tools'])
            lines.append('| %s | %s | %d | %s |'
                         % (item['id'], item['tier'], item['toolCount'], names[:200]))
    else:
        lines.append('本次样本里没有扩展注册工具。')
    lines += ['', '## 4 逐条结果', '',
              '| # | id | 层 | 装入 | probe | 工具 | 耗时ms | 错误 |', '|---|---|---|---|---|---|---|---|']
    for index, item in enumerate(report['items'], 1):
        lines.append('| %d | %s | %s | %s | %s | %d | %d | %s |'
                     % (index, item['id'], item['tier'], item['installed'],
                        'ok' if item['probe_ok'] else 'fail', item['toolCount'],
                        item['duration_ms'], (item['error'] or '').replace('|', '\\|')[:120]))
    lines.append('')
    path.write_text(chr(10).join(lines), encoding='utf-8')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='市场扩展装进 0.4 宿主布局后真加载跑批')
    parser.add_argument('--limit', type=int, default=0, help='只跑前 N 个（分批用）')
    parser.add_argument('--ids', default='', help='只跑这些 id，逗号分隔')
    parser.add_argument('--host-root', default='', help='已存在的宿主根（缺省用 resolve_host_root）')
    parser.add_argument('--source', default='', help='--copy 时要复制的 0.4 源（缺省自动找）')
    parser.add_argument('--copy', action='store_true', help='先制备一份全新宿主根再全量装')
    parser.add_argument('--work-dir', default=str(WORK), help='--copy 的落点（默认 .tmp/sweep-work）')
    parser.add_argument('--timeout', type=float, default=20.0, help='单扩展加载超时秒数')
    parser.add_argument('--with-skills', action='store_true',
                        help='额外把 0.4 的 skills/ 搬进宿主根（+75MB，bili-autopilot 这类要）')
    parser.add_argument('--quiet', action='store_true', help='不打印逐条进度')
    args = parser.parse_args(argv)

    if not MARKET.is_dir():
        print('市场缓存不存在：%s' % MARKET, file=sys.stderr)
        return 2
    zips = _select(MARKET.glob('*.zip'), args.limit, args.ids.split(','))
    if not zips:
        print('没有选中任何扩展', file=sys.stderr)
        return 2
    host, info, error = _prepare_host(args)
    if error:
        print(error, file=sys.stderr)
        return 2
    if qqagent_host.is_protected(host):
        print('拒绝对只读基线跑批：%s' % host, file=sys.stderr)
        return 2
    if args.with_skills:
        info['skills'] = _add_skills(host, args)
    tiers = _read_tiers()
    print('host=%s mode=%s 选中 %d 个（seed %d / copied %d / skills %d）'
          % (host, info['mode'], len(zips), info.get('seeded', 0), info.get('copied', 0),
             (info.get('skills') or {}).get('copied', 0)))
    started = time.perf_counter()
    items = []
    for index, path in enumerate(zips, 1):
        record = _run_one(path, host, args.timeout, tiers)
        items.append(record)
        if not args.quiet:
            print('[%3d/%d] %-34s tier=%-14s probe=%s tools=%d %dms %s'
                  % (index, len(zips), record['id'], record['tier'],
                     'ok' if record['probe_ok'] else 'FAIL', record['toolCount'],
                     record['duration_ms'], record['error'][:70]),
                  flush=True)
    elapsed = time.perf_counter() - started
    report = {
        'meta': {
            'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'command': 'python tools/sweep_market_extensions.py ' + ' '.join(sys.argv[1:]),
            'market': str(MARKET), 'limit': args.limit, 'ids': args.ids,
            'timeout': args.timeout,
        },
        'host': {'path': str(host), 'mode': info['mode'], 'copied': info.get('copied', 0),
                 'skipped': info.get('skipped', 0), 'seeded': info.get('seeded', 0),
                 'smokeMs': info.get('smokeMs', 0), 'reuseFrom': info.get('reuseFrom', ''),
                 'skills': info.get('skills'), 'withSkills': bool(args.with_skills)},
        'items': items,
    }
    report['stats'] = _stats(items, elapsed)
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    _write_markdown(report, REPORT_MD)
    stats = report['stats']
    print('完成：装入 %d / 复用 %d / probe 成功 %d / 工具 %d / 总耗时 %.1fs'
          % (stats['installedNow'], stats['reusedInstalled'], stats['probeOk'],
             stats['toolsTotal'], elapsed))
    print('报告：%s' % REPORT_MD)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
