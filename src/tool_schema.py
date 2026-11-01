"""从 skill / plugin 源码静态提取工具 schema（WP5c）。

为什么要静态解析：.tmp/inv/skill-tools.json 的工具条目只有 {name, description, hasSchema}，
schema 本体没保留；宿主 extension_host.owned_tools 也只回传 {id,name,description}。而 schema
就写在源码里：

    api.registerTool({
      id: 'wiki_query',
      name: '查 GBF 灰机 wiki',
      description: '查询《碧蓝幻想》灰机中文 wiki……',
      parameters: { type: 'object', properties: { query: {...} }, required: ['query'] },
      async execute(ctx, args) { ... }
    })

所以本模块自带一个 JS 对象字面量解析器：词法切分 + 递归下降，**不执行任何 JS、不起子进程**。
容忍单/双/反引号、尾逗号、行注释与块注释、模板字符串、嵌套对象/数组、字符串 + 拼接，以及
值后面跟函数或标识符（无法静态求值的键直接丢弃，不许整体失败——execute 后面永远跟着函数体）。

对外契约（另一包按此消费，签名不许改）：
- extract_tools(extension_dir, entry_files=None) -> {tool_id: {id,name,description,parameters,source}}
- schema_for(extension_dir, tool_id, fallback=None) -> dict
- tool_catalog(extension_dir) -> [{'name','description','parameters'}]

稳定性要求：对市场里 512 个 plugin + 376 个 skill 全量跑不许抛；单包解析目标 ≤50ms，
超预算就降级成"已经解出来的部分"（TIME_BUDGET）。
"""
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

_log = logging.getLogger(__name__)

ENTRY_NAMES = ('index.js', 'main.js', 'plugin.js')
MANIFEST_NAMES = ('manifest.json', 'plugin.json', 'skill.json')
MAX_ENTRY_FILES = 3
MAX_FILES = 40
MAX_BYTES = 512 * 1024
MAX_FILE_BYTES = 512 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
TIME_BUDGET = 0.05
EMPTY_SCHEMA = {'type': 'object', 'properties': {}}


def _empty_schema() -> dict:
    """每次给一份新对象：dict() 是浅拷贝，共用 properties 会让调用方互相污染。"""
    return {'type': 'object', 'properties': {}}
PARAM_KEYS = ('parameters', 'inputSchema', 'input_schema', 'schema', 'params')
DESC_KEYS = ('description', 'summary', 'desc')
ID_KEYS = ('id', 'toolId', 'tool_id', 'key', 'name')

_NAME_BAD = re.compile(r'[^0-9A-Za-z_-]+')
_ID_BAD = re.compile(r'[^0-9a-z]+')

# 工具注册调用点：registerTool({...}) / tools.register({...}) / register_tool({...})
_CALL_RE = re.compile(
    r'\b(?:registerTool|register_tools?)\b|\b(?:tools|registry)\s*\.\s*(?:register|add)\b')

# 遮罩扫描的起点：注释与引号。用逐点搜索代替单个联合正则——正则字面量里
# 允许出现不配对的引号（如 /["']/），联合正则会把它当成字符串起点一路遮到文件尾。
_MASK_START_RE = re.compile(r"""//|/\*|/|['"`]""")

# 正则字面量的判定：出现在这些字符之后，或这些关键字之后，斜杠才是正则开头
_REGEX_AFTER = set('(,=:[!&|?{};+-*%~^<>')
_REGEX_KEYWORDS = {'return', 'typeof', 'case', 'in', 'of', 'new', 'delete', 'void',
                   'do', 'else', 'yield', 'await', 'instanceof'}
_PREV_WORD_RE = re.compile(r'[A-Za-z_$][\w$]*$')

_SKIP_RE = re.compile(r'(?:\s+|//[^\n]*|/\*(?:[^*]|\*(?!/))*?\*/)+')
_TOKEN_RE = re.compile(r"""
    (?P<str>'(?:\\[\s\S]|[^'\\])*'|"(?:\\[\s\S]|[^"\\])*"|`(?:\\[\s\S]|[^`\\])*`)
  | (?P<num>-?(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?))
  | (?P<id>[A-Za-z_$][\w$]*)
  | (?P<punc>[{}\[\]:,()=+\-*/<>!&|%~^?.;@#])
""", re.X | re.S)

