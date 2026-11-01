"""PluginManager: loads, stores settings, and dispatches plugin hooks."""
from __future__ import annotations
import json
import importlib
import importlib.util
import math
import copy
import asyncio
import inspect
import queue
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import (ModRuntime, ModToolError, MOD_API_VERSION, MOD_PERMISSIONS, PluginBase,
               has_confirm)

PLUGIN_LOAD_TIMEOUT = 4.0
# mod 钩子超时（秒）：超时按失败处理，主体继续跑其它插件（模块级常量，可整体改）。
MOD_HOOK_TIMEOUT = 2.0
# 连续失败（超时/抛异常）多少次自动禁用；成功一次即清零。
MOD_FAILURE_LIMIT = 3
# 目录 mod 的清单文件名：<custom_plugins_dir>/<id>/plugin.json
MOD_MANIFEST_NAME = "plugin.json"
# mod 工具名的长度上限：与 JS 扩展 <skillId>__<tool> 同一口径
# （runtime/qqagent-host/src/plugin-loader.js:77-84，以及 src/tool_loop.py:174 的 _clean_name）。
MOD_TOOL_NAME_LIMIT = 64
# 工具名里只允许这些字符（其它一律拒，不做静默替换：模型看不到被换过的名字）。
MOD_TOOL_NAME_BAD = re.compile(r"[^0-9A-Za-z_-]")


