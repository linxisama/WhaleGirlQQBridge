"""plugin_core.selftest — T1–T10 自检（规格 §10）。

每一项都跑真实现路径：真 loader、真仲裁、真出站管道、真 KV 落盘，只用 mock 传输层与 mock 模型。
证据写到 <cwd>/selftest-out/。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import traceback
from pathlib import Path

from .audit import Log
from .host import DEFAULT_SYSTEM_PROMPT, Host, normalize_message
from .mock import MockModel, MockSource, MockTransport

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
EXAMPLES_DIR = PACKAGE_DIR / "examples"

BASE_MANIFEST = {
    "name": "inline",
    "version": "1.0.0",
    "api_version": 1,
    "author": {"name": "selftest", "contact": "selftest@example.invalid"},
    "description": "selftest 内联插件",
    "entry": "plugin.py:setup",
    "permissions": ["read_message", "send_message"],
    "capabilities": [{"stage": "inbound", "produces": "main", "priority": 50}],
    "side": "both",
    "data_policy": "retain",
    "upstream": {"protocol": "mock", "kernel": "plugin_core", "range": ">=1.0.0 <2.0.0"},
    "native": True,
    "storage": {},
    "config_schema": {},
}


# --- 造插件 ---------------------------------------------------------
def write_inline(plugins_dir, plugin_id, source, **overrides):
    """在 plugins_dir 下造一个合法插件目录（manifest 可局部覆盖）。"""
    target = Path(plugins_dir) / plugin_id
    target.mkdir(parents=True, exist_ok=True)
    manifest = dict(BASE_MANIFEST)
    manifest.update(overrides)
    manifest["id"] = plugin_id
    manifest["name"] = overrides.get("name", plugin_id)
    (target / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (target / "plugin.py").write_text(source, encoding="utf-8")
    return target


def copy_plugin(case, name, *, patch_manifest=None):
    """把 examples/<name> 复制进本项的插件目录，可按需改 manifest。"""
    plugins = case.plugins_dir()
    target = plugins / name
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(EXAMPLES_DIR / name, target)
    if patch_manifest:
        path = target / "plugin.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(patch_manifest)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def write_config(data_dir, plugin_id, values):
    path = Path(data_dir) / "plugin_config" / ("%s.json" % plugin_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


class Case:
    """一项自检：累积失败原因与证据。"""

    def __init__(self, code, title, workdir, out_dir):
        self.code = code
        self.title = title
        self.workdir = Path(workdir)
        self.out_dir = Path(out_dir)
        self.failures = []
        self.notes = []
        self.evidence = {}

    def plugins_dir(self):
        path = self.workdir / "plugins"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def data_dir(self, name="data"):
        path = self.workdir / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def expect(self, condition, label):
        if not condition:
            self.failures.append(label)
        return bool(condition)

    def note(self, text):
        self.notes.append(text)

    def save(self, name, payload):
        self.evidence[name] = payload
        return payload

    def status(self):
        return "PASS" if not self.failures else "FAIL"

    def detail(self):
        if self.failures:
            return "%s：%s" % (self.title, "；".join(self.failures))
        return "%s：%s" % (self.title, "；".join(self.notes) or "通过")

    def write_evidence(self):
        path = self.out_dir / ("%s.json" % self.code)
        path.write_text(
            json.dumps(
                {
                    "code": self.code,
                    "title": self.title,
                    "status": self.status(),
                    "detail": self.detail(),
                    "failures": self.failures,
                    "evidence": self.evidence,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return path


# --- T1 空载七步 ----------------------------------------------------
def t1(case):
    plugins = case.plugins_dir()
    data = case.data_dir()
    transport = MockTransport()
    model = MockModel("空载下的默认回复。")
    host = Host(plugins, data, transport, model)
    snapshot = host.start()
    case.expect(transport.connected, "第1步 连上：mock 传输层 connected 为真")
    case.expect(snapshot == (), "第1步 空插件目录的冻结清单是空 tuple")

    raw = MockSource().message("你好，空载测试")
    normalized = normalize_message(raw)
    case.expect(
        set(normalized) == {"session", "sender", "message"},
        "第2步 归一化只留 session/sender/message，收到 %s" % sorted(normalized),
    )
    case.expect(
        "source" not in normalized and "raw_seq" not in normalized,
        "第2步 上游原始字段 source/raw_seq 不进入归一化结果",
    )
    record = host.handle(raw)

    case.expect(
        any("归一化完成" in line for line in host.log.lines()), "第2步 日志打出归一化字段"
    )

    prompt = model.last_prompt()
    case.expect(prompt and prompt[0].get("role") == "system", "第3步 组 prompt：首条是 system")
    case.expect(
        prompt and prompt[-1].get("role") == "user" and prompt[-1].get("content") == "你好，空载测试",
        "第3步 组 prompt：末条是用户原话",
    )
    case.save("prompt", prompt)

    case.expect(model.calls_count >= 1, "第4步 调模型：calls=%d" % model.calls_count)
    case.expect(
        transport.sent_texts() == ["空载下的默认回复。"],
        "第5步 回复发送：mock 收到 %r" % (transport.sent_texts(),),
    )

    session_path = data / "session.json"
    case.expect(session_path.is_file(), "第6步 会话落盘：session.json 存在")
    turns = 0
    if session_path.is_file():
        payload = json.loads(session_path.read_text(encoding="utf-8"))
        turns = payload.get("turns", 0)
    case.expect(turns >= 1, "第6步 会话落盘：turns=%d" % turns)
    host2 = Host(plugins, data, MockTransport(), MockModel("重建后的回复。"))
    host2.start()
    case.expect(host2.session_loaded, "第6步 重建 Host 后会话回读成功")
    case.expect(
        sum(len(bucket["turns"]) for bucket in host2.sessions.values()) == turns,
        "第6步 重建 Host 后 turns 数一致（%d）" % turns,
    )

    case.expect(
        prompt and prompt[0].get("content") == DEFAULT_SYSTEM_PROMPT,
        "第7步 默认人格：system 就是内置一句话提示",
    )
    case.expect(
        record.get("fallback") == "default-persona",
        "第7步 无插件也回话：仲裁记录 fallback=default-persona",
    )
    case.save("arbitration", record)
    case.note("7 步全过，重启后记得 %d 轮，无插件仍回话" % turns)


# --- T2 hello 3 秒内回 ----------------------------------------------
def t2(case):
    copy_plugin(case, "hello")
    transport = MockTransport()
    host = Host(case.plugins_dir(), case.data_dir(), transport, MockModel())
    started = time.perf_counter()
    host.start()
    record = host.handle(MockSource().message("随便说点什么"))
    elapsed = time.perf_counter() - started
    texts = transport.sent_texts()
    case.expect(
        texts == ["你好，我是 hello 插件。"],
        "hello 固定文本回复，实际 %r" % (texts,),
    )
    case.expect(elapsed < 3.0, "耗时 %.3fs < 3s" % elapsed)
    case.expect(record["winner"]["plugin"] == "hello", "胜出者是 hello")
    case.save("arbitration", record)
    case.note("固定文本回复，启动+仲裁 %.3fs" % elapsed)


# --- T3 未声明 send_message 被拒 + 审计 ------------------------------
def t3(case):
    copy_plugin(case, "hello", patch_manifest={"permissions": ["read_message", "store"]})
    transport = MockTransport()
    host = Host(case.plugins_dir(), case.data_dir(), transport, MockModel())
    host.start()
    record = host.handle(MockSource().message("你好"))
    denied = host.audit.select(event="denied")
    case.expect(not transport.history, "没有任何消息发出去")
    case.expect(len(denied) == 1, "审计里恰好 1 条 denied，实际 %d" % len(denied))
    if denied:
        case.expect(
            denied[0].get("permission") == "send_message",
            "denied 记录写明缺 send_message，实际 %r" % denied[0].get("permission"),
        )
    case.expect(record["sent"] == [], "仲裁记录 sent 为空")
    case.expect(host.started, "进程没崩，主体仍可用")
    case.save("denied", denied)
    case.note("动作被拒、审计 1 条、无消息发出、进程存活")


# --- T4 异常隔离 ----------------------------------------------------
BOOM_SOURCE = """from plugin_core.sdk import Action


