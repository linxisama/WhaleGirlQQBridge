"""Legacy ID; only manages the robot itself.

两条通道，安全口径不同：

1. **旧通道**：绑定管理员私聊“机器人 状态/暂停…”；读操作直接回答，写操作进本地审核队列
   （控制台点“审核并执行”）。
2. **管理员模式**：先私聊“进入管理员模式”，把密码作为**下一条**消息发过来；通过后
   在会话有效期内可以直接执行管理操作（暂停、换人设、开关插件、看回复记录…），
   每一步都写进控制台可见的审计记录。

密码那条消息会被立刻抹掉正文再返回，**绝不进数据库、不进日志、不喂给模型**。
"""
import asyncio
import re
import time

from . import PluginBase, PluginMeta, FieldSchema
from ..admin_session import ADMIN_HELP, entry_request, exit_request
from ..robot_admin import READ_ACTIONS, WRITE_ACTIONS, admin_ids, clean_log, read_status, set_paused


def parse_command(text, aliases=None):
    raw = re.sub(r'^[\\/]', '', str(text).strip())
    match = re.match(r'^(?:机器人|控制台|电脑)\s*(.*)$', raw)
    if not match: return None
    actions = {'状态': 'status', '运行状态': 'status', '连接': 'connections', '连接状态': 'connections', '队列': 'queue', '消息队列': 'queue', '日志': 'logs', '运行日志': 'logs', '人设': 'personas', '插件': 'plugins', '暂停': 'pause', '暂停回复': 'pause', '恢复': 'resume', '恢复回复': 'resume', '重连': 'reconnect', '重连QQ': 'reconnect', '重新连接QQ': 'reconnect'}
    command = match[1].strip()
    if command not in actions and not re.match(r'^(?:机器人|控制台|电脑)\s+', raw): return None
    return (actions.get(command, 'help'), '')


# 管理员模式下的简写命令：不带“机器人”前缀也认（只在会话内生效）
SESSION_WORDS = {
    '状态': 'status', '运行状态': 'status', '连接': 'connections', '连接状态': 'connections',
    '队列': 'queue', '消息队列': 'queue', '日志': 'logs', '运行日志': 'logs',
    '用量': 'usage', '用量统计': 'usage', '重试失败': 'retry_failed', '重试': 'retry_failed',
    '暂停': 'pause', '暂停回复': 'pause', '恢复': 'resume', '恢复回复': 'resume',
    '重连': 'reconnect', '重连QQ': 'reconnect', '重新连接QQ': 'reconnect',
}
# 聊天侧只允许开关"纯聊天"的内置插件。自定义插件（用户从外部导入的代码）
# 与系统管理类插件不开放远程开关：否则拿到管理员密码的人可以远程启用
# 一个带宿主系统调用的插件，等于绕过"聊天不能控制电脑"的边界。
SAFE_CHAT_PLUGINS = frozenset({
    'image_understanding', 'speech_to_text_qq', 'memes', 'quote_reply',
    'conversation_memory', 'keyword_trigger', 'keyword_reply', 'reply_variety',
    'send_pacing', 'burst_merge', 'sticker_memory', 'emotion_state',
    'token_saver', 'ambient_reply', 'sticker_cadence', 'face_decode',
})

SESSION_HEADS = {'回复记录': 'replies', '回复': 'replies', '聊天记录': 'replies',
                 '人设': 'persona', '人格': 'persona', '角色': 'persona',
                 '插件': 'plugin', '市场': 'market', '速度': 'speed'}

PERSONA_ALIASES = re.compile(
    r'^(?:(?:切换|更换|换一个|换个|换成|换|设置|修改|改成|改为)\s*)?'
    r'(?:人设|人格|角色)\s*(?:[:：]\s*)?(.*)$')

PERSONA_TARGET_ALIASES = re.compile(
    r'^(?:切换|更换|换|设置|修改|改成|改为)(?:成|为|到)\s*(.+)$')


def _normalize_admin_text(text):
    raw = str(text or '').strip().replace('\u3000', ' ')
    raw = re.sub(r'^[\\/]+', '', raw).strip()
    return re.sub(r'\s+', ' ', raw)


