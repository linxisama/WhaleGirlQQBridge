"""Owned, bounded sticker files; network inputs never become local paths."""
import asyncio
import base64
import copy
import hashlib
import ipaddress
import json
import re
import socket
import time
from pathlib import Path
from urllib.parse import urljoin,urlsplit
import httpx
from .image_input import image_source

MAX_BYTES=6*1024*1024
ID=re.compile(r'^[0-9a-f]{64}$')

def decode_image(data):
    """Decode using the same Qt image formats bundled with the desktop app."""
    if not data or len(data)>MAX_BYTES:
        raise ValueError('图片为空或超过6MB')
    source=image_source('base64://'+base64.b64encode(data).decode())
    if not source:
        raise ValueError('不支持的图片格式')
    extension={'jpeg':'jpg'}.get(source[11:].split(';')[0],source[11:].split(';')[0])
    from PyQt6.QtCore import QBuffer,QIODevice
    from PyQt6.QtGui import QImageReader
    buffer=QBuffer();buffer.setData(data);buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader=QImageReader(buffer);reader.setDecideFormatFromContent(True)
    size=reader.size()
    if not reader.canRead() or size.width()<=0 or size.height()<=0 or size.width()*size.height()>16000000:
        raise ValueError('图片损坏或尺寸过大')
    return extension,reader,buffer

def frames_for_vision(data,want=2):
    _,reader,buffer=decode_image(data)
    frames=[]
    for _ in range(max(1,min(int(want),3))):
        frame=reader.read()
        if frame.isNull():
            break
        # 普通图工作帧：1024 对聊天截图的小字偏小；长图仍走 tiles_for_vision。
        if frame.width()>1568 or frame.height()>1568:
            from PyQt6.QtCore import Qt
            frame=frame.scaled(1568,1568,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation)
        from PyQt6.QtCore import QBuffer,QIODevice
        out=QBuffer();out.open(QIODevice.OpenModeFlag.WriteOnly)
        if not frame.save(out,'PNG'):
            break
        frames.append('data:image/png;base64,'+base64.b64encode(bytes(out.data())).decode())
        if not reader.supportsAnimation():
            break
    buffer.close()
    if not frames:
        raise ValueError('图片无法解码')
    return frames

