"""固定版本的模板目录、资源校验与有超时的本机生成。"""
import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

BASE = 'https://raw.githubusercontent.com/MemeCrafters/meme-generator-rs/v0.2.3/resources/images/'


def bundle():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))


@lru_cache(maxsize=1)
def catalog():
    return json.loads((bundle() / 'assets/memes/catalog.json').read_text(encoding='utf-8'))


def find_template(name):
    return next((item for item in catalog() if name == item['key'] or name in item['keywords']), None)


def download_assets(key, home):
    import httpx
    import shutil
    import threading
    from concurrent.futures import ThreadPoolExecutor
    manifest = json.loads((bundle() / 'assets/memes/resources.json').read_text(encoding='utf-8'))
    entries = [item for item in manifest['images'] if item['file'].startswith(key + '/')]
    if len(entries) > 200:
        raise ValueError('此模板资源过多，请换一个模板')
    transferred = 0
    budget_lock = threading.Lock()
    # 下载也在可终止的生成子进程内执行，超时不会留下写缓存的宿主线程。
    def download(item):
        nonlocal transferred
        target = (home / 'resources/images' / item['file']).resolve()
        if not target.is_relative_to((home / 'resources/images').resolve()):
            raise ValueError('模板资源路径无效')
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == item['hash']:
            return
        seed = bundle() / 'assets/memes/images' / item['file']
        if seed.is_file() and hashlib.sha256(seed.read_bytes()).hexdigest() == item['hash']:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(seed, target)
            return
        sources = [BASE, 'https://cdn.jsdelivr.net/gh/MemeCrafters/meme-generator-rs@v0.2.3/resources/images/']
        for source in sources:
            try:
                with httpx.Client(timeout=httpx.Timeout(10, connect=4), follow_redirects=False) as client:
                    chunks = []
                    size = 0
                    with client.stream('GET', source + quote(item['file'], safe='/')) as response:
                        response.raise_for_status()
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            with budget_lock:
                                transferred += len(chunk)
                                if transferred > 64 * 1024 * 1024:
                                    raise ValueError('模板素材超过本次64MB下载预算')
                            if size > 6 * 1024 * 1024:
                                raise ValueError('单个模板素材超过6MB')
                            chunks.append(chunk)
                data = b''.join(chunks)
                if hashlib.sha256(data).hexdigest() != item['hash']:
                    raise ValueError('模板素材校验失败')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                return
            except Exception:
                continue
        raise ValueError('模板素材下载失败，请检查网络后重试；本次未生成图片')
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(download, entries))


async def generate(home, key, images, texts, preview=False, options=None, timeout=45, download=True):
    home = Path(home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=home, prefix='job-') as temporary:
        job = Path(temporary)
        request, response = job / 'request.json', job / 'result.json'
        request.write_text(json.dumps({'home': str(home), 'key': key, 'images': [base64.b64encode(v).decode('ascii') for v in images],
                                       'texts': texts, 'preview': preview, 'options': options or {}, 'download': download}), encoding='utf-8')
        command = ([sys.executable, '--meme-worker'] if getattr(sys, 'frozen', False)
                   else [sys.executable, '-m', 'src.meme_worker'])
        env = dict(os.environ, MEME_HOME=str(home))
        process = await asyncio.create_subprocess_exec(*command, str(request), str(response),
            cwd=str(bundle()), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000 if os.name == 'nt' else 0)
        try:
            await asyncio.wait_for(process.wait(), timeout)
        finally:
            if process.returncode is None:
                if os.name == 'nt':
                    # onefile exe 有引导父进程和真正工作子进程，必须结束整棵树。
                    killer = await asyncio.create_subprocess_exec(
                        str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/taskkill.exe'),
                        '/PID', str(process.pid), '/T', '/F', stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, creationflags=0x08000000)
                    await killer.wait()
                else:
                    process.kill()
                await process.wait()
        if process.returncode != 0 or not response.is_file():
            raise ValueError('表情引擎未正常返回；本次没有生成图片，控制台可继续使用')
        result = json.loads(response.read_text(encoding='utf-8'))
        if not result.get('ok'):
            raise ValueError('表情生成失败：' + str(result.get('error', '未知原因')))
        output = Path(result['image']).resolve()
        if not output.is_relative_to(job.resolve()) or output.stat().st_size > 6 * 1024 * 1024:
            raise ValueError('表情引擎输出无效')
        return output.read_bytes()