class ModManifestError(ValueError):
    """plugin.json 不合契约：拒载该 mod，并带上 code 便于审计归类。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _ModHookError(RuntimeError):
    """mod 钩子超时/抛异常的统一出口：交给调用方既有的 except 记录，不往主体冒。"""


def _find_dependency_cycle(edges: Dict[str, List[str]]):
    """在 mod 依赖图里找一条环（形如 ['a', 'b', 'a']），无环返回 None。

    迭代式 DFS，不用递归：mod 再多也不会撞 RecursionError。按传入顺序挑起点、
    按 depends 顺序走边，同一张图每次挑出来的环一样，断言才写得住。
    """
    state: Dict[str, int] = {}     # 缺省=未访问，1=在当前路径上，2=已收敛
    for start in edges:
        if state.get(start):
            continue
        path = [start]
        state[start] = 1
        stack = [(start, iter(edges.get(start, ())))]
        while stack:
            node, pending = stack[-1]
            advanced = False
            for dep in pending:
                where = state.get(dep, 0)
                if where == 0:
                    state[dep] = 1
                    path.append(dep)
                    stack.append((dep, iter(edges.get(dep, ()))))
                    advanced = True
                    break
                if where == 1:
                    return path[path.index(dep):] + [dep]
            if not advanced:
                state[node] = 2
                stack.pop()
                path.pop()
    return None


def _file_stamp(path) -> tuple:
    """(mtime_ns, size)：变更检测的快速判据，**不是内容校验**。

    mtime 只到秒不够（同一秒内覆盖同长度源码很常见），所以取纳秒；但两个数都只是
    "看起来像变了"：只要 mtime 被回填、且大小一字不差（cp -p、解压、rsync --times、
    部分编辑器保留时间戳），改过的文件照样判成 unchanged，改动就是看不见
    （见 docs/插件接入契约-mod.md §8 第 6 条）。要认内容只能另外比哈希。
    """
    try:
        info = Path(path).stat()
    except OSError:
        return (-1, -1)
    return (int(info.st_mtime_ns), int(info.st_size))

# 允许写进 settings、但不需要出现在插件 config_schema 里的控制字段。
# "安装后立即启用"必须对没声明 enabled 的第三方插件同样有效，而 PluginBase.enabled
# 统一从 settings['enabled'] 读（见 plugins/__init__.py），所以这里放行并自行校验。
CONTROL_SETTING_FIELDS = frozenset({'enabled'})


def _validate_control_field(field: str, value: Any, plugin_id: str) -> Any:
    """校验保留控制字段的取值；非控制字段一律仍按未知字段拒绝。"""
    if field == 'enabled':
        if not isinstance(value, bool):
            raise ValueError(
                f"{plugin_id}: '{field}' 需要布尔值 true/false，"
                f"收到 {type(value).__name__}")
        return value
    raise ValueError(f"{plugin_id}: unknown setting field '{field}'")

PLUGIN_MODULES = [
    'quick_menu',
    'archive_isolation','cross_chat_link','long_image_reader','hot_trend_tracker','search_query_hygiene','pc_control',
    'sticker_steal','sticker_admin','emoji_echo','msg_reaction',
    "conversation_memory", "chat_controls", "emotion_state", "image_library", "memes", "scheduled_announcer",
    "sleep_mode", "burst_merge", "send_pacing", "context_chime",
    "repeat_echo", "question_relay", "proactive_chat", "silence_ping",
    "probability_reply", "typing_indicator", "anti_repeat",
    "mention_gate", "chatter_guard", "keyword_reply", "typo_sim",
    "short_followup", "mention_nudge_guard", "addressee_guard",
    "sticker_cadence", "reply_guards", "quote_reply",
    "turn_complete", "at_mention", "poke_counter", "icebreaker", "sticker_memory",
    "affinity_proactive", "pixiv_illust", "face_decode", "sticker_cap", "share_card",
    "forward_summary", "speech_to_text_qq", "image_understanding", "early_finish",
    "market_probe", "minimal_mode", "token_saver", "cache_stability", "affinity",
    "member_aliases", "group_cost_report", "reply_freshness", "reply_variety",
    "ambient_reply", "qqagent_bridge",
]

# 安装包只内置基础插件；冻结运行时不要再尝试 import 市场模块（否则日志里全是
# "Failed to load xxx"，看起来像坏了，实际只是没随包分发）。
if getattr(sys, 'frozen', False):
    from ..plugin_catalog import BASIC_PLUGINS
    PLUGIN_MODULES = [name for name in PLUGIN_MODULES if name in BASIC_PLUGINS]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _smaller_limit(current, candidate) -> int:
    """多个插件都要给输出封顶时取**最小**的那个（0 表示不封顶）。

    只有"调小"这一条路：省 token 的档位不该把上限顶上去，也不该被别家放大。
    """
    try:
        wanted = int(candidate or 0)
    except (TypeError, ValueError):
        return int(current or 0)
    if wanted <= 0:
        return int(current or 0)
    return wanted if not current or wanted < int(current) else int(current)


def _coerce_numeric(value: Any, expected_type: type) -> Any:
    """拒绝隐式字符串转换；int 字段只接受整数或整数值浮点。"""
    if expected_type is int:
        if isinstance(value, bool):
            raise ValueError("int field cannot accept bool")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise ValueError(f"int field requires int, got {type(value).__name__}")
    if expected_type is float:
        if isinstance(value, bool):
            raise ValueError("float field cannot accept bool")
        if isinstance(value, (int, float)):
            result = float(value)
        else:
            raise ValueError(f"float field requires number, got {type(value).__name__}")
        if math.isnan(result) or math.isinf(result):
            raise ValueError("float field must not be NaN or Infinity")
        return result
    raise ValueError(f"unknown numeric type {expected_type}")


def _validate_field(schema, value, field_name: str, plugin_id: str) -> Any:
    """按 FieldSchema 校验单个字段，非法值抛 ValueError。"""
    ftype = schema.type
    if ftype == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError(f"{plugin_id}.{field_name}: bool field requires bool")
    if ftype in ("int", "float"):
        value = _coerce_numeric(value, int if ftype == "int" else float)
        if schema.min is not None and value < schema.min:
            raise ValueError(f"{plugin_id}.{field_name}: value {value} below min {schema.min}")
        if schema.max is not None and value > schema.max:
            raise ValueError(f"{plugin_id}.{field_name}: value {value} above max {schema.max}")
        return value
    if ftype == "string":
        if not isinstance(value, str):
            raise ValueError(f"{plugin_id}.{field_name}: string field requires str")
        return value
    if ftype == "select":
        if isinstance(value, bool):
            raise ValueError(f"{plugin_id}.{field_name}: select field cannot accept bool")
        allowed = {opt.get("value") for opt in schema.options} | set(schema.values)
        if value in allowed:
            return value
        raise ValueError(f"{plugin_id}.{field_name}: value {value!r} not in allowed select options")
    # 未知 schema 类型属于 schema 自身的错误，此处不拦。
    return value


def _fallback_field_default(schema_field, defaults: Dict[str, Any], field_name: str) -> Any:
    """字段级回退：优先用 default_settings，其次按类型给中性值。"""
    if field_name in defaults:
        return copy.deepcopy(defaults[field_name])
    ftype = schema_field.type
    if ftype == "bool":
        return False
    if ftype == "int":
        return 0
    if ftype == "float":
        return 0.0
    if ftype == "string":
        return ""
    if ftype == "select":
        options = schema_field.options or []
        values = schema_field.values or []
        if options:
            first = options[0]
            return first.get("value") if isinstance(first, dict) else first
        if values:
            return values[0]
        return None
    return None


class ModToolHost:
    """一个 mod 的工具表 + 三道闸（权限 / 超时自愈 / 审计）的唯一实现处。

    为什么要有这个类：src/tool_loop.py:354 的 register_plugin_tools 只认插件实例上的
    list_tools() 和 call_tool(name, args)，所以这两个方法必须真的挂在**插件实例**上
    （见 PluginManager._attach_mod_tools）；但 mod 作者不该自己写它们，也不该自己实现
    权限/超时/自愈/审计。于是工具表放在这里，方法绑到实例上。

    注册入口 register_tool 同时挂在两处：插件实例上（加载期，post_load 里就能用）和
    ModRuntime 门面上（钩子内用 runtime.register_tool(...)）。

    内置 63 个插件与裸 .py 插件一条都不经过这里：它们没有 register_tool 权限体系，
    工具环对它们的行为完全不变。
    """

    def __init__(self, manager, plugin, mod_id: str):
        self._manager = manager
        self._plugin = plugin
        self._mod_id = str(mod_id)
        self._pid = str(getattr(getattr(plugin, 'META', None), 'id', '') or mod_id)
        manifest = manager._mod_manifests.get(self._mod_id) or {}
        self._permissions = frozenset(str(item)
                                      for item in (manifest.get('permissions') or ()))
        self._tools: Dict[str, dict] = {}

    # ── 名字 ────────────────────────────────────────────────────────────────

    def full_name(self, name) -> str:
        """规范全名 mod__<mod_id>__<name>：审计、去重、长度都按它判。"""
        return f"mod__{self._mod_id}__{name}"

    def registry_name(self, name) -> str:
        """工具环里的真名：tool_loop 会再拼一层 <插件 id>__（src/tool_loop.py:246）。"""
        return f"{self._pid}__{name}"

    # ── 注册 ────────────────────────────────────────────────────────────────

    def register_tool(self, name, handler, description='', parameters=None, *, write=False):
        """登记一个工具，返回它的规范全名；任何不合规都抛 ModToolError（中文原因）。"""
        if "register_tool" not in self._permissions:
            raise ModToolError(
                f'mod {self._mod_id} 的 plugin.json 没有声明 register_tool 权限，'
                '不能注册工具：请在 permissions 里加上 "register_tool" 后重新加载')
        if not isinstance(name, str) or not name:
            raise ModToolError("工具名必须是非空字符串")
        bad = MOD_TOOL_NAME_BAD.search(name)
        if bad:
            raise ModToolError(
                f"工具名 {name!r} 含非法字符 {bad.group(0)!r}：只允许字母、数字、"
                "下划线、连字符（[A-Za-z0-9_-]）")
        full = self.full_name(name)
        bridge = self.registry_name(name)
        if len(full) > MOD_TOOL_NAME_LIMIT or len(bridge) > MOD_TOOL_NAME_LIMIT:
            raise ModToolError(
                f"工具名过长：{full!r}（工具环里的名字是 {bridge!r}）超过 "
                f"{MOD_TOOL_NAME_LIMIT} 字符上限，请改短 name")
        if full in self._tools:
            raise ModToolError(f"工具 {full} 与本 mod 已注册的工具重名："
                               "先注册的那条保留，后注册的这条被拒")
        if not callable(handler):
            raise ModToolError(f"工具 {full} 的 handler 必须可调用")
        if parameters is not None and not isinstance(parameters, dict):
            raise ModToolError(f"工具 {full} 的 parameters 必须是 dict 或 None")
        self._tools[full] = {
            "name": name, "full": full, "bridge": bridge, "handler": handler,
            "description": str(description or ""), "write": bool(write),
            "parameters": (dict(parameters) if parameters
                           else {"type": "object", "properties": {}}),
        }
        return full

    # ── 工具环桥（挂在插件实例上的那两个方法）─────────────────────────────────

    @property
    def enabled(self) -> bool:
        """mod 自己（插件实例）是否还启用着；被自动禁用后工具立刻不可见。"""
        try:
            return bool(self._plugin.enabled)
        except Exception:
            return False

    def list_tools(self) -> list:
        """工具环要的清单；被禁用/被自动禁用的 mod 一条都不暴露。"""
        if not self.enabled:
            return []
        return [{"name": record["name"],
                 "description": self._describe(record),
                 "parameters": dict(record["parameters"])}
                for record in self._tools.values()]

    def call_tool(self, name, arguments=None):
        """工具环的调用入口：权限 → 写确认 → 超时/失败自愈 → 审计，全走这一处。

        返回值一律是 JSON 可序列化的 dict：成功给 value，失败给 error.code。
        """
        started = time.monotonic()
        record = self._lookup(name)
        if record is None:
            return self._refuse(str(name or ""), False, "unknown-tool",
                                f"mod {self._mod_id} 没有注册叫 {name!r} 的工具",
                                started)
        tool, write = record["full"], record["write"]
        if "register_tool" not in self._permissions:
            return self._refuse(
                tool, write, "no-permission",
                f'mod {self._mod_id} 的 plugin.json 没有声明 register_tool 权限，'
                f'工具 {record["name"]} 不可调用', started)
        if not self.enabled:
            return self._refuse(
                tool, write, "disabled",
                f"mod {self._pid} 已被禁用（连续失败自动禁用或手动关闭），"
                f'工具 {record["name"]} 不可调用', started)
        arguments = dict(arguments) if isinstance(arguments, dict) else {}
        # 与闸门共用同一份确认判定（has_confirm）：闸门认的写法这里也认，
        # 不会出现"闸门放行、这里再拒一次"；但这一层仍独立拦没有确认的写工具。
        if write and not has_confirm(arguments):
            return self._refuse(
                tool, True, "confirm-required",
                f"{tool} 是写操作：arguments 里必须带 confirm:true，本次未执行", started)
        handler = record["handler"]
        value, error, timed_out = self._manager._run_guarded(
            lambda: handler(dict(arguments), self.facade()), MOD_HOOK_TIMEOUT)
        duration = int(round((time.monotonic() - started) * 1000))
        if timed_out or error:
            code = "timeout" if timed_out else "exception"
            self._write_audit(tool, write, False, code, duration, error)
            # 复用既有的连续失败计数与自动禁用，不另造一套。
            self._manager._record_mod_failure(
                self._plugin, "tool:" + record["name"], code, error)
            return {"ok": False, "tool": tool, "write": write, "code": code,
                    "duration_ms": duration, "error": {"code": code, "message": error}}
        self._manager._record_mod_success(self._plugin)
        self._write_audit(tool, write, True, "ok", duration)
        return {"ok": True, "tool": tool, "write": write, "code": "ok",
                "duration_ms": duration, "value": value}

    def facade(self) -> "ModRuntime":
        """每次调用新建一个门面：handler 的第 2 个参数永远是它，绝不是活的 QQRuntime。"""
        return ModRuntime(self._pid, lambda: self._plugin.settings,
                          permissions=self._permissions, tools=self)

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _lookup(self, name):
        """容忍三种写法：<name>（工具环传的就是它）、<插件 id>__<name>、mod__<mod_id>__<name>。"""
        key = str(name or '')
        for record in self._tools.values():
            if key in (record["name"], record["bridge"], record["full"]):
                return record
        return None

    @staticmethod
    def _describe(record) -> str:
        """说明文字；写工具必须自报 confirm 要求，否则模型不知道该带参数。"""
        text = record["description"] or record["full"]
        if record["write"]:
            return f"{text}（写操作：调用时 arguments 必须带 confirm:true，否则不执行）"
        return text

    def _refuse(self, tool, write, code, message, started) -> dict:
        duration = int(round((time.monotonic() - started) * 1000))
        self._write_audit(tool, write, False, code, duration, message)
        return {"ok": False, "tool": tool, "write": bool(write), "code": code,
                "duration_ms": duration, "error": {"code": code, "message": message}}

    def _write_audit(self, tool, write, ok, code, duration_ms, detail='') -> None:
        """每次调用一行 tool-call 审计（成功、拒、超时、异常都写）。"""
        self._manager._write_audit(
            'tool-call', plugin=self._pid, hook='tool-call', code=code, detail=detail,
            tool=tool, write=bool(write), ok=bool(ok), duration_ms=int(duration_ms))


class PluginManager:
    def __init__(self, data_dir: Path, custom_plugins_dir: Optional[Path] = None):
        self.data_dir = Path(data_dir)
        self.state_dir = self.data_dir / "plugin_state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._settings_path = self.data_dir / "plugin_settings.json"
        # 按次计费（中转站每次固定扣费）时，用它关掉"能省就省"的额外调用
        self.per_call_mode = False
        # 用户自己装的插件清单（与内置 PLUGIN_MODULES 分开记账，
        # 这样"删除"只可能删到自定义插件，内置的删不掉）
        self._custom_path = self.data_dir / "custom_plugins.json"
        # 自定义插件文件落在这里。**不能放 src/plugins/**：打包成 exe 之后
        # 代码在 exe 内部，磁盘上没有 src/plugins 目录，装进去的东西重启就没了。
        # 放在可写的用户数据目录里，装/删/重启都能保持。
        self.custom_plugins_dir = (Path(custom_plugins_dir) if custom_plugins_dir
                                   else self.data_dir / "custom_plugins")
        self.custom_plugins_dir.mkdir(parents=True, exist_ok=True)
        self._settings: Dict[str, dict] = {}
        self._plugins: Dict[str, PluginBase] = {}
        # 加载期问题（模块 import 失败/实例化失败/post_load 失败/设置被静默改写）：
        # 交给运行记录显示一次，而不是只 print 到日志文件里没人看见。
        self._load_problems: list = []
        # mod 记账：<custom_plugins_dir>/<id>/plugin.json 命中的入口与清单。
        # 只有登记在这里的插件才走「门面 runtime + 钩子超时 + 失败自愈」通道，
        # 内置插件与老的裸 .py 插件一条都不经过（见 _is_mod）。
        self._mod_entries: Dict[str, Path] = {}
        self._mod_manifests: Dict[str, dict] = {}
        self._mod_plugins: Dict[str, str] = {}
        # 增量同步的两本账：每个 mod 上次判过的版本（清单+入口的 mtime_ns/size），
        # 以及上次拒载的原因（同一个坏版本不重复写加载问题与审计）。
        self._mod_stamps: Dict[str, tuple] = {}
        self._mod_refusals: Dict[str, tuple] = {}
        # mod 集合的只读修订号：挂载/摘除/重挂/全量 reload 后 +1（见 mod_revision）。
        self._mod_revision = 0
        self._hook_failures: Dict[str, int] = {}
        # mod 的工具表：<插件 id> -> ModToolHost。只有目录 mod 会进这里。
        self._mod_tool_hosts: Dict[str, "ModToolHost"] = {}
        self._load_settings()
        self._scan_mod_dirs()
        self._load_plugins()
        self._reconcile_settings_with_schemas()

    def custom_plugin_path(self, plugin_id: str) -> Path:
        """自定义插件的文件路径（安装与卸载都用它，保证是同一个地方）。"""
        return self.custom_plugins_dir / f"{plugin_id}.py"

    # ── 自定义插件清单 ────────────────────────────────────────────────────────
    def custom_plugin_ids(self) -> List[str]:
        """已登记的自定义插件标识。清单损坏时返回空列表，不崩。"""
        try:
            raw = json.loads(self._custom_path.read_text("utf-8"))
        except Exception:
            return []
        if isinstance(raw, dict):
            raw = raw.get("plugins")
        if not isinstance(raw, list):
            return []
        return [str(item) for item in raw if isinstance(item, str)]

    def _save_custom(self, ids: List[str]) -> None:
        payload = {"plugins": sorted(set(ids))}
        self._custom_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # ── 目录插件包（<custom_plugins_dir>/<id>/plugin.json）的只读查询 ─────────
    # 目录包不登记进 custom_plugins.json：清单只管裸 .py，目录包以磁盘上的
    # plugin.json 为唯一事实。这里只暴露只读映射，界面/路由不用摸私有字段。
    def custom_mod_ids(self) -> List[str]:
        """当前已挂载的目录插件包 id（按目录名排序）。"""
        return sorted(self._mod_entries)

    def mod_plugin_ids(self, mod_id: str) -> List[str]:
        """一个目录包挂载出来的插件 id 列表（没挂载时为空）。"""
        owner = str(mod_id or "")
        if not owner:
            return []
        return sorted(pid for pid, item in self._mod_plugins.items() if item == owner)

    def mod_id_for_plugin(self, plugin_id: str) -> str:
        """插件 id 属于哪个目录包；裸 .py 或内置插件返回空串。"""
        return str(self._mod_plugins.get(str(plugin_id or ""), ""))

    def custom_mod_path(self, mod_id: str) -> Path:
        """目录包根路径（删除/打开目录都用它）。"""
        return self.custom_plugins_dir / str(mod_id or "")

    def register_custom(self, plugin_id: str) -> None:
        """把插件登记为自定义插件（下次启动也会加载）。"""
        ids = self.custom_plugin_ids()
        if plugin_id not in ids:
            ids.append(plugin_id)
            self._save_custom(ids)

    def unregister_custom(self, plugin_id: str) -> bool:
        """从自定义清单移除；返回是否真的移除了（内置插件返回 False）。"""
        ids = self.custom_plugin_ids()
        if plugin_id not in ids:
            return False
        ids = [item for item in ids if item != plugin_id]
        self._save_custom(ids)
        return True

    def reload(self) -> None:
        """重新读取设置与插件（装/卸插件后调用，界面无需重启）。

        但"无需重启"只覆盖设置与钩子：所有插件实例都会重建、_mod_revision 无条件 +1，
        而**已经建好的工具表不会跟着换**。QQ 侧那张表缓存在 runtime.tool_registry
        （src/tool_loop.py:384-398），要等下一次 qq_app.QQRuntime.tool_registry_fresh()
        看到修订号变了才失效重建（旧 spec 在此之前仍绑着已不在 _plugins 里的旧实例）；
        控制台侧由 ConsoleAgent 每轮自己比 mod_revision 重建。调用方若刚改过插件代码，
        别指望"重载完就能调到新版本"。
        """
        self._settings = {}
        self._plugins = {}
        # mod 目录可能在两次 reload 之间被新增/删除，这里一并重新扫。
        self._mod_entries = {}
        self._mod_manifests = {}
        self._mod_plugins = {}
        self._mod_stamps = {}
        self._mod_refusals = {}
        self._hook_failures = {}
        self._mod_tool_hosts = {}
        self._load_settings()
        self._scan_mod_dirs()
        self._load_plugins()
        self._reconcile_settings_with_schemas()
        # 就算集合看起来没变，所有插件实例也是新建的：旧 spec 绑的是旧实例。
        self._bump_mod_revision()

    def _load_settings(self):
        """读取设置文件；顶层不是 dict 时整体回退为空，不崩溃。"""
        try:
            raw = json.loads(self._settings_path.read_text("utf-8"))
            self._settings = raw if isinstance(raw, dict) else {}
        except Exception:
            self._settings = {}

    # 旧版本里这些插件的默认值是"关闭"，用户如果没动过（存的还是旧默认），
    # 升级后应当跟着新默认一起生效 —— 否则"改了默认也没用"，功能等于没交付。
    LEGACY_DEFAULTS = {
        "token_saver": (
            {"enabled": False, "historyMode": "window", "keepRecentMessages": 14,
             "maxDigestChars": 600, "maxContentChars": 4000, "verbose": False},
            "Token 节流以前默认关闭；这版默认开启（省 token），可在插件中心随时关掉",
        ),
    }

    def _upgrade_legacy_defaults(self) -> None:
        """把"从未改过、仍是旧默认值"的插件升级到新默认。"""
        for pid, (legacy, reason) in self.LEGACY_DEFAULTS.items():
            plugin = self._plugins.get(pid)
            if plugin is None:
                continue
            current = self._settings.get(pid)
            if not isinstance(current, dict):
                continue
            untouched = all(current.get(key) == value for key, value in legacy.items())
            if not untouched:
                continue          # 用户改过，尊重用户的选择
            merged = copy.deepcopy(plugin.META.default_settings or {})
            self._settings[pid] = merged
            self._load_problems.append(f"{pid}：{reason}")
            try:
                self._save_settings()
            except Exception:
                pass

    def _reconcile_settings_with_schemas(self):
        """插件加载后按各自 schema 校正已存值：非法字段回退默认，未知字段忽略。

        校正会**改写内存里的设置**，但以前既不落盘也不提示：界面显示的是校正后的值、
        磁盘上还是旧的，用户下次一保存就被整体覆盖，全程无声无息。
        现在把每一次回退/丢弃记进加载问题，启动时在运行记录里说清楚。
        """
        dirty = False
        for pid, plugin in self._plugins.items():
            meta = plugin.META
            schema = meta.config_schema or {}
            defaults = meta.default_settings or {}
            current = self._settings.get(pid)
            if not isinstance(current, dict):
                current = {}
            merged = copy.deepcopy(defaults)
            for key, value in current.items():
                if key not in schema:
                    if key in CONTROL_SETTING_FIELDS:
                        # 控制字段不归 schema 管，但取值仍要合法；若在这里被丢掉，
                        # 下次加载插件就会悄悄退回未启用，所以必须原样留下。
                        try:
                            merged[key] = _validate_control_field(key, value, pid)
                        except ValueError as exc:
                            self._load_problems.append(
                                f"{pid}.{key}={value!r} 不合理（{exc}），已忽略")
                            dirty = True
                        continue
                    self._load_problems.append(
                        f"{pid}.{key} 不是有效设置项，已忽略（磁盘上的旧值会在下次保存时移除）")
                    dirty = True
                    continue
                try:
                    validated = _validate_field(schema[key], value, key, pid)
                except Exception as exc:
                    merged[key] = _fallback_field_default(schema[key], defaults, key)
                    self._load_problems.append(
                        f"{pid}.{key}={value!r} 不合理（{exc}），已回退为 {merged[key]!r}")
                    dirty = True
                    continue
                if validated != value:
                    self._load_problems.append(
                        f"{pid}.{key}={value!r} 已按规则调整为 {validated!r}")
                    dirty = True
                merged[key] = validated
            self._settings[pid] = merged
        try:
            self._upgrade_legacy_defaults()
        except Exception as exc:
            self._load_problems.append(f"旧默认值升级跳过：{type(exc).__name__}: {exc}")
        if dirty:
            # 顺手落盘一次，让"界面看到的"和"磁盘上的"重新一致
            try:
                self._save_settings()
            except Exception as exc:
                self._load_problems.append(f"校正后的设置写盘失败：{exc}")

    def _save_settings(self):
        """原子写盘：先写临时文件再替换，失败时不留半截文件。"""
        tmp = self._settings_path.with_suffix(".json.tmp")
        payload = json.dumps(self._settings, ensure_ascii=False, indent=2)
        tmp.write_text(payload, "utf-8")
        tmp.replace(self._settings_path)

    @staticmethod
    def _run_isolated(fn, timeout=PLUGIN_LOAD_TIMEOUT):
        """在守护线程里跑插件导入/实例化；超时或 BaseException 都只跳过该插件。"""
        result = queue.Queue()

        def worker():
            try:
                result.put(('ok', fn()))
            except BaseException as exc:  # 插件可能 sys.exit/SystemExit，不能带走管理器
                result.put(('err', f'{type(exc).__name__}: {exc}'))

        thread = threading.Thread(target=worker, daemon=True,
                                  name='plugin-load')
        thread.start()
        thread.join(max(0.1, float(timeout)))
        if thread.is_alive():
            return None, f'加载超过 {timeout:g} 秒（已跳过，插件线程不会阻塞退出）'
        try:
            kind, value = result.get_nowait()
        except queue.Empty:
            return None, '插件加载无返回（已跳过）'
        if kind == 'err':
            return None, str(value)
        return value, ''

    # ── mod 目录挂载 / 运行时围栏 / 坏插件自愈 ──────────────────────────────

    @property
    def plugin_audit_path(self) -> Path:
        """审计 JSONL 的落点：插件库同级的 plugin_audit.jsonl。

        data_dir 通常已经是 <data>/qq-only（QQRuntime 的插件库），再补一层会落到
        <data>/qq-only/qq-only；只有从 <data> 建 manager 的调用方才需要补 qq-only。
        """
        base = self.data_dir
        if base.name.lower() != "qq-only":
            base = base / "qq-only"
        return base / "plugin_audit.jsonl"

    def _write_audit(self, event: str, plugin: str = '', hook: str = '',
                     code: str = '', detail: str = '', **extra) -> None:
        """追加一行审计。写盘失败只告警：审计绝不允许影响插件运行。

        工具调用行要带 tool/write/ok/duration_ms（见 ModToolHost._write_audit），
        走 extra 合并进去；其余事件的行走的还是原来的六个字段，一个字节都没变。
        """
        record = {
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "plugin": plugin, "event": event, "hook": hook,
            "code": code, "detail": detail,
        }
        for key, value in extra.items():
            record.setdefault(key, value)
        try:
            path = self.plugin_audit_path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            print(f"[plugins] 审计写入失败（已忽略）：{type(exc).__name__}: {exc}")

    def _scan_mod_dirs(self) -> None:
        """扫 custom_plugins_dir：带 plugin.json 的子目录就是一个 mod，自动挂载。

        不需要 register_custom，也不需要主体改任何代码 —— 挂载点就是目录本身。
        裸 <id>.py 不走这里（旧通道原样保留）。

        依赖解析与拓扑排序都在这里落定：_mod_entries 的迭代顺序就是挂载顺序，
        _load_plugins 按它排列模块，所以 post_load 的调用顺序就是合法依赖序
        （被依赖者先挂）。被环牵连或缺依赖的 mod 一个都不进 _mod_entries。
        """
        self._mod_entries = {}
        self._mod_manifests = {}
        self._mod_stamps = {}
        self._mod_refusals = {}
        found, refused = self._read_mod_dirs()
        for mod_id, (code, detail) in refused.items():
            self._refuse_mod(mod_id, code, detail)
            self._mod_refusals[mod_id] = (code, detail)
        ordered, plan_refusals = self._plan_mods(found, refused=refused)
        for mod_id, (code, detail) in plan_refusals.items():
            self._refuse_mod(mod_id, code, detail)
            self._mod_refusals[mod_id] = (code, detail)
        for mod_id in ordered:
            entry_path, manifest, stamp = found[mod_id]
            self._mod_entries[mod_id] = entry_path
            self._mod_manifests[mod_id] = manifest
            self._mod_stamps[mod_id] = stamp

    def _read_mod_dirs(self):
        """读 custom_plugins_dir，返回 (可用候选, 拒载原因)；两本账都不在这里写。

        found[mod_id] = (entry_path, manifest, stamp)，stamp 是清单与入口文件的
        (mtime_ns, size)，增量同步靠它判"这个 mod 变过没有"。
        全量扫描每次都记拒载，增量同步只对换了版本的记一次，所以校验与记账分开。
        """
        found: Dict[str, tuple] = {}
        refused: Dict[str, tuple] = {}
        try:
            candidates = sorted(item for item in self.custom_plugins_dir.iterdir()
                                if item.is_dir())
        except OSError as exc:
            self._load_problems.append(f"自定义插件目录读取失败（mod 未挂载）：{exc}")
            return found, refused
        for mod_dir in candidates:
            manifest_path = mod_dir / MOD_MANIFEST_NAME
            if not manifest_path.is_file():
                continue            # 普通子目录（解压残留等），不是 mod
            try:
                entry_path, manifest = self._validate_mod_manifest(mod_dir, manifest_path)
            except ModManifestError as exc:
                refused[mod_dir.name] = (exc.code, str(exc))
                continue
            mod_id = manifest["id"]
            if mod_id in PLUGIN_MODULES:
                refused[mod_id] = ('builtin_conflict',
                                   '与内置插件同名：内置版本优先，该目录 mod 未挂载')
                continue
            if (self.custom_plugins_dir / f"{mod_id}.py").is_file():
                refused[mod_id] = ('file_conflict',
                                   f'同名裸插件 {mod_id}.py 存在，按旧通道加载，目录 mod 未挂载')
                continue
            found[mod_id] = (entry_path, manifest,
                             (_file_stamp(manifest_path), _file_stamp(entry_path)))
        return found, refused

    @staticmethod
    def _missing_dep_detail(dep: str, reason: str = '') -> str:
        """"依赖没挂上"的统一措辞：拓扑排序与摘除联动共用，同一个版本就不会反复记账。"""
        if reason:
            return f'[dependency-missing] 依赖 {dep} 未挂载（{reason}）'
        return (f'[dependency-missing] 依赖 {dep} 未挂载：'
                '自定义插件目录里没有它，也不是内置插件 id')

    def _plan_mods(self, found, available=(), refused=None):
        """依赖解析 + 拓扑排序，返回 (按挂载顺序的 id 列表, {id: (code, detail)})。

        found 是 _read_mod_dirs() 的候选表；available 是"已经挂着的 mod id"
        （增量同步时用；全量扫描传空，此时依赖只能由内置插件或同批候选满足）。
        依赖可指向另一个目录 mod，也可以指向内置插件 id —— 内置模块在 _load_plugins
        里排在目录 mod 之前，所以"内置插件已装载"在挂载那一刻成立。
        被环牵连的、依赖落不了地的，一个都不进返回列表：不留半挂载。
        """
        builtin = set(PLUGIN_MODULES)
        mounted = set(available)
        already_refused = dict(refused or {})
        ids = list(found)          # 目录序（_read_mod_dirs 已 sorted），挑边与挑点都稳定
        edges = {mod_id: [dep for dep in (found[mod_id][1].get('depends') or [])
                          if dep in found and dep != mod_id]
                 for mod_id in ids}
        plan_refusals: Dict[str, tuple] = {}
        alive = set(ids)
        while alive:
            cycle = _find_dependency_cycle({mod_id: edges[mod_id] for mod_id in ids
                                           if mod_id in alive})
            if cycle is None:
                break
            detail = '[dependency-cycle] 依赖成环：' + ' -> '.join(cycle)
            for mod_id in cycle[:-1]:
                plan_refusals[mod_id] = ('dependency-cycle', detail)
                alive.discard(mod_id)
        # Kahn：一遍遍找"依赖全部就位"的，先无依赖的、后被依赖者
        ordered: List[str] = []
        blocked = [mod_id for mod_id in ids if mod_id in alive]
        progress = True
        while blocked and progress:
            progress = False
            for mod_id in list(blocked):
                depends = found[mod_id][1].get('depends') or []
                if all(dep in builtin or dep in mounted or dep in ordered
                       for dep in depends):
                    ordered.append(mod_id)
                    blocked.remove(mod_id)
                    progress = True
        for mod_id in blocked:
            depends = found[mod_id][1].get('depends') or []
            missing = [dep for dep in depends
                       if not (dep in builtin or dep in mounted or dep in ordered)]
            dep = missing[0]
            if dep in plan_refusals:
                detail = self._missing_dep_detail(dep, plan_refusals[dep][0])
            elif dep in found:
                detail = self._missing_dep_detail(dep, '同批里它自己也没挂上')
            elif dep in already_refused:
                detail = self._missing_dep_detail(dep, f'该目录被拒载：{already_refused[dep][0]}')
            else:
                detail = self._missing_dep_detail(dep)
            plan_refusals[mod_id] = ('dependency-missing', detail)
        return ordered, plan_refusals

    def _refuse_mod(self, mod_id: str, code: str, detail: str) -> None:
        """拒载一个 mod：写加载问题 + 审计；其它插件照常加载、主体照常启动。"""
        self._load_problems.append(f"插件 mod {mod_id} 拒载：{detail}")
        self._write_audit('load-refused', plugin=mod_id, code=code, detail=detail)

    def _validate_mod_manifest(self, mod_dir: Path, manifest_path: Path):
        """校验 plugin.json；不合格抛 ModManifestError（带 code，便于审计归类）。"""
        try:
            raw = json.loads(manifest_path.read_text("utf-8"))
        except Exception as exc:
            raise ModManifestError('bad_json', f'{MOD_MANIFEST_NAME} 不是合法 JSON：{exc}')
        if not isinstance(raw, dict):
            raise ModManifestError('bad_manifest', f'{MOD_MANIFEST_NAME} 顶层必须是对象')
        mod_id = raw.get("id")
        if not isinstance(mod_id, str) or not mod_id.strip():
            raise ModManifestError('missing_id', '缺少必填字段 id')
        if mod_id != mod_dir.name:
            raise ModManifestError(
                'id_mismatch', f'id {mod_id!r} 与目录名 {mod_dir.name!r} 不一致')
        api_version = raw.get("api_version")
        if isinstance(api_version, bool) or not isinstance(api_version, int):
            raise ModManifestError('bad_api_version',
                                   f'api_version 必须是整数，当前只支持 {MOD_API_VERSION}')
        if api_version != MOD_API_VERSION:
            raise ModManifestError(
                'api_version_unsupported',
                f'api_version={api_version} 不受支持，当前只支持 {MOD_API_VERSION}')
        entry = raw.get("entry")
        if not isinstance(entry, str) or not entry.strip():
            raise ModManifestError('missing_entry', '缺少必填字段 entry')
        root = mod_dir.resolve()
        entry_path = (root / entry).resolve()
        if root not in entry_path.parents:
            raise ModManifestError(
                'entry_escape', f'entry {entry!r} 越出 mod 目录（不许 .. 或绝对路径）')
        if not entry_path.is_file():
            raise ModManifestError('entry_missing', f'entry {entry!r} 指向的文件不存在')
        permissions = raw.get("permissions") or []
        if not isinstance(permissions, list) or any(not isinstance(p, str) for p in permissions):
            raise ModManifestError('bad_permissions', 'permissions 必须是字符串列表')
        unknown = sorted(set(permissions) - set(MOD_PERMISSIONS))
        if unknown:
            raise ModManifestError(
                'bad_permissions',
                f'permissions 含未知取值 {unknown}，可选 {sorted(MOD_PERMISSIONS)}')
        for key in ("name", "description", "version"):
            if key in raw and not isinstance(raw[key], str):
                raise ModManifestError('bad_field', f'{key} 必须是字符串')
        if "enabled_by_default" in raw and not isinstance(raw["enabled_by_default"], bool):
            raise ModManifestError('bad_field', 'enabled_by_default 必须是布尔值')
        # depends：可选，mod id 列表。老 mod 不写就是空列表，行为与加载顺序都不变。
        depends = raw.get("depends", [])
        if not isinstance(depends, list):
            raise ModManifestError(
                'bad_depends',
                '[bad_depends] depends 必须是 mod id 列表（字符串数组），'
                f'收到 {type(depends).__name__}')
        normalized: List[str] = []
        for item in depends:
            if not isinstance(item, str) or not item.strip():
                raise ModManifestError(
                    'bad_depends',
                    '[bad_depends] depends 里的每一项都必须是非空字符串 mod id，'
                    f'收到 {item!r}')
            if item not in normalized:
                normalized.append(item)
        if mod_id in normalized:
            raise ModManifestError(
                'dependency-self',
                f'[dependency-self] depends 里出现了自己（{mod_id}）：mod 不能依赖自身')
        raw["depends"] = normalized
        return entry_path, raw

    def _is_mod(self, plugin) -> bool:
        """这个插件实例是不是从目录 mod 加载的（决定走门面还是拿活的 runtime）。

        注意：_mod_plugins 可能不存在 —— 既有测试会用 PluginManager.__new__
        绕过 __init__ 直接塞 _plugins，这里抛任何异常都会断掉整条钩子链。
        """
        pid = getattr(getattr(plugin, 'META', None), 'id', '')
        return pid in (getattr(self, '_mod_plugins', None) or {})

    def _note_mod_plugin(self, plugin, mod_id: str) -> None:
        """登记 mod 里加载出来的插件 id，并让 manifest 的 enabled_by_default 生效。"""
        pid = getattr(getattr(plugin, 'META', None), 'id', '')
        if not pid:
            return
        self._mod_plugins[pid] = mod_id
        manifest = self._mod_manifests.get(mod_id) or {}
        if manifest.get('enabled_by_default') is False:
            current = self._settings.get(pid)
            merged = dict(current) if isinstance(current, dict) else {}
            merged.setdefault('enabled', False)
            self._settings[pid] = merged
        # 挂上（含重挂后重新挂上）一个 mod 插件实例：工具表能看到的集合变了。
        self._bump_mod_revision()

    def _audit_mounted(self, mod_id: str) -> None:
        manifest = self._mod_manifests.get(mod_id) or {}
        entry = self._mod_entries.get(mod_id)
        owned = [pid for pid, owner in self._mod_plugins.items() if owner == mod_id]
        self._write_audit(
            'mounted', plugin=mod_id, code='ok',
            detail=(f"plugins={','.join(owned) or '-'} "
                    f"entry={entry.name if entry else '-'} "
                    f"version={manifest.get('version') or '-'} "
                    f"permissions={','.join(manifest.get('permissions') or []) or '-'}"))

    # ── mod 集合的只读修订号 ────────────────────────────────────────────────

    @property
    def mod_revision(self) -> int:
        """按"哪些 mod 插件实例正挂着"计数的只读修订号；只增不减，从 0 开始。

        谁需要它：控制台 Agent 的工具表（src/console_agent.py:631 的 _tool_turn）。
        工具表是按 _plugins 建的快照（src/tool_loop.py:354 register_plugin_tools），
        mod 一挂一摘，旧表里留下的 spec 就指向已经不存在的插件实例，表本身却毫无
        变化迹象。建表一方把当时的修订号记下来，之后每次只比这个数，就不必每轮
        重建表，也不必靠轮询 mtime 猜。

        只增的时机（都是"工具表会看到的插件集合真的变了"）：_note_mod_plugin
        （挂上 mod 插件实例，含重挂）与 _unmount_mod（摘掉至少一个 mod 插件实例），
        外加 reload()（全量重建所有插件实例，即使集合看起来没变，handler 也是新对象）。

        __new__ 绕过 __init__ 的 manager 根本没有这个字段（既有测试就这么造 manager）：
        取不到就当 0，读这个属性永远不抛。
        """
        try:
            return int(getattr(self, '_mod_revision', 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _bump_mod_revision(self) -> int:
        """修订号 +1，并把新值返回（内部用；外部只读 mod_revision）。"""
        self._mod_revision = self.mod_revision + 1
        return self._mod_revision

    # ── 目录 mod 的增量同步（新增/摘除/重挂）────────────────────────────────

    def _ensure_mod_state(self) -> None:
        """补齐 mod 记账字典：__new__ 绕过 __init__ 的旧路径也要能跑（与 _is_mod 同口径）。"""
        for name in ('_mod_entries', '_mod_manifests', '_mod_plugins',
                     '_mod_tool_hosts', '_mod_stamps', '_mod_refusals'):
            if getattr(self, name, None) is None:
                setattr(self, name, {})

    def _unmount_mod(self, mod_id: str) -> list:
        """摘掉一个目录 mod 的全部运行时痕迹，返回被摘掉的插件 id。

        实例从 _plugins 里拿掉后，钩子链自然就不再跑它；但**工具表不会随实例消失**：
        建好的表缓存在 runtime.tool_registry 上（src/tool_loop.py:384-398），而
        register_plugin_tools 只往表里追加、从不注销（src/tool_loop.py:354-379），所以
        旧 spec 依旧 available=True，还能调到那个已经不在 _plugins 里的旧实例。
        这里仍然不去别处注销工具（插件层不该伸手进宿主的缓存），失效靠修订号：本函数会
        bump 一次，QQ 侧下一次 qq_app.QQRuntime.tool_registry_fresh()、控制台侧下一轮
        比号时各建各的新表。设置键**不删**：与既有摘除语义一致，用户重装后配置还在。
        """
        owners = getattr(self, '_mod_plugins', None)
        if not isinstance(owners, dict):
            owners = self._mod_plugins = {}
        failures = getattr(self, '_hook_failures', None)
        removed = [pid for pid, owner in owners.items() if owner == mod_id]
        for pid in removed:
            owners.pop(pid, None)
            self._plugins.pop(pid, None)
            if isinstance(failures, dict):
                failures.pop(pid, None)      # 换了版本，连续失败计数从零开始
        hosts = getattr(self, '_mod_tool_hosts', None)
        if not isinstance(hosts, dict):
            hosts = self._mod_tool_hosts = {}
        for pid in [pid for pid, host in hosts.items()
                    if getattr(host, '_mod_id', '') == mod_id]:
            hosts.pop(pid, None)
        if removed:
            # 真的有实例被摘掉（不是"本来就没挂"）：按 _plugins 建的工具表过期了。
            self._bump_mod_revision()
        return removed

    def _drop_mod(self, mod_id: str, code: str, report: dict, bucket: str,
                  detail: str = '') -> list:
        """摘掉一个 mod 并清账（设置键不动），写一条 mod-unmounted 审计。"""
        removed = self._unmount_mod(mod_id)
        self._mod_entries.pop(mod_id, None)
        self._mod_manifests.pop(mod_id, None)
        self._mod_stamps.pop(mod_id, None)
        report.setdefault(bucket, []).append(mod_id)
        self._write_audit('mod-unmounted', plugin=mod_id, code=code,
                          detail=detail or '目录或清单已变化，该 mod 已摘除',
                          plugins=','.join(removed) or '-')
        return removed

    def _cascade_dependents(self, lost, report) -> None:
        """把"依赖刚才掉下去了"的已挂 mod 一起摘掉：依赖关系不留半截。

        只在增量同步里用：全量 reload 会重建所有 mod，没有"一半在、一半不在"的窗口。
        """
        queue = list(dict.fromkeys(lost))
        seen = set()
        while queue:
            gone = queue.pop(0)
            if gone in seen:
                continue
            seen.add(gone)
            for mod_id in list(self._mod_entries):
                if mod_id in seen:
                    continue
                manifest = self._mod_manifests.get(mod_id) or {}
                if gone not in (manifest.get('depends') or []):
                    continue
                coded = self._missing_dep_detail(
                    gone, '它已摘除或拒载，依赖它的这个 mod 一并摘除')
                self._drop_mod(mod_id, 'dependency-lost', report, 'removed',
                               f'依赖 {gone} 已摘除或拒载')
                self._mod_refusals[mod_id] = ('dependency-missing', coded)
                self._load_problems.append(f"插件 mod {mod_id} 摘除：{coded}")
                queue.append(mod_id)
                seen.add(mod_id)

    def _mount_mod(self, mod_id: str) -> bool:
        """挂载单个目录 mod，返回是否真的挂上了至少一个插件实例。

        与全量加载共用 _mount_module（导入超时、post_load 失败、工具表同一套），
        区别只是一个模块失败不牵连同批其它 mod。
        """
        if mod_id not in (getattr(self, '_mod_entries', None) or {}):
            return False
        return self._mount_module(mod_id)

    def sync_mods(self) -> dict:
        """目录 mod 的增量同步：只扫 custom_plugins_dir，按需挂载/摘除/重挂。

        变更判据是清单与入口文件的 (mtime_ns, size)：没动过的 mod 一律不重挂，
        它的 post_load 因此不会被再次调用；连续两次调用第二次是零动作。
        单个 mod 的任何异常都只记问题，不打断整轮同步（与 _run_isolated 同口径）。
        返回 {"added","removed","reloaded","unchanged","rejected","failed","actions"}。
        """
        self._ensure_mod_state()
        report = {"added": [], "removed": [], "reloaded": [], "unchanged": [],
                  "rejected": [], "failed": [], "actions": 0}
        found, refused_now = self._read_mod_dirs()
        present = set(found) | set(refused_now)
        lost = set()
        # 1. 目录/清单没了：摘除（设置键保留）
        tracked = list(dict.fromkeys(list(self._mod_stamps) + list(self._mod_entries)))
        for mod_id in tracked:
            if mod_id in present:
                continue
            self._drop_mod(mod_id, 'dir-gone', report, 'removed',
                           '目录或 plugin.json 已消失')
            self._mod_refusals.pop(mod_id, None)
            lost.add(mod_id)
        # 2. 这一轮校验就不过的：同一个坏版本只记一次，换了个错再记
        for mod_id, (code, detail) in refused_now.items():
            if self._mod_refusals.get(mod_id) == (code, detail):
                report["unchanged"].append(mod_id)
                continue
            if mod_id in self._mod_entries:
                self._drop_mod(mod_id, code, report, 'rejected', detail)
            else:
                report["rejected"].append(mod_id)
            self._refuse_mod(mod_id, code, detail)
            self._mod_refusals[mod_id] = (code, detail)
            self._mod_stamps.pop(mod_id, None)
            lost.add(mod_id)
        # 3. 版本变了或新出现的：先算依赖顺序，再逐个挂
        work = []
        for mod_id, (_entry, _manifest, stamp) in found.items():
            previous = self._mod_refusals.get(mod_id)
            if self._mod_stamps.get(mod_id) == stamp and not (
                    previous and str(previous[0]).startswith('dependency-')):
                report["unchanged"].append(mod_id)
                continue
            work.append(mod_id)
        batch = {mod_id: found[mod_id] for mod_id in work}
        mounted_mods = set((getattr(self, '_mod_plugins', None) or {}).values())
        ordered, plan_refusals = self._plan_mods(
            batch, available=mounted_mods - set(batch), refused=refused_now)
        def reject(mod_id, code, detail):
            """拒载记账：同一个坏版本只记一次，换了个错再记（幂等的另一半）。"""
            previous = self._mod_refusals.get(mod_id)
            if mod_id not in self._mod_entries and previous == (code, detail):
                report["unchanged"].append(mod_id)
            else:
                if mod_id in self._mod_entries:
                    self._drop_mod(mod_id, code, report, 'rejected', detail)
                else:
                    report["rejected"].append(mod_id)
                self._refuse_mod(mod_id, code, detail)
            self._mod_refusals[mod_id] = (code, detail)
            self._mod_stamps[mod_id] = found[mod_id][2]
            lost.add(mod_id)

        def mounted_now():
            """这一刻真的挂着的 mod（含本轮刚挂上的），依赖判定按它算。"""
            return set((getattr(self, '_mod_plugins', None) or {}).values())

        for mod_id, (code, detail) in plan_refusals.items():
            reject(mod_id, code, detail)
        for mod_id in ordered:
            entry_path, manifest, stamp = found[mod_id]
            # 依赖在排序时看着齐，挂到一半可能已经塌了（比如前者挂载失败）：
            # 挂之前再核一次，缺了就不挂，绝不留"依赖没到位却跑着"的 mod。
            unmet = [dep for dep in (manifest.get('depends') or [])
                     if dep not in PLUGIN_MODULES and dep not in mounted_now()]
            if unmet:
                reject(mod_id, 'dependency-missing',
                       self._missing_dep_detail(unmet[0], '它这一次没挂上'))
                continue
            was_mounted = mod_id in self._mod_entries
            if was_mounted:
                self._unmount_mod(mod_id)
                self._write_audit('mod-unmounted', plugin=mod_id, code='file-changed',
                                  detail='清单或入口文件变了：先摘后挂')
            self._mod_entries[mod_id] = entry_path
            self._mod_manifests[mod_id] = manifest
            self._mod_stamps[mod_id] = stamp
            self._mod_refusals.pop(mod_id, None)
            try:
                ok = self._mount_mod(mod_id)
            except Exception as exc:      # 单个 mod 的任何异常都不许打断整轮同步
                ok = False
                self._load_problems.append(
                    f"插件 mod {mod_id} 同步挂载异常（已跳过）：{type(exc).__name__}: {exc}")
            if ok:
                report["reloaded" if was_mounted else "added"].append(mod_id)
            else:
                # 挂不上就不留半截记账：条目摘掉；stamp 留着，同一个坏文件不在每轮重试。
                self._unmount_mod(mod_id)
                self._mod_entries.pop(mod_id, None)
                self._mod_manifests.pop(mod_id, None)
                report["failed"].append(mod_id)
                lost.add(mod_id)
        # 4. 依赖掉下去的已挂 mod 一并摘除，不留半截依赖关系
        self._cascade_dependents(lost, report)
        report["actions"] = (len(report["added"]) + len(report["removed"])
                             + len(report["reloaded"]))
        if any(report[key] for key in
               ('added', 'removed', 'reloaded', 'rejected', 'failed')):
            self._write_audit(
                'mod-sync',
                code='partial' if (report['rejected'] or report['failed']) else 'ok',
                detail=(f"added={','.join(report['added']) or '-'} "
                        f"reloaded={','.join(report['reloaded']) or '-'} "
                        f"removed={','.join(report['removed']) or '-'} "
                        f"rejected={','.join(report['rejected']) or '-'} "
                        f"failed={','.join(report['failed']) or '-'}"))
        return report

    def _hook_runtime(self, plugin, runtime):
        """mod 拿只读门面；内置插件继续拿原来那个**活的** QQRuntime（这条有测试钉住）。"""
        if not self._is_mod(plugin):
            return runtime
        pid = plugin.META.id
        manifest = self._mod_manifests.get(self._mod_plugins.get(pid, '')) or {}
        # 工具宿主可能不在（__new__ 绕过 __init__ 的旧测试，或挂载失败时）：
        # 门面照旧给，只是这时候 register_tool 会明确报错而不是静默。
        hosts = getattr(self, '_mod_tool_hosts', None) or {}
        return ModRuntime(pid, lambda: plugin.settings,
                          note=getattr(runtime, 'note', None),
                          permissions=manifest.get("permissions") or (),
                          tools=hosts.get(pid))

    def _attach_mod_tools(self, plugin, mod_id: str) -> None:
        """给目录 mod 的插件实例挂上工具表：list_tools/call_tool 就是工具环认的两个方法。

        必须在 post_load() 之前挂：mod 就是在 post_load 里声明自己的工具。
        内置插件与裸 .py 插件一条都不经过这里，工具环对它们的行为完全不变。
        """
        pid = str(getattr(getattr(plugin, 'META', None), 'id', '') or mod_id)
        host = ModToolHost(self, plugin, mod_id)
        self._mod_tool_hosts[pid] = host
        try:
            plugin.list_tools = host.list_tools
            plugin.call_tool = host.call_tool
            plugin.register_tool = host.register_tool
        except Exception as exc:
            self._load_problems.append(
                f"插件 mod {mod_id} 的工具表挂载失败（该 mod 不能提供工具）：{exc}")

    @staticmethod
    def _run_guarded(fn, timeout):
        """在守护线程里跑一个钩子；返回 (value, error, timed_out)。

        跟 _run_isolated 同一套做法：超时只是**不再等**那个线程，不用 join 死等，
        也不阻塞调用方（同步钩子是插件自己要在里面 sleep 的，那是它的问题）。
        """
        result = queue.Queue()

        def worker():
            try:
                result.put(('ok', fn()))
            except BaseException as exc:  # mod 里 sys.exit() 也不能带走主体
                result.put(('err', f'{type(exc).__name__}: {exc}'))

        thread = threading.Thread(target=worker, daemon=True, name='plugin-mod-hook')
        thread.start()
        thread.join(max(0.05, float(timeout)))
        if thread.is_alive():
            return None, f'钩子超过 {timeout:g} 秒未返回（已跳过）', True
        try:
            kind, value = result.get_nowait()
        except queue.Empty:
            return None, '钩子无返回（已跳过）', True
        if kind == 'err':
            return None, str(value), False
        return value, '', False

    def _record_mod_success(self, plugin) -> None:
        """成功调用一次即清零连续失败计数。"""
        pid = getattr(getattr(plugin, 'META', None), 'id', '')
        if pid in self._hook_failures:
            self._hook_failures.pop(pid, None)

    def _record_mod_failure(self, plugin, hook: str, code: str, detail: str) -> None:
        """记一次 mod 钩子失败：审计 + 计数，攒满 MOD_FAILURE_LIMIT 就自动禁用。"""
        pid = getattr(getattr(plugin, 'META', None), 'id', '') or type(plugin).__name__
        self._write_audit('hook-timeout' if code == 'timeout' else 'hook-failed',
                          plugin=pid, hook=hook, code=code, detail=detail)
        count = self._hook_failures.get(pid, 0) + 1
        self._hook_failures[pid] = count
        if count >= MOD_FAILURE_LIMIT:
            self._auto_disable_mod(plugin, pid, hook, detail)

    def _auto_disable_mod(self, plugin, pid: str, hook: str, detail: str) -> None:
        """连续失败到上限：把 enabled 写 false 并落盘，其它插件完全不受影响。"""
        current = self._settings.get(pid)
        merged = dict(current) if isinstance(current, dict) else {}
        merged['enabled'] = False
        self._settings[pid] = merged
        self._hook_failures.pop(pid, None)
        note = (f"插件 {pid} 连续失败 {MOD_FAILURE_LIMIT} 次（{hook}：{detail}），"
                "已自动禁用；可在插件设置里重新开启")
        try:
            self._save_settings()
        except Exception as exc:
            note += f"；设置落盘失败：{exc}"
        self._load_problems.append(note)
        self._write_audit('auto-disabled', plugin=pid, hook=hook,
                          code='auto-disable', detail=detail)

    def _call_mod_hook(self, plugin, hook: str, fn, runtime, *args):
        """调用**同步**钩子：mod 走门面 + 超时 + 失败累计；内置插件原样调用。

        内置插件的路径保持完全不变（同一个 runtime、同样的异常直接冒给调用方），
        所以这里只有 mod 才多出来一层围栏。
        """
        if not self._is_mod(plugin):
            return fn(*args, runtime)
        facade = self._hook_runtime(plugin, runtime)
        value, error, timed_out = self._run_guarded(
            lambda: fn(*args, facade), MOD_HOOK_TIMEOUT)
        if timed_out or error:
            self._record_mod_failure(
                plugin, hook, 'timeout' if timed_out else 'exception', error)
            raise _ModHookError(error)
        self._record_mod_success(plugin)
        return value

    async def _call_mod_hook_async(self, plugin, hook: str, fn, runtime, *args):
        """调用**异步**钩子：mod 用 asyncio.wait_for 限时（不会卡住事件循环）。"""
        if not self._is_mod(plugin):
            return await fn(*args, runtime)
        facade = self._hook_runtime(plugin, runtime)
        try:
            value = await asyncio.wait_for(fn(*args, facade), MOD_HOOK_TIMEOUT)
        except (asyncio.TimeoutError, TimeoutError):
            detail = f'钩子超过 {MOD_HOOK_TIMEOUT:g} 秒未返回（已跳过）'
            self._record_mod_failure(plugin, hook, 'timeout', detail)
            raise _ModHookError(detail)
        except Exception as exc:
            detail = f'{type(exc).__name__}: {exc}'
            self._record_mod_failure(plugin, hook, 'exception', detail)
            raise _ModHookError(detail)
        self._record_mod_success(plugin)
        return value

    async def _await_hook_value(self, plugin, hook: str, value, runtime):
        """钩子可能返回 awaitable（工具钩子）：mod 的 await 也必须限时。"""
        if not inspect.isawaitable(value):
            return value
        if not self._is_mod(plugin):
            return await value
        try:
            return await asyncio.wait_for(value, MOD_HOOK_TIMEOUT)
        except (asyncio.TimeoutError, TimeoutError):
            detail = f'钩子超过 {MOD_HOOK_TIMEOUT:g} 秒未返回（已跳过）'
            self._record_mod_failure(plugin, hook, 'timeout', detail)
            raise _ModHookError(detail)
        except Exception as exc:
            detail = f'{type(exc).__name__}: {exc}'
            self._record_mod_failure(plugin, hook, 'exception', detail)
            raise _ModHookError(detail)

    async def _call_hook_maybe_async(self, plugin, hook: str, fn, runtime, *args):
        """同步/异步都可能的钩子（工具钩子、on_pre_ai_async）。"""
        value = self._call_mod_hook(plugin, hook, fn, runtime, *args)
        return await self._await_hook_value(plugin, hook, value, runtime)

    def _strip_mod_on_ready(self, plugin, hint):
        """mod 不许用 on_ready 通道（主体代执行代码）：剥掉并留一条审计。

        内置插件的 on_ready 原样通过 —— 那是 image_understanding 等插件在用的既有能力。
        """
        if not self._is_mod(plugin) or not isinstance(hint, dict):
            return hint
        if not callable(hint.get('on_ready')):
            return hint
        pid = plugin.META.id
        self._write_audit(
            'on-ready-stripped', plugin=pid, hook='on_pre_ai', code='on_ready',
            detail='mod 不允许通过 on_ready 让主体代执行代码，已剥掉该回调')
        stripped = dict(hint)
        stripped.pop('on_ready', None)
        return stripped

    def _import_plugin_module(self, mod_name: str):
        """加载一个插件模块。

        自定义插件文件存在时，**优先加载自定义版本**；没有自定义文件才导入
        包内同名的内置插件。这样安装同名兼容/升级包时不会被内置插件挡住，
        同时保留没有自定义文件时走内置版本的行为。
        自定义文件存在但加载失败时返回 None，不静默回退内置版本，否则安装器
        会误判为成功、用户装的新版本其实没有生效。
        """
        path = self.custom_plugins_dir / f"{mod_name}.py"
        if path.is_file():
            return self._load_custom_module(mod_name, path)
        # 目录 mod：入口文件由 plugin.json 的 entry 指定，加载方式与裸 .py 完全一致
        # （同一个 _load_custom_module、同一个包上下文），只是路径来自清单。
        mod_entry = self._mod_entries.get(mod_name)
        if mod_entry is not None:
            return self._load_custom_module(mod_name, mod_entry)

        try:
            return importlib.import_module(f".{mod_name}", package="src.plugins")
        except Exception as exc:
            self._last_import_error = f'{type(exc).__name__}: {exc}'
        return None

    def _load_custom_module(self, mod_name: str, path: Path):
        """按文件路径加载自定义插件；设置包上下文以支持相对导入。"""
        # 唯一的模块名，避免与包内模块重名互相污染
        alias = f"src_custom_plugin_{mod_name}"
        try:
            spec = importlib.util.spec_from_file_location(alias, path)
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            # 关键：项目里的插件习惯用**相对导入**（`from . import PluginBase`）。
            # 按文件路径加载的模块默认是顶层模块，没有包上下文，
            # 相对导入会直接 ImportError（实测：
            # "attempted relative import with no known parent package"）。
            # 把 __package__ 指到 src.plugins，相对导入才能解析。
            module.__package__ = "src.plugins"
            sys.modules[alias] = module
            # 同一秒内覆盖相同长度的源码，SourceFileLoader 可能命中旧 pyc。
            # 清掉该文件对应的缓存，让新版源码重新编译，避免“装上了但运行旧代码”。
            try:
                cache_path = importlib.util.cache_from_source(str(path))
                Path(cache_path).unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
            spec.loader.exec_module(module)
            return module
        except Exception as exc:
            self._last_import_error = f'{type(exc).__name__}: {exc}'
            print(f"[plugins] Failed to load custom {mod_name} from {path}: {exc}")
            sys.modules.pop(alias, None)
            return None

    def _load_plugins(self):
        # 内置模块 + 用户自己装的（自定义清单）；自定义排在后面，
        # 万一标识撞了也不会顶掉内置插件的行为。
        modules = list(PLUGIN_MODULES)
        for custom_id in self.custom_plugin_ids():
            if custom_id not in modules:
                modules.append(custom_id)
        # 目录 mod 排在最后：标识撞了也不顶掉内置插件（撞内置的 mod 在扫描时已拒载）。
        for mod_id in self._mod_entries:
            if mod_id not in modules:
                modules.append(mod_id)

        for mod_name in modules:
            self._mount_module(mod_name)

    def _mount_module(self, mod_name: str) -> bool:
        """导入并挂载一个模块，返回是否真的挂上了至少一个插件实例。

        全量 _load_plugins 与增量 sync_mods 共用这一份实现：导入超时（_run_isolated）、
        post_load 失败回滚、mod 工具表挂载、mounted 审计都在这里，两条路径口径一致。
        """
        mod_entries = getattr(self, '_mod_entries', None) or {}
        before = set(self._plugins)
        is_mod = mod_name in mod_entries
        mod, error = self._run_isolated(lambda name=mod_name: self._import_plugin_module(name))
        if mod is None:
            detail = error or getattr(self, '_last_import_error', '')
            self._load_problems.append(
                f"插件模块 {mod_name} 加载失败（已跳过，不影响其它插件）"
                + (f"：{detail}" if detail else ""))
            print(f"[plugins] Skipped {mod_name}: {detail}")
            return False
        loaded_any = False
        for attr in dir(mod):
            cls = getattr(mod, attr)
            if not (isinstance(cls, type) and issubclass(cls, PluginBase)
                    and cls is not PluginBase and cls.META is not None):
                continue

            def build(c=cls):
                inst = c(self)
                pid = inst.META.id
                self._plugins[pid] = inst
                if pid not in self._settings:
                    self._settings[pid] = copy.deepcopy(inst.META.default_settings)
                if is_mod:
                    self._attach_mod_tools(inst, mod_name)
                try:
                    inst.post_load()
                except BaseException:
                    self._plugins.pop(pid, None)
                    raise
                return inst

            inst, error = self._run_isolated(build)
            if inst is None:
                for pid in set(self._plugins) - before:
                    self._plugins.pop(pid, None)
                self._load_problems.append(
                    f"{mod_name}.{attr} 初始化失败（已跳过）：{error}")
                print(f"[plugins] Skipped {mod_name}.{attr}: {error}")
                continue
            loaded_any = True
            if is_mod:
                self._note_mod_plugin(inst, mod_name)
        if not loaded_any:
            # 没有 PluginBase 子类的模块也跳过并记录，避免静默不加载。
            for pid in set(self._plugins) - before:
                self._plugins.pop(pid, None)
            self._load_problems.append(f"插件模块 {mod_name} 没有可用插件类（已跳过）")
        elif is_mod:
            self._audit_mounted(mod_name)
        return loaded_any

    def take_load_problems(self) -> list[str]:
        """取走并清空"加载期问题"，交给运行记录显示一次。

        以前这些失败只 ``print``：打包运行时 stdout 进的是日志文件，控制台
        「运行记录」里什么都看不到，用户只会觉得"插件装了没反应"。
        """
        problems, self._load_problems = list(self._load_problems), []
        return problems

    def get_settings(self, plugin_id: str) -> dict:
        """返回深拷贝，调用方修改不会影响内部状态。"""
        return copy.deepcopy(self._settings.get(plugin_id, {}))

    def update_settings(self, plugin_id: str, settings: dict):
        """校验后整体提交；任何一步失败都不改变内存与文件。"""
        if plugin_id not in self._plugins:
            raise KeyError(f"Unknown plugin: {plugin_id}")
        if not isinstance(settings, dict):
            raise ValueError("settings must be a dict")
        meta = self._plugins[plugin_id].META
        schema = meta.config_schema or {}
        defaults = copy.deepcopy(meta.default_settings or {})
        previous = copy.deepcopy(self._settings.get(plugin_id, {}))

        # 先全部校验并构建新值，再落内存；非法输入在这一步就抛出，内存不会被改。
        merged = copy.deepcopy(defaults)
        for key, value in previous.items():
            if key not in schema:
                if key in CONTROL_SETTING_FIELDS:
                    try:
                        merged[key] = _validate_control_field(key, value, plugin_id)
                    except ValueError:
                        pass
                continue
            try:
                merged[key] = _validate_field(schema[key], value, key, plugin_id)
            except Exception:
                merged[key] = _fallback_field_default(schema[key], defaults, key)
        for key, value in settings.items():
            if key not in schema:
                # 保留控制字段（enabled）不要求插件声明，其余未知字段照旧拒绝。
                if key in CONTROL_SETTING_FIELDS:
                    merged[key] = _validate_control_field(key, value, plugin_id)
                    continue
                raise ValueError(f"{plugin_id}: unknown setting field '{key}'")
            merged[key] = _validate_field(schema[key], value, key, plugin_id)

        self._plugins[plugin_id].validate_settings(merged)

        self._settings[plugin_id] = merged
        try:
            self._save_settings()
        except Exception as exc:
            self._settings[plugin_id] = previous
            raise exc

    def list_plugins(self) -> List[dict]:
        result = []
        for pid, plugin in self._plugins.items():
            m = plugin.META
            result.append({
                "id": m.id,
                "name": m.name,
                "description": m.description,
                "category": m.category,
                "enabled": plugin.enabled,
                "settings": copy.deepcopy(self._settings.get(pid, {})),
                "defaults": copy.deepcopy(m.default_settings or {}),
                "schema": m.schema_dict(),
            })
        return result

    # ── Hook dispatch ──────────────────────────────────────────────────────

    async def observe(self,message,runtime):
        for plugin in self._plugins.values():
            callback=getattr(plugin,'observe',None)
            if callback and self._enabled(plugin):
                try:await self._call_mod_hook_async(plugin,'observe',callback,runtime,message)
                except Exception:runtime.note(f'{plugin.META.name} 后台处理未完成，聊天继续')

    async def background(self,runtime):
        for plugin in self._plugins.values():
            callback=getattr(plugin,'background',None)
            if callback and self._enabled(plugin):
                try:await self._call_mod_hook_async(plugin,'background',callback,runtime)
                except Exception:runtime.note(f'{plugin.META.name} 后台动作未完成')
            elif callback and callable(getattr(plugin,'cancel_background',None)):
                plugin.cancel_background()

    def cancel_background(self):
        for plugin in self._plugins.values():
            callback=getattr(plugin,'cancel_background',None)
            if callback:callback()

    async def prepare_delivery(self,message,reply,runtime):
        if message.get('_robot_admin'):return reply
        if message.get('_poke_back'):
            poke=self._plugins.get('probability_reply')
            if not poke or not self._enabled(poke):return ''
        for plugin in self._plugins.values():
            callback=getattr(plugin,'prepare_delivery',None)
            if callback and self._enabled(plugin):
                # 单个插件出错**不能丢回复**：以前异常会一路冒到发送链，
                # 这条回复被标 failed、用户什么也收不到（运行记录里也只有一句失败）。
                try:
                    reply=await self._call_mod_hook_async(plugin,'prepare_delivery',callback,runtime,message,reply)
                except Exception as exc:
                    self._note_failure(runtime, plugin, 'prepare_delivery', exc)
        return reply

    @staticmethod
    def _note_failure(runtime, plugin, hook: str, exc: Exception) -> None:
        """插件某个钩子出问题时写进运行记录（界面上看得见），而不是只 print 到日志文件。"""
        name = getattr(getattr(plugin, 'META', None), 'id', None) or type(plugin).__name__
        text = f"插件 {name}.{hook} 出错（已跳过该插件）：{type(exc).__name__}: {exc}"
        print(f"[plugins] {text}")
        note = getattr(runtime, 'note', None)
        if callable(note):
            try:
                note(text)
            except Exception:
                pass

    def _enabled(self, plugin) -> bool:
        """读插件开关，读不出来就当作关闭。

        注意：``plugin.enabled`` 会去问 manager 拿设置，设置文件损坏或读取抛错时
        会连带 raise。如果把它留在 try 之外，一个坏插件就能中断整条 ingest/AI 链路
        （QQ 侧表现为"机器人突然不回了"）。这里统一兜住。
        """
        try:
            return bool(plugin.enabled)
        except Exception as exc:
            print(f"[plugins] {plugin.META.id}.enabled error: {exc}")
            return False

    def run_on_ingest(self, message: dict, runtime) -> Optional[str]:
        """Returns an immediate reply string if any plugin wants to handle it."""
        for plugin in self._plugins.values():
            if not self._enabled(plugin):
                continue
            try:
                r = self._call_mod_hook(plugin, 'on_ingest', plugin.on_ingest, runtime, message)
                if r is not None:
                    return r
            except Exception as exc:
                self._note_failure(runtime, plugin, 'on_ingest', exc)
        return None

    def run_wants_reply(self, message: dict, runtime) -> bool:
        """Any enabled plugin wants this unaddressed message to reach the AI."""
        for plugin in self._plugins.values():
            if not self._enabled(plugin):
                continue
            try:
                if getattr(plugin, 'wants_reply', None) and self._call_mod_hook(
                        plugin, 'wants_reply', plugin.wants_reply, runtime, message):
                    return True
            except Exception as exc:
                self._note_failure(runtime, plugin, 'wants_reply', exc)
        return False

    def run_on_block(self, message: dict, runtime) -> bool:
        """Returns True if any enabled plugin wants to block this message."""
        for plugin in self._plugins.values():
            if not self._enabled(plugin):
                continue
            try:
                if self._call_mod_hook(plugin, 'on_block', plugin.on_block, runtime, message):
                    return True
            except Exception as exc:
                self._note_failure(runtime, plugin, 'on_block', exc)
        return False

    def run_on_pre_ai(self, message: dict, conversation: list, runtime) -> dict:
        """Collects pre-AI hints: delay, block, extra_system, volatile_system, on_ready, max_tokens.

        ``on_ready`` 是本项目给"需要 await 的动作"留的口子：``on_pre_ai`` 是同步钩子
        （不能 await），但有些插件要在这一轮里做一次异步动作（例如发"正在输入"状态）。
        插件可以把一个**无参可调用对象**放进 ``on_ready``，由工作循环
        （``qq_app._work``）在调用模型之前执行；它必须是零参数的，核心会用
        ``asyncio.create_task`` 跑它，并在发送回复前 await 完，保证状态先于消息。

        ``max_tokens`` 是给"省 token"档位用的输出上限（例如极简模式）；
        核心只会用它**调小**本轮的输出上限，不会放大。

        ``volatile_system`` 放"每轮都不一样"的说明（心情数值、本轮发言人摘要、跟聊提醒…）。
        它们会被排到系统提示词的**最后**：服务商按前缀缓存计费，前缀越稳定命中越多，
        所以稳定说明在前、易变说明在后（见「缓存稳定性」插件）。
        """
        result = {"delay": 0.0, "block": False, "extra_system": "", "volatile_system": "",
                  "on_ready": None, "max_tokens": 0}
        for plugin in self._plugins.values():
            if not self._enabled(plugin):
                continue
            try:
                r = self._strip_mod_on_ready(plugin, self._call_mod_hook(
                    plugin, 'on_pre_ai', plugin.on_pre_ai, runtime, message, conversation))
                if not r:
                    continue
                if r.get("block"):
                    result["block"] = True
                if r.get("delay", 0) > result["delay"]:
                    result["delay"] = r["delay"]
                if r.get("extra_system"):
                    result["extra_system"] += "\n" + r["extra_system"]
                if r.get("volatile_system"):
                    result["volatile_system"] += "\n" + r["volatile_system"]
                if callable(r.get("on_ready")) and result["on_ready"] is None:
                    result["on_ready"] = r["on_ready"]
                result["max_tokens"] = _smaller_limit(result["max_tokens"], r.get("max_tokens"))
            except Exception as exc:
                self._note_failure(runtime, plugin, 'on_pre_ai', exc)
        return result

    async def run_on_pre_ai_async(self, message, conversation, runtime):
        result = {"delay": 0.0, "block": False, "extra_system": "", "volatile_system": "",
                  "on_ready": None, "max_tokens": 0}
        actions = []
        for plugin in self._plugins.values():
            if not self._enabled(plugin):
                continue
            try:
                callback = getattr(plugin, 'on_pre_ai_async', plugin.on_pre_ai)
                hint = await self._call_hook_maybe_async(
                    plugin, 'on_pre_ai_async', callback, runtime, message, conversation)
                hint = self._strip_mod_on_ready(plugin, hint)
                if not hint:
                    continue
                result['block'] = result['block'] or bool(hint.get('block'))
                result['delay'] = max(result['delay'], float(hint.get('delay', 0)))
                if hint.get('extra_system'):
                    result['extra_system'] += '\n' + str(hint['extra_system'])
                if hint.get('volatile_system'):
                    result['volatile_system'] += '\n' + str(hint['volatile_system'])
                if callable(hint.get('on_ready')):
                    actions.append((plugin.META.name, hint['on_ready']))
                result['max_tokens'] = _smaller_limit(result['max_tokens'], hint.get('max_tokens'))
            except Exception as exc:
                runtime.note(f'{plugin.META.name} 生成前处理失败（{type(exc).__name__}）')
        if actions:
            async def ready():
                async def run(name, action):
                    try:
                        await asyncio.wait_for(action(), 10)
                    except Exception:
                        runtime.note(f'{name} 附加动作未完成，正常回复继续')
                await asyncio.gather(*(run(name, action) for name, action in actions))
            result['on_ready'] = ready
        return result

    async def describe_images(self, message: dict, runtime) -> None:
        """Image recognition must finish before generating the text reply."""
        plugin = self._plugins.get("image_understanding")
        if plugin is not None:
            try:
                timeout = max(1, min(120, float(plugin.get('timeoutSec', 30))))
                await asyncio.wait_for(plugin.describe(message, runtime), timeout)
                return
            except asyncio.TimeoutError:
                if message.get('vision_description') and str(message.get('vision_status') or '').startswith('部分缺失'):
                    message['vision_status'] = f'部分缺失：后续图片识别超过 {timeout:g} 秒，只能参考已读取的部分'
                    runtime.note('图片识别超时，已保留确认读到的部分；未读区域不推断')
                    return
                message.pop('vision_description', None)
                message.pop('vision_cache_hit', None)
                message['vision_status'] = f'失败：图片下载或识别超过 {timeout:g} 秒，未读取到图片内容'
                runtime.note('图片下载或识别超时，继续文字回复流程；未自动重试图片请求')
                return
            except Exception:
                message.pop("vision_description", None)
                message.pop("vision_cache_hit", None)
        if any(isinstance(item, dict) and item.get("type") == "image"
               for item in message.get("media", [])):
            message["vision_status"] = "失败：图片识别插件未加载或执行异常"

    async def run_on_prepare(self, message, runtime):
        from ..speech_input import supplied_transcripts
        supplied_transcripts(message)
        for plugin in self._plugins.values():
            if self._enabled(plugin):
                try:
                    await self._call_mod_hook_async(plugin,'on_prepare',plugin.on_prepare,runtime,message)
                except Exception:
                    runtime.note(f"{plugin.META.name} 读取附加消息失败")
        if any(isinstance(item, dict) and item.get('type') == 'record'
               for item in (message.get('media') or [])):
            # 插件禁用或异常时也要标记未转写，不能让占位符被当作已听到的语音。
            message.setdefault('speech_status', '语音转写未启用或未完成')

    def run_on_sent(self, message, reply, runtime):
        for plugin in self._plugins.values():
            if self._enabled(plugin):
                try:
                    self._call_mod_hook(plugin, 'on_sent', plugin.on_sent, runtime, message, reply)
                except Exception:
                    runtime.note(f"{plugin.META.name} 保存状态失败")

    async def run_on_before_tool(self, tool_id, arguments, runtime):
        """工具执行前钩子；插件可返回 block / arguments。失败只跳过该插件。"""
        result = {'block': False, 'arguments': arguments, 'reason': '', 'extra_system': ''}
        for plugin in self._plugins.values():
            if not self._enabled(plugin):
                continue
            fn = getattr(plugin, 'on_before_tool', None)
            if not callable(fn):
                continue
            try:
                hint = await self._call_hook_maybe_async(
                    plugin, 'on_before_tool', fn, runtime, str(tool_id), dict(arguments or {}))
                if not isinstance(hint, dict):
                    continue
                if hint.get('block'):
                    result['block'] = True
                    result['reason'] = str(hint.get('reason') or '')
                if isinstance(hint.get('arguments'), dict):
                    arguments = hint['arguments']
                    result['arguments'] = arguments
                if hint.get('extra_system'):
                    result['extra_system'] += '\n' + str(hint['extra_system'])
            except Exception as exc:
                self._note_failure(runtime, plugin, 'on_before_tool', exc)
        return result

    async def run_on_after_tool(self, tool_id, arguments, result_value, runtime):
        """工具执行后钩子；可返回替换结果，失败只跳过该插件。"""
        output = result_value
        for plugin in self._plugins.values():
            if not self._enabled(plugin):
                continue
            fn = getattr(plugin, 'on_after_tool', None)
            if not callable(fn):
                continue
            try:
                value = await self._call_hook_maybe_async(
                    plugin, 'on_after_tool', fn, runtime, str(tool_id),
                    dict(arguments or {}), output)
                if value is not None:
                    output = value
            except Exception as exc:
                self._note_failure(runtime, plugin, 'on_after_tool', exc)
        return output

    async def transform_request(self, body, context=None):
        """把真实请求体交给能力桥的 llm.request-params provider 改写。

        没有桥（或桥停用）时原样返回；桥自己检查 enabled/profile，这里不再
        维护第二套开关，避免两处判断漂移。
        """
        bridge = self._plugins.get('qqagent_bridge')
        transform = getattr(bridge, 'transform_request', None)
        if not callable(transform) or not self._enabled(bridge):
            return {'body': body}
        return await transform(body, context or {})

    @staticmethod
    def _accepts_body_transform(engine) -> bool:
        try:
            params = inspect.signature(engine.chat).parameters.values()
        except (TypeError, ValueError):
            return True
        return any(param.name == 'body_transform' or param.kind == param.VAR_KEYWORD
                   for param in params)

    async def generate_reply(self, engine, conversation, runtime=None, **kwargs):
        # 先摘掉内部参数：engine.chat 只接受模型相关关键字，多一个都会 TypeError
        message = kwargs.pop('_message', None)
        bridge = self._plugins.get('qqagent_bridge')
        transform = getattr(bridge, 'transform_request', None)
        if (callable(transform) and 'body_transform' not in kwargs
                and self._enabled(bridge) and self._accepts_body_transform(engine)):
            context = {'messages': [dict(item) for item in (conversation or []) if isinstance(item, dict)],
                       'senderId': str((message or {}).get('sender_id') or ''),
                       'model': str(getattr(engine, 'model', '') or '')}

            async def _body_transform(payload):
                return await transform(payload, dict(context))

            kwargs['body_transform'] = _body_transform
        import asyncio
        timeout=max(1,float(kwargs.get('timeout',20) or 20))
        # 一次普通请求与已配置的超时重试共用总预算；备用模型不能再累加几分钟。
        budget=timeout+(min(120,max(timeout*1.8,timeout+8)) if kwargs.get('retry_on_timeout',True) else 0)+3
        deadline=asyncio.get_running_loop().time()+budget
        controls = self._plugins.get('chat_controls')
        try:
            request=(controls.generate(engine, conversation, **kwargs) if controls and self._enabled(controls)
                     else engine.chat(conversation, **kwargs))
            reply = await asyncio.wait_for(request,budget)
        except asyncio.TimeoutError:
            engine.last_error=f'模型回复总等待超时（{budget:g}s），已停止后续备用请求'
            return ''
        # 防复读：本会话最近几条回复和新回复太像时，带指令重生成一次（见 reply_variety）。
        variety = self._plugins.get('reply_variety')
        # 按次计费时"太像就重生成"等于多花一次钱，默认跳过
        variety_retry = not (self.per_call_mode and variety is not None
                             and variety.get('retryOnce'))
        if variety is not None and self._enabled(variety) and variety_retry:
            try:
                remaining=deadline-asyncio.get_running_loop().time()
                if remaining>0:
                    reply = await asyncio.wait_for(variety.refine(engine, conversation, reply, message, runtime,
                                                                 **kwargs),remaining)
            except Exception as exc:
                try:
                    if runtime is not None:
                        runtime.note(f"[防复读] 跳过：{type(exc).__name__}: {exc}")
                except Exception:
                    pass
        return reply

    async def run_on_post_reply(self, message: dict, reply: str, runtime) -> str:
        """依次调用各插件的 on_post_reply 变换回复文本。

        历史契约只认字符串返回值。为支持"正文 @名字 → 真 @ 提醒"这类需要附加
        消息段信息的插件（见 src/plugin_segments.py），这里额外接受
        ``(文本, 元数据dict)`` 形式的返回值：元数据写到 ``runtime.reply_meta``，
        由发送链读取；文本仍以字符串在插件之间传递，旧插件不受影响。
        """
        for plugin in self._plugins.values():
            if not self._enabled(plugin):
                continue
            try:
                result = await self._call_mod_hook_async(
                    plugin, 'on_post_reply', plugin.on_post_reply, runtime, message, reply)
            except Exception as exc:
                self._note_failure(runtime, plugin, 'on_post_reply', exc)
                continue
            if isinstance(result, tuple) and len(result) == 2:
                text, meta = result
                if isinstance(text, str):
                    reply = text
                if isinstance(meta, dict) and meta:
                    existing = getattr(runtime, "reply_meta", None)
                    merged = dict(existing) if isinstance(existing, dict) else {}
                    for key, value in meta.items():
                        if key == "media" and value:
                            merged["media"] = list(merged.get("media") or []) + list(value)
                        elif key == "mentions" and value:
                            merged["mentions"] = list(merged.get("mentions") or []) + list(value)
                        else:
                            merged[key] = value
                    runtime.reply_meta = merged
            elif isinstance(result, str):
                reply = result
        return reply

    async def run_tick(self, runtime):
        for plugin in self._plugins.values():
            # 用 _enabled()：直接读 plugin.enabled 在设置文件损坏时会抛错，
            # 一个坏插件就能把整轮 tick 打断（并把这个异常顶到调用方）。
            if not self._enabled(plugin):
                continue
            try:
                await self._call_mod_hook_async(plugin, 'tick', plugin.tick, runtime)
            except Exception as exc:
                # 以前只 print：打包运行时 stdout 进的是日志文件，控制台"运行记录"里
                # 什么都没有，用户看到的现象是"插件开着却一直没反应"。
                self._note_failure(runtime, plugin, 'tick', exc)
