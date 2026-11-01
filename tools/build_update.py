#!/usr/bin/env python3
r"""生成鲸鱼娘轻量更新包（增量差分 + 校验 + 回滚），纯标准库。

安装包已改成 onedir（{app}\WhaleGirlQQBridge.exe + {app}\_internal\...），
所以「改一行代码」只需要换掉真正变了的文件，而不是重下 70+MB 的整包。

用法：
    python tools/build_update.py --old <旧版本目录> --new <新版本目录> \
        --version 5.2.52 --out update/WhaleGirlQQBridge-Update-5.2.52.zip [--base 5.2.51]

产出 zip：
    files/<相对路径>      新增或内容变化的文件（sha256 相同的不放）
    manifest.json         format/version/base_version/generated_at/files[]/delete[]
    apply_update.py       随包分发的应用脚本（--target / --dry-run / --force，失败自动回滚）
    README-更新说明.txt    一页纸使用说明

旧版本目录 / 新版本目录都是「安装目录」的样子（含 WhaleGirlQQBridge.exe），
正式流程里 --new 直接用 dist/<ver>/WhaleGirlQQBridge。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile
from datetime import datetime, timezone

FORMAT = "whale-update"
ENTRY_EXE = "WhaleGirlQQBridge.exe"
VERSION_FILE = "version.txt"
BACKUP_PREFIX = ".update-backup-"
MANIFEST_NAME = "manifest.json"
APPLY_NAME = "apply_update.py"
README_NAME = "README-更新说明.txt"


def pin_output_encoding():
    """把本进程的 stdout/stderr 钉成 UTF-8，免得中文在 cp936 控制台变乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass


