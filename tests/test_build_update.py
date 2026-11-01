"""tools/build_update.py 的端到端测试：造一份「旧版本」，生成更新包，正向应用 + 回滚。

跑法（仓库根）：
    python -m unittest discover -s tests -p "test_build_update.py" -v
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = ROOT / "tools" / "build_update.py"
ENTRY = "WhaleGirlQQBridge.exe"
VERSION = "5.2.52"
BASE = "5.2.51"

# 子进程输出编码不靠运气：显式钉住 UTF-8，父进程也按 UTF-8 解码（任何终端代码页下都成立）。
# 脚本自己也会 reconfigure 一次，这里只是让断言不依赖宿主 shell 的 PYTHONUTF8 / PYTHONIOENCODING。
CHILD_ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def snapshot(root):
    """安装目录里除备份目录外的全部文件 -> sha256。"""
    root = Path(root)
    result = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if ".update-backup-" in rel:
            continue
        result[rel] = sha256(path)
    return result


class BuildUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="whale-update-test-"))
        self.new = self.tmp / "new" / "WhaleGirlQQBridge"
        self.old = self.tmp / "old"
        write(self.new / ENTRY, b"MZ-new-exe-5.2.52")
        write(self.new / "version.txt", VERSION + "\n")
        write(self.new / "_internal" / "python312.dll", b"dll-same")
        write(self.new / "_internal" / "same.bin", b"never-changes")
        write(self.new / "_internal" / "changed.bin", b"changed-new")
        write(self.new / "_internal" / "sub" / "deep.bin", b"deep-new")
        write(self.new / "_internal" / "sub" / "nest" / "leaf.bin", b"leaf-same")

        shutil.copytree(self.new, self.old)
        write(self.old / "version.txt", BASE + "\n")
        (self.old / "_internal" / "changed.bin").write_bytes(b"changed-old")
        (self.old / "_internal" / "sub" / "deep.bin").write_bytes(b"deep-old")
        write(self.old / "_internal" / "gone1.bin", b"only-in-old-1")
        write(self.old / "_internal" / "gone2.bin", b"only-in-old-2")
        self.zip_path = self.tmp / "out" / ("WhaleGirlQQBridge-Update-" + VERSION + ".zip")

    def tearDown(self):
        for path in self.tmp.rglob("*"):
            try:
                if path.is_file():
                    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_build(self, *extra):
        return subprocess.run(
            [sys.executable, str(BUILD_SCRIPT), "--old", str(self.old), "--new", str(self.new),
             "--version", VERSION, "--out", str(self.zip_path), *extra],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=CHILD_ENV,
        )

    def extract_package(self):
        target = self.tmp / "pkg"
        with zipfile.ZipFile(self.zip_path) as archive:
            archive.extractall(target)
        return target

    def run_apply(self, package, *extra, target=None):
        return subprocess.run(
            [sys.executable, str(package / "apply_update.py"), "--target", str(target or self.old), *extra],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=CHILD_ENV,
        )

    def build_and_extract(self):
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.extract_package()

    def test_1_package_layout_and_manifest(self):
        self.build_and_extract()
        with zipfile.ZipFile(self.zip_path) as archive:
            names = sorted(archive.namelist())
        self.assertIn("manifest.json", names)
        self.assertIn("apply_update.py", names)
        self.assertIn("README-更新说明.txt", names)
        packaged = [n for n in names if n.startswith("files/")]
        self.assertEqual(sorted(packaged), ["files/_internal/changed.bin", "files/_internal/sub/deep.bin"])

        manifest = json.loads((self.tmp / "pkg" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["format"], "whale-update")
        self.assertEqual(manifest["version"], VERSION)
        self.assertEqual(manifest["base_version"], BASE)
        self.assertEqual([item["path"] for item in manifest["files"]],
                         ["_internal/changed.bin", "_internal/sub/deep.bin"])
        self.assertEqual(sorted(manifest["delete"]), ["_internal/gone1.bin", "_internal/gone2.bin"])
        self.assertEqual(manifest["files"][0]["sha256"], sha256(self.new / "_internal" / "changed.bin"))

    def test_2_dry_run_touches_nothing(self):
        package = self.build_and_extract()
        before = snapshot(self.old)
        result = self.run_apply(package, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("预演结束", result.stdout)
        self.assertEqual(snapshot(self.old), before)

    def test_3_apply_makes_target_identical_to_new(self):
        package = self.build_and_extract()
        result = self.run_apply(package)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[成功]", result.stdout)
        self.assertEqual(snapshot(self.old), snapshot(self.new))
        self.assertFalse((self.old / "_internal" / "gone1.bin").exists())
        self.assertFalse((self.old / "_internal" / "gone2.bin").exists())
        self.assertEqual((self.old / "version.txt").read_text(encoding="utf-8").strip(), VERSION)
        self.assertEqual(list(self.old.glob(".update-backup-*")), [])

    def test_4_corrupt_manifest_rolls_back_untouched(self):
        package = self.build_and_extract()
        manifest_path = package / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        before = snapshot(self.old)
        result = self.run_apply(package)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("校验失败", result.stdout)
        self.assertEqual(snapshot(self.old), before)

    def test_5_failure_midway_rolls_back_and_keeps_backup(self):
        package = self.build_and_extract()
        before = snapshot(self.old)
        blocked = self.old / "_internal" / "sub" / "deep.bin"
        os.chmod(blocked, stat.S_IREAD)
        try:
            result = self.run_apply(package)
        finally:
            os.chmod(blocked, stat.S_IWRITE | stat.S_IREAD)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("[已回滚]", result.stdout)
        self.assertEqual(snapshot(self.old), before)
        backups = list(self.old.glob(".update-backup-*"))
        self.assertEqual(len(backups), 1, result.stdout)
        self.assertEqual(sha256(backups[0] / "_internal" / "changed.bin"), before["_internal/changed.bin"])

    def test_6_rejects_directory_without_entry_exe(self):
        package = self.build_and_extract()
        plain = self.tmp / "plain"
        write(plain / "readme.txt", b"not an install dir")
        result = self.run_apply(package, target=plain)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("环境不符", result.stdout)

    def test_7_base_version_gate_and_force(self):
        package = self.build_and_extract()
        write(self.old / "version.txt", "5.0.0\n")
        result = self.run_apply(package)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        forced = self.run_apply(package, "--force")
        self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
        self.assertEqual((self.old / "version.txt").read_text(encoding="utf-8").strip(), VERSION)


if __name__ == "__main__":
    unittest.main()
