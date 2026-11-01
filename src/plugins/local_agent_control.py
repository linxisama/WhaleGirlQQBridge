"""T17：QQ 电脑指令插件（主人私聊路由 + 动作预览 + 本地审批 + 设备状态 + 急停）。

路由（1.7.2）：
  可信 QQ 私聊事件 → 主人身份 + 账号绑定 → 明确的“电脑”命令 → 结构化动作提案
  → 参数 schema/权限/资源白名单 → **本地控制台批准** → Agent 执行 → 校验结果
  → 脱敏摘要回原私聊。

边界（T17 验收）：
- 群聊/引用/转发**不**触发动作（待解释数据，非授权证明）。
- 审批绑定参数哈希，改参必须重新审批。
- 结果只回原绑定私聊；取消/解绑立即使后续动作失效。
- 截图/文件回传不因“允许操作”自动放开（需独立允许范围）。

模型只能“建议动作”，不能填 approved:true、不能伪造签名（contracts/policy 守门）。
本插件是“入口 + 路由”；真正的执行与闸在 local_agent/runtime/executor/policy。

实现补充（2026-10-20）：命令解析是纯函数（可单测），不做自然语言猜测；无法识别
的“电脑 …”指令明确回复不支持，而不是交给模型执行。
"""
from __future__ import annotations

import re
import secrets
import time

from . import PluginBase, PluginMeta, FieldSchema
from ..audio_contracts import ContractError
from ..local_agent.contracts import ActionProposal

_PC_WORDS = {'电脑', '操作电脑', '打开程序', '调音量', '暂停媒体', '电脑状态', '电脑能力'}
_OPEN = re.compile(r'^电脑\s*打开\s*([A-Za-z0-9_.-]{1,32})$')
_VOLUME = re.compile(r'^电脑\s*音量\s*(\d{1,3})$')
_QUOTE_KEYS = ('quote', 'quoted', 'reply', 'reference', 'message_reference', 'forward')


def parse_pc_command(text: str) -> dict | None:
    """把明确的中文命令解析成动作包；不做模糊猜测。"""
    raw = re.sub(r'\s+', ' ', str(text or '').strip())
    if not raw:
        return None
    if raw in {'电脑状态', '电脑 状态', '电脑 在线'}:
        return {'action': 'agent.health', 'args': {}}
    if raw in {'电脑能力', '电脑 能力', '电脑 能做什么'}:
        return {'action': 'agent.capabilities', 'args': {}}
    if raw in {'电脑 暂停', '电脑暂停', '电脑 暂停媒体', '暂停媒体'}:
        return {'action': 'media.pause', 'args': {}}
    match = _OPEN.match(raw)
    if match:
        return {'action': 'app.open', 'args': {'app_id': match.group(1).lower()}}
    match = _VOLUME.match(raw)
    if match:
        level = int(match.group(1))
        if 0 <= level <= 100:
            return {'action': 'volume.set', 'args': {'level': level}}
        return None
    return None


def looks_like_pc_command(text: str) -> bool:
    raw = str(text or '').strip()
    return raw in _PC_WORDS or raw.startswith('电脑 ') or raw.startswith('电脑')


def _is_quoted(message: dict) -> bool:
    return any(message.get(key) for key in _QUOTE_KEYS)


class LocalAgentControl(PluginBase):
    META = PluginMeta(
        id='local_agent_control', name='本地电脑操作', category='agent',
        enabled_by_default=False,
        description='主人私聊“电脑 状态/能力/打开X/音量N/暂停媒体”生成动作提案，'
                    '在本地控制台批准后由 Windows Agent 执行（首版 L0/L1）。'
                    '群聊/引用不触发；审批绑定参数；结果只回原私聊。',
        config_schema={
            'enabled': FieldSchema(type='bool', label='启用'),
            'ownerIds': FieldSchema(type='string', label='已绑定主人（只读）',
                                    description='只接受配对主人私聊，换任一项重新验证。'),
            'devices': FieldSchema(type='string', label='已配对设备（只读）'),
            'emergencyStop': FieldSchema(type='bool', label='全局急停',
                                         description='一键清空未执行授权，下一动作前生效。'),
        },
        default_settings={'enabled': False, 'ownerIds': '', 'devices': '',
                          'emergencyStop': False})

    def _owners(self) -> set[str]:
        return {part for part in re.split(r'[,\s]+', str(self.get('ownerIds', ''))) if part}

    def _is_owner(self, message) -> bool:
        return (message.get('kind') == 'private'
                and str(message.get('sender_id', '')) in self._owners())

    def on_ingest(self, message, runtime):
        """只吃“主人私聊 + 明确电脑命令”，否则绝不碰（群/引用/转发直接放过）。"""
        if not self.enabled:
            return None
        text = str(message.get('text') or '').strip()
        if not looks_like_pc_command(text):
            return None
        if message.get('kind') != 'private' or _is_quoted(message):
            message['_robot_admin'] = True
            return '电脑操作只接受已配对主人的原始私聊指令；群聊、引用、转发不触发动作。'
        if not self._is_owner(message):
            message['_robot_admin'] = True
            return '电脑操作仅限已配对的机器人主人私聊；群消息/引用/转发不触发动作。'
        command = parse_pc_command(text)
        if command is None:
            message['_robot_admin'] = True
            return ('不支持的电脑指令。可用：电脑 状态 / 电脑 能力 / 电脑 打开 <应用名> / '
                    '电脑 音量 0-100 / 电脑 暂停。')
        console = getattr(runtime, 'local_agent_console', None)
        if console is None:
            message['_robot_admin'] = True
            return ('电脑操作尚未配对设备。请在本机控制台完成设备配对与策略确认；'
                    '聊天消息不会自动获得宿主权限。')
        devices = [item for item in console.devices() if item.get('paired')]
        if not devices:
            message['_robot_admin'] = True
            return '没有已配对的本地设备；请先在本机控制台完成配对。'
        device_id = str(devices[0]['device_id'])
        proposal = ActionProposal(
            job_id='agent-' + secrets.token_hex(8),
            device_id=device_id,
            action=command['action'],
            args=command['args'],
            expires_at=time.time() + 120,
            policy_version=int(devices[0].get('policy_version') or 1),
            nonce=secrets.token_hex(16))
        try:
            account = str(message.get('account_id') or message.get('account') or '')
            if not account:
                getter = getattr(runtime, 'account', None)
                if callable(getter):
                    try:
                        account = str(getter() or '')
                    except Exception:
                        account = ''
            console.broker.submit_private(
                proposal,
                caller=str(message.get('sender_id') or ''),
                qq_account=account,
                message_id=str(message.get('id') or message.get('message_id') or ''))
        except ContractError as exc:
            message['_robot_admin'] = True
            return f'电脑动作被拒绝（{exc.code.value}）。'
        message['_robot_admin'] = True
        return (f'已提交电脑动作：{command["action"]}，等待本机控制台批准。'
                '批准后才会执行；结果只回这条私聊。')

    @staticmethod
    def deliver_result(message: dict, text: str) -> str:
        """结果只回原绑定私聊；群聊/引用一律不投递（T17 验收）。"""
        if message.get('kind') != 'private' or _is_quoted(message):
            return ''
        return str(text or '')
