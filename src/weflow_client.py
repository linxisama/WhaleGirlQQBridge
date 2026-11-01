"""WeFlow 只读客户端（微信聊天记录数据源）。

WeFlow 是本地聊天记录查看/分析工具，HTTP API 默认 `http://127.0.0.1:5031`，
需要 Bearer Token。**它没有发送接口**，所以这里是**只读**的：
健康检查、会话列表、消息、联系人、群成员。

用途：把微信聊天记录导入语料/记忆/年度报告；不参与"机器人登录"。
"""
from __future__ import annotations

__all__ = ['WeFlowClient', 'WeFlowError']

import httpx


class WeFlowError(RuntimeError):
    """WeFlow 返回的错误（带可读原因，不吞状态码）。"""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class WeFlowClient:
    def __init__(self, base_url: str = 'http://127.0.0.1:5031', token: str = '',
                 timeout: float = 8.0):
        self.base_url = str(base_url or '').rstrip('/')
        self.token = str(token or '')
        self.timeout = float(timeout)

    def _headers(self) -> dict:
        return {'Authorization': 'Bearer ' + self.token} if self.token else {}

    def _get(self, path: str, params: dict | None = None) -> dict:
        if not self.base_url:
            raise WeFlowError('没有配置 WeFlow 地址（默认 http://127.0.0.1:5031）')
        url = self.base_url + path
        try:
            response = httpx.get(url, params=params or {}, headers=self._headers(),
                                 timeout=self.timeout, trust_env=False)
        except Exception as error:
            raise WeFlowError(f'连不上 WeFlow：{type(error).__name__}: {error}') from None
        if response.status_code == 401:
            raise WeFlowError('WeFlow Token 不对（在 WeFlow 设置里复制 API Token）', 401)
        if response.status_code >= 400:
            raise WeFlowError(f'WeFlow 返回 {response.status_code}：{response.text[:120]}',
                              response.status_code)
        try:
            data = response.json()
        except Exception:
            raise WeFlowError('WeFlow 返回的不是 JSON（端口填错了？）') from None
        return data if isinstance(data, dict) else {'data': data}

    # ── 只读接口 ────────────────────────────────────────────────────
    def health(self) -> dict:
        return self._get('/api/v1/health')

    def sessions(self, keyword: str = '', limit: int = 50) -> list:
        data = self._get('/api/v1/sessions', {'keyword': keyword, 'limit': int(limit)})
        items = data.get('sessions')
        return items if isinstance(items, list) else []

    def messages(self, talker: str, limit: int = 50, offset: int = 0,
                 start: str = '', end: str = '', keyword: str = '') -> dict:
        params = {'talker': talker, 'limit': int(limit), 'offset': int(offset)}
        if start:
            params['start'] = start
        if end:
            params['end'] = end
        if keyword:
            params['keyword'] = keyword
        return self._get('/api/v1/messages', params)

    def contacts(self, keyword: str = '', limit: int = 50) -> list:
        data = self._get('/api/v1/contacts', {'keyword': keyword, 'limit': int(limit)})
        items = data.get('contacts')
        return items if isinstance(items, list) else []

    def group_members(self, chatroom_id: str) -> list:
        data = self._get('/api/v1/group-members', {'chatroomId': chatroom_id})
        items = data.get('members')
        return items if isinstance(items, list) else []

    def status(self) -> dict:
        """给界面用的一句话状态；连不上也不抛。"""
        try:
            health = self.health()
        except WeFlowError as error:
            return {'ok': False, 'message': str(error)}
        return {'ok': str(health.get('status') or '') == 'ok',
                'message': '已连接', 'status': health.get('status')}