_IMPORT_RE = re.compile(r"""(?:from\s*['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"]\s*\)"""
                        r"""|import\(\s*['"]([^'"]+)['"]\s*\))""")

_UNESCAPE = {'n': '\n', 't': '\t', 'r': '\r', 'b': '\b', 'f': '\f', 'v': '\v', '0': '\0'}
_ESC_RE = re.compile(r'\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|[\s\S])')

_UNKNOWN = object()
_PAIRS = {'{': '}', '(': ')', '[': ']'}


def _clean_name(value) -> str:
    """与 tool_loop._clean_name（src/tool_loop.py:172-174）同一口径。"""
    return _NAME_BAD.sub('_', str(value or '').strip()).strip('_')[:64]


def _id_key(value) -> str:
    return _ID_BAD.sub('', str(value or '').lower())


def _unquote(literal: str) -> str:
    body = literal[1:-1]

    def repl(match):
        token = match.group(1)
        if len(token) == 5 and token[0] == 'u':
            return chr(int(token[1:], 16))
        if len(token) == 3 and token[0] == 'x':
            return chr(int(token[1:], 16))
        return _UNESCAPE.get(token, token)

    try:
        return _ESC_RE.sub(repl, body)
    except Exception:
        return body


def _blank(out: List[str], start: int, end: int) -> None:
    for index in range(start, min(end, len(out))):
        if out[index] != '\n':
            out[index] = ' '


def _quote_end(src: str, start: int, quote: str, same_line: bool) -> int:
    """返回配对引号的下标；找不到返回 -1（把该引号当普通字符，继续往下扫）。"""
    total = len(src)
    index = start + 1
    while index < total:
        char = src[index]
        if char == '\\':
            index += 2
            continue
        if char == quote:
            return index
        if same_line and char == '\n':
            return -1
        index += 1
    return -1


def _is_regex_start(src: str, index: int) -> bool:
    """判断 index 处的 '/' 是正则字面量开头还是除号。

    正则字面量里允许出现引号和反引号，若不把它们整段遮掉，里面的孤立引号
    会把后面的代码吞成空白（真实案例：sb6657-memes、mimo-tts）。
    """
    cursor = index - 1
    while cursor >= 0 and src[cursor] in ' \t\r\n':
        cursor -= 1
    if cursor < 0:
        return True
    # 只回看 64 字符：src[:index].rstrip() 在 125KB 的包上会退化成 O(n²)（实测 0.58s）
    word = _PREV_WORD_RE.search(src, max(0, cursor - 63), cursor + 1)
    if word:
        return word.group(0) in _REGEX_KEYWORDS
    return src[cursor] in _REGEX_AFTER


def _regex_end(src: str, start: int) -> int:
    """返回正则字面量结尾 '/' 的下标；跨行或找不到时返回 -1。"""
    total = len(src)
    index = start + 1
    in_class = False
    while index < total:
        char = src[index]
        if char == '\\':
            index += 2
            continue
        if char == '\n':
            return -1
        if in_class:
            if char == ']':
                in_class = False
        elif char == '[':
            in_class = True
        elif char == '/':
            return index
        index += 1
    return -1


def _mask(src: str) -> str:
    """把注释与字符串的内容换成空格（长度不变），用来判断某个位置是不是代码区。

    单/双引号字符串必须能在同一行内闭合才算字符串——这条规则让正则字面量里的
    孤立引号（/["']/）不会被误判，从而不会把后续代码整片吞掉。模板字符串可以跨行；
    嵌套在 ${} 里的模板串属已知限制（会被多遮一段，不会抛异常）。
    """
    out = list(src)
    total = len(src)
    pos = 0
    while pos < total:
        match = _MASK_START_RE.search(src, pos)
        if not match:
            break
        token = match.group(0)
        start = match.start()
        if token == '//':
            end = src.find('\n', start)
            end = total if end < 0 else end
            _blank(out, start, end)
            pos = end
        elif token == '/*':
            end = src.find('*/', start + 2)
            end = total if end < 0 else end + 2
            _blank(out, start, end)
            pos = end
        elif token == '/':
            if not _is_regex_start(src, start):
                pos = start + 1
                continue
            end = _regex_end(src, start)
            if end < 0:
                pos = start + 1
                continue
            _blank(out, start + 1, end)
            pos = end + 1
        else:
            end = _quote_end(src, start, token, token != '`')
            if end < 0:
                pos = start + 1
                continue
            _blank(out, start + 1, end)
            pos = end + 1
    return ''.join(out)


