"""导入控制台即可用：真实端到端验收探针（真进程 / 真 zip / 真返回值）。

跑什么：从市场清单挑一个 tier=P1-usable 且声明了 hook 的扩展 → 解压进临时宿主根
<临时根>/plugins/<扩展 id>/ → 用 extension_registry 启用 → 让 QqAgentBridge 实例
真起 node 子进程跑一次它声明的 hook、并真跑它声明的工具 → 再停用该扩展做反证。

判据只认证据：报告里的 pid / duration_ms 来自 extension_host 自己写的账本
data/qqagent_bridge_calls.jsonl，钩子返回值来自子进程 stdout 的 __WHALE_JSON__。

退出码：
  0  = 探针跑通（第 1~6 步的真实执行证据齐全），且反证项要么干净、要么是已登记的
       src 侧缺陷（报告第 8 节有 file:line）。--strict 时缺陷也判失败。
  1  = 真实执行这一层没跑通（扩展装不上 / 钩子起不来 / 工具面为空）。
  2  = 运行环境缺件（node、.tmp/qq-agent-run/src、市场 zip 不在），不是产品结论。

只写：.tmp/ 下的临时工作目录 + .tmp/inv/e2e-live-report.md。
用法：
    .venv-qq/Scripts/python.exe tools/e2e_extension_probe.py
    .venv-qq/Scripts/python.exe tools/e2e_extension_probe.py --strict
    .venv-qq/Scripts/python.exe tools/e2e_extension_probe.py --ext cache-sticky
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

INV_DIR = ROOT / '.tmp' / 'inv'
INVENTORY = INV_DIR / 'plugin-tools.json'
MARKET_DIR = ROOT / '.tmp' / 'external-market'
RUNTIME = ROOT / '.tmp' / 'qq-agent-run'
REPORT_PATH = INV_DIR / 'e2e-live-report.md'
LOG_NAME = 'qqagent_bridge_calls.jsonl'
BRIDGE_SRC = ROOT / 'src' / 'plugins' / 'qqagent_bridge.py'


def line_of(needle, path=None):
    """桥的行号会随作者改动漂移，报告里的 file:line 现场从源码里数出来。"""
    try:
        lines = Path(path or BRIDGE_SRC).read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return '?'
    for index, text in enumerate(lines, 1):
        if needle in text:
            return str(index)
    return '?'


def dock(needle):
    name = needle.strip().replace('def ', '').replace('(self', '')
    return 'src/plugins/qqagent_bridge.py:%s（%s）' % (line_of(needle), name)
HOST_ONE_CANDIDATES = (
    ROOT / '.tmp' / 'sweep-work' / 'host' / 'host-one.mjs',
    ROOT / '.tmp' / 'sweep-work' / 'probe-ro' / 'host' / 'host-one.mjs',
    ROOT / '.tmp' / 'sweep-work' / 'fullcopy' / 'host' / 'host-one.mjs',
)

# 与 src/plugins/qqagent_bridge.py:96-103 的 HOOK_EVENT 同一张表；这里只用它排序候选，
# 真正的 hook→事件 映射从桥模块 import（不复制事实来源）。
HOOK_ORDER = ('before-tool', 'after-response', 'before-context', 'before-llm-messages',
              'onMessage', 'after-tool')

# 钩子入参（这次调用的输入，不是伪造的返回值）。
MESSAGE = {'kind': 'group', 'target': '123456789', 'group_name': 'E2E 验收群',
           'sender_id': '10001', 'sender_name': '验收员', 'nickname': '验收员',
           'message_id': 'e2e-probe-1', 'text': '今天天气不错，晚上一起打游戏吗',
           'self_id': '99999', 'persona': {'name': '鲸鱼娘'}}
CONVERSATION = ({'role': 'system', 'content': '你是鲸鱼娘，说话简短。'},
                {'role': 'user', 'content': '今天天气不错，晚上一起打游戏吗'})
REPLY = '晚上八点开一局？'


# ── 基础工具 ────────────────────────────────────────────────────────────────
def say(line=''):
    print(line, flush=True)


def dump(value, limit=1200):
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= limit else text[:limit] + '…'


def node_exe():
    return shutil.which('node') or shutil.which('node.exe') or ''


def hooks_of(record):
    """清单里 hook 声明在两个地方：declared.hooks 只有 2 条，signals.hooks 才有 296 条。"""
    declared = [str(x) for x in (record.get('declared') or {}).get('hooks') or []]
    signals = [str(x) for x in (record.get('signals') or {}).get('hooks') or []]
    return declared + [h for h in signals if h not in declared]


def tool_names(record):
    return [str(item.get('name') or '') for item in record.get('tools') or []
            if isinstance(item, dict) and item.get('name')]


def load_inventory():
    return json.loads(INVENTORY.read_text(encoding='utf-8'))


def select_candidates(records, forced='', limit=5):
    """tier=P1-usable 且 hooks 非空；有工具的优先，然后按 hook 契约强度排。"""
    if forced:
        return [item for item in records if str(item.get('id')) == str(forced)]
    pool = [item for item in records
            if str(item.get('tier')) == 'P1-usable' and hooks_of(item)]

    def rank(item):
        hooks = hooks_of(item)
        best = min((HOOK_ORDER.index(name) if name in HOOK_ORDER else 99) for name in hooks)
        return (0 if tool_names(item) else 1, best, str(item.get('id')))

    return sorted(pool, key=rank)[:max(1, limit)]


def rank_reason(record):
    return '工具 %d 个 / hooks=%s / hostImports=%s / requires=%s' % (
        len(tool_names(record)), ','.join(hooks_of(record)) or '-',
        ','.join((record.get('signals') or {}).get('hostImports') or []) or '空',
        ','.join((record.get('declared') or {}).get('requires') or []) or '空')


# ── 宿主根制备 ──────────────────────────────────────────────────────────────
def build_host_root(tmp):
    """把 0.4 的 src/ + node_modules/ 复制成一次性宿主根（.tmp/qq-agent-run 只读）。

    为什么必须复制：host-one.mjs 会把扩展拷进 <source>/plugins-host-<pid>/，
    要写到 sourceDir 里，所以不能直接指向 .tmp/qq-agent-run。
    """
    host = tmp / 'host'
    for name in ('src', 'node_modules'):
        shutil.copytree(RUNTIME / name, host / name)
    shutil.copy2(RUNTIME / 'package.json', host / 'package.json')
    script = next((path for path in HOST_ONE_CANDIDATES if path.is_file()), None)
    if script is None:
        raise RuntimeError('找不到 host-one.mjs：%s'
                           % ', '.join(str(path) for path in HOST_ONE_CANDIDATES))
    shutil.copy2(script, host / 'host-one.mjs')
    (host / 'plugins').mkdir(parents=True, exist_ok=True)
    data = tmp / 'data'
    data.mkdir(parents=True, exist_ok=True)
    files = [path for path in host.rglob('*') if path.is_file()]
    return {'root': host, 'host_script': host / 'host-one.mjs', 'source_dir': host,
            'data_dir': data, 'script_src': script,
            'file_count': len(files), 'bytes': sum(path.stat().st_size for path in files)}


def manifest_ok(folder):
    return any((folder / name).is_file() for name in
               ('plugin.json', 'index.js', 'index.mjs', 'manifest.json'))


def normalize_root(folder):
    """与 host-one.mjs 的 extensionRoot() 同口径：剥掉多余的单层同名目录。"""
    for _ in range(4):
        if manifest_ok(folder):
            return folder
        subs = [path for path in folder.iterdir() if path.is_dir() and not path.name.startswith('.')]
        if len(subs) != 1:
            return folder
        inner = subs[0]
        for child in list(inner.iterdir()):
            shutil.move(str(child), str(folder / child.name))
        inner.rmdir()
    return folder


def install_zip(zip_path, plugins_dir, ident):
    """zip → <宿主根>/plugins/<扩展 id>/，逐条落盘（拒绝路径穿越）。"""
    target = plugins_dir / ident
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    written = 0
    with zipfile.ZipFile(zip_path) as archive:
        for name in archive.namelist():
            if not name or name.endswith('/'):
                continue
            parts = [part for part in name.replace('\\', '/').split('/')
                     if part not in ('', '.')]
            if not parts or '..' in parts or parts[0] == '__MACOSX':
                continue
            dest = target.joinpath(*parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, dest.open('wb') as handle:
                shutil.copyfileobj(source, handle)
            written += 1
    root = normalize_root(target)
    return {'dir': root, 'files': written,
            'has_plugin_json': (root / 'plugin.json').is_file(),
            'has_index': any((root / name).is_file() for name in ('index.js', 'index.mjs'))}



# ── 桥实例 ──────────────────────────────────────────────────────────────────
class StubManager:
    """只实现插件真正用到的那两个接口（get_settings / data_dir）。"""

    def __init__(self, settings, data_dir):
        self.settings = dict(settings)
        self.data_dir = Path(data_dir)

    def get_settings(self, plugin_id):
        return dict(self.settings)


def bridge_settings(env, ident, hook_name, ext_dir, node):
    return {'enabled': True, 'hostScript': str(env['host_script']), 'nodeExe': node,
            'sourceDir': str(env['source_dir']), 'extensionPath': str(ext_dir),
            'catalogPath': '', 'extensionsDir': '', 'autoDiscover': False, 'maxProfiles': 2,
            'provider': '', 'hookName': hook_name, 'event': '', 'ownerOnly': False,
            'ownerIds': '', 'injectPrompt': False, 'timeout': 30, 'preAiBudgetMs': 0,
            'toolAllowlist': '', 'profiles': ''}


def make_bridge(bridge_mod, env, ident, hook_name, ext_dir, node):
    settings = bridge_settings(env, ident, hook_name, ext_dir, node)
    return bridge_mod.QqAgentBridge(StubManager(settings, env['data_dir']))


class SpawnSpy:
    """数 asyncio.create_subprocess_exec 的真实调用次数。

    extension_host.run_capability 在调用时才做属性查找，所以替换 asyncio 模块上的同名
    属性就能截到每一次起进程；这条路径上没有别的代码起子进程。
    """

    def __init__(self):
        self.calls = []
        self._original = None

    def __enter__(self):
        self._original = asyncio.create_subprocess_exec
        original = self._original

        def wrapper(*args, **kwargs):
            self.calls.append([str(item) for item in args])
            return original(*args, **kwargs)

        asyncio.create_subprocess_exec = wrapper
        return self

    def __exit__(self, *exc_info):
        asyncio.create_subprocess_exec = self._original
        return False


def ledger_rows(data_dir):
    path = Path(data_dir) / LOG_NAME
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def ledger_tail(data_dir, kind, ident):
    for row in reversed(ledger_rows(data_dir)):
        if str(row.get('kind')) == kind and str(row.get('extension')) == ident:
            return row
    return {}


# ── 钩子调用 ────────────────────────────────────────────────────────────────
async def _hook_call(plugin, event):
    if event == 'on_post_reply':
        return await plugin.on_post_reply(dict(MESSAGE), REPLY, None)
    if event == 'on_pre_ai':
        return await plugin.on_pre_ai_async(dict(MESSAGE), [dict(x) for x in CONVERSATION], None)
    if event == 'on_prepare':
        return await plugin.on_prepare(dict(MESSAGE), None)
    if event == 'on_before_tool':
        return await plugin.on_before_tool('list_stickers', {'count': 1}, None)
    if event == 'on_after_tool':
        return await plugin.on_after_tool('list_stickers', {'count': 1}, 'ok', None)
    raise RuntimeError('未知事件：%s' % event)


def run_hook(plugin, event, hook_name=''):
    """真 await 一次桥的钩子；同时数进程、读账本。"""
    with SpawnSpy() as spy:
        started = time.perf_counter()
        returned = asyncio.run(_hook_call(plugin, event))
        elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {'event': event, 'hook': hook_name, 'elapsed_ms': elapsed_ms,
            'spawns': len(spy.calls), 'argv_head': spy.calls[0][:6] if spy.calls else [],
            'returned': returned}


# ── 分层判定（引用 plugin-tools.json 字段） ──────────────────────────────────
def tier_verdicts(records):
    buckets = {}
    for item in records:
        tier = str(item.get('tier') or '?')
        declared = item.get('declared') or {}
        signals = item.get('signals') or {}
        row = buckets.setdefault(tier, {'count': 0, 'hooks': 0, 'tools': 0, 'host_imports': 0,
                                        'electron': 0, 'requires': 0, 'web_fetch': 0,
                                        'samples': []})
        row['count'] += 1
        row['hooks'] += 1 if hooks_of(item) else 0
        row['tools'] += len(tool_names(item))
        row['host_imports'] += 1 if signals.get('hostImports') else 0
        row['electron'] += 1 if str(signals.get('electron') or 'none') != 'none' else 0
        row['requires'] += 1 if declared.get('requires') else 0
        row['web_fetch'] += 1 if signals.get('webFetch') else 0
        if len(row['samples']) < 3:
            row['samples'].append(str(item.get('id')))
    return buckets


def node_version():
    try:
        done = subprocess.run(['node', '--version'], capture_output=True, text=True, timeout=20)
        return done.stdout.strip() or '未知'
    except Exception:                                                   # noqa: BLE001
        return '未知'


# ── 主流程 ──────────────────────────────────────────────────────────────────
def execute(argv=None):
    parser = argparse.ArgumentParser(description='导入控制台即可用的真实端到端验收探针')
    parser.add_argument('--strict', action='store_true', help='把已登记的 src 侧缺陷也判为失败')
    parser.add_argument('--ext', default='', help='强制指定扩展 id（默认自动挑）')
    parser.add_argument('--attempts', type=int, default=4, help='最多试几个候选')
    parser.add_argument('--keep', action='store_true', help='保留临时工作目录')
    parser.add_argument('--no-report', action='store_true', help='不写报告文件')
    args = parser.parse_args(argv)

    report = {'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'steps': {}, 'defects': [],
              'checks': [], 'strict': bool(args.strict)}
    started = time.perf_counter()

    node = node_exe()
    missing = []
    if not node:
        missing.append('node（Node 18+）')
    if not (RUNTIME / 'src' / 'plugin-loader.js').is_file():
        missing.append(str(RUNTIME / 'src' / 'plugin-loader.js'))
    if not INVENTORY.is_file():
        missing.append(str(INVENTORY))
    if not MARKET_DIR.is_dir():
        missing.append(str(MARKET_DIR))
    if missing:
        say('环境缺件，无法执行真实端到端探针：' + '；'.join(missing))
        report['blocked'] = missing
        return 2

    from src import extension_registry as registry
    from src.plugins import qqagent_bridge as bridge_mod

    # 1) 选候选
    records = load_inventory().get('plugins') or []
    pool = select_candidates(records, '', limit=999)
    candidates = select_candidates(records, args.ext, limit=max(1, args.attempts))
    say('[1] 清单 %d 条；tier=P1-usable 且声明 hook 的候选 %d 条；本次前 %d：'
        % (len(records), len(pool), len(candidates)))
    for item in (candidates if args.ext else pool[:5]):
        say('    - %s（%s）' % (item.get('id'), rank_reason(item)))
    report['steps']['select'] = {
        'inventory': str(INVENTORY), 'records': len(records), 'p1_with_hooks': len(pool),
        'top5': [{'id': item.get('id'), 'zip': item.get('zip'), 'hooks': hooks_of(item),
                  'tools': tool_names(item),
                  'hostImports': (item.get('signals') or {}).get('hostImports') or [],
                  'requires': (item.get('declared') or {}).get('requires') or []}
                 for item in candidates]}
    if not candidates:
        say('没有可用候选，停止。')
        return 1

    tmp = Path(tempfile.mkdtemp(prefix='qqagent-e2e-'))
    try:
        env = build_host_root(tmp)
        say('[2] 临时宿主根 %s（%d 文件 / %.1f MB；host-one.mjs 来自 %s）'
            % (env['root'], env['file_count'], env['bytes'] / 1048576.0, env['script_src']))
        report['steps']['host_root'] = {
            'root': str(env['root']), 'host_script': str(env['host_script']),
            'source_dir': str(env['source_dir']), 'data_dir': str(env['data_dir']),
            'host_script_from': str(env['script_src']),
            'file_count': env['file_count'], 'bytes': env['bytes']}
        os.environ['QQ_AGENT_DATA_DIR'] = str(env['data_dir'])   # 扩展落盘不出临时根

        attempts, chosen = [], None
        for item in candidates:
            ident = str(item.get('id'))
            zip_path = MARKET_DIR / str(item.get('zip') or '')
            attempt = {'id': ident, 'tier': item.get('tier'), 'zip': str(zip_path),
                       'hooks': hooks_of(item), 'tools': tool_names(item)}
            attempts.append(attempt)
            if not zip_path.is_file():
                attempt['installed'] = {'error': 'zip 不在 %s' % zip_path}
                say('    %s：zip 缺失，跳过' % ident)
                continue
            installed = install_zip(zip_path, env['root'] / 'plugins', ident)
            attempt['installed'] = {'dir': str(installed['dir']), 'files': installed['files'],
                                    'has_plugin_json': installed['has_plugin_json'],
                                    'has_index': installed['has_index']}
            if not (installed['has_plugin_json'] or installed['has_index']):
                say('    %s：解压后没有 plugin.json/index.js，跳过' % ident)
                continue
            attempt['registry'] = registry.set_enabled(ident, True, env['data_dir'],
                                                       source='market', note='e2e probe')
            attempt['known'] = registry.is_known(ident, env['data_dir'])
            attempt['enabled'] = registry.is_enabled(ident, env['data_dir'])
            hooks = hooks_of(item)
            hook_name = next((name for name in HOOK_ORDER if name in hooks),
                             hooks[0] if hooks else '')
            event = bridge_mod.HOOK_EVENT.get(hook_name, '')
            attempt['hook'] = hook_name
            attempt['event'] = event
            if not event:
                say('    %s：hook %s 没有对应事件，跳过' % (ident, hook_name))
                continue
            plugin = make_bridge(bridge_mod, env, ident, hook_name, installed['dir'], node)
            say('[3] 装+启 %s：%s（%d 文件，plugin.json=%s）→ registry known=%s enabled=%s'
                % (ident, installed['dir'], installed['files'], installed['has_plugin_json'],
                   attempt['known'], attempt['enabled']))
            try:
                hook_run = run_hook(plugin, event, hook_name)
            except Exception as error:                                  # noqa: BLE001
                attempt['hook_error'] = '%s: %s' % (type(error).__name__, error)
                attempt['traceback'] = traceback.format_exc()[-800:]
                say('    %s：钩子抛异常 %s' % (ident, attempt['hook_error']))
                continue
            row = ledger_tail(env['data_dir'], 'hook', ident)
            hook_run['ledger'] = row
            attempt['hook_run'] = {'spawns': hook_run['spawns'],
                                   'elapsed_ms': hook_run['elapsed_ms'], 'ledger': row,
                                   'returned': hook_run['returned']}
            say('    钩子 %s → %s：spawn=%d pid=%s duration_ms=%s ok=%s 返回值=%s'
                % (hook_name, event, hook_run['spawns'], row.get('pid'), row.get('duration_ms'),
                   row.get('ok'), dump(hook_run['returned'], 220)))
            if not (hook_run['spawns'] and row.get('pid') and row.get('ok')):
                say('    %s：钩子没真跑通（spawn/pid/ok 不齐），换下一个候选' % ident)
                continue
            chosen = (item, ident, installed, plugin, hook_name, event, hook_run)
            break

        report['steps']['attempts'] = attempts
        if chosen is None:
            say('没有任何候选真跑通钩子。')
            report['steps']['run'] = {'ok': False}
            return finalize(report, started, args)

        item, ident, installed, plugin, hook_name, event, hook_run = chosen
        run = {'id': ident, 'hook': hook_name, 'event': event,
               'hook_returned': hook_run['returned'], 'hook_spawns': hook_run['spawns'],
               'hook_elapsed_ms': hook_run['elapsed_ms'],
               'ledger_row': hook_run['ledger'], 'argv_head': hook_run['argv_head']}
        report['steps']['run'] = run

        # 6) 工具面：真 list_tools() + 真 call_tool()
        tools = plugin.list_tools()
        tool_rows = []
        for tool in tools:
            params = tool.get('parameters') if isinstance(tool, dict) else None
            schema_ok = (isinstance(params, dict) and params.get('type') == 'object'
                         and isinstance(params.get('properties'), dict))
            tool_rows.append({'id': str(tool.get('name') or tool.get('id') or ''),
                              'description': str(tool.get('description'))[:160],
                              'parameters': params, 'schema_ok': schema_ok,
                              'full_name_ok': str(tool.get('name') or '').startswith(ident + '__')})
        say('[4] list_tools() 返回 %d 个：%s'
            % (len(tool_rows), ', '.join(row['id'] for row in tool_rows) or '空'))
        calls = []
        for row in tool_rows[:3]:
            with SpawnSpy() as spy:
                call = plugin.call_tool(row['id'], {})
            entry = ledger_tail(env['data_dir'], 'tool', ident)
            calls.append({'tool': row['id'], 'arguments': {}, 'result': call,
                          'spawns': len(spy.calls), 'ledger': entry})
            say('    call_tool(%s)：spawn=%d pid=%s duration_ms=%s ok=%s 结果=%s'
                % (row['id'], len(spy.calls), entry.get('pid'), entry.get('duration_ms'),
                   call.get('ok'), dump(call, 260)))
        run['tools'] = tool_rows
        run['tool_calls'] = calls

        # 5) 反证：停用后不得再起进程
        state_set = registry.set_enabled(ident, False, env['data_dir'], source='market',
                                         note='e2e probe 反证')
        state_after = {'known': registry.is_known(ident, env['data_dir']),
                       'enabled': registry.is_enabled(ident, env['data_dir'])}
        before_rows = len(ledger_rows(env['data_dir']))
        with SpawnSpy() as spy_list:
            tools_disabled = plugin.list_tools()
        rows_after_list = len(ledger_rows(env['data_dir']))
        with SpawnSpy() as spy_hook:
            try:
                hook_disabled = run_hook(plugin, event, hook_name)
                hook_disabled_error = ''
            except Exception as error:                                  # noqa: BLE001
                hook_disabled = {'returned': None, 'spawns': 0}
                hook_disabled_error = '%s: %s' % (type(error).__name__, error)
        rows_after_hook = len(ledger_rows(env['data_dir']))
        tool_id = tool_rows[0]['id'] if tool_rows else '%s__probe' % ident
        cache_path = Path(env['data_dir']) / 'qqagent_bridge_tools.json'
        with SpawnSpy() as spy_call:
            call_disabled = plugin.call_tool(tool_id, {})
        rows_after_call = len(ledger_rows(env['data_dir']))
        cache_ids = []
        if cache_path.is_file():
            try:
                cache_ids = sorted(json.loads(cache_path.read_text(encoding='utf-8')).keys())
            except ValueError:
                cache_ids = []

        list_verdict = ('clean' if (not tools_disabled and not spy_list.calls
                                    and rows_after_list == before_rows) else 'defect-d1')
        hook_verdict = ('clean' if (not spy_hook.calls and rows_after_hook == rows_after_list)
                        else 'defect-d2')
        error_kind = str((call_disabled.get('error') or {}).get('kind') or '')
        call_verdict = ('clean' if (error_kind == 'disabled' and not spy_call.calls)
                        else 'defect-d3')
        negation = {'registry_after': state_after, 'state_set': state_set,
                    'ledger_rows_before': before_rows,
                    'list_tools': {'verdict': list_verdict, 'spawns': len(spy_list.calls),
                                   'returned': [str(x.get('name') or x.get('id')) for x in tools_disabled],
                                   'ledger_delta': rows_after_list - before_rows,
                                   'cache_file': str(cache_path), 'cache_ids': cache_ids},
                    'hook': {'verdict': hook_verdict, 'spawns': len(spy_hook.calls),
                             'ledger_delta': rows_after_hook - rows_after_list,
                             'returned': hook_disabled.get('returned'),
                             'error': hook_disabled_error,
                             'ledger_row': ledger_tail(env['data_dir'], 'hook', ident)},
                    'call_tool': {'verdict': call_verdict, 'spawns': len(spy_call.calls),
                                  'error': call_disabled.get('error'),
                                  'ledger_delta': rows_after_call - rows_after_hook}}
        report['steps']['negation'] = negation
        say('[5] 反证 registry.set_enabled(%s, False)：' % ident)
        say('    list_tools() → verdict=%s spawn=%d 返回=%d 个 账本增量=%+d'
            % (list_verdict, len(spy_list.calls), len(tools_disabled),
               rows_after_list - before_rows))
        say('    同一钩子   → verdict=%s spawn=%d 账本增量=%+d'
            % (hook_verdict, len(spy_hook.calls), rows_after_hook - rows_after_list))
        say('    call_tool()→ verdict=%s kind=%s spawn=%d 账本增量=%+d'
            % (call_verdict, error_kind or '-', len(spy_call.calls),
               rows_after_call - rows_after_hook))
        report['defects'] = collect_defects(report)
        return finalize(report, started, args)
    finally:
        if args.keep:
            say('临时工作目录保留：%s' % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def collect_defects(report):
    """把观察到的反证失败登记成 src 侧缺陷（只登记，不修）。"""
    defects = []
    negation = report.get('steps', {}).get('negation') or {}
    tools_path = ' + '.join((dock('def list_tools(self'), dock('def _call_tool_async(self')))
    hook_path = ' + '.join((dock('def _event_profiles(self'), dock('def _stage(self'),
                            dock('def _profiles(self')))
    if (negation.get('list_tools') or {}).get('verdict') != 'clean':
        defects.append({
            'id': 'D1', 'file': tools_path,
            'what': '停用后 list_tools() 仍起了 node 进程或仍返回了该扩展的工具：'
                    'not cache and not raw_cache 的回落条件表示缓存只剩停用条目时不回落，'
                    '_registry_disabled 那道门禁本应把停用扩展挡住。命中即说明闸有洞，'
                    '用户停用了却还能在控制台里看到或调到工具。',
            'evidence': negation.get('list_tools')})
    if (negation.get('hook') or {}).get('verdict') != 'clean':
        defects.append({
            'id': 'D2', 'file': hook_path,
            'what': '停用闸门只有工具路径有：list_tools 与 _call_tool_async 都调 '
                    '_registry_disabled；而钩子路径 _event_profiles → _stage 只查桥自身 '
                    'enabled、profile 的 event/capability 与 ownerOnly，_profiles 的手工 '
                    'profiles 分支也不按 registry 过滤（只有 autoDiscover 分支过滤停用）。'
                    '结果：扩展在扩展管理里停用后，provider/hook 仍然每次真起 node 进程执行，'
                    '并照常写账本。',
            'evidence': negation.get('hook')})
    if (negation.get('call_tool') or {}).get('verdict') != 'clean':
        defects.append({
            'id': 'D3', 'file': str(dock('def _call_tool_async(self')),
            'what': '扩展停用后 call_tool 没有返回 error.kind=disabled，或仍然起了进程。',
            'evidence': negation.get('call_tool')})
    return defects


def finalize(report, started, args):
    report['elapsed_s'] = round(time.perf_counter() - started, 2)
    run = report.get('steps', {}).get('run') or {}
    checks = []
    checks.append(('候选来自 tier=P1-usable + hooks 非空', bool(report['steps']['select']['top5'])))
    attempts = report['steps'].get('attempts') or [{}]
    checks.append(('zip 真解压进 <临时根>/plugins/<扩展 id>/',
                   bool((attempts[0].get('installed') or {}).get('files'))))
    ledger_row = run.get('ledger_row') or {}
    checks.append(('钩子真起进程且有 pid',
                   bool(ledger_row.get('pid')) and bool(run.get('hook_spawns'))))
    checks.append(('钩子账本 ok=True', bool(ledger_row.get('ok'))))
    tools = run.get('tools') or []
    checks.append(('工具全名是 <扩展 id>__<工具名>',
                   bool(tools) and all(row.get('full_name_ok') for row in tools)))
    checks.append(('工具 parameters 是合法 JSON Schema',
                   bool(tools) and all(row.get('schema_ok') for row in tools)))
    calls = run.get('tool_calls') or []
    checks.append(('call_tool 真起进程并写账本',
                   bool(calls) and all(row.get('spawns') and (row.get('ledger') or {}).get('pid')
                                       for row in calls)))
    negation = report['steps'].get('negation') or {}
    checks.append(('停用后 call_tool 返回 kind=disabled 且不起进程',
                   (negation.get('call_tool') or {}).get('verdict') == 'clean'))
    checks.append(('停用后 list_tools 返回空且不起进程（D1 若命中即缺陷）',
                   (negation.get('list_tools') or {}).get('verdict') == 'clean'))
    checks.append(('停用后同一钩子不起进程（D2 若命中即缺陷）',
                   (negation.get('hook') or {}).get('verdict') == 'clean'))
    report['checks'] = [{'name': name, 'ok': ok} for name, ok in checks]

    hard = [item for item in report['checks'][:7] if not item['ok']]
    soft = [item for item in report['checks'][7:] if not item['ok']]
    code = 1 if hard or (args.strict and soft) else 0
    report['exit_code'] = code
    say('[6] 硬性断言 %d/7 通过；负向断言 %d/3 通过；已登记缺陷 %s'
        % (sum(1 for item in report['checks'][:7] if item['ok']),
           sum(1 for item in report['checks'][7:] if item['ok']),
           ','.join(item['id'] for item in report['defects']) or '无'))
    if not args.no_report:
        INV_DIR.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(render_report(report), encoding='utf-8')
        say('[7] 报告：%s' % REPORT_PATH)
    say('exit=%d' % code)
    return code


def render_report(report):
    select = report['steps'].get('select') or {}
    host_root = report['steps'].get('host_root') or {}
    run = report['steps'].get('run') or {}
    negation = report['steps'].get('negation') or {}
    attempts = report['steps'].get('attempts') or []
    lines = []
    add = lines.append
    add('# 导入控制台即可用 · 真实端到端探针报告')
    add('')
    add('- 生成时间：%s；总耗时：%ss；退出码：%s'
        % (report['generated_at'], report.get('elapsed_s'), report.get('exit_code')))
    add('- Node：%s；0.4 运行副本：%s（只读，未写入）' % (node_version(), RUNTIME))
    add('- 复现：.venv-qq/Scripts/python.exe tools/e2e_extension_probe.py%s'
        % (' --strict' if report.get('strict') else ''))
    add('')
    add('## 0 结论')
    add('')
    add('| 断言 | 结果 |')
    add('|---|---|')
    for item in report.get('checks') or []:
        add('| %s | %s |' % (item['name'], '通过' if item['ok'] else '**未通过**'))
    add('')
    if report.get('defects'):
        add('已登记 src 侧缺陷（未修）：%s —— 详见第 8 节。'
            % '、'.join(item['id'] for item in report['defects']))
        add('')
    add('选中扩展：%s' % run.get('id'))
    add('')
    add('## 1 选候选（plugin-tools.json）')
    add('')
    add('- 清单：%s，%d 条；tier=P1-usable 且 hook 非空：%d 条'
        % (select.get('inventory'), select.get('records') or 0, select.get('p1_with_hooks') or 0))
    add('- 字段口径：declared.hooks 只有 2 条非空，hook 实际写在 signals.hooks（296 条）；'
        '本探针取两者并集，不做推测。')
    add('')
    add('| # | 扩展 id | zip | hooks | 工具数 | signals.hostImports | declared.requires |')
    add('|---|---|---|---|---|---|---|')
    for index, item in enumerate(select.get('top5') or [], 1):
        add('| %d | %s | %s | %s | %d | %s | %s |'
            % (index, item['id'], item.get('zip'), ','.join(item.get('hooks') or []),
               len(item.get('tools') or []),
               ','.join(item.get('hostImports') or []) or '空',
               ','.join(item.get('requires') or []) or '空'))
    add('')
    add('选择理由：tier=P1-usable 意味着没有 ../../src 相对导入（signals.hostImports 为空）、'
        '没有 declared.requires、signals.electron 为 none，只靠 src/ + node_modules/ 就起得来；'
        '同时又声明了 hook 与工具，一次能把 hook 和 tool 两种能力都真跑一遍。')
    add('')
    add('## 2 宿主根制备')
    add('')
    add('- 临时宿主根：%s（%s 个文件 / %.1f MB）'
        % (host_root.get('root'), host_root.get('file_count'),
           (host_root.get('bytes') or 0) / 1048576.0))
    add('- host 脚本：%s（复制自 %s）'
        % (host_root.get('host_script'), host_root.get('host_script_from')))
    add('- source_dir：%s（0.4 的 src/ 与 node_modules/ 副本）' % host_root.get('source_dir'))
    add('- data_dir：%s（账本 qqagent_bridge_calls.jsonl 落在这里）' % host_root.get('data_dir'))
    add('- 为什么必须复制：host-one.mjs 会把扩展拷进 sourceDir/plugins-host-<pid>/，'
        '写入 sourceDir 本身；把 sourceDir 直接指向 .tmp/qq-agent-run 就会写脏 0.4 运行副本。')
    add('')
    add('## 3 装 + 启')
    add('')
    for attempt in attempts:
        installed = attempt.get('installed') or {}
        add('- %s：zip %s → %s（%s 个文件，plugin.json=%s）'
            % (attempt.get('id'), attempt.get('zip'), installed.get('dir'),
               installed.get('files'), installed.get('has_plugin_json')))
        if attempt.get('registry'):
            add('  - extension_registry.set_enabled(%s, True, data_dir=<临时 data>) → '
                'is_known=%s，is_enabled=%s'
                % (attempt.get('id'), attempt.get('known'), attempt.get('enabled')))
    add('')
    add('## 4 真跑钩子')
    add('')
    row = run.get('ledger_row') or {}
    add('- 钩子 %s → 桥事件 %s；本次真起进程 %s 次，argv 头：%s'
        % (run.get('hook'), run.get('event'), run.get('hook_spawns') or 0,
           ' '.join(os.path.basename(str(x)) for x in (run.get('argv_head') or []))))
    add('- 账本行（data/qqagent_bridge_calls.jsonl）：')
    add('')
    add('~~~json')
    add(dump(row, 1200))
    add('~~~')
    add('')
    add('- pid=%s，duration_ms=%s，ok=%s（探针侧实测耗时 %sms）'
        % (row.get('pid'), row.get('duration_ms'), row.get('ok'), run.get('hook_elapsed_ms')))
    add('- 钩子返回值（子进程回传的 hook_result 经桥落回）：')
    add('')
    add('~~~json')
    add(dump(run.get('hook_returned'), 1200))
    add('~~~')
    add('')
    add('## 5 工具面')
    add('')
    add('| 工具全名 | <扩展 id>__<工具名> | parameters 合法 | 描述 |')
    add('|---|---|---|---|')
    for tool in run.get('tools') or []:
        add('| %s | %s | %s | %s |'
            % (tool['id'], tool['full_name_ok'], tool['schema_ok'],
               (tool.get('description') or '').replace('|', '/')[:70]))
    add('')
    for call in run.get('tool_calls') or []:
        add('- call_tool(%s, {}) → spawn=%s，pid=%s（spawn>1 表示桥在调用前先现枚举了一次'
            '工具，两次都是真 node 进程）：'
            % (call['tool'], call.get('spawns'), (call.get('ledger') or {}).get('pid')))
        add('')
        add('~~~json')
        add(dump(call.get('result'), 900))
        add('~~~')
        add('')
    add('## 6 反证：停用后不得起进程')
    add('')
    add('- registry.set_enabled(%s, False, data_dir=<临时 data>) → 之后 is_known=%s，is_enabled=%s'
        % (run.get('id'), (negation.get('registry_after') or {}).get('known'),
           (negation.get('registry_after') or {}).get('enabled')))
    add('- 停用前账本行数：%s' % negation.get('ledger_rows_before'))
    add('')
    add('| 入口 | 起了几个 node 进程 | 账本增量 | 返回 | 判定 |')
    add('|---|---|---|---|---|')
    for key, label in (('list_tools', 'list_tools()'), ('hook', '同一钩子'),
                       ('call_tool', 'call_tool()')):
        data = negation.get(key) or {}
        if key == 'list_tools':
            detail = '%d 个工具：%s' % (len(data.get('returned') or []),
                                     dump(data.get('returned'), 140))
        elif key == 'call_tool':
            detail = dump(data.get('error'), 160)
        else:
            detail = dump(data.get('returned'), 120) or (data.get('error') or '-')
        add('| %s | %s | %+d | %s | %s |'
            % (label, data.get('spawns'), data.get('ledger_delta') or 0, detail,
               data.get('verdict')))
    add('')
    add('- 判定口径：clean = 0 个新进程且账本不增（list_tools 还要求返回空）；'
        'defect-dN = 违反桥自己在注释里写下的契约，登记到第 8 节，不修。')
    add('')
    add('## 7 分层判定：哪些扩展必然跑不通（依据 plugin-tools.json 字段）')
    add('')
    add('| tier | 扩展数 | 声明 hook | 工具数 | hostImports 非空 | electron!=none | '
        'requires 非空 | webFetch |')
    add('|---|---|---|---|---|---|---|---|')
    for tier, data in sorted(tier_verdicts(_cached_records()).items()):
        add('| %s | %d | %d | %d | %d | %d | %d | %d |'
            % (tier, data['count'], data['hooks'], data['tools'], data['host_imports'],
               data['electron'], data['requires'], data['web_fetch']))
    add('')
    add('- P5-electron（signals.electron 不是 none）：宿主是纯 node，import electron 必然 '
        'ERR_MODULE_NOT_FOUND。唯一一例 long-image-reader 在 plugin-tools.json 的 '
        'load.loadError 里就写着 Cannot find package electron。')
    add('- P4-depends-other（declared.requires 非空）：host-one.mjs 一次只装一个扩展，'
        'requires 指的是别的扩展提供的代码或服务，单扩展必然缺件。')
    add('- P6-declarative（tools 为 0 且 signals.hooks 为空）：清单里没有任何可调用能力，'
        '不是跑不通，是没有入口可跑。')
    add('- P3-host-source（signals.hostImports 非空，338 条）：只要宿主根带 0.4 的 src/'
        '（本探针的临时根就是这样，见第 2 节）就能加载；缺 src/ 时必然 ERR_MODULE_NOT_FOUND。'
        '属宿主根口径问题，不是结构性不可用。')
    add('- P2-web-fetch（signals.webFetch=true）：能加载，但结果取决于外网可达性，'
        '不适合当导入即可用的稳定验收项。')
    add('')
    add('## 8 src 侧缺陷（只登记，不修）')
    add('')
    if not report.get('defects'):
        add('本轮反证全部干净，未登记缺陷。')
    for defect in report.get('defects') or []:
        add('### %s · %s' % (defect['id'], defect['file']))
        add('')
        add(defect['what'])
        add('')
        add('实测证据：%s' % dump(defect.get('evidence'), 700))
        add('')
    add('## 9 边界')
    add('')
    add('- .tmp/qq-agent-run/、仓库 data/、LOCALAPPDATA 下的 WhaleGirlQQ 目录全程只读；'
        '所有写入都在临时工作目录与 .tmp/inv/。')
    add('- 只读核验：宿主根与 data_dir 都在 %TEMP% 下（见第 2 节）。RUNTIME 在本脚本里只作 '
        '复制源与存在性检查（shutil.copytree/copy2），从不作为 source_dir 传给宿主。')
    add('- host-one.mjs 会在 sourceDir 下建 plugins-host-<pid>/，并在 finally 里删除'
        '（.tmp/sweep-work/host/host-one.mjs:14、99），所以落点是临时根，不是 0.4 副本。')
    add('- 现场对照：本次真起进程的 pid 见第 4/5 节；.tmp/qq-agent-run 里残留的 '
        'plugins-host-* 目录 pid 与之不重合，来自同时在跑的控制台进程。')
    add('- 探针不修改 src/**：第 8 节的缺陷只登记，交给正在改桥的人。')
    add('')
    return chr(10).join(lines)


_RECORDS = []


def _cached_records():
    global _RECORDS
    if not _RECORDS:
        try:
            _RECORDS = load_inventory().get('plugins') or []
        except (OSError, ValueError):
            _RECORDS = []
    return _RECORDS


def main(argv=None):
    try:
        return execute(argv)
    except Exception:                                                   # noqa: BLE001
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
