"""W6a：主人在 QQ 私聊里发话即授权后台操作（工作区读写 + 一次性确认码）。

命令面是**固定的几个词**，不是命令字符串（原包 windows_executor.py 的红线：
绝不让聊天消息拼出任意 shell）：

    「记事本」/「读记事本」     读工作区 note.txt，回一段摘要
                               （摘要上限 READ_PREVIEW_CHARS = 1200 字，超出截断）
    「记到记事本：<内容>」      覆盖写 note.txt
    「追加记事本：<内容>」      追加写 note.txt（文件不存在时新建）
    「列工作区」                列工作区根目录（最多 LIST_PREVIEW_ITEMS = 40 项）
    「读文件 <相对路径>」       读工作区内的一个 UTF-8 文本文件（摘要同上）
    「<6 位纯数字>」            用掉一次性确认码

除此之外一律不接管：任何自由 shell、绝对路径、任意动作名都进不来。

授权（谁说了算）
    只有「主人私聊」生效。主人判定复用 qq_app.owner_ids()（agent_admin.json +
    pc_control 的 ownerIds），由外部以回调注入；私聊判定沿用 W4b 的
    message.get('kind') == 'private'。群聊**不**作为授权证明：主人在群里发
    同样的词也只回一句拒绝。
    非主人命中命令面时明确回一句拒绝，并且不产生任何文件写入、也不产生
    agent_workspace 的写审计行（只在本模块的授权审计里留一条 denied）。

确认码（危险动作）
    会碰本机的动作（启停服务 / 敲键盘 / 截图 / 打开路径或网址）不走聊天文本，
    而是由程序侧调 OwnerOps.request() 申请一次确认，得到
    CODE_DIGITS = 6 位数字码：

    * TTL CODE_TTL = 120 秒，过期即废；
    * 一次性：执行成功或失败都当场消耗，重放只会回“已经用过”；
    * 同一时刻只保留一个待确认项，新的覆盖旧的并在回复里说明；
    * 只有主人私聊发这 6 位数字才算数，非主人拿到码也无效；
    * 错误码不消耗待确认项（不执行），过期码、重放码都有明确回应。

    执行器由调用方接线（executor=）：本模块不 import 也不复用 console 的
    confirm 状态机，没有接线执行器时明确回“本机没有被改动”，绝不假装执行。

审计
    <data_dir>/qq-only/qq_ops_audit.jsonl，一行一个事件，字段
    {ts, kind, principal, ok, code, ...}。kind 取
    denied（越权）/ code-issued / code-replaced / code-used / code-invalid /
    code-expired / code-replay / command（工作区指令结果）。工作区**写**本身另有
    src/agent_workspace.py 的审计（agent_workspace_audit.jsonl），这里只补
    拒绝、确认码与指令结果，保证“查得到谁在什么时候要过什么”。
"""
from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path

from . import agent_workspace
from .chat_state import person_of

__all__ = ['AGENT_ID', 'NOTE_NAME', 'READ_PREVIEW_CHARS', 'LIST_PREVIEW_ITEMS',
           'CODE_DIGITS', 'CODE_TTL', 'DANGEROUS', 'AUDIT_FILE_NAME',
           'OwnerOps', 'parse_command']

AGENT_ID = 'qq'                    # 工作区键固定 qq：QQ 侧只有这一个可写域
NOTE_NAME = 'note.txt'             # 「记事本」就是工作区根的 note.txt
READ_PREVIEW_CHARS = 1200          # 读记事本/读文件的摘要上限（超出截断）
LIST_PREVIEW_ITEMS = 40            # 「列工作区」最多列多少项
CODE_DIGITS = 6
CODE_TTL = 120.0                   # 确认码有效期（秒）
AUDIT_FILE_NAME = 'qq_ops_audit.jsonl'
SPENT_RETENTION = 1800.0           # 用完/过期的码记多久（够覆盖重放与误触）
SPENT_MAX = 64                     # ……最多记多少个，防止内存无界

