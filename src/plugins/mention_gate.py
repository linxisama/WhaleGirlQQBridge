"""mention_gate.py - 点名闸门：只在被点名时开口 / 只回指定的人。

对齐 JS 版 ``mention-gate`` 的三个功能：

① ``group_ids``：名单里的群，机器人只有被 @（或戳一戳）才开口，其余消息当场丢弃，
   一次模型都不调。
② ``user_rules``：精确到"哪个群的哪个人"（一行 ``群号=QQ号,QQ号``）。写了这一项的群，
   名单上的人 @ 它会被忽略，同一群其他人照常回复。
③ ``mode``：``allowlist``（默认）= 名单之外的群完全不受影响；``only`` = 只服务名单里的群。

@ 的判定按 **QQ 号**（``message["at_ids"]``），不看昵称 —— 重名的人 @ 不到它。
戳一戳按 ``message["notice"] == "poke"`` 识别（需要 OneBot 上报 notice 事件）。

降级点（相对 JS 版）：
- JS 版把"①"写成宿主的**分群档位**，因此连"是否建会话"都能省下；本项目只能在
  ingest 阶段丢弃消息（同样不调模型，但已经进过一次 ingest）。
- JS 版的"②"除了硬拦截发送工具，还会往提示词里加一句规则；本项目的 ``extra_system``
  与工具级硬拦截不等价（本项目没有 tool 层），因此只做消息级拦截 + 提示词说明。
- ``user_rules`` 里写昵称的部分无法解析（本项目拿不到群成员名单），只有 QQ 号生效。
"""

from __future__ import annotations

import re
from typing import Optional

from . import PluginBase, PluginMeta, FieldSchema

# 群号 / QQ 号抽取：官方 QQ 的群 openid 是**十六进制串**（含 A-F），只认数字会把
# "1CBE87EF553193840AFFDCC788043580" 切成 ['553193840','788043580']（真机踩过：
# 名单里的群永远匹配不上，only 模式下等于把全部群都拦了）。
# 所以按"任何非字母数字的分隔符"切，只保留长度 ≥5 的字母数字片段。
_ID_RE = re.compile(r'[0-9A-Za-z]{5,64}')


def parse_ids(value: str) -> list:
    """从任意分隔的文本里抽出群号 / QQ 号 / 群 openid，保持顺序去重。

    两种形状都算 id：
    - 纯数字 5~12 位：普通 QQ 的群号 / QQ 号；
    - ≥20 位的长字母数字串：官方 QQ 的 openid（例如 ``1CBE87EF553193840AFFDCC788043580``）。

    这样既不会把 openid 按字母切碎（真机踩过：名单里的群永远匹配不上），
    也不会把 ``group=someone`` 这种说明文字误当成 id。
    """
    result = []
    for match in _ID_RE.findall(str(value or "")):
        is_qq = match.isdigit() and 5 <= len(match) <= 12
        is_openid = len(match) >= 20
        if (is_qq or is_openid) and match not in result:
            result.append(match)
    return result


def parse_user_rules(value: str) -> dict:
    """解析"群号=QQ号,QQ号"的多行规则，返回 ``{群号: [QQ号, ...]}``。

    容忍空行、``#`` 注释行与全角等号；写错的行走日志（由调用方决定怎么提示）。
    """
    rules = {}
    for raw_line in str(value or "").replace("＝", "=").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        group_part, user_part = line.split("=", 1)
        groups = parse_ids(group_part)
        users = parse_ids(user_part)
        if not groups or not users:
            continue
        for group in groups:
            existing = rules.setdefault(group, [])
            for user in users:
                if user not in existing:
                    existing.append(user)
    return rules


