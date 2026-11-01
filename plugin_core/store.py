"""plugin_core.store — 命名空间 KV + data_policy（规格 §7）。"""

from __future__ import annotations

import json
from pathlib import Path

from .api import DATA_POLICIES, KernelError, PermissionDenied


class PluginKV:
    """一个插件自己的 KV：键不跨插件可见（一插件一文件一 dict）。"""

    def __init__(self, owner: str, store: "Store") -> None:
        self.owner = owner
        self.store = store

    def _data(self) -> dict:
        return self.store.kv_data(self.owner)

    def get(self, key: str, default=None):
        return self._data().get(key, default)

    def set(self, key: str, value) -> None:
        data = self._data()
        data[key] = value
        self.store.kv_flush(self.owner)

    def delete(self, key: str) -> bool:
        data = self._data()
        if key in data:
            del data[key]
            self.store.kv_flush(self.owner)
            return True
        return False

    def keys(self) -> list:
        return sorted(self._data().keys())

    def as_dict(self) -> dict:
        return dict(self._data())


class Store:
    def __init__(self, data_dir=None) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self.memory_kv = {}
        self.memory_cfg = {}
        if self.data_dir is not None:
            (self.data_dir / "plugin_kv").mkdir(parents=True, exist_ok=True)
            (self.data_dir / "plugin_config").mkdir(parents=True, exist_ok=True)

    # --- 路径 ---
    def kv_path(self, plugin_id: str) -> Path:
        return self.data_dir / "plugin_kv" / ("%s.json" % plugin_id)

    def cfg_path(self, plugin_id: str) -> Path:
        return self.data_dir / "plugin_config" / ("%s.json" % plugin_id)

    # --- KV ---
    def kv_data(self, plugin_id: str) -> dict:
        if plugin_id in self.memory_kv:
            return self.memory_kv[plugin_id]
        data = {}
        if self.data_dir is not None:
            path = self.kv_path(plugin_id)
            if path.exists():
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
        self.memory_kv[plugin_id] = data
        return data

    def kv_flush(self, plugin_id: str) -> None:
        if self.data_dir is None:
            return
        path = self.kv_path(plugin_id)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(self.kv_data(plugin_id), fh, ensure_ascii=False, indent=2, sort_keys=True)

    def kv(self, plugin_id: str) -> PluginKV:
        return PluginKV(plugin_id, self)

    # --- 配置 ---
    def read_config(self, plugin_id: str) -> dict:
        if self.data_dir is not None:
            path = self.cfg_path(plugin_id)
            if path.exists():
                with open(path, "r", encoding="utf-8") as fh:
                    cfg = json.load(fh)
                self.memory_cfg[plugin_id] = cfg
                return cfg
        return dict(self.memory_cfg.get(plugin_id, {}))

    def write_config(self, plugin_id: str, values: dict, *, declared, flags) -> dict:
        if "store" not in set(declared or ()):
            raise PermissionDenied(plugin_id, "config_write", "store")
        if "config_write" not in set(flags or ()):
            raise PermissionDenied(plugin_id, "config_write", "config_write")
        if not isinstance(values, dict):
            raise KernelError("config 必须是 dict")
        cfg = self.read_config(plugin_id)
        cfg.update(values)
        self.memory_cfg[plugin_id] = cfg
        if self.data_dir is not None:
            with open(self.cfg_path(plugin_id), "w", encoding="utf-8", newline="\n") as fh:
                json.dump(cfg, fh, ensure_ascii=False, indent=2, sort_keys=True)
        return cfg

    # --- 卸载 ---
    def uninstall(self, plugin_id: str, data_policy: str) -> dict:
        if data_policy not in DATA_POLICIES:
            raise KernelError("未知 data_policy %r" % (data_policy,))
        removed = []
        self.memory_kv.pop(plugin_id, None)
        if data_policy == "purge":
            for path in (self.kv_path(plugin_id), self.cfg_path(plugin_id)):
                if self.data_dir is not None and path.exists():
                    path.unlink()
                    removed.append(str(path))
            self.memory_cfg.pop(plugin_id, None)
        return {"plugin": plugin_id, "data_policy": data_policy, "removed": removed}
