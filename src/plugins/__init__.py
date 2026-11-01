"""Plugin system for QQ robot - 触发节奏 trigger rhythm plugins."""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── 确认口径（闸门与 mod 工具宿主共用这一份实现）───────────────────────────────
# 为什么只写一份：控制台闸门（src/agent_tools.py DangerGate）与 mod 工具宿主
# （src/plugins/manager.py ModToolHost.call_tool）各判一次"这次调用带没带用户确认"。
# 两处口径一旦不同，就会出现"闸门放行、mod 层再拒一次"：模型被连拒两次，还不知道
# 该改哪里。真值表只在这里改，谁要判确认都调 has_confirm / confirm_flag。
# 放在本包 __init__ 的理由：它不 import 任何 src 模块，src/agent_tools.py 与
# src/plugins/manager.py 都可以安全地向上引用，不产生循环导入。
CONFIRM_KEY = "confirm"
CONFIRM_TRUE_TEXTS = ("1", "true", "yes", "on", "开", "开启")


def confirm_flag(value) -> bool:
    """把一个值解释成布尔：True/非零数、"1"/"true"/"yes"/"on"/"开"/"开启" 为真。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    return text in CONFIRM_TRUE_TEXTS


def has_confirm(arguments) -> bool:
    """arguments 里带没带确认（键名 CONFIRM_KEY）；不是 dict 一律当没带。"""
    args = arguments if isinstance(arguments, dict) else {}
    return confirm_flag(args.get(CONFIRM_KEY))


@dataclass
class FieldSchema:
    type: str
    label: str
    description: str = ""
    group: str = ""
    values: List[str] = field(default_factory=list)
    options: List[Dict] = field(default_factory=list)
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    multiline: bool = False
    file_picker: str = ""  # file / files / labeled_files / directory
    file_extensions: List[str] = field(default_factory=list)
    file_max_mb: int = 6
    editor: str = ""


@dataclass
class PluginMeta:
    id: str
    name: str
    description: str
    category: str = "utility"
    enabled_by_default: bool = True
    config_schema: Dict[str, FieldSchema] = field(default_factory=dict)
    default_settings: Dict[str, Any] = field(default_factory=dict)

    def schema_dict(self) -> dict:
        result = {}
        for k, v in self.config_schema.items():
            d = {"type": v.type, "label": v.label, "description": v.description}
            if v.group:
                d["group"] = v.group
            if v.values:
                d["values"] = v.values
            if v.options:
                d["options"] = v.options
            if v.min is not None:
                d["min"] = v.min
            if v.max is not None:
                d["max"] = v.max
            if v.step is not None:
                d["step"] = v.step
            if v.multiline:
                d["multiline"] = True
            if v.file_picker:
                d.update(file_picker=v.file_picker, file_extensions=list(v.file_extensions), file_max_mb=v.file_max_mb)
            if v.editor:
                d['editor']=v.editor
            result[k] = d
        return result


class PluginBase:
    META: PluginMeta = None

    def __init__(self, manager):
        self._manager = manager

    @property
    def settings(self) -> dict:
        return self._manager.get_settings(self.META.id)

    @property
    def raw_settings(self) -> dict:
        """已保存的**原始**设置，不做 schema 过滤。

        ``settings`` 只返回 schema 里已知的键，所以**看不见已废弃的旧键**
        （例如 mention_gate 早期的 ``onlyGroups``）。做"旧键迁移"必须用这个，
        否则旧值在读取阶段就被过滤掉了，迁移拿不到东西。
        """
        stored = self._manager._settings.get(self.META.id)
        return dict(stored) if isinstance(stored, dict) else {}

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", self.META.enabled_by_default))

    def get(self, key, default=None):
        s = self.settings
        if key in s:
            return s[key]
        return self.META.default_settings.get(key, default)

    def on_ingest(self, message: dict, runtime) -> Optional[str]:
        """Return a str to immediately reply (skip AI), None to continue."""
        return None

    def on_block(self, message: dict, runtime) -> bool:
        """Return True to silently drop this message."""
        return False

    def wants_reply(self, message: dict, runtime) -> bool:
        """Return True to let an otherwise-unaddressed group message reach the AI.

        "群聊随机接话"靠这个钩子参与群聊：没有它，未 @ 的消息在进入队列前就被丢了。
        """
        return False

    def on_pre_ai(self, message: dict, conversation: list, runtime) -> Optional[dict]:
        """Return dict with optional keys: delay(s), block, extra_system, volatile_system, max_tokens."""
        return None

    def volatile_hint(self, runtime, text: str) -> Optional[dict]:
        """包一条"每轮都会变"的说明：装了「缓存稳定性」并开着就排到提示词最后。

        服务商按**前缀**缓存计费：稳定内容（人设、固定规则）在前、易变内容
        （心情数值、本轮发言人摘要、跟聊提醒…）在后，命中率才高。没装或关掉那个插件时
        退回原来的写法（``extra_system``，混在插件顺序里），行为和以前完全一致。
        """
        gate = getattr(getattr(runtime, "plugins", None), "_plugins", {}).get("cache_stability")
        volatile = gate is not None and gate.enabled and bool(gate.get("stableOrder", True))
        return {"volatile_system" if volatile else "extra_system": text}

    def validate_settings(self, settings: dict) -> None:
        """Optional validation of structured values after schema validation."""

    async def on_prepare(self, message: dict, runtime) -> None:
        """Await protocol lookups before constructing the model conversation."""

    def on_sent(self, message: dict, reply: str, runtime) -> None:
        """Record only successfully sent results."""

    async def on_post_reply(self, message: dict, reply: str, runtime) -> str:
        """Transform the final reply string (send-pacing uses this)."""
        return reply

    async def tick(self, runtime) -> None:
        """Periodic background task called every ~60s when bot is running."""

    def post_load(self) -> None:
        """在**实例已被管理器注册之后**调用一次；默认什么都不做。

        为什么需要这个钩子：插件的 ``__init__`` 跑在管理器把实例放进
        ``_plugins`` **之前**，所以 ``__init__`` 里调
        ``self._manager.update_settings(...)`` 会抛
        ``KeyError: Unknown plugin: <id>``。
        ``mention_gate`` 的旧键迁移就是这么静默失败的（日志里只有一行
        "迁移失败（不影响运行）"）。需要落盘改设置的插件请把逻辑放这儿。
        """

    def state_file(self, name: str) -> Path:
        p = self._manager.state_dir / self.META.id
        p.mkdir(parents=True, exist_ok=True)
        return p / name

    def load_state(self, name: str, default=None):
        f = self.state_file(name)
        try:
            return json.loads(f.read_text("utf-8"))
        except Exception:
            return {} if default is None else default

    def save_state(self, name: str, data):
        self.state_file(name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), "utf-8"
        )

# ── mod（可单独挂载的插件目录）──────────────────────────────────────────────
# 这一段是**只加不改**的新增内容：PluginBase 的钩子签名与 load_state/save_state
# 行为保持不变，内置 63 个插件与老的 <id>.py 自定义插件都不受影响。
MOD_API_VERSION = 1

# mod 能声明的权限（manifest 里 permissions 的合法取值）。除 register_tool 外只做
# **声明与查询**，不在这里拦截具体动作（例如 network），拦截靠的是下面 ModRuntime
# 只暴露少量成员；register_tool 是唯一在动作处被真实拦截的一项：没声明它的 mod
# 调 register_tool 会被 PluginManager 的工具宿主直接拒掉（见 ModToolHost.register_tool）。
MOD_PERMISSIONS = frozenset({
    "read_message", "send_message", "store_data", "network", "manage_settings",
    "register_tool",
})


class ModToolError(ValueError):
    """注册/调用 mod 工具被拒：消息是给作者看的中文原因，不能静默。"""


class ModRuntime:
    """mod 插件在钩子里拿到的**只读门面**，不是活的 QQRuntime。

    为什么要有它：内置插件拿的是真 runtime（可以直接翻 bot/plugins/db），那是 63 个
    内置插件的既有契约，动不得；mod 是第三方代码，把同一个对象递过去等于把主体内部
    整体交出去。

    暴露（就这些）：mod_id / api_version / settings / get / has_permission /
    register_tool / log / note / now。
    封锁：其它任何属性（bot、plugins、config、db、engine…）一律 AttributeError，
    并且错误文本直接说明"mod 不允许访问"，而不是让人以为是拼错了。

    register_tool 只是转交：真正做权限校验、超时、失败自愈和审计的是主体侧的
    工具宿主（plugins/manager.py ModToolHost），门面里不复制那三道闸。
    """

    __slots__ = ("__mod_id", "__permissions", "__reader", "__note", "__tools")

    def __init__(self, mod_id: str, settings_reader, note=None, permissions=(), tools=None):
        object.__setattr__(self, "_ModRuntime__mod_id", str(mod_id))
        object.__setattr__(self, "_ModRuntime__permissions",
                           frozenset(str(item) for item in (permissions or ())))
        object.__setattr__(self, "_ModRuntime__reader", settings_reader)
        object.__setattr__(self, "_ModRuntime__note", note)
        object.__setattr__(self, "_ModRuntime__tools", tools)

    @property
    def mod_id(self) -> str:
        return self.__mod_id

    @property
    def api_version(self) -> int:
        return MOD_API_VERSION

    @property
    def settings(self) -> dict:
        """当前插件设置的一份拷贝；改它不会写回任何地方。"""
        try:
            value = self.__reader()
        except Exception:
            return {}
        return dict(value) if isinstance(value, dict) else {}

    def get(self, key, default=None):
        s = self.settings
        return s[key] if key in s else default

    def has_permission(self, name: str) -> bool:
        return str(name) in self.__permissions

    def log(self, message) -> None:
        self.note(message)

    def note(self, message) -> None:
        note = self.__note
        if callable(note):
            try:
                note(str(message))
                return
            except Exception:
                pass
        print(f"[mod:{self.__mod_id}] {message}")

    def now(self) -> float:
        """当前时间戳（秒）；mod 不拿主体的时间源，只给这个数。"""
        import time
        return time.time()

    def register_tool(self, name, handler, description='', parameters=None, *,
                      write=False):
        """把一个工具交给主体登记，返回它的规范全名 mod__<mod_id>__<name>。

        handler 的签名是 handler(arguments: dict, runtime: ModRuntime) -> Any，
        第二个参数永远是**门面**（不是活的 QQRuntime）；返回值可以是字符串或任何
        可 JSON 化的值。工具由插件实例上的 list_tools()/call_tool() 统一暴露给 QQ
        工具环，mod 作者不用自己写那两个方法。

        write=True 表示"写操作"：调用时 arguments 必须带 confirm:true，否则不执行。
        """
        tools = self.__tools
        if tools is None:
            raise ModToolError(
                f"mod {self.__mod_id} 没有工具宿主（插件实例未挂上工具表），不能注册工具")
        return tools.register_tool(name, handler, description, parameters, write=write)

    def __getattr__(self, name):
        raise AttributeError(
            f"ModRuntime 不向 mod 提供 {name!r}：mod 只能访问 "
            "mod_id/api_version/settings/get/has_permission/register_tool/"
            "log/note/now")

    def __setattr__(self, name, value):
        raise AttributeError(f"ModRuntime 是只读门面，不能设置 {name!r}")

    def __repr__(self) -> str:
        return f"<ModRuntime mod={self.__mod_id} api={MOD_API_VERSION}>"

