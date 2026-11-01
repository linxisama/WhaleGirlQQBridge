"""原生文件选择框（独立子进程）与 4 条 browse 路由失败映射的测试。

为什么把所有用例的子进程都换成假的：真跑 native_picker.pick 会在用户桌面上
弹出模态文件对话框，而这里要验证的恰好是「子进程以各种方式失败时路由只回 400、
后端进程不退出」。假掉的只有 _spawn 这一层，pick 的解析、超时与错误映射
以及 4 条真实路由全部走真代码。
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

from src import native_picker

try:
    from fastapi.testclient import TestClient

    HAS_CLIENT = True
except Exception:
    HAS_CLIENT = False


def _done(stdout: bytes = b"", returncode: int = 0, stderr: bytes = b""):
    """造一个和 subprocess.run 同形的完成态。"""
    return subprocess.CompletedProcess(args=["picker"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def _json_payload(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class PickerUnitTests(unittest.TestCase):
    """父进程侧解析与命令拼装的单元测试（不经过 HTTP）。"""

    def test_command_runs_this_file_in_source_mode(self):
        command = native_picker._command(native_picker.MODE_FILE, "选择插件文件",
                                         [("插件文件", "*.py")])
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1], str(pathlib.Path(native_picker.__file__).resolve()))
        self.assertEqual(command[2], native_picker.MODE_FILE)
        self.assertIn("--title", command)
        self.assertEqual(list(json.loads(command[command.index("--filetypes") + 1])),
                         [["插件文件", "*.py"]])

    def test_command_reuses_exe_when_frozen(self):
        with unittest.mock.patch.object(sys, "frozen", True, create=True):
            command = native_picker._command(native_picker.MODE_DIR, "", None)
        self.assertEqual(command, [sys.executable, native_picker.MODE_DIR])

    def test_pick_reads_json_after_noise(self):
        noisy = b"tk: warning\n{\"paths\": [\"C:/plugins/a.py\"]}\n"
        with unittest.mock.patch.object(native_picker, "_spawn",
                                        lambda command, wait: _done(noisy)):
            self.assertEqual(native_picker.pick(native_picker.MODE_FILE),
                             ["C:/plugins/a.py"])

    def test_pick_rejects_unknown_mode(self):
        with self.assertRaises(native_picker.PickerError):
            native_picker.pick("--pick-everything")

    def test_timeout_env_override(self):
        with unittest.mock.patch.dict(os.environ, {"WHALEGIRL_PICKER_TIMEOUT": "7"}):
            self.assertEqual(native_picker._timeout(), 7.0)
        with unittest.mock.patch.dict(os.environ, {"WHALEGIRL_PICKER_TIMEOUT": "abc"}):
            self.assertEqual(native_picker._timeout(), native_picker.DEFAULT_TIMEOUT)


@unittest.skipUnless(HAS_CLIENT, "需要 fastapi.testclient")
class BrowseRouteTests(unittest.TestCase):
    """4 条 browse 路由：成功返回字段不变，失败一律 400。"""

    def setUp(self):
        import yaml

        from src.qq_app import QQRuntime, create_app, ROOT

        self._tmp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.root = pathlib.Path(self._tmp.name)
        config = self.root / "test.yaml"
        config.write_text(yaml.safe_dump({
            "ai": {"api_url": "https://example.test/v1", "api_key": "fake-key",
                   "model": "test-model"},
            "qq": {"app_id": "123", "client_secret": "fake-secret"},
        }), encoding="utf-8")
        self.bot = QQRuntime(config_path=config, db_path=self.root / "test.sqlite",
                             data_root=self.root / "runtime")
        self.client = TestClient(create_app(self.bot))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self._tmp.cleanup()

    def _browse(self, route: str, spawn):
        with unittest.mock.patch.object(native_picker, "_spawn", spawn):
            return self.client.post(route)

    def test_single_file_returns_path(self):
        response = self._browse("/api/plugins/browse",
                                lambda command, wait: _done(_json_payload(
                                    {"paths": ["C:/picked/p.py"]})))
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["path"], "C:/picked/p.py")

    def test_feature_pack_returns_path(self):
        response = self._browse("/api/feature-packs/browse",
                                lambda command, wait: _done(_json_payload(
                                    {"paths": ["C:/packs/demo.zip"]})))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["path"], "C:/packs/demo.zip")

    def test_many_files_return_all_paths(self):
        response = self._browse("/api/plugins/browse-many",
                                lambda command, wait: _done(_json_payload(
                                    {"paths": ["C:/picked/a.py", "C:/picked/b.py"]})))
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["paths"], ["C:/picked/a.py", "C:/picked/b.py"])

    def test_folder_returns_directory(self):
        response = self._browse("/api/plugins/browse-folder",
                                lambda command, wait: _done(_json_payload(
                                    {"paths": ["C:/plugins/demo"]})))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["paths"], ["C:/plugins/demo"])

    def test_cancel_returns_empty_path(self):
        response = self._browse("/api/plugins/browse",
                                lambda command, wait: _done(_json_payload({"paths": []})))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["path"], "")

    def test_nonzero_exit_maps_to_400(self):
        response = self._browse("/api/plugins/browse",
                                lambda command, wait: _done(
                                    _json_payload({"error": "没有图形环境"}), returncode=1))
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("没有图形环境", response.json()["detail"])

    def test_garbled_output_maps_to_400(self):
        response = self._browse("/api/plugins/browse-many",
                                lambda command, wait: _done(
                                    b"Tcl_AsyncDelete: async handler deleted\n"))
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("合法 JSON", response.json()["detail"])

    def test_timeout_maps_to_400(self):
        def boom(command, wait):
            raise subprocess.TimeoutExpired(command, wait)

        response = self._browse("/api/plugins/browse-folder", boom)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("没有返回", response.json()["detail"])

    def test_child_error_payload_maps_to_400(self):
        response = self._browse("/api/feature-packs/browse",
                                lambda command, wait: _done(_json_payload(
                                    {"error": "无法打开原生选择框：no display"})))
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("no display", response.json()["detail"])

    def test_failure_keeps_backend_serving(self):
        failed = self._browse("/api/plugins/browse",
                              lambda command, wait: _done(b"not json"))
        self.assertEqual(failed.status_code, 400, failed.text)
        again = self._browse("/api/plugins/browse",
                             lambda command, wait: _done(_json_payload(
                                 {"paths": ["C:/picked/again.py"]})))
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["path"], "C:/picked/again.py")


if __name__ == "__main__":
    unittest.main()
