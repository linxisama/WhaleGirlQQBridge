"""market_probe.py - 插件市场探针：抓目录、按关键词筛出跟机器人有关的插件。

以前的"市场探针"只是安装流程的测试壳子（只有元数据、没有任何钩子、装了不干活）。
现在它真的做事：读配置里的市场目录页 → 解析条目（名称/作者/链接/简介/标签）→
按关键词过滤 → 给管理员回一份清单。

**它不安装任何东西**：远程代码仍旧要人在控制台「插件 → 添加插件」里看过预检、
点过"确认安装"才会落地 —— 探针只负责"看"，不负责"装"。

支持两种目录格式：

1. JSON 目录（推荐给自家站点）：``{"plugins": [{"id","name","url","description","tags"}]}``
   或直接一个数组；字段名兼容 ``name/title``、``url/link/homepage``、``tags/keywords``。
2. HTML 目录页：认 ``detail.php?id=...`` 这类详情链接（DeepSeek Harness 插件商店就是这个形状），
   从链接后面的正文里抽作者/简介/标签。
"""
from __future__ import annotations

import json
import re
from html import unescape

from . import PluginBase, PluginMeta, FieldSchema

DEFAULT_MARKET = 'https://kondius.cn/api/community/plugins'
DEFAULT_KEYWORDS = '机器人,QQ,消息,记忆,token,微信'
MAX_ITEMS = 200

# 本项目从 QQ Agent 社区市场移植过的插件（市场 id → 我们的插件名）。
# 探针会在清单里标出来，避免重复评估同一个插件。
PORTED = {
    'minimal-mode': '极简模式',
    'token-saver': 'Token 节流',
    'cache-stability': '缓存稳定性',
    'affinity': '群友好感度',
    'member-aliases': '群友外号对照',
    'group-cost-report': '群友花费统计',
    'reply-freshness': '回复时机与引用',
    'early-finish': '提前收尾（省 token）',
}
# 评估过但不移植的（依据见 功能对照与使用说明.md）
REJECTED = {
    'context-reuse': '宿主为无状态会话补历史，本项目每轮已重建历史',
    'finish-merge': '宿主每条回复两次模型调用，本项目只调一次',
}
_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'[ \t\r\f\v]+')
_DETAIL_RE = re.compile(r'<a\s[^>]*href="(?P<href>[^"]*detail\.php\?id=[^"]*)"[^>]*>(?P<text>.*?)</a>', re.S | re.I)
# 第二种常见形状：详情页是 /plugins/<slug>/ 这种路径（dshbase 的目录就是这样）。
# 目录页本身（…/plugins/、…/plugins/directory/）要排掉。
_SLUG_RE = re.compile(r'<a\s[^>]*href="(?P<href>[^"]*?/plugins/(?P<slug>[A-Za-z0-9._\-]{2,60})/)"[^>]*>(?P<text>.*?)</a>', re.S | re.I)
_SKIP_SLUGS = {'directory', 'index', 'all', 'search'}
_JSON_LIKE = re.compile(r'^\s*[\[{]')


def _text(raw) -> str:
    """把一段 HTML 压成一行纯文本。"""
    value = unescape(_TAG_RE.sub(' ', str(raw or '')))
    return _WS_RE.sub(' ', value).replace('\n', ' ').strip()


def _entry(name, url, author='', description='', tags=(), **extra) -> dict:
    entry = {'name': str(name or '').strip(), 'url': str(url or '').strip(),
             'author': str(author or '').strip(), 'description': str(description or '').strip(),
             'tags': [str(t).strip() for t in (tags or []) if str(t).strip()]}
    entry.update({key: value for key, value in extra.items() if value not in (None, '', 0, [])})
    return entry


def parse_market(text, base_url: str = '') -> list[dict]:
    """解析市场目录。返回 ``[{name,url,author,description,tags}]``，解析不出来就返回空表。"""
    body = str(text or '')
    if not body.strip():
        return []
    if _JSON_LIKE.match(body):
        return _parse_json_market(body, base_url)
    return _parse_html_market(body, base_url)


def _absolute(url: str, base_url: str) -> str:
    value = str(url or '').strip()
    if not value:
        return ''
    if value.startswith(('http://', 'https://')):
        return value
    if not base_url:
        return value
    return base_url.rstrip('/') + '/' + value.lstrip('/')


