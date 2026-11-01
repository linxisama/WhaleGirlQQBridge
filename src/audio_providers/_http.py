"""音频引擎适配器的共享 HTTP 边界（T05/T06/T07 共用）。

统一三件事，避免每个适配器各写一套（也避免各写错一套）：
- 只连本机/白名单端点、参数数组式请求（不拼 URL 查询、不跟随重定向）；
- 响应体按上限流式读取，超限立即中止（不把 8MB 当无上限）；
- HTTP/连接/超时异常统一映射成 ``audio_contracts.ErrorCode``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

import httpx

from ..audio_contracts import ContractError, ErrorCode, reject

LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1'}


@dataclass
class HttpResult:
    status: int
    content: bytes
    content_type: str = ''
    url: str = ''


def validate_loopback(endpoint: str, *, name: str, path: Optional[str] = None) -> str:
    """本地引擎地址校验：本机、http(s)、无凭据/查询/片段、路径固定。"""
    raw = str(endpoint or '').strip()
    try:
        url = urlsplit(raw)
        port = url.port
    except ValueError:
        reject(ErrorCode.INVALID_INPUT, f'{name} 地址无效', endpoint=raw)
    if (url.scheme not in {'http', 'https'} or url.hostname not in LOOPBACK_HOSTS
            or url.username or url.password or url.query or url.fragment or port == 0):
        reject(ErrorCode.INVALID_INPUT, f'预留接口仅连接本机 {name} 地址', endpoint=raw)
    if path is not None and url.path.rstrip('/') != path:
        reject(ErrorCode.INVALID_INPUT, f'预留接口仅连接本机 {name} 的 {path} 地址',
               endpoint=raw)
    return raw


def validate_outbound(url: str, *, allowed_hosts: set[str], name: str) -> str:
    """云端下载地址校验：https + 白名单域名，拒绝重定向跨域（调用前先看 Location）。"""
    raw = str(url or '').strip()
    parsed = urlsplit(raw)
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or not host:
        reject(ErrorCode.ASSET_INVALID, f'{name} 返回了非 https 地址')
    if host not in allowed_hosts:
        reject(ErrorCode.ASSET_INVALID, f'{name} 返回了未授权域名，拒绝下载', host=host)
    if parsed.username or parsed.password:
        reject(ErrorCode.ASSET_INVALID, f'{name} 地址含凭据，拒绝下载')
    return raw


async def request_bytes(method: str, url: str, *, payload=None, headers=None,
                        transport=None, timeout: float = 60.0,
                        max_bytes: int = 8 * 1024 * 1024,
                        follow_redirects: bool = False,
                        client=None) -> HttpResult:
    """发一次请求并读回响应（有上限）。异常统一为契约错误码。

    ``client`` 可由网关注入并复用（省掉每次 TLS/连接建立）；未注入时临时建一个。
    """
    async def send(http_client):
        async with http_client.stream(method, url, json=payload, headers=headers,
                                      timeout=timeout) as response:
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    reject(ErrorCode.ASSET_INVALID, '响应体超过大小上限，已中止读取',
                           limit=max_bytes)
                chunks.append(chunk)
            return HttpResult(status=response.status_code, content=b''.join(chunks),
                              content_type=response.headers.get('content-type', ''),
                              url=str(response.url))

    try:
        if client is not None:
            return await send(client)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False,
                                     follow_redirects=follow_redirects,
                                     transport=transport) as owned:
            return await send(owned)
    except ContractError:
        raise
    except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
        reject(ErrorCode.PROVIDER_UNAVAILABLE, '无法连接语音服务', error=type(exc).__name__)
    except (httpx.TimeoutException, httpx.RemoteProtocolError, httpx.ReadError) as exc:
        # 请求可能已经送达：调用方（云端）据此决定“结果未知、不重复计费”。
        reject(ErrorCode.DEADLINE_EXCEEDED, '语音服务响应超时或中断',
               error=type(exc).__name__)
    except httpx.HTTPError as exc:
        reject(ErrorCode.PROVIDER_UNAVAILABLE, '语音服务请求失败',
               error=type(exc).__name__)
    except OSError as exc:
        reject(ErrorCode.PROVIDER_UNAVAILABLE, '语音服务不可达', error=str(exc))


def classify_status(status: int, content: bytes, *, name: str) -> None:
    """把 HTTP 错误码映射成可解释的契约错误（保留脱敏的响应片段）。"""
    if 200 <= status < 300:
        return
    detail = content[:300].decode('utf-8', 'replace')
    if 300 <= status < 400:
        reject(ErrorCode.ASSET_INVALID, f'{name} 返回重定向，拒绝跟随', status=status)
    if status in (401, 403):
        reject(ErrorCode.UNAUTHORIZED, f'{name} 拒绝鉴权（{status}）', status=status)
    if status == 404:
        reject(ErrorCode.MODEL_NOT_READY, f'{name} 未找到模型或接口（404）', status=status)
    if status == 409:
        reject(ErrorCode.MODEL_NOT_READY, f'{name} 模型未就绪（409）', status=status)
    if status == 429:
        reject(ErrorCode.BUDGET_EXCEEDED, f'{name} 限流或额度不足（429）', status=status)
    if status >= 500:
        reject(ErrorCode.PROVIDER_UNAVAILABLE, f'{name} 服务端错误（{status}）',
               status=status, detail=detail)
    reject(ErrorCode.PROVIDER_UNAVAILABLE, f'{name} 返回 {status}', status=status,
           detail=detail)