# 只认这几个固定动作名（文案自成一套，不 import console_agent 的 DANGEROUS）。
DANGEROUS = {
    'service': '启停机器人服务',
    'type_text': '往当前窗口打字',
    'hotkey': '模拟快捷键',
    'screenshot': '截屏',
    'open_path': '打开本机文件或文件夹',
    'open_url': '打开网址',
}

_NOTE_READ_WORDS = ('记事本', '读记事本')
_LIST_WORDS = ('列工作区',)
_NOTE_WRITE_WORDS = (('记到记事本', 'note-write'), ('追加记事本', 'note-append'))
_READ_FILE_PREFIX = '读文件'
_CODE_RE = re.compile(r'^\d{6}$')
_TRAILING = '。．.！!？?~～ \t'


def _after_separator(rest):
    """去掉关键词后面的空白/冒号，剩下的就是参数（全角半角冒号都认）。"""
    value = str(rest or '').lstrip()
    if value[:1] in ('：', ':'):
        value = value[1:]
    return value.strip()


def parse_command(text):
    """把一条消息正文解析成指令；不是本模块的命令面就返回 None。

    「记事本」「读记事本」「列工作区」要求**整句就是这个词**（允许句末标点），
    免得把“记事本在哪啊”这类闲聊当成指令。带参数的三类按前缀识别。
    """
    stripped = str(text or '').strip()
    if not stripped:
        return None
    bare = stripped.rstrip(_TRAILING)
    if bare in _NOTE_READ_WORDS:
        return {'kind': 'note-read'}
    if bare in _LIST_WORDS:
        return {'kind': 'list'}
    for word, kind in _NOTE_WRITE_WORDS:
        if stripped.startswith(word):
            return {'kind': kind, 'content': _after_separator(stripped[len(word):])}
    if stripped.startswith(_READ_FILE_PREFIX):
        return {'kind': 'read-file',
                'relpath': _after_separator(stripped[len(_READ_FILE_PREFIX):])}
    return None


def error_code(result):
    """取 agent_workspace 返回的错误码。

    它有两种失败形状：区间逻辑给扁平 {ok:False, code, message}，
    路径契约层给嵌套 {ok:False, error:{code, message}}；这里统一成扁平。
    """
    if not isinstance(result, dict):
        return 'error'
    code = result.get('code')
    if not code:
        inner = result.get('error')
        code = inner.get('code') if isinstance(inner, dict) else ''
    return str(code or 'error')


def error_text(result):
    """取错误的人话说明；没有说明就退回错误码（绝不给空字符串）。"""
    message = ''
    if isinstance(result, dict):
        message = result.get('message') or ''
        inner = result.get('error')
        if not message and isinstance(inner, dict):
            message = inner.get('message') or ''
    return str(message or error_code(result))


class _Pending:
    """一个待确认项（只在内存里，进程重启即失效）。"""

    __slots__ = ('code', 'action', 'detail', 'created', 'ttl', 'principal')

    def __init__(self, code, action, detail, created, ttl, principal):
        self.code = code
        self.action = action
        self.detail = detail
        self.created = created
        self.ttl = ttl
        self.principal = principal

    def seconds_left(self, now):
        return max(0.0, float(self.created) + float(self.ttl) - float(now))


