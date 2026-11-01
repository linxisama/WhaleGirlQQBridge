"""从网址安装 / 卸载插件的行为与安全边界测试。

⚠️ 这里测的重点是**安全闸门**，不是"能不能下载"：
远程安装 = 把外部代码装进程序并且会被 import 执行，所以
- 只允许 https；
- 必须 confirmed=True；
- 含危险用法（system/subprocess/eval/exec/socket…）一律拒绝；
- 不是插件的代码拒绝；
- zip 防目录穿越；
- 内置插件不允许删除。
"""

import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

from src import plugin_installer as pi
from src.plugins.manager import PLUGIN_MODULES, PluginManager

SAFE_PLUGIN = '''
from src.plugins import FieldSchema, PluginBase, PluginMeta


class MyPlugin(PluginBase):
    META = PluginMeta(
        id="my_plugin",
        name="我的插件",
        description="测试用",
        config_schema={"enabled": FieldSchema(type="bool", label="启用")},
        default_settings={"enabled": False},
    )
'''


def make_zip(files: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class SourceResolutionTests(unittest.TestCase):
    def test_https_py(self):
        self.assertEqual(pi.resolve_source("https://e.com/a.py"),
                         ("file", "https://e.com/a.py"))

    def test_https_zip(self):
        self.assertEqual(pi.resolve_source("https://e.com/a.zip"),
                         ("archive", "https://e.com/a.zip"))

    def test_github_repo_becomes_zip(self):
        kind, url = pi.resolve_source("https://github.com/owner/repo")
        self.assertEqual(kind, "archive")
        self.assertIn("codeload.github.com", url)

    def test_github_blob_becomes_raw(self):
        kind, url = pi.resolve_source("https://github.com/o/r/blob/dev/p.py")
        self.assertEqual(kind, "file")
        self.assertEqual(url, "https://raw.githubusercontent.com/o/r/dev/p.py")

    def test_http_rejected(self):
        with self.assertRaises(pi.InstallError) as caught:
            pi.resolve_source("http://e.com/a.py")
        self.assertIn("https", str(caught.exception))

    def test_other_schemes_rejected(self):
        for url in ("ftp://e.com/a.py", "file:///c:/a.py", "javascript:alert(1)", ""):
            with self.subTest(url=url):
                with self.assertRaises(pi.InstallError):
                    pi.resolve_source(url)

    def test_unknown_extension_rejected(self):
        with self.assertRaises(pi.InstallError):
            pi.resolve_source("https://e.com/a.txt")


class PluginIdTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(pi.normalize_plugin_id("My-Plugin"), "my_plugin")
        self.assertEqual(pi.normalize_plugin_id("abc123"), "abc123")

    def test_invalid(self):
        for raw in ("", "1abc", "a", "has space", "有中文", "a" * 60, "../evil"):
            with self.subTest(raw=raw):
                with self.assertRaises(pi.InstallError):
                    pi.normalize_plugin_id(raw)


class SourceCheckTests(unittest.TestCase):
    def test_safe_plugin_passes(self):
        self.assertEqual(pi.check_source(SAFE_PLUGIN), [])

    def test_syntax_error_reported(self):
        problems = pi.check_source("def broken(:\n")
        self.assertTrue(any("合法的 Python" in p for p in problems))

    def test_not_a_plugin_rejected(self):
        problems = pi.check_source("x = 1\n")
        self.assertTrue(any("PluginBase" in p for p in problems))

    def test_dangerous_calls_rejected(self):
        cases = {
            "os.system": "import os\nos.system('calc')\n# PluginBase PluginMeta",
            "subprocess": "import subprocess\n# PluginBase PluginMeta",
            "eval": "eval('1')\n# PluginBase PluginMeta",
            "exec": "exec('x=1')\n# PluginBase PluginMeta",
            "socket": "import socket\n# PluginBase PluginMeta",
            "pickle": "import pickle\npickle.loads(b'')\n# PluginBase PluginMeta",
            "rmtree": "import shutil\nshutil.rmtree('x')\n# PluginBase PluginMeta",
            "ctypes": "import ctypes\n# PluginBase PluginMeta",
            "winreg": "import winreg\n# PluginBase PluginMeta",
        }
        for label, code in cases.items():
            with self.subTest(label=label):
                problems = pi.check_source(code)
                self.assertTrue(problems, f"{label} 应该被拒绝")

    def test_reason_is_human_readable(self):
        problems = pi.check_source("import os\nos.system('x')\n# PluginBase PluginMeta")
        self.assertTrue(any("系统命令" in p for p in problems))


class ArchiveTests(unittest.TestCase):
    def test_picks_plugin_py(self):
        data = make_zip({"readme.md": "hi", "my_plugin.py": SAFE_PLUGIN})
        name, code = pi._pick_from_archive(data, None)
        self.assertEqual(name, "my_plugin")
        self.assertIn("PluginBase", code)

    def test_prefers_requested_id(self):
        data = make_zip({"a.py": SAFE_PLUGIN, "b.py": SAFE_PLUGIN})
        name, _ = pi._pick_from_archive(data, "b")
        self.assertEqual(name, "b")

    def test_prefers_index_py(self):
        data = make_zip({"zzz.py": SAFE_PLUGIN, "index.py": SAFE_PLUGIN})
        name, _ = pi._pick_from_archive(data, None)
        self.assertEqual(name, "index")

    def test_directory_traversal_rejected(self):
        data = make_zip({"../../evil.py": SAFE_PLUGIN})
        with self.assertRaises(pi.InstallError) as caught:
            pi._pick_from_archive(data, None)
        self.assertIn("可疑路径", str(caught.exception))

    def test_no_python_rejected(self):
        data = make_zip({"readme.md": "hi"})
        with self.assertRaises(pi.InstallError):
            pi._pick_from_archive(data, None)

    def test_bad_zip_rejected(self):
        with self.assertRaises(pi.InstallError):
            pi._pick_from_archive(b"not a zip", None)


class InstallGateTests(unittest.TestCase):
    """安装失败要**不留痕迹**：不应写出半个文件。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_requires_confirmation(self):
        with mock.patch.object(pi, "_fetch", return_value=SAFE_PLUGIN.encode()):
            with self.assertRaises(pi.InstallError) as caught:
                pi.install_from_url("https://e.com/a.py", self.dir, confirmed=False)
        self.assertIn("确认", str(caught.exception))
        self.assertEqual(list(self.dir.glob("*.py")), [])

    def test_install_writes_file(self):
        with mock.patch.object(pi, "_fetch", return_value=SAFE_PLUGIN.encode()):
            result = pi.install_from_url("https://e.com/my_plugin.py", self.dir,
                                         confirmed=True)
        self.assertEqual(result["plugin_id"], "my_plugin")
        self.assertTrue((self.dir / "my_plugin.py").is_file())

    def test_dangerous_source_not_written(self):
        code = "import os\nos.system('x')\n# PluginBase PluginMeta\n"
        with mock.patch.object(pi, "_fetch", return_value=code.encode()):
            with self.assertRaises(pi.InstallError) as caught:
                pi.install_from_url("https://e.com/evil.py", self.dir, confirmed=True)
        self.assertIn("安全检查", str(caught.exception))
        self.assertEqual(list(self.dir.glob("*.py")), [], "被拒的插件不该落盘")

    def test_existing_file_needs_overwrite(self):
        (self.dir / "my_plugin.py").write_text("# old", encoding="utf-8")
        with mock.patch.object(pi, "_fetch", return_value=SAFE_PLUGIN.encode()):
            with self.assertRaises(pi.InstallError) as caught:
                pi.install_from_url("https://e.com/my_plugin.py", self.dir, confirmed=True)
        self.assertIn("已存在", str(caught.exception))

    def test_size_limit_enforced(self):
        with mock.patch.object(pi, "_fetch", side_effect=pi.InstallError("内容过大")):
            with self.assertRaises(pi.InstallError):
                pi.install_from_url("https://e.com/a.py", self.dir, confirmed=True)

    def test_http_source_refused(self):
        with self.assertRaises(pi.InstallError):
            pi.install_from_url("http://e.com/a.py", self.dir, confirmed=True)


class CustomRegistryTests(unittest.TestCase):
    """自定义插件清单：与内置模块分开记账。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_starts_empty(self):
        manager = PluginManager(self.dir)
        self.assertEqual(manager.custom_plugin_ids(), [])

    def test_register_and_persist(self):
        manager = PluginManager(self.dir)
        manager.register_custom("my_plugin")
        self.assertEqual(manager.custom_plugin_ids(), ["my_plugin"])
        # 新实例也要读得到
        self.assertEqual(PluginManager(self.dir).custom_plugin_ids(), ["my_plugin"])

    def test_register_is_idempotent(self):
        manager = PluginManager(self.dir)
        manager.register_custom("dup")
        manager.register_custom("dup")
        self.assertEqual(manager.custom_plugin_ids(), ["dup"])

    def test_unregister_builtin_returns_false(self):
        manager = PluginManager(self.dir)
        # 内置插件不在自定义清单里 —— 这是"内置不可删"的底层保证
        self.assertFalse(manager.unregister_custom("sleep_mode"))

    def test_unregister_custom(self):
        manager = PluginManager(self.dir)
        manager.register_custom("temp")
        self.assertTrue(manager.unregister_custom("temp"))
        self.assertEqual(manager.custom_plugin_ids(), [])

    def test_corrupt_registry_is_empty_not_crash(self):
        (self.dir / "custom_plugins.json").write_text("{ broken", encoding="utf-8")
        manager = PluginManager(self.dir)
        self.assertEqual(manager.custom_plugin_ids(), [])
        self.assertGreater(len(manager._plugins), 20, "内置插件仍应正常加载")

    def test_registry_accepts_dict_shape(self):
        (self.dir / "custom_plugins.json").write_text(
            json.dumps({"plugins": ["a_plugin"]}), encoding="utf-8")
        self.assertEqual(PluginManager(self.dir).custom_plugin_ids(), ["a_plugin"])

    def test_builtin_never_in_custom_list(self):
        manager = PluginManager(self.dir)
        for builtin in PLUGIN_MODULES[:5]:
            self.assertNotIn(builtin, manager.custom_plugin_ids())


CUSTOM_CODE = '''from src.plugins import FieldSchema, PluginBase, PluginMeta


class DemoPlugin(PluginBase):
    META = PluginMeta(
        id="demo_custom",
        name="演示自定义插件",
        description="测试",
        config_schema={"enabled": FieldSchema(type="bool", label="启用")},
        default_settings={"enabled": False},
    )
'''


class CustomPluginPersistenceTests(unittest.TestCase):
    """自定义插件必须存在**可写的用户数据目录**里。

    这是打包成 exe 之后才暴露的关键点：exe 运行时磁盘上**没有 src/plugins 目录**
    （代码在 exe 内部），把用户插件写到那里重启就消失了。
    所以安装位置是 ``<data_dir>/custom_plugins/``，并且按**文件路径**加载。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _install_fake(self, plugin_id="demo_custom"):
        manager = PluginManager(self.dir)
        manager.custom_plugin_path(plugin_id).write_text(CUSTOM_CODE, encoding="utf-8")
        manager.register_custom(plugin_id)
        manager.reload()
        return manager

    def test_custom_dir_is_under_data_dir(self):
        manager = PluginManager(self.dir)
        self.assertEqual(manager.custom_plugins_dir, self.dir / "custom_plugins")
        self.assertTrue(manager.custom_plugins_dir.is_dir())

    def test_custom_dir_can_be_overridden(self):
        other = self.dir / "elsewhere"
        manager = PluginManager(self.dir, custom_plugins_dir=other)
        self.assertEqual(manager.custom_plugins_dir, other)
        self.assertTrue(other.is_dir())

    def test_installed_plugin_is_loaded(self):
        manager = self._install_fake()
        self.assertIn("demo_custom", manager._plugins)
        self.assertEqual(manager._plugins["demo_custom"].META.name, "演示自定义插件")

    def test_plugin_survives_restart(self):
        self._install_fake()
        restarted = PluginManager(self.dir)
        self.assertIn("demo_custom", restarted._plugins)
        self.assertEqual(restarted.custom_plugin_ids(), ["demo_custom"])

    def test_plugin_has_settings_after_restart(self):
        self._install_fake()
        restarted = PluginManager(self.dir)
        self.assertIn("enabled", restarted.get_settings("demo_custom"))

    def test_uninstall_removes_file_and_registration(self):
        manager = self._install_fake()
        path = manager.custom_plugin_path("demo_custom")
        manager.unregister_custom("demo_custom")
        path.unlink()
        sys.modules.pop("src_custom_plugin_demo_custom", None)
        manager.reload()
        self.assertNotIn("demo_custom", manager._plugins)
        self.assertFalse(path.is_file())
        self.assertEqual(manager.custom_plugin_ids(), [])

    def test_broken_custom_plugin_does_not_break_builtins(self):
        manager = PluginManager(self.dir)
        manager.custom_plugin_path("broken_one").write_text("this is not python(", encoding="utf-8")
        manager.register_custom("broken_one")
        manager.reload()
        self.assertNotIn("broken_one", manager._plugins)
        self.assertGreater(len(manager._plugins), 20, "内置插件不该被一个坏插件带崩")

    def test_registered_but_missing_file_is_skipped(self):
        manager = PluginManager(self.dir)
        manager.register_custom("ghost_plugin")      # 只登记，不建文件
        manager.reload()
        self.assertNotIn("ghost_plugin", manager._plugins)
        self.assertGreater(len(manager._plugins), 20)


class LocalFileTests(unittest.TestCase):
    """本地文件安装。

    关键点：本地文件**同样**是要被 import 执行的代码，安全检查一条都不能少。
    "是本地的所以放心"是错误假设。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self.plugins_dir = self.dir / "custom_plugins"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name, content=SAFE_PLUGIN):
        path = self.dir / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_inspect_local_py(self):
        path = self._write("my_plugin.py")
        info = pi.inspect_local(path)
        self.assertTrue(info["ok"])
        self.assertEqual(info["plugin_id"], "my_plugin")
        self.assertEqual(info["problems"], [])
        self.assertIn("本地文件", info["resolved_url"])
        self.assertIn("PluginBase", info["preview"])

    def test_inspect_local_does_not_write_plugins(self):
        path = self._write("my_plugin.py")
        pi.inspect_local(path)
        self.assertFalse(self.plugins_dir.exists())

    def test_inspect_local_rejects_dangerous_code(self):
        path = self._write("evil.py", "import os\nos.system('x')\n# PluginBase PluginMeta")
        info = pi.inspect_local(path)
        self.assertFalse(info["ok"])
        self.assertTrue(any("系统命令" in p for p in info["problems"]))

    def test_inspect_local_rejects_non_plugin(self):
        path = self._write("plain.py", "x = 1\n")
        info = pi.inspect_local(path)
        self.assertFalse(info["ok"])
        self.assertTrue(any("PluginBase" in p for p in info["problems"]))

    def test_unsupported_suffix_rejected(self):
        path = self._write("thing.txt", "hello")
        with self.assertRaises(pi.InstallError) as caught:
            pi.inspect_local(path)
        self.assertIn(".py", str(caught.exception))

    def test_missing_file_rejected(self):
        # 预检不再抛异常：路径不存在是"这个包不能用"，界面照常渲染失败结果。
        info = pi.inspect_local(self.dir / "nope.py")
        self.assertFalse(info["ok"])
        self.assertEqual(info["error_code"], "missing")
        self.assertIn("找不到文件", info["error"])
        # 安装是写操作，仍然抛异常回到 HTTP 400，不能失败得无声无息。
        with self.assertRaises(pi.InstallError) as caught:
            pi.install_from_file(self.dir / "nope.py", self.plugins_dir, confirmed=True)
        self.assertIn("找不到文件", str(caught.exception))

    def test_install_requires_confirmation(self):
        path = self._write("my_plugin.py")
        with self.assertRaises(pi.InstallError) as caught:
            pi.install_from_file(path, self.plugins_dir, confirmed=False)
        self.assertIn("确认", str(caught.exception))
        self.assertFalse(self.plugins_dir.exists())

    def test_install_copies_into_plugins_dir(self):
        path = self._write("my_plugin.py")
        result = pi.install_from_file(path, self.plugins_dir, confirmed=True)
        self.assertEqual(result["plugin_id"], "my_plugin")
        installed = self.plugins_dir / "my_plugin.py"
        self.assertTrue(installed.is_file())
        # 源文件保持不动（是复制，不是移动）
        self.assertTrue(path.is_file())
        self.assertIn("PluginBase", installed.read_text(encoding="utf-8"))

    def test_install_local_rejects_dangerous_code(self):
        path = self._write("evil.py", "import os\nos.system('x')\n# PluginBase PluginMeta")
        with self.assertRaises(pi.InstallError) as caught:
            pi.install_from_file(path, self.plugins_dir, confirmed=True)
        self.assertIn("安全检查", str(caught.exception))
        self.assertFalse((self.plugins_dir / "evil.py").exists())

    def test_install_local_from_zip(self):
        data = make_zip({"README.md": "x", "my_plugin.py": SAFE_PLUGIN})
        path = self.dir / "pack.zip"
        path.write_bytes(data)
        result = pi.install_from_file(path, self.plugins_dir, confirmed=True)
        self.assertEqual(result["plugin_id"], "my_plugin")
        self.assertTrue((self.plugins_dir / "my_plugin.py").is_file())

    def test_install_local_respects_given_id(self):
        path = self._write("whatever.py")
        result = pi.install_from_file(path, self.plugins_dir, plugin_id="renamed",
                                      confirmed=True)
        self.assertEqual(result["plugin_id"], "renamed")
        self.assertTrue((self.plugins_dir / "renamed.py").is_file())

    def test_install_local_duplicate_needs_overwrite(self):
        path = self._write("my_plugin.py")
        pi.install_from_file(path, self.plugins_dir, confirmed=True)
        with self.assertRaises(pi.InstallError) as caught:
            pi.install_from_file(path, self.plugins_dir, confirmed=True)
        self.assertIn("已存在", str(caught.exception))

    def test_local_install_then_manager_loads_it(self):
        """端到端：本地文件 → 安装 → 管理器加载。"""
        data_dir = self.dir / "data"
        manager = PluginManager(data_dir)
        path = self._write("local_probe.py", CUSTOM_CODE.replace("demo_custom", "local_probe"))
        pi.install_from_file(path, manager.custom_plugins_dir, confirmed=True)
        manager.register_custom("local_probe")
        manager.reload()
        self.assertIn("local_probe", manager._plugins)

    def test_archive_size_limit(self):
        big = self.dir / "big.zip"
        big.write_bytes(b"0" * 16)
        with mock.patch.object(pi, "MAX_ARCHIVE_BYTES", 8):
            info = pi.inspect_local(big)
        self.assertFalse(info["ok"])
        self.assertEqual(info["error_code"], "too-large")
        self.assertIn("过大", info["error"])


