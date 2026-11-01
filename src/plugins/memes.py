"""将 nonebot-plugin-memes 的本机生成能力适配到宿主插件接口。"""
import asyncio
import base64
import json
import random
import re
import shlex
import time

from . import PluginBase, PluginMeta, FieldSchema
from ..meme_service import catalog, find_template, generate


def parse_command(text, direct=True):
    text = re.sub(r'^(?:@\S+\s+)+', '', str(text or '').strip()).replace('[图片]', '').strip()
    if text.startswith('/'):
        text = text[1:]
    parts = shlex.split(text)
    if not parts:
        return None
    name, args = parts[0], parts[1:]
    commands = {'表情列表': 'list', '表情详情': 'info', '表情预览': 'preview',
                '表情制作': 'make', '随机表情': 'random', '表情统计': 'stats'}
    if name in commands:
        return commands[name], args
    if direct and find_template(name):
        return 'make', [name] + args
    return None


class Memes(PluginBase):
    META = PluginMeta(id='memes', name='表情包制作（NoneBot 适配）', category='media',
        description='基于你提供的 nonebot-plugin-memes 0.8.1 和 meme-generator 0.2.3，支持300种模板、搜索/详情/预览、图片加字与GIF、本地制作和成功发送统计。群聊先@；图片在本机处理。首次使用有素材的模板会按需下载官方素材。',
        config_schema={
            'enabled': FieldSchema(type='bool', label='启用'),
            'directKeywords': FieldSchema(type='bool', label='允许直接用模板关键词制作'),
            'defaultTexts': FieldSchema(type='bool', label='未给文字时使用模板默认文字'),
            'downloadResources': FieldSchema(type='bool', label='首次使用时下载模板素材', description='仅从固定版本的官方仓库下载并校验，不上传用户图片。关闭后只使用已缓存素材。'),
            'disabledTemplates': FieldSchema(type='string', label='禁用模板（名称或key，逗号分隔）'),
            'cooldownSec': FieldSchema(type='int', label='同一会话同一人制作间隔（秒）', min=0, max=300),
            'maxImages': FieldSchema(type='int', label='每次最多输入图片数', min=1, max=5),
        }, default_settings={'enabled': True, 'directKeywords': True, 'defaultTexts': True,
                             'downloadResources': True, 'disabledTemplates': '', 'cooldownSec': 10, 'maxImages': 3})

    def __init__(self, manager):
        super().__init__(manager)
        self._last = {}

    async def on_prepare(self, message, runtime):
        try:
            command = parse_command(message.get('text'), self.get('directKeywords', True))
        except ValueError:
            # 普通聊天里的单引号不属于表情命令，不能劫持它。
            if str(message.get('text', '')).lstrip().startswith(('/表情制作', '/表情详情', '/表情预览')):
                message['_direct_reply'] = '引号未成对，请用中文双引号外的英文双引号包住一段文字。'
            return
        if not command:
            return
        message.pop('_reply_meta', None)
        message.pop('_meme_generated', None)
        message['_direct_reply'] = '表情命令未完成，本次没有发出图片。'
        action, args = command
        disabled = {v.strip() for v in re.split('[,，\n]', self.get('disabledTemplates', '')) if v.strip()}
        available = [v for v in catalog() if v['key'] not in disabled and not disabled.intersection(v['keywords'])]
        if action == 'list':
            page = int(args.pop()) if args and args[-1].isdigit() else 1
            query = ' '.join(args).lower()
            found = [v for v in available if query in ' '.join([v['key']] + v['keywords'] + v['tags']).lower()]
            pages = max(1, (len(found) + 19) // 20)
            page = max(1, min(page, pages))
            names = [f"{v['keywords'][0] if v['keywords'] else v['key']}（{v['key']}）" for v in found[(page-1)*20:page*20]]
            message['_direct_reply'] = f'表情模板 {len(found)} 个，第 {page}/{pages} 页：\n' + '\n'.join(names) + '\n用 /表情列表 2 翻页；/表情详情 名称 查看用法；/表情制作 名称 "文字" 并附图片。'
            return
        if action == 'stats':
            stats = self.load_state('usage.json', {})
            rows = sorted(stats.items(), key=lambda v: v[1], reverse=True)[:10]
            message['_direct_reply'] = '表情成功发送统计：\n' + ('\n'.join(f'{k}：{v}次' for k, v in rows) or '还没有成功发送的制作记录。')
            return
        if action == 'random':
            candidates = [v for v in available if v['min_images'] <= len(message.get('media', [])) <= v['max_images'] and (v['min_texts'] == 0 or v['default_texts'])]
            if not candidates:
                message['_direct_reply'] = '没有匹配当前图片数量的随机模板，请先附一张图片或选择具体模板。'
                return
            template = random.choice(candidates)
        else:
            template = find_template(args.pop(0)) if args else None
        if not template or template not in available:
            message['_direct_reply'] = '没有找到该模板或模板已禁用。用 /表情列表 搜索词 查找。'
            return
        key = template['key']
        if action == 'info':
            message['_direct_reply'] = (f"{ '、'.join(template['keywords']) }（{key}）\n图片：{template['min_images']}–{template['max_images']}张；文字：{template['min_texts']}–{template['max_texts']}段。\n默认文字：{' / '.join(template['default_texts']) or '无'}\n用法：/表情制作 {key} \"文字一\" \"文字二\"，需要图片时与命令一起附上。可用 --参数 '{{\"选项名\":值}}' 传模板选项。")
            return
        try:
            options = {}
            if '--参数' in args:
                index = args.index('--参数')
                options = json.loads(' '.join(args[index+1:]))
                args = args[:index]
                if not isinstance(options, dict) or len(options) > 20 or any(type(v) not in (str, bool, int, float) for v in options.values()):
                    raise ValueError('模板参数须为最多20项的 JSON 对象，值只能是文字、数字或布尔值')
            texts = args or (template['default_texts'] if self.get('defaultTexts', True) else [])
            media = [v for v in message.get('media', []) if isinstance(v, dict) and v.get('type') == 'image']
            preview = action == 'preview'
            if not preview:
                if not template['min_images'] <= len(media) <= min(template['max_images'], self.get('maxImages', 3)):
                    raise ValueError(f"此模板需要 {template['min_images']}–{template['max_images']} 张图片，本插件每次上限 {self.get('maxImages',3)} 张。请把图片和命令一起发送。")
                if not template['min_texts'] <= len(texts) <= template['max_texts'] or any(len(v) > 300 for v in texts):
                    raise ValueError(f"此模板需要 {template['min_texts']}–{template['max_texts']} 段文字，每段最多300字；含空格的文字请用英文双引号包住。")
            cooldown_key = (runtime.mode, str(message.get('kind')), str(message.get('target')), str(message.get('sender_id')))
            now = time.monotonic()
            if now - self._last.get(cooldown_key, -1e9) < self.get('cooldownSec', 10):
                raise ValueError('制作得有点快，请稍等几秒再试。')
            self._last = {k: v for k, v in self._last.items() if now-v < 600}
            self._last[cooldown_key] = now
            images = []
            if not preview:
                from ..image_input import image_source
                from ..sticker_library import fetch_image, decode_image
                for item in media:
                    store=getattr(runtime,'stickers',None)
                    if store:
                        images.append((await store.resolve(item,runtime))['data'])
                        continue
                    source = image_source(item.get('url')) or image_source(item.get('file'))
                    if not source and runtime.mode in ('onebot','qqbridge'):
                        peer=runtime.bridge if runtime.mode=='qqbridge' else runtime.onebot
                        info = await peer.call('get_image', {'file': str(item.get('file') or '')})
                        source = image_source(info.get('url')) if isinstance(info, dict) else ''
                    if not source:
                        raise ValueError('平台未提供可读取的图片；请把图片和命令一起发送')
                    raw = await fetch_image(source)
                    decode_image(raw)
                    images.append(raw)
            home = self.state_file('engine/config.toml').parent
            raw = await generate(home, key, images, texts, preview, options, download=self.get('downloadResources', True))
            from ..sticker_library import decode_image
            decode_image(raw)
            message['_reply_meta'] = {'media': [{'type': 'image', 'data': {'file': 'base64://' + base64.b64encode(raw).decode('ascii')}}]}
            message['_meme_generated'] = key
            message['_direct_reply'] = '做好啦：' + (template['keywords'][0] if template['keywords'] else key)
        except asyncio.TimeoutError:
            message['_direct_reply'] = '表情制作或素材下载超时，本次没有发出图片；可稍后重试。'
        except Exception as exc:
            runtime.note(f'表情包制作未完成：{type(exc).__name__}')
            message['_direct_reply'] = str(exc)[:350] if isinstance(exc, ValueError) else '表情素材或生成引擎暂不可用，本次没有发出图片。'

    def on_sent(self, message, reply, runtime):
        key = message.get('_meme_generated')
        if key:
            stats = self.load_state('usage.json', {})
            stats[key] = int(stats.get(key, 0)) + 1
            self.save_state('usage.json', stats)
