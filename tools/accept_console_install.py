#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""市场包导入控制台验收：真起 FastAPI、真下载/解压、真装进宿主根，全走真实 HTTP。

用法：
  .venv-qq/Scripts/python.exe tools/accept_console_install.py
  .venv-qq/Scripts/python.exe tools/accept_console_install.py --limit 2 --keep
退出码：0 全过 / 1 有失败 / 2 环境不可用（缺 node、缺清单、市场不可达等，不崩）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
# 别让控制台的"父进程消失就退出"看门狗把这次验收进程带走。
os.environ.pop('WHALEGIRL_PARENT_PID', None)

INV = ROOT / '.tmp' / 'inv'
ACCEPT = ROOT / '.tmp' / 'accept'
SWEEP = INV / 'sweep-report.json'
PLUGIN_TOOLS = INV / 'plugin-tools.json'
SKILL_TOOLS = INV / 'skill-tools.json'
SKILL_MARKET = Path(r'D:\聊天机器人\插件市场\Skill市场')
MD_OUT = INV / 'console-accept.md'
JSON_OUT = ACCEPT / 'console-accept.json'
WORK = ACCEPT / 'work'

#: 清单里的兼容层级（P*，静态兼容判定）→ 控制台 registry 的层级（T*，启用门禁口径）。
TIER_MAP = {
    'P1-usable': 'T1-ready',
    'P2-web-fetch': 'T2-web-fetch',
    'P3-host-source': 'T4-host-source',
    'P4-depends-other': 'T5-depends-other',
    'P5-electron': 'T6-disabled',
    'P6-declarative': 'T1-ready',
}
#: 控制台允许自动启用的唯一口径（src/qq_app.py:2532 _EXTENSION_BLOCKED_TIERS）。
BLOCKED_TIERS = ('T4-host-source', 'T6-disabled')
#: 对应到 P* 的"装上也不该自动启用"。
BLOCKED_P = ('P3-host-source', 'P5-electron')
#: P2/P4 的宿主侧清单不携带 external/depends，registry 只能看到包里声明的，故两者都算合规。
TIER_ALSO_OK = {'P2-web-fetch': ('T1-ready',), 'P4-depends-other': ('T1-ready',)}

PLUGIN_PLAN = (('P1-usable', 20), ('P3-host-source', 30), ('P2-web-fetch', 3),
               ('P4-depends-other', 3), ('P6-declarative', 3))
#: 两个已知边界：探针预期失败，用来证明控制台"装上但不吹可用"。
BOUNDARY = (('long-image-reader', 'P5-electron', '缺 electron（P5-electron）'),
            ('bili-autopilot', 'P3-host-source', '缺宿主根 skills/ 树'))
SKILL_PLAN = (('T1-ready', 5), ('T2-web-fetch', 7), ('T3-extra-api', 1), ('T5-depends-other', 2))
SKILL_ALWAYS = ('wiki-query',)          # T1，清单自带的最小可跑示例
SKILL_T4_COUNT = 3
SKILL_T6_COUNT = 1


def _now() -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S')


def _pct(values, q):
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return float(ordered[low] + (ordered[high] - ordered[low]) * (pos - low))


def _stats(values):
    if not values:
        return {'n': 0, 'p50': 0.0, 'p90': 0.0, 'max': 0.0, 'avg': 0.0}
    return {'n': len(values), 'p50': round(_pct(values, 0.5), 1),
            'p90': round(_pct(values, 0.9), 1), 'max': round(max(values), 1),
            'avg': round(statistics.fmean(values), 1)}


def _snapshot(base: Path) -> dict:
    """目录清单（相对路径 → 字节数）；不存在返回空，不抛。"""
    if not base.is_dir():
        return {}
    result = {}
    for path in base.rglob('*'):
        if not path.is_file():
            continue
        rel = path.relative_to(base)
        if rel.parts and rel.parts[0] == 'logs':
            continue          # 日志是追加写的诊断通道，跑一次就变，不算安装副作用
        try:
            result[str(rel)] = path.stat().st_size
        except OSError:
            result[str(rel)] = -1
    return result


def _json_body(response) -> dict:
    try:
        data = response.json()
    except Exception:
        return {'_raw': (response.text or '')[:500]}
    return data if isinstance(data, dict) else {'_raw': str(data)[:500]}


def _market_zip_index(directory: Path) -> dict:
    """skill id → zip。口径照抄 src/skill_tools.py:615 _market_index：只读包内清单，不猜文件名。"""
    from src import skill_tools
    if not directory.is_dir():
        return {}
    return dict(skill_tools._market_index(directory))


def _looks_network(text: str) -> bool:
    """把网络层抖动（超时/证书/DNS/连接）与产品缺陷区分开。"""
    hay = str(text or '').lower()
    return any(key in hay for key in ('网络', '超时', 'timeout', 'timed out', 'connect',
                                      '证书', 'ssl', 'getaddrinfo', 'temporary failure'))


