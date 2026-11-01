"""人设文件格式兼容；文件内提示词只是待导入的数据，不在导入时执行。"""
from pathlib import Path
import yaml

MAX_TEXT = 256 * 1024
MAX_FILE = 8 * 1024 * 1024


def read_persona_file(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE:
        raise ValueError('文件太大（带图片的人设上限 8 MB）')
    encoding = 'utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
    return raw.decode(encoding)


def parse_persona(content, filename=''):
    if not isinstance(content, str) or not content.strip():
        raise ValueError('文件是空的')
    size = len(content.encode('utf-8'))
    if size > MAX_FILE:
        raise ValueError('文件太大（带图片的人设上限 8 MB）')
    plain = Path(filename).suffix.lower() == '.txt'
    try:
        data = yaml.safe_load(content.lstrip('\ufeff'))
    except yaml.YAMLError as exc:
        if not plain:
            raise ValueError('文件不是有效的人设（YAML 解析失败），请检查缩进和引号') from exc
        data = None
    if plain and (not isinstance(data, dict) or not any(k in data for k in ('system_prompt', 'persona', 'format'))):
        data = {'system_prompt': content.strip(), 'display_name': Path(filename).stem}
    if not isinstance(data, dict):
        raise ValueError('文件内容不是一个人设（应是一组键值）')
    if 'format' in data or 'persona' in data:
        if data.get('format') not in (None, 'WhaleGirlPersona/v1'):
            raise ValueError('不支持这个人设文件版本，请导出为 WhaleGirlPersona/v1')
        if not isinstance(data.get('persona'), dict):
            raise ValueError('人设文件中的 persona 应是一组人设字段')
        envelope = data
        data = dict(envelope['persona'])
        for key in ('image_data', 'image_ext'):
            if key in envelope:
                data[key] = envelope[key]
    prompt = data.get('system_prompt')
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('这个人设没有「人设本体」（system_prompt），导入后不能用')
    if size > MAX_TEXT and not isinstance(data.get('image_data'), str):
        raise ValueError('文件太大（纯文字人设上限 256 KB）')
    return data
