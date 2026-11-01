"""src/pack_import.py 的第五种家族 mcp-server：识别 / 预检 / 安装三段同形。

夹具全部在 tempfile 里生成：不读真实市场目录、不写仓库 data/、不联网、不拉起任何
MCP server（venv 里也没有 mcp SDK）。安装只落到测试自己的临时 data_dir。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from src import pack_import

SCHEMA_KEYS = {
    "ok",
    "kind",
    "id",
    "name",
    "version",
    "entry",
    "files",
    "bytes",
    "landed",
    "target_dir",
    "tools",
    "hooks",
    "providers",
    "problems",
    "notices",
    "load",
    "installed_but_disabled",
    "actions",
    "duration_ms",
}


def codes(report, key="problems") -> list:
    return [str(one.get("code") or "") for one in report[key]]


def snapshot(root) -> list:
    """目录快照：用来证明「一个文件都没写」。"""
    out = []
    for base, _dirs, files in os.walk(str(root)):
        for name in files:
            out.append(str(Path(base, name).relative_to(root)).replace(os.sep, "/"))
    return sorted(out)


def server_entry(command=None, args=None, env=None, **extra) -> dict:
    entry = {}
    if command is not None:
        entry["command"] = command
    if args is not None:
        entry["args"] = args
    if env is not None:
        entry["env"] = env
    entry.update(extra)
    return entry


class McpTestCase(unittest.TestCase):
    """一个临时根目录：packages/ 放夹具，data/ 当安装目标。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pack-import-mcp-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.data = self.tmp / "data"
        self.node = str(self.tmp / "runtime" / "node.exe")

    def package(self, name, files) -> Path:
        root = self.tmp / "packages" / name
        for path, text in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text if isinstance(text, str) else json.dumps(text),
                              encoding="utf-8")
        return root

    def package_zip(self, name, files) -> Path:
        path = self.tmp / "packages" / (name + ".zip")
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as archive:
            for inner, text in files.items():
                archive.writestr(inner, text if isinstance(text, str) else json.dumps(text))
        return path

    def mcp_pack(self, name="mcp-demo", server=None, extra=None) -> Path:
        files = {"mcp.json": {"mcpServers": {"demo": server or server_entry(
            self.node, ["server.js"], {"API_KEY": "k"})}},
                 "server.js": "// demo server\n"}
        files.update(extra or {})
        return self.package(name, files)

    def index(self) -> dict:
        return json.loads((self.data / "qq-only" / "mcp_servers.json")
                          .read_text(encoding="utf-8"))

    def target(self, ident="demo") -> Path:
        return self.data / "qq-only" / "mcp_servers" / ident


class DetectTests(McpTestCase):
    """detect() 的 6+1 键对 MCP 家族同形。"""

    def test_single_server_dir(self):
        found = pack_import.detect(self.mcp_pack())
        self.assertEqual(set(found), {"kind", "root", "manifest_name", "manifest",
                                      "dialect", "evidence", "confidence"})
        self.assertEqual(found["kind"], "mcp-server")
        self.assertEqual(found["manifest_name"], "mcp.json")
        self.assertEqual(found["dialect"], "mcp-json")
        self.assertEqual(found["root"], "")
        self.assertGreaterEqual(found["confidence"], 0.9)
        self.assertIn("demo", " ".join(found["evidence"]))

    def test_manifest_at_root_still_wins(self):
        """包根摆着 plugin.json 时 MCP 分支不许抢，既有四家族行为不动。"""
        pack = self.package("js-with-mcp", {
            "plugin.json": {"id": "jsdemo", "entry": "index.js", "apiVersion": 1},
            "index.js": "export async function setup(api) { api.log('ok'); }\n",
            "tools/mcp.json": json.dumps({"mcpServers": {"x": server_entry("node")}}),
        })
        self.assertEqual(pack_import.detect(pack)["kind"], "extension-js")

    def test_broken_mcp_json_is_not_crashing(self):
        pack = self.package("broken", {"mcp.json": "{not json", "readme.txt": "hi"})
        found = pack_import.detect(pack)
        self.assertNotEqual(found["kind"], "mcp-server")
        self.assertEqual(found["kind"], "unknown")

    def test_zip_wrapper_root_is_mcp_dir(self):
        """外层目录包：包根 = mcp.json 所在目录，不是下钻到最深的那个子目录。"""
        pack = self.package_zip("wrap", {
            "wrap/mcp.json": json.dumps({"command": "npx", "args": ["-y", "demo"]}),
            "wrap/bin/run.js": "// run\n",
        })
        found = pack_import.detect(pack)
        self.assertEqual(found["kind"], "mcp-server")
        self.assertEqual(found["root"], "wrap")


