"""plugin_core.cli — python -m plugin_core.cli demo|selftest|run（规格 §9）。

纯标准库；所有子命令都只走 mock 传输层与 mock 模型，不联网、不接真 QQ。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from . import selftest as selftest_module
from .arbiter import reason_label
from .audit import Log
from .host import Host
from .mock import MockModel, MockSource, MockTransport

DEMO_SOURCE = '''"""demo 用最小插件：固定回复。"""

from plugin_core.sdk import Action


def setup(api) -> None:
    def generate(ctx):
        return [Action.send_text(ctx.session["key"], %(reply)r)]

    def bid(ctx):
        return api.bid("main", %(priority)d, generate=generate, reason=%(reason)r)

    api.declare_inbound(bid)
'''


def demo_manifest(plugin_id, name, priority):
    return {
        "id": plugin_id,
        "name": name,
        "version": "1.0.0",
        "api_version": 1,
        "author": {"name": "plugin_core demo", "contact": "demo@example.invalid"},
        "description": "demo 用的最小竞标插件",
        "entry": "plugin.py:setup",
        "permissions": ["read_message", "send_message"],
        "capabilities": [{"stage": "inbound", "produces": "main", "priority": priority}],
        "side": "both",
        "data_policy": "retain",
        "upstream": {"protocol": "mock", "kernel": "plugin_core", "range": ">=1.0.0 <2.0.0"},
        "native": True,
        "storage": {},
        "config_schema": {},
    }


def write_demo_plugin(plugins_dir, plugin_id, name, priority, reply):
    target = Path(plugins_dir) / plugin_id
    target.mkdir(parents=True, exist_ok=True)
    manifest = demo_manifest(plugin_id, name, priority)
    (target / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (target / "plugin.py").write_text(
        DEMO_SOURCE % {"priority": priority, "reply": reply, "reason": "demo 固定竞标"},
        encoding="utf-8",
    )
    return target


def cmd_demo(args) -> int:
    """打印三行：竞标名单 / 胜出 / 落败及原因。"""
    with tempfile.TemporaryDirectory() as tmp:
        plugins = Path(tmp) / "plugins"
        plugins.mkdir()
        write_demo_plugin(plugins, "a", "A", 80, "A 的回复")
        write_demo_plugin(plugins, "b", "B", 20, "B 的回复")
        host = Host(plugins, Path(tmp) / "data", MockTransport(), MockModel())
        host.start()
        record = host.handle(MockSource().message("你好"))
        display = {"a": "A", "b": "B"}

        def name_of(plugin_id):
            return display.get(plugin_id, plugin_id)

        bids = ", ".join(
            "%s(%s,%d)" % (name_of(item["plugin"]), item["produces"], item["priority"])
            for item in record["bids"]
        )
        print("竞标：[%s]" % bids)
        winner = record["winner"] or record["silent_winner"]
        if winner:
            print("胜出：%s" % name_of(winner["plugin"]))
        else:
            print("胜出：无")
        losers = []
        for item in record["dropped"]:
            label = item.get("label") or reason_label(item["reason"])
            losers.append("%s（%s）" % (name_of(item["plugin"]), label))
        print("落败：%s" % ("、".join(losers) if losers else "无"))
    return 0


def cmd_run(args) -> int:
    """对一条真实文本跑一次仲裁，打印仲裁记录。"""
    logger = Log("plugin_core", sink=None)
    host = Host(
        Path(args.plugins),
        Path(args.data),
        MockTransport(),
        MockModel(),
        config={"system_prompt": args.system_prompt} if args.system_prompt else None,
        logger=logger,
    )
    snapshot = host.start()
    record = host.handle(MockSource().message(args.message))
    print("插件清单：%s" % ", ".join(item["id"] for item in snapshot))
    print("拒载：%d 个" % len(host.refused))
    print(json.dumps(record, ensure_ascii=False, indent=2))
    print("实际发出：")
    for item in host.transport.history:
        print("  %s → %s" % (item["conversation"], item["text"]))
    if args.verbose:
        for line in logger.lines():
            print(line)
    return 0


def cmd_selftest(args) -> int:
    return selftest_module.run_all(only=args.only, out_dir=Path(args.out))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m plugin_core.cli",
        description="独立插件内核参考实现：demo / selftest / run",
    )
    sub = parser.add_subparsers(dest="command")

    demo = sub.add_parser("demo", help="打印一次真实仲裁的竞标/胜出/落败")
    demo.set_defaults(func=cmd_demo)

    selftest = sub.add_parser("selftest", help="跑 T1–T10 自检")
    selftest.add_argument("--all", action="store_true", help="跑全部 T1–T10")
    selftest.add_argument("--only", default=None, help="只跑某一项，例如 T8")
    selftest.add_argument("--out", default="selftest-out", help="证据目录（默认 ./selftest-out）")
    selftest.set_defaults(func=cmd_selftest)

    run = sub.add_parser("run", help="对一条文本跑一次仲裁")
    run.add_argument("--plugins", required=True, help="插件目录")
    run.add_argument("--data", required=True, help="数据目录")
    run.add_argument("--message", required=True, help="入站文本")
    run.add_argument("--system-prompt", default=None, help="默认人格的系统提示")
    run.add_argument("--verbose", action="store_true", help="打印内核日志")
    run.set_defaults(func=cmd_run)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 2
    if args.command == "selftest" and not args.all and not args.only:
        parser.error("selftest 需要 --all 或 --only Tn")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
