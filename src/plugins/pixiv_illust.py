"""Pixiv图片检索：Lolicon兼容服务的全年龄图片，下载验证后走QQ富媒体发送。
默认关闭；不把Cookie发送给第三方，不执行未验证的直连模式。
作品去重只在发送成功后记录，网络失败明确反馈。
"""

from __future__ import annotations

import random
import re
import json
import asyncio
import time
from typing import Optional
from urllib.parse import quote, urlencode
from ..public_web import read_public
from ..sticker_library import fetch_image, decode_image
import base64

from . import PluginBase, PluginMeta, FieldSchema

VALID_RATINGS = ("sfw", "r18", "all")
DEFAULT_SEARCH_TEMPLATE = "https://www.pixiv.net/ajax/search/artworks/{kw}?word={kw}&p={page}"
DEFAULT_IMAGE_TEMPLATE = "https://pixiv.re/{pid}-{page}.png"
STATE_FILE = "pixiv_illust.json"


def parse_ratings(raw) -> dict:
    """解析形如 ``group:123=r18,private:456=sfw`` 的按会话分级。

    值只接受 ``sfw``/``r18``/``all``（大小写不敏感）；键必须是
    ``group:数字`` 或 ``private:数字``；非法项忽略（不抛异常）。
    """
    result = {}
    for item in re.split(r'[,，\s]+', str(raw or "")):
        item = item.strip()
        if "=" not in item:
            continue
        key, _, value = item.partition("=")
        key = key.strip()
        value = value.strip().lower()
        if not re.match(r'^(group|private):\d+$', key):
            continue
        if value not in VALID_RATINGS:
            continue
        result[key] = value
    return result


def rating_allowed(rating, allowed) -> bool:
    """按会话分级判断这张图能不能发。未知 rating 一律 False（保守侧）。"""
    rating = str(rating or "").strip().lower()
    allowed = str(allowed or "").strip().lower()
    if rating not in ("sfw", "r18"):
        return False
    if allowed == "all":
        return True
    return rating == allowed


def build_search_url(template, keyword, page) -> str:
    """把 ``{kw}`` 与 ``{page}`` 填进搜索模板。

    占位符缺失时的稳定规则（写死在这里，别再改）：
    缺少 ``{kw}`` 就把 ``kw=<编码后的关键词>`` 追加到末尾；
    缺少 ``{page}`` 就把 ``page=<页码>`` 追加到末尾。追加用 ``&`` 连接。
    关键词用 ``urllib.parse.quote``（空格编码为 %20，不是 +）。
    """
    text = str(template or "").strip() or DEFAULT_SEARCH_TEMPLATE
    encoded = quote(str(keyword if keyword is not None else ""), safe="")
    if "{kw}" in text:
        text = text.replace("{kw}", encoded)
    else:
        text += ("&" if "?" in text else "?") + f"kw={encoded}"
    if "{page}" in text:
        text = text.replace("{page}", str(int(page) if str(page).lstrip("-").isdigit() else 1))
    else:
        text += "&page=" + str(int(page) if str(page).lstrip("-").isdigit() else 1)
    return text


def build_image_url(template, pid, page=1) -> str:
    """把 ``{pid}`` 与 ``{page}`` 填进图片模板；缺失时按同样规则追加。"""
    text = str(template or "").strip() or DEFAULT_IMAGE_TEMPLATE
    safe_page = int(page) if str(page).lstrip("-").isdigit() else 1
    if safe_page < 1:
        safe_page = 1
    if "{pid}" in text:
        text = text.replace("{pid}", str(pid))
    else:
        text += ("&" if "?" in text else "?") + f"pid={pid}"
    if "{page}" in text:
        text = text.replace("{page}", str(safe_page))
    else:
        text += "&page=" + str(safe_page)
    return text


def pick_candidates(works, ratings_allowed, max_count, exclude_ai=False) -> list:
    """筛选候选作品：分级过滤 + AI 过滤 + 按 id 去重 + 截断，保持输入顺序。

    ``works`` 元素形如 ``{'id': '123', 'pageCount': 2, 'xRestrict': 0, 'aiType': 1}``；
    ``xRestrict >= 1`` 视为 r18，``aiType == 2`` 视为 AI 作品。
    """
    limit = max(0, int(max_count) if str(max_count).lstrip("-").isdigit() else 0)
    seen = set()
    result = []
    if limit <= 0:
        return result
    for work in works or []:
        if not isinstance(work, dict):
            continue
        work_id = work.get("id")
        if work_id in seen:
            continue
        if exclude_ai and work.get("aiType") == 2:
            continue
        try:
            restrict = int(work.get("xRestrict") or 0)
        except (TypeError, ValueError):
            restrict = 0
        rating = "r18" if restrict >= 1 else "sfw"
        if not rating_allowed(rating, ratings_allowed):
            continue
        seen.add(work_id)
        result.append(work)
        if len(result) >= limit:
            break
    return result