async def _post(console, path: str, payload: dict, args) -> tuple:
    """POST 一次，网络层失败按 --retries 重试；返回 (status, body, ms, attempts, kind)。"""
    last_error = '没有尝试'
    last_ms = 0
    for attempt in range(max(0, args.retries) + 1):
        started = time.perf_counter()
        try:
            status, body, ms = await console.post(path, payload, timeout=args.install_timeout)
        except Exception as exc:
            last_error = '%s: %s' % (type(exc).__name__, exc)
            last_ms = int((time.perf_counter() - started) * 1000)
            continue
        message = str(body.get('detail') or body.get('error') or '')
        if status == 200 and body.get('ok'):
            return status, body, ms, attempt + 1, ''
        if _looks_network(message) and attempt < args.retries:
            last_error = message
            last_ms = ms
            continue
        return status, body, ms, attempt + 1, 'network' if _looks_network(message) else ''
    return 0, {'detail': last_error}, last_ms, max(0, args.retries) + 1, 'network'


class Console:
    """进程内真 uvicorn（127.0.0.1 随机端口）+ 真 socket 的 httpx 客户端。"""

    def __init__(self, root: Path, timeout: float):
        self.root = root
        self.timeout = timeout
        self.base = ''
        self.server = None
        self.bot = None
        self.client = None
        self._task = None

    async def start(self):
        import httpx
        import uvicorn
        from src.qq_app import QQRuntime, create_app

        (self.root / 'styles' / 'custom').mkdir(parents=True, exist_ok=True)
        (self.root / 'styles' / 'default.yaml').write_text('name: default\n', encoding='utf-8')
        self.bot = QQRuntime(root=self.root, config_path=self.root / 'config.qq.yaml',
                             db_path=self.root / 'accept.sqlite3')
        app = create_app(self.bot)
        config = uvicorn.Config(app, host='127.0.0.1', port=0, log_level='error',
                                log_config=None, lifespan='on')
        self.server = uvicorn.Server(config)
        self._task = asyncio.create_task(self.server.serve())
        for _ in range(400):
            await asyncio.sleep(0.05)
            if getattr(self.server, 'started', False):
                break
        else:
            raise RuntimeError('uvicorn 没起来')
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.base = 'http://127.0.0.1:%d' % port
        self.client = httpx.AsyncClient(base_url=self.base, timeout=self.timeout)

    async def get(self, path, timeout=None):
        started = time.perf_counter()
        response = await self.client.get(path, timeout=timeout or self.timeout)
        return response.status_code, _json_body(response), int((time.perf_counter() - started) * 1000)

    async def post(self, path, payload, timeout=None):
        started = time.perf_counter()
        response = await self.client.post(path, json=payload, timeout=timeout or self.timeout)
        return response.status_code, _json_body(response), int((time.perf_counter() - started) * 1000)

    async def stop(self):
        if self.client is not None:
            await self.client.aclose()
        if self.server is not None:
            self.server.should_exit = True
            try:
                await asyncio.wait_for(self._task, timeout=20)
            except Exception:
                pass


def _pick(pool, count: int, rng) -> list:
    ordered = sorted(pool)
    if count >= len(ordered):
        return ordered
    return sorted(rng.sample(ordered, count))


def build_plugin_cases(limit: int, seed: int) -> tuple:
    sweep = json.loads(SWEEP.read_text(encoding='utf-8'))
    tools = json.loads(PLUGIN_TOOLS.read_text(encoding='utf-8'))
    oracle = {str(item['id']): item for item in sweep.get('items') or []}
    tiers = {str(item['id']): str(item.get('tier') or '') for item in tools.get('plugins') or []}
    pools = {}
    for ident, tier in tiers.items():
        if ident in oracle and tier:
            pools.setdefault(tier, set()).add(ident)
    boundary_ids = {ident for ident, _tier, _why in BOUNDARY}
    rng = random.Random(seed)
    cases, notes = [], []
    for tier, count in PLUGIN_PLAN:
        want = count if limit <= 0 else min(count, limit)
        candidates = pools.get(tier, set()) - boundary_ids
        for ident in _pick(candidates, want, rng):
            cases.append({'kind': 'plugin', 'id': ident, 'class': tier,
                          'expect_probe': bool(oracle[ident].get('probe_ok')),
                          'expect_tier': TIER_MAP[tier], 'boundary': '',
                          'ref': oracle[ident].get('zip') or ''})
    for ident, tier, why in BOUNDARY:
        if ident not in oracle:
            notes.append('边界样例 %s 不在 sweep 清单里，已跳过' % ident)
            continue
        cases.append({'kind': 'plugin', 'id': ident, 'class': tier,
                      'expect_probe': bool(oracle[ident].get('probe_ok')),
                      'expect_tier': TIER_MAP[tier], 'boundary': why,
                      'ref': oracle[ident].get('zip') or ''})
    return cases, notes


