"""构建元数据测试：exe 版本资源生成器 + 安装器静默安装选项。

守住两个已复现的缺陷，避免它们再回来：
  D1  打出来的 exe 没有任何版本资源（文件版本/产品名称/公司全空）——根因是 PyInstaller
      命令行只给了 --icon，没有 --version-file。
  D2  /VERYSILENT 静默安装仍会建桌面快捷方式——Inno 的 UsePreviousTasks 默认开启，
      沿用上一版安装时勾选过的 desktopicon 任务。

跑法（仓库根）：
    python -m unittest discover -s tests -p "test_build_metadata.py" -v
"""

import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GENERATOR = ROOT / ".build-tools" / "make_version_file.py"
BUILD_SCRIPT = ROOT / "build_installer.ps1"
INSTALLER_SCRIPT = ROOT / "installer.iss"

VERSION = "5.2.106"
FULL_VERSION = "5.2.106.0"
PRODUCT_NAME = "鲸鱼娘 QQ 账号版"
ORIGINAL_FILENAME = "WhaleGirlQQBridge.exe"
REQUIRED_FIELDS = (
    "CompanyName",
    "FileDescription",
    "FileVersion",
    "LegalCopyright",
    "OriginalFilename",
    "ProductName",
    "ProductVersion",
)


def load_generator():
    """按路径加载 .build-tools/make_version_file.py（它不是包，也不在 sys.path 上）。"""
    spec = importlib.util.spec_from_file_location("make_version_file_under_test", GENERATOR)
    if spec is None or spec.loader is None:
        raise AssertionError(f"加载不了版本资源生成器：{GENERATOR}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evaluation_namespace():
    """把 VSVersionInfo 那一套换成「摊平成 dict」的桩。

    生成器产出的文本必须是单个表达式（PyInstaller 6 用 eval() 读它，见
    PyInstaller/utils/win32/versioninfo.py 的 load_version_info_from_text_file）。
    用桩求值就能拿到 字段名 -> 值 的 dict，逐项断言，而不需要装 PyInstaller。
    """

    def StringStruct(name, val):
        return {"__struct__": name, "val": val}

    def StringTable(name, kids):
        return {kid["__struct__"]: kid["val"] for kid in kids}

    def StringFileInfo(kids):
        merged = {}
        for kid in kids:
            merged.update(kid)
        return merged

    def VarStruct(name, kids):
        return {"__struct__": name, "kids": kids}

    def VarFileInfo(kids):
        return {"__varfileinfo__": kids}

    def FixedFileInfo(filevers=(0, 0, 0, 0), prodvers=(0, 0, 0, 0), **kwargs):
        return {"filevers": tuple(filevers), "prodvers": tuple(prodvers)}

    def VSVersionInfo(ffi=None, kids=None):
        info = dict(ffi or {})
        for kid in kids or []:
            info.update(kid)
        return info

    return {
        "StringStruct": StringStruct,
        "StringTable": StringTable,
        "StringFileInfo": StringFileInfo,
        "VarStruct": VarStruct,
        "VarFileInfo": VarFileInfo,
        "FixedFileInfo": FixedFileInfo,
        "VSVersionInfo": VSVersionInfo,
    }


def installed_app_name():
    """installer.iss 里的产品名口径（#define AppName），用来对齐 CompanyName/ProductName。"""
    text = INSTALLER_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r'#define\s+AppName\s+"([^"]+)"', text)
    if not match:
        raise AssertionError("installer.iss 里找不到 #define AppName")
    return match.group(1)


