"""原生表情引擎只在子进程导入；库崩溃时不会终止 QQ 控制台。"""
import base64
import json
import os
from pathlib import Path


def main(request_path, result_path):
    request = json.loads(Path(request_path).read_text(encoding='utf-8'))
    os.environ['MEME_HOME'] = request['home']
    try:
        if request.get('download', True):
            from .meme_service import download_assets
            download_assets(request['key'], Path(request['home']))
        import meme_generator as engine
        meme = engine.get_meme(request['key'])
        if request.get('preview'):
            result = meme.generate_preview()
        else:
            images = [engine.Image(str(i), base64.b64decode(value, validate=True))
                      for i, value in enumerate(request.get('images', []))]
            result = meme.generate(images, request.get('texts', []), request.get('options', {}))
        if not isinstance(result, bytes):
            kind = type(result).__name__
            detail = getattr(result, 'feedback', '') or getattr(result, 'error', '')
            raise ValueError(f'{kind}：{str(detail)[:160]}')
        if not result or len(result) > 6 * 1024 * 1024:
            raise ValueError('生成图片为空或超过 QQ 的 6MB 限制')
        output = Path(result_path).with_suffix('.image')
        output.write_bytes(result)
        response = {'ok': True, 'image': str(output)}
    except Exception as exc:
        response = {'ok': False, 'error': str(exc)[:250]}
    Path(result_path).write_text(json.dumps(response, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    import sys
    main(*sys.argv[1:3])
