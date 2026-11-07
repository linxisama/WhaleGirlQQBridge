"""Build an allowlisted, credential-free user program folder; no agent records."""
import argparse
import hashlib
import json
import os
import re
import shutil
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

# 运行时载荷：runtime/qqagent-host 是 338 个 P3 扩展的 ../../src/* 落点，
# runtime/node 是干净机器上没装 Node 时的兜底。整树收，**不能**套 src 的后缀白名单——
# 载荷里有 .ts / .mjs / .map、.exe 和无后缀文件（LICENSE、.bin/js-yaml），
# 按后缀过滤会静默吃掉它们。
RUNTIME_PAYLOAD=('runtime',)
# DSH 接入口按文件名逐个放行：tools/ 里还有 agent_apis.py / team.py / coordination.py
# 这类协作材料，绝不能整目录复制出去。三个 MCP 桥脚本必须在列：setup_dsh_memory.py
# 默认把它们（whale_memory_mcp.py / whale_bot_mcp.py / whale_tools_mcp.py）的**发布包内路径**
# 写进 DSH 登记项，漏一个就是「登记成功、一启动就找不到脚本」。
DSH_TOOLS=('whale_bot_mcp.py','whale_memory_mcp.py','whale_tools_mcp.py','verify_whale_bot_mcp.py',
           'setup_dsh_memory.py','e2e_extension_probe.py')
# 与载荷配套的说明文档：讲清能力边界与怎么排障，用户排障时手边得有。
DSH_DOCS=('扩展能力桥-使用与排障.md','扩展能力桥-能力边界.md','技能市场-使用与排障.md','扩展与技能市场-总览.md',
          '交付说明-本轮.md','交付说明-Agent平台.md','插件接入契约-mod.md',
          '插件规格-v1.md','插件交接说明.md','新方向-Agent平台-框架.md','新方向-任务书-Agent平台.md')

def collect_paths(root=ROOT):
    """按白名单收集要打包的文件（绝对路径）；缺失的可选项跳过，不报错。"""
    root=Path(root).resolve()
    required=['qq_main.py','start.bat','requirements.qq.txt','LICENSE','QQ版使用说明.md']
    paths=[root/p for p in required]
    optional=['插件功能与接入说明.md','对话与电脑操控使用说明.md','qq_gui.py','gui_launcher.py','requirements.gui.txt','安装使用说明.txt','功能对照与使用说明.md','插件本地文件接入说明.md','icon.ico','version.txt','start-web.bat']
    paths += [root/p for p in optional if (root/p).is_file()]
    paths += [p for p in (root/'src').rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix in {'.py','.js','.html','.css','.yaml'}]
    paths += [p for p in (root/'plugin_core').rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix in {'.py','.json','.md'}]
    paths += list((root/'styles').glob('*.yaml'))
    paths += [p for p in (root/'assets').rglob('*') if p.is_file() and p.suffix.lower() in {'.png','.jpg','.jpeg','.gif','.webp','.ico','.svg','.json'}]
    paths += [p for p in (root/'third_party'/'estrella').glob('*') if p.is_file() and p.name in {'LICENSE','NOTICE','SOURCE.md'}]
    paths += [p for p in (root/'third_party'/'memes').glob('*') if p.is_file()]
    paths += [p for p in (root/'assets'/'memes').glob('*.json') if p.is_file() and p not in paths]
    payload=root.joinpath(*RUNTIME_PAYLOAD)
    if payload.is_dir():
        paths += [p for p in payload.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix.lower()!='.pyc']
    paths += [root/'tools'/name for name in DSH_TOOLS if (root/'tools'/name).is_file()]
    paths += [root/'docs'/name for name in DSH_DOCS if (root/'docs'/name).is_file()]
    return paths