def setup(api) -> None:
    def generate(ctx):
        return [Action.send_text(ctx.session["key"], "不该出现")]

    def bid(ctx):
        raise RuntimeError("selftest T4 故意抛异常")

    api.declare_inbound(bid)
"""


def t4(case):
    write_inline(
        case.plugins_dir(),
        "boom",
        BOOM_SOURCE,
        capabilities=[{"stage": "inbound", "produces": "main", "priority": 95}],
    )
    copy_plugin(case, "real")
    transport = MockTransport()
    host = Host(case.plugins_dir(), case.data_dir(), transport, MockModel())
    host.start()
    record = host.handle(MockSource().message("在吗"))
    failed = host.audit.select(event="hook-failed")
    case.expect(host.started, "主体没被打断")
    case.expect(
        any(item.get("plugin") == "boom" for item in failed),
        "审计 hook-failed 指向 boom，实际 %r" % [item.get("plugin") for item in failed],
    )
    case.expect(
        bool(record["sent"]) and "在的" in record["sent"][0]["text"],
        "其它插件照常回复，实际 %r" % (record["sent"],),
    )
    case.save("hook-failed", failed)
    case.note("boom 抛异常被隔离，real 正常回复")


# --- T5 静态扫描 ----------------------------------------------------
def forbidden_tokens():
    """逐字符拼装被测关键词：连编译后的常量里也不会出现完整拼写，扫描器不会自己命中自己。"""
    pieces = [
        ("o", "n", "e", "b", "o", "t"),
        ("n", "a", "p", "c", "a", "t"),
        ("s", "n", "o", "w", "l", "u", "m", "a"),
        ("a", "s", "t", "r", "b", "o", "t"),
    ]
    return ["".join(chars) for chars in pieces]


def t5(case):
    tokens = forbidden_tokens()
    hits = []
    scanned = 0
    for path in sorted(PACKAGE_DIR.rglob("*")):
        if not path.is_file() or path.suffix not in (".py", ".json", ".md", ".txt", ".cfg"):
            continue
        scanned += 1
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for token in tokens:
            if token in text:
                hits.append({"file": str(path), "token": token})
    case.expect(not hits, "命中禁用协议端关键词：%r" % (hits,))
    case.expect(scanned > 10, "实际扫到 %d 个源文件" % scanned)
    case.save("scan", {"files": scanned, "tokens": tokens, "hits": hits})
    case.note("%d 个源文件 0 命中" % scanned)


# --- T6 可摘除 ------------------------------------------------------
def t6(case):
    for name in ("hello", "persona", "real"):
        copy_plugin(case, name)
    host = Host(case.plugins_dir(), case.data_dir("data-a"), MockTransport(), MockModel("回复"))
    snapshot = host.start()
    ids = [item["id"] for item in snapshot]
    case.expect(ids == ["hello", "persona", "real"], "冻结清单 %r" % (ids,))
    shutil.rmtree(case.plugins_dir() / "persona")
    host2 = Host(case.plugins_dir(), case.data_dir("data-b"), MockTransport(), MockModel("回复"))
    snapshot2 = host2.start()
    ids2 = [item["id"] for item in snapshot2]
    case.expect(ids2 == ["hello", "real"], "删掉 persona 后冻结清单 %r" % (ids2,))
    case.expect(host2.started, "主体仍然启动")
    record = host2.handle(MockSource().message("随便说"))
    case.expect(record["winner"]["plugin"] == "hello", "剩下插件照常工作")
    case.save("snapshots", {"before": ids, "after": ids2})
    case.note("摘掉 persona 后清单 3 → 2，主体照常工作")


# --- T7 api_version 严格比对 ----------------------------------------
def t7(case):
    copy_plugin(case, "hello", patch_manifest={"api_version": 999})
    copy_plugin(case, "real")
    transport = MockTransport()
    host = Host(case.plugins_dir(), case.data_dir(), transport, MockModel())
    host.start()
    loaded = list(host.plugins)
    case.expect(loaded == ["real"], "只有 real 载入，实际 %r" % (loaded,))
    refused = host.refused
    case.expect(refused and refused[0]["code"] == "api-version-mismatch", "拒载码 api-version-mismatch")
    detail = refused[0]["detail"] if refused else ""
    case.expect("999" in detail and "!= 1" in detail, "拒载说明写明 999 != 1，实际 %r" % (detail,))
    case.expect(
        any("999" in line for line in host.log.lines()), "日志里有 api_version=999 的说明"
    )
    record = host.handle(MockSource().message("在吗"))
    case.expect(
        bool(record["sent"]) and "在的" in record["sent"][0]["text"], "其余插件照常工作"
    )
    case.save("refused", refused)
    case.note("api_version=999 拒载，real 照常回复")


# --- T8 persona + hello 仲裁 ----------------------------------------
def t8(case):
    copy_plugin(case, "hello")
    copy_plugin(case, "persona")
    transport = MockTransport()
    model = MockModel("persona 的模型回复")
    host = Host(case.plugins_dir(), case.data_dir(), transport, model)
    host.start()
    record = host.handle(MockSource().message("你好"))
    case.expect(record["winner"] is not None and record["winner"]["plugin"] == "persona",
                "胜出者是 persona（80）")
    case.expect(record["silent_winner"] is None, "没有 silent 竞标")
    dropped = {item["plugin"]: item for item in record["dropped"]}
    case.expect("hello" in dropped, "hello 出现在 dropped")
    if "hello" in dropped:
        case.expect(dropped["hello"]["reason"] == "priority-low", "hello 落败原因 priority-low")
        case.expect(dropped["hello"]["priority"] == 20, "hello 的 priority 记为 20")
        case.expect(dropped["hello"].get("winner_priority") == 80, "记录胜者 priority=80")
    case.expect(record["generated"] == ["persona"], "只有胜者生成，record.generated=%r" % (record["generated"],))
    case.expect(model.calls_count == 1, "竞标阶段不调模型：模型只被调用 1 次，实际 %d" % model.calls_count)
    case.expect(transport.sent_texts() == ["persona 的模型回复"], "只有 persona 的消息发出")
    case.expect(
        transport.history and transport.history[0]["plugin"] == "persona",
        "出站记录归属 persona",
    )
    case.save("arbitration", record)
    case.note("persona(80) 胜 hello(20)，模型只调 1 次")


# --- T9 出站改写 / 取消 / 防回灌 -------------------------------------
SHIELD_SOURCE = """def setup(api) -> None:
    def middleware(ctx, draft):
        return None

    api.declare_outbound(middleware)
