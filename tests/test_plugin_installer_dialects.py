"""插件预检的清单方言与容错回归（W11-2）。

真实市场里的 50 个包有 42 个是 B1 老方言：plugin.json 的 version 写成整数 1、带
app_version 键、甚至根本没有 entry。这些是正常清单，不是坏清单。另外还有 zip 损坏、
编码不对、二进制垃圾这类"读不动"的包——预检只该说"这个包不能用"，不该抛异常。

危险用法的判定必须基于真实代码：注释里写一句 os.system、字符串里放一段
subprocess.run 都不算调用；真的 import subprocess 或调用 eval 仍然要拦下。
"""
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from src import plugin_installer as pi


PLUGIN_CODE = '''from src.plugins import PluginBase, PluginMeta


class ProbePlugin(PluginBase):
    META = PluginMeta(id="probe_plugin", name="探针插件", description="测试用插件")

    def handle(self, message):
        return message
'''

OTHER_PLUGIN_CODE = PLUGIN_CODE.replace("probe_plugin", "other_plugin")

MARKERS_ONLY = "# PluginBase PluginMeta\n"

# GBK 编码的插件源码（国区最常见的非 UTF-8 包）：注释与字符串里都是中文。
SAMPLE_GBK = "# 中文说明：这个插件用 GBK 保存\n" + PLUGIN_CODE


def make_zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def b1_manifest(**overrides):
    """B1 老方言：version 是整数，带 app_version，没有 entry。"""
    meta = {"format": "whale-plugin", "version": 1, "id": "b1_probe",
            "name": "B1 探针", "description": "老导出器写的包", "app_version": "5.0.0"}
    meta.update(overrides)
    return json.dumps(meta, ensure_ascii=False)


def new_manifest(**overrides):
    """新方言：version 是字符串，entry 指源码。"""
    meta = {"id": "new_probe", "name": "新包", "description": "新导出器写的包",
            "version": "1.2.0", "entry": "new_probe.py"}
    meta.update(overrides)
    return json.dumps(meta, ensure_ascii=False)


class DialectTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _zip(self, name, data):
        path = self.dir / name
        path.write_bytes(data)
        return path

    def _inspect_zip(self, files, name="pack.zip"):
        return pi.inspect_local(self._zip(name, make_zip(files)))


class B1LegacyManifestTests(DialectTestCase):
    def test_integer_version_is_a_valid_manifest(self):
        info = self._inspect_zip({"plugin.json": b1_manifest(), "b1_probe.py": PLUGIN_CODE})
        self.assertTrue(info["ok"], info["problems"])
        self.assertEqual(info["plugin_id"], "probe_plugin")
        # 源码里没写 version，展示回落到清单里的整数版本
        self.assertEqual(info["version"], "1")
        self.assertEqual(info["name"], "探针插件")
        self.assertEqual(info["problems"], [])

    def test_float_version_is_a_valid_manifest(self):
        info = self._inspect_zip({"plugin.json": b1_manifest(version=1.5),
                                  "b1_probe.py": PLUGIN_CODE})
        self.assertTrue(info["ok"], info["problems"])
        self.assertEqual(info["version"], "1.5")

    def test_app_version_key_marks_legacy_without_entry(self):
        info = self._inspect_zip({"plugin.json": b1_manifest(version="1.0.0", entry=None),
                                  "b1_probe.py": PLUGIN_CODE})
        self.assertTrue(info["ok"], info["problems"])
        self.assertEqual(info["problems"], [])
        self.assertTrue(any("entry" in notice for notice in info["notices"]))

    def test_missing_entry_falls_back_to_single_source(self):
        info = self._inspect_zip({"plugin.json": b1_manifest(entry="absent.py"),
                                  "b1_probe.py": PLUGIN_CODE})
        self.assertTrue(info["ok"], info["problems"])
        self.assertTrue(any("回退" in notice for notice in info["notices"]))

    def test_wrong_entry_with_several_sources_lists_candidates(self):
        info = self._inspect_zip({
            "plugin.json": b1_manifest(entry="absent.py"),
            "aaa_one.py": PLUGIN_CODE,
            "bbb_two.py": OTHER_PLUGIN_CODE,
        })
        self.assertFalse(info["ok"])
        joined = "；".join(info["problems"])
        self.assertIn("aaa_one.py", joined)
        self.assertIn("bbb_two.py", joined)


class EntryResolutionTests(DialectTestCase):
    def test_new_dialect_wrong_entry_is_still_blocking(self):
        info = self._inspect_zip({
            "plugin.json": new_manifest(entry="absent.py"),
            "new_probe.py": PLUGIN_CODE,
        })
        self.assertFalse(info["ok"])
        self.assertTrue(any("入口不存在" in p for p in info["problems"]),
                        info["problems"])

    def test_entry_picks_the_declared_source(self):
        info = self._inspect_zip({
            "plugin.json": new_manifest(entry="new_probe.py"),
            "new_probe.py": PLUGIN_CODE,
            "helper.py": "x = 1\n",
        })
        self.assertTrue(info["ok"], info["problems"])

    def test_several_declaring_sources_without_entry_lists_candidates(self):
        info = self._inspect_zip({"aaa_one.py": PLUGIN_CODE, "bbb_two.py": OTHER_PLUGIN_CODE})
        self.assertFalse(info["ok"])
        joined = "；".join(info["problems"])
        self.assertIn("aaa_one.py", joined)
        self.assertIn("bbb_two.py", joined)