class MentionGate(PluginBase):
    META = PluginMeta(
        id="mention_gate",
        name="点名闸门",
        description=(
            "给机器人加一道点名闸门：① 名单里的群只在被 @ 或戳一戳时才开口（其余消息不调模型）；"
            "② 精确到某个群的某个人（名单外的人照常回复）；③ 名单之外的群可以完全不受影响，"
            "也可以谁叫都不理。@ 按 QQ 号判定，重名不会误判。"
        ),
        category="message",
        enabled_by_default=True,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用点名闸门"),
            "group_ids": FieldSchema(
                type="string", label="① 整群只在被@时回复",
                description="填群号，多个用逗号隔开，如 123456789, 987654321。留空 = 本项不启用。",
            ),
            "user_rules": FieldSchema(
                type="string", label="② 只回指定的人（按群，一行一个）", multiline=True,
                description="格式：群号=QQ号，一行一个。例：123456789=10001,10002。本项只认 QQ 号。",
            ),
            "mode": FieldSchema(
                type="select", label="③ 名单外群的待遇", values=["allowlist", "only"],
                description="allowlist = 名单之外的群保持你原来的设置；only = 只服务名单里的群。",
            ),
            "prompt_note": FieldSchema(
                type="bool", label="提示词里也写明本群规则",
                description="默认开：受管群会多一句规则说明，免得模型以为自己漏了消息。",
            ),
        },
        default_settings={
            "enabled": True,
            "group_ids": "",
            "user_rules": "",
            "mode": "allowlist",
            "prompt_note": True,
        },
    )

    def __init__(self, manager):
        super().__init__(manager)

    def post_load(self):
        """迁移旧键。

        必须放在 ``post_load`` 而不是 ``__init__``：``__init__`` 跑在管理器把本实例
        放进 ``_plugins`` 之前，那时 ``update_settings`` 会抛
        ``Unknown plugin: mention_gate`` —— 这个迁移以前就是这么**静默失败**的。
        """
        self._migrate_legacy()

    # ── 旧键迁移 ─────────────────────────────────────────────────────────
    # 本项目早期版本用的是 onlyGroups（"生效群"）与 allowKeyword（"豁免关键词"），
    # 与 JS 的 groupIds（"这些群仅 @"）语义**不同**：直接用新名字读旧值会把行为弄反。
    # 因此只做一次单向迁移：旧值存在且新键为空时，把旧群号搬过来并落盘，
    # 同时把差异写进日志。旧字段不再参与判定。
    def _migrate_legacy(self):
        try:
            # 用 raw_settings：settings 会被 schema 过滤，看不见已废弃的旧键
            settings = dict(self.raw_settings)
            legacy_groups = settings.pop("onlyGroups", None)
            legacy_keyword = settings.pop("allowKeyword", None)
            changed = False
            if legacy_groups and not settings.get("group_ids"):
                ids = parse_ids(legacy_groups)
                if ids:
                    settings["group_ids"] = ",".join(ids)
                    changed = True
                    print(f"[plugins] mention_gate: 已把旧 onlyGroups 迁移为 group_ids（{len(ids)} 个群）；"
                          f"注意新语义是“这些群只在被 @ 时回复”")
            if legacy_keyword is not None:
                changed = True
                print("[plugins] mention_gate: 旧 allowKeyword 已停用；"
                      "新语义只认 @ 与戳一戳，不再按关键词豁免")
            if changed:
                self._manager.update_settings(self.META.id, settings)
        except Exception as exc:
            print(f"[plugins] mention_gate 迁移失败（不影响运行）：{exc}")

    # ── 内部 ─────────────────────────────────────────────────────────────
    def _group_ids(self) -> list:
        return parse_ids(self.get("group_ids", ""))

    def _user_rules(self) -> dict:
        return parse_user_rules(self.get("user_rules", ""))

    def _blocked_by_user_rule(self, group_id: str, message: dict) -> bool:
        """② 只回指定的人：名单上的人 @ 它时忽略，同一群其他人照常。"""
        rule = self._user_rules().get(group_id)
        if not rule:
            return False
        sender_id = str(message.get("sender_id") or "")
        if not sender_id:
            return False  # 拿不到 QQ 号时不误伤（宁可按原设置回复）
        return sender_id in rule

    def on_block(self, message: dict, runtime) -> bool:
        if not self.get("enabled"):
            return False
        try:
            if message.get("kind") != "group":
                return False
            group_id = str(message.get("target", ""))
            managed = self._group_ids()
            in_group_list = group_id in managed
            has_user_rule = group_id in self._user_rules()

            # 名单之外：only 模式下一律不理；allowlist 模式完全不受影响。
            if not in_group_list and not has_user_rule:
                return str(self.get("mode", "allowlist")) == "only"

            # ② 优先于 ①：写了"只回指定的人"的群里，名单上的人被忽略。
            if self._blocked_by_user_rule(group_id, message):
                return True

            # ① 整群闸门：只有被 @ 或（戳的是机器人自己）才放行。
            if in_group_list:
                if message.get("notice") == "poke":
                    # 拍一拍要确认戳的是**机器人**：群里"甲戳乙"不该让机器人开口。
                    return not self._poked_me(message, runtime)
                if message.get("at"):
                    return False
                # 唤醒词（昵称）也算点名：chat_controls 那边认的称呼，这里不能拦掉，
                # 否则"设了唤醒词却不触发"——因为消息在这道闸门就被丢了（真机踩过）。
                if self._addressed_by_alias(message, runtime):
                    return False
                return True
            return False
        except Exception:
            return False

    def _addressed_by_alias(self, message: dict, runtime) -> bool:
        """这句话是否命中 chat_controls 里配置的唤醒词（昵称即点名）。

        两个插件以前各定义一套"点名"：chat_controls 认唤醒词、mention_gate 只认 @，
        结果是"设了唤醒词根本不触发"。现在统一走 ``src/mention.py`` 的判定。
        """
        try:
            from ..mention import addressed as mention_hit
            controls = self._manager._plugins.get("chat_controls")
            aliases = ""
            if controls is not None:
                try:
                    aliases = str(controls.get("aliases", "") or "")
                except Exception:
                    aliases = ""
            if not aliases:
                aliases = "鲸鱼娘,小鲸鱼"        # 与 chat_controls 的默认值保持一致
            hit, _why = mention_hit(message.get("text", ""), aliases,
                                    at=bool(message.get("at")))
            return hit
        except Exception:
            return False

    @staticmethod
    def _self_id(runtime) -> str:
        """取机器人自己的 QQ 号（尽力而为，取不到返回空串）。"""
        for source in (getattr(runtime, "self_id", None),
                       getattr(runtime, "account", None)):
            try:
                value = source() if callable(source) else source
            except Exception:
                continue
            if value:
                return str(value)
        onebot = getattr(runtime, "onebot", None)
        for attr in ("self_id", "qq"):
            value = getattr(onebot, attr, None) if onebot is not None else None
            if value:
                return str(value)
        return ""

    def _poked_me(self, message: dict, runtime) -> bool:
        """这条拍一拍是不是戳在机器人身上。

        传输层给的 ``notice_data`` 形如 ``{"poked", "poker", "group"}``。
        取不到"被戳的是谁"或取不到机器人自己的 QQ 号时，保守放行（保持旧行为），
        避免因为拿不到身份而把功能关死。
        """
        notice_data = message.get("notice_data")
        poked = ""
        if isinstance(notice_data, dict):
            poked = str(notice_data.get("poked") or "").strip()
        if not poked:
            return True
        self_id = self._self_id(runtime)
        if not self_id:
            return True
        return poked == self_id

    def on_pre_ai(self, message: dict, conversation: list, runtime) -> Optional[dict]:
        """受管群且开了 prompt_note 时，往系统提示里补一句规则说明。"""
        if not self.get("enabled") or not self.get("prompt_note", True):
            return None
        try:
            if message.get("kind") != "group":
                return None
            group_id = str(message.get("target", ""))
            rules = self._user_rules()
            if group_id in rules:
                note = (f"【点名闸门】本群只服务指定的人；如果消息不是他们发的，就不要回复。"
                        f"能收到这条消息说明是名单上的人点名找你，正常回即可。")
            elif group_id in self._group_ids():
                note = "【点名闸门】本群只在被 @ 或戳一戳时才找你；能收到消息说明有人点名了，正常回即可。"
            else:
                return None
            return {"extra_system": note}
        except Exception:
            return None
