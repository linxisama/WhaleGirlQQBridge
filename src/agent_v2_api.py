"""本地 Agent 控制台 API（T17 的原生审批页数据源，仅本机 admin 中间件可达）。

安全边界：
- 批准只由本地控制台点击触发；HTTP 请求体不能携带“已批准/签名”之类的字段，
  身份/审批仍由 ActionPolicy 校验；
- 配对入口**不**通过 HTTP 开放（避免网页/模型自行配对设备）；
- 急停在“下一动作前”生效，清空未执行授权。
"""
from __future__ import annotations

import asyncio

from fastapi import HTTPException
from pydantic import BaseModel

from .audio_contracts import ContractError, ErrorCode
from .local_agent.console import AgentConsole


class AgentJobRequest(BaseModel):
    job_id: str


def ensure_agent_console(app) -> AgentConsole:
    existing = getattr(app.state, 'agent_console', None)
    if existing is not None:
        return existing
    bot = app.state.bot
    console = AgentConsole(bot.root, bot.config, bot.db)
    app.state.agent_console = console
    try:
        bot.local_agent_console = console
    except Exception:
        pass
    return console


def install_agent_routes(app):
    def console() -> AgentConsole:
        return ensure_agent_console(app)

    @app.get('/api/v2/agent/status')
    async def status():
        return console().status()

    @app.get('/api/v2/agent/devices')
    async def devices():
        return {'devices': console().devices()}

    @app.get('/api/v2/agent/pending')
    async def pending():
        return {'pending': console().pending()}

    @app.post('/api/v2/agent/approve')
    async def approve(body: AgentJobRequest):
        try:
            return await asyncio.to_thread(console().approve, body.job_id)
        except ContractError as exc:
            raise _http(exc)

    @app.post('/api/v2/agent/reject')
    async def reject(body: AgentJobRequest):
        try:
            return console().reject(body.job_id)
        except ContractError as exc:
            raise _http(exc)

    @app.post('/api/v2/agent/emergency-stop')
    async def emergency_stop():
        return console().emergency_stop()

    @app.post('/api/v2/agent/resume')
    async def resume():
        return console().resume()

    @app.get('/api/v2/agent/audit')
    async def audit(limit: int = 50):
        return {'events': console().audit(limit=limit)}


def _http(exc: ContractError) -> HTTPException:
    status = 400 if exc.code in {
        ErrorCode.INVALID_INPUT, ErrorCode.CAPABILITY_UNSUPPORTED,
        ErrorCode.UNAUTHORIZED, ErrorCode.POLICY_DENIED, ErrorCode.APPROVAL_EXPIRED,
    } else 503
    return HTTPException(status, detail=exc.to_payload())