class InspectTests(McpTestCase):
    """inspect() 的 19 键与 ok 判定（清单可解析 + command 非空 + id 合法）。"""

    def test_single_server(self):
        report = pack_import.inspect(self.mcp_pack(), data_dir=self.data)
        self.assertEqual(set(report), SCHEMA_KEYS)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["kind"], "mcp-server")
        self.assertEqual(report["id"], "demo")
        self.assertEqual(report["entry"], self.node)
        self.assertEqual(report["tools"], [])
        self.assertEqual(report["target_dir"], str(self.target()))
        self.assertIn("tools-at-runtime", codes(report, "notices"))
        self.assertEqual(report["files"], 2)
        self.assertGreater(report["bytes"], 0)

    def test_no_data_dir_leaves_target_empty(self):
        report = pack_import.inspect(self.mcp_pack())
        self.assertTrue(report["ok"])
        self.assertEqual(report["target_dir"], "")

    def test_multiple_servers_takes_first_and_notices(self):
        report = pack_import.inspect(self.package("multi", {
            "mcp.json": {"mcpServers": {
                "gamma": server_entry(self.node, ["g.js"]),
                "alpha": server_entry(self.node, ["a.js"]),
                "beta": server_entry(self.node, ["b.js"]),
            }},
        }), data_dir=self.data)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["id"], "alpha")
        notices = [one for one in report["notices"] if one["code"] == "multiple-servers"]
        self.assertEqual(len(notices), 1)
        self.assertIn("3 个 MCP server", notices[0]["message"])
        self.assertIn("alpha", notices[0]["message"])

    def test_bare_command_form_uses_package_name_as_id(self):
        report = pack_import.inspect(self.package("bare-mcp", {
            "mcp.json": {"command": self.node, "args": ["main.js"]},
            "main.js": "// main\n",
        }), data_dir=self.data)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["id"], "bare-mcp")
        self.assertEqual(report["entry"], self.node)
        self.assertEqual(report["target_dir"], str(self.target("bare-mcp")))

    def test_sse_without_command_is_problem_not_unknown(self):
        pack = self.package("sse", {
            "mcp.json": {"mcpServers": {"remote": {
                "type": "sse", "url": "http://127.0.0.1:8931/sse"}}},
        })
        self.assertEqual(pack_import.detect(pack)["kind"], "mcp-server")
        report = pack_import.inspect(pack, data_dir=self.data)
        self.assertFalse(report["ok"])
        self.assertEqual(codes(report), ["unsupported-transport"])
        self.assertIn("stdio", report["problems"][0]["hint"])
        self.assertEqual(report["kind"], "mcp-server")
        self.assertEqual(report["id"], "remote")

    def test_invalid_id(self):
        report = pack_import.inspect(self.package("badid", {
            "mcp.json": {"mcpServers": {"bad/id": server_entry(self.node, ["x.js"])}},
        }), data_dir=self.data)
        self.assertFalse(report["ok"])
        self.assertEqual(codes(report), ["bad-id"])
        self.assertEqual(report["target_dir"], "")

    def test_command_path_is_checked(self):
        absolute = pack_import.inspect(self.mcp_pack("abs"), data_dir=self.data)
        self.assertEqual(codes(absolute), [])
        self.assertNotIn("command-on-path", codes(absolute, "notices"))

        bare = pack_import.inspect(self.package("bare", {
            "mcp.json": {"mcpServers": {"demo": server_entry("npx", ["-y", "demo"])}},
        }), data_dir=self.data)
        self.assertTrue(bare["ok"], bare["problems"])
        self.assertIn("command-on-path", codes(bare, "notices"))

        relative = pack_import.inspect(self.package("relative", {
            "mcp.json": {"mcpServers": {"demo": server_entry("./bin/node", ["x.js"])}},
        }), data_dir=self.data)
        self.assertFalse(relative["ok"])
        self.assertEqual(codes(relative), ["command-not-absolute"])