class OwnerOps:
    """QQ 主人指令面 + 一次性确认码。

    data_dir 是数据根（工作区在 <data_dir>/agent_workspace/qq，审计在
    <data_dir>/qq-only/qq_ops_audit.jsonl）；owners 是“返回主人 id 集合”的回调，
    由 qq_app 传 self.owner_ids；executor(action, detail) 由调用方接线。
    """

    def __init__(self, data_dir, *, owners=None, executor=None, clock=None,
                 audit_file=None, code_factory=None):
        self.data_dir = Path(data_dir) if data_dir else None
        self._owners = owners
        self._executor = executor
        self._clock = clock or time.time
        self._audit_file = Path(audit_file) if audit_file else None
        self._code_factory = code_factory
        self._pending = None
        self._spent = {}          # 码 -> (发生时刻, 'used' | 'expired')

    # ── 审计 ────────────────────────────────────────────────────────────
    def audit_path(self):
        """审计文件位置；没有 data_dir 时返回 None（只影响可观测性，不影响业务）。"""
        if self._audit_file is not None:
            return self._audit_file
        if self.data_dir is None:
            return None
        try:
            return agent_workspace.audit_path(self.data_dir).parent / AUDIT_FILE_NAME
        except agent_workspace.WorkspaceError:
            return None

    def audit(self, kind, *, principal='', ok=True, code='', **extra):
        """追加一行审计；写不进也不反向阻塞业务，只返回 False。"""
        entry = {'ts': round(float(self._clock()), 3), 'kind': str(kind),
                 'principal': str(principal or ''), 'ok': bool(ok), 'code': str(code or '')}
        for key, value in extra.items():
            if value is not None:
                entry[key] = value
        path = self.audit_path()
        if path is None:
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'a', encoding='utf-8', newline='\n') as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
            return True
        except OSError:
            return False

    # ── 执行器 ──────────────────────────────────────────────────────────
    def set_executor(self, executor):
        """接线危险动作执行器（None = 没有可执行通道）。返回旧值，便于复原。

        执行器签名 (action, detail) -> bool | {'ok','code','detail'}；
        只有确认码用掉之后才会被调用。
        """
        previous = self._executor
        self._executor = executor
        return previous

    # ── 主人判定 ────────────────────────────────────────────────────────
    def owners(self):
        if self._owners is None:
            return set()
        try:
            return {str(item) for item in (self._owners() or set()) if str(item).strip()}
        except Exception:
            return set()          # 判不准时按“不是主人”处理，宁可少授权

    # ── QQ 侧入口 ───────────────────────────────────────────────────────
    def handle(self, message):
        """处理一条 QQ 消息；返回回执 dict，不是本模块的命令面时返回 None。

        返回 {'kind', 'ok', 'status', 'text'}：qq_app 只负责把 text 回出去，
        决策（谁、能不能、写不写）全在这里，避免授权逻辑散进消息链路。
        """
        message = message if isinstance(message, dict) else {}
        text = str(message.get('text') or '')
        command = parse_command(text)
        digits = text.strip()
        if command is None and _CODE_RE.fullmatch(digits):
            # 纯数字只在“真的有码”时才接管，否则它就是普通聊天（“123456”照常回）。
            if self._pending is not None or digits in self._spent:
                command = {'kind': 'confirm', 'digits': digits}
        if command is None:
            return None
        private = message.get('kind') == 'private'
        who = person_of(message)
        principal = 'qq:' + str(who)
        if not (private and who and who in self.owners()):
            return self._refuse(command, principal, private=private)
        kind = command['kind']
        if kind == 'confirm':
            return self._confirm(command['digits'], principal)
        if kind == 'note-read':
            return self._note_read(principal)
        if kind in ('note-write', 'note-append'):
            return self._note_write(principal, command.get('content') or '',
                                    append=(kind == 'note-append'))
        if kind == 'list':
            return self._list(principal)
        return self._read_file(principal, command.get('relpath') or '')

    def _refuse(self, command, principal, *, private):
        kind = str(command.get('kind') or '')
        self.audit('denied', principal=principal, ok=False, code='not-owner',
                   command=kind, private=bool(private))
        if private:
            text = ('这条指令只认主人私聊。你的 QQ 不在主人名单里，'
                    '所以既没有读取、也没有写入任何文件。')
        else:
            text = ('群聊不作为授权证明：主人的指令只在私聊里生效，'
                    '这条既没有读取、也没有写入任何文件。')
        return {'kind': kind, 'ok': False, 'status': 'not-owner', 'text': text,
                'command': kind}

    # ── 四类工作区指令 ──────────────────────────────────────────────────
    def _reply(self, kind, principal, ok, status, text, **extra):
        self.audit('command', principal=principal, ok=bool(ok), code=str(status),
                   command=kind, **extra)
        return {'kind': kind, 'ok': bool(ok), 'status': str(status), 'text': str(text)}

    def _note_read(self, principal):
        result = agent_workspace.read_text(self.data_dir, AGENT_ID, NOTE_NAME)
        if not result.get('ok'):
            status = error_code(result)
            if status == 'not_found':
                return self._reply('note-read', principal, True, 'ok',
                                   '记事本还是空的：工作区里还没有 note.txt。')
            return self._reply('note-read', principal, False, status,
                               '读记事本失败：' + error_text(result))
        text = str(result.get('text') or '')
        size = int(result.get('bytes') or 0)
        body = text[:READ_PREVIEW_CHARS]
        if len(text) > READ_PREVIEW_CHARS:
            body += '\n…（已截断，只显示前 %d 字）' % READ_PREVIEW_CHARS
        return self._reply('note-read', principal, True, 'ok',
                           '记事本 note.txt（共 %d 字节）：\n%s' % (size, body or '（空）'),
                           path=NOTE_NAME, bytes=size)

    def _note_write(self, principal, content, *, append):
        kind = 'note-append' if append else 'note-write'
        text = str(content or '')
        if not text.strip():
            return self._reply(kind, principal, False, 'empty',
                               '要写的内容是空的：请用「%s：内容」这样的格式。'
                               % ('追加记事本' if append else '记到记事本'))
        writer = agent_workspace.append_text if append else agent_workspace.write_text
        result = writer(self.data_dir, AGENT_ID, NOTE_NAME, text, principal=principal)
        if not result.get('ok'):
            status = error_code(result)
            return self._reply(kind, principal, False, status,
                               '写入失败：' + error_text(result))
        written = int(result.get('bytes') or 0)
        # 只回工作区相对路径，不回绝对路径：绝对路径会带出安装目录与用户名。
        return self._reply(kind, principal, True, 'ok',
                           '已%s %s：这次 %d 字节。' % ('追加到' if append else '写入',
                                                      NOTE_NAME, written),
                           path=NOTE_NAME, bytes=written)

    def _list(self, principal):
        result = agent_workspace.list_dir(self.data_dir, AGENT_ID, '')
        if not result.get('ok'):
            status = error_code(result)
            if status == 'not_found':
                return self._reply('list', principal, True, 'ok',
                                   '工作区还是空的：还没有任何文件。')
            return self._reply('list', principal, False, status,
                               '列工作区失败：' + error_text(result))
        entries = list(result.get('entries') or [])
        if not entries:
            return self._reply('list', principal, True, 'ok', '工作区还是空的：还没有任何文件。')
        marks = {'dir': '[目录]', 'file': '[文件]', 'link': '[链接]'}
        lines = []
        for item in entries[:LIST_PREVIEW_ITEMS]:
            name = str(item.get('name') or '')
            mark = marks.get(str(item.get('type') or ''), '[其它]')
            if str(item.get('type')) == 'file':
                lines.append('%s %s（%d 字节）' % (mark, name, int(item.get('size') or 0)))
            else:
                lines.append('%s %s' % (mark, name))
        more = len(entries) - LIST_PREVIEW_ITEMS
        if more > 0:
            lines.append('…还有 %d 项未列出' % more)
        return self._reply('list', principal, True, 'ok',
                           '工作区共 %d 项：\n%s' % (len(entries), '\n'.join(lines)),
                           entries=len(entries))

    def _read_file(self, principal, relpath):
        rel = str(relpath or '').strip().replace('\\', '/')
        if not rel:
            return self._reply('read-file', principal, False, 'empty',
                               '要读哪个文件？格式是「读文件 相对路径」，例如「读文件 note.txt」。')
        result = agent_workspace.read_text(self.data_dir, AGENT_ID, rel)
        if not result.get('ok'):
            status = error_code(result)
            head = '拒绝读取 %s：' % rel if status == 'denied' else '读取 %s 失败：' % rel
            return self._reply('read-file', principal, False, status,
                               head + error_text(result), path=rel)
        text = str(result.get('text') or '')
        size = int(result.get('bytes') or 0)
        body = text[:READ_PREVIEW_CHARS]
        if len(text) > READ_PREVIEW_CHARS:
            body += '\n…（已截断，只显示前 %d 字）' % READ_PREVIEW_CHARS
        return self._reply('read-file', principal, True, 'ok',
                           '%s（共 %d 字节）：\n%s' % (rel, size, body or '（空）'),
                           path=rel, bytes=size)

    # ── 确认码：程序侧申请 ──────────────────────────────────────────────
    def request(self, action, detail='', *, principal='system', ttl=None):
        """申请一次危险动作确认，返回 {'ok','status','confirm','text','expiresAt'}。

        只接受 DANGEROUS 里的固定动作名：其它字符串一律拒绝并审计，
        所以调用方也没法借这条路执行任意命令。**这一步不会执行任何动作。**
        """
        action = str(action or '').strip()
        label = DANGEROUS.get(action)
        if label is None:
            self.audit('code-issued', principal=principal, ok=False,
                       code='unknown-action', action=action[:40])
            return {'ok': False, 'status': 'unknown-action', 'confirm': '', 'expiresAt': 0.0,
                    'text': '不认识的动作名（只允许 %s），没有生成确认码。'
                            % '、'.join(sorted(DANGEROUS))}
        now = float(self._clock())
        replaced = self._pending
        if replaced is not None:
            self.audit('code-replaced', principal=principal, ok=False, code='replaced',
                       action=replaced.action, confirm=replaced.code)
        code = self._take_code()
        code_ttl = float(CODE_TTL if ttl is None else ttl)
        self._pending = _Pending(code=code, action=action, detail=str(detail or ''),
                                 created=now, ttl=code_ttl, principal=str(principal or ''))
        self.audit('code-issued', principal=principal, ok=True, code='ok', action=action,
                   confirm=code, ttl=code_ttl, replaced=replaced is not None)
        note = '（上一个待确认项已作废，以这个为准。）\n' if replaced is not None else ''
        text = ('要执行「%s」需要确认码：%s\n%s%d 秒内有效，只能使用一次；'
                '回复这 %d 位数字就会执行，过期或非本人回复都不算数。'
                % (label, code, note, int(code_ttl), CODE_DIGITS))
        return {'ok': True, 'status': 'ok', 'confirm': code,
                'expiresAt': now + code_ttl, 'text': text, 'action': action}

    def pending(self):
        """当前待确认项（不含码本身）；没有则返回 None。"""
        item = self._pending
        if item is None:
            return None
        now = float(self._clock())
        return {'action': item.action, 'label': DANGEROUS.get(item.action, item.action),
                'detail': item.detail, 'expiresAt': item.created + item.ttl,
                'secondsLeft': item.seconds_left(now)}

    def _take_code(self):
        if self._code_factory is not None:
            return str(self._code_factory())
        for _ in range(128):
            code = '%0*d' % (CODE_DIGITS, random.randint(0, 10 ** CODE_DIGITS - 1))
            if code in self._spent:
                continue
            if self._pending is not None and self._pending.code == code:
                continue
            return code
        return '%0*d' % (CODE_DIGITS, random.randint(0, 10 ** CODE_DIGITS - 1))

    # ── 确认码：QQ 侧使用 ───────────────────────────────────────────────
    def _confirm(self, digits, principal):
        digits = str(digits or '').strip()
        item = self._pending
        now = float(self._clock())
        if item is None:
            spent = self._spent.get(digits)
            if spent is not None:
                status = 'expired' if spent[1] == 'expired' else 'replayed'
                self.audit('code-expired' if status == 'expired' else 'code-replay',
                           principal=principal, ok=False, code=status, confirm=digits)
                text = ('这个确认码已经过期作废了，没有执行任何操作。'
                        if status == 'expired' else
                        '这个确认码已经用过了（一次性），不会再次执行。')
                return {'kind': 'confirm', 'ok': False, 'status': status, 'text': text}
            return {'kind': 'confirm', 'ok': False, 'status': 'stale',
                    'text': '现在没有待确认的动作，这个数字没有执行任何操作。'}
        if item.seconds_left(now) <= 0:
            self._pending = None
            self._remember(item.code, now, 'expired')
            self.audit('code-expired', principal=principal, ok=False, code='expired',
                       action=item.action)
            return {'kind': 'confirm', 'ok': False, 'status': 'expired',
                    'text': '确认码已过期（有效 %d 秒），没有执行任何操作；'
                            '需要的话请重新发起。' % int(item.ttl)}
        if digits != item.code:
            self.audit('code-invalid', principal=principal, ok=False, code='mismatch',
                       action=item.action, confirm=digits)
            return {'kind': 'confirm', 'ok': False, 'status': 'mismatch',
                    'text': '确认码不对（这个码还能用 %d 秒），没有执行任何操作。'
                            % int(item.seconds_left(now))}
        # 一次性：先消耗再执行，执行失败也不退回（不给人“重试到成功”的窗口）。
        self._pending = None
        self._remember(item.code, now, 'used')
        label = DANGEROUS.get(item.action, item.action)
        self.audit('code-used', principal=principal, ok=True, code='ok',
                   action=item.action, detail=item.detail, confirm=item.code,
                   requested_by=item.principal)
        outcome = self._run(item)
        if outcome.get('ok'):
            detail = str(outcome.get('detail') or '').strip()
            text = '确认码已使用，动作「%s」已执行。%s' % (label, ('\n' + detail) if detail else '')
        elif outcome.get('code') == 'no-executor':
            text = '确认码已使用并失效，但「%s」没有接线执行器，本机没有被改动。' % label
        else:
            detail = str(outcome.get('detail') or '').strip()
            text = '确认码已使用并失效，但「%s」没执行成功。%s' % (label, ('\n' + detail) if detail else '')
        return {'kind': 'confirm', 'ok': bool(outcome.get('ok')),
                'status': str(outcome.get('code') or ''), 'text': text}

    def _remember(self, code, now, status):
        if code:
            self._spent[str(code)] = (float(now), str(status))
        self._prune(now)

    def _prune(self, now):
        limit = float(now) - SPENT_RETENTION
        for key in [key for key, value in self._spent.items() if value[0] < limit]:
            self._spent.pop(key, None)
        while len(self._spent) > SPENT_MAX:
            oldest = min(self._spent, key=lambda item: self._spent[item][0])
            self._spent.pop(oldest, None)

    def _run(self, item):
        """交给调用方接线的执行器；没接线就明确说“什么都没做”。"""
        if self._executor is None:
            return {'ok': False, 'code': 'no-executor', 'detail': ''}
        try:
            result = self._executor(item.action, item.detail)
        except Exception as exc:                     # 执行器的异常不能打挂消息链路
            return {'ok': False, 'code': 'executor-error',
                    'detail': '%s: %s' % (type(exc).__name__, exc)}
        if isinstance(result, dict):
            ok = bool(result.get('ok'))
            return {'ok': ok, 'code': str(result.get('code') or ('ok' if ok else 'failed')),
                    'detail': str(result.get('detail') or result.get('message') or '')}
        return {'ok': bool(result), 'code': 'ok' if result else 'failed', 'detail': ''}
