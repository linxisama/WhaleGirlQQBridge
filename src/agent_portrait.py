"""自画像「我是谁」：把每天重写的一份自述写进 我是谁.md，旧版按时间快照。

设计参考鱼缸项目的 bridge/scripts/self-portrait.js，但保留本项目的边界：

- 本模块只做「读素材 → 组装提示 → 落盘 → 维护记忆」，**不联网、不碰 AIEngine**；
  模型函数由调用方注入（refresh(generate=...)），测试可以完全离线；
- 旧版不覆盖式丢弃：写新正文前先把旧正文快照到 <data_dir>/我是谁-history/；
- 正文同时维护成一条 layer='soul'、tags=['portrait'] 的记忆：默认人设（persona=''）留空 = 全局 soul，
  其它人设带自己的 persona，读写都按人设过滤；
- 写入一律 UTF-8 无 BOM、LF，同目录 .tmp 前缀 + os.replace 原子替换。

开关 ai.portrait_enabled 默认关闭：关着时 refresh 直接返回 disabled，连提示都不组装。
"""
import inspect
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

DEFAULTS = {'enabled': False}
MAX_BODY = 4000
MIN_BODY = 80
PREV_CAP = 1600
SOUL_CAP = 1200
DIARY_CAP = 1200
SOUL_LIMIT = 20
MIN_SECTION = 200
PORTRAIT_TAG = 'portrait'
PORTRAIT_SOURCE = 'portrait'
# unified_memory.remember 会把不在 SOURCES 里的 source 静默改成 'desktop'（真实签名行为），
# 所以"这条是不是自画像"只认 tags 里的 PORTRAIT_TAG，source 命中只作为兼容分支。
PORTRAIT_FILE = '我是谁.md'
HISTORY_DIR = '我是谁-history'
PERSONA_DIR = 'personas'
PERSONA_MAX = 64
# 跟 agent_dream.DIARY_NAME 同一个文件名；两模块互相 import 会绕成环，所以各写各的。
DIARY_FILE = '日记.md'

INSTRUCTION = (
    '请用第一人称重写一份「我是谁」：200–400 字，中文散文，不分小标题、不用列表；\n'
    '只写材料里能看出来是真的东西，不编造；上一版写得还对的地方就沿用，不要为了改而改；\n'
    '不写「作为 AI」这类话，不写代码和路径。\n'
    '只输出正文：不要标题、不要代码围栏、不要任何解释。'
)

_FENCE = re.compile(r'^[ \t]*```[^\n]*$', re.M)
_HEADLINE = re.compile(r'^[ \t]*(?:#{1,6}[ \t]*)?我是谁[ \t]*[：:]?[ \t]*$')
_HEAD_PREFIX = re.compile(r'^[ \t]*(?:#{1,6}[ \t]*)?我是谁[ \t]*[：:][ \t]*')
_BLANK_RUNS = re.compile(r'\n{3,}')


# ---------- 配置 ----------