class _Tokens:
    """带注释跳过的轻量词法器；只在需要时前进，避免为大文件预切全量 token。"""

    __slots__ = ('src', 'pos')

    def __init__(self, src: str, pos: int):
        self.src = src
        self.pos = pos

    def _skip(self):
        while True:
            match = _SKIP_RE.match(self.src, self.pos)
            if not match:
                return
            self.pos = match.end()

    def peek(self):
        self._skip()
        match = _TOKEN_RE.match(self.src, self.pos)
        if not match:
            return 'eof', ''
        return match.lastgroup, match.group(0)

    def take(self):
        kind, text = self.peek()
        if kind != 'eof':
            self.pos += len(text)
        return kind, text


def _skip_balanced(tok: _Tokens, opener: str) -> None:
    closer = _PAIRS.get(opener, '}')
    depth = 0
    while True:
        kind, text = tok.peek()
        if kind == 'eof':
            return
        tok.take()
        if kind != 'punc':
            continue
        if text == opener:
            depth += 1
        elif text == closer:
            depth -= 1
            if depth <= 0:
                return


def _resync(tok: _Tokens) -> None:
    """丢掉一个静态求不出来的值，对齐到本层的 ',' 或 '}'。"""
    depth = 0
    while True:
        kind, text = tok.peek()
        if kind == 'eof':
            return
        if kind == 'punc':
            if text in _PAIRS:
                depth += 1
                tok.take()
                continue
            if text in (')', ']'):
                if depth == 0:
                    tok.take()
                    continue
                depth -= 1
                tok.take()
                continue
            if text == '}':
                if depth == 0:
                    return
                depth -= 1
                tok.take()
                continue
            if text == ',' and depth == 0:
                tok.take()
                return
        tok.take()


def _primary(tok: _Tokens):
    kind, text = tok.peek()
    if kind == 'punc':
        if text == '{':
            return _parse_object(tok)
        if text == '[':
            return _parse_array(tok)
        tok.take()
        return _UNKNOWN
    if kind == 'str':
        tok.take()
        if text[:1] == '`':
            return _UNKNOWN if ('$' + '{') in text else _unquote(text)
        return _unquote(text)
    if kind == 'num':
        tok.take()
        try:
            return int(text, 0)
        except ValueError:
            try:
                return float(text)
            except ValueError:
                return _UNKNOWN
    if kind == 'id':
        tok.take()
        if text == 'true':
            return True
        if text == 'false':
            return False
        if text == 'null':
            return None
        return _UNKNOWN
    if kind != 'eof':
        tok.take()
    return _UNKNOWN


def _value(tok: _Tokens):
    value = _primary(tok)
    while True:
        kind, text = tok.peek()
        if kind == 'punc' and text == '+':
            tok.take()
            extra = _primary(tok)
            value = value + extra if isinstance(value, str) and isinstance(extra, str) else _UNKNOWN
            continue
        return value


def _parse_array(tok: _Tokens):
    out: List = []
    tok.take()
    while True:
        kind, text = tok.peek()
        if kind == 'eof':
            return out
        if kind == 'punc' and text == ']':
            tok.take()
            return out
        if kind == 'punc' and text == ',':
            tok.take()
            continue
        if kind == 'punc' and text == '.':
            while tok.peek() == ('punc', '.'):
                tok.take()
        before = tok.pos
        value = _value(tok)
        if value is _UNKNOWN:
            if tok.pos == before:
                tok.take()
            continue
        out.append(value)


def _parse_object(tok: _Tokens, out: Optional[dict] = None) -> dict:
    result = {} if out is None else out
    tok.take()
    while True:
        kind, text = tok.peek()
        if kind == 'eof':
            return result
        if kind == 'punc' and text == '}':
            tok.take()
            return result
        if kind == 'punc' and text == ',':
            tok.take()
            continue
        if kind == 'punc' and text == '.':
            while tok.peek() == ('punc', '.'):
                tok.take()
            spread = _value(tok)
            if isinstance(spread, dict):
                result.update(spread)
            continue
        if kind in ('str', 'num', 'id'):
            key = _unquote(text) if kind == 'str' else text
            tok.take()
        else:
            tok.take()
            continue
        kind2, text2 = tok.peek()
        if kind2 == 'punc' and text2 == ':':
            tok.take()
            before = tok.pos
            value = _value(tok)
            if value is not _UNKNOWN:
                result[key] = value
            elif tok.pos == before:
                tok.take()
            continue
        if kind2 == 'punc' and text2 == '(':
            _skip_balanced(tok, '(')
            if tok.peek() == ('punc', '{'):
                _skip_balanced(tok, '{')
            continue
        _resync(tok)


