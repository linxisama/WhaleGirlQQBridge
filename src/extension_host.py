"""第三方 QQ Agent 扩展的隔离子进程执行层（provider / hook / tool 三种能力）。

为什么单独成一个模块：桥插件要被 manager 正常加载，测试和 WP2 的工具环又要在
不启动整个机器人的情况下直接调用同一套执行逻辑。所以"起 node 子进程 → 超时强杀 →
解析 __WHALE_JSON__ 回传 → 写 jsonl 记账"集中在这里，插件只负责把消息翻译成
0.4 宿主语义的 payload。

安全边界（用户必须知情）
------------------------
1. 执行扩展 = 在这台机器上执行第三方 JavaScript。host-one.mjs 用 0.4 的
   plugin-loader 动态 import() 扩展的 index.js（host-one.mjs:55-63），扩展代码
   能读环境变量、读写当前用户能访问的文件、联网。**这不是沙箱。**
2. 本模块只做三件低成本防护，不构成对恶意扩展的安全边界：
   ① 只有"已启用 + 被显式选中"的扩展才会被执行（白名单选择在上层桥插件里）；
   ② 一个扩展一个 node 进程，跑完即退，超时强杀，互不共享内存；
   ③ 能力名必须命中白名单（provider / hook / tool 各一张表），未命中直接拒绝，
      连进程都不启动；暴露出去的 tool id 只认该扩展自己注册的那些。
3. 扩展拿不到任何反向入口：没有 Python 回调句柄、没有控制台接口、没有设置写权限，
   它只能在 argv 里看到本次 payload。
4. 宿主缺口（不伪造）：0.4 会把 store / memory / session 这类真实对象传进 hook 和
   provider，桥这边没有等价物，一律显式给 None。扩展若直接用这些对象会抛错，
   这里的处理是**记录失败**，而不是塞假数据让它"看起来跑通"。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import app_log

# host-one.mjs 把结果打在一行标记后面：__WHALE_JSON__{...}
MARKER = "__WHALE_JSON__"
DEFAULT_TIMEOUT = 10.0
MAX_TIMEOUT = 60.0
SYNC_TIMEOUT_SLACK = 5.0
LOG_NAME = "qqagent_bridge_calls.jsonl"
LOG_MAX_BYTES = 8 * 1024 * 1024
STATUS_TAIL_BYTES = 256 * 1024
# no-json 的排障窗口：stderr 尾巴整行保留 800 字（写进 result['stderr_tail'] 与账本），
# no-json 文案里只挑最关键的原始 JS 异常那一行（140 字）。
STDERR_TAIL_CHARS = 800
STDERR_ERROR_CHARS = 140

# 与 qqagent_bridge.HOOK_EVENT 保持一致；这里再存一份是为了让本模块能在不 import
# 插件模块（避免循环 import）的情况下校验 hook 名。
HOOK_NAMES = ('before-context', 'before-llm-messages', 'onMessage', 'after-response',
              'before-tool', 'after-tool')


# ── 进程调用 ─────────────────────────────────────────────────────────────────
def clamp_timeout(value, default: float = DEFAULT_TIMEOUT) -> float:
    """把任何来源的超时值收敛到 [1, MAX_TIMEOUT]；坏值退回默认。"""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    if seconds != seconds or seconds <= 0:  # NaN / 0 / 负数
        return default
    return max(1.0, min(MAX_TIMEOUT, seconds))


def build_argv(node: str, host_script: str, extension: str, capability: str,
               capability_arg: str = '', payload: Optional[dict] = None) -> List[str]:
    """host-one.mjs 的调用约定：argv=[node, host, 扩展, plugin|skill, 能力, 参数, payload]。"""
    capability = str(capability or '')
    pack = json.dumps(payload or {}, ensure_ascii=False)
    if capability in ('hook', 'tool'):
        return [node, host_script, extension, 'plugin', capability,
                str(capability_arg or ''), pack]
    if not capability:
        # 空能力 = 只加载不调用：host-one 回传 extension 元数据与 tools 清单。
        return [node, host_script, extension, 'plugin', '']
    return [node, host_script, extension, 'plugin', capability, pack]


def parse_host_stdout(text: str) -> dict:
    """从 host-one.stdout 里取出那个 JSON；解析不出来返回 {}（绝不抛）。"""
    text = text or ''
    raw = ''
    if MARKER in text:
        raw = text.split(MARKER, 1)[1]
    else:
        start, end = text.find('{'), text.rfind('}')
        if start >= 0 and end > start:
            raw = text[start:end + 1]
    raw = raw.strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _value_for(kind: str, data: dict) -> Any:
    key = {'list': 'tools', 'tool': 'tool_result', 'hook': 'hook_result'}.get(kind, 'provider_result')
    return data.get(key)


def _stderr_tail(text: str, limit: int = 400) -> str:
    text = (text or '').strip()
    return text[-limit:] if len(text) > limit else text


def _stderr_excerpt(text: str, limit: int = STDERR_TAIL_CHARS) -> str:
    """stderr 的尾巴，从整行开始截（避免半行 JSON/堆栈）；没有 stderr 就是空串。"""
    text = (text or '').replace('\r\n', '\n').replace('\r', '\n').strip()
    if not text:
        return ''
    if len(text) <= limit:
        return text
    head, sep, rest = text[-limit:].partition('\n')
    return (rest if sep else text[-limit:]).strip()


_ERROR_WORDS = ('Error', 'Exception')


def _stderr_error_line(text: str, limit: int = STDERR_ERROR_CHARS) -> str:
    """挑 stderr 里那一行原始 JS 异常（`TypeError: ...` / `Uncaught Error: ...`）。

    node 崩掉的 stderr 形如"源码行 + 插入符 + 空行 + Error: 原因 + 堆栈 + Node.js 版本"：
    只截尾巴会剩下一堆 at 栈帧和版本号，真正的原因那行反而被挤掉（这就是 no-json 里
    看不到 JS 错误文本的原因）。所以先找错误行，挑不到才退回最后一行非空文本
    （例如 node 找不到模块时的 `Cannot find module ...`）。
    """
    lines = [line.strip() for line in (text or '').replace('\r\n', '\n').split('\n')]
    lines = [line for line in lines if line]
    for line in reversed(lines):
        if line.startswith('at '):
            continue  # 栈帧（文件路径里也可能带 Error 字样）
        head = line.split(':', 1)[0]
        if any(word in head for word in _ERROR_WORDS):
            return line[:limit]
    return lines[-1][:limit] if lines else ''


def _missing_inputs(host_script, extension, source_dir) -> str:
    if not host_script or not Path(str(host_script)).is_file():
        return 'hostScript 不是文件'
    if not extension or not Path(str(extension)).exists():
        return 'extensionPath 不存在'
    if not source_dir or not Path(str(source_dir)).is_dir():
        return 'sourceDir 不是目录'
    return ''


async def _terminate(proc) -> None:
    """超时先 kill（Windows 上是 TerminateProcess），再给 2 秒收尸。"""
    with contextlib.suppress(ProcessLookupError, OSError, AttributeError):
        proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), 2)


async def _drain(proc):
    with contextlib.suppress(Exception):
        return await asyncio.wait_for(proc.communicate(), 5)
    return b'', b''


def _coerce_extra_env(extra_env) -> Dict[str, str]:
    """把调用方的 extra_env 收成 {str: str}；None 等于没给，非 dict 一律 ValueError。

    键值统一转 str（子进程环境只能是字符串），QQ_AGENT_SOURCE 由本模块拼，不许被覆盖。
    形状不对就当场抛：注入通道写错要立刻在调用点暴露，而不是悄悄少一个键让扩展
    "看起来跑通了"。
    """
    if extra_env is None:
        return {}
    if not isinstance(extra_env, dict):
        raise ValueError('extra_env 必须是 dict 或 None，收到 %s'
                         % type(extra_env).__name__)
    extra: Dict[str, str] = {}
    for key, value in extra_env.items():
        name = str(key)
        if name == 'QQ_AGENT_SOURCE':
            raise ValueError('extra_env 不能覆盖 QQ_AGENT_SOURCE（由 source_dir 决定）')
        extra[name] = str(value)
    return extra


#: 父进程里可能残留、但**只能由调用方按次显式给**的环境键。父进程若还留着上次实验或
#: 手工设的 QQ_AGENT_CTX，直接继承会让 host-one 把不属于本轮的会话值塞给扩展；本模块
#: 只认 extra_env 里显式传进来的那个值。
INHERITED_ENV_BLOCKLIST = ('QQ_AGENT_CTX', 'QQ_AGENT_DEPENDENCY_IDS',
                           'QQ_AGENT_HOST_TIMEOUT_MS')


def _child_env(source_dir, extra, data_dir=None, timeout=DEFAULT_TIMEOUT) -> Dict[str, str]:
    """拼子进程环境：继承父进程，剔除残留键，再由 source_dir 与 extra_env 决定注入值。"""
    env = {key: value for key, value in os.environ.items()
           if key not in INHERITED_ENV_BLOCKLIST}
    env['QQ_AGENT_SOURCE'] = str(source_dir)
    # 依赖只读扩展管理的权威启用表。缺失/损坏时 enabled_ids 返回 []，绝不回退到全开。
    # extra_env 可为离线验收按次提供显式 allowlist；不会写回注册表或父进程环境。
    from . import extension_registry
    env['QQ_AGENT_DEPENDENCY_IDS'] = json.dumps(extension_registry.enabled_ids(data_dir),
                                                ensure_ascii=False)
    env.update(extra)
    # 给 Node 留少量清理窗口；Python 的强杀预算仍由 clamp_timeout 控制。
    env['QQ_AGENT_HOST_TIMEOUT_MS'] = str(max(100, int(clamp_timeout(timeout) * 1000) - 250))
    return env


def _cleanup_isolated_roots(source_dir, pid) -> None:
    """强杀后的兜底清理：仅删除带本次 pid/source 所有权标记的宿主临时根。"""
    if not isinstance(pid, int) or pid <= 0:
        return
    root = Path(str(source_dir)).resolve()
    for prefix in ('plugins-host-', 'skills-host-'):
        candidate = root / ('%s%d' % (prefix, pid))
        try:
            if candidate.is_symlink() or candidate.resolve().parent != root:
                continue
            marker = candidate / '.whale-host-one-owner.json'
            if marker.is_symlink() or marker.stat().st_size > 4096:
                continue
            owner = json.loads(marker.read_text(encoding='utf-8'))
            if owner.get('pid') != pid or Path(str(owner.get('source') or '')).resolve() != root:
                continue
            shutil.rmtree(candidate)
        except (OSError, ValueError, TypeError, AttributeError):
            pass


# ── 每次调用独占的工作目录 ───────────────────────────────────────────────────
#: 调用目录的父目录：<data_dir>/qq-only/extension_cwd/；data_dir 缺失时退到系统
#: 临时目录下的同名结构。扩展代码里的相对路径（fs.writeFileSync('data/x.json')）
#: 因此落在本次调用目录里，而不是继承宿主进程的 cwd 写进仓库根。
CALL_CWD_PARTS = ('qq-only', 'extension_cwd')


def call_cwd_root(data_dir=None) -> Path:
    """本次能力调用独占工作目录的父目录（只算路径，不创建）。

    调用方传进来的通常是 PluginManager.data_dir，它本身已是 <data>/qq-only；
    不剥掉这层就会落到 <data>/qq-only/qq-only/extension_cwd，和
    default_host_root 的落点错开一层。
    """
    base = Path(os.path.abspath(str(data_dir))) if data_dir else Path(tempfile.gettempdir())
    if base.name.lower() == 'qq-only':
        base = base.parent
    return base.joinpath(*CALL_CWD_PARTS)


def _make_call_cwd(data_dir=None) -> Tuple[Optional[Path], str]:
    """建一个本次调用独占的工作目录，返回 (path, reason)，绝不抛。

    mkdtemp 保证名字独占（<父 pid>-<随机后缀>），并发调用不会互相踩到。创建失败时
    返回 (None, 原因)：调用方回落成不传 cwd（子进程继承父进程目录，行为与改动前
    一致），原因写进返回值 cwd_error。
    """
    try:
        root = call_cwd_root(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix='%d-' % os.getpid(), dir=str(root))), ''
    except (OSError, ValueError, TypeError) as exc:
        return None, ('%s: %s' % (type(exc).__name__, exc))[:200]


def _drop_call_cwd(path) -> None:
    """删掉本次调用目录（含扩展写进去的文件与子目录）；删除失败静默。"""
    if not path:
        return
    with contextlib.suppress(OSError, ValueError, TypeError):
        shutil.rmtree(str(path), ignore_errors=True)


async def run_capability(*, node: str, host_script: str, extension: str, source_dir: str,
                         capability: str, capability_arg: str = '',
                         payload: Optional[dict] = None, timeout=DEFAULT_TIMEOUT,
                         data_dir=None, kind: str = 'provider', extension_id: str = '',
                         path_label: str = 'async',
                         extra_env: Optional[dict] = None) -> dict:
    """跑一次扩展能力调用；任何失败都收敛成结构化结果，不抛异常。

    kind 决定从回传 JSON 里取哪个字段：provider / hook / tool / list。
    extra_env 只加给这一个子进程（不改 os.environ），键值转 str，非 dict 抛 ValueError。
    """
    extra = _coerce_extra_env(extra_env)
    seconds = clamp_timeout(timeout)
    ident = str(extension_id or Path(str(extension or '')).name or '')
    started = time.perf_counter()
    result: Dict[str, Any] = {'ok': False, 'kind': kind, 'capability': capability,
                              'argument': capability_arg, 'extension': ident,
                              'value': None, 'raw': {}, 'error': '', 'timed_out': False,
                              'duration_ms': 0, 'pid': None, 'stderr': '',
                              'stderr_tail': ''}
    missing = _missing_inputs(host_script, extension, source_dir)
    if missing:
        result['error'] = missing
        result['duration_ms'] = int((time.perf_counter() - started) * 1000)
        _record(result, seconds, data_dir, path_label)
        return result
    # 每个子进程一个本次调用独占的工作目录：扩展里的相对路径写在这里，不落到仓库根。
    call_cwd, cwd_error = _make_call_cwd(data_dir)
    result['cwd'] = str(call_cwd or '')
    if cwd_error:
        # 建不出来不炸：回落成不传 cwd（子进程继承父进程目录，行为同改动前），原因进返回值。
        result['cwd_error'] = cwd_error
    if call_cwd:
        # 子进程不再继承父进程 cwd，三个以 cwd 为基准的入参先钉成绝对路径，否则 node
        # 侧的 path.resolve 会把相对路径解析到新目录里。回落时不改原值。
        source_dir = os.path.abspath(str(source_dir))
        host_script = os.path.abspath(str(host_script))
        extension = os.path.abspath(str(extension))
    argv = build_argv(str(node or 'node'), str(host_script), str(extension), capability,
                      capability_arg, payload)
    env = _child_env(source_dir, extra, data_dir=data_dir, timeout=seconds)
    try:
        proc = None
        out = err = b''
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env, cwd=str(call_cwd) if call_cwd else None)
            result['pid'] = proc.pid
            try:
                out, err = await asyncio.wait_for(proc.communicate(), seconds)
            except asyncio.TimeoutError:
                result['timed_out'] = True
                await _terminate(proc)
                out, err = await _drain(proc)
            except asyncio.CancelledError:
                await _terminate(proc)
                await _drain(proc)
                _cleanup_isolated_roots(source_dir, result['pid'])
                raise
        except (OSError, ValueError) as exc:
            result['error'] = ('%s: %s' % (type(exc).__name__, exc))[:200]
            result['duration_ms'] = int((time.perf_counter() - started) * 1000)
            _record(result, seconds, data_dir, path_label)
            return result
        stdout = out.decode('utf-8', 'replace') if isinstance(out, bytes) else str(out or '')
        stderr = err.decode('utf-8', 'replace') if isinstance(err, bytes) else str(err or '')
        result['stderr'] = _stderr_tail(stderr)
        # 完整尾巴单列一个字段：result['stderr'] 只留 400 字且被 no-json 文案二次截断，
        # 排障要看的原始 JS 异常常常正好被截掉。
        result['stderr_tail'] = _stderr_excerpt(stderr)
        result['duration_ms'] = int((time.perf_counter() - started) * 1000)
        if result['timed_out']:
            _cleanup_isolated_roots(source_dir, result['pid'])
            result['error'] = 'timeout after %gs（已强杀子进程）' % seconds
            _record(result, seconds, data_dir, path_label)
            return result
        data = parse_host_stdout(stdout)
        result['raw'] = data
        if not data:
            code = getattr(proc, 'returncode', None)
            # 拼的是原始 JS 异常那一行（不是 stderr 尾巴）：node 崩掉时尾巴只有堆栈和版本号。
            # stderr 为空时文案与改前完全一致：'no-json (rc=N)'。
            tail = (': ' + _stderr_error_line(stderr)) if (stderr or '').strip() else ''
            result['error'] = ('no-json (rc=%s)%s' % (code, tail))[:200]
            _record(result, seconds, data_dir, path_label)
            return result
        if data.get('loadable') is False or data.get('ok') is False:
            result['error'] = str(data.get('error') or 'extension-not-loadable')[:200]
            result['timed_out'] = data.get('error_code') == 'host-timeout'
            _record(result, seconds, data_dir, path_label)
            return result
        result['value'] = _value_for(kind, data)
        result['ok'] = True
        _record(result, seconds, data_dir, path_label)
        return result
    finally:
        # 成功、失败、超时、被强杀、被取消：本次调用目录一律删掉（删除失败静默）。
        _drop_call_cwd(call_cwd)


def run_capability_sync(*, extra_env: Optional[dict] = None, **kwargs) -> dict:
    """同步钩子（manager.run_on_pre_ai 那条链）用的入口。

    同步事件里不能 await，所以这里在独立线程里跑独立事件循环。没有事件循环在跑时
    直接用 asyncio.run；有循环在跑时主线程按 timeout+5s 兜住 join —— 调用方线程仍
    会被占用到最多那么多秒，桥因此给同步路径单独的短预算（preAiBudgetMs），并把
    真实耗时写进 jsonl。

    extra_env 与 run_capability 同义；这里先校验一次，坏形状在进线程之前就抛。
    """
    extra = _coerce_extra_env(extra_env)
    if extra:
        kwargs['extra_env'] = extra
    kwargs.setdefault('path_label', 'sync')
    seconds = clamp_timeout(kwargs.get('timeout'))
    if not _loop_running():
        try:
            return asyncio.run(run_capability(**kwargs))
        except RuntimeError as exc:
            return _failure(kwargs, '%s: %s' % (type(exc).__name__, exc))
    box: Dict[str, Any] = {}

    def _worker():
        try:
            box['value'] = asyncio.run(run_capability(**kwargs))
        except BaseException as exc:  # 线程里不能把异常漏出去
            box['error'] = ('%s: %s' % (type(exc).__name__, exc))[:200]

    thread = threading.Thread(target=_worker, name='qqagent-bridge-sync', daemon=True)
    thread.start()
    thread.join(seconds + SYNC_TIMEOUT_SLACK)
    if isinstance(box.get('value'), dict):
        return box['value']
    return _failure(kwargs, box.get('error') or 'sync-timeout')


# ── 失败日志：扩展没跑起来时把原因写进结构化日志（src/app_log.py）───────────
#: 同一 (source, 扩展, 能力, 错误类别) 的去重窗口（秒），与技能侧同口径。
FAILURE_LOG_WINDOW = 60.0
_FAILURE_LOG_AT: Dict[tuple, float] = {}

#: error 文本 → 稳定机器码；顺序有意义（sync-timeout 必须先于 timeout 命中）。
_ERROR_CODE_HINTS = (
    ('sync-timeout', 'sync-timeout'),
    ('timeout', 'timeout'),
    ('白名单', 'not-allowed'),
    ('不属于这个扩展', 'not-owned'),
    ('missing', 'missing-input'),
    ('cancel', 'cancelled'),
    ('no-json', 'no-json'),
    ('not-loadable', 'not-loadable'),
)


def _failure_log_gate(key: tuple) -> bool:
    """窗口内同一个 key 只放行一次；顺手清掉已出窗的旧键，防长期累积。"""
    now = time.monotonic()
    last = _FAILURE_LOG_AT.get(key)
    if last is not None and now - last < FAILURE_LOG_WINDOW:
        return False
    _FAILURE_LOG_AT[key] = now
    if len(_FAILURE_LOG_AT) > 256:
        for stale in [k for k, at in _FAILURE_LOG_AT.items()
                      if now - at >= FAILURE_LOG_WINDOW]:
            _FAILURE_LOG_AT.pop(stale, None)
    return True


def error_code_of(error) -> str:
    """error 文本 → 机器可读类别（取不到就是空串）。"""
    text = str(error or '').lower()
    for needle, code in _ERROR_CODE_HINTS:
        if needle.lower() in text:
            return code
    return ''


def _failure_detail(value) -> str:
    """结构化返回压成一段文本：JSON 优先，退到 repr，再截到 DETAIL_MAX。"""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = repr(value)
    return text[:app_log.DETAIL_MAX]


def _log_extension_failure(data_dir, *, extension_id, capability, kind, code,
                           message, payload) -> None:
    """扩展能力失败落一条 error（source='extension'）；只吞不抛，不影响返回值。"""
    try:
        target = str(extension_id or capability or '')
        if not _failure_log_gate(('extension', target, str(capability or ''), code)):
            return
        app_log.default_app_log(data_dir).error(
            'extension', str(message)[:app_log.MESSAGE_MAX], detail=_failure_detail(payload),
            code=str(code or '')[:app_log.CODE_MAX],
            extra={'capability': str(capability or ''), 'kind': str(kind or '')})
    except Exception:
        pass


def _failure(kwargs: dict, error: str) -> dict:
    outcome = {'ok': False, 'error': error, 'kind': kwargs.get('kind', 'provider'),
               'value': None, 'raw': {}, 'timed_out': False, 'duration_ms': 0,
               'extension': str(kwargs.get('extension_id') or ''), 'stderr': '',
               'stderr_tail': '', 'cwd': ''}
    capability = kwargs.get('capability') or ''
    _log_extension_failure(kwargs.get('data_dir'), extension_id=outcome['extension'],
                           capability=capability, kind=outcome['kind'],
                           code=error_code_of(error),
                           message='扩展能力 %s 没跑起来：%s' % (capability or '(未命名)',
                                                              str(error or '未知原因')[:300]),
                           payload=outcome)
    return outcome


def _loop_running() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


# ── tool 面：枚举与调用 ──────────────────────────────────────────────────────
def owned_tools(data: dict, extension_id: str = '') -> List[dict]:
    """只留该扩展自己注册的 tool。

    host-one 的 buildToolDefs() 会把 0.4 宿主自带工具（list_stickers 等）一起吐
    出来，那些工具需要真实宿主运行时才有意义。这里按 host-one 回传的
    extension.tools 与 "<扩展 id>__" 前缀双重过滤，避免把宿主工具误当成扩展能力。
    """
    ext = data.get('extension') or {}
    declared = {str(item) for item in (ext.get('tools') or [])}
    prefix = ('%s__' % extension_id) if extension_id else ''
    result = []
    for tool in data.get('tools') or []:
        if not isinstance(tool, dict) or not tool.get('id'):
            continue
        tool_id = str(tool['id'])
        if tool_id in declared or (prefix and tool_id.startswith(prefix)):
            result.append({'id': tool_id, 'name': str(tool.get('name') or ''),
                           'description': str(tool.get('description') or '')})
    return result


async def list_tools(*, node: str, host_script: str, extension: str, source_dir: str,
                     timeout=DEFAULT_TIMEOUT, data_dir=None, extension_id: str = '',
                     extra_env: Optional[dict] = None) -> dict:
    """枚举一个扩展注册的 tool；返回 {'ok', 'tools': [...], 'error'}。"""
    extra = _coerce_extra_env(extra_env)
    run = await run_capability(node=node, host_script=host_script, extension=extension,
                               source_dir=source_dir, capability='', capability_arg='',
                               payload={}, timeout=timeout, data_dir=data_dir, kind='list',
                               extension_id=extension_id, extra_env=extra)
    return {'ok': bool(run.get('ok')), 'tools': owned_tools(run.get('raw') or {}, extension_id),
            'error': run.get('error') or '', 'extension': run.get('extension') or extension_id,
            'duration_ms': run.get('duration_ms') or 0}


def _log_tool_failure(data_dir, base: dict, tool_id: str, extension_id: str) -> None:
    """tool 面失败出口的唯一日志钩子（白名单、越权、子进程失败共用）。"""
    _log_extension_failure(data_dir, extension_id=extension_id, capability=tool_id, kind='tool',
                           code=error_code_of(base.get('error')),
                           message='扩展工具 %s 没跑起来：%s' % (
                               tool_id or '(未命名)', str(base.get('error') or '未知原因')[:300]),
                           payload=base)


async def call_tool(*, node: str, host_script: str, extension: str, source_dir: str,
                    tool_id: str, arguments: Optional[dict] = None,
                    timeout=DEFAULT_TIMEOUT, data_dir=None, extension_id: str = '',
                    allowed_ids: Optional[Iterable[str]] = None,
                    extra_env: Optional[dict] = None) -> dict:
    """调用一个扩展的 tool；白名单不命中就不启动进程。

    extra_env 只加给这一个子进程（见 run_capability）；非 dict 抛 ValueError。
    """
    extra = _coerce_extra_env(extra_env)
    tool_id = str(tool_id or '')
    base = {'ok': False, 'value': None, 'tool': tool_id, 'extension': extension_id,
            'duration_ms': 0, 'error': '', 'raw': {}}
    if allowed_ids is not None and tool_id not in {str(x) for x in allowed_ids}:
        base['error'] = 'tool 不在该扩展的白名单里'
        _log_tool_failure(data_dir, base, tool_id, extension_id)
        return base
    if extension_id and not tool_id.startswith('%s__' % extension_id):
        base['error'] = 'tool id 不属于这个扩展'
        _log_tool_failure(data_dir, base, tool_id, extension_id)
        return base
    run = await run_capability(node=node, host_script=host_script, extension=extension,
                               source_dir=source_dir, capability='tool', capability_arg=tool_id,
                               payload=arguments or {}, timeout=timeout, data_dir=data_dir,
                               kind='tool', extension_id=extension_id, extra_env=extra)
    base.update(ok=bool(run.get('ok')), value=run.get('value'),
                error=run.get('error') or '', duration_ms=run.get('duration_ms') or 0,
                raw=run.get('raw') or {})
    if not base['ok']:
        _log_tool_failure(data_dir, base, tool_id, extension_id)
    return base


# ── 记账：data/qqagent_bridge_calls.jsonl ────────────────────────────────────
def default_data_dir() -> Path:
    """未显式指定时落到 src.data_root 下的 data/（源码=仓库根/data）。

    以前是 __file__ 的上两级；冻结后 __file__ 在解包目录 _internal 里，写进去等于
    写安装目录，所以改走与 DATA_ROOT 同一个函数（冻结=%LOCALAPPDATA%/WhaleGirlQQ/data）。
    """
    from .data_root import resolve_data_root
    return resolve_data_root() / 'data'


def log_path(data_dir=None) -> Path:
    return Path(data_dir or default_data_dir()) / LOG_NAME


def _record(result: dict, seconds: float, data_dir, path_label: str) -> None:
    """每次 provider/hook/tool 调用写一行；日志本身出问题绝不打断调用。"""
    entry = {'ts': round(time.time(), 3), 'time': time.strftime('%Y-%m-%dT%H:%M:%S'),
             'kind': result.get('kind') or '', 'extension': result.get('extension') or '',
             'capability': result.get('argument') or result.get('capability') or '',
             'ok': bool(result.get('ok')), 'timed_out': bool(result.get('timed_out')),
             'duration_ms': int(result.get('duration_ms') or 0),
             'timeout_s': round(seconds, 2), 'path': path_label,
             'pid': result.get('pid'),
             'error': str(result.get('error') or '')[:200]}
    if result.get('stderr_tail'):
        # 只在真有 stderr 时补这一列：老账本行没有该字段也不影响读。
        entry['stderr_tail'] = str(result['stderr_tail'])[:STDERR_TAIL_CHARS]
    # 新增可选列：本次调用的独占工作目录（扩展的相对路径落点），以及建不出来时的原因。
    if result.get('cwd'):
        entry['cwd'] = str(result['cwd'])
    if result.get('cwd_error'):
        entry['cwd_error'] = str(result['cwd_error'])[:200]
    try:
        path = log_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > LOG_MAX_BYTES:
            with contextlib.suppress(OSError):
                os.replace(str(path), str(path) + '.1')
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + chr(10))
    except OSError:
        pass


def read_log(data_dir=None, limit: int = 4000) -> List[dict]:
    """读最近若干条调用记录（只读文件尾部，不整文件载入）。"""
    path = log_path(data_dir)
    try:
        with path.open('rb') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - STATUS_TAIL_BYTES))
            chunk = handle.read().decode('utf-8', 'replace')
    except OSError:
        return []
    rows = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue  # 尾部可能截断半行
        if isinstance(item, dict):
            rows.append(item)
    return rows[-max(1, int(limit or 4000)):]


def bridge_status(data_dir=None, enabled_extensions: Optional[Iterable[str]] = None,
                  limit_errors: int = 5) -> dict:
    """给控制台端点 / WP2 验收用的状态快照。"""
    rows = read_log(data_dir)
    path = log_path(data_dir)
    status = {'enabled_extensions': sorted({str(x) for x in (enabled_extensions or []) if str(x)}),
              'provider_calls': 0, 'provider_failures': 0,
              'hook_calls': 0, 'hook_failures': 0,
              'tool_calls': 0, 'tool_failures': 0,
              'calls': len(rows), 'last_errors': [],
              'log_path': str(path), 'log_exists': path.is_file()}
    for row in rows:
        bucket = {'provider': 'provider', 'hook': 'hook', 'tool': 'tool'}.get(str(row.get('kind') or ''))
        if not bucket:
            continue
        status['%s_calls' % bucket] += 1
        if not row.get('ok'):
            status['%s_failures' % bucket] += 1
    for row in reversed(rows):
        if row.get('ok'):
            continue
        status['last_errors'].append({'ts': row.get('ts'), 'time': row.get('time') or '',
                                      'kind': row.get('kind') or '',
                                      'extension': row.get('extension') or '',
                                      'capability': row.get('capability') or '',
                                      'error': str(row.get('error') or '')[:200]})
        if len(status['last_errors']) >= max(1, int(limit_errors or 5)):
            break
    return status


# ── 市场清单发现（补 hooks / tools 字段） ────────────────────────────────────
MANIFEST_NAMES = ('all-plugins-v4.json', 'all-plugins-v3.json', 'all-plugins-v2.json')
HOOKS_MAP_NAMES = ('hooks-map.json',)


def _load_json(path) -> Any:
    try:
        return json.loads(Path(str(path)).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _as_list(value) -> List[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, (list, tuple)):
        return []
    names = []
    for item in value:
        if isinstance(item, str) and item:
            names.append(item)
        elif isinstance(item, dict):
            name = str(item.get('name') or item.get('id') or '')
            if name:
                names.append(name)
    return names


def sibling_files(catalog_path, names) -> List[Path]:
    """catalog.json 旁边那几个旁挂清单（市场导出时一起生成的）。"""
    folder = Path(str(catalog_path or '')).parent
    if str(folder) in ('', '.'):
        return []
    return [folder / name for name in names if (folder / name).is_file()]


def load_hooks_map(catalog_path) -> dict:
    """hooks-map.json：0.4 hook 名 → Python 事件名的对照表（缺则空）。"""
    for path in sibling_files(catalog_path, HOOKS_MAP_NAMES):
        data = _load_json(path)
        if isinstance(data, dict) and isinstance(data.get('hooks'), dict):
            return data
    return {}


def discover_extensions(catalog_path, extensions_dir='', manifest_paths=None) -> List[dict]:
    """把市场清单补成"带 hooks / tools 的可选扩展"。

    catalog.json 只有 providers（写在 capabilities 里）和 tools 的**名字**，没有
    hooks 字段 —— 这正是 296 个 hook 型扩展在自动模式下不可见的原因。这里按
    catalog → all-plugins-v4.json（extension.hooks）的顺序回退，缺失就当空列表，
    绝不推测。
    """
    catalog = _load_json(catalog_path)
    if catalog is None:
        return []
    items = catalog if isinstance(catalog, list) else (
        catalog.get('plugins') or catalog.get('items') or catalog.get('extensions') or [])
    if not isinstance(items, list):
        return []
    sidecars: Dict[str, dict] = {}
    for path in (manifest_paths if manifest_paths is not None
                 else sibling_files(catalog_path, MANIFEST_NAMES)):
        data = _load_json(path)
        if not isinstance(data, list):
            continue
        for row in data:
            if not isinstance(row, dict) or not row.get('id'):
                continue
            ext = row.get('extension') or {}
            sidecars.setdefault(str(row['id']), ext if isinstance(ext, dict) else {})
    base = Path(str(extensions_dir)).resolve() if extensions_dir else None
    result = []
    for item in items:
        if not isinstance(item, dict) or not item.get('id'):
            continue
        if str(item.get('kind', 'plugin')) != 'plugin':
            continue
        extension = Path(str(item.get('file') or ''))
        # 宿主根里装好的扩展是一个目录（<host_root>/plugins/<id>），host-one.mjs
        # 接受目录，所以这里也收目录；只认文件会让市场装进宿主的扩展全体失联。
        if not (extension.is_file() or extension.is_dir()):
            continue
        if base is not None:
            try:
                extension.resolve().relative_to(base)
            except (OSError, ValueError):
                continue
        ident = str(item['id'])
        side = sidecars.get(ident) or {}
        result.append({'id': ident,
                       'name': str(item.get('name') or side.get('name') or ident),
                       'file': extension,
                       'host_imports': _as_list(item.get('host_imports')),
                       'providers': _as_list(item.get('capabilities')) or _as_list(side.get('providers')),
                       'hooks': _as_list(item.get('hooks')) or _as_list(side.get('hooks')),
                       'tools': _as_list(item.get('tools')) or _as_list(side.get('tools'))})
    return result
