"""W3：主体 agent 的受控工作区（沙箱 + 一次性授权令牌 + 全量写审计）。

产品新方向是 Agent 平台：主体 agent（ConsoleAgent）需要人设、记事本、草稿与对话
转写落盘，但不能因此获得整盘写权限。本模块只做三件事：

1. 目录约定：<data_dir>/agent_workspace/<agentId>/ 下放
   notes.md / notes/ / persona/ / drafts/ / transcripts/。
2. 沙箱：默认只允许写这个目录；任何越界目标必须先拿到一次性授权令牌。
3. 审计：每次写（含被拒）追加一行 jsonl 到 <data_dir>/qq-only/agent_workspace_audit.jsonl。

权限闸不另造模型：审批登记/校验直接用 src.local_agent.policy.ActionPolicy
（令牌绑 param_hash + 设备 + 主体 + 有效期 + 策略版本，执行后 consume()
防重放），本模块只提供 param_hash 的算法与路径边界判定：

    param_hash = sha256(agentId + '\n' + 规范化 relpath + '\n' + 内容哈希 + '\n' + op)[:32]

与参考实现（local_agent/windows_executor.py 的路径校验）一致，越界判定
解析最终路径并逐级检查重解析点，拒绝：'..' 穿越、UNC、盘符相对路径、
控制字符、符号链接、junction。写盘前再确认一次。

边界取舍（有意为之，见 W3 报告）：
- '..' / UNC / C:xxx 盘符相对 / 控制字符 / 重解析点 = 一律拒绝，持令牌也不行
  （批准不能把路径歧义合法化）。
- 绝对路径（越出工作区）只有在持有效令牌时放行一次；令牌过期、已消耗、
  内容/路径/操作任一变化都结构化拒绝。
- 读取永远不出沙箱（没有"读越界"这条路）。

结构化返回：成功 {'ok': True, ...}，失败 {'ok': False, 'error': {'code','message'}}。
resolve() 是唯一抛异常的函数（WorkspaceError，带同样的结构化载体）。
import 期不创建任何目录；只有真正写文件时才 mkdir(parents=True, exist_ok=True)。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat as stat_module
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .audio_contracts import ContractError
from .local_agent.contracts import ActionProposal
from .local_agent.policy import ActionPolicy

__all__ = [
    'AGENT_ID_PATTERN', 'MAX_FILE_BYTES', 'AGENT_WORKSPACE_AUDIT_MAX_BYTES',
    'WORKSPACE_AREAS', 'WRITE_OPS', 'WorkspaceError',
    'root', 'resolve', 'read_text', 'write_text', 'append_text', 'list_dir',
    'stats', 'grant_write', 'audit_tail', 'audit_path', 'param_hash',
]

AGENT_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
WORKSPACE_DIR_NAME = 'agent_workspace'
AUDIT_DIR_NAME = 'qq-only'
AUDIT_FILE_NAME = 'agent_workspace_audit.jsonl'
AUDIT_PREVIOUS_NAME = 'agent_workspace_audit.previous.jsonl'
MAX_FILE_BYTES = 1024 * 1024
AGENT_WORKSPACE_AUDIT_MAX_BYTES = 8 * 1024 * 1024
WORKSPACE_AREAS = ('notes.md', 'notes', 'persona', 'drafts', 'transcripts')
WRITE_OPS = frozenset({'write', 'append'})

_CONTROL = re.compile(r'[\x00-\x1f\x7f]')
_REPARSE_ATTRIBUTE = int(getattr(stat_module, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))
_CONSUMED: 'deque[str]' = deque(maxlen=512)     # 已消耗令牌（区分“重放”与“从未批准”）
_POLICIES: dict = {}                            # 只放内存对象，import 期不建目录

_CONTRACT_CODES = {
    'APPROVAL_EXPIRED': 'expired',
    'UNAUTHORIZED': 'unauthorized',
    'POLICY_DENIED': 'denied',
    'INVALID_INPUT': 'invalid',
    'CAPABILITY_UNSUPPORTED': 'denied',
}


class WorkspaceError(Exception):
    """结构化拒绝：to_dict() 给出 {'ok': False, 'error': {...}} 载体。"""

    def __init__(self, code: str, message: str, **details: Any):
        self.code = str(code)
        self.message = str(message)
        self.details = {str(key): str(value) for key, value in details.items()}
        super().__init__(f'{self.code}: {self.message}')

    def to_dict(self) -> dict:
        error: dict = {'code': self.code, 'message': self.message}
        if self.details:
            error['details'] = dict(self.details)
        return {'ok': False, 'error': error}


def _fail(code: str, message: str, **details: Any) -> None:
    raise WorkspaceError(code, message, **details)


def _contract_dict(exc: ContractError) -> dict:
    code = getattr(exc.code, 'value', str(exc.code))
    text = str(exc)
    message = text.split(': ', 1)[1] if ': ' in text else text
    return WorkspaceError(_CONTRACT_CODES.get(code, 'denied'), message).to_dict()


# ── 路径约定与越界判定 ──────────────────────────────────────────────────────

def _data_base(data_dir) -> Path:
    raw = data_dir
    if isinstance(raw, Path):
        raw = str(raw)
    if not isinstance(raw, str) or not raw.strip():
        _fail('invalid', 'data_dir 必须是非空路径')
    if _CONTROL.search(raw):
        _fail('invalid', 'data_dir 含控制字符')
    return Path(os.path.abspath(raw))


def _agent_id(agent_id) -> str:
    raw = agent_id
    if isinstance(raw, Path):
        raw = str(raw)
    if not isinstance(raw, str) or not AGENT_ID_PATTERN.fullmatch(raw):
        _fail('invalid', 'agentId 只允许 [A-Za-z0-9_-]{1,64}')
    return raw


@dataclass(frozen=True)
class _Target:
    """规范化目标：norm 进 param_hash/审计，inside 决定是否需要令牌。"""

    norm: str
    path: Path
    inside: bool


def root(data_dir, agent_id) -> Path:
    """<data_dir>/agent_workspace/<agentId>；只算路径，不建目录。"""
    return _data_base(data_dir) / WORKSPACE_DIR_NAME / _agent_id(agent_id)


def _inside(base: Path, target: Path) -> bool:
    try:
        Path(os.path.normcase(os.path.abspath(str(target)))).relative_to(
            Path(os.path.normcase(os.path.abspath(str(base)))))
        return True
    except ValueError:
        return False


def _classify(workspace_root: Path, relpath, *, allow_root: bool = False) -> _Target:
    """把 relpath 规范化成目标；结构上不可接受的形状直接拒绝。"""
    raw = relpath
    if isinstance(raw, Path):
        raw = str(raw)
    if not isinstance(raw, str):
        _fail('invalid', '路径必须是字符串')
    if not raw.strip():
        if allow_root:
            return _Target('', workspace_root, True)
        _fail('denied', '路径为空')
    if _CONTROL.search(raw):
        _fail('denied', '路径含控制字符')
    unified = raw.replace('\\', '/')
    if unified.startswith('//'):
        _fail('denied', '不允许 UNC 网络路径')
    drive = ''
    matched = re.match(r'^([A-Za-z]):(.*)$', unified)
    if matched:
        drive = matched.group(1).upper() + ':'
        tail = matched.group(2)
        if not tail.startswith('/'):
            _fail('denied', '不允许盘符相对路径（C:xxx 存在歧义）')
        unified = tail
    rooted = unified.startswith('/')
    segments = unified.split('/')
    if '..' in segments:
        _fail('denied', "路径不允许 '..' 穿越")
    parts = [segment for segment in segments if segment not in ('', '.')]
    if not parts:
        if allow_root:
            return _Target('', workspace_root, True)
        _fail('denied', '路径未指向任何文件')
    relative = '/'.join(parts)
    if drive or rooted:
        if not drive and os.name == 'nt':
            _fail('denied', '不允许根相对路径（缺少盘符）')
        if drive:
            candidate = Path(drive + '\\' + relative.replace('/', '\\'))
        else:
            candidate = Path('/' + relative)
        return _Target(str(candidate), candidate, _inside(workspace_root, candidate))
    return _Target(relative, workspace_root.joinpath(*parts), True)


def _is_reparse(item: Path) -> bool:
    try:
        info = os.lstat(str(item))
    except OSError:
        return False
    if int(getattr(info, 'st_file_attributes', 0)) & _REPARSE_ATTRIBUTE:
        return True
    if os.path.islink(str(item)):
        return True
    junction = getattr(os.path, 'isjunction', None)
    if junction is not None:
        try:
            return bool(junction(str(item)))
        except OSError:
            return False
    return False


def _assert_no_reparse(base: Path, target: Path) -> None:
    """从 base 逐级到 target（含 target 自身）检查符号链接 / junction。"""
    base_text = os.path.normcase(os.path.abspath(str(base)))
    current = Path(os.path.abspath(str(target)))
    chain: list = []
    while True:
        chain.append(current)
        if os.path.normcase(str(current)) == base_text:
            break
        parent = current.parent
        if os.path.normcase(str(parent)) == os.path.normcase(str(current)):
            break                      # 已到盘符根仍未命中 base（越界已由别处拒绝）
        current = parent
    for item in reversed(chain):
        if _is_reparse(item):
            _fail('denied', f'路径包含符号链接或 junction：{item.name}')


def _assert_confined(workspace_root: Path, target: Path) -> None:
    """工作区内目标：逐级查重解析点 + 解析最终路径做 relative_to 兜底。"""
    _assert_no_reparse(workspace_root, target)
    resolved_root = Path(os.path.realpath(str(workspace_root)))
    resolved_target = Path(os.path.realpath(str(target)))
    if not _inside(resolved_root, resolved_target):
        _fail('denied', '解析后的最终路径越出 agent 工作区')


def resolve(data_dir, agent_id, relpath) -> Path:
    """工作区内的绝对路径；越界一律抛 WorkspaceError（永不越界）。"""
    workspace_root = root(data_dir, agent_id)
    target = _classify(workspace_root, relpath, allow_root=True)
    if not target.inside:
        _fail('denied', '路径越出 agent 工作区', path=target.norm)
    _assert_confined(workspace_root, target.path)
    return target.path


# ── 权限闸：复用 ActionPolicy（令牌绑 param_hash/设备/主体/有效期/策略版本）──

class WorkspacePolicy(ActionPolicy):
    """工作区写操作按 L1 登记；越界写额外要求一次性令牌（见 grant_write）。"""

    def is_first_release_allowed(self, action: str) -> bool:
        return str(action) in WRITE_OPS


@dataclass
class _WorkspaceProposal(ActionProposal):
    """把工作区的 param_hash 交给 ActionPolicy.authorize 走同一条查表。"""

    workspace_hash: str = ''

    def param_hash(self) -> str:
        return str(self.workspace_hash)


def _device_id(workspace_root: Path) -> str:
    """把“这个 agent 的这个工作区”当成设备，审批令牌因此绑死到具体目录。"""
    digest = hashlib.sha256(str(workspace_root).encode('utf-8')).hexdigest()[:16]
    return 'ws-' + digest


def _policy_for(workspace_root: Path) -> WorkspacePolicy:
    key = os.path.normcase(os.path.abspath(str(workspace_root)))
    policy = _POLICIES.get(key)
    if policy is None:
        policy = WorkspacePolicy(policy_version=1, approval_ttl=120.0)
        _POLICIES[key] = policy
    return policy


def param_hash(agent_id: str, relpath: str, content_sha256: str, op: str) -> str:
    """sha256(agentId + 规范化 relpath + 内容哈希 + op) 的前 32 位。"""
    raw = '\n'.join((str(agent_id), str(relpath), str(content_sha256), str(op)))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _encode_text(text) -> bytes:
    if not isinstance(text, str):
        _fail('invalid', '正文必须是字符串')
    try:
        return text.encode('utf-8')
    except UnicodeEncodeError:
        _fail('encoding', '正文无法编码为 UTF-8')


def _token_of(grant) -> str:
    if grant is None:
        return ''
    if isinstance(grant, str):
        return grant.strip()
    if isinstance(grant, dict):
        value = grant.get('param_hash')
        return str(value).strip() if value else ''
    _fail('invalid', 'grant 必须是 param_hash 字符串或 grant_write 的返回结构')


def _authorize_token(policy: WorkspacePolicy, workspace_root: Path, expected: str,
                     token: str, principal: str, now: Optional[float],
                     op: str) -> None:
    if token in _CONSUMED:
        _fail('replayed', '一次性令牌已被使用（防重放）')
    approval = policy.approvals.get(token)
    if approval is None:
        _fail('unauthorized', '令牌无效或未经本地批准')
    proposal = _WorkspaceProposal(
        job_id='agent-workspace', device_id=_device_id(workspace_root), action=str(op),
        args={'agent': workspace_root.name, 'token': token}, expires_at=0.0,
        policy_version=policy.policy_version, nonce=token, workspace_hash=token)
    policy.authorize(proposal, now=now)          # 过期/设备/策略版本由 ActionPolicy 判定
    if approval.param_hash != expected:
        _fail('denied', '令牌参数不匹配（agent/路径/内容/操作已变化，需重新授权）')
    if principal and approval.principal and str(principal) != approval.principal:
        _fail('unauthorized', '令牌主体与本次调用不一致')


def _consume(policy: WorkspacePolicy, token: str) -> None:
    policy.consume(token)
    _CONSUMED.append(token)


def grant_write(data_dir, agent_id, relpath, *, principal: str, ttl: float = 120.0,
                content: str = '', op: str = 'write') -> dict:
    """为“这一次、这个内容、这个 op”签发越界写令牌。

    返回 {'ok': True, 'param_hash', 'expires_at', ...}；令牌一次性、到期作废。
    """
    try:
        agent = _agent_id(agent_id)
        base = _data_base(data_dir)
        workspace_root = base / WORKSPACE_DIR_NAME / agent
        target = _classify(workspace_root, relpath)
        if str(op) not in WRITE_OPS:
            _fail('invalid', '授权只支持 write / append')
        digest = _sha256(_encode_text(content))
        token = param_hash(agent, target.norm, digest, str(op))
        policy = _policy_for(workspace_root)
        approval = policy.grant(
            token, device_id=_device_id(workspace_root), principal=str(principal or ''),
            expires_at=time.time() + float(ttl))
    except WorkspaceError as exc:
        return exc.to_dict()
    except ContractError as exc:
        return _contract_dict(exc)
    except (TypeError, ValueError):
        return WorkspaceError('invalid', 'ttl 必须是数字').to_dict()
    return {'ok': True, 'param_hash': token, 'expires_at': float(approval.expires_at),
            'agent': agent, 'path': target.norm, 'op': str(op),
            'inside': target.inside, 'sha256': digest,
            'principal': approval.principal}


# ── 审计 ────────────────────────────────────────────────────────────────────

def audit_path(data_dir) -> Path:
    """审计文件位置；只算路径，不建目录。"""
    return _data_base(data_dir) / AUDIT_DIR_NAME / AUDIT_FILE_NAME


def _audit(data_base: Optional[Path], entry: dict) -> bool:
    if data_base is None:
        return False
    try:
        path = audit_path(data_base)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size >= int(AGENT_WORKSPACE_AUDIT_MAX_BYTES):
            os.replace(str(path), str(path.with_name(AUDIT_PREVIOUS_NAME)))
        line = json.dumps(entry, ensure_ascii=False, sort_keys=False)
        with open(path, 'a', encoding='utf-8', newline='\n') as handle:
            handle.write(line + '\n')
        return True
    except OSError:
        return False      # 审计不可用不反向阻塞业务，但结果里如实反映


# 越界写的审计闸：审计是越界写的**前置条件**，记不进去就不许落盘、不消耗令牌。
# 界内写不走这里 —— 界内写审计失败只记 audited=False 的老语义保持不变。
_AUDIT_GATE = {'fails': 0, 'muted': False}
AUDIT_GATE_MUTE_AFTER = 3


def _audit_safe(data_base: Optional[Path], entry: dict) -> bool:
    """_audit 的兜底版：审计实现自己抛异常时按"没记上"处理，返回 False。

    界内写靠它保持老语义 —— 文件已经写成功，结果如实报 audited=False，
    不把审计实现自己的异常抛给调用方。
    """
    try:
        return bool(_audit(data_base, entry))
    except Exception:
        return False


def _audit_gate(data_base: Optional[Path], entry: dict) -> bool:
    """越界写专用：审计必须真的落盘，返回 True 才允许写目标文件。

    失败原因写 stderr（便于用户知道"为什么写不进去"），但同一进程内连续失败
    超过 AUDIT_GATE_MUTE_AFTER 次后不再逐条打印 —— 审计目录坏掉时不该反过来
    把日志刷爆；审计恢复成功即复位。
    """
    try:
        ok = bool(_audit(data_base, entry))
        detail = '审计文件不可写'
    except Exception as exc:            # 审计实现抛异常与"写不进去"同等对待
        ok = False
        detail = f'{type(exc).__name__}: {exc}'
    if ok:
        _AUDIT_GATE['fails'] = 0
        _AUDIT_GATE['muted'] = False
        return True
    _AUDIT_GATE['fails'] += 1
    if not _AUDIT_GATE['muted']:
        print(f'[agent_workspace] 越界写被拒：审计落盘失败（第 {_AUDIT_GATE["fails"]} 次）{detail}',
              file=sys.stderr)
        if _AUDIT_GATE['fails'] >= AUDIT_GATE_MUTE_AFTER:
            _AUDIT_GATE['muted'] = True
            print('[agent_workspace] 审计连续失败，后续同类失败不再逐条打印；修好审计目录后自动恢复。',
                  file=sys.stderr)
    return False


def audit_tail(data_dir, limit: int = 50) -> list:
    """最近 limit 行审计（新的在后）；文件不存在时返回空列表。"""
    try:
        path = audit_path(data_dir)
        size = path.stat().st_size
    except (WorkspaceError, OSError):
        return []
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 50
    window = max(64 * 1024, limit * 512)
    start = max(0, size - window)
    try:
        with open(path, 'rb') as handle:
            handle.seek(start)
            blob = handle.read()
    except OSError:
        return []
    lines = [line for line in blob.decode('utf-8', 'replace').split('\n') if line.strip()]
    if start > 0 and lines:
        lines = lines[1:]                      # 丢掉被窗口截断的首行
    entries: list = []
    for line in lines[-limit:]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


# ── 写 ──────────────────────────────────────────────────────────────────────

def _write_bytes(target: _Target, payload: bytes, op: str, workspace_root: Path) -> None:
    path = target.path
    if path.exists() and path.is_dir():
        _fail('invalid', '目标是一个已存在的目录')

    def _prepare_parent() -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _fail('io', '创建目录失败', error=type(exc).__name__)

    def _recheck() -> None:
        if target.inside:
            _assert_confined(workspace_root, path)
        else:
            _assert_no_reparse(Path(path.anchor or os.sep), path)

    if op == 'write':
        _recheck()                             # 先确认，别让 mkdir 顺着软链在外面建目录
        _prepare_parent()
        _recheck()                             # 写盘前再确认一次
        try:
            with open(path, 'wb') as handle:
                handle.write(payload)
        except OSError as exc:
            _fail('io', '写入失败', error=type(exc).__name__)
        return

    existing = 0
    if path.exists():
        try:
            existing = int(path.stat().st_size)
        except OSError as exc:
            _fail('io', '读取目标大小失败', error=type(exc).__name__)
    if existing + len(payload) > MAX_FILE_BYTES:
        _fail('too_large', f'追加后超过单文件上限 {MAX_FILE_BYTES} 字节')
    _recheck()
    _prepare_parent()
    _recheck()
    try:
        with open(path, 'ab') as handle:
            handle.write(payload)
    except OSError as exc:
        _fail('io', '追加失败', error=type(exc).__name__)


def _write(data_dir, agent_id, relpath, text, *, op: str, grant, principal: str,
           now: Optional[float]) -> dict:
    entry = {'ts': 0.0, 'agent': '', 'op': str(op), 'path': '', 'ok': False,
             'code': '', 'principal': str(principal or ''), 'param_hash': ''}
    data_base: Optional[Path] = None
    token = ''
    preaudited = False
    try:
        entry['ts'] = time.time()
        token = _token_of(grant)
        entry['param_hash'] = token
        agent = _agent_id(agent_id)
        entry['agent'] = agent
        data_base = _data_base(data_dir)
        workspace_root = data_base / WORKSPACE_DIR_NAME / agent
        payload = _encode_text(text)
        if len(payload) > MAX_FILE_BYTES:
            _fail('too_large', f'单文件上限 {MAX_FILE_BYTES} 字节')
        target = _classify(workspace_root, relpath)
        entry['path'] = target.norm
        digest = _sha256(payload)
        expected = param_hash(agent, target.norm, digest, str(op))
        policy = _policy_for(workspace_root)
        if token:
            _authorize_token(policy, workspace_root, expected, token, principal, now,
                             str(op))
            if not target.inside:
                # 越界写：审计先落盘，成功了才写目标文件（令牌此时还没消耗）。
                entry['ok'] = True
                if not _audit_gate(data_base, entry):
                    entry['ok'] = False
                    refused = WorkspaceError(
                        'audit_failed',
                        '审计写入失败，已拒绝本次越界写（目标文件未改动；界内写不受影响）').to_dict()
                    refused['code'] = 'audit_failed'
                    return refused
                preaudited = True
        elif not target.inside:
            _fail('denied', '越界写需要一次性授权令牌')
        _write_bytes(target, payload, str(op), workspace_root)
        if token:
            _consume(policy, token)
    except WorkspaceError as exc:
        entry['code'] = exc.code
        _audit_safe(data_base, entry)
        return exc.to_dict()
    except ContractError as exc:
        entry['code'] = _CONTRACT_CODES.get(getattr(exc.code, 'value', ''), 'denied')
        _audit_safe(data_base, entry)
        return _contract_dict(exc)
    except OSError as exc:
        entry['code'] = 'io'
        _audit_safe(data_base, entry)
        return WorkspaceError('io', '文件操作失败', error=type(exc).__name__).to_dict()
    entry['ok'] = True
    entry['code'] = ''
    audited = True if preaudited else _audit_safe(data_base, entry)
    return {'ok': True, 'path': str(target.path), 'bytes': len(payload),
            'sha256': digest, 'param_hash': token, 'op': str(op),
            'inside': target.inside, 'audited': audited}


def write_text(data_dir, agent_id, relpath, text, *, grant=None, principal: str = '',
               now: Optional[float] = None) -> dict:
    """整文件写入（UTF-8，<=1MB）。越界目标必须带 grant 令牌。"""
    return _write(data_dir, agent_id, relpath, text, op='write', grant=grant,
                  principal=principal, now=now)


def append_text(data_dir, agent_id, relpath, text, *, grant=None, principal: str = '',
                now: Optional[float] = None) -> dict:
    """追加写入（UTF-8，追加后 <=1MB）。越界目标必须带 grant 令牌。"""
    return _write(data_dir, agent_id, relpath, text, op='append', grant=grant,
                  principal=principal, now=now)


# ── 读 / 列目录 / 统计（全部只在沙箱内）─────────────────────────────────────

def read_text(data_dir, agent_id, relpath) -> dict:
    """读取工作区内的 UTF-8 文本；非 UTF-8 返回结构化错误，不抛解码异常。"""
    try:
        workspace_root = root(data_dir, agent_id)
        target = _classify(workspace_root, relpath)
        if not target.inside:
            _fail('denied', '越界读取被拒绝（读写都只允许工作区内的相对路径）')
        path = target.path
        _assert_confined(workspace_root, path)   # 先判边界，再谈存在性与大小
        try:
            info = path.stat()
        except FileNotFoundError:
            _fail('not_found', '文件不存在')
        except OSError as exc:
            _fail('io', '读取文件失败', error=type(exc).__name__)
        if stat_module.S_ISDIR(info.st_mode):
            _fail('invalid', '目标是一个目录')
        if int(info.st_size) > MAX_FILE_BYTES:
            _fail('too_large', f'文件超过单文件上限 {MAX_FILE_BYTES} 字节')
        try:
            with open(path, 'rb') as handle:
                blob = handle.read()
        except OSError as exc:
            _fail('io', '读取文件失败', error=type(exc).__name__)
        try:
            text = blob.decode('utf-8')
        except UnicodeDecodeError:
            _fail('encoding', '文件不是合法 UTF-8 文本')
    except WorkspaceError as exc:
        return exc.to_dict()
    return {'ok': True, 'text': text, 'path': str(path), 'bytes': len(blob),
            'sha256': _sha256(blob)}


def list_dir(data_dir, agent_id, relpath: str = '') -> dict:
    """列工作区目录（不跟随符号链接；重解析点标成 link）。"""
    try:
        workspace_root = root(data_dir, agent_id)
        target = _classify(workspace_root, relpath, allow_root=True)
        if not target.inside:
            _fail('denied', '越界目录被拒绝')
        path = target.path
        if not path.exists():
            _fail('not_found', '目录不存在')
        if not path.is_dir():
            _fail('invalid', '目标不是目录')
        _assert_confined(workspace_root, path)
        entries: list = []
        try:
            children = sorted(path.iterdir(), key=lambda item: item.name.lower())
        except OSError as exc:
            _fail('io', '列目录失败', error=type(exc).__name__)
        for item in children:
            try:
                info = os.lstat(str(item))
            except OSError:
                continue
            attributes = int(getattr(info, 'st_file_attributes', 0))
            if attributes & _REPARSE_ATTRIBUTE or os.path.islink(str(item)):
                kind = 'link'
            elif stat_module.S_ISDIR(info.st_mode):
                kind = 'dir'
            elif stat_module.S_ISREG(info.st_mode):
                kind = 'file'
            else:
                kind = 'other'
            entries.append({'name': item.name, 'type': kind,
                            'size': int(info.st_size)})
    except WorkspaceError as exc:
        return exc.to_dict()
    return {'ok': True, 'path': str(path), 'entries': entries}


def stats(data_dir, agent_id) -> dict:
    """工作区体积概览（不跟随重解析点，不创建任何目录）。"""
    try:
        workspace_root = root(data_dir, agent_id)
        areas = {name: {'files': 0, 'bytes': 0, 'exists': (workspace_root / name).exists()}
                 for name in WORKSPACE_AREAS}
        files = dirs = total = 0
        stack: list = [workspace_root]
        while stack:
            current = stack.pop()
            try:
                children = list(os.scandir(str(current)))
            except OSError:
                continue
            for child in children:
                try:
                    if child.is_symlink():
                        continue
                    if child.is_dir(follow_symlinks=False):
                        dirs += 1
                        stack.append(Path(child.path))
                        continue
                    if not child.is_file(follow_symlinks=False):
                        continue
                    size = int(child.stat(follow_symlinks=False).st_size)
                except OSError:
                    continue
                files += 1
                total += size
                try:
                    relative = Path(child.path).relative_to(workspace_root)
                except ValueError:
                    continue
                key = relative.parts[0] if len(relative.parts) > 1 else relative.name
                area = areas.get(key)
                if area is not None:
                    area['files'] += 1
                    area['bytes'] += size
    except WorkspaceError as exc:
        return exc.to_dict()
    return {'ok': True, 'agent': _agent_id(agent_id), 'root': str(workspace_root),
            'exists': workspace_root.exists(), 'files': files, 'dirs': dirs,
            'bytes': total, 'areas': areas, 'limits': {
                'max_file_bytes': MAX_FILE_BYTES,
                'audit_max_bytes': int(AGENT_WORKSPACE_AUDIT_MAX_BYTES)}}