def _parse_js_object(src, start_index):
    """把 start_index 处的 JS 对象字面量解析成 dict；解析不动就返回 None（绝不抛）。

    部分成功也算成功：已经解出来的键会保留，未解出的键直接丢弃。
    """
    result: Dict[str, object] = {}
    try:
        if not isinstance(src, str) or not src:
            return None
        pos = int(start_index or 0)
        if pos < 0 or pos >= len(src):
            return None
        tok = _Tokens(src, pos)
        if tok.peek() != ('punc', '{'):
            return None
        _parse_object(tok, result)
        return result or None
    except Exception as exc:  # 词法/递归下降里的任何意外都只降级，不外溢
        _log.debug('js object parse failed at %s: %s', start_index, exc)
        return result or None


def _call_sites(src: str) -> List[int]:
    """返回所有 registerTool({...}) 里 '{' 的下标（只认代码区，不认注释与字符串）。"""
    starts: List[int] = []
    try:
        masked = _mask(src)
    except Exception:
        return starts
    for match in _CALL_RE.finditer(masked):
        index = match.end()
        while index < len(masked) and masked[index] in ' \t\r\n':
            index += 1
        # 可选调用 a.registerTool?.({...})：真实包 bilibili-act 就是这么写的
        if masked[index:index + 2] == '?.':
            index += 2
            while index < len(masked) and masked[index] in ' \t\r\n':
                index += 1
        if index >= len(masked) or masked[index] != '(':
            continue
        index += 1
        while index < len(masked) and masked[index] in ' \t\r\n':
            index += 1
        if index < len(masked) and masked[index] == '{':
            starts.append(index)
    return starts


def _as_schema(parameters) -> dict:
    """把源码里抠出来的 parameters 规整成 JSON Schema（口径参考 tool_loop:177-184）。"""
    if not isinstance(parameters, dict) or not parameters:
        return _empty_schema()
    if parameters.get('type') == 'object':
        return parameters
    if 'properties' in parameters:
        return dict(parameters, type='object')
    return {'type': 'object', 'properties': dict(parameters)}


def _normalise(obj: dict, source: str) -> Optional[dict]:
    if not isinstance(obj, dict) or not obj:
        return None
    raw_id = ''
    for key in ID_KEYS:
        value = obj.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            raw_id = str(value).strip()
            break
    if not raw_id:
        return None
    label = ''
    if isinstance(obj.get('name'), str) and obj['name'].strip() != raw_id:
        label = obj['name'].strip()
    description = ''
    for key in DESC_KEYS:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            description = ' '.join(value.split())
            break
    if not description:
        description = label or raw_id
    parameters: dict = {}
    for key in PARAM_KEYS:
        value = obj.get(key)
        if isinstance(value, dict) and value:
            parameters = value
            break
    return {'id': raw_id, 'name': _clean_name(raw_id), 'label': label,
            'description': description, 'parameters': parameters, 'source': source}


def _code_tools(text: str) -> List[dict]:
    found: Dict[str, dict] = {}
    for start in _call_sites(text):
        obj = _parse_js_object(text, start)
        tool = _normalise(obj, 'code') if obj else None
        if not tool:
            continue
        old = found.get(tool['id'])
        if old is None or (not old['parameters'] and tool['parameters']):
            found[tool['id']] = tool
    return list(found.values())


def _read_text(path: Path, limit: int):
    try:
        if not path.is_file():
            return ''
        size = path.stat().st_size
        if size <= 0 or size > limit:
            return ''
        raw = path.read_bytes()
    except Exception:
        return ''
    for encoding in ('utf-8-sig', 'utf-8', 'utf-16', 'gb18030'):
        try:
            return raw.decode(encoding)
        except Exception:
            continue
    return raw.decode('utf-8', 'replace')


