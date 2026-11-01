"""Bounded public reads for fixed search/trending services."""
import httpx
from .sticker_library import public_host
from urllib.parse import urljoin,urlsplit

async def read_public(url, *, timeout=12, headers=None, proxy=None, with_url=False):
    url=await public_host(url)
    request_headers={'User-Agent':'Mozilla/5.0','Accept-Language':'zh-CN,zh;q=0.9'}
    parsed=urlsplit(url);host=parsed.hostname or ''
    request_headers['Referer']=('https://www.bilibili.com/' if host.endswith('bilibili.com') else
        'https://tieba.baidu.com/' if host=='tieba.baidu.com' else parsed.scheme+'://'+parsed.netloc+'/')
    request_headers.update(headers or {})
    async with httpx.AsyncClient(timeout=timeout,trust_env=False,follow_redirects=False,proxy=proxy) as client:
        for _ in range(4):
            url=await public_host(url)
            async with client.stream('GET',url,headers=request_headers) as res:
                if res.status_code in {301,302,303,307,308}:
                    redirect=urljoin(url,res.headers.get('location',''))
                    if urlsplit(redirect).hostname!=urlsplit(url).hostname:
                        request_headers.pop('Cookie',None);request_headers.pop('Authorization',None)
                    url=redirect;continue
                res.raise_for_status();parts=[];size=0
                async for chunk in res.aiter_bytes():
                    size+=len(chunk)
                    if size>2*1024*1024:raise ValueError('网页超过读取上限')
                    parts.append(chunk)
                text=b''.join(parts).decode(res.encoding or 'utf-8','replace')
                return (text,str(res.url)) if with_url else text
        raise ValueError('网页重定向次数过多')