RELATIVE_IMPORT_CODE = '''from . import FieldSchema, PluginBase, PluginMeta


class RelativeProbe(PluginBase):
    META = PluginMeta(
        id="relative_probe",
        name="相对导入探针",
        description="用项目惯用的相对导入写法",
        config_schema={"enabled": FieldSchema(type="bool", label="启用")},
        default_settings={"enabled": False},
    )
'''


class RelativeImportTests(unittest.TestCase):
    """自定义插件用**相对导入**时必须也能加载。

    实测踩过的坑：用户装的插件文件写 `from . import PluginBase`（项目内置插件都是这写法），
    而按文件路径加载出来的模块默认是顶层模块、没有包上下文，相对导入直接
    `ImportError: attempted relative import with no known parent package`。
    表现为"文件装上去了，但插件没生效"。

    修法是加载时把 ``module.__package__`` 指到 ``src.plugins``。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_relative_import_plugin_loads(self):
        manager = PluginManager(self.dir)
        manager.custom_plugin_path("relative_probe").write_text(
            RELATIVE_IMPORT_CODE, encoding="utf-8")
        manager.register_custom("relative_probe")
        manager.reload()
        self.assertIn("relative_probe", manager._plugins)
        self.assertEqual(manager._plugins["relative_probe"].META.name, "相对导入探针")

    def test_relative_import_plugin_survives_restart(self):
        manager = PluginManager(self.dir)
        manager.custom_plugin_path("relative_probe").write_text(
            RELATIVE_IMPORT_CODE, encoding="utf-8")
        manager.register_custom("relative_probe")
        manager.reload()
        restarted = PluginManager(self.dir)
        self.assertIn("relative_probe", restarted._plugins)

    def test_failed_custom_plugin_is_not_left_in_sys_modules(self):
        # 加载失败的模块不该留在 sys.modules 里，否则重装会命中脏缓存
        manager = PluginManager(self.dir)
        manager.custom_plugin_path("broken_x").write_text("import nonexistent_zzz",
                                                          encoding="utf-8")
        manager.register_custom("broken_x")
        manager.reload()
        self.assertNotIn("src_custom_plugin_broken_x", sys.modules)


if __name__ == "__main__":
    unittest.main()