def tiles_for_vision(data, max_width=1024, tile_height=1700, overlap=100, max_tiles=6):
    """把长图切成可读的工作块，而不是把 900×9000 压成 102×1024。

    宽度不超过 max_width 时保持原像素宽度（文字不被横向缩小）；
    高度按块数均分并保留少量重叠，避免跨块的句子被切断。
    """
    _, reader, buffer = decode_image(data)
    try:
        image = reader.read()
    finally:
        buffer.close()
    if image.isNull():
        raise ValueError('图片无法解码')
    from PyQt6.QtCore import QBuffer, QIODevice, Qt
    if image.width() > max_width:
        image = image.scaled(max_width, max_width * 8,
                             Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    width, height = image.width(), image.height()
    if height <= tile_height + overlap:
        return [frames_for_vision(data, 1)[0]]
    count = min(max_tiles, max(1, (height + tile_height - 1) // tile_height))
    # 均分后每块高度（含重叠），保证最后一块覆盖到底部。
    step = max(1, (height + (count - 1) * overlap) // count)
    step = min(tile_height, max(step, height // count))
    frames = []
    top = 0
    for index in range(count):
        bottom = height if index == count - 1 else min(height, top + step + overlap)
        tile = image.copy(0, top, width, max(1, bottom - top))
        out = QBuffer(); out.open(QIODevice.OpenModeFlag.WriteOnly)
        if not tile.save(out, 'PNG'):
            break
        frames.append('data:image/png;base64,' + base64.b64encode(bytes(out.data())).decode())
        if bottom >= height:
            break
        top = min(bottom - overlap, height - 1)
    if not frames:
        raise ValueError('长图无法生成识别块')
    return frames


async def public_host(url):
    source=image_source(url)
    if not source or source.startswith('data:'):
        raise ValueError('图片链接不可用')
    parsed=urlsplit(source)
    try:
        results=await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo,parsed.hostname,parsed.port or (443 if parsed.scheme=='https' else 80),0,socket.SOCK_STREAM),3)
    except (OSError,asyncio.TimeoutError):
        raise ValueError('图片地址无法解析') from None
    if not results or any(not ipaddress.ip_address(r[4][0].split('%')[0]).is_global for r in results):
        raise ValueError('图片链接不能指向本机或内网')
    return source

async def fetch_image(source,maximum=MAX_BYTES):
    source=image_source(source)
    if not source:
        raise ValueError('没有可下载的图片链接')
    if source.startswith('data:'):
        data=base64.b64decode(source.split(',',1)[1],validate=True)
        if len(data)>maximum:
            raise ValueError('图片超过大小上限')
        return data
    # QQ 图床对 UA/Referer 敏感：用浏览器 UA + 群聊来源，能明显减少 403/空响应。
    async with httpx.AsyncClient(timeout=httpx.Timeout(12,connect=5),trust_env=False,follow_redirects=False,
                                 headers={'User-Agent':('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                                        'AppleWebKit/537.36 (KHTML, like Gecko) '
                                                        'Chrome/126.0 Safari/537.36'),
                                          'Accept':'image/avif,image/webp,image/*,*/*;q=0.8',
                                          'Referer':'https://qun.qq.com/'}) as client:
        for _ in range(4):
            source=await public_host(source)
            async with client.stream('GET',source) as response:
                if response.status_code in {301,302,303,307,308}:
                    source=urljoin(source,response.headers.get('location',''))
                    continue
                response.raise_for_status()
                if int(response.headers.get('content-length','0'))>maximum:
                    raise ValueError('图片超过大小上限')
                chunks=[];size=0
                async for chunk in response.aiter_bytes():
                    size+=len(chunk)
                    if size>maximum:
                        raise ValueError('图片超过大小上限')
                    chunks.append(chunk)
                return b''.join(chunks)
    raise ValueError('图片链接重定向次数过多')

def source_key(item):
    value=image_source(item.get('url')) or image_source(item.get('file')) or str(item.get('file_uuid') or item.get('file') or '')
    return hashlib.sha256(value.encode()).hexdigest() if value else ''

_TAG_SPLIT = re.compile(r'[，,。.、；;：:！!？?\s/]+')


def derive_tags(text, limit=8):
    """从说明里提取短标签；只是本地索引，不把长描述当标签。"""
    tags = []
    for part in _TAG_SPLIT.split(str(text or '')):
        part = part.strip('「」“”"\'')
        if 2 <= len(part) <= 12 and part not in tags:
            tags.append(part)
        if len(tags) >= limit:
            break
    return tags


class StickerLibrary:
    def __init__(self,root):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.images=self.root/'images';self.images.mkdir(exist_ok=True)
        self.index=self.root/'index.json'
        try:
            raw=json.loads(self.index.read_text('utf-8'))
            self._entries={k:v for k,v in raw.items() if ID.fullmatch(k) and isinstance(v,dict)}
        except (OSError,ValueError,AttributeError):
            # Never overwrite a damaged index silently.
            if self.index.exists():
                raise ValueError('表情库索引损坏，请从备份恢复') from None
            self._entries={}
        self._sources={};self._inflight={};self._recent={};self._meanings={}

    def _save(self,entries):
        temp=self.index.with_suffix('.json.tmp')
        temp.write_text(json.dumps(entries,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        temp.replace(self.index);self._entries=entries

    def path(self,key):
        if not ID.fullmatch(str(key)) or key not in self._entries:
            raise KeyError('表情不存在')
        ext=self._entries[key].get('ext')
        if ext not in {'png','jpg','gif','webp'}:
            raise ValueError('表情文件格式无效')
        path=(self.images/(key+'.'+ext)).resolve()
        if not path.is_relative_to(self.images.resolve()):
            raise ValueError('表情文件路径无效')
        return path

    def entries(self):
        return [copy.deepcopy(v) for k,v in self._entries.items() if self.path(k).is_file()]

    def put(self,data,label='',description='',tags=None,max_items=800,max_mb=96):
        ext,_,buffer=decode_image(data);buffer.close()
        digest=hashlib.sha256(data).hexdigest()
        now=time.time();entries=copy.deepcopy(self._entries)
        if digest in entries:
            entries[digest]['last_seen']=now;entries[digest]['seen']=int(entries[digest].get('seen',1))+1
            self._save(entries);return copy.deepcopy(entries[digest]),False
        label=str(label).strip() or '收集_'+digest[:10]
        self.validate_label(label)
        tags = list(tags) if tags is not None else (derive_tags(description) or derive_tags(label))
        if any(v.get('label')==label for v in entries.values()):
            label=label[:25]+'_'+digest[:10]
        path=(self.images/(digest+'.'+ext)).resolve()
        assert path.is_relative_to(self.images)
        temp=path.with_suffix(path.suffix+'.tmp');temp.write_bytes(data);temp.replace(path)
        entries[digest]={'id':digest,'label':label,'description':str(description)[:1000],
                         'tags':[str(x)[:20] for x in tags[:12]],'ext':ext,
                         'bytes':len(data),'created':now,'last_seen':now,'last_used':0,'uses':0,'seen':1}
        removed=[]
        # Only files owned by this index can be evicted. Manual gallery is separate.
        while len(entries)>max_items or sum(v['bytes'] for v in entries.values())>max_mb*1024*1024:
            oldest=min((k for k in entries if k!=digest),key=lambda k:(entries[k].get('last_used',0),entries[k].get('created',0)),default=None)
            if oldest is None:
                path.unlink(missing_ok=True);raise ValueError('表情库容量不足')
            removed.append(self.path(oldest));entries.pop(oldest)
        try:
            self._save(entries)
        except Exception:
            path.unlink(missing_ok=True);raise
        for file in removed:
            file.unlink(missing_ok=True)
        return copy.deepcopy(entries[digest]),True

    @staticmethod
    def validate_label(label):
        if not isinstance(label,str) or not label.strip() or len(label)>40 or any(c in label for c in '[]|\r\n'):
            raise ValueError('标签需为1～40字，不能包含方括号、竖线或换行')

    def update(self,key,label,description,tags=None):
        self.path(key);self.validate_label(label)
        if any(k!=key and v.get('label')==label.strip() for k,v in self._entries.items()):
            raise ValueError('标签重复')
        if not isinstance(description,str) or len(description)>1000:
            raise ValueError('图片说明最多1000字')
        entries=copy.deepcopy(self._entries);entries[key].update(label=label.strip(),description=description,
                            **({'tags':[str(x)[:20] for x in tags[:12]]} if tags is not None else {}))
        self._save(entries);return copy.deepcopy(entries[key])

    def remove(self,key):
        path=self.path(key)
        entries=copy.deepcopy(self._entries);entries.pop(key)
        self._save(entries);path.unlink(missing_ok=True)
        self._sources={k:v for k,v in self._sources.items() if v!=key}

    def used(self,keys):
        entries=copy.deepcopy(self._entries)
        for key in keys:
            if key in entries:
                entries[key]['last_used']=time.time();entries[key]['uses']+=1
        self._save(entries)

    def description_for(self,item):
        key=self._sources.get(source_key(item))
        return self._entries.get(key,{}).get('description','')

    def search(self, query='', limit=10):
        """轻量语义检索：标签/说明/标签名二元组重合，不调用模型。"""
        text = re.sub(r'\s+', '', str(query or ''))
        if not text:
            return self.entries()[:max(1, int(limit))]
        grams = {text[i:i + 2] for i in range(max(0, len(text) - 1))}
        scored = []
        for key, item in self._entries.items():
            if not self.path(key).is_file():
                continue
            hay = (str(item.get('label') or '') + str(item.get('description') or '')
                   + ' '.join(map(str, item.get('tags') or [])))
            score = sum(1 for gram in grams if gram in hay)
            if text in hay:
                score += 3
            if score:
                scored.append((score, item.get('uses', 0), item))
        scored.sort(key=lambda pair: (pair[0], pair[1]), reverse=True)
        return [copy.deepcopy(pair[2]) for pair in scored[:max(1, int(limit))]]

    def record_description(self,item,description):
        if description:
            self._meanings[source_key(item)]=str(description)[:1000]
            while len(self._meanings)>2000:self._meanings.pop(next(iter(self._meanings)))
        key=self._sources.get(source_key(item))
        if key in self._entries and description:
            self.update(key,self._entries[key]['label'],str(description)[:1000])

    async def resolve(self,item,runtime):
        identity=source_key(item)
        if not identity:
            raise ValueError('没有图片地址')
        # 不同账号的同名文件编号不一定是同一张图片。
        key=(runtime.mode,str(getattr(runtime,'account',lambda:'')()),identity)
        from .received_images import ReceivedImages
        cache_root=(Path(runtime.root)/'data'/'received_images' if getattr(runtime,'root',None)
                    else self.root/'received_images')
        if not hasattr(self,'_received') or self._received.root!=cache_root.resolve():
            self._received=ReceivedImages(cache_root)
        if key in self._recent:
            result=self._recent[key]
            if not Path(result['local_path']).is_file():
                result['local_path']=str(self._received.put(result['data'],result['ext']))
            return result
        if key in self._inflight:
            return await asyncio.shield(self._inflight[key])
        async def load():
            source=image_source(item.get('url')) or image_source(item.get('file'))
            file_id=item.get('file') or item.get('file_uuid')
            if not source and runtime.mode in ('onebot','qqbridge') and file_id:
                peer=runtime.bridge if runtime.mode=='qqbridge' else runtime.onebot
                info=await peer.call('get_image',{'file':str(file_id)})
                if isinstance(info,dict):
                    source=image_source(info.get('url')) or image_source(info.get('file'))
            if not source:
                raise ValueError('没有可下载的图片链接')
            data=await fetch_image(source)
            ext,reader,buffer=decode_image(data)
            result={'data':data,'ext':ext,'width':reader.size().width(),'height':reader.size().height(),'animated':reader.supportsAnimation() and reader.imageCount()>1}
            buffer.close()
            result['local_path']=str(self._received.put(data,ext))
            return result
        task=asyncio.create_task(load());self._inflight[key]=task
        try:
            result=await task
            self._recent[key]=result
            while len(self._recent)>8:
                self._recent.pop(next(iter(self._recent)))
            return result
        finally:
            self._inflight.pop(key,None)

    def label_for_key(self, item):
        """已收藏图片的来源键 → 当前标签；未收藏返回空串。"""
        key = self._sources.get(source_key(item))
        if not key or key not in self._entries:
            return ''
        return str(self._entries[key].get('label') or '')

    def bind(self,item,key):
        self._sources[source_key(item)]=key
        if source_key(item) in self._meanings:
            self.record_description(item,self._meanings[source_key(item)])
        while len(self._sources)>2000:
            self._sources.pop(next(iter(self._sources)))