"""


def t9(case):
    copy_plugin(case, "hello")
    copy_plugin(case, "filter")
    data = case.data_dir()
    write_config(data, "filter", {"rewrite_map": {"hello": "内核"}, "cancel_words": ["广告"]})
    transport = MockTransport()
    host = Host(case.plugins_dir(), data, transport, MockModel())
    host.start()
    record = host.handle(MockSource().message("说点什么"))
    case.expect(
        transport.sent_texts() == ["你好，我是 内核 插件。"],
        "出站改写生效，实际 %r" % (transport.sent_texts(),),
    )
    case.expect(
        host.outbound.passes.get("filter") == 1,
        "防回灌：改写结果不再进管道，filter 只跑 1 次，实际 %r" % (host.outbound.passes,),
    )
    case.save("rewrite", {"sent": transport.sent_texts(), "passes": dict(host.outbound.passes)})

    # 取消：命中词表就整条不发（换一份数据目录，配置跟着走）
    data_cancel = case.data_dir("data-cancel")
    write_config(data_cancel, "filter", {"rewrite_map": {}, "cancel_words": ["插件"]})
    transport2 = MockTransport()
    host2 = Host(case.plugins_dir(), data_cancel, transport2, MockModel())
    host2.start()
    record2 = host2.handle(MockSource().message("说点什么"))
    case.expect(not transport2.history, "命中 cancel_words 后什么都不发")
    case.expect(
        bool(host2.audit.select(event="outbound-cancelled")), "审计记下 outbound-cancelled"
    )

    # 短路：priority 5 的中间件先取消，priority 10 的 filter 根本不执行
    write_inline(
        case.plugins_dir(),
        "shield",
        SHIELD_SOURCE,
        permissions=["send_message"],
        capabilities=[{"stage": "outbound", "produces": "cancel", "priority": 5}],
    )
    transport3 = MockTransport()
    host3 = Host(case.plugins_dir(), case.data_dir("data-short"), transport3, MockModel())
    host3.start()
    host3.handle(MockSource().message("说点什么"))
    case.expect(not transport3.history, "短路后什么都不发")
    case.expect(
        host3.outbound.passes.get("shield") == 1,
        "shield 执行 1 次，实际 %r" % (host3.outbound.passes,),
    )
    case.expect(
        "filter" not in host3.outbound.passes,
        "短路后 filter 未执行，实际 %r" % (host3.outbound.passes,),
    )
    case.save(
        "short-circuit",
        {"cancel_sent": transport2.history, "passes": dict(host3.outbound.passes)},
    )
    case.note("改写 1 次不再回灌 / 取消 / 短路三种路径全过")


# --- T10 data_policy ------------------------------------------------
def t10(case):
    copy_plugin(case, "persona")
    copy_plugin(case, "hello")
    data = case.data_dir()
    host = Host(case.plugins_dir(), data, MockTransport(), MockModel("persona 回复"))
    host.start()
    record = host.handle(MockSource().message("你好"))
    case.expect(record["winner"]["plugin"] == "persona", "persona 先胜出")
    kv_persona = Path(host.store.kv_path("persona"))
    case.expect(kv_persona.is_file(), "persona KV 落盘")
    record2 = host.handle(MockSource().message("随便说"))
    case.expect(record2["winner"]["plugin"] == "hello", "第二条消息 hello 胜出")
    kv_hello = Path(host.store.kv_path("hello"))
    case.expect(kv_hello.is_file(), "hello KV 落盘")
    if kv_hello.is_file():
        case.expect(
            json.loads(kv_hello.read_text(encoding="utf-8")).get("hits") == 1, "hello 记下 hits=1"
        )

    info = host.uninstall("persona")
    case.expect(info["data_policy"] == "purge", "persona 的 data_policy 是 purge")
    case.expect(not kv_persona.exists(), "purge 卸载后 KV 文件被删")
    case.expect("persona" not in host.plugins, "persona 已从插件表移除")

    info2 = host.uninstall("hello")
    case.expect(info2["data_policy"] == "retain", "hello 的 data_policy 是 retain")
    case.expect(kv_hello.is_file(), "retain 卸载后 KV 文件保留")

    host2 = Host(case.plugins_dir(), data, MockTransport(), MockModel("persona 回复"))
    host2.start()
    case.expect(
        host2.store.kv_data("hello").get("hits") == 1, "重装 hello 读回 retain 的 hits=1"
    )
    case.expect(host2.store.kv_data("persona") == {}, "persona 重装从空 KV 开始，不串味")
    case.save(
        "uninstall",
        {"persona": info, "hello": info2, "kv": {"hello": host2.store.kv_data("hello")}},
    )
    case.note("purge 清 KV / retain 保留 / 重装不串味")


TESTS = [
    ("T1", "空载七步", t1),
    ("T2", "hello 3 秒内回", t2),
    ("T3", "未声明 send_message 被拒", t3),
    ("T4", "异常隔离", t4),
    ("T5", "静态扫描零命中", t5),
    ("T6", "可摘除", t6),
    ("T7", "api_version 严格比对", t7),
    ("T8", "persona+hello 仲裁", t8),
    ("T9", "出站改写/取消/防回灌", t9),
    ("T10", "data_policy", t10),
]


def run_all(only=None, out_dir=None) -> int:
    out = Path(out_dir) if out_dir is not None else Path.cwd() / "selftest-out"
    out.mkdir(parents=True, exist_ok=True)
    selected = TESTS
    if only:
        wanted = str(only).strip().upper()
        selected = [item for item in TESTS if item[0] == wanted]
        if not selected:
            print("没有 %r 这项自检；可选：%s" % (only, ", ".join(code for code, _, _ in TESTS)))
            return 2

    base = Path(tempfile.mkdtemp(prefix="plugin-core-selftest-"))
    summary = []
    failures = 0
    try:
        for code, title, fn in selected:
            workdir = base / code
            workdir.mkdir(parents=True, exist_ok=True)
            case = Case(code, title, workdir, out)
            try:
                fn(case)
            except Exception:
                case.failures.append(
                    "自检自身抛异常：%s" % traceback.format_exc(limit=4).strip().replace("\n", " | ")
                )
            case.write_evidence()
            if case.failures:
                failures += 1
            print("%s %s %s" % (case.code, case.status(), case.detail()))
            summary.append(
                {
                    "code": case.code,
                    "title": case.title,
                    "status": case.status(),
                    "failures": case.failures,
                    "detail": case.detail(),
                }
            )
    finally:
        shutil.rmtree(base, ignore_errors=True)

    (out / "summary.json").write_text(
        json.dumps({"tests": summary, "failures": failures, "out": str(out)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    total = len(selected)
    if failures:
        print("失败 %d / %d 项：%s" % (failures, total, ", ".join(item["code"] for item in summary if item["failures"])))
        return 1
    print("全部通过：%d / %d 项（证据在 %s）" % (total, total, out))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(run_all(only=None if "--all" in sys.argv else None))
