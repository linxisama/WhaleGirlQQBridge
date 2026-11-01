"""at_mention.py - 用名字 @ 群友（正文 @名字 → 真正的 QQ @ 提醒）。

移植自 JS 插件 ``at-mention``（QQ Agent，MIT）。原版挂在宿主发送前的
``message.outgoing-at-resolve`` 能力上；本项目对应 ``on_post_reply``：解析回复正文，
把认领到的名字换成元数据里的 ``mentions``，再由发送链组装成真正的 ``at`` 消息段
（见 src/plugin_segments.py）。

为什么值得存在：OneBot 里真 @ 必须由独立的 ``at`` 段产生，正文写 ``@某人``
只是普通字符，**对方收不到提醒**。而模型手上通常只有昵称、没有 QQ 号。

名录来源（降级点）：原版的群成员名单由宿主提供（``members``）。本项目没有群成员
列表接口，改为**观察累积** —— 每条入站消息都带发送者的 QQ 号与昵称，攒成
"最近说过话的人"名单（见 src/member_directory.py）。所以：
- 能 @ 到最近在本会话说过话的人；
- 从没说过话、也没被别处记录过的人，认不出来（按原样发送，不会猜）。
"""

from __future__ import annotations

from typing import Optional

from ..member_directory import MemberDirectory, resolve_mentions
from . import PluginBase, PluginMeta, FieldSchema


class AtMention(PluginBase):
    META = PluginMeta(
        id="at_mention",
        name="@群友（按名字）",
        description=(
            "让机器人用名字 @ 群友：它在正文里写「@泡影之梦 你说得对」时，这里把名字认领成"
            "真正的 QQ @ 提醒（对方会收到红点），而不是一串没人看得到的普通字符。支持一次 @ 多人。"
            "认不出名字就按原样发送，绝不猜——猜错会 @ 到无关的人。"
            "（降级说明：群成员名单靠观察最近说过话的人累积，没说过话的人认不出来。）"
        ),
        category="message",
        enabled_by_default=False,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用按名字 @"),
            "maxMentions": FieldSchema(
                type="int", label="单条消息最多 @ 几人", min=1, max=10,
                description="默认 5，防止模型刷屏式 @ 一长串人。",
            ),
            "allowPartialMatch": FieldSchema(
                type="bool", label="允许前缀匹配",
                description="开启时「@泡影」能匹配到群名片为「泡影之梦」的人；关掉只认完全同名。",
            ),
            "debug": FieldSchema(type="bool", label="记录解析日志"),
        },
        default_settings={
            "enabled": False,
            "maxMentions": 5,
            "allowPartialMatch": True,
            "debug": False,
        },
    )

    def __init__(self, manager):
        super().__init__(manager)
        # 名录放内存即可：它只是"最近说过话的人"，重启后靠新消息重新攒起来。
        self._directory = MemberDirectory()

    # 观察每条入站消息，累积"谁是谁"——离开这个，@ 解析就没有名单可查。
    def on_ingest(self, message: dict, runtime) -> Optional[str]:
        self._directory.observe(message)
        return None

    def on_block(self, message: dict, runtime) -> bool:
        self._directory.observe(message)
        return False

    def on_pre_ai(self, message: dict, conversation: list, runtime) -> Optional[dict]:
        self._directory.observe(message)
        return None

    async def on_post_reply(self, message: dict, reply: str, runtime):
        if not self.get("enabled"):
            return reply
        try:
            # 私聊没有群成员，真 @ 也无意义
            if (message or {}).get("kind") != "group":
                return reply
            if getattr(runtime, 'mode', '') == 'official':
                return reply  # 官方群接入仅支持文字提及，不能删掉名字再丢弃元数据。
            members = self._directory.members(message)
            if not members:
                return reply
            result = resolve_mentions(
                reply,
                members,
                max_mentions=self.get("maxMentions", 5),
                allow_partial_match=bool(self.get("allowPartialMatch", True)),
            )
            if not result["hits"]:
                return reply
            if self.get("debug"):
                who = "、".join(f"{hit['name']}({hit['userId']})" for hit in result["hits"])
                print(f"[plugins] at_mention: 已把正文里的 @ 转成真 @ 提醒：{who}")
            # 元数据交给 manager 收集、发送链组装 at 段；
            # 正文里的 @名字 已去掉，避免和真 @ 重复显示。
            return result["text"], {"mentions": result["atUserIds"]}
        except Exception:
            # 解析失败一律当作"没解析出来"，绝不挡住一条消息的发送
            return reply
