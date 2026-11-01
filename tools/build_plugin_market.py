#!/usr/bin/env python3
"""把「市场插件」逐个打包成可导入的 zip，并写使用说明。

输出目录默认：``D:\聊天机器人\插件市场``

每个包：

```
<id>.py            # 插件源码（安装器按它加载）
plugin.json        # 标识/名称/版本/分类/说明
使用说明.md         # 功能、配置项、安装步骤、注意事项
```

生成后会再用 ``plugin_installer.inspect_local`` 预检每个 zip，确保能被控制台
「添加插件 → 本地文件」正常识别。基础插件不打包（安装包内置）。

用法::

    .venv-build\Scripts\python.exe tools\build_plugin_market.py
    ... --output "D:\聊天机器人\插件市场" --clean
"""
from __future__ import annotations

import argparse
import ast
import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import plugin_installer
from src.plugin_catalog import BASIC_PLUGINS, market_plugins
from src.runtime_identity import APP_VERSION

DEFAULT_OUTPUT = Path(r'D:\聊天机器人\插件市场')
PLUGIN_DIR = ROOT / 'src' / 'plugins'


def _literal(node, default=None):
    try:
        return ast.literal_eval(node)
    except Exception:
        return default


def _call_name(node):
    if isinstance(node, ast.Call):
        func = node.func
        return getattr(func, 'id', None) or getattr(func, 'attr', None)
    return None


FIELD_POSITIONAL = ('type', 'label', 'description', 'group', 'values', 'options',
                    'min', 'max', 'step', 'multiline', 'file_picker',
                    'file_extensions', 'file_max_mb', 'editor')


def _fields(call):
    result = {}
    if _call_name(call) == 'FieldSchema':
        for name, node in zip(FIELD_POSITIONAL, getattr(call, 'args', []) or []):
            result[name] = _literal(node, None)
    for keyword in getattr(call, 'keywords', []) or []:
        if keyword.arg:
            result[keyword.arg] = _literal(keyword.value, None)
    return result


def read_meta(path: Path) -> dict:
    """静态读取 PluginMeta 与配置 schema；不执行插件代码。"""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    meta = {'id': path.stem, 'name': path.stem, 'description': '', 'category': '',
            'enabled_by_default': False, 'config': [], 'defaults': {}}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', '') == 'META' for t in node.targets):
            call = node.value if isinstance(node.value, ast.Call) else None
            if call is None or _call_name(call) != 'PluginMeta':
                continue
            fields = _fields(call)
            for key in ('id', 'name', 'description', 'category', 'enabled_by_default'):
                if key in fields:
                    meta[key] = fields[key]
            schema = call.keywords and next((k.value for k in call.keywords if k.arg == 'config_schema'), None)
            if isinstance(schema, ast.Dict):
                for key_node, value_node in zip(schema.keys, schema.values):
                    key = _literal(key_node, '')
                    if not key:
                        continue
                    entry = {'key': key, 'label': key, 'type': '', 'default': None, 'description': ''}
                    if isinstance(value_node, ast.Call):
                        entry.update(_fields(value_node))
                        entry['key'] = key
                    meta['config'].append(entry)
            defaults = call.keywords and next((k.value for k in call.keywords if k.arg == 'default_settings'), None)
            if isinstance(defaults, ast.Dict):
                for key_node, value_node in zip(defaults.keys, defaults.values):
                    key = _literal(key_node, '')
                    if key:
                        meta['defaults'][key] = _literal(value_node, None)
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', '') == 'DEFAULTS' for t in node.targets):
            if isinstance(node.value, ast.Dict):
                for key_node, value_node in zip(node.value.keys, node.value.values):
                    key = _literal(key_node, '')
                    if key and key not in meta['defaults']:
                        meta['defaults'][key] = _literal(value_node, None)
    return meta


def instructions(meta: dict, version: str) -> str:
    lines = [
        f"# {meta['name']}（{meta['id']}）",
        '',
        f"- 版本：{version}",
        f"- 分类：{meta.get('category') or '未分类'}",
        f"- 默认启用：{'是' if meta.get('enabled_by_default') else '否'}",
        '',
        '## 功能',
        '',
        (meta.get('description') or '见插件本身说明。').strip(),
        '',
        '## 安装',
        '',
        '1. 打开鲸鱼娘 QQ 控制台 →「插件中心」；',
        '2. 点「添加插件」→「浏览…」，选择本压缩包；',
        '3. 点「检查」，确认名称、说明与安全检查通过；',
        '4. 勾选「覆盖同名插件」（更新时）和「安装后立即启用」；',
        '5. 点「确认安装」。',
        '',
        '安装后可在插件列表里调整参数；不需要时在插件中心删除。',
        '',
    ]
    config = meta.get('config') or []
    if config:
        lines += ['## 配置项', '', '| 键 | 名称 | 类型 | 默认值 | 说明 |', '|---|---|---|---|---|']
        for item in config:
            default = meta.get('defaults', {}).get(item['key'], item.get('default'))
            description = str(item.get('description') or '').replace('|', '／').replace('\n', ' ')
            lines.append(f"| `{item['key']}` | {item.get('label') or item['key']} | "
                         f"{item.get('type') or ''} | `{default}` | {description} |")
        lines.append('')
    else:
        lines += ['## 配置项', '', '此插件没有额外配置项。', '']
    lines += [
        '## 注意',
        '',
        '- 插件是外部代码，安装后会被加载执行，请只使用可信来源；',
        '- 依赖外部服务的插件（搜索、图片站、语音等）需要相应网络或本机组件；',
        '- 与聊天主链路无关的插件默认关闭，按需开启。',
        '',
    ]
    return '\n'.join(lines)