def session_command(text):
    """会话内命令解析；返回 (action, arg)，不是命令就返回 None。"""
    raw = _normalize_admin_text(text)
    if not raw:
        return None
    # 允许“人设：名字”“插件：关 x”“状态：”这类带中文冒号的写法。
    raw = re.sub(r'^([^\s:：]{1,12})\s*[:：]\s*', r'\1 ', raw, count=1).strip()
    head, _, rest = raw.partition(' ')
    head, rest = head.strip(), rest.strip()
    if head in {'帮助', '管理', '管理帮助', 'help', '?', '？'}:
        return 'help', ''
    if head in SESSION_WORDS:
        return SESSION_WORDS[head], ''
    if head in SESSION_HEADS:
        return SESSION_HEADS[head], rest
    # “切换成默认人格”“换成傲娇鲸鱼娘人设”：目标写在“人设/人格/角色”前面。
    target = PERSONA_TARGET_ALIASES.match(raw)
    if target:
        return 'persona', target.group(1).strip(' :：')
    alias = PERSONA_ALIASES.match(raw)
    if alias:
        return 'persona', alias.group(1).strip(' :：')
    return None

class PcControl(PluginBase):
    META = PluginMeta(id='pc_control', name='机器人控制台管理', category='utility', enabled_by_default=False,
        description=('管理员私聊发送“机器人 状态/连接/队列/日志/人设/插件”直接查询；暂停、恢复回复和重连 QQ 走本地审核。'
                     '也可以私聊“进入管理员模式”，输入在控制台设置的管理员密码后，在会话有效期内直接执行管理操作'
                     '（状态、回复记录、换人设、开关插件、暂停/恢复、重连、重试失败），每一步都记录在控制台。'
                     '只管理机器人本身，不操作宿主机器。'),
        config_schema={'enabled': FieldSchema(type='bool', label='启用私聊管理员指令'),
            'ownerIds': FieldSchema(type='string', label='已绑定管理员（只读）', editor='pc_owner',
                                    description='在“机器人管理”页从最近私聊/群聊用户中选择绑定或解除。'),
            'maxPerHour': FieldSchema(type='int', label='每小时管理操作上限', min=1, max=100)},
        default_settings={'enabled': False, 'ownerIds': '', 'maxPerHour': 20})

    def is_admin(self, message):
        owners = set(admin_ids(self.get('ownerIds', '')))
        return message.get('kind') == 'private' and str(message.get('sender_id', '')) in owners

    def wants(self, message, runtime) -> bool:
        """这条私聊是否该走管理员通道（必须抢在关键词/复读等插件之前）。"""
        if not self.is_admin(message):
            return False
        text = str(message.get('text') or '').strip()
        sessions = getattr(runtime, 'admin', None)
        who = str(message.get('sender_id') or '')
        if sessions is not None and sessions.awaiting(who):
            return True          # 密码那一条必须被吃掉：不能进模型，也不能被关键词插件回掉
        if parse_command(text):
            return True
        if entry_request(text) or exit_request(text):
            return True
        return bool(sessions is not None and sessions.active(who) and session_command(text))

    def on_ingest(self, message, runtime):
        text = str(message.get('text') or '').strip()
        sessions = getattr(runtime, 'admin', None)
        who = str(message.get('sender_id') or '')
        if sessions is not None and self.is_admin(message):
            if sessions.awaiting(who):
                return self._password_attempt(message, runtime, sessions, who, text)
            if entry_request(text):
                message['_robot_admin'] = True
                return self._enter(runtime, sessions, who)
            if exit_request(text):
                sessions.end(who)
                message['_robot_admin'] = True
                return '已退出管理员模式；之后的远程变更需要本地审核。'
            if sessions.active(who):
                command = session_command(text) or parse_command(text)
                if command:
                    message['_robot_admin'] = True
                    return self._run(message, runtime, command)
        # ── 旧通道 ────────────────────────────────────────────────────────────
        command = parse_command(text)
        if not command:
            return None
        if not self.is_admin(message):
            return '机器人管理仅接受本地绑定管理员的私聊指令。'
        message['_robot_admin'] = True
        if command[0] == 'help':
            return ('机器人管理（旧通道）支持：状态、连接、队列、日志、人设、插件、暂停回复、恢复回复、重连QQ；'
                    '远程变更需要本地审核。设了管理员密码后，可以发“进入管理员模式”直接管理。'
                    '不能截图宿主桌面、调音量、运行程序或清空回收站。')
        if command[0] in READ_ACTIONS:
            return read_status(runtime, command[0])
        if command[0] in WRITE_ACTIONS:
            return runtime.pc.request(message, *command)

    def on_sent(self, message, reply, runtime):
        if message.get('_pc_job') and getattr(runtime, 'pc', None):
            runtime.pc.receipt_sent(message['_pc_job'])

    # ── 管理员模式 ────────────────────────────────────────────────────────────
    def _enter(self, runtime, sessions, who):
        if not str(runtime.config.get('qq.admin_password_hash', '') or ''):
            return '还没有设置管理员密码：请在控制台「机器人管理」页设置后再试（旧通道仍可用）。'
        left = sessions.locked_for(who)
        if left:
            return f'密码已锁定，请约 {max(1, left // 60 + 1)} 分钟后再试。'
        sessions.begin(who)
        return f'请输入管理员密码（下一条消息，{sessions.minutes} 分钟内有效；发“取消”可放弃）。'

    def _password_attempt(self, message, runtime, sessions, who, text):
        message['_robot_admin'] = True
        # 先把正文抹掉再校验：这条消息会被写进 inbox 的 payload，明文密码绝不能落库。
        message['text'] = '【管理员密码已隐藏】'
        message['media'] = []
        if text in {'取消', 'cancel', '放弃'}:
            sessions.cancel(who)
            return '已取消进入管理员模式。'
        if entry_request(text):
            # 又发了一次“进入管理员模式”：重新提示，不要白白算一次失败
            return f'请输入管理员密码（下一条消息，{sessions.minutes} 分钟内有效；发“取消”可放弃）。'
        left = sessions.locked_for(who)
        if left:
            return f'密码已锁定，请约 {max(1, left // 60 + 1)} 分钟后再试。'
        outcome = sessions.attempt(who, text,
                                   runtime.config.get('qq.admin_password_hash', ''),
                                   runtime.config.get('qq.admin_password_salt', ''))
        if outcome == 'ok':
            runtime.note('管理员已通过密码进入管理员模式')
            return f'已进入管理员模式（{sessions.minutes} 分钟）。\n' + ADMIN_HELP
        if outcome == 'locked':
            runtime.note('管理员密码连续错误，已临时锁定')
            return '密码错误次数过多，已锁定 10 分钟。'
        return f'密码不正确，还可以尝试 {sessions.attempts_left(who)} 次。'

    def _run(self, message, runtime, command):
        action, arg = command
        try:
            result = self._admin_action(runtime, action, arg)
        except ValueError as exc:
            result = f'操作未执行：{exc}'
        except Exception as exc:
            result = f'操作未完成：{clean_log(str(exc))[:160]}'
        try:
            runtime.pc.record_admin(action, arg, result)
        except Exception:
            runtime.note('管理员操作已执行，但审计记录写入失败')
        return result

    def _admin_action(self, runtime, action, arg):
        if action == 'help':
            return ADMIN_HELP
        if action in READ_ACTIONS:
            return read_status(runtime, action)
        if action in ('pause', 'resume'):
            return set_paused(runtime, action == 'pause')
        if action == 'reconnect':
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return runtime.pc.request({'id': 'admin-reconnect', 'kind': 'private',
                                           'target': 'admin', 'sender_id': ''}, 'reconnect', '')
            loop.create_task(_reconnect(runtime))
            return '已开始重连 QQ，稍后用“连接”查看结果。'
        if action == 'usage':
            summary = getattr(getattr(runtime.ai, 'usage', None), 'summary', {}) or {}
            rows = '\n'.join(f'{k}：{v}' for k, v in summary.items()) or '暂无用量记录'
            return f'模型：{runtime.ai.model}\n{rows}'
        if action == 'replies':
            return self._recent_replies(runtime, arg)
        if action == 'persona':
            return self._persona(runtime, arg)
        if action == 'plugin':
            return self._plugins(runtime, arg)
        if action == 'market':
            probe = runtime.plugins._plugins.get('market_probe')
            if probe is None:
                return '没有加载“市场探针”插件，无法查看插件市场。'
            return probe.probe_now(runtime, arg)
        if action == 'retry_failed':
            changed = runtime.db.execute(
                "UPDATE inbox SET state='pending' WHERE state='failed' AND mode=? AND account=?",
                (runtime.mode, runtime.account())).rowcount
            runtime.db.commit()
            return f'已把 {changed} 条失败消息重新排队。' if changed else '没有失败的消息需要重试。'
        raise ValueError(f'不支持的管理操作：{action}')

    @staticmethod
    def _recent_replies(runtime, arg):
        try:
            limit = max(1, min(20, int(str(arg).strip())))
        except (TypeError, ValueError):
            limit = 5
        rows = runtime.db.execute(
            "SELECT room, COALESCE(json_extract(payload,'$.sender_name'),''), reply, created FROM inbox"
            " WHERE mode=? AND account=? AND reply<>'' ORDER BY created DESC LIMIT ?",
            (runtime.mode, runtime.account(), limit)).fetchall()
        if not rows:
            return '近期没有回复记录。'
        lines = [f"{time.strftime('%m-%d %H:%M', time.localtime(t))} {room} {sender}：{clean_log(reply)[:80]}"
                 for room, sender, reply, t in rows]
        return f'最近 {len(rows)} 条回复（新→旧）：\n' + '\n'.join(lines)

    @staticmethod
    def _persona(runtime, arg):
        name = re.sub(r'^[\\/]+', '', str(arg or '').strip()).strip()
        name = re.sub(r'\s*(?:的)?\s*(?:人设|人格|角色)\s*$', '', name).strip()
        if not name:
            styles = runtime.style.list_styles()
            return ('当前人设：' + runtime.style.active_name + '\n可用人设：'
                    + ('、'.join(v['name'] for v in styles) or '默认人设')
                    + '\n切换：人设 <标识>')
        if name in {'默认', '默认人设', '默认人格', '默认角色', '默认风格', '内置', '内置人设'}:
            name = 'default'
        styles = runtime.style.list_styles()
        if name not in {str(v.get('name')) for v in styles}:
            for style in styles:
                if str(style.get('display_name') or '') == name:
                    name = str(style.get('name') or name)
                    break
        ok, message = runtime.set_persona(name)
        return message

    @staticmethod
    def _plugins(runtime, arg):
        raw = str(arg or '').strip()
        if not raw:
            plugins = runtime.plugins.list_plugins()
            enabled = sum(1 for p in plugins if p.get('enabled'))
            lines = [('✓ ' if p.get('enabled') else '○ ') + f"{p.get('name')}（{p.get('id')}）" for p in plugins]
            return f'插件 {enabled}/{len(plugins)} 已启用：\n' + '\n'.join(lines)[:1500]
        match = re.match(r'^(开|关|开启|关闭|启用|停用)\s*([A-Za-z0-9_\-]+)$', raw)
        if not match:
            return '用法：插件 开 <插件标识> / 插件 关 <插件标识>；只发“插件”看列表。'
        want = match[1] in {'开', '开启', '启用'}
        pid = match[2]
        plugin = runtime.plugins._plugins.get(pid)
        if plugin is None:
            return f'没有这个插件：{pid}'
        if pid not in SAFE_CHAT_PLUGINS:
            return (f'{plugin.META.name} 不开放聊天侧开关：自定义插件或系统管理插件'
                    '只能在控制台里手动操作。')
        runtime.plugins.update_settings(pid, {'enabled': want})
        runtime.note(f"管理员{'开启' if want else '关闭'}了插件 {plugin.META.name}")
        return f"{plugin.META.name} 已{'开启' if want else '关闭'}。"


async def _reconnect(runtime):
    """后台重连：管理员模式下直接执行（密码认证过），不经过本地审核。"""
    try:
        from ..robot_admin import ConsoleBackend
        await ConsoleBackend(runtime).execute('reconnect')
    except Exception as exc:
        runtime.note(f'管理员请求重连失败：{clean_log(str(exc))[:120]}')