class VersionFileTest(unittest.TestCase):
    """D1：生成器产出的版本资源文本必须覆盖全部字段、四段式版本号、可被求值。"""

    def setUp(self):
        self.generator = load_generator()

    def test_text_carries_version_and_every_required_field(self):
        text = self.generator.render(VERSION)
        self.assertIn(FULL_VERSION, text)
        for field in REQUIRED_FIELDS:
            self.assertIn(field, text, f"版本资源文本缺字段 {field}")

    def test_text_execs_into_field_dict(self):
        text = self.generator.render(VERSION)
        namespace = evaluation_namespace()

        # exec：整份文本作为语句块也不报错（单表达式 + 注释）。
        exec(text, dict(namespace))

        info = eval(text, dict(namespace))  # noqa: S307 - 被求值的是仓库自己生成的脚本
        self.assertIsInstance(info, dict)

        for field in REQUIRED_FIELDS:
            self.assertIn(field, info, f"版本资源缺字段 {field}")
            self.assertTrue(info[field], f"版本资源字段 {field} 是空的")

        self.assertEqual(info["FileVersion"], FULL_VERSION)
        self.assertEqual(info["ProductVersion"], FULL_VERSION)
        expected = tuple(int(part) for part in FULL_VERSION.split("."))
        self.assertEqual(info["filevers"], expected)
        self.assertEqual(info["prodvers"], expected)
        self.assertEqual(info["ProductName"], PRODUCT_NAME)
        self.assertEqual(info["OriginalFilename"], ORIGINAL_FILENAME)
        self.assertTrue(info["LegalCopyright"].startswith("Copyright"))

    def test_names_match_installer_app_name(self):
        """installer.iss 的 [Setup] 没有 AppPublisher（Inno 的发布者回落到 AppName），
        所以 exe 里的公司名/产品名必须跟 AppName 一个口径，不能在两处各写一份。"""
        expected = installed_app_name()
        self.assertEqual(expected, PRODUCT_NAME)
        fields = self.generator.version_fields(VERSION)
        self.assertEqual(fields["ProductName"], expected)
        self.assertEqual(fields["CompanyName"], expected)

    def test_version_is_always_four_segments(self):
        self.assertEqual(self.generator.version_tuple(VERSION), tuple(int(part) for part in FULL_VERSION.split(".")))
        self.assertEqual(self.generator.version_string(VERSION), FULL_VERSION)
        self.assertEqual(self.generator.version_string("5.2"), "5.2.0.0")
        self.assertEqual(self.generator.version_string("5"), "5.0.0.0")
        self.assertEqual(self.generator.version_string(FULL_VERSION), FULL_VERSION)

    def test_bad_version_is_rejected(self):
        for bad in ("", "5.2.57.0.1", "5.x.57", "５.2.57", "5.2.65536"):
            with self.assertRaises(ValueError, msg=f"应当拒绝 {bad!r}"):
                self.generator.version_tuple(bad)

    def test_reads_version_txt_with_crlf(self):
        """version.txt 是 CRLF 结尾的，读出来不能带 \\r。"""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "version.txt").write_bytes(f"{VERSION}\r\n".encode("utf-8"))
            self.assertEqual(self.generator.read_version(tmp), VERSION)

    def test_default_output_stays_out_of_repo_root_and_dist(self):
        out = self.generator.default_output(VERSION)
        self.assertEqual(out.parent, ROOT / ".build-tools")
        self.assertEqual(out.name, f"version_info_{FULL_VERSION}.txt")

    def test_write_version_file_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.generator.write_version_file(VERSION, Path(tmp) / "nested" / "vi.txt")
            self.assertTrue(path.is_file())
            raw = path.read_bytes()
            self.assertNotIn(b"\r", raw)  # 原样 LF 落盘，不做 CRLF 转换
            text = raw.decode("utf-8")
            self.assertIn(FULL_VERSION, text)


def _pyinstaller_versioninfo():
    """装得上 PyInstaller 就返回它的 versioninfo 模块，否则 None（skipped）。"""
    try:
        from PyInstaller.utils.win32 import versioninfo
    except Exception:
        return None
    return versioninfo


@unittest.skipUnless(_pyinstaller_versioninfo(), "本解释器没装 PyInstaller，跳过真实加载器校验")
class PyInstallerContractTest(unittest.TestCase):
    """真 PyInstaller 的 eval() 契约：产出的文本必须能被它读成 VSVersionInfo 并序列化。"""

    def test_loader_accepts_generated_file(self):
        generator = load_generator()
        versioninfo = _pyinstaller_versioninfo()
        with tempfile.TemporaryDirectory() as tmp:
            path = generator.write_version_file(VERSION, Path(tmp) / "vi.txt")
            info = versioninfo.load_version_info_from_text_file(str(path))
        self.assertIsInstance(info, versioninfo.VSVersionInfo)
        self.assertTrue(info.toRaw())