def escape_cq_text(text) -> str:
    """转义 CQ 码特殊字符。顺序重要：``&`` 必须先替换，否则会把新 & 再转一遍。"""
    result = str(text if text is not None else "")
    result = result.replace("&", "&amp;")
    result = result.replace("[", "&#91;")
    result = result.replace("]", "&#93;")
    result = result.replace(",", "&#44;")
    return result


def pick_image_index(page_count, picker=None) -> int:
    """在多页作品里挑一页（1 起算）；``page_count <= 0`` 返回 1。

    ``picker`` 接收候选列表 ``[1..page_count]`` 并返回其一，便于测试。
    """
    try:
        count = int(page_count)
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return 1
    candidates = list(range(1, count + 1))
    choose = picker or random.choice
    picked = choose(candidates)
    if picked not in candidates:
        return 1
    return int(picked)


class PixivIllust(PluginBase):
    META = PluginMeta(
        id="pixiv_illust",
        name="Pixiv 插图",
        description=(
            "启用后发 /插图 关键词，读取配置的Lolicon兼容服务并发送真实图片，附作品及作者来源。"
            "当前接入全年龄图片；Pixiv直连和Cookie模式尚未验证，不能只改开关就使用。默认关闭。"
        ),
        category="media",
        enabled_by_default=False,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用"),
            "searchBackend": FieldSchema(
                type="select", label="搜索后端", values=["auto", "lolicon", "pixiv"],
                description="auto = 按可用情况挑；lolicon 用第三方 API；pixiv 直连搜索。",
            ),
            "loliconApiUrl": FieldSchema(
                type="string", label="Lolicon API 地址",
                description="留空 = 用默认地址。需要联网才能用。",
            ),
            "searchUrlTemplate": FieldSchema(
                type="string", label="搜索地址模板", multiline=True,
                description="占位符 {kw}（关键词）与 {page}（页码）；缺失时会自动追加。",
            ),
            "imageUrlTemplate": FieldSchema(
                type="string", label="图片地址模板", multiline=True,
                description="占位符 {pid}（作品号）与 {page}（页码）。",
            ),
            "cookie": FieldSchema(
                type="string", label="Pixiv Cookie（敏感）", multiline=True,
                description="本插件不读不写这个值，只由你手动填写；接入取图时才用得上。",
            ),
            "userAgent": FieldSchema(type="string", label="User-Agent"),
            "proxyUrl": FieldSchema(type="string", label="代理地址", description="留空 = 不使用代理。"),
            "excludeAI": FieldSchema(type="bool", label="排除 AI 作品"),
            "poolSize": FieldSchema(type="int", label="候选池大小", min=1, max=200),
            "maxCount": FieldSchema(type="int", label="单次最多几张", min=1, max=20),
            "ratings": FieldSchema(
                type="string", label="按会话分级",
                description="形如 group:123=r18,private:456=sfw；值取 sfw/r18/all。",
            ),
            "allowR18": FieldSchema(type="bool", label="允许 R18", description="默认关。"),
            "ownerIds": FieldSchema(type="string", label="主人 QQ（逗号分隔）"),
            "adminIds": FieldSchema(type="string", label="管理员 QQ（逗号分隔）"),
            "stateCap": FieldSchema(
                type="int", label="已见作品记录上限", min=10, max=100000,
                description="超过后淘汰最旧的记录。",
            ),
            "retryCandidates": FieldSchema(type="int", label="失败后重试候选数", min=0, max=50),
            "deadTtlDays": FieldSchema(type="int", label="失效链接保留天数", min=0, max=3650),
            "timeoutMs": FieldSchema(type="int", label="请求超时（毫秒）", min=1000, max=60000),
        },
        default_settings={
            "enabled": False,
            "searchBackend": "auto",
            "loliconApiUrl": "https://api.lolicon.app/setu/v2",
            "searchUrlTemplate": DEFAULT_SEARCH_TEMPLATE,
            "imageUrlTemplate": DEFAULT_IMAGE_TEMPLATE,
            "cookie": "",
            "userAgent": "",
            "proxyUrl": "",
            "excludeAI": False,
            "poolSize": 20,
            "maxCount": 3,
            "ratings": "",
            "allowR18": False,
            "ownerIds": "",
            "adminIds": "",
            "stateCap": 200,
            "retryCandidates": 5,
            "deadTtlDays": 7,
            "timeoutMs": 10000,
        },
    )

    def __init__(self, manager):
        super().__init__(manager)
        self._loaded = False
        self._seen: dict = {}

    # ── 状态 ──────────────────────────────────────────────────────────────
    def _ensure_loaded(self) -> None:
        """从基类的状态文件读一次。

        注意方法名不能叫 ``load_state``：那会覆盖基类同名方法，而基类实现里又会调
        ``self.load_state(...)``（原稿因此无限递归）。
        """
        if self._loaded:
            return
        raw = self.load_state(STATE_FILE, {"version": 1, "seen": {}})
        seen = raw.get("seen") if isinstance(raw, dict) else None
        self._seen = dict(seen) if isinstance(seen, dict) else {}
        self._loaded = True
        self._prune()

    def state(self) -> dict:
        self._ensure_loaded()
        return {"version": 1, "seen": dict(self._seen)}

    def _prune(self) -> None:
        limit = max(1, int(self.get("stateCap", 200) or 200))
        while len(self._seen) > limit:
            # 淘汰最旧的：按时间戳排序，时间戳相同时按插入顺序（dict 保序）
            oldest = min(self._seen, key=lambda key: (float(self._seen[key] or 0), list(self._seen).index(key)))
            self._seen.pop(oldest, None)

    def remember(self, work_id, now: Optional[float] = None) -> None:
        """记下"这个作品见过了"。"""
        import time
        self._ensure_loaded()
        self._seen[str(work_id)] = float(time.time() if now is None else now)
        self._prune()

    def persist(self) -> None:
        try:
            self._ensure_loaded()
            self.save_state(STATE_FILE, {"version": 1, "seen": dict(self._seen)})
        except Exception:
            pass

    def already_seen(self, work_id) -> bool:
        self._ensure_loaded()
        return str(work_id) in self._seen

    # ── 钩子 ──────────────────────────────────────────────────────────────
    def on_ingest(self, message: dict, runtime) -> Optional[str]:
        """联网操作由异步on_prepare接收，不在同步接收循环阻塞。"""
        return None

    async def on_prepare(self,message,runtime):
        text=str(message.get('text') or '').strip()
        if text!='/插图' and not text.startswith('/插图 '):return
        message.pop('_pixiv_selected',None);message.pop('_reply_meta',None)
        message['_direct_reply']='插图服务未返回可发送图片。'
        if self.get('searchBackend')=='pixiv':
            message['_direct_reply']='Pixiv直连尚未验证，请在配置中选择 auto 或 lolicon。';return
        keyword=text[3:].strip()[:100]
        if not keyword:
            message['_direct_reply']='用法：/插图 关键词，例如 /插图 海洋。';return
        timeout=max(1,float(self.get('timeoutMs',10000))/1000)
        try:
            limit=min(int(self.get('maxCount',3)),1 if runtime.mode=='official' else 20)
            endpoint=str(self.get('loliconApiUrl') or '').strip()
            if not endpoint:raise ValueError('服务地址为空')
            url=endpoint+('&' if '?' in endpoint else '?')+urlencode({'r18':0,'num':min(20,int(self.get('poolSize',20))),
                'keyword':keyword,'excludeAI':'true' if self.get('excludeAI') else 'false','size':'regular'})
            raw=json.loads(await asyncio.wait_for(read_public(url,timeout=timeout,proxy=str(self.get('proxyUrl') or '') or None,
                headers={'User-Agent':str(self.get('userAgent') or 'Mozilla/5.0')}),timeout))
            if raw.get('error'):raise ValueError('插图来源返回错误')
            works=raw.get('data')
            if not isinstance(works,list):raise ValueError('插图来源数据格式错误')
            selected=[];media=[];tries=0;deadline=time.monotonic()+timeout
            for work in works[:200]:
                if not isinstance(work,dict) or not str(work.get('pid','')).isdigit():continue
                if work.get('r18') is not False:continue
                if self.get('excludeAI') and work.get('aiType')==2:continue
                pid=str(work['pid'])
                if self.already_seen(pid):continue
                address=(work.get('urls') or {}).get('regular') or (work.get('urls') or {}).get('original')
                if not isinstance(address,str):continue
                tries+=1
                try:
                    image=await asyncio.wait_for(fetch_image(address),max(.01,deadline-time.monotonic()))
                    _,_,buffer=decode_image(image);buffer.close()
                except Exception:
                    if time.monotonic()>=deadline or tries>=limit+int(self.get('retryCandidates',5)):break
                    continue
                media.append({'type':'image','data':{'file':'base64://'+base64.b64encode(image).decode()}})
                selected.append({'id':pid,'title':str(work.get('title') or '')[:120],
                    'author':str(work.get('author') or '')[:80],'url':'https://www.pixiv.net/artworks/'+pid})
                if len(media)>=limit:break
            if not media:
                message['_direct_reply']='没有找到未发送过的可用全年龄图片，或图片下载失败。';return
            message['_pixiv_selected']=selected
            message['_reply_meta']={'media':media}
            message['_direct_reply']='\n'.join(f"{v['title']} · {v['author']}\n{v['url']}" for v in selected)
        except Exception as exc:
            runtime.note(f'插图服务读取失败（{type(exc).__name__}）')
            message['_direct_reply']='插图服务暂时不可用，请检查插件中的服务地址或代理；本次没有发出图片。'

    def on_sent(self,message,reply,runtime):
        for entry in message.get('_pixiv_selected',[]):self.remember(entry['id'])
        if message.get('_pixiv_selected'):self.persist()