def _manifest_entry(root: Path) -> str:
    for name in MANIFEST_NAMES:
        data = _read_text(root / name, MAX_MANIFEST_BYTES)
        if not data:
            continue
        try:
            payload = json.loads(data)
        except Exception:
            continue
        entry = payload.get('entry') if isinstance(payload, dict) else None
        if isinstance(entry, str) and entry.strip():
            return entry.strip().lstrip('./')
    return ''


def _entry_files(root: Path, entry_files=None) -> List[str]:
    names: List[str] = []
    for item in list(entry_files or []):
        text = str(item or '').strip().replace('\\', '/')
        if text and (root / text).is_file() and text not in names:
            names.append(text)
    if not names:
        for name in ENTRY_NAMES:
            if (root / name).is_file():
                names.append(name)
                if len(names) >= MAX_ENTRY_FILES:
                    break
    if not names:
        entry = _manifest_entry(root)
        if entry and (root / entry).is_file():
            names.append(entry)
    if not names:
        shallow = sorted((path.name for path in root.glob('*.js') if path.is_file()))
        names = shallow[:MAX_ENTRY_FILES]
    if not names:
        names = _nested_entries(root)
    return names


def _nested_entries(root: Path, depth: int = 4) -> List[str]:
    """入口不在包根时往子目录里找（真例：sb6657-memes.zip 是 <id>/<id>/index.js）。"""
    found: List[str] = []
    try:
        base_depth = len(root.parts)
        for path in root.rglob('*'):
            if len(path.parts) - base_depth > depth:
                continue
            if path.name not in ENTRY_NAMES or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if 'node_modules' in relative:
                continue
            found.append(relative)
            if len(found) >= MAX_ENTRY_FILES:
                break
    except Exception as exc:
        _log.debug('nested entry scan failed on %s: %s', root, exc)
        return []
    found.sort(key=lambda item: (item.count('/'), item))
    return found[:MAX_ENTRY_FILES]


def _relative_candidates(base: str, spec: str) -> List[str]:
    prefix = spec[2:] if spec.startswith('./') else spec
    try:
        joined = os.path.normpath(os.path.join(os.path.dirname(base), prefix)).replace('\\', '/')
    except Exception:
        return []
    if joined.startswith('..') or joined.startswith('/'):
        return []
    result = [joined] if Path(joined).suffix else []
    for suffix in ('.js', '.mjs', '.cjs'):
        result.append(joined + suffix)
    for suffix in ENTRY_NAMES:
        result.append(joined + '/' + suffix)
    return result


def _closure(root: Path, entries: Iterable[str]) -> List[str]:
    """入口文件 + 它们相对 import 的一层闭包（≤40 文件、总 ≤512KB）。"""
    names: List[str] = []
    total = 0
    pending = list(entries)
    while pending and len(names) < MAX_FILES:
        name = pending.pop(0)
        if not name or name in names:
            continue
        path = root / name
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
        except Exception:
            continue
        if size > MAX_FILE_BYTES or total + size > MAX_BYTES:
            continue
        total += size
        names.append(name)
        text = _read_text(path, MAX_FILE_BYTES)
        for match in _IMPORT_RE.finditer(text or ''):
            spec = match.group(1) or match.group(2) or match.group(3) or ''
            if spec.startswith('.'):
                pending.extend(_relative_candidates(name, spec))
    return names


def _manifest_tools(root: Path) -> List[dict]:
    found: Dict[str, dict] = {}
    for name in MANIFEST_NAMES:
        data = _read_text(root / name, MAX_MANIFEST_BYTES)
        if not data:
            continue
        try:
            payload = json.loads(data)
        except Exception:
            continue
        tools = payload.get('tools') if isinstance(payload, dict) else None
        if not isinstance(tools, list):
            continue
        for item in tools:
            tool = _normalise(item, 'manifest') if isinstance(item, dict) else None
            if tool and tool['id'] not in found:
                found[tool['id']] = tool
    return list(found.values())


