"""probability_reply.py - 戳一戳概率回应: probabilistic response to poke events."""
from __future__ import annotations
import random
import asyncio
import hashlib
import json
import time
from typing import Optional
from . import PluginBase, PluginMeta, FieldSchema


class ProbabilityReply(PluginBase):
    META = PluginMeta(
        id="probability_reply",
        name="戳一戳概率回应",
        description=(
            "被戳时按概率决定是否回应、反戳或忽略。"
            "（修正说明：原来只靠文本里有没有「戳了戳」判断拍一拍，而本项目传的是"
            "语义化的 notice='poke'，导致插件**从不触发**；现在两种形式都认，"
            "并且冷却按「谁戳的」计算。）"
        ),
        category="social",
        enabled_by_default=False,
        config_schema={
            "enabled":        FieldSchema(type="bool", label="启用"),
            "replyPercent":   FieldSchema(type="int",  label="回复概率%",   min=0, max=100),
            "pokeBackPercent":FieldSchema(type="int",  label="反戳概率%",   min=0, max=100),
            "ignorePokes":    FieldSchema(type="bool", label="完全忽略戳"),
            "burstEnabled":   FieldSchema(type="bool", label="启用连戳检测"),
            "burstCount":     FieldSchema(type="int",  label="连戳次数", min=2),
            "burstWindowSec": FieldSchema(type="int",  label="连戳窗口(秒)", min=1),
            "cooldownSec":    FieldSchema(type="int",  label="冷却时间(秒)", min=0),
        },
        default_settings={
            "enabled":        False,
            "replyPercent":   60,
            "pokeBackPercent":30,
            "ignorePokes":    False,
            "burstEnabled":   False,
            "burstCount":     5,
            "burstWindowSec": 30,
            "cooldownSec":    600,
        },
    )

    def __init__(self, manager):
        super().__init__(manager)
        # {(group_or_user, sender_id): [timestamp, ...]}
        self._poke_times: dict = {}
        # {(group_or_user, sender_id): last_reply_time}
        self._last_reply: dict = {}

    def _is_poke(self, message: dict) -> bool:
        """这条消息是不是"被戳了"。

        传输层（``qq_transports.onebot_notice``）给的拍一拍是**语义化**的：
        ``{"notice": "poke", "text": "", "notice_data": {"poked", "poker", "group"}}``。
        原来这里只检查文本里有没有"戳了戳"（那是别的实现的做法），
        所以在本项目里**这个插件永远不会触发** —— 现在两种形式都认。
        """
        if not isinstance(message, dict):
            return False
        if str(message.get("notice") or "") == "poke":
            return True
        # 官方模式：平台不推送 poke 事件，qq_app 把"@ 了机器人但没说话"标成 _soft_poke。
        # 这种也算"被戳了"，走同一套概率/冷却（但不会反戳，官方接口没有这个动作）。
        if message.get("_soft_poke"):
            return True
        text = str(message.get("text") or "")
        return (
            "戳了戳" in text
            or text.startswith("[poke]")
            or message.get("poke_type") is not None
        )

    @staticmethod
    def _poker_id(message: dict) -> str:
        """谁戳的。优先用 notice_data.poker，回退 sender_id。

        冷却要按"戳的人"算：否则同一个人换个会话标识就能绕过冷却。
        """
        notice_data = message.get("notice_data")
        if isinstance(notice_data, dict):
            poker = str(notice_data.get("poker") or "").strip()
            if poker:
                return poker
        return str(message.get("sender_id") or "")

    def on_ingest(self, message: dict, runtime) -> Optional[str]:
        if not self.get("enabled"):
            return None
        if runtime is not None and (getattr(runtime,'mode','')!='onebot' or message.get('notice')!='poke'):
            return None
        if not self._is_poke(message):
            return None
        try:
            if self.get("ignorePokes", False):
                return ""  # acknowledge but ignore

            # 拍一拍是按"谁戳的"记冷却；普通消息按发送者
            sender_id = self._poker_id(message)
            target    = str(message.get("target", ""))
            key = (target, sender_id)
            now = time.time()

            # Cooldown
            cooldown = self.get("cooldownSec", 600)
            last_r = self._last_reply.get(key, 0)
            if now - last_r < cooldown:
                return ""

            # Burst detection
            if self.get("burstEnabled", False):
                poke_list = self._poke_times.setdefault(key, [])
                poke_list.append(now)
                window = self.get("burstWindowSec", 30)
                poke_list[:] = [t for t in poke_list if now - t <= window]
                if len(poke_list) >= self.get("burstCount", 5):
                    poke_list.clear()
                    self._last_reply[key] = now
                    return "好了好了别戳了！"

            roll = random.randint(1, 100)
            reply_pct = self.get("replyPercent", 60)
            poke_pct  = self.get("pokeBackPercent", 30)

            # 官方模式没有"反戳"这个接口动作：_soft_poke 一律走 AI 回应那一支，
            # 不要把概率分给反戳（否则用户会收到"当前接入不支持真实反戳。"这种废回复）。
            poke_back_supported = not message.get('_soft_poke')
            if roll <= reply_pct or not poke_back_supported:
                self._last_reply[key] = now
                return None  # let AI handle it
            elif roll <= reply_pct + poke_pct:
                self._last_reply[key] = now
                message['_poke_back']=True
                return '[poke_back]'  # prepare_delivery replaces this internal marker before sending
            else:
                return ""  # ignore
        except Exception:
            return None


    async def prepare_delivery(self,message,reply,runtime):
        if not message.get('_poke_back'):return reply
        if getattr(runtime,'mode','')!='onebot':return '当前接入不支持真实反戳。'
        identity=hashlib.sha256(json.dumps([runtime.account(),message.get('kind'),message.get('target'),message.get('id')]).encode()).hexdigest()
        issued=self.load_state('poke_actions.json',{})
        if not isinstance(issued,dict):issued={}
        if identity in issued:return '这次反戳已尝试，未重复执行。'
        issued[identity]=time.time()
        self.save_state('poke_actions.json',dict(list(issued.items())[-2000:]))
        try:
            params={'user_id':int(self._poker_id(message))}
            action='group_poke' if message.get('kind')=='group' else 'friend_poke'
            if action=='group_poke':params['group_id']=int(message['target'])
            await asyncio.wait_for(runtime.onebot.call(action,params),5)
            return '戳回你一下～'
        except (asyncio.TimeoutError,TimeoutError):
            runtime.note('反戳结果未确认，未自动重试')
            return '反戳结果尚未确认，未重复执行。'
        except Exception:
            runtime.note('反戳未完成，协议端可能不支持此动作')
            return '这次没能戳回去，协议端未完成操作。'

    def on_pre_ai(self,message,conversation,runtime):
        if message.get('notice')=='poke':
            return {'extra_system':'刚才用户对你执行了QQ戳一戳动作，简短自然回应；没有文字或图片，不要编造收到的内容。'}
        if message.get('_soft_poke'):
            return {'extra_system':('用户只 @ 了你一下，没有任何文字或图片（QQ 官方接口下这相当于"戳了你一下"）。'
                                    '用一句话简短回应"被戳"，不要编造对方说了什么，也不要长篇大论。')}
