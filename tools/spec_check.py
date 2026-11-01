"""spec_check.py - 插件规格的三层合规测量（只读，唯一产出是判定矩阵）。

三层口径（与规格书 §9 对齐）：

* L1 解析/预检   —— 包能读出清单、判出家族、包内结构自洽（pack_import.detect + inspect）
* L2 装进宿主根 —— 真落盘到隔离宿主根，文件到齐（pack_import.install）
* L3 宿主接受并真调 —— 扩展走 host-one 真加载并真调一个工具；Python 插件走 Python 加载；
                      技能走分层判定 T1-ready

用法：
    python tools/spec_check.py                              # 三处登记语料 941 包
    python tools/spec_check.py --corpus <dir> --json out.json
    python tools/spec_check.py <包目录或 zip>                # 单包自检
    python tools/spec_check.py <包> --layers l1            # 只跑 L1
    python tools/spec_check.py --codes                      # 打印原因码字典

本脚本只读：不改 src/、runtime/、任何插件包；唯一写盘是 --work 指定的隔离宿主根
（默认 .tmp/inv/conformance-host，含 --json 指定的结果文件）。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
import zipfile
from collections import Counter, OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import pack_import  # noqa: E402

LAYERS = ("l1", "l2", "l3")
FAMILIES = ("extension-js", "plugin-py", "skill", "feature-pack", pack_import.MCP_KIND,
            "unknown")

DEFAULT_CORPORA = (
    ("external-market", Path(r"D:/聊天机器人/qq机器人/.tmp/external-market")),
    ("market-root", Path(r"D:/聊天机器人/插件市场")),
    ("skill-market", Path(r"D:/聊天机器人/插件市场/Skill市场")),
)

# 原因码字典：码 → (归属层, 含义)。实测得到的集合，全部来自 src/ 的真实判定点。
CODE_LAYERS = OrderedDict((
    ("path-invalid", ("l1", "目标不是目录/zip/单文件")),
    ("path-missing", ("l1", "目标不存在")),
    ("zip-invalid", ("l1", "zip 打不开或为空壳")),
    ("empty-pack", ("l1", "包里没有任何文件")),
    ("inspect-failed", ("l1", "预检内部异常")),
    ("unrecognized", ("l1", "五种家族都认不出")),
    ("manifest-invalid", ("l1", "选中的清单文件不是合法 JSON（pack_import 静默降级，本脚本补查）")),
    ("capability-names-invalid", ("l1", "capabilities/requires 不是≤128 项非空字符串数组")),
    ("py-entry-missing", ("l1", "plugin.json 无 entry，包内也没有唯一 .py")),
    ("cjs-unsupported", ("l1", "入口是 CommonJS，宿主只吃 ESM")),
    ("unsupported-transport", ("l1", "MCP 只有 url/type，没有 command")),
    ("bad-id", ("l1", "MCP server id 不合法")),
    ("command-not-absolute", ("l1", "MCP command 是相对路径")),
    ("b1-dialect-normalized", ("l1", "whale-plugin v1 老方言已归一（告知，不拒收）")),
    ("needs-console-ui", ("l1", "清单/代码要 ui/index.html，包里没有；宿主也无此能力")),
    ("enabled-by-default-false", ("l1", "装后默认不启用，需手动开")),
    ("kind-mismatch", ("l2", "指定家族与内容判定不符")),
    ("duplicate-id", ("l2", "同一宿主根里 id 已被前一个包占用")),
    ("exists", ("l2", "目标落点已存在（非本轮语料内重复）")),
    ("install-failed", ("l2", "落盘过程异常")),
    ("installer-missing", ("l2", "复用的安装器取不到")),
    ("security-forbidden-subprocess", ("l2", "Python 安全闸拦下 subprocess 等禁用面")),
    ("security-forbidden-other", ("l2", "Python 安全闸拦下 socket/ctypes/eval 等")),
    ("host-root-invalid", ("l3", "宿主根缺标记文件")),
    ("node-missing", ("l3", "找不到 node，扩展无法真加载")),
    ("host-script-missing", ("l3", "找不到 host-one.mjs")),
    ("load-failed", ("l3", "宿主加载扩展抛错")),
    ("enable-required", ("l3", "宿主配置里该扩展未启用：装进去了，默认不加载")),
    ("missing-capability", ("l3", "扩展要求的依赖/能力提供者没启用或没装")),
    ("load-needs-config", ("l3", "扩展加载前要求先填配置（主人 QQ 号/模型/关键词等）")),
    ("timeout", ("l3", "宿主加载/调用超时")),
    ("no-capability", ("l3", "加载成功但 tools/hooks/providers 全空：什么都没接上")),
    ("no-callable-tool", ("l3", "加载成功且只注册了 hooks/providers，无工具可真调")),
    ("call-failed", ("l3", "工具真调传输失败")),
    ("call-needs-config", ("l3", "工具真调返回 isError：要配置/前置条件")),
    ("call-bad-args", ("l3", "工具真调返回 isError：空参数被参数校验拒")),
    ("call-biz-rejected", ("l3", "工具真调返回 isError：其它业务级拒绝")),
    ("python-load-failed", ("l3", "Python 插件加载失败")),
)) 

# 技能分层（T2..T6）与 L3 未验证类码由运行期补进字典
SKILL_TIERS = ("T1-ready", "T2-web-fetch", "T3-extra-api", "T4-host-source",
               "T5-depends-other", "T6-disabled")
UNVERIFIED_CODES = ("call-skipped-danger", "no-callable-tool", "no-host-call")

DANGER = re.compile(
    r"(shutdown|reboot|poweroff|kill|exit|restart|delete|remove|unlink|write|save|upload|"
    r"send|exec|shell|cmd|run_|install|uninstall|format|clear|reset|ban|kick|mute|admin|"
    r"approve|grant|invite|set_|_set|config|patch|deploy|stop|start|record)",
    re.IGNORECASE,
)
NEED_CFG = ("还没", "未配置", "请先", "请填", "尚未", "没有", "缺少", "找不到", "需要",
            "未启用", "不可用")
NEED_NET = ("fetch failed", "ENOTFOUND", "ECONNREFUSED", "ETIMEDOUT", "abort", "network",
            "getaddrinfo", "socket hang up")
# src/plugin_installer.py:53-67 的三张表：命中哪张决定二级原因码
SECURITY_SUBPROCESS = "subprocess"
SECURITY_OTHER = ("socket", "ctypes", "winreg", "os.system", "os.popen", "os.remove",
                  "os.unlink", "shutil.rmtree", "pickle", "eval", "exec")


def codes_of(items) -> list:
    return [str(item.get("code") or "") for item in (items or []) if item.get("code")]


def detail_of(items, code: str) -> str:
    for item in items or []:
        if str(item.get("code") or "") == code:
            return str(item.get("message") or "")[:300]
    return ""


def security_code(text: str) -> str:
    """把 install-failed 的安全闸消息还原成具体禁用面（src/plugin_installer.py:53-67）。"""
    body = text or ""
    if SECURITY_SUBPROCESS in body:
        return "security-forbidden-subprocess"
    if any(token in body for token in SECURITY_OTHER):
        return "security-forbidden-other"
    return ""


def biz_code(text: str) -> str:
    """把工具 isError 的正文归到具体原因码（沿用 compat_audit_calls.py:42-55 的分桶口径）。"""
    body = text or ""
    low = body.lower()
    if any(token.lower() in low for token in NEED_NET):
        return "call-failed"
    if any(token in body for token in NEED_CFG):
        return "call-needs-config"
    if "参数" in body or "argument" in low or "required" in low or "invalid" in low:
        return "call-bad-args"
    return "call-biz-rejected"


def probe_code(message: str) -> str:
    """import_probe 的失败正文归到具体原因码：绝大多数「加载失败」其实是没启用/没配。"""
    body = message or ""
    if "的依赖未启用" in body or "缺少已安装的能力提供者" in body:
        return "missing-capability"
    if "宿主配置未启用" in body:
        return "enable-required"
    if "需要控制台 UI" in body or "needs-console-ui" in body:
        return "needs-console-ui"
    if "不可用：请" in body or "不可用：还没" in body or "不可用：没拿到" in body:
        return "load-needs-config"
    return "load-failed"


def collect_targets(corpora) -> list:
    """[(corpus_label, path)]：语料目录取顶层 *.zip，与既有审计同口径。"""
    targets = []
    for label, base in corpora:
        path = Path(base)
        if not path.is_dir():
            raise SystemExit("语料目录不存在：%s" % path)
        zips = sorted(item for item in path.iterdir()
                      if item.is_file() and item.suffix.lower() == ".zip")
        targets.extend((label, item) for item in zips)
    return targets


class Host:
    """隔离宿主根：只由本脚本显式创建，绝不回落到仓库真实 data/。"""

    def __init__(self, work: Path):
        self.data_dir = work / "data"
        self.plugins_dir = self.data_dir / "plugins"
        self.skills_root = self.data_dir
        self.host_root = self.data_dir / pack_import.QQ_ONLY / "qqagent_host"
        self._node = None
        self._script = None
        self.node_ready = None

    def prepare(self) -> None:
        self.plugins_dir.mkdir(parents=True, exist_ok=True)

    def scratch(self) -> Path:
        """目录包要装盘时必须先打成 zip：install() 只吃 zip 与单文件 .py。"""
        path = self.data_dir.parent / "scratch"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def node(self):
        if self._node is None:
            from src import qqagent_host
            self._node = qqagent_host._node_binary(None) or ""
        return self._node or None

    def script(self):
        if self._script is None:
            from src import qqagent_host
            if qqagent_host.is_host_root(self.host_root):
                self._script = qqagent_host.find_host_script(self.host_root) or ""
            else:
                self._script = ""
        return self._script or None

    def resolvable(self) -> bool:
        return bool(self.node() and self.script())


MANIFEST_NAMES = ("plugin.json", "skill.json", "mcp.json", "pack.json")
MANIFEST_DEPTH = 4


def check_manifest_text(name: str, text: str) -> str:
    try:
        value = json.loads(text)
    except ValueError as exc:
        return "%s 不是合法 JSON：%s" % (name, exc)
    if not isinstance(value, dict):
        return "%s 顶层必须是 JSON 对象，现在是 %s" % (name, type(value).__name__)
    return ""


def manifest_invalid(target: Path, found: dict) -> str:
    """pack_import 把坏清单降级成「家族仍可判、problems 为空」，规格要求单独点名。

    只读 detect() 选中的那个清单名，深度上限与 _find_root 的 wrap 容忍一致。
    """
    wanted = str(found.get("manifest_name") or "")
    if not wanted:
        return ""
    target = Path(target)
    if target.is_file() and target.suffix.lower() == ".zip":
        with zipfile.ZipFile(str(target)) as archive:
            for name in sorted(archive.namelist()):
                if name.rsplit("/", 1)[-1] != wanted or name.count("/") > MANIFEST_DEPTH:
                    continue
                try:
                    text = archive.read(name).decode("utf-8-sig", "replace")
                except BaseException:
                    continue
                broken = check_manifest_text(name, text)
                if broken:
                    return broken
        return ""
    if target.is_dir():
        for item in sorted(target.rglob(wanted)):
            name = item.relative_to(target).as_posix()
            if not item.is_file() or name.count("/") > MANIFEST_DEPTH:
                continue
            try:
                text = item.read_text(encoding="utf-8-sig", errors="replace")
            except BaseException:
                continue
            broken = check_manifest_text(name, text)
            if broken:
                return broken
    return ""


def check_l1(target: Path, host: Host) -> dict:
    out = {"ok": False, "codes": [], "notices": [], "kind": "unknown", "id": "",
           "name": target.name, "detail": ""}
    try:
        found = pack_import.detect(target) or {}
    except BaseException as exc:  # detect 契约上永不抛，这里只兜底
        out["codes"].append("inspect-failed")
        out["detail"] = "%s: %s" % (type(exc).__name__, exc)
        return out
    broken = manifest_invalid(target, found)
    if broken:
        out["kind"] = str(found.get("kind") or "unknown")
        out["codes"].append("manifest-invalid")
        out["detail"] = broken
        return out
    out["kind"] = str(found.get("kind") or "unknown")
    report = pack_import.inspect(target, plugins_dir=host.plugins_dir,
                                 skills_runtime=host.skills_root, data_dir=host.data_dir) or {}
    out["id"] = str(report.get("id") or "")
    out["kind"] = str(report.get("kind") or out["kind"])
    problems = report.get("problems") or []
    out["codes"] = codes_of(problems)
    out["notices"] = codes_of(report.get("notices"))
    out["ok"] = not problems
    if problems:
        out["detail"] = detail_of(problems, out["codes"][0])
    return out


def pack_dir(target: Path, host: Host) -> Path:
    """目录包打成临时 zip 再走 install：安装器只吃 zip 与单文件 .py。"""
    if not target.is_dir():
        return target
    archive = host.scratch() / ("%s.zip" % target.name)
    with zipfile.ZipFile(str(archive), "w", zipfile.ZIP_DEFLATED) as handle:
        for item in sorted(target.rglob("*")):
            if item.is_file():
                handle.write(str(item), item.relative_to(target).as_posix())
    return archive


def check_l2(target: Path, host: Host, seen_ids: dict, overwrite: bool) -> dict:
    out = {"ok": False, "codes": [], "landed": "", "detail": ""}
    try:
        source = pack_dir(target, host)
    except BaseException as exc:  # noqa: BLE001
        out["codes"].append("install-failed")
        out["detail"] = "打临时 zip 失败：%s: %s" % (type(exc).__name__, exc)
        return out
    try:
        report = pack_import.install(source, confirmed=True, overwrite=overwrite,
                                     plugins_dir=str(host.plugins_dir),
                                     skills_runtime=str(host.skills_root),
                                     data_dir=str(host.data_dir)) or {}
    except BaseException as exc:
        out["codes"].append("install-failed")
        out["detail"] = "%s: %s" % (type(exc).__name__, exc)
        return out
    problems = report.get("problems") or []
    codes = []
    for code in codes_of(problems):
        ident = str(report.get("id") or "")
        if code == "exists" and ident and ident in seen_ids:
            codes.append("duplicate-id")
            continue
        if code == "install-failed":
            refined = security_code(detail_of(problems, code))
            codes.append(refined or code)
            continue
        codes.append(code)
    out["codes"] = codes
    out["landed"] = str(report.get("landed") or "")
    out["ok"] = bool(out["landed"]) and not problems
    out["load"] = report.get("load") or {}
    out["target_dir"] = str(report.get("target_dir") or "")
    out["notices"] = codes_of(report.get("notices"))
    out["tools"] = [str(item.get("id") or item) if isinstance(item, dict) else str(item)
                    for item in (report.get("tools") or [])]
    if problems:
        out["detail"] = detail_of(problems, codes_of(problems)[0])
    return out


def check_l3_extension(host: Host, landed: str, tools: list, call_limit: int,
                       timeout: float) -> dict:
    """宿主真加载 + 真调：import_probe 拿注册工具，再逐个真空参数调用。"""
    out = {"ok": False, "status": "fail", "codes": [], "detail": "",
           "tools_registered": 0, "calls": []}
    from src import extension_host, qqagent_host
    folder = Path(landed) if landed else None
    if folder is None or not folder.is_dir():
        out["codes"].append("extension-missing")
        return out
    if not host.resolvable():
        out["codes"].append("node-missing" if not host.node() else "host-script-missing")
        out["status"] = "unverified"
        return out
    probe = qqagent_host.import_probe(folder, host.host_root, timeout=timeout,
                                      data_dir=str(host.data_dir),
                                      extension_id=folder.name) or {}
    if not probe.get("ok"):
        error = probe.get("error") or {}
        message = str(error.get("message") or probe.get("loadError") or "")
        out["codes"].append(probe_code(message))
        out["detail"] = message[:300]
        return out
    registered = [{"id": str(item.get("id") or ""), "name": str(item.get("name") or "")}
                  for item in (probe.get("tools") or [])]
    out["tools_registered"] = len(registered)
    hooks = len(probe.get("hooks") or [])
    providers = len(probe.get("providers") or [])
    out["hooks"] = hooks
    out["providers"] = providers
    if not registered:
        if not hooks and not providers:
            out["codes"].append("no-capability")
            out["detail"] = "宿主加载成功，但 tools/hooks/providers 全空"
            return out
        out["codes"].append("no-callable-tool")
        out["status"] = "unverified"
        out["detail"] = "只注册了 %d 个 hook / %d 个 provider，没有可真调的工具" % (hooks, providers)
        return out
    callable_tools = [item for item in registered
                      if item["id"] and not DANGER.search(item["id"])
                      and not DANGER.search(item["name"] or "")]
    if not callable_tools:
        out["codes"].append("call-skipped-danger")
        out["status"] = "unverified"
        return out
    for item in callable_tools[:max(1, call_limit)]:
        try:
            result = extension_host.run_capability_sync(
                node=host.node(), host_script=str(host.script()), extension=str(folder),
                source_dir=str(host.host_root), capability="tool",
                capability_arg=item["id"], payload={}, timeout=timeout, kind="tool",
                data_dir=str(host.data_dir), extension_id=folder.name, path_label="spec-check")
        except Exception as exc:  # noqa: BLE001 - 单次调用失败不影响整包判定
            result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        value = result.get("value")
        biz = isinstance(value, dict) and bool(value.get("isError"))
        out["calls"].append({"tool": item["id"], "ok": bool(result.get("ok")),
                             "biz_error": biz,
                             "error": str(result.get("error") or "")[:200]})
        if not result.get("ok"):
            out["codes"].append("call-failed")
            out["detail"] = str(result.get("error") or "")[:300]
            return out
        if biz:
            text = str(value.get("content") or "")
            out["codes"].append(biz_code(text))
            out["detail"] = text[:300]
            return out
    out["ok"] = True
    out["status"] = "pass"
    return out


def check_l3(target: Path, kind: str, l2: dict, host: Host, call_limit: int,
             timeout: float) -> dict:
    """按家族分流：只有扩展能被宿主真加载真调；其余家族如实标注验证手段。"""
    out = {"ok": False, "status": "fail", "codes": [], "detail": "",
           "tools_registered": 0, "calls": []}
    if kind == "extension-js":
        return check_l3_extension(host, l2.get("landed") or "", l2.get("tools") or [],
                                  call_limit, timeout)
    load = l2.get("load") or {}
    if kind == "plugin-py":
        if load.get("ok"):
            out.update(ok=True, status="pass")
            out["detail"] = "Python 侧加载成功；本脚本不跨语言真调"
            return out
        out["codes"].append("python-load-failed")
        out["detail"] = str(load.get("error") or "")[:300]
        return out
    if kind == "skill":
        tier = str(load.get("code") or "unclassified")
        if tier == "T1-ready":
            out.update(ok=True, status="pass")
            return out
        out["codes"].append(tier if tier in SKILL_TIERS else "skill-not-ready")
        out["detail"] = str(load.get("error") or "")[:300]
        return out
    out["codes"].append("no-host-call")
    out["status"] = "unverified"
    out["detail"] = "%s 家族在本脚本里没有真调通道" % kind
    return out


def check_one(target: Path, label: str, host: Host, opts) -> dict:
    row = {"corpus": label, "name": target.name, "path": str(target), "kind": "unknown",
           "id": "", "l1": False, "l2": False, "l3": False, "l3_status": "skipped",
           "l1_codes": [], "l2_codes": [], "l3_codes": [], "notices": [],
           "details": {}}
    l1 = check_l1(target, host)
    row["kind"], row["id"] = l1["kind"], l1["id"]
    row["l1"], row["l1_codes"], row["notices"] = l1["ok"], l1["codes"], l1["notices"]
    if l1["codes"]:
        row["details"]["l1"] = l1["detail"]
    if not l1["ok"] or "l2" not in opts.layers:
        return row
    l2 = check_l2(target, host, opts.seen_ids, opts.overwrite)
    row["l2"], row["l2_codes"] = l2["ok"], l2["codes"]
    if l2["codes"]:
        row["details"]["l2"] = l2["detail"]
    if l2["ok"] and row["id"]:
        opts.seen_ids.setdefault(row["id"], row["name"])
    if not l2["ok"] or "l3" not in opts.layers:
        return row
    l3 = check_l3(target, row["kind"], l2, host, opts.call_limit, opts.timeout)
    row["l3"], row["l3_codes"], row["l3_status"] = l3["ok"], l3["codes"], l3["status"]
    row["l3_tools_registered"] = l3.get("tools_registered") or 0
    row["l3_calls"] = l3.get("calls") or []
    if l3["detail"]:
        row["details"]["l3"] = l3["detail"]
    return row


def summarize(rows: list, elapsed: float, errors: list) -> dict:
    kinds = Counter(row["kind"] for row in rows)
    matrix = OrderedDict()
    for family in FAMILIES:
        members = [row for row in rows if row["kind"] == family]
        if not members:
            continue
        matrix[family] = {
            "total": len(members),
            "l1": sum(1 for row in members if row["l1"]),
            "l2": sum(1 for row in members if row["l2"]),
            "l3": sum(1 for row in members if row["l3"]),
            "l3_fail": sum(1 for row in members if row["l2"] and row["l3_status"] == "fail"),
            "l3_unverified": sum(1 for row in members if row["l3_status"] == "unverified"),
        }
    stuck = [row for row in rows if row["l1"] and row["l2"] and not row["l3"]
             and row["l3_status"] == "fail"]
    groups = OrderedDict()
    for row in stuck:
        code = (row["l3_codes"] or ["unknown"])[0]
        groups.setdefault(code, []).append(row["name"])
    unverified = [row for row in rows if row["l3_status"] == "unverified"]
    return {
        "total": len(rows),
        "kinds": dict(sorted(kinds.items())),
        "matrix": matrix,
        "l1_ok": sum(1 for row in rows if row["l1"]),
        "l2_ok": sum(1 for row in rows if row["l2"]),
        "l3_ok": sum(1 for row in rows if row["l3"]),
        "l1_codes": dict(sorted(Counter(code for row in rows for code in row["l1_codes"]).items())),
        "l2_codes": dict(sorted(Counter(code for row in rows for code in row["l2_codes"]).items())),
        "l3_codes": dict(sorted(Counter(code for row in rows for code in row["l3_codes"]).items())),
        "notices": dict(sorted(Counter(code for row in rows for code in row["notices"]).items())),
        "stuck_total": len(stuck),
        "stuck_groups": OrderedDict((code, {"count": len(names), "names": sorted(names),
                                            "samples": sorted(names)[:3]})
                                    for code, names in sorted(groups.items(),
                                                              key=lambda kv: -len(kv[1]))),
        "unverified_total": len(unverified),
        "unverified_by_code": dict(sorted(Counter(code for row in unverified
                                                   for code in row["l3_codes"]).items())),
        "errors": errors,
        "elapsed_s": round(elapsed, 1),
        "rows": rows,
    }


def print_matrix(summary: dict) -> None:
    print("=== 三层合规矩阵（%d 包）===" % summary["total"])
    print("%-14s %6s %7s %7s %7s %8s %8s" % ("family", "total", "L1过", "L2过", "L3过",
                                             "L3不过", "L3未验"))
    for family, stat in summary["matrix"].items():
        print("%-14s %6d %7d %7d %7d %8d %8d" % (
            family, stat["total"], stat["l1"], stat["l2"], stat["l3"], stat["l3_fail"],
            stat["l3_unverified"]))
    print("%-14s %6d %7d %7d %7d %8d %8d" % (
        "合计", summary["total"], summary["l1_ok"], summary["l2_ok"], summary["l3_ok"],
        summary["stuck_total"], summary["unverified_total"]))
    print()
    print("L1 原因码 %s" % json.dumps(summary["l1_codes"], ensure_ascii=False))
    print("L2 原因码 %s" % json.dumps(summary["l2_codes"], ensure_ascii=False))
    print("L3 原因码 %s" % json.dumps(summary["l3_codes"], ensure_ascii=False))
    print("告知码   %s" % json.dumps(summary["notices"], ensure_ascii=False))
    print()
    print("=== 「L1 过 L2 过 L3 不过」%d 包（按原因分组，完整清单见 --json）==="
          % summary["stuck_total"])
    for code, group in summary["stuck_groups"].items():
        print("[%-26s %3d] %s" % (code, group["count"], " / ".join(group["samples"])))
    if summary["unverified_total"]:
        print()
        print("L3 未验证 %d 包：%s" % (summary["unverified_total"],
                                      json.dumps(summary["unverified_by_code"],
                                                 ensure_ascii=False)))


def print_single(row: dict) -> None:
    print("包名     %s" % row["name"])
    print("家族/ID  %s / %s" % (row["kind"], row["id"] or "(无)"))
    for layer in LAYERS:
        if layer == "l3" and row["l3_status"] == "skipped":
            print("L3       SKIP（L2 未通过或未请求）")
            continue
        verdict = "PASS" if row[layer] else ("UNVERIFIED" if
                                            (layer == "l3" and row["l3_status"] == "unverified")
                                            else "FAIL")
        codes = row["%s_codes" % layer] or []
        print("%-8s %s%s" % (layer.upper(), verdict,
                             ("  ← " + ",".join(codes)) if codes else ""))
        detail = (row["details"] or {}).get(layer)
        if detail and not row[layer]:
            print("         详情：%s" % detail)
    if row["notices"]:
        print("告知码   %s" % ",".join(row["notices"]))
    if row.get("l3_calls"):
        print("真调     %s" % json.dumps(row["l3_calls"], ensure_ascii=False))


def print_codes() -> None:
    print("%-30s %-4s %s" % ("原因码", "层", "含义"))
    for code, (layer, meaning) in CODE_LAYERS.items():
        print("%-30s %-4s %s" % (code, layer.upper(), meaning))
    print("%-30s %-4s %s" % ("T2-web-fetch…T6-disabled", "L3", "技能分层未达 T1-ready"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="插件规格三层合规测量（只读）")
    parser.add_argument("targets", nargs="*", help="单包目录或 zip（留空则跑默认语料）")
    parser.add_argument("--corpus", action="append", default=[],
                        help="语料目录，可多次；给了就不再跑默认三处语料")
    parser.add_argument("--json", default="", help="把完整结果写到这里")
    parser.add_argument("--work", default="", help="隔离宿主根，默认 .tmp/inv/conformance-host")
    parser.add_argument("--layers", default="l1,l2,l3", help="要跑的层，默认全跑")
    parser.add_argument("--limit", type=int, default=0, help="最多测几个包")
    parser.add_argument("--call-limit", type=int, default=1, help="每包最多真调几个工具")
    parser.add_argument("--timeout", type=float, default=20.0, help="单次宿主调用超时秒数")
    parser.add_argument("--overwrite", action="store_true", help="L2 允许覆盖同名落点")
    parser.add_argument("--reuse", action="store_true",
                        help="沿用 --work 里已有的宿主根（默认每轮清空，避免旧落点干扰 L2）")
    parser.add_argument("--codes", action="store_true", help="打印原因码字典后退出")
    args = parser.parse_args(argv)

    if args.codes:
        print_codes()
        return 0

    opts = argparse.Namespace(
        layers={item.strip().lower() for item in args.layers.split(",") if item.strip()},
        call_limit=max(1, args.call_limit), timeout=float(args.timeout),
        overwrite=bool(args.overwrite), seen_ids={})
    unknown = opts.layers - set(LAYERS)
    if unknown:
        raise SystemExit("未知的层：%s（只支持 %s）" % (",".join(sorted(unknown)),
                                                       ",".join(LAYERS)))

    single = bool(args.targets)
    if single:
        targets = [("target", Path(item)) for item in args.targets]
        for _, item in targets:
            if not item.exists():
                raise SystemExit("目标不存在：%s" % item)
        work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="spec-check-"))
        host = Host(work)
        host.prepare()
    else:
        corpora = ([(Path(base).name, Path(base)) for base in args.corpus] if args.corpus
                   else DEFAULT_CORPORA)
        targets = collect_targets(corpora)
        work = Path(args.work) if args.work else ROOT / ".tmp" / "inv" / "conformance-host"
        if work.exists() and not args.reuse:
            shutil.rmtree(work, ignore_errors=True)
            print("已清空旧宿主根：%s" % work)
        host = Host(work)
        host.prepare()
    if args.limit:
        targets = targets[:args.limit]

    started = time.perf_counter()
    rows, errors = [], []
    for index, (label, target) in enumerate(targets, 1):
        try:
            rows.append(check_one(target, label, host, opts))
        except BaseException as exc:  # 单包炸掉不许带倒整轮
            errors.append({"name": target.name, "error": "%s: %s" % (type(exc).__name__, exc)})
            rows.append({"corpus": label, "name": target.name, "path": str(target),
                         "kind": "unknown", "id": "", "l1": False, "l2": False, "l3": False,
                         "l3_status": "fail", "l1_codes": ["check-crashed"], "l2_codes": [],
                         "l3_codes": [], "notices": [], "details": {}})
        if not single and index % 100 == 0:
            print("... %d/%d 已测（%.1fs）" % (index, len(targets),
                                              time.perf_counter() - started), flush=True)
            _flush(args.json, rows, started, errors, partial=True)

    summary = summarize(rows, time.perf_counter() - started, errors)
    if single:
        for row in rows:
            print_single(row)
    else:
        print_matrix(summary)
    if errors:
        print("异常包 %d：%s" % (len(errors), json.dumps(errors[:5], ensure_ascii=False)))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        print("结果 -> %s" % args.json)
        _trim(summary)
    if single and not args.work:
        shutil.rmtree(host.data_dir.parent, ignore_errors=True)
    return 0


def _trim(summary: dict) -> None:
    """--json 之后回收行内的调用明细，让控制台只留一层摘要。"""
    summary.pop("rows", None)


def _flush(path_text: str, rows: list, started: float, errors: list, partial: bool) -> None:
    if not path_text:
        return
    target = Path(path_text)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"partial": partial, "rows": rows,
                                  "errors": errors,
                                  "elapsed_s": round(time.perf_counter() - started, 1)},
                                 ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