def build_one(plugin_id: str, output: Path, version: str, overwrite: bool) -> dict:
    source = PLUGIN_DIR / f'{plugin_id}.py'
    if not source.is_file():
        return {'id': plugin_id, 'ok': False, 'error': '源文件不存在'}
    meta = read_meta(source)
    if meta['id'] != plugin_id:
        # 文件标识以 META.id 为准，否则安装后会和目录名不一致
        plugin_id = str(meta['id'])
    safe_name = ''.join(ch for ch in str(meta['name']) if ch not in '\\/:*?"<>|').strip() or plugin_id
    target = output / f'{safe_name}-{plugin_id}.zip'
    if target.exists() and not overwrite:
        return {'id': plugin_id, 'ok': False, 'error': f'已存在：{target.name}'}
    manifest = {
        'format': 'whale-plugin', 'version': 1,
        'id': plugin_id, 'name': meta['name'], 'description': meta.get('description', ''),
        'category': meta.get('category', ''), 'enabled_by_default': bool(meta.get('enabled_by_default')),
        'app_version': version,
        'config': meta.get('config') or [], 'defaults': meta.get('defaults') or {},
    }
    docs_dir = output / '使用说明'
    docs_dir.mkdir(parents=True, exist_ok=True)
    guide = instructions(meta, version)
    (docs_dir / f'{safe_name}-{plugin_id}.md').write_text(guide, encoding='utf-8')
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.write(source, f'{plugin_id}.py')
        archive.writestr('plugin.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr('使用说明.md', guide)
    info = plugin_installer.inspect_local(target)
    return {'id': plugin_id, 'ok': bool(info.get('ok')), 'zip': str(target),
            'name': meta['name'], 'category': meta.get('category', ''),
            'size': target.stat().st_size,
            'problems': info.get('problems') or [], 'missing': info.get('missing') or []}


def main(argv=None):
    parser = argparse.ArgumentParser(description='打包插件市场')
    parser.add_argument('--output', default=str(DEFAULT_OUTPUT))
    parser.add_argument('--clean', action='store_true', help='先清空输出目录里的旧 zip')
    parser.add_argument('--version', default=APP_VERSION)
    parser.add_argument('--no-overwrite', action='store_true')
    args = parser.parse_args(argv)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if args.clean:
        for old in output.glob('*.zip'):
            old.unlink()
    results = []
    for plugin_id in market_plugins():
        result = build_one(plugin_id, output, args.version, not args.no_overwrite)
        results.append(result)
        state = 'OK ' if result.get('ok') else 'BAD'
        print(f"[{state}] {result.get('name') or result['id']:<20} "
              f"{result.get('zip') or result.get('error')}")
        if result.get('problems'):
            print('     问题：' + '；'.join(result['problems']))
        if result.get('missing'):
            print('     本机缺少依赖：' + '、'.join(result['missing']))
    catalog = [
        '# 插件市场', '',
        f'- 应用版本：{args.version}',
        f'- 基础插件（安装包内置，{len(BASIC_PLUGINS)} 个）：' + '、'.join(BASIC_PLUGINS),
        f'- 市场插件：{len(results)} 个', '',
        '| 插件 | 标识 | 分类 | 文件 | 状态 |', '|---|---|---|---|---|',
    ]
    for item in results:
        catalog.append(f"| {item.get('name') or item['id']} | `{item['id']}` | "
                       f"{item.get('category') or ''} | "
                       f"{Path(item.get('zip', '')).name or item.get('error', '')} | "
                       f"{'通过预检' if item.get('ok') else '未通过'} |")
    (output / 'README.md').write_text('\n'.join(catalog) + '\n', encoding='utf-8')
    ok = sum(1 for item in results if item.get('ok'))
    print(f'\n完成：{ok}/{len(results)} 个通过预检，输出目录 {output}')
    return 0 if ok == len(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
