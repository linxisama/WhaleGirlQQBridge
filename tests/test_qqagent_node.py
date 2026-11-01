"""随包 Node 的发现顺序与能力桥就绪判定。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import qqagent_bridge_control as control
from src import qqagent_host as qh


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake-node")
    return path


class NodeDiscoveryTests(unittest.TestCase):
    def test_随包_node_优先于_PATH(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            bundled = base / "bundled"
            node = _touch(bundled / "node.exe")
            other = _touch(base / "path-node.exe")
            with mock.patch.object(qh, "BUNDLED_NODE_DIR", bundled), \
                    mock.patch.object(qh.shutil, "which", return_value=str(other)), \
                    mock.patch.object(qh, "_common_node_paths", return_value=()):
                self.assertEqual(qh.node_binary(), str(node))
                self.assertEqual(qh.node_info()["source"], "bundled")

    def test_环境变量优先于随包(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            bundled = base / "bundled"
            _touch(bundled / "node.exe")
            env_node = _touch(base / "env-node.exe")
            with mock.patch.dict(os.environ, {qh.NODE_ENV: str(env_node)}), \
                    mock.patch.object(qh, "BUNDLED_NODE_DIR", bundled), \
                    mock.patch.object(qh.shutil, "which", return_value=None), \
                    mock.patch.object(qh, "_common_node_paths", return_value=()):
                info = qh.node_info()
                self.assertEqual(info["path"], str(env_node))
                self.assertEqual(info["source"], "env")

    def test_PATH里的WindowsApps商店桩被跳过(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            common = base / "common"
            node = _touch(common / "node.exe")
            stub = r"C:\Users\demo\AppData\Local\Microsoft\WindowsApps\node.exe"
            with mock.patch.object(qh, "BUNDLED_NODE_DIR", base / "missing"), \
                    mock.patch.object(qh.shutil, "which", return_value=stub), \
                    mock.patch.object(qh, "_common_node_paths", return_value=(common,)):
                info = qh.node_info()
                self.assertEqual(info["path"], str(node))
                self.assertEqual(info["source"], "common")

    def test_显式路径不被自动发现替换(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            explicit = _touch(base / "explicit" / "node.exe")
            bundled = base / "bundled"
            _touch(bundled / "node.exe")
            with mock.patch.object(qh, "BUNDLED_NODE_DIR", bundled), \
                    mock.patch.object(qh.shutil, "which", return_value=None):
                self.assertEqual(qh.node_binary(str(explicit)), str(explicit))

    def test_旧配置里的裸命令node找不到时回退随包(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            bundled = base / "bundled"
            node = _touch(bundled / "node.exe")
            with mock.patch.object(qh, "BUNDLED_NODE_DIR", bundled), \
                    mock.patch.object(qh.shutil, "which", return_value=None), \
                    mock.patch.object(qh, "_common_node_paths", return_value=()):
                info = qh.node_info("node")
                self.assertTrue(info["ok"])
                self.assertEqual(info["path"], str(node))
                self.assertEqual(info["source"], "bundled")

    def test_重装后旧随包Node路径失效时回退当前随包(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            bundled = base / "new" / "runtime" / "node"
            node = _touch(bundled / "node.exe")
            stale = base / "old" / "_internal" / "runtime" / "node" / "node.exe"
            with mock.patch.dict(os.environ, {qh.NODE_ENV: ""}), \
                    mock.patch.object(qh, "BUNDLED_NODE_DIR", bundled), \
                    mock.patch.object(qh.shutil, "which", return_value=None), \
                    mock.patch.object(qh, "_common_node_paths", return_value=()):
                info = qh.node_info(str(stale))
                self.assertTrue(info["ok"])
                self.assertEqual(info["path"], str(node))
                self.assertEqual(info["source"], "bundled")
                self.assertEqual(info["stale"], str(stale))

    def test_显式路径写错时不悄悄回退随包Node(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            bundled = base / "bundled"
            _touch(bundled / "node.exe")
            with mock.patch.object(qh, "BUNDLED_NODE_DIR", bundled), \
                    mock.patch.object(qh.shutil, "which", return_value=None):
                info = qh.node_info(str(base / "gone" / "node.exe"))
                self.assertFalse(info["ok"])
                self.assertEqual(info["source"], "argument")
                self.assertEqual(info["error"]["code"], "node-invalid")
                self.assertIn("gone", info["error"]["message"])
                self.assertIsNone(qh.node_binary(str(base / "gone" / "node.exe")))

    def test_找不到_node时给出中文原因(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.dict(os.environ, {qh.NODE_ENV: ""}), \
                    mock.patch.object(qh, "BUNDLED_NODE_DIR", Path(temp) / "missing"), \
                    mock.patch.object(qh.shutil, "which", return_value=None), \
                    mock.patch.object(qh, "_common_node_paths", return_value=()):
                info = qh.node_info()
                self.assertFalse(info["ok"])
                self.assertEqual(info["error"]["code"], "node-missing")
                self.assertIn("Node 20+", info["error"]["message"])
                self.assertIn(qh.NODE_ENV, info["error"]["message"])


class _Bridge:
    def _profiles(self):
        return [{"provider": "demo.provider"}]


class _Manager:
    _plugins = {"qqagent_bridge": _Bridge()}

    def __init__(self, settings):
        self.settings = settings

    def get_settings(self, _pid):
        return dict(self.settings)


class BridgeNodeStatusTests(unittest.TestCase):
    def test_缺Node时能力桥不ready(self):
        missing = {"ok": False, "path": "", "source": "", "error": {"code": "node-missing"}}
        with mock.patch.object(control.qqagent_host, "node_info", return_value=missing):
            state = control.bridge_status(_Manager({}))
        self.assertIn("Node 20+", state["missing"])
        self.assertFalse(state["ready"])


if __name__ == "__main__":
    unittest.main()
