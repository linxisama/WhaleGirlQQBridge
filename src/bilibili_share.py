"""公开分享信息和可用的低清 MP4；失败不推测视频画面。"""
import asyncio
import json
import re
import time
from urllib.parse import urlencode,urlsplit
from .public_web import read_public
try:
    from .plugins.share_card import extract_urls, card_text
except ImportError:
    # share_card 是可选插件；核心 B 站分享不能因为它没随包分发就整体 import 失败。
    def extract_urls(text):
        return re.findall(r'https?://[^\s<>()]+', str(text or ''))[:8]
    def card_text(item):
        return ''



def bili_source(message):
    text=str(message.get('text') or '')
    for item in message.get('media') or []:
        if isinstance(item,dict) and item.get('type') in ('json','xml'):text+=' '+card_text(item)
    for url in extract_urls(text):
        host=(urlsplit(url).hostname or '').lower()
        if host=='b23.tv' or host=='bilibili.com' or host.endswith('.bilibili.com'):return url
    match=re.search(r'(?<![A-Za-z0-9])(BV[A-Za-z0-9]{10}|av\d{1,16})(?![A-Za-z0-9])',text)
    return 'https://www.bilibili.com/video/'+match[1] if match else ''


def approved_video(url):
    if not isinstance(url,str) or len(url)>6000:return False
    p=urlsplit(url)
    host=(p.hostname or '').lower()
    return p.scheme=='https' and not p.username and not p.password and (host=='bilivideo.com' or host.endswith('.bilivideo.com'))


async def read_video(source,want_video=False):
    async def read():
        url=source
        if urlsplit(url).hostname=='b23.tv':
            _,url=await read_public(url,timeout=4,with_url=True)
        host=(urlsplit(url).hostname or '').lower()
        if host!='bilibili.com' and not host.endswith('.bilibili.com'):raise ValueError('短链接未跳转到B站')
        match=re.search(r'/video/(BV[A-Za-z0-9]{10}|av\d{1,16})',url)
        if not match:raise ValueError('未取得视频编号，请发送 BV 号或视频页链接')
        value=match[1];params={'bvid':value} if value.startswith('BV') else {'aid':value[2:]}
        raw=json.loads(await read_public('https://api.bilibili.com/x/web-interface/view?'+urlencode(params),timeout=4))
        data=raw.get('data')
        if raw.get('code')!=0 or not isinstance(data,dict):raise ValueError('视频不存在、受限或公开接口暂不可用')
        bvid=str(data.get('bvid') or value)
        result={'title':str(data.get('title') or '')[:200], 'description':str(data.get('desc') or '')[:900],
            'uploader':str((data.get('owner') or {}).get('name') or '')[:100],
            'url':'https://www.bilibili.com/video/'+bvid,'published':data.get('pubdate'),'queried_at':time.time()}
        if not want_video:return result
        cid=data.get('cid');duration=data.get('duration',0)
        if not cid or not isinstance(duration,(float,int)) or duration>600:
            result['video_error']='此视频较长或没有可用分P信息，改发原链接。';return result
        stream=json.loads(await read_public('https://api.bilibili.com/x/player/playurl?'+urlencode(
            dict(bvid=bvid,cid=cid,qn=16,fnval=1,platform='html5',high_quality=0)),timeout=4))
        streams=(stream.get('data') or {}).get('durl') or []
        if stream.get('code')!=0 or len(streams)!=1:
            result['video_error']='暂未取得可直接发送的单段 MP4，改发原链接。';return result
        item=streams[0];video=item.get('url','')
        if video.startswith('http://'):video='https://'+video[7:]
        size=item.get('size')
        if not approved_video(video) or not isinstance(size,(int,float)) or not 0<size<=25*1024*1024:
            result['video_error']='视频超过25MB或没有可用的公开地址，改发原链接。';return result
        result['video']={'url':video,'size':size}
        return result
    return await asyncio.wait_for(read(),10 if want_video else 6)
