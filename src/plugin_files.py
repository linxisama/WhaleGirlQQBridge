"""Local user-selected plugin assets. No network, execution or configuration writes."""
import base64
import hashlib
import re
import uuid
from pathlib import Path

from .image_input import image_source

IMAGE_EXTENSIONS={'.png','.jpg','.jpeg','.gif','.webp'}

# 单文件上限区间。原来写死 1–64，而 pc_control 的「额外程序」声明了 100MB
# （可独立运行的 EXE 超 64MB 很常见），于是 upload_options 在**弹文件框之前**就抛错，
# 用户点「上传文件…」永远打不开对话框。这里把区间放宽到 256MB，并让插件的声明说了算。
MIN_FILE_MB=1
MAX_FILE_MB=256
# 一次上传的总量硬上限：既防误选一整个目录，也不至于把磁盘写满。
TOTAL_LIMIT_MB=1024
CHUNK=1024*1024


def upload_options(spec):
    mode=spec.get('file_picker','')
    if mode not in {'file','files','labeled_files','directory'}:
        raise ValueError('插件未声明可用的本地文件选择方式')
    extensions=[str(v).lower() for v in spec.get('file_extensions',[])]
    if any(not re.fullmatch(r'\.[a-z0-9]{1,10}',v) for v in extensions):
        raise ValueError('插件文件类型配置无效')
    limit=spec.get('file_max_mb',6)
    if isinstance(limit,bool) or not isinstance(limit,int) or not MIN_FILE_MB<=limit<=MAX_FILE_MB:
        raise ValueError(f'插件单文件上限须为{MIN_FILE_MB}–{MAX_FILE_MB}MB')
    return mode,extensions,limit*1024*1024


def _digest(path,limit):
    """流式计算 sha256 与大小；超过上限立刻抛错（不再把整文件读进内存）。"""
    digest=hashlib.sha256();size=0
    with path.open('rb') as handle:
        while True:
            chunk=handle.read(CHUNK)
            if not chunk:
                break
            size+=len(chunk)
            if size>limit:
                raise ValueError(f'{path.name} 超过单文件 {limit//1024//1024}MB 上限')
            digest.update(chunk)
    if not size:
        raise ValueError(f'{path.name} 为空文件')
    return digest.hexdigest(),size


def import_plugin_files(root,plugin_id,field,paths,spec):
    mode,extensions,limit=upload_options(spec)
    if mode=='directory':
        raise ValueError('目录应通过文件夹选择器设置')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}',plugin_id) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}',field):
        raise ValueError('插件或字段标识无效')
    if not paths or len(paths)>20 or (mode=='file' and len(paths)!=1):
        raise ValueError('一次请选择1–20个文件，单文件字段只能选择1个')
    # 总量上限跟着"允许的单文件上限"走：选一个 100MB 的 EXE 必须能成功，
    # 否则插件声明 100MB 就还是形同虚设。
    total_limit=max(64*1024*1024,limit*min(len(paths),20))
    total_limit=min(total_limit,TOTAL_LIMIT_MB*1024*1024)
    pending=[]
    total=0
    # Validate the complete selection before creating any managed files.
    for raw in paths:
        path=Path(raw)
        if not path.is_file():
            raise ValueError('选中的文件不存在，请重新选择')
        extension=path.suffix.lower()
        if extensions and extension not in extensions:
            raise ValueError(f'不支持的文件类型：{path.name}')
        if extension and not re.fullmatch(r'\.[a-z0-9]{1,10}',extension):
            raise ValueError('文件扩展名无效')
        if path.stat().st_size>limit:
            raise ValueError(f'{path.name} 超过单文件 {limit//1024//1024}MB 上限')
        digest,size=_digest(path,limit)
        if extension in IMAGE_EXTENSIONS:
            # 图片校验只需要文件头；上限取 8MB（image_source 本身的内联上限），
            # 不能因为插件把 file_max_mb 调大就整块读进内存。
            with path.open('rb') as handle:
                head=handle.read(min(limit,8*1024*1024)+1)
            if not image_source('base64://'+base64.b64encode(head).decode()):
                raise ValueError(f'{path.name} 不是受支持的图片')
        total+=size
        if total>total_limit:
            raise ValueError(f'一次上传总大小不能超过 {total_limit//1024//1024}MB，请分批选择')
        pending.append((path.name,path.stem,digest+extension,path,size))
    root=Path(root).resolve()
    destination=(root/plugin_id/field).resolve()
    if not destination.is_relative_to(root):
        raise ValueError('上传目录无效')
    destination.mkdir(parents=True,exist_ok=True)
    result=[]
    for name,label,filename,source,size in pending:
        target=destination/filename
        if not target.resolve().is_relative_to(destination):
            raise ValueError('上传文件路径无效')
        if target.exists():
            if not target.is_file():
                raise ValueError('已保存文件校验失败，请检查数据目录')
            existing_digest,_=_digest(target,limit)
            if existing_digest!=filename.rsplit('.',1)[0]:
                raise ValueError('已保存文件校验失败，请检查数据目录')
        else:
            # 边复制边校验：大文件（几十上百 MB 的 EXE）不再整块读进内存。
            temporary=destination/(uuid.uuid4().hex+'.tmp')
            digest=hashlib.sha256();written=0
            try:
                with source.open('rb') as src,temporary.open('wb') as dst:
                    while True:
                        chunk=src.read(CHUNK)
                        if not chunk:
                            break
                        digest.update(chunk);written+=len(chunk)
                        dst.write(chunk)
                if written!=size or digest.hexdigest()!=filename.rsplit('.',1)[0]:
                    raise ValueError(f'{name} 复制过程中被改动，请重新选择')
                temporary.replace(target)
            finally:
                if temporary.exists():
                    temporary.unlink()
        result.append({'name':name,'label':label,'path':str(target)})
    return result


def merge_uploaded_files(current,items,mode):
    if mode=='file':
        return items[0]['path'] if items else current
    if mode not in {'files','labeled_files'}:
        raise ValueError('不支持此文件字段')
    current=str(current or '')
    lines=current.splitlines()
    labels=set()
    existing=set()
    for line in lines:
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if mode=='labeled_files':
            label,separator,value=line.partition('|')
            if separator:
                labels.add(label.strip())
                existing.add(value.strip().casefold())
        else:
            existing.add(line.strip().casefold())
    additions=[]
    for item in items:
        if item['path'].casefold() in existing:
            continue
        if mode=='labeled_files':
            label=re.sub(r'[|\[\]\r\n\t]','_',item['label']).strip()[:40] or '文件'
            candidate=label
            index=2
            while candidate in labels:
                suffix=f'_{index}'
                candidate=label[:40-len(suffix)]+suffix
                index+=1
            labels.add(candidate)
            additions.append(candidate+' | '+item['path'])
        else:
            additions.append(item['path'])
        existing.add(item['path'].casefold())
    if not additions:
        return current
    return current.rstrip() + ('\n' if current.strip() else '') + '\n'.join(additions)