def build_plugin(source, destination, archive_path):
    """Build one installable plugin with an explicit, non-recursive file allowlist."""
    from src.plugin_installer import check_source, inspect_local, normalize_plugin_id

    source = Path(source).resolve()
    destination = Path(destination).resolve()
    archive_path = Path(archive_path).resolve()
    if destination.exists() or archive_path.exists():
        raise FileExistsError('插件目标目录或压缩包已存在，拒绝覆盖')
    metadata = json.loads((source/'plugin.json').read_text(encoding='utf-8'))
    plugin_id = normalize_plugin_id(metadata.get('id'))
    entry = metadata.get('entry')
    if entry != plugin_id + '.py':
        raise ValueError('插件入口必须为与id一致的单个.py文件')
    if not re.fullmatch(r'\d+\.\d+\.\d+', str(metadata.get('version', ''))):
        raise ValueError('插件版本须为x.y.z')
    names = [entry, 'plugin.json', 'README.md', 'LICENSE']
    payload = {}
    for name in names:
        path = source/name
        if path.is_symlink() or not path.is_file() or path.resolve().parent != source:
            raise ValueError('插件白名单文件缺失或不在源目录内')
        data = path.read_bytes()
        if len(data) > 512 * 1024 or re.search(rb'sk-[A-Za-z0-9_-]{16,}', data):
            raise ValueError('插件文件过大或疑似含密钥，拒绝打包')
        payload[name] = data
    problems = check_source(payload[entry].decode('utf-8'))
    if problems:
        raise ValueError('插件源码预检失败：' + '；'.join(problems))
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}
    payload['PACKAGE-MANIFEST.json'] = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + '.building')
    staging.mkdir(exist_ok=False)
    for name, data in payload.items():
        (staging/name).write_bytes(data)
    staging.rename(destination)
    with zipfile.ZipFile(archive_path, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in payload.items():
            archive.writestr(name, data)
    checked = inspect_local(archive_path)
    if not checked['ok'] or checked['plugin_id'] != plugin_id:
        raise ValueError('插件压缩包安装预检失败')
    return {'plugin_id': plugin_id, 'version': metadata['version'],
            'directory': str(destination), 'archive': str(archive_path),
            'sha256': hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            'bytes': archive_path.stat().st_size, 'files': list(payload), 'install_precheck': True}


def build(root=ROOT, destination=None):
    root=Path(root).resolve()
    destination=Path(destination) if destination else root/'release'/('whalegirl-qq-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S'))
    destination=destination.resolve()
    if destination.exists():
        raise FileExistsError('目标目录已存在，拒绝覆盖')
    destination.parent.mkdir(parents=True,exist_ok=True)
    paths=collect_paths(root)
    manifest={}
    for path in paths:
        data=path.read_bytes()
        if re.search(rb'sk-[A-Za-z0-9_-]{16,}',data):
            raise ValueError('发布文件疑似含密钥，已拒绝打包')
        manifest[path.relative_to(root).as_posix()]=hashlib.sha256(data).hexdigest()
    staging=destination.with_name(destination.name+'.building')
    staging.mkdir(exist_ok=False)
    try:
        for path in paths:
            target=staging/path.relative_to(root)
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,target)
        (staging/'.tmp').mkdir()
        (staging/'config.qq.yaml').write_text('ai:\n  api_url: ""\n  api_key: ""\n  model: ""\nqq:\n  app_id: ""\n  client_secret: ""\nqq_only:\n  mode: official\n',encoding='utf-8')
        (staging/'BUILD-MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        staging.rename(destination)
    except Exception:
        # Preserve staging for diagnosis; never delete an unverified computed path.
        raise
    return destination


# 发布 zip 必须是脚本产物：PowerShell 5.1 的 Compress-Archive 会写出反斜杠分隔的条目名
# （当时 585 条目里 567 条反斜杠），不合 ZIP 规范，所以这里用 zipfile 自己写。
ARCHIVE_EPOCH=(2026,1,1,0,0,0)
ARCHIVE_SKIP=('__pycache__',)

def _archive_stamp(deterministic):
    """deterministic 时全体条目共用的时间戳；SOURCE_DATE_EPOCH 优先（可复现构建设定）。"""
    if not deterministic:
        return None
    epoch=os.environ.get('SOURCE_DATE_EPOCH','').strip()
    if epoch.isdigit():
        return tuple(time.gmtime(int(epoch))[:6])
    return ARCHIVE_EPOCH

def _archive_mtime(path):
    """沿用文件 mtime；DOS 时间下限 1980 年，更早的钳到 1980-01-01。"""
    stamp=tuple(time.localtime(path.stat().st_mtime)[:6])
    return stamp if stamp[0]>=1980 else (1980,1,1,0,0,0)

def _archive_paths(directory):
    """(目录, 文件) 两张相对清单，均按路径排序；跳过 __pycache__ 与 .pyc。"""
    directories,files=[],[]
    for path in sorted(directory.rglob('*')):
        if any(part in ARCHIVE_SKIP for part in path.parts):
            continue
        if path.is_dir():
            directories.append(path)
        elif path.is_file() and path.suffix.lower()!='.pyc':
            files.append(path)
    return directories,files

def _archive_member(archive,name,data,source,stamp,is_dir):
    info=zipfile.ZipInfo(name,date_time=stamp or _archive_mtime(source))
    info.compress_type=zipfile.ZIP_STORED if is_dir else zipfile.ZIP_DEFLATED
    info.external_attr=((0o40755 if is_dir else 0o644)<<16)|(0x10 if is_dir else 0)
    archive.writestr(info,data)

def write_archive(directory, archive_path, deterministic=False):
    """把发布目录写成 ZIP：条目名全用正斜杠，逐目录显式写目录条目（空目录也算），
    跳过 __pycache__ 与 .pyc；deterministic 时全体条目同一固定时间戳，同一目录两次产出
    字节完全相同。archive_path 已存在则拒绝覆盖。"""
    directory=Path(directory).resolve()
    archive_path=Path(archive_path).resolve()
    if not directory.is_dir():
        raise NotADirectoryError('发布目录不存在: '+str(directory))
    if archive_path.exists():
        raise FileExistsError('压缩包已存在，拒绝覆盖')
    stamp=_archive_stamp(deterministic)
    directories,files=_archive_paths(directory)
    archive_path.parent.mkdir(parents=True,exist_ok=True)
    # 'x' 让并发下也已存在的路径直接失败，不会写出半成品。
    with zipfile.ZipFile(archive_path,'x',compression=zipfile.ZIP_DEFLATED) as archive:
        for path in directories:
            _archive_member(archive,path.relative_to(directory).as_posix()+'/',b'',path,stamp,True)
        for path in files:
            _archive_member(archive,path.relative_to(directory).as_posix(),path.read_bytes(),path,stamp,False)
    data=archive_path.read_bytes()
    return {'entries':len(directories)+len(files),'files':len(files),'dirs':len(directories),
            'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)}

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--destination')
    parser.add_argument('--source',help='直接归档一个已存在的发布目录，跳过 build')
    parser.add_argument('--archive',help='把发布目录写成 ZIP（正斜杠条目、含目录条目、跳过 __pycache__/.pyc）')
    parser.add_argument('--deterministic',action='store_true',help='固定条目时间戳，同一目录两次产出字节相同')
    args=parser.parse_args()
    target=Path(args.source) if args.source else build(destination=args.destination)
    print(target)
    if args.archive:
        print(write_archive(target,args.archive,deterministic=args.deterministic))
