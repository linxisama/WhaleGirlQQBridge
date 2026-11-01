"""plugin_core — 独立插件内核参考实现。

纯标准库、零依赖、可独立运行：不 import 本仓库任何 src/ 模块，也不联网。
权威规格：docs/插件内核-API-v1.md。

公开面示例：
    from plugin_core import Host, MockTransport, MockModel, MockSource
    host = Host("examples", ".data", MockTransport(), MockModel())
    host.start()
    record = host.handle(MockSource().message("你好"))
"""

from .api import (
    API_VERSION,
    HOST_VERSION,
    PLUGIN_API_VERSION,
    Bid,
    FreezeViolation,
    HookFailed,
    KernelError,
    LifecycleError,
    ManifestError,
    PermissionDenied,
    REFUSAL_CODES,
    bid,
    coerce_bid,
)
from .arbiter import Arbiter
from .audit import Audit, Log, stderr_sink
from .events import Lifecycle
from .host import DEFAULT_SYSTEM_PROMPT, Host, normalize_message
from .loader import LoadedPlugin, Loader, resolve_entry
from .manifest import capability_index, has_capability, load_manifest_file, normalize_manifest
from .mock import MockModel, MockSource, MockTransport
from .outbound import OutboundPipeline
from .permissions import PermissionGate
from .sdk import Action, Context, Draft, ModelView, PluginAPI, StoreView
from .store import PluginKV, Store

PluginContext = Context

__version__ = HOST_VERSION


def load_plugins(plugins_dir, data_dir=None, *, audit=None, logger=None):
    """只做扫描 + 加载，不启动主体：返回 (loaded, refused) 二元组。

    需要完整生命周期（enable / freeze / handle）时请直接用 Host。
    """
    audit = audit if audit is not None else Audit(data_dir)
    log = logger if logger is not None else Log("plugin_core")
    gate = PermissionGate(audit, log.child("permissions"))
    store = Store(data_dir)
    loader = Loader(plugins_dir, audit, log.child("loader"), gate, store)
    result = loader.load_tree()
    return result["loaded"], result["refused"]


__all__ = [
    "API_VERSION",
    "PLUGIN_API_VERSION",
    "HOST_VERSION",
    "__version__",
    # 主体与生命周期
    "Host",
    "DEFAULT_SYSTEM_PROMPT",
    "Arbiter",
    "OutboundPipeline",
    "Lifecycle",
    "Loader",
    "LoadedPlugin",
    "load_plugins",
    "resolve_entry",
    # 上下文与产出
    "Context",
    "PluginContext",
    "PluginAPI",
    "Bid",
    "bid",
    "coerce_bid",
    "Action",
    "Draft",
    "ModelView",
    "StoreView",
    "PluginKV",
    "Store",
    # mock
    "MockTransport",
    "MockModel",
    "MockSource",
    # 权限 / 审计 / manifest
    "PermissionGate",
    "Audit",
    "Log",
    "stderr_sink",
    "normalize_message",
    "normalize_manifest",
    "load_manifest_file",
    "has_capability",
    "capability_index",
    # 错误与常量
    "KernelError",
    "ManifestError",
    "PermissionDenied",
    "FreezeViolation",
    "LifecycleError",
    "HookFailed",
    "REFUSAL_CODES",
]