def build_skill_cases(limit: int, seed: int) -> tuple:
    skills = json.loads(SKILL_TOOLS.read_text(encoding='utf-8')).get('skills') or []
    tiers = {str(item['id']): str(item.get('tier') or '') for item in skills}
    zips = _market_zip_index(SKILL_MARKET)
    pools = {}
    for ident, tier in tiers.items():
        if ident in zips and tier:
            pools.setdefault(tier, set()).add(ident)
    rng = random.Random(seed + 1)
    chosen, notes = [], []
    for ident in SKILL_ALWAYS:
        if ident in tiers:
            chosen.append(ident)
        else:
            notes.append('必抽样例 %s 不在 skill 清单里' % ident)
    plan = list(SKILL_PLAN) + [('T4-host-source', SKILL_T4_COUNT), ('T6-disabled', SKILL_T6_COUNT)]
    for tier, count in plan:
        want = count if limit <= 0 else min(count, limit)
        picked = _pick(pools.get(tier, set()) - set(chosen), want, rng)
        if len(picked) < want:
            notes.append('%s 可抽包只有 %d 个（计划 %d）' % (tier, len(picked), want))
        chosen.extend(picked)
    cases = []
    for ident in chosen:
        cases.append({'kind': 'skill', 'id': ident, 'class': tiers.get(ident, ''),
                      'zip': str(zips.get(ident, '')), 'expect_tier': tiers.get(ident, '')})
    return cases, notes