def extract_tools(extension_dir, entry_files=None) -> Dict[str, dict]:
    """静态提取一个包里的工具定义：{tool_id: {id,name,description,parameters,source}}。

    数据源两条：包内清单（manifest.json/plugin.json/skill.json 的 tools 数组，source='manifest'）
    与源码里的 registerTool({...}) 对象字面量（source='code'）。同一 id 以源码为准，清单只补缺
    （description / parameters）。任何异常都吞掉并记 debug，返回已经解出来的部分。
    """
    result: Dict[str, dict] = {}
    try:
        root = Path(extension_dir)
        if not root.is_dir():
            return result
        started = time.monotonic()
        for tool in _manifest_tools(root):
            result[tool['id']] = tool
        for name in _closure(root, _entry_files(root, entry_files)):
            elapsed = time.monotonic() - started
            if result and elapsed > TIME_BUDGET:
                _log.debug('time budget hit on %s at %s', root, name)
                break
            if elapsed > TIME_BUDGET * 4:
                break
            text = _read_text(root / name, MAX_FILE_BYTES)
            if not text:
                continue
            for tool in _code_tools(text):
                old = result.get(tool['id'])
                if old is None:
                    result[tool['id']] = tool
                    continue
                merged = dict(old)
                for key in ('description', 'parameters', 'label'):
                    if not merged.get(key) and tool.get(key):
                        merged[key] = tool[key]
                if tool.get('parameters'):
                    merged['source'] = 'code'
                result[tool['id']] = merged
    except Exception as exc:
        _log.debug('extract_tools failed on %s: %s', extension_dir, exc)
    return result


def _lookup(tools: Dict[str, dict], tool_id) -> Optional[dict]:
    key = str(tool_id or '')
    if key in tools:
        return tools[key]
    cleaned = _clean_name(key)
    for item in tools.values():
        if item.get('name') == cleaned:
            return item
    wanted = _id_key(key)
    if wanted:
        for tid, item in tools.items():
            if _id_key(tid) == wanted:
                return item
    return None


def schema_for(extension_dir, tool_id, fallback=None) -> dict:
    """取一个工具的 JSON Schema；找不到返回 fallback（None 时返回空 object schema）。"""
    try:
        item = _lookup(extract_tools(extension_dir), tool_id)
        if item:
            parameters = item.get('parameters')
            if isinstance(parameters, dict) and parameters:
                return _as_schema(parameters)
    except Exception as exc:
        _log.debug('schema_for failed on %s/%s: %s', extension_dir, tool_id, exc)
    if isinstance(fallback, dict):
        return fallback
    return _empty_schema()


def tool_catalog(extension_dir) -> List[dict]:
    """给 extension_host.list_tools 直接消费的目录：[{name, description, parameters}]。

    name 是**运行时裸名**（宿主工具 id 按 tool_loop 同样规则清洗后的名字），按名排序保证幂等。
    """
    try:
        tools = extract_tools(extension_dir)
    except Exception:
        return []
    catalog: List[dict] = []
    for tid in sorted(tools):
        item = tools[tid]
        catalog.append({'name': item.get('name') or _clean_name(tid),
                        'description': item.get('description') or item.get('name') or tid,
                        'parameters': item.get('parameters') or _empty_schema()})
    return catalog


def scan_stats(extension_dir, entry_files=None) -> dict:
    """诊断用：一个包扫了哪些文件、找到几个注册调用、抽出几个工具、为什么没抽到。"""
    stats = {'files': [], 'calls': 0, 'tools': 0, 'withParameters': 0, 'reason': '',
             'elapsedMs': 0.0}
    started = time.monotonic()
    try:
        root = Path(extension_dir)
        if not root.is_dir():
            stats['reason'] = 'not-a-directory'
            return stats
        files = _closure(root, _entry_files(root, entry_files))
        stats['files'] = files
        if not files:
            stats['reason'] = 'no-entry-file'
            return stats
        for name in files:
            text = _read_text(root / name, MAX_FILE_BYTES)
            if text:
                stats['calls'] += len(_call_sites(text))
        tools = extract_tools(root)
        stats['tools'] = len(tools)
        stats['withParameters'] = sum(1 for item in tools.values() if item.get('parameters'))
        if not tools:
            stats['reason'] = 'no-registerTool-object' if not stats['calls'] else 'unparsable'
        elif not stats['withParameters']:
            stats['reason'] = 'no-parameters-key'
    except Exception as exc:
        stats['reason'] = 'error:%s' % type(exc).__name__
        _log.debug('scan_stats failed on %s: %s', extension_dir, exc)
    stats['elapsedMs'] = round((time.monotonic() - started) * 1000, 2)
    return stats
