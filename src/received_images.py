"""收到图片的独立缓存；只清理由本索引登记的文件，不触碰用户图库。"""
import hashlib
import json
import re
import time
from pathlib import Path


class ReceivedImages:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.index = self.root / 'index.json'
        try:
            entries = json.loads(self.index.read_text('utf-8'))
            if not isinstance(entries, dict):
                raise ValueError('图片缓存索引格式错误')
            self.entries = {k: v for k, v in entries.items()
                            if re.fullmatch(r'[0-9a-f]{64}', k) and isinstance(v, dict)
                            and v.get('ext') in ('png', 'jpg', 'gif', 'webp')}
        except FileNotFoundError:
            self.entries = {}

    def path(self, key, entry):
        path = (self.root / (key + '.' + entry['ext'])).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError('缓存文件不能指向目录之外')
        return path

    def put(self, data, ext):
        if ext not in ('png', 'jpg', 'gif', 'webp'):
            raise ValueError('缓存图片格式无效')
        key = hashlib.sha256(data).hexdigest()
        entries = {k: dict(v) for k, v in self.entries.items()
                   if self.path(k, v).is_file()}
        path = self.path(key, {'ext': ext})
        existed = path.is_file()
        if not existed:
            temp = path.with_suffix(path.suffix + '.tmp')
            temp.write_bytes(data)
            temp.replace(path)
        entries.setdefault(key, {'ext': ext, 'created': time.time()})
        removed = []
        if len(entries) > 50:
            # 51 张删除最旧的 25 张。新收到的图片至少保留到本次识别结束。
            oldest = sorted((k for k in entries if k != key),
                            key=lambda k: entries[k]['created'])[:len(entries)//2]
            for old in oldest:
                removed.append(self.path(old, entries.pop(old)))
        try:
            temp = self.index.with_suffix('.json.tmp')
            temp.write_text(json.dumps(entries, ensure_ascii=False) + '\n', encoding='utf-8')
            temp.replace(self.index)
            self.entries = entries
        except Exception:
            if not existed:
                path.unlink(missing_ok=True)
            raise
        for old in removed:
            old.unlink(missing_ok=True)
        return path