async def _run(args) -> int:
    environment = {'python': sys.version.split()[0], 'startedAt': _now()}
    problems = []
    for name, path in (('sweep-report.json', SWEEP), ('plugin-tools.json', PLUGIN_TOOLS),
                       ('skill-tools.json', SKILL_TOOLS)):
        if not path.is_file():
            problems.append('缺清单 %s' % path)
    if not SKILL_MARKET.is_dir():
        problems.append('缺 skill 市场目录 %s' % SKILL_MARKET)
    try:
        import httpx  # noqa: F401
        import uvicorn  # noqa: F401
        from fastapi import FastAPI  # noqa: F401
    except Exception as exc:
        problems.append('依赖缺失：%s' % exc)
    try:
        from src import qqagent_host
        environment['node'] = qqagent_host.node_binary() or ''
        if not environment['node']:
            problems.append('没找到 node，宿主探测跑不起来')
        environment['runtimeSource'] = str((qqagent_host.runtime_source_candidates() or [''])[0])
    except Exception as exc:
        problems.append('宿主运行时模块不可用：%s' % exc)
    if problems:
        print('环境不可用：')
        for line in problems:
            print('  - %s' % line)
        JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
        JSON_OUT.write_text(json.dumps({'ok': False, 'environment': environment,
                                        'problems': problems}, ensure_ascii=False, indent=2),
                            encoding='utf-8')
        return 2

    plugin_cases, notes_a = build_plugin_cases(args.limit, args.seed)
    skill_cases, notes_b = build_skill_cases(args.limit, args.seed)
    if args.plugins_only:
        skill_cases = []
    if args.skills_only:
        plugin_cases = []

    if WORK.exists() and not args.keep:
        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    data_root = WORK / 'data-root'
    if data_root.exists():
        shutil.rmtree(data_root, ignore_errors=True)
    data_root.mkdir(parents=True)
    # 控制台自己启动时会产出这份 skill 清单快照；验收用同一份快照，层级才与清单同源。
    snapshot_dir = data_root / 'data' / 'qq-only'
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SKILL_TOOLS, snapshot_dir / 'skill-tools.json')

    repo_data = ROOT / 'data' / 'qq-only'
    user_data = Path(os.environ.get('LOCALAPPDATA') or '') / 'WhaleGirlQQ' / 'data' / 'qq-only'
    before_repo = _snapshot(repo_data)
    before_user = _snapshot(user_data)

    console = Console(data_root, args.timeout)
    records = []
    host_first = {}
    host_last = {}
    samples = {}
    try:
        await console.start()
        environment['baseUrl'] = console.base
        environment['dataRoot'] = str(data_root)
        status, host_first, ms = await console.get('/api/extensions/host', timeout=args.short)
        environment['hostGetMs'] = ms
        if status != 200:
            raise RuntimeError('GET /api/extensions/host 返回 %s' % status)

        for case in plugin_cases:
            record = await _plugin_case(console, case, args)
            records.append(record)
            if case['class'] == 'P1-usable' and record['ok'] and 'p1' not in samples:
                samples['p1'] = {'id': record['id'], 'response': record['response']}
            if case['class'] == 'P3-host-source' and record['ok'] and 'p3' not in samples:
                samples['p3'] = {'id': record['id'], 'response': record['response']}

        for case in skill_cases:
            record = await _skill_case(console, case, args)
            records.append(record)
            if case['class'] == 'T4-host-source' and record['ok'] and 't4' not in samples:
                samples['t4'] = {'id': record['id'], 'response': record['response']}

        status, host_last, ms = await console.get('/api/extensions/host', timeout=args.short)
        environment['hostGetLastMs'] = ms
    except Exception as exc:
        records.append({'kind': 'env', 'id': '(启动/收尾)', 'class': 'env', 'ok': False,
                        'failures': ['exception: %s: %s' % (type(exc).__name__, exc)],
                        'duration_ms': 0, 'expected_usable': True})
    finally:
        try:
            await console.stop()
        except Exception:
            pass

    after_repo = _snapshot(repo_data)
    after_user = _snapshot(user_data)
    repo_diff = _diff(before_repo, after_repo)
    user_diff = _diff(before_user, after_user)

    plugin_records = [r for r in records if r['kind'] == 'plugin']
    skill_records = [r for r in records if r['kind'] == 'skill']
    plugin_ok = sum(1 for r in plugin_records if r['ok'])
    skill_ok = sum(1 for r in skill_records if r['ok'])
    # 失败三分类：真缺陷（断言/落位不一致）/ 网络抖动（重试后仍失败）/ 预期内不可用（P4/P5/边界）。
    for record in records:
        if record['ok']:
            continue
        if not record.get('expected_usable'):
            record['failureClass'] = 'expected'
        elif record.get('failureClass') != 'network':
            record['failureClass'] = 'defect'
    failures = [r for r in records if not r['ok']]
    defects = [r for r in failures if r.get('failureClass') == 'defect']
    network_failures = [r for r in failures if r.get('failureClass') == 'network']
    expected_failures = [r for r in failures if r.get('failureClass') == 'expected']
    repo_touched = bool(repo_diff['added'] or repo_diff['removed'] or repo_diff['changed'])

    payload = {
        'ok': not failures and not repo_touched and bool(records),
        'generatedAt': _now(),
        'environment': environment,
        'plan': {'plugins': len(plugin_cases), 'skills': len(skill_cases),
                 'seed': args.seed, 'limit': args.limit, 'retries': args.retries,
                 'notes': notes_a + notes_b,
                 'reuse': '全部用例共用同一份临时 data_root；宿主根只准备一次，扩展按 id 各占一个目录'},
        'hostGetFirst': host_first,
        'hostGetLast': host_last,
        'samples': samples,
        'counts': {'plugins': [plugin_ok, len(plugin_records)],
                   'skills': [skill_ok, len(skill_records)]},
        'failures': failures,
        'defects': defects,
        'networkFailures': network_failures,
        'expectedFailures': expected_failures,
        'unusable': [{'id': r['id'], 'class': r.get('class', ''), 'tier': r.get('tier', ''),
                      'enabled': r.get('enabled'), 'probeError': r.get('probeError', ''),
                      'boundary': r.get('boundary', '')}
                     for r in plugin_records if r.get('probeOk') is False],
        'dataDirCheck': {'repoBefore': len(before_repo), 'repoAfter': len(after_repo),
                         'repoDiff': repo_diff, 'userBefore': len(before_user),
                         'userAfter': len(after_user), 'userDiff': user_diff},
        'durations': _duration_view(records),
        'records': records,
        'workDir': str(WORK),
    }
    ACCEPT.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    MD_OUT.parent.mkdir(parents=True, exist_ok=True)
    MD_OUT.write_text(_render_markdown(payload), encoding='utf-8')

    print('plugins %d/%d, skills %d/%d' % (plugin_ok, len(plugin_records),
                                           skill_ok, len(skill_records)))
    for record in records:
        if not record['ok']:
            print('  FAIL %-6s %-34s %s' % (record['kind'], record['id'],
                                            '; '.join(record['failures'])[:200]))
    if repo_touched:
        print('  仓库 data/qq-only 被动过：%s' % json.dumps(repo_diff, ensure_ascii=False)[:400])
    print('报告 %s（%d 行）+ %s'
          % (MD_OUT.relative_to(ROOT), len(MD_OUT.read_text(encoding='utf-8').splitlines()),
             JSON_OUT.relative_to(ROOT)))
    print('失败分类：真缺陷 %d，网络抖动 %d，预期内不可用 %d'
          % (len(defects), len(network_failures), len(expected_failures)))
    if not records:
        return 2
    return 0 if not failures and not repo_touched else 1


def _diff(before: dict, after: dict) -> dict:
    return {'added': sorted(set(after) - set(before))[:40],
            'removed': sorted(set(before) - set(after))[:40],
            'changed': sorted(k for k in set(before) & set(after) if before[k] != after[k])[:40]}


