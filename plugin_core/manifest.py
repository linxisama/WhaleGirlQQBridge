"""plugin_core.manifest — manifest v1 读取与校验（规格 §2 / §8.8）。

17 个拒载码全部从这里或其调用方产生，code 稳定不变。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .api import (
    API_VERSION,
    DATA_POLICIES,
    OPTIONAL_FIELDS,
    OUTBOUND_PRODUCES,
    PRODUCES,
    REQUIRED_FIELDS,
    SIDES,
    STAGES,
    ManifestError,
)
from .permissions import normalize_flags, normalize_permissions

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+")
ENTRY_RE = re.compile(r"^(?P<path>[^:]+\.py):(?P<func>[A-Za-z_][A-Za-z0-9_]*)$")
FORBIDDEN_RANGE_WORDS = ("最新版", "最新", "latest", "stable", "any", "*")


def _need_text(value, field, code="bad-manifest", max_len=None):
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(code, "%s 必须是非空字符串" % field)
    if max_len is not None and len(value) > max_len:
        raise ManifestError(code, "%s 长度 %d 超过上限 %d" % (field, len(value), max_len))
    return value


def _normalize_capabilities(value):
    if not isinstance(value, list):
        raise ManifestError("bad-capability", "capabilities 必须是数组")
    out = []
    for index, item in enumerate(value):
        where = "capabilities[%d]" % index
        if not isinstance(item, dict):
            raise ManifestError("bad-capability", "%s 必须是对象" % where)
        stage = item.get("stage")
        if stage not in STAGES:
            raise ManifestError(
                "bad-capability", "%s.stage 必须是 %s 之一，收到 %r" % (where, "/".join(STAGES), stage)
            )
        produces = item.get("produces")
        allowed = OUTBOUND_PRODUCES if stage == "outbound" else PRODUCES
        if produces not in allowed:
            raise ManifestError(
                "bad-capability",
                "%s.produces 在 stage=%s 下必须是 %s 之一，收到 %r"
                % (where, stage, "/".join(allowed), produces),
            )
        priority = item.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ManifestError("bad-capability", "%s.priority 必须是 int，收到 %r" % (where, priority))
        entry = {"stage": stage, "produces": produces, "priority": priority}
        if "exclusive" in item:
            if not isinstance(item["exclusive"], bool):
                raise ManifestError("bad-capability", "%s.exclusive 必须是 bool" % where)
            if produces != "main":
                raise ManifestError(
                    "bad-capability", "%s.exclusive 只对 produces=main 有意义" % where
                )
            entry["exclusive"] = item["exclusive"]
        if "requires_main" in item:
            if not isinstance(item["requires_main"], bool):
                raise ManifestError("bad-capability", "%s.requires_main 必须是 bool" % where)
            if produces != "attach":
                raise ManifestError(
                    "bad-capability", "%s.requires_main 只对 produces=attach 有意义" % where
                )
            entry["requires_main"] = item["requires_main"]
        out.append(entry)
    return out


def _normalize_upstream(value):
    if not isinstance(value, dict):
        raise ManifestError("bad-upstream", "upstream 必须是对象 {protocol,kernel,range}")
    for field in ("protocol", "kernel", "range"):
        if field not in value:
            raise ManifestError("bad-upstream", "upstream 缺字段 %s" % field)
        _need_text(value[field], "upstream.%s" % field, code="bad-upstream")
    rng = value["range"].strip()
    if rng.lower() in FORBIDDEN_RANGE_WORDS:
        raise ManifestError("bad-upstream", "upstream.range 不许写 %r，必须写明确区间（例 >=3.2 <4）" % rng)
    if not any(ch.isdigit() for ch in rng):
        raise ManifestError("bad-upstream", "upstream.range 必须是明确的版本区间（例 >=3.2 <4），收到 %r" % rng)
    return {"protocol": value["protocol"], "kernel": value["kernel"], "range": rng}


def normalize_manifest(data, dir_name: str | None = None) -> dict:
    """校验并归一化一份 manifest；失败抛 ManifestError(17 码之一)。"""
    if not isinstance(data, dict):
        raise ManifestError("bad-manifest", "plugin.json 顶层必须是对象")

    for field in REQUIRED_FIELDS:
        if field not in data:
            raise ManifestError("missing-field", "缺必填字段 %s" % field)

    plugin_id = data["id"]
    if not isinstance(plugin_id, str) or not ID_RE.match(plugin_id):
        raise ManifestError("bad-id", "id 必须是 kebab-case ^[a-z0-9][a-z0-9-]{0,63}$，收到 %r" % (plugin_id,))
    if dir_name is not None and plugin_id != dir_name:
        raise ManifestError("id-mismatch", "id=%r 必须等于目录名 %r" % (plugin_id, dir_name))

    _need_text(data["name"], "name")

    version = data["version"]
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise ManifestError("bad-manifest", "version 必须是 semver（例 1.0.0），收到 %r" % (version,))

    api_version = data["api_version"]
    if isinstance(api_version, bool) or not isinstance(api_version, int):
        raise ManifestError("bad-api-version", "api_version 必须是 int，收到 %r" % (api_version,))
    if api_version != API_VERSION:
        raise ManifestError(
            "api-version-mismatch", "api_version=%s != %s" % (api_version, API_VERSION)
        )

    author = data["author"]
    if not isinstance(author, dict):
        raise ManifestError("bad-manifest", "author 必须是对象 {name,contact}")
    for field in ("name", "contact"):
        if field not in author:
            raise ManifestError("missing-field", "author 缺字段 %s" % field)
        _need_text(author[field], "author.%s" % field)

    description = data["description"]
    if not isinstance(description, str):
        raise ManifestError("bad-manifest", "description 必须是字符串")
    if len(description) > 30:
        raise ManifestError("bad-manifest", "description 是 %d 字，超过 30 字上限" % len(description))

    entry = data["entry"]
    if not isinstance(entry, str) or not ENTRY_RE.match(entry):
        raise ManifestError(
            "bad-entry", "entry 必须是 '<相对路径>.py:<函数名>'，收到 %r" % (entry,)
        )

    permissions = normalize_permissions(data["permissions"])
    flags = normalize_flags(data.get("permission_flags"), permissions)

    capabilities = _normalize_capabilities(data["capabilities"])

    side = data["side"]
    if side not in SIDES:
        raise ManifestError("bad-side", "side 必须是 %s 之一，收到 %r" % ("/".join(SIDES), side))

    data_policy = data["data_policy"]
    if data_policy not in DATA_POLICIES:
        raise ManifestError(
            "bad-data-policy",
            "data_policy 必须是 %s 之一，收到 %r" % ("/".join(DATA_POLICIES), data_policy),
        )

    upstream = _normalize_upstream(data["upstream"])

    native = data["native"]
    if not isinstance(native, bool):
        raise ManifestError("bad-manifest", "native 必须是 bool，收到 %r" % (native,))

    storage = data.get("storage", {})
    if not isinstance(storage, dict):
        raise ManifestError("bad-manifest", "storage 必须是对象")
    config_schema = data.get("config_schema", {})
    if not isinstance(config_schema, dict):
        raise ManifestError("bad-manifest", "config_schema 必须是对象")

    normalized = {
        "id": plugin_id,
        "name": data["name"],
        "version": version,
        "api_version": api_version,
        "author": {"name": author["name"], "contact": author["contact"]},
        "description": description,
        "entry": entry,
        "permissions": permissions,
        "permission_flags": flags,
        "capabilities": capabilities,
        "side": side,
        "data_policy": data_policy,
        "upstream": upstream,
        "native": native,
        "storage": storage,
        "config_schema": config_schema,
    }
    return normalized


def load_manifest_file(plugin_dir, dir_name: str | None = None) -> dict:
    plugin_dir = Path(plugin_dir)
    path = plugin_dir / "plugin.json"
    if not path.exists():
        raise ManifestError("bad-manifest", "%s 下没有 plugin.json" % plugin_dir.name)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ManifestError("bad-json", "plugin.json 不是合法 JSON: %s" % exc) from exc
    except UnicodeDecodeError as exc:
        raise ManifestError("bad-json", "plugin.json 不是 UTF-8: %s" % exc) from exc
    return normalize_manifest(data, dir_name if dir_name is not None else plugin_dir.name)


def has_capability(manifest: dict, stage: str, produces=None) -> bool:
    for cap in manifest.get("capabilities", ()):
        if cap.get("stage") != stage:
            continue
        if produces is None or cap.get("produces") == produces:
            return True
    return False


def capability_index(manifest: dict, stage: str, produces=None) -> int:
    """manifest 声明序：该 capability 在 capabilities 数组里的下标（兜底 10**6）。"""
    for index, cap in enumerate(manifest.get("capabilities", ())):
        if cap.get("stage") != stage:
            continue
        if produces is None or cap.get("produces") == produces:
            return index
    return 10**6
