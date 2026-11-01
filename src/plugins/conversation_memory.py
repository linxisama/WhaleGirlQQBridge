"""Explicit self memory, evidence-based auto memory and same-room people profiles."""
import re
from . import PluginBase, PluginMeta, FieldSchema
from ..chat_state import scope_of, person_of, quoted_context, memory_similarity


AUTO_FACT = re.compile(
    r'我(?:喜欢|不喜欢|讨厌|叫|的昵称是|的(?:生日|名字|昵称|年龄|职业|工作|学校|专业|爱好|兴趣|家乡|城市|住址|电话|微信|QQ|游戏|宠物|家人|朋友)(?:是|叫|在|有|为)?|'
    r'是|在|住|养|玩|学|上班|来自|的爱好是|平时|最近在|正在学|家里有|有个|有一个|今年|去年|明年|养了|住在)'
    r'[^\n。！？!?]{1,70}[。！!~～]?')


def auto_fact(text):
    """从一句话里提取"明确的个人自述"；疑问、假设、玩笑一律不记。"""
    value = str(text or '').strip()
    if len(value) < 4 or len(value) > 120:
        return None
    if any(word in value for word in ('如果', '假如', '要是', '比如说', '我是说', '开玩笑')):
        return None
    if re.search(r'(吗|呢|？|\?)\s*[。！!~～]?$', value):
        return None
    match = AUTO_FACT.fullmatch(value)
    return match.group(0) if match else None


def select_manual(text, items, limit=6, max_chars=800):
    """按与当前消息的相关度挑手动记忆，并限制条数/字数，避免提示词被撑爆。

    6 条 / 800 字：手动记忆是"人设补充"，不是设定集。以前 8 条 / 1800 字，长人设
    一叠加就吃掉小半提示词预算，而排在第 7、8 位的记忆本来也很少被用到。
    """
    values = [str(value) for value in items or [] if str(value).strip()]
    if text:
        values.sort(key=lambda value: memory_similarity(text, value), reverse=True)
    picked = []
    used = 0
    for value in values[:max(0, int(limit))]:
        if picked and used + len(value) > max_chars:
            break
        picked.append(value)
        used += len(value) + 1
    return picked


class ConversationMemory(PluginBase):
    META = PluginMeta(id='conversation_memory', name='长期记忆与群友档案', category='utility',
        description='记住个人事实与偏好，按会话和发送者隔离。/记住 内容、/记忆 关键词、/忘记 关键词、/我的印象、/群友。自动观察需两次独立消息复证，不能凭模型猜测写入。',
        config_schema={
            'enabled':FieldSchema(type='bool', label='启用'),
            'autoRemember':FieldSchema(type='bool', label='自动观察明确的个人偏好'),
            'recallLimit':FieldSchema(type='int', label='每轮最多引用记忆数', min=1, max=10),
            'maxEntries':FieldSchema(type='int', label='长期记忆总容量', min=10, max=5000),
        }, default_settings={'enabled':True, 'autoRemember':True, 'recallLimit':5, 'maxEntries':500})

    def on_ingest(self, message, runtime):
        state = getattr(runtime, 'chat_state', None)
        if state is None or not person_of(message):
            return None
        scope, subject = scope_of(message,runtime), person_of(message)
        text = str(message.get('text') or '').strip()
        try:
            if text in {'/帮助','/功能'}:
                lines = ['/记住 内容：保存自己的事实或偏好','/记忆 [关键词]：查询自己的记忆','/忘记 关键词或全部：删除自己的记忆','/我的印象、/待确认记忆、/群友：查看记录']
                for pid,line in [('memes','/表情列表、/表情制作 名称 文字：制作表情包（可附图片）'),('image_library','/表情 [标签]：查看图库或发送登记的图片'),('emotion_state','/状态：查看鲸鱼娘心情与精力')]:
                    plugin = self._manager._plugins.get(pid)
                    if plugin and plugin.enabled:
                        lines.append(line)
                return '鲸鱼娘聊天指令：\n'+'\n'.join(lines)
            if text.startswith(('/记住 ', '记住：', '记住:')):
                fact = re.sub(r'^(?:/记住\s+|记住[:：])', '', text).strip()
                mid = state.remember(scope,subject,fact,message['id'],True,int(self.get('maxEntries',500)))
                return f'记住了：{fact}（编号 {mid}）'
            if text.startswith('/忘记 '):
                count = state.forget(scope,subject,text[4:].strip())
                return f'已删除你的 {count} 条长期记忆。'
            if text == '/记忆' or text.startswith('/记忆 ') or text == '/我的印象':
                query = text[4:].strip() if text.startswith('/记忆 ') else ''
                found = state.recall(scope,subject,query,10)
                return ('记得这些：\n'+'\n'.join(f"{item['id']}：{item['content']}" for item in found)
                        if found else '当前会话还没有确认过的个人记忆，可以用 /记住 内容 添加。')
            if text == '/待确认记忆':
                found = state.recall(scope,subject,limit=10,candidates=True)
                return '\n'.join(item['content'] for item in found) or '没有待复证的记忆。'
            if text == '/群友':
                return '\n'.join(f"{p['name'] or p['id']}：近24小时聊了 {p['messages_today']} 条" for p in state.people(scope)) or '近24小时还没有群友记录。'
            if self.get('autoRemember', True):
                # 只收"明确的第一人称自述"，而且先记为待复证：同一事实被两条独立消息
                # 说过才升级为长期记忆，避免模型/口嗨被永久记住。
                fact = auto_fact(text)
                if fact:
                    # 私聊或群里 @ 机器人是"当面说的"，直接算确认事实；群聊里没点名的
                    # 自述仍先记待复证，避免把别人的闲聊当成主人的偏好。
                    direct = message.get('kind') == 'private' or bool(message.get('at'))
                    state.remember(scope, subject, fact, message['id'],
                                   confirmed=direct,
                                   maximum=int(self.get('maxEntries', 500)))
        except ValueError as exc:
            return str(exc) if text.startswith(('/', '记住')) else None
        return None

    def on_pre_ai(self, message, conversation, runtime):
        state = getattr(runtime,'chat_state',None)
        if state is None:
            return None
        scope, subject = scope_of(message,runtime),person_of(message)
        notes = state.recall(scope,subject,limit=int(self.get('recallLimit',5))) if subject else []
        name=message.get('_effective_persona') or getattr(getattr(runtime,'style',None),'active_name','')
        manual=runtime.db.execute('SELECT content FROM self_memories WHERE persona=? AND enabled=1 ORDER BY created LIMIT 100',(name,)).fetchall() if name else []
        if manual:
            # 手动记忆最多 100 条，全塞进提示词既费 token 又冲淡重点：
            # 按与当前消息的相关度排序，最多取 6 条 / 800 字；没有消息文本时取最近的。
            picked=select_manual(str(message.get('text') or ''), [row[0] for row in manual])
            if picked:
                conversation.insert(0,{'role':'system','content':'本地控制台设置的自我记忆：\n'+'\n'.join(picked)})
        if notes:
            conversation.insert(max(0,len(conversation)-1),quoted_context('对当前发送者的已确认长期记忆', notes))
        return None