async def _plugin_case(console, case: dict, args) -> dict:
    ident = case['id']
    tier_class = case['class']
    record = {'kind': 'plugin', 'id': ident, 'class': tier_class,
              'boundary': case['boundary'], 'expectProbe': case['expect_probe'],
              'expectTier': case['expect_tier'],
              'expected_usable': not (tier_class in ('P4-depends-other', 'P5-electron')
                                      or bool(case['boundary'])),
              'duration_ms': 0, 'failures': [], 'ok': False}
    payload = {'id': ident, 'confirmed': True, 'enable': True}
    started = time.perf_counter()
    status, body, ms, attempts, kind = await _post(console, '/api/extensions/install',
                                                  payload, args)
    record['duration_ms'] = int((time.perf_counter() - started) * 1000)
    record['httpStatus'] = status
    record['requestMs'] = ms
    record['attempts'] = attempts
    record['failureClass'] = kind
    record['response'] = {key: body.get(key) for key in
                          ('success', 'ok', 'enabled', 'hostRoot', 'message', 'probe',
                           'error', 'detail') if key in body}
    item = body.get('item') if isinstance(body.get('item'), dict) else {}
    if item:
        record['response']['item'] = {key: item.get(key) for key in
                                      ('id', 'tier', 'enabled', 'note') if key in item}
    if status != 200 or not body.get('ok'):
        record['failures'].append('http %s %s' % (status, str(body.get('detail')
                                                              or body.get('error') or '')[:200]))
        return record
    host_root = Path(str(body.get('hostRoot') or ''))
    folder = host_root / 'plugins' / ident
    record['hostRoot'] = str(host_root)
    if not host_root.is_dir():
        record['failures'].append('hostRoot 不存在：%s' % host_root)
    if not folder.is_dir():
        record['failures'].append('宿主根里没有 plugins/%s' % ident)
    # 每一例都重新问一次宿主状态（ready 必须是真读来的，不是缓存的布尔）。
    try:
        status2, host_body, ms2 = await console.get('/api/extensions/host', timeout=args.short)
    except Exception as exc:
        record['failures'].append('host get: %s: %s' % (type(exc).__name__, exc))
        host_body, status2, ms2 = {}, 0, 0
    record['hostGet'] = {'status': status2, 'ok': bool(host_body.get('ok')),
                         'prepared': bool(host_body.get('prepared')),
                         'root': str(host_body.get('root') or ''), 'ms': ms2,
                         'missing': host_body.get('missing') or []}
    if status2 != 200 or not host_body.get('ok'):
        record['failures'].append('GET host 没就绪（ok=%s prepared=%s）'
                                  % (host_body.get('ok'), host_body.get('prepared')))
    elif str(host_body.get('root') or '') != str(host_root):
        record['failures'].append('GET host 的 root 与本次 hostRoot 不一致')
    probe = body.get('probe') if isinstance(body.get('probe'), dict) else {}
    record['probeOk'] = bool(probe.get('ok'))
    host = body.get('host') if isinstance(body.get('host'), dict) else {}
    if host.get('id') and str(host.get('id')) != ident:
        record['failures'].append('宿主落位 id 不一致：%s' % host.get('id'))
    error = probe.get('error') if isinstance(probe.get('error'), dict) else {}
    record['probeError'] = str(error.get('message') or probe.get('loadError') or '')[:300]
    if record['probeOk'] != bool(case['expect_probe']):
        detail = ''
        detail = str(error.get('message') or probe.get('loadError') or '')[:160]
        record['failures'].append('probe.ok=%s 与清单 %s 不一致（%s）'
                                  % (record['probeOk'], case['expect_probe'], detail))
    # 安装响应的 item 里没有 tier（src/qq_app.py:2535 只回 id/enabled/note），
    # tier 是 registry 的视图，必须另问 GET /api/extensions。
    registry = {}
    try:
        status3, listing, ms3 = await console.get('/api/extensions?q=%s' % ident, timeout=args.short)
        registry = next((one for one in (listing.get('items') or [])
                         if isinstance(one, dict) and str(one.get('id')) == ident), {})
    except Exception as exc:
        record['failures'].append('extensions list: %s: %s' % (type(exc).__name__, exc))
    record['registry'] = {key: registry.get(key) for key in
                          ('id', 'tier', 'enabled', 'note') if key in registry}
    if not registry:
        record['failures'].append('GET /api/extensions?q=%s 里没有这条' % ident)
    actual_tier = str(registry.get('tier') or '')
    record['tier'] = actual_tier
    allowed = {case['expect_tier']} | set(TIER_ALSO_OK.get(tier_class, ()))
    if actual_tier not in allowed:
        record['failures'].append('tier=%s 不在允许集 %s' % (actual_tier, sorted(allowed)))
    elif actual_tier != case['expect_tier']:
        record.setdefault('observations', []).append(
            'tier=%s（清单口径 %s：宿主侧清单不携带该维度）' % (actual_tier, case['expect_tier']))
    enabled = bool(body.get('enabled'))
    record['enabled'] = enabled
    record['note'] = str(item.get('note') or registry.get('note') or '')
    if 'enabled' in registry and bool(registry.get('enabled')) != enabled:
        record['failures'].append('安装响应 enabled=%s 与 registry 列表 %s 不一致'
                                  % (enabled, registry.get('enabled')))
    expect_enabled = tier_class not in BLOCKED_P
    if enabled != expect_enabled:
        record['failures'].append('enabled=%s 与层级规则（应为 %s）不一致'
                                  % (enabled, expect_enabled))
    if not expect_enabled:
        message = str(body.get('message') or '')
        if '未启用' not in message and '未启用' not in record['note']:
            record['failures'].append('未启用但缺中文 note/message')
    record['ok'] = not record['failures']
    return record


