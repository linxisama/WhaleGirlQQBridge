"""plugin_core.loader — 目录扫描、entry 解析、freeze、拒载审计（规格 §0.1/§2）。"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

from .api import KernelError, ManifestError
from .events import Lifecycle
from .manifest import load_manifest_file
from .sdk import PluginAPI


class LoadedPlugin:
    """一个已读入的插件（尚未 enable 也算）。"""

    def __init__(self, plugin_id: str, manifest: dict, plugin_dir: Path, api: PluginAPI, lifecycle: Lifecycle, setup) -> None:
        self.id = plugin_id
        self.manifest = manifest
        self.dir = Path(plugin_dir)
        self.api = api
        self.lifecycle = lifecycle
        self.setup = setup
        self.enabled = False
        self.loaded = False

    @property
    def permissions(self) -> set:
        return set(self.manifest.get("permissions") or ())

    @property
    def declarations(self):
        return self.api.declarations

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "name": self.manifest.get("name"),
            "version": self.manifest.get("version"),
            "api_version": self.manifest.get("api_version"),
            "permissions": sorted(self.permissions),
            "capabilities": [dict(c) for c in self.manifest.get("capabilities") or ()],
            "side": self.manifest.get("side"),
            "data_policy": self.manifest.get("data_policy"),
            "dir": str(self.dir),
        }

    def __repr__(self) -> str:
        return "LoadedPlugin(%s, enabled=%r)" % (self.id, self.enabled)


def resolve_entry(plugin_dir, entry: str):
    """entry -> 可调用 setup。逃逸/缺失/坏形状分别抛对应拒载码。"""
    path_part, _, func_part = entry.partition(":")
    rel = Path(path_part)
    root = Path(plugin_dir).resolve()
    if rel.is_absolute():
        raise ManifestError("entry-escape", "entry 不许是绝对路径：%r" % entry)
    target = (Path(plugin_dir) / rel).resolve()
    if target != root and root not in target.parents:
        raise ManifestError("entry-escape", "entry 逃出插件目录：%r" % entry)
    if not target.exists() or not target.is_file():
        raise ManifestError("entry-missing", "entry 文件不存在：%r" % path_part)
    module_name = "plugin_core_plugin_%s" % uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(module_name, str(target))
    if spec is None or spec.loader is None:
        raise ManifestError("bad-entry", "无法为 %r 建立模块加载器" % path_part)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # 插件模块自身导入期异常
        raise ManifestError("bad-entry", "入口模块导入失败：%s: %s" % (type(exc).__name__, exc)) from exc
    fn = getattr(module, func_part, None)
    if fn is None:
        raise ManifestError("bad-entry", "入口函数 %s 不存在于 %s" % (func_part, path_part))
    if not callable(fn):
        raise ManifestError("bad-entry", "入口 %s 不可调用" % entry)
    return fn


class Loader:
    def __init__(self, plugins_dir, audit, log, gate, store) -> None:
        self.plugins_dir = Path(plugins_dir) if plugins_dir is not None else None
        self.audit = audit
        self.log = log
        self.gate = gate
        self.store = store

    def scan_dirs(self) -> list:
        if self.plugins_dir is None or not self.plugins_dir.exists():
            return []
        return sorted(
            (path for path in self.plugins_dir.iterdir() if path.is_dir()),
            key=lambda path: path.name,
        )

    def record_refusal(self, plugin_dir, error: ManifestError, extra: dict | None = None) -> dict:
        event = {
            "event": "load-refused",
            "plugin": plugin_dir.name if plugin_dir is not None else None,
            "code": error.code,
            "detail": error.detail,
            "dir": str(plugin_dir) if plugin_dir is not None else None,
        }
        if extra:
            event.update(extra)
        rec = self.audit.write(event)
        self.log.error(
            "拒载插件 %s：%s（%s）" % (plugin_dir.name if plugin_dir else "?", error.code, error.detail)
        )
        return rec

    def load_dir(self, plugin_dir: Path, seen: dict):
        manifest = load_manifest_file(plugin_dir, plugin_dir.name)
        plugin_id = manifest["id"]
        if plugin_id in seen:
            raise ManifestError("duplicate-id", "插件 id %r 已存在（%s）" % (plugin_id, seen[plugin_id].dir))
        setup = resolve_entry(plugin_dir, manifest["entry"])
        lifecycle = Lifecycle(plugin_id, audit=self.audit, log=self.log.child("lifecycle"))
        api = PluginAPI(
            plugin_id,
            manifest,
            log=self.log,
            store=self.store,
            audit=self.audit,
            gate=self.gate,
            lifecycle=lifecycle,
        )
        lifecycle.enter("preLaunch")
        lifecycle.enter("load")
        try:
            setup(api)
        except Exception as exc:
            raise ManifestError("setup-error", "setup 抛异常：%s: %s" % (type(exc).__name__, exc)) from exc
        plugin = LoadedPlugin(plugin_id, manifest, plugin_dir, api, lifecycle, setup)
        plugin.loaded = True
        return plugin

    def load_tree(self, plugin_dirs=None) -> dict:
        """载入一批目录；单个失败只影响该插件。返回 {"loaded": [..], "refused": [...]}。"""
        dirs = list(plugin_dirs if plugin_dirs is not None else self.scan_dirs())
        loaded = []
        refused = []
        seen = {}
        for plugin_dir in dirs:
            try:
                plugin = self.load_dir(Path(plugin_dir), seen)
            except ManifestError as exc:
                refused.append({"dir": str(plugin_dir), "code": exc.code, "detail": exc.detail})
                self.record_refusal(Path(plugin_dir), exc)
                continue
            except Exception as exc:  # 兜底：绝不让坏插件打断主体启动
                wrapped = ManifestError("setup-error", "%s: %s" % (type(exc).__name__, exc))
                refused.append({"dir": str(plugin_dir), "code": wrapped.code, "detail": wrapped.detail})
                self.record_refusal(Path(plugin_dir), wrapped)
                continue
            seen[plugin.id] = plugin
            loaded.append(plugin)
        return {"loaded": loaded, "refused": refused}
