#!/usr/bin/env python3
"""把发布包组装到指定目录（给用户的外发包）。

与 ``tools/build_release.py`` 同样的白名单思路（不复制 agents/shared/collaboration/
tools/tests/密钥/缓存），区别是这里还带上**已构建好的 exe**，方便直接双击运行。

用法：python tools/assemble_package.py D:\\聊天机器人\\发布包
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE_NAME = "鲸鱼娘QQ控制台.exe"

# 顶层必须带的文件
REQUIRED = ["qq_main.py", "start.bat", "requirements.qq.txt", "LICENSE", "QQ版使用说明.md"]
# 整目录带上的（只挑需要的扩展名）
# assets 必须带：界面图标与 icon.ico 在里面（exe 内的图标已嵌入，但源码运行方式需要它们）
DIRS = ("src", "styles", "assets")
SUFFIXES = {".py", ".js", ".html", ".css", ".yaml", ".png", ".ico", ".json"}


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python tools/assemble_package.py <目标目录>")
        return 2
    destination = Path(sys.argv[1]).resolve()
    if destination.exists():
        print(f"目标已存在，拒绝覆盖：{destination}")
        return 1

    exe = ROOT / "dist" / EXE_NAME
    if not exe.is_file():
        print(f"找不到 exe：{exe}（先跑 tools/build_package.py）")
        return 1

    staging = destination.with_name(destination.name + ".building")
    staging.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, str] = {}
    try:
        paths = [ROOT / name for name in REQUIRED]
        for part in DIRS:
            paths += [p for p in (ROOT / part).rglob("*")
                      if p.is_file() and "__pycache__" not in p.parts and p.suffix in SUFFIXES]
        for path in paths:
            if not path.is_file():
                continue
            data = path.read_bytes()
            # 与 build_release 同款密钥扫描：疑似密钥就拒发
            if re.search(rb"sk-[A-Za-z0-9_-]{16,}", data):
                raise ValueError(f"发布文件疑似含密钥，已拒绝：{path.relative_to(ROOT)}")
            manifest[path.relative_to(ROOT).as_posix()] = hashlib.sha256(data).hexdigest()
            target = staging / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)

        # exe 放最外层，双击就能用
        shutil.copy2(exe, staging / EXE_NAME)
        manifest[EXE_NAME] = hashlib.sha256(exe.read_bytes()).hexdigest()
        # exe 图标也给一份：用户想换图标/做快捷方式时会用到
        if (ROOT / "icon.ico").is_file():
            shutil.copy2(ROOT / "icon.ico", staging / "icon.ico")
            manifest["icon.ico"] = hashlib.sha256((ROOT / "icon.ico").read_bytes()).hexdigest()

        (staging / ".tmp").mkdir(exist_ok=True)
        # 空白配置模板：密钥留空，由用户在界面里填
        (staging / "config.qq.yaml").write_text(
            'ai:\n  api_url: ""\n  api_key: ""\n  model: ""\n'
            'qq:\n  app_id: ""\n  client_secret: ""\n  backend: official\n'
            'qq_only:\n  mode: official\n',
            encoding="utf-8",
        )
        (staging / "BUILD-MANIFEST.json").write_text(
            json.dumps({"built_at": datetime.now(timezone.utc).isoformat(),
                        "files": manifest}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        staging.rename(destination)
    except Exception:
        # 保留 staging 以便排查；不删可能算错的路径
        raise
    print(f"✅ 发布包已生成：{destination}")
    print(f"   文件数：{len(manifest)}（含 {EXE_NAME}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