class StructuredFailureTests(DialectTestCase):
    def test_corrupt_zip_returns_failure(self):
        info = pi.inspect_local(self._zip("bad.zip", b"PK\x03\x04 definitely not a zip"))
        self.assertFalse(info["ok"])
        self.assertEqual(info["error_code"], "bad-zip")
        self.assertIn("zip", info["error"])

    def test_empty_file_returns_failure(self):
        info = pi.inspect_local(self._zip("empty.zip", b""))
        self.assertFalse(info["ok"])
        self.assertEqual(info["error_code"], "bad-zip")

    def test_missing_path_returns_failure(self):
        info = pi.inspect_local(self.dir / "nope.zip")
        self.assertFalse(info["ok"])
        self.assertEqual(info["error_code"], "missing")
        self.assertIn("找不到文件", info["error"])

    def test_gbk_source_is_decoded_not_refused(self):
        """GBK 源码是国区最常见的非 UTF-8 插件：语料实测装不进去（InstallError: 文件不是 UTF-8 文本）。

        现在 _decode 在 utf-8/utf-8-sig/带 BOM 的 utf-16 之后兜底 gb18030，并额外要求「解出来
        仍是合法 Python」，所以 GBK 插件能装、注释里的中文也不会变成乱码。
        """
        path = self.dir / "gbk.py"
        path.write_bytes(SAMPLE_GBK.encode("gbk"))
        info = pi.inspect_local(path)
        self.assertTrue(info["ok"], info["problems"])
        self.assertIn("中文说明", info["preview"])

    def test_binary_garbage_source_returns_failure(self):
        path = self.dir / "junk.py"
        path.write_bytes(b"\x00\x01\x02\x03 junk \x00")
        info = pi.inspect_local(path)
        self.assertFalse(info["ok"])
        self.assertTrue(info["problems"])

    def test_zip_without_py_source_returns_failure(self):
        info = pi.inspect_local(self._zip("nosrc.zip", make_zip({"README.md": "hi"})))
        self.assertFalse(info["ok"])
        self.assertEqual(info["error_code"], "no-source")

    def test_failure_keeps_the_result_shape(self):
        ok_info = self._inspect_zip({"plugin.json": b1_manifest(), "b1_probe.py": PLUGIN_CODE})
        bad_info = pi.inspect_local(self.dir / "nope.zip")
        self.assertEqual(sorted(ok_info.keys()), sorted(bad_info.keys()))
        self.assertEqual(ok_info["error"], "")
        self.assertEqual(ok_info["error_code"], "")


class DangerDetectionTests(DialectTestCase):
    def test_terms_in_comments_and_strings_are_not_calls(self):
        code = ('# 这里 os.system 只是注释\n'
                'TEXT = "subprocess.run 也不是调用"\n'
                'NOTE = """eval 与 exec 写在文档串里"""\n'
                'from src.plugins import PluginBase, PluginMeta\n'
                '\n'
                '\n'
                'class CommentedPlugin(PluginBase):\n'
                '    META = PluginMeta(id="commented", name="注释测试")\n'
                '\n'
                '    def handle(self, message):\n'
                '        return message\n')
        self.assertEqual(pi.check_source(code), [])

    def test_real_dangerous_usage_is_still_flagged(self):
        samples = {
            "import subprocess": "import subprocess\n",
            "aliased import": "import subprocess as sp\n",
            "from import alias": "from subprocess import run as r\n",
            "os.system": "import os\nos.system('calc')\n",
            "from os import system": "from os import system as s\ns('calc')\n",
            "eval": "eval('1')\n",
            "exec": "exec('x = 1')\n",
            "rmtree": "import shutil\nshutil.rmtree('x')\n",
            "pickle.loads": "import pickle\npickle.loads(b'')\n",
        }
        for label, snippet in samples.items():
            with self.subTest(label):
                problems = pi.check_source(snippet + MARKERS_ONLY)
                self.assertTrue(problems, label)
                self.assertTrue(any("禁止" in p for p in problems), problems)


class PluginIdTests(unittest.TestCase):
    def test_normalization_unchanged(self):
        self.assertEqual(pi.normalize_plugin_id("  Owner-Identity "), "owner_identity")
        self.assertEqual(pi.normalize_plugin_id("b1_probe"), "b1_probe")
        with self.assertRaises(pi.InstallError):
            pi.normalize_plugin_id("Bad Id")


if __name__ == "__main__":
    unittest.main()