async def _skill_case(console, case: dict, args) -> dict:
    ident = case['id']
    record = {'kind': 'skill', 'id': ident, 'class': case['class'],
              'expectTier': case['expect_tier'], 'zip': case['zip'],
              'expected_usable': True, 'duration_ms': 0, 'failures': [], 'ok': False}
    if not Path(case['zip']).is_file():
        record['failures'].append('市场 zip 不存在：%s' % case['zip'])
        return record
    payload = {'zipPath': case['zip'], 'enable': True}
    started = time.perf_counter()
    status, body, ms, attempts, kind = await _post(console, '/api/skills/install', payload, args)
    record['duration_ms'] = int((time.perf_counter() - started) * 1000)
    record['httpStatus'] = status
    record['requestMs'] = ms
    record['attempts'] = attempts
    record['failureClass'] = kind
    record['response'] = {key: body.get(key) for key in
                          ('ok', 'id', 'tier', 'enabled', 'note', 'files', 'bytes', 'error')
                          if key in body}
    if status != 200 or not body.get('ok'):
        record['failures'].append('http %s %s' % (status, str(body.get('error')
                                                             or body.get('detail') or '')[:200]))
        return record
    installed_id = str(body.get('id') or '')
    record['installedId'] = installed_id
    if installed_id != ident:
        record['failures'].append('落地 id=%s 与清单 %s 不一致' % (installed_id, ident))
    tier = str(body.get('tier') or '')
    record['tier'] = tier
    if tier != case['expect_tier']:
        record['failures'].append('tier=%s 与清单 %s 不一致' % (tier, case['expect_tier']))
    enabled = bool(body.get('enabled'))
    record['enabled'] = enabled
    record['note'] = str(body.get('note') or '')
    expect_enabled = tier not in BLOCKED_TIERS
    if enabled != expect_enabled:
        record['failures'].append('enabled=%s 与规则（应为 %s）不一致' % (enabled, expect_enabled))
    if not expect_enabled and '未启用' not in record['note']:
        record['failures'].append('未启用但 note 不是中文原因：%s' % record['note'][:120])
    try:
        status2, listing, ms2 = await console.post('/api/skills/installed', {},
                                                   timeout=args.short)
    except Exception as exc:
        record['failures'].append('installed: %s: %s' % (type(exc).__name__, exc))
        status2, listing, ms2 = 0, {}, 0
    item = next((one for one in (listing.get('items') or [])
                 if isinstance(one, dict) and str(one.get('id')) == installed_id), None)
    record['installedListing'] = {'status': status2, 'count': listing.get('count'), 'ms': ms2,
                                  'found': bool(item), 'tier': (item or {}).get('tier'),
                                  'enabled': (item or {}).get('enabled'),
                                  'blocked': (item or {}).get('blocked')}
    if status2 != 200 or not listing.get('ok'):
        record['failures'].append('POST /api/skills/installed 失败：%s' % status2)
    elif item is None:
        record['failures'].append('落盘复核没找到 %s' % installed_id)
    else:
        if str(item.get('tier') or '') != tier:
            record['failures'].append('复核 tier=%s 与安装响应 %s 不一致'
                                      % (item.get('tier'), tier))
        if bool(item.get('enabled')) != enabled:
            record['failures'].append('复核 enabled=%s 与安装响应 %s 不一致'
                                      % (item.get('enabled'), enabled))
    skill_dir = Path(str(listing.get('root') or '')) / installed_id
    record['skillDir'] = str(skill_dir)
    if not skill_dir.is_dir():
        record['failures'].append('落盘目录不存在：%s' % skill_dir)
    record['ok'] = not record['failures']
    return record


def _duration_view(records) -> dict:
    buckets = {}
    for record in records:
        key = record.get('class') or record['kind']
        buckets.setdefault(key, []).append(record.get('duration_ms') or 0)
    host = [r['hostGet']['ms'] for r in records if isinstance(r.get('hostGet'), dict)]
    view = {key: _stats(values) for key, values in sorted(buckets.items())}
    view['GET /api/extensions/host'] = _stats(host)
    return view