def _flag(value):
    """只把明确的真值当成开；其它（含 None、'maybe'）一律回落 False。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or '').strip().lower()
    return text in ('1', 'true', 'yes', 'on', '开', '开启')


def _cfg_get(config, key, default):
    """读点号键（如 ai.portrait_enabled）：点号访问对象、嵌套 dict、.config 属性三路都认。

    取值口径对齐 src/agent_dream.py:39 的 _fetch：先问一次扁平点号键，拿不到再把 key
    拆成「段名 + 末尾键」（ai / portrait_enabled）去嵌套 dict 里找，最后才回落默认值。
    """
    if config is None:
        return default
    getter = getattr(config, 'get', None)
    if callable(getter):
        try:
            value = getter(key)
        except Exception:
            value = None
        if value is not None:
            return value
    section = config if isinstance(config, dict) else getattr(config, 'config', None)
    if isinstance(section, dict):
        head, _, name = key.rpartition('.')
        inner = section.get(head)
        if isinstance(inner, dict) and inner.get(name) is not None:
            return inner[name]
    return default


def settings(config) -> dict:
    """读 ai.portrait_enabled；点号键与嵌套 dict 两种口径都认，缺键、None、非法字符串回落 False。"""
    return {'enabled': _flag(_cfg_get(config, 'ai.portrait_enabled', DEFAULTS['enabled']))}


# ---------- 人设 ----------

_CONTROL = re.compile(r'[\x00-\x1f\x7f]')
_NAME_BAD = re.compile(r'[<>:"|?*\\/]')
_RESERVED = frozenset({'CON', 'PRN', 'AUX', 'NUL'} |
                      {'COM%d' % index for index in range(1, 10)} |
                      {'LPT%d' % index for index in range(1, 10)})


def persona_slug(persona) -> str:
    """人设名 → 安全的目录名：去控制字符、路径分隔符与 Windows 非法字符，最长 PERSONA_MAX。

    默认人设（'' 或清洗后什么都不剩）返回 ''，由 persona_dir 决定落回 data_dir 本身。
    清洗只会把字符换成 '-'，不保留任何分隔符，所以拼出来的目录永远在 data_dir 之内。
    """
    text = _CONTROL.sub('', str(persona or '')).strip()
    text = _NAME_BAD.sub('-', text).strip('. ')
    text = text[:PERSONA_MAX].strip('. ')
    if text.upper() in _RESERVED:
        text = '_' + text
    return text


def persona_dir(data_dir, persona='') -> Path:
    """人设数据目录：默认人设就是 data_dir 本身（旧路径逐字节不变），其余进 personas/<slug>/。"""
    slug = persona_slug(persona)
    root = Path(data_dir)
    if not slug:
        return root
    return root / PERSONA_DIR / slug


def persona_search(memory, persona, query='', **kwargs):
    """按人设取记忆：默认人设保持改动前的全局口径，非默认人设硬过滤到自己那份。

    strict_scope 只在 persona 非空时才在 SQL 里加 persona 条件，所以默认人设仍然读得到
    历史数据（那些行 persona 本来就是 ''）。
    """
    slug = persona_slug(persona)
    if slug:
        return memory.search(query=query, persona=slug, strict_scope=True, **kwargs)
    return memory.search(query=query, **kwargs)


# ---------- 路径 ----------

def portrait_path(data_dir, persona='') -> Path:
    return persona_dir(data_dir, persona) / PORTRAIT_FILE


def history_dir(data_dir, persona='') -> Path:
    return persona_dir(data_dir, persona) / HISTORY_DIR


# ---------- 读 ----------

def read_portrait(data_dir, persona='') -> str:
    """读当前自画像正文；不存在或读失败返回空串（BOM 与首尾空白一并去掉）。"""
    try:
        text = portrait_path(data_dir, persona).read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return ''
    return text.lstrip('\ufeff').strip()


# ---------- 清洗 / 落盘 ----------

def _tidy(text):
    return _BLANK_RUNS.sub('\n\n', text).strip()


def clean_text(raw) -> str:
    """清洗模型输出：去代码围栏行、去首行「我是谁：」这类标题、统一 LF、去多余空行。"""
    body = str(raw or '').replace('\r\n', '\n').replace('\r', '\n')
    body = _FENCE.sub('', body)
    lines = body.split('\n')
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if _HEADLINE.match(line):
            lines[index] = ''
        elif _HEAD_PREFIX.match(line):
            lines[index] = _HEAD_PREFIX.sub('', line, count=1)
        break
    return _tidy('\n'.join(lines))


def _write_text(path, text):
    """同目录 .tmp 前缀临时文件 + os.replace：UTF-8 无 BOM，LF，不留下半截文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.tmp-' + path.name + '-' + uuid.uuid4().hex[:8])
    try:
        with open(temporary, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _stamp(now):
    moment = now
    if moment is None:
        moment = datetime.now()
    elif isinstance(moment, (int, float)):
        moment = datetime.fromtimestamp(float(moment))
    elif not hasattr(moment, 'strftime'):
        moment = datetime.now()
    return moment.strftime('%Y%m%d-%H%M%S')


def _snapshot(data_dir, old, now, persona=''):
    """旧正文进 我是谁-history/<YYYYmmdd-HHMMSS>.md；同一秒重复时追加 -2、-3…"""
    stamp = _stamp(now)
    folder = history_dir(data_dir, persona)
    name, index = stamp, 1
    while True:
        target = folder / f'{name}.md'
        if not target.exists():
            break
        index += 1
        name = f'{stamp}-{index}'
    _write_text(target, old)
    return str(target)


# ---------- 记忆 ----------

def _is_portrait(item):
    if PORTRAIT_SOURCE == str(item.get('source') or ''):
        return True
    return PORTRAIT_TAG in (item.get('tags') or [])


def _upsert_memory(memory, body, source, persona=''):
    """自画像在记忆里只留一条：找到就 update，找不到才 remember；按人设各留一条。"""
    evidence = f'自画像重写（{source}）'[:1000]
    for item in persona_search(memory, persona, '', layer='soul', limit=50):
        if _is_portrait(item):
            updated = memory.update(item['id'], content=body, importance=95, evidence=evidence)
            return str((updated or item)['id']), False
    created = memory.remember(body, source=PORTRAIT_SOURCE, layer='soul', importance=95,
                              confidence=95, channel='', persona=persona_slug(persona),
                              tags=[PORTRAIT_TAG], evidence=evidence)
    return str(created['id']), not created.get('merged', False)


# ---------- 提示 ----------

def _soul_digest(memory, persona=''):
    lines = []
    for item in persona_search(memory, persona, '', layer='soul', limit=SOUL_LIMIT):
        if _is_portrait(item):
            continue
        text = str(item.get('content') or '').strip()
        if text and len(lines) < SOUL_LIMIT:
            lines.append('- ' + text[:200])
    return '\n'.join(lines)


def _diary_digest(memory, data_dir=None, persona=''):
    """日记尾部：默认人设沿用 memory.diary_tail（旧口径），非默认人设只读自己目录里的日记。

    diary_entries 表没有 persona 列（day 还是 UNIQUE），全局日记尾巴无法按人设过滤，
    所以非默认人设宁可这一段空着，也不把别人的日记塞进自己的自画像提示词。
    """
    slug = persona_slug(persona)
    if slug:
        if data_dir is None:
            return ''
        try:
            raw = (persona_dir(data_dir, persona) / DIARY_FILE).read_text(
                encoding='utf-8', errors='ignore')
        except OSError:
            return ''
        return str(raw or '').strip()[-DIARY_CAP:]
    reader = getattr(memory, 'diary_tail', None)
    if not callable(reader):
        return ''
    try:
        return str(reader(DIARY_CAP) or '').strip()
    except Exception:
        return ''


def build_prompt(memory, data_dir, *, max_chars=3000, now=None, persona='') -> str:
    """拼素材（旧版自述 / soul 记忆 / 日记尾部）+ 固定指令；超长只裁素材，不裁指令。"""
    try:
        max_chars = max(400, min(20000, int(max_chars or 3000)))
    except (TypeError, ValueError):
        max_chars = 3000
    sections = []
    previous = read_portrait(data_dir, persona)
    if previous:
        sections.append(('【上一版自述，可以沿用】', previous[:PREV_CAP]))
    soul = _soul_digest(memory, persona)
    if soul:
        sections.append(('【记忆里最要紧的几条】', soul[:SOUL_CAP]))
    diary = _diary_digest(memory, data_dir, persona)
    if diary:
        sections.append(('【最近的日记（时间线）】', diary[:DIARY_CAP]))
    budget = max_chars - len(INSTRUCTION) - 2

    def _join():
        return '\n\n'.join(f'{title}\n{text}' for title, text in sections)

    body = _join()
    # 超预算从素材尾部开始收缩：先截最后一段，短到没意义就整段丢掉，指令永远不动。
    while sections and len(body) > budget:
        title, text = sections[-1]
        keep = len(text) - (len(body) - budget)
        if keep < MIN_SECTION:
            sections.pop()
        else:
            sections[-1] = (title, text[:keep])
        body = _join()
    return (body + '\n\n' + INSTRUCTION) if body else INSTRUCTION


# ---------- 写 ----------

def write_portrait(memory, data_dir, text, *, source='dream', now=None, persona='') -> dict:
    """旧版快照 → 原子写正文 → 维护唯一的 soul 记忆（每个 persona 各一条）。"""
    body = _tidy(str(text or '').replace('\r\n', '\n').replace('\r', '\n'))
    truncated = len(body) > MAX_BODY
    if truncated:
        body = body[:MAX_BODY].rstrip()
    old = read_portrait(data_dir, persona)
    snapshot = _snapshot(data_dir, old, now, persona) if old else ''
    path = portrait_path(data_dir, persona)
    _write_text(path, body)
    memory_id, created = _upsert_memory(memory, body, source, persona)
    return {'ok': True, 'path': str(path), 'chars': len(body), 'truncated': truncated,
            'snapshot': snapshot, 'memory_id': memory_id, 'created': created}


# ---------- 刷新 ----------

def refresh(memory, data_dir, *, generate, config=None, now=None, persona='') -> dict:
    """关着就不动；开着就：组装提示 → generate(prompt) → 清洗 → 落盘并维护记忆。

    只支持同步 generate：返回 awaitable 时不做事件循环，直接报
    generate_async_unsupported 并关掉协程（避免 "never awaited" 警告）。
    """
    if not settings(config).get('enabled'):
        return {'ok': False, 'reason': 'disabled'}
    prompt = build_prompt(memory, data_dir, now=now, persona=persona)
    try:
        raw = generate(prompt)
    except Exception as error:
        return {'ok': False, 'reason': 'generate_failed', 'detail': str(error)}
    if inspect.isawaitable(raw):
        closer = getattr(raw, 'close', None)
        if callable(closer):
            closer()
        return {'ok': False, 'reason': 'generate_async_unsupported'}
    body = clean_text(raw)
    if len(body) < MIN_BODY:
        return {'ok': False, 'reason': 'too_short'}
    result = write_portrait(memory, data_dir, body, now=now, persona=persona)
    return {**result, 'ok': True}