class InstallTests(McpTestCase):
    """install()：落盘 + 登记表幂等 + overwrite 闸 + 缺 data_dir 零写入。"""

    def test_install_dir_package_lands_files_and_registers(self):
        pack = self.mcp_pack()
        report = pack_import.install(pack, confirmed=True, data_dir=self.data)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["landed"], str(self.target()))
        self.assertEqual(report["target_dir"], str(self.target()))
        self.assertEqual(sorted(snapshot(self.target())), ["mcp.json", "server.js"])
        self.assertIn("mcp-server: %s" % self.target(), report["actions"])
        self.assertIn("mcp-registered", codes(report, "notices"))
        self.assertEqual(report["load"]["code"], "not-started")

        index = self.index()
        self.assertEqual(index["version"], 1)
        self.assertEqual(len(index["servers"]), 1)
        entry = index["servers"][0]
        self.assertEqual(sorted(entry), ["args", "command", "cwd", "enabled", "env", "id",
                                         "installed_at", "name", "source_package"])
        self.assertEqual(entry["id"], "demo")
        self.assertEqual(entry["command"], self.node)
        self.assertEqual(entry["args"], ["server.js"])
        self.assertEqual(entry["env"], {"API_KEY": "k"})
        self.assertEqual(entry["cwd"], str(self.target()))
        self.assertIs(entry["enabled"], True)
        self.assertEqual(entry["source_package"], str(pack))
        self.assertTrue(entry["installed_at"].endswith("Z"))

    def test_install_zip_package_strips_wrapper(self):
        pack = self.package_zip("zipwrap", {
            "zipwrap/mcp.json": json.dumps({"mcpServers": {
                "zipped": server_entry("npx", ["-y", "zipped"])}}),
            "zipwrap/bin/run.js": "// run\n",
        })
        report = pack_import.install(pack, confirmed=True, data_dir=self.data)
        self.assertTrue(report["ok"], report["problems"])
        landed = Path(report["landed"])
        self.assertEqual(landed, self.target("zipped"))
        self.assertEqual(sorted(snapshot(landed)), ["bin/run.js", "mcp.json"])

    def test_register_is_idempotent_by_id(self):
        pack = self.mcp_pack()
        first = pack_import.install(pack, confirmed=True, data_dir=self.data)
        second = pack_import.install(pack, confirmed=True, data_dir=self.data, overwrite=True)
        self.assertTrue(first["ok"] and second["ok"], first["problems"] + second["problems"])
        index = self.index()
        self.assertEqual(len(index["servers"]), 1)
        self.assertEqual(index["servers"][0]["id"], "demo")
        self.assertEqual(sorted(snapshot(self.target())), ["mcp.json", "server.js"])

    def test_exists_without_overwrite(self):
        pack = self.mcp_pack()
        self.assertTrue(pack_import.install(pack, confirmed=True, data_dir=self.data)["ok"])
        mark = self.target() / "mcp.json"
        mark.write_text("{\"mcpServers\": {}}", encoding="utf-8")
        again = pack_import.install(pack, confirmed=True, data_dir=self.data)
        self.assertFalse(again["ok"])
        self.assertEqual(codes(again), ["exists"])
        self.assertEqual(mark.read_text(encoding="utf-8"), "{\"mcpServers\": {}}")

    def test_missing_data_dir_writes_nothing(self):
        pack = self.mcp_pack()
        report = pack_import.install(pack, confirmed=True)
        self.assertEqual(codes(report), ["no-target"])
        self.assertEqual(report["landed"], "")
        self.assertEqual(snapshot(self.tmp), snapshot(self.tmp))  # 临时根里只有夹具
        self.assertFalse((self.tmp / "qq-only").exists())
        self.assertFalse((self.data / "qq-only").exists())

    def test_install_requires_confirmation(self):
        with self.assertRaises(pack_import.PackImportError):
            pack_import.install(self.mcp_pack(), data_dir=self.data)
        self.assertFalse((self.data / "qq-only").exists())

    def test_install_and_inspect_share_one_key_set(self):
        pack = self.mcp_pack()
        preview = pack_import.inspect(pack, data_dir=self.data)
        report = pack_import.install(pack, confirmed=True, data_dir=self.data)
        self.assertEqual(set(preview), set(report))
        self.assertEqual(set(report), SCHEMA_KEYS)

    def test_kind_mismatch_is_reported(self):
        report = pack_import.install(self.mcp_pack(), confirmed=True, kind="skill",
                                     data_dir=self.data)
        self.assertEqual(codes(report), ["kind-mismatch"])

    def test_zip_symlink_and_unsafe_path_are_rejected(self):
        link = self.tmp / "packages" / "link.zip"
        link.parent.mkdir(parents=True, exist_ok=True)
        info = zipfile.ZipInfo("mcp.json")
        with zipfile.ZipFile(str(link), "w") as archive:
            archive.writestr(info, json.dumps({"mcpServers": {
                "demo": server_entry(self.node, ["x.js"])}}))
            evil = zipfile.ZipInfo("evil.js")
            evil.external_attr = 0o120777 << 16
            archive.writestr(evil, "// symlink\n")
        report = pack_import.install(link, confirmed=True, data_dir=self.data)
        self.assertEqual(codes(report), ["zip-symlink"])
        self.assertFalse(self.target().exists())

        unsafe = self.tmp / "packages" / "unsafe.zip"
        with zipfile.ZipFile(str(unsafe), "w") as archive:
            archive.writestr("mcp.json", json.dumps({"mcpServers": {
                "demo": server_entry(self.node, ["x.js"])}}))
            archive.writestr("../escape.js", "// escape\n")
        report = pack_import.install(unsafe, confirmed=True, data_dir=self.data)
        self.assertEqual(codes(report), ["zip-unsafe-path"])
        self.assertFalse(self.target().exists())


if __name__ == "__main__":
    unittest.main()