def _render_markdown(payload: dict) -> str:
    plugins_ok, plugins_all = payload['counts']['plugins']
    skills_ok, skills_all = payload['counts']['skills']
    check = payload['dataDirCheck']
    repo_touched = bool(check['repoDiff']['added'] or check['repoDiff']['removed']
                        or check['repoDiff']['changed'])
    lines = []
    add = lines.append
    add('# 市场包导入控制台验收（真 HTTP）')
    add('')
    add('- 生成时间：%s' % payload['generatedAt'])
    total_ok = plugins_ok + skills_ok
    total_all = plugins_all + skills_all
    add('- 失败分类：真缺陷 %d、网络抖动 %d、预期内不可用 %d'
        % (len(payload['defects']), len(payload['networkFailures']),
           len(payload['expectedFailures'])))
    add('- 结论：**%s**；插件 %d/%d，skill %d/%d，合计 %d/%d（通过率 %.1f%%），真缺陷 %d 条，仓库 data/qq-only %s'
        % ('通过' if payload['ok'] else '未通过', plugins_ok, plugins_all, skills_ok, skills_all,
           total_ok, total_all, (100.0 * total_ok / total_all) if total_all else 0.0,
           len(payload['defects']), '被改动' if repo_touched else '未被改动'))
    env = payload['environment']
    add('- 起应用方式：进程内真 uvicorn（%s）+ httpx 真 socket，不是 TestClient'
        % env.get('baseUrl', ''))
    add('- 临时 data_root：%s（绝不写用户真实数据目录）' % env.get('dataRoot', ''))
    add('- 宿主运行时来源：%s；node：%s' % (env.get('runtimeSource', ''), env.get('node', '')))
    add('- 抽样种子 %d，--limit %d；%s'
        % (payload['plan']['seed'], payload['plan']['limit'], payload['plan']['reuse']))
    for note in payload['plan']['notes']:
        add('- 抽样说明：%s' % note)
    add('')
    add('## GET /api/extensions/host 真实响应')
    add('')
    first = payload.get('hostGetFirst') or {}
    last = payload.get('hostGetLast') or {}
    add('~~~json')
    add(json.dumps({key: first.get(key) for key in
                    ('success', 'ok', 'prepared', 'root', 'exists', 'files', 'bytes',
                     'plugins', 'script', 'node', 'missing', 'message') if key in first},
                   ensure_ascii=False, indent=2)[:1400])
    add('~~~')
    add('')
    add('（首次调用时宿主根还没 materialize，ok=false/prepared=false 是预期：装第一个扩展时才自动准备。）')
    add('')
    add('收尾时同一端点：ok=%s prepared=%s files=%s root=%s'
        % (last.get('ok'), last.get('prepared'), last.get('files'), last.get('root', '')))
    add('')
    add('## 插件层逐例')
    add('')
    add('| id | 期望tier | 实际tier | probe.ok | 期望probe | enabled | 失败原因 |')
    add('| --- | --- | --- | --- | --- | --- | --- |')
    for record in [r for r in payload['records'] if r['kind'] == 'plugin']:
        add('| %s%s | %s | %s | %s | %s | %s | %s |'
            % (record['id'], '（边界）' if record.get('boundary') else '',
               record.get('expectTier', ''), record.get('tier', '') or '-',
               record.get('probeOk'), record.get('expectProbe'),
               record.get('enabled'), '；'.join(record['failures'])[:180] or '—'))
    add('')
    add('## Skill 层逐例')
    add('')
    add('| id | 期望tier | 实际tier | enabled | note | 复核 | 失败原因 |')
    add('| --- | --- | --- | --- | --- | --- | --- |')
    for record in [r for r in payload['records'] if r['kind'] == 'skill']:
        listing = record.get('installedListing') or {}
        add('| %s | %s | %s | %s | %s | %s | %s |'
            % (record['id'], record.get('expectTier', ''), record.get('tier', '') or '-',
               record.get('enabled'), (record.get('note') or '')[:60],
               'found' if listing.get('found') else 'missing',
               '；'.join(record['failures'])[:180] or '—'))
    add('')
    add('## 耗时（毫秒）')
    add('')
    add('| 类别 | n | p50 | p90 | max | avg |')
    add('| --- | --- | --- | --- | --- | --- |')
    for key, stats in payload['durations'].items():
        add('| %s | %d | %.1f | %.1f | %.1f | %.1f |'
            % (key, stats['n'], stats['p50'], stats['p90'], stats['max'], stats['avg']))
    add('')
    add('## 失败归因')
    add('')
    add('失败共 %d 例：真缺陷 %d、网络抖动 %d、预期内不可用 %d。'
        % (len(payload['failures']), len(payload['defects']),
           len(payload['networkFailures']), len(payload['expectedFailures'])))
    add('')
    add('### 真缺陷（%d）' % len(payload['defects']))
    add('')
    if not payload['defects']:
        add('无。')
    for record in payload['defects']:
        add('- %s（%s，%s）：%s' % (record['id'], record['kind'], record['class'],
                                   '；'.join(record['failures'])))
    add('')
    add('### 网络抖动（重试后仍失败，%d）' % len(payload['networkFailures']))
    add('')
    if not payload['networkFailures']:
        add('无。')
    for record in payload['networkFailures']:
        add('- %s（%s，尝试 %s 次）：%s'
            % (record['id'], record['kind'], record.get('attempts'),
               '；'.join(record['failures'])))
    add('')
    add('### 预期内不可用（%d）' % len(payload['expectedFailures']))
    add('')
    if not payload['expectedFailures']:
        add('无。')
    for record in payload['expectedFailures']:
        add('- %s（%s%s）：%s'
            % (record['id'], record['class'],
               '，边界：%s' % record['boundary'] if record.get('boundary') else '',
               '；'.join(record['failures'])))
    add('')
    add('### 观察（不算失败）')
    add('')
    observed = [r for r in payload['records'] if r.get('observations')]
    if not observed:
        add('无。')
    for record in observed:
        add('- %s：%s' % (record['id'], '；'.join(record['observations'])))
    add('')
    add('## 口径与已知判断（不是缺陷）')
    add('')
    add('- tier 取自 GET /api/extensions?q=id（registry 视图）；安装响应 item 只有 id/enabled/note，没有 tier。')
    add('- 插件层清单的 P*（兼容判定）与 registry 的 T*（启用门禁）是两套词汇，映射见脚本 TIER_MAP。')
    add('- P2-web-fetch / P4-depends-other 的宿主侧 plugin.json 不携带 external/depends，registry 只能落 T1-ready；'
        '这是包内信息丢失，不是安装失败，已按允许集放行并逐例标注。')
    add('- 宿主根实际是 <data_root>/data/qq-only/qq-only/qqagent_host（双层 qq-only：'
        'bot.plugins.data_dir 已是 data/qq-only，default_host_root 再拼一次 qq-only）。'
        'app 内所有调用方同口径，内部自洽，只记为命名观察。')
    add('- 可用性口径是宿主 import_probe 的 probe.ok，而不是安装返回的 ok。')
    add('')
    add('## 装上但不可用（probe.ok=false，%d 例）' % len(payload['unusable']))
    add('')
    add('ok=true 只代表安装流程成功，**不等于可用**；可用性以宿主 import_probe 为准。')
    add('')
    if not payload['unusable']:
        add('无：本轮抽到的插件探针全过。')
    else:
        add('| id | 兼容层级 | 实际tier | enabled | 探针错误 |')
        add('| --- | --- | --- | --- | --- |')
        for one in payload['unusable']:
            add('| %s%s | %s | %s | %s | %s |'
                % (one['id'], '（边界）' if one['boundary'] else '', one['class'],
                   one['tier'], one['enabled'], (one['probeError'] or '-')[:160]))
    add('')
    add('## data/qq-only 起止清单')
    add('')
    add('- 仓库 data/qq-only：%d → %d 个文件，新增 %d、消失 %d、变化 %d'
        % (check['repoBefore'], check['repoAfter'], len(check['repoDiff']['added']),
           len(check['repoDiff']['removed']), len(check['repoDiff']['changed'])))
    add('- 用户真实数据目录：%d → %d 个文件，新增 %d、消失 %d、变化 %d'
        % (check['userBefore'], check['userAfter'], len(check['userDiff']['added']),
           len(check['userDiff']['removed']), len(check['userDiff']['changed'])))
    if repo_touched:
        add('- 明细：%s' % json.dumps(check['repoDiff'], ensure_ascii=False)[:600])
    add('')
    add('## 三类真实响应片段')
    add('')
    for key, title in (('p1', 'P1-usable 安装成功'), ('p3', 'P3-host-source 安装成功（被拦层级）'),
                       ('t4', 'T4-host-source skill 被挡住')):
        sample = payload['samples'].get(key)
        add('### %s' % title)
        add('')
        if not sample:
            add('本次抽样里没有可用样例。')
            add('')
            continue
        add('%s：' % sample['id'])
        add('')
        add('~~~json')
        add(json.dumps(sample['response'], ensure_ascii=False, indent=2)[:1200])
        add('~~~')
        add('')
    add('## 复现')
    add('')
    add('~~~')
    add('# 全量（插件 61 + skill 20）')
    add('PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 .venv-qq/Scripts/python.exe tools/accept_console_install.py')
    add('# 冒烟（每类 1 个）')
    add('PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 .venv-qq/Scripts/python.exe tools/accept_console_install.py --limit 1 --keep')
    add('~~~')
    add('')
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='市场包导入控制台验收（真 HTTP）')
    parser.add_argument('--limit', type=int, default=0, help='每类最多抽几个（0=用默认计划）')
    parser.add_argument('--seed', type=int, default=1525, help='抽样随机种子，保证可复现')
    parser.add_argument('--timeout', type=float, default=90.0, help='单请求超时（秒）')
    parser.add_argument('--short', type=float, default=20.0, help='只读端点超时（秒）')
    parser.add_argument('--install-timeout', type=float, default=120.0, help='安装请求超时（秒）')
    parser.add_argument('--retries', type=int, default=1,
                        help='网络层失败的重试次数（默认 1 次；不掩盖真缺陷）')
    parser.add_argument('--plugins-only', action='store_true')
    parser.add_argument('--skills-only', action='store_true')
    parser.add_argument('--keep', action='store_true', help='保留上一轮的临时 data_root')
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