class BuildScriptTest(unittest.TestCase):
    """D1：PyInstaller 命令行必须带 --version-file，且指向 .build-tools 下的生成物。

    build_installer.ps1 必须按 utf-8-sig 读：它带 UTF-8 BOM，正文是中文。Windows PowerShell
    5.1 对**无 BOM** 的 UTF-8 脚本按 cp936 解码，中文字符串里的引号会被吞掉，脚本在解析阶段
    就报 "Unexpected token"、退出码 1、构建零产出（实测日志见 .tmp/inv/reg5257/build/）。
    """

    def ps1_text(self):
        return BUILD_SCRIPT.read_text(encoding="utf-8-sig")

    def test_pyinstaller_command_line_has_version_file(self):
        text = self.ps1_text()
        command_lines = [line for line in text.splitlines() if "PyInstaller" in line and "--onedir" in line]
        self.assertTrue(command_lines, "build_installer.ps1 里找不到 PyInstaller 命令行")
        for line in command_lines:
            self.assertIn("--version-file", line, "PyInstaller 命令行缺 --version-file（D1 会复现）")
            self.assertIn("$versionInfoFile", line)

    def test_pyinstaller_command_ships_qqagent_runtime_payload(self):
        # 能力桥的宿主载荷（host-one.mjs + src/ + node_modules/）必须随包：冻结态
        # qqagent_host.RUNTIME_PAYLOAD_DIR 指向 _internal/runtime/qqagent-host，
        # 不打进去市场装的扩展就永远跑不起来（能力桥状态恒为“还缺 host-one.mjs”）。
        text = self.ps1_text()
        joined = "\n".join(line for line in text.splitlines()
                           if "PyInstaller" in line and "--onedir" in line)
        self.assertIn("runtime/qqagent-host;runtime/qqagent-host", joined,
                      "PyInstaller 命令行没打包 runtime/qqagent-host")
        self.assertIn("runtime/node;runtime/node", joined,
                      "PyInstaller 命令行没打包 runtime/node")
        self.assertTrue((ROOT / "runtime" / "qqagent-host" / "host-one.mjs").is_file(),
                        "仓库里缺 runtime/qqagent-host/host-one.mjs，打包清单与载荷不同步")
        self.assertTrue((ROOT / "runtime" / "node" / "node.exe").is_file(),
                        "仓库里缺 runtime/node/node.exe，干净机器上的扩展加载会失败")
        self.assertTrue((ROOT / "runtime" / "node" / "LICENSE").is_file(),
                        "仓库里缺 Node 的 LICENSE，不能静默分发二进制")

    def test_version_file_is_generated_into_build_tools(self):
        text = self.ps1_text()
        self.assertIn("make_version_file.py", text, "build_installer.ps1 没有调用版本资源生成器")
        self.assertIn(".build-tools/version_info_", text)
        self.assertNotIn("dist/version_info_", text)

    def test_build_script_starts_with_utf8_bom(self):
        self.assertEqual(
            BUILD_SCRIPT.read_bytes()[:3],
            b"\xef\xbb\xbf",
            "build_installer.ps1 缺 UTF-8 BOM：Windows PowerShell 5.1 会按 cp936 解码正文，解析阶段直接失败",
        )

    def test_build_script_is_not_pure_ascii(self):
        # 正文里确实有非 ASCII（中文注释与中文报错串），所以 BOM 不是可有可无的装饰。
        data = BUILD_SCRIPT.read_bytes()
        self.assertTrue(any(byte > 0x7F for byte in data[3:]), "正文已是纯 ASCII，这条 BOM 约束可以放宽")

    def test_chinese_hint_survives_utf8_sig_read(self):
        text = self.ps1_text()
        self.assertIn("找不到构建用的 Python", text, "中文报错串读坏了，说明 .ps1 的编码/读法不对")
        self.assertNotIn("\ufeff", text, "按 utf-8-sig 读仍残留 BOM 字符，读法不对")


class InstallerScriptTest(unittest.TestCase):
    """D2：静默安装不再沿用上一版任务，但工程里仍保留可选的桌面图标任务。"""

    @staticmethod
    def section(text, name):
        """取一个 ini 段落的内容（不含段头）。按行找段头，注释里提到别的段名不会干扰。"""
        lines = text.splitlines()
        if name not in [line.strip() for line in lines]:
            raise AssertionError(f"installer.iss 里找不到段落 {name}")
        collected = []
        inside = False
        for line in lines:
            stripped = line.strip()
            if not inside:
                if stripped == name:
                    inside = True
                continue
            if stripped.startswith("[") and stripped.endswith("]"):
                break
            collected.append(line)
        return "\n".join(collected)

    def test_setup_disables_previous_tasks(self):
        text = INSTALLER_SCRIPT.read_text(encoding="utf-8")
        setup = self.section(text, "[Setup]")
        directives = [
            line.strip()
            for line in setup.splitlines()
            if line.strip() and not line.strip().startswith(";")
        ]
        self.assertIn(
            "UsePreviousTasks=no",
            directives,
            "[Setup] 缺 UsePreviousTasks=no，/VERYSILENT 会沿用上版的 desktopicon（D2 会复现）",
        )

    def test_desktop_icon_is_still_an_optional_task(self):
        text = INSTALLER_SCRIPT.read_text(encoding="utf-8")
        tasks = self.section(text, "[Tasks]")
        self.assertIn('Name: "desktopicon"', tasks, "[Tasks] 的 desktopicon 被删了，交互安装就没法选桌面图标了")
        self.assertIn("Tasks: desktopicon", text, "[Icons] 的桌面快捷方式没有绑定 desktopicon 任务")


if __name__ == "__main__":
    unittest.main()