def _parse_json_market(body: str, base_url: str) -> list[dict]:
    try:
        data = json.loads(body)
    except ValueError:
        return []
    items = data
    if isinstance(data, dict):
        items = data.get('plugins') or data.get('items') or data.get('data') or []
    if not isinstance(items, list):
        return []
    entries = []
    for item in items[:MAX_ITEMS]:
        if isinstance(item, str):
            entries.append(_entry(item, item))
            continue
        if not isinstance(item, dict):
            continue
        name = item.get('name') or item.get('title') or item.get('id') or ''
        url = item.get('url') or item.get('link') or item.get('homepage') or item.get('repo') or ''
        if not url and item.get('downloadUrl'):
            # QQ Agent 社区市场的形状：相对路径 /api/community/plugins/<id>/download
            url = _absolute(str(item['downloadUrl']), 'https://kondius.cn')
        tags = item.get('tags') or item.get('keywords') or []
        if isinstance(tags, str):
            tags = [t for t in re.split(r'[,，\s]+', tags) if t]
        entries.append(_entry(name, _absolute(url, base_url),
                              item.get('author') or item.get('owner') or '',
                              item.get('description') or item.get('summary') or '', tags,
                              market_id=str(item.get('id') or '').strip(),
                              category=str(item.get('categoryName') or item.get('category') or '').strip(),
                              version=str(item.get('version') or '').strip(),
                              downloads=int(item.get('downloads') or 0),
                              install_code=str(item.get('installCode') or '').strip()))
    return [e for e in entries if e['name']]


def _parse_html_market(body: str, base_url: str) -> list[dict]:
    """HTML 目录：认详情链接，并从链接后面的正文里抽 作者 / 简介 / 标签。"""
    entries, seen = [], set()
    matches = list(_DETAIL_RE.finditer(body))
    if not matches:
        matches = [m for m in _SLUG_RE.finditer(body) if (m.group('slug') or '').lower() not in _SKIP_SLUGS]
    for match in matches:
        name = _text(match.group('text')) or match.groupdict().get('slug', '')
        url = _absolute(match.group('href'), base_url)
        if not name or url in seen:
            continue
        seen.add(url)
        tail = _text(body[match.end():match.end() + 900])
        parts = [p for p in re.split(r'\s{2,}', tail) if p]
        author = parts[0] if parts else ''
        description = parts[1] if len(parts) > 1 else ''
        tags = []
        for part in parts[2:4]:
            if re.fullmatch(r'[\w\-\u4e00-\u9fff ]{2,60}', part or ''):
                tags.extend(t for t in part.split(' ') if t)
        entries.append(_entry(name, url, author, description, tags[:8]))
        if len(entries) >= MAX_ITEMS:
            break
    return entries


def filter_entries(entries, keywords) -> list[dict]:
    """按关键词过滤（命中名称/简介/标签，大小写不敏感）；关键词为空则全都要。"""
    words = [w.strip().lower() for w in re.split(r'[,，\s]+', str(keywords or '')) if w.strip()]
    if not words:
        return list(entries)
    hits = []
    for entry in entries:
        haystack = ' '.join([entry.get('name', ''), entry.get('description', ''),
                             ' '.join(entry.get('tags') or [])]).lower()
        if any(word in haystack for word in words):
            hits.append(entry)
    return hits


def format_report(entries, *, total=None, keywords='', url='', limit=8) -> str:
    """给管理员看的一段纯文本清单（不装任何东西）。"""
    head = f"插件市场：{url or '未配置地址'}"
    if total is not None:
        head += f"（共 {int(total)} 项"
        head += f"，命中「{keywords}」{len(entries)} 项）" if keywords else "）"
    lines = [head]
    if not entries:
        lines.append('没有命中项。可以换关键词，或在插件设置里改市场地址。')
    for index, entry in enumerate(entries[:max(1, int(limit or 8))], 1):
        who = f"（{entry['author']}）" if entry.get('author') else ''
        lines.append(f"{index}. {entry['name']}{who}{_status_of(entry)}")
        if entry.get('description'):
            lines.append(f"   {entry['description'][:110]}")
        bits = []
        if entry.get('category'):
            bits.append(str(entry['category']))
        if entry.get('version'):
            bits.append(f"v{entry['version']}")
        if entry.get('downloads'):
            bits.append(f"{entry['downloads']} 次下载")
        if entry.get('install_code'):
            bits.append(f"口令 {entry['install_code']}")
        if bits:
            lines.append('   ' + ' · '.join(bits))
        if entry.get('url'):
            lines.append(f"   {entry['url']}")
    if len(entries) > int(limit or 8):
        lines.append(f"……还有 {len(entries) - int(limit or 8)} 项，用“市场 <关键词>”缩小范围。")
    lines.append('探针只负责看：要安装请到控制台「插件 → 添加插件」走预检并确认后才落地。')
    return '\n'.join(lines)