APPLY_SCRIPT = r'''#!/usr/bin/env python3
"""应用鲸鱼娘轻量更新包（纯标准库，Windows 友好）。

用法：
    python apply_update.py --target "<安装目录>" [--dry-run] [--force]

    --target   安装目录（里面必须能找到 WhaleGirlQQBridge.exe）
    --dry-run  只打印将要新增/覆盖/删除哪些文件，一个字节都不改
    --force    目标 version.txt 与更新包的 base_version 不一致时仍然继续

退出码：0 成功 / 1 校验失败已回滚 / 2 环境不符（不是安装目录、包损坏、版本不对）
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile

FORMAT = "whale-update"
ENTRY_EXE = "WhaleGirlQQBridge.exe"
VERSION_FILE = "version.txt"
BACKUP_PREFIX = ".update-backup-"


def pin_output_encoding():
    """把 stdout/stderr 钉成 UTF-8。

    不钉的话在 cp936 终端里 print 的中文会变成乱码，用户看到的是一屏问号；
    反过来在 UTF-8 代码页里被当 GBK 读也是乱码。编码由脚本自己决定，不靠环境变量。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass


def sha256_file(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest().lower()


def safe_rel(rel):
    """把清单里的相对路径规范成 POSIX 形式；越界路径直接拒绝。"""
    text = str(rel).replace("\\", "/")
    if not text or text.startswith("/") or ":" in text.split("/")[0]:
        raise ValueError("清单里出现非法路径：" + str(rel))
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ValueError("清单里出现非法路径：" + str(rel))
    return "/".join(parts)


def join_rel(root, rel):
    return os.path.join(root, *safe_rel(rel).split("/"))


def resolve_source(package):
    """找到含 manifest.json 的包目录；脚本被打包成 zip 时先解压到临时目录。"""
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.isfile(os.path.join(here, "manifest.json")):
        return here
    candidate = package
    if not candidate:
        zips = [n for n in sorted(os.listdir(here)) if n.lower().endswith(".zip")]
        if len(zips) == 1:
            candidate = os.path.join(here, zips[0])
    if not candidate or not os.path.isfile(candidate):
        raise ExitTwo("找不到 manifest.json：请把 update.zip 解压后再运行，或用 --package 指定 zip 路径")
    tmp = tempfile.mkdtemp(prefix="whale-update-")
    with zipfile.ZipFile(candidate) as archive:
        archive.extractall(tmp)
    if not os.path.isfile(os.path.join(tmp, "manifest.json")):
        raise ExitTwo("这个 zip 不是鲸鱼娘更新包（缺 manifest.json）：" + candidate)
    print("[信息] 已把更新包解压到临时目录：" + tmp)
    return tmp


class ExitTwo(Exception):
    """环境不符，退出码 2。"""


def load_manifest(source):
    path = os.path.join(source, "manifest.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ExitTwo("读不了 manifest.json：" + str(exc))
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise ExitTwo("manifest.json 的 format 不是 " + FORMAT)
    files = manifest.get("files")
    deletes = manifest.get("delete", [])
    if not isinstance(files, list) or not isinstance(deletes, list):
        raise ExitTwo("manifest.json 结构不对：files/delete 必须是数组")
    return manifest


def prune_empty_dirs(path, stop):
    stop = os.path.abspath(stop)
    current = os.path.abspath(path)
    while current != stop and current.startswith(stop):
        try:
            os.rmdir(current)
        except OSError:
            return
        current = os.path.dirname(current)


def restore_one(backup_root, target, rel):
    src = join_rel(backup_root, rel)
    dst = join_rel(target, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        try:
            os.remove(dst)
        except PermissionError:
            os.chmod(dst, 0o666)
            os.remove(dst)
    shutil.copy2(src, dst)


def rollback(target, backup_root, applied, created, removed):
    """把已经动过的文件恢复原样；返回失败清单（空表示回滚干净）。"""
    failed = []
    for rel in reversed(applied):
        try:
            if rel in created:
                dst = join_rel(target, rel)
                if os.path.exists(dst):
                    try:
                        os.remove(dst)
                    except PermissionError:
                        os.chmod(dst, 0o666)
                        os.remove(dst)
            else:
                restore_one(backup_root, target, rel)
        except OSError as exc:
            failed.append(rel + "(" + str(exc) + ")")
    for rel in reversed(removed):
        try:
            restore_one(backup_root, target, rel)
        except OSError as exc:
            failed.append(rel + "(" + str(exc) + ")")
    return failed


def main(argv=None):
    # 必须在任何 print / argparse 报错之前钉死编码，否则中文在 cp936 终端里直接乱码。
    pin_output_encoding()
    parser = argparse.ArgumentParser(description="应用鲸鱼娘轻量更新包")
    parser.add_argument("--target", required=True, help="安装目录（含 WhaleGirlQQBridge.exe）")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不改任何文件")
    parser.add_argument("--force", action="store_true", help="版本对不上也继续")
    parser.add_argument("--package", help="更新包 zip 路径（脚本已解压时可省略）")
    args = parser.parse_args(argv)

    target = os.path.abspath(args.target)
    if not os.path.isdir(target) or not os.path.isfile(os.path.join(target, ENTRY_EXE)):
        print("[环境不符] 目标目录里找不到 " + ENTRY_EXE + "，这不像鲸鱼娘的安装目录：" + target)
        print("           安装目录一般是 %LOCALAPPDATA%\\Programs\\WhaleGirlQQBridge")
        return 2

    try:
        source = resolve_source(args.package)
        manifest = load_manifest(source)
    except ExitTwo as exc:
        print("[环境不符] " + str(exc))
        return 2

    version = str(manifest.get("version", "")).strip() or "未知"
    base_version = str(manifest.get("base_version", "")).strip() or "未知"
    files = manifest["files"]
    deletes = manifest["delete"]
    print("[信息] 更新包版本 " + version + "，基础版本 " + base_version + "，改动文件 " + str(len(files)) + " 个，删除 " + str(len(deletes)) + " 个")

    installed = os.path.join(target, VERSION_FILE)
    if os.path.isfile(installed):
        try:
            with open(installed, "r", encoding="utf-8", errors="replace") as handle:
                current = handle.read().strip()
        except OSError:
            current = ""
        if current and base_version != "未知" and current != base_version and not args.force:
            print("[环境不符] 当前安装版本是 " + current + "，这个更新包要求 " + base_version + "。")
            print("           跨版本请直接下载完整安装包；确认可以硬上就加 --force。")
            return 2

    expected = {}
    planned = []
    problems = []
    for item in files:
        try:
            rel = safe_rel(item.get("path"))
        except ValueError as exc:
            problems.append(str(exc))
            continue
        want = str(item.get("sha256", "")).lower()
        src = os.path.join(source, "files", *rel.split("/"))
        if not os.path.isfile(src):
            problems.append("更新包里缺少文件：" + rel)
            continue
        got = sha256_file(src)
        if want and got != want:
            problems.append("更新包内容被改动过（sha256 不符）：" + rel)
            continue
        expected[rel] = want or got
        dst = join_rel(target, rel)
        planned.append((rel, dst, os.path.isfile(dst)))

    if problems:
        print("[校验失败] " + str(len(problems)) + " 个问题，未改动任何目标文件：")
        for text in problems:
            print("  - " + text)
        return 1

    print("[信息] " + ("预演（--dry-run），不会改任何文件：" if args.dry_run else "开始应用："))
    for rel, dst, exists in planned:
        print("  " + ("覆盖 " if exists else "新增 ") + rel)
    for rel in deletes:
        try:
            clean = safe_rel(rel)
        except ValueError as exc:
            print("[校验失败] " + str(exc))
            return 1
        if os.path.isfile(join_rel(target, clean)):
            print("  删除 " + clean)

    if args.dry_run:
        print("[完成] 预演结束，实际未改动任何文件。")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_root = os.path.join(target, BACKUP_PREFIX + stamp)
    applied = []
    created = []
    removed = []
    try:
        os.makedirs(backup_root, exist_ok=True)
        for rel, dst, exists in planned:
            if exists:
                saved = join_rel(backup_root, rel)
                os.makedirs(os.path.dirname(saved), exist_ok=True)
                shutil.copy2(dst, saved)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(source, "files", *rel.split("/")), dst)
            applied.append(rel)
            if not exists:
                created.append(rel)
        for rel in deletes:
            clean = safe_rel(rel)
            dst = join_rel(target, clean)
            if not os.path.isfile(dst):
                continue
            saved = join_rel(backup_root, clean)
            os.makedirs(os.path.dirname(saved), exist_ok=True)
            shutil.copy2(dst, saved)
            os.remove(dst)
            removed.append(clean)
            prune_empty_dirs(os.path.dirname(dst), target)

        bad = [rel for rel in applied if sha256_file(join_rel(target, rel)) != expected[rel]]
        if bad:
            raise RuntimeError("落盘后 sha256 复核不通过：" + ", ".join(bad))
    except Exception as exc:  # noqa: BLE001 - 任何异常都必须回滚，不能半装
        print("[失败] " + str(exc))
        failures = rollback(target, backup_root, applied, created, removed)
        if failures:
            print("[回滚不完整] 这些文件没能复原，请从备份目录手工恢复：" + backup_root)
            for text in failures:
                print("  - " + text)
        else:
            print("[已回滚] 目标文件已逐字节恢复原内容。")
        print("[备份保留] " + backup_root)
        return 1

    try:
        with open(os.path.join(target, VERSION_FILE), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(version + "\n")
    except OSError as exc:
        print("[提醒] 版本文件写不进去（不影响本次更新）：" + str(exc))

    shutil.rmtree(backup_root, ignore_errors=True)
    print("[成功] 已更新到 " + version + "，改动 " + str(len(applied)) + " 个文件，删除 " + str(len(removed)) + " 个。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


README_TEMPLATE = """鲸鱼娘 QQ 控制台 轻量更新包  {VERSION}
===========================================================

这个包只装了「{VERSION} 相对 {BASE} 变动的那些文件」，不是完整安装包，
所以比整包（约 75MB）小得多。适用前提：你已经装过 {BASE} 的 onedir 版本。

一、怎么用（Windows）
-----------------------------------------------------------
1. 先退出鲸鱼娘（托盘图标右键退出，或在任务管理器里结束 WhaleGirlQQBridge.exe）。
2. 找到安装目录，一般是：
       %LOCALAPPDATA%\\Programs\\WhaleGirlQQBridge
   里面应该有 WhaleGirlQQBridge.exe 和 _internal 文件夹。
3. 把这个 zip 解压到任意临时文件夹（例如 桌面\\update）。
4. 在临时文件夹里打开命令行，执行：
       python apply_update.py --target "%LOCALAPPDATA%\\Programs\\WhaleGirlQQBridge"
   只想先看看会改哪些文件（不动任何东西）：
       python apply_update.py --target "..." --dry-run
   脚本自己会把输出编码钉成 UTF-8；万一你的终端仍显示乱码，先执行
       chcp 65001
   再重跑，或者直接在资源管理器里双击 apply_update.py。
5. 看到「[成功] 已更新到 {VERSION}」就好了；然后再启动鲸鱼娘。

没装 Python 的话：本更新包可以用任何一个 Python 3.8+ 运行；
也可以解压后把 files 目录里的文件按相同相对路径手工覆盖过去，
并删掉 manifest.json 的 delete 列表里的文件——但这样就少了校验和回滚，
不推荐。

二、退出码
-----------------------------------------------------------
0  成功
1  校验失败，已经自动回滚（目标文件逐字节恢复成更新前）
2  环境不符：不是安装目录 / 包里缺文件 / 当前版本不是 {BASE} 且没加 --force

三、失败了怎么办
-----------------------------------------------------------
- 脚本自己会回滚，并在安装目录留下 .update-backup-<时间戳> 备份文件夹，
  里面是更新前的原文件；确认没问题后可以手工删掉它。
- 如果看到「[回滚不完整]」，按提示从那个备份文件夹把文件拷回去。
- 回滚之后程序仍是旧版本，可继续正常使用。

四、不适用的情况（重要）
-----------------------------------------------------------
- 老版本是一次性打包的 onefile 安装包（整包只有一个 70+MB 的
  WhaleGirlQQBridge.exe，没有 _internal 目录）：这种装法**不能**用更新包，
  请直接下载完整安装包覆盖安装。
- 跨了多个版本（比如 {BASE} 之前的老版本）：请用完整安装包。
- 想保留旧版本回退能力：更新前自己把整个安装目录复制一份。

五、包内容
-----------------------------------------------------------
files/            新增或内容变化的文件（相对安装目录的路径）
manifest.json     版本、文件清单、每个文件的 sha256、需要删除的文件清单
apply_update.py   应用脚本（纯标准库，带校验与自动回滚）
README-更新说明.txt  本文件
"""


SKIP_NAMES = {VERSION_FILE}


def sha256_file(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest().lower()


def scan(root):
    """扫出目录下每个文件的相对路径 -> {sha256, size}，跳过版本文件与备份目录。"""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise SystemExit("目录不存在：" + root)
    found = {}
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(BACKUP_PREFIX))
        for name in sorted(names):
            if name in SKIP_NAMES:
                continue
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            found[rel] = {"sha256": sha256_file(full), "size": os.path.getsize(full)}
    return found


def read_version(root):
    path = os.path.join(root, VERSION_FILE)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def build(old_dir, new_dir, version, out_zip, base=""):
    if not os.path.isfile(os.path.join(new_dir, ENTRY_EXE)):
        raise SystemExit("新版本目录里没有 " + ENTRY_EXE + "：" + new_dir)
    old = scan(old_dir)
    new = scan(new_dir)
    changed = [rel for rel in sorted(new) if old.get(rel, {}).get("sha256") != new[rel]["sha256"]]
    deleted = sorted(rel for rel in old if rel not in new)
    base_version = base or read_version(old_dir) or "未知"

    manifest = {
        "format": FORMAT,
        "version": version,
        "base_version": base_version,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "files": [{"path": rel, "sha256": new[rel]["sha256"], "size": new[rel]["size"]} for rel in changed],
        "delete": deleted,
    }

    out_zip = os.path.abspath(out_zip)
    os.makedirs(os.path.dirname(out_zip) or ".", exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for rel in changed:
            archive.write(os.path.join(new_dir, *rel.split("/")), "files/" + rel)
        archive.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        archive.writestr(APPLY_NAME, APPLY_SCRIPT)
        archive.writestr(README_NAME, README_TEMPLATE.replace("{VERSION}", version).replace("{BASE}", base_version))
    return manifest, out_zip


def main(argv=None):
    pin_output_encoding()
    parser = argparse.ArgumentParser(description="生成鲸鱼娘轻量更新包（增量）")
    parser.add_argument("--old", required=True, help="旧版本目录（安装目录的样子）")
    parser.add_argument("--new", required=True, help="新版本目录（dist/<ver>/WhaleGirlQQBridge）")
    parser.add_argument("--version", required=True, help="这次更新包对应的新版本号")
    parser.add_argument("--out", required=True, help="输出 zip 路径")
    parser.add_argument("--base", default="", help="基础版本号；默认读旧目录的 version.txt")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.old):
        raise SystemExit("旧版本目录不存在：" + args.old)
    if not os.path.isfile(os.path.join(args.old, ENTRY_EXE)):
        raise SystemExit("旧版本目录里没有 " + ENTRY_EXE + "，不像安装目录：" + args.old)

    manifest, out_zip = build(args.old, args.new, args.version, args.out, args.base)
    size = os.path.getsize(out_zip)
    changed_bytes = sum(item["size"] for item in manifest["files"])
    print("[完成] " + out_zip)
    print("       版本 " + manifest["version"] + "（基于 " + manifest["base_version"] + "）")
    print("       files 条目 " + str(len(manifest["files"])) + " 个 / delete 条目 " + str(len(manifest["delete"])) + " 个")
    print("       未压缩内容 " + str(changed_bytes) + " B，zip " + str(size) + " B")
    print("       sha256 " + sha256_file(out_zip))
    return 0


if __name__ == "__main__":
    sys.exit(main())