def _status_of(entry) -> str:
    """标出这个市场插件在本项目里的状态（已移植 / 已评估不适用）。"""
    market_id = str(entry.get('market_id') or '').strip()
    if not market_id:
        return ''
    if market_id in PORTED:
        return f'　✅ 本项目已移植（{PORTED[market_id]}）'
    if market_id in REJECTED:
        return f'　⏭ 已评估不适用：{REJECTED[market_id]}'
    return ''


class MarketProbe(PluginBase):
    META = PluginMeta(
        id='market_probe', name='市场探针', category='utility', enabled_by_default=False,
        description=('抓取插件市场目录并按关键词筛出相关插件，结果只展示、不自动安装。'
                     '默认指向 QQ Agent 社区市场（kondius.cn/api/community/plugins），'
                     '清单里会标出哪些本项目已经移植过、哪些评估过但不适用；'
                     '也支持通用 JSON 目录（{"plugins":[{name,url,description,tags}]}）与常见 HTML 目录页。'
                     '管理员在 QQ 私聊里发“市场 [关键词]”，或在管理员模式下直接发“市场”。'),
        config_schema={
            'enabled': FieldSchema('bool', '启用市场探针'),
            'marketUrl': FieldSchema('string', '市场目录地址（http/https）'),
            'keywords': FieldSchema('string', '默认关键词（逗号分隔，留空=全部）'),
            'maxItems': FieldSchema('int', '最多展示条数', min=1, max=30),
            'timeoutSec': FieldSchema('int', '抓取超时（秒）', min=3, max=60),
        },
        default_settings={'enabled': False, 'marketUrl': DEFAULT_MARKET,
                          'keywords': DEFAULT_KEYWORDS, 'maxItems': 8, 'timeoutSec': 15})

    async def fetch(self, runtime, url: str = '', keywords: str = '', limit: int = None) -> str:
        """抓取 + 过滤 + 生成清单。任何失败都写成一句人话，不抛异常。"""
        target = str(url or self.get('marketUrl', DEFAULT_MARKET) or '').strip()
        if not re.match(r'^https?://', target):
            return '市场地址要以 http:// 或 https:// 开头，请在插件设置里填写。'
        words = str(keywords or self.get('keywords', '') or '')
        count = int(limit or self.get('maxItems', 8) or 8)
        timeout = max(3, int(self.get('timeoutSec', 15) or 15))
        try:
            from ..public_web import read_public
            body = await read_public(target, timeout=timeout)
        except Exception as exc:
            return f'市场目录抓取失败：{str(exc)[:120]}（可稍后重试或换地址）'
        entries = parse_market(body, target)
        if not entries:
            return (f'目录抓到了，但没解析出插件条目：{target}\n'
                    '常见原因：这个目录页是脚本动态渲染的（HTML 里只有导航链接）。\n'
                    '两种解决办法：① 把市场地址换成站点自己的 JSON 目录'
                    '（{"plugins":[{name,url,description,tags}]}）；'
                    '② 直接填某个插件的详情页地址。')
        hits = filter_entries(entries, words)
        runtime.note(f'[市场探针] {target} 命中 {len(hits)} 项（共 {len(entries)} 项）')
        return format_report(hits, total=len(entries), keywords=words, url=target, limit=count)

    def on_ingest(self, message, runtime):
        """管理员私聊“市场 [关键词]”即可探一次（只在插件已启用时）。"""
        if not self.get('enabled') or message.get('kind') != 'private':
            return None
        match = re.match(r'^[/\\]?市场(?:\s+(.*))?$', str(message.get('text') or '').strip())
        if not match:
            return None
        control = runtime.plugins._plugins.get('pc_control')
        if control is None or not control.is_admin(message):
            return '市场探针只回应绑定管理员的私聊指令。'
        message['_robot_admin'] = True
        return self.probe_now(runtime, match.group(1) or '')

    def probe_now(self, runtime, keywords: str = '') -> str:
        """同步外壳：给管理员通道用（内部跑一次抓取）。"""
        import asyncio

        async def run():
            return await self.fetch(runtime, keywords=keywords)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(run())
        # 已经在事件循环里（on_ingest 是同步钩子）：交给后台任务，先回一句"正在看"
        task = asyncio.ensure_future(run())
        task.add_done_callback(lambda t: runtime.note('[市场探针] ' + str(t.result() if not t.exception() else t.exception())[:300]))
        return '正在查看插件市场，稍后把结果记到控制台运行记录里。'
