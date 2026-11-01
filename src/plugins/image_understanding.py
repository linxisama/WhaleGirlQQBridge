"""Awaited image recognition. No local file access and no invented sticker memory."""
from urllib.parse import urlsplit

from . import PluginBase, PluginMeta, FieldSchema
from ..image_input import image_source
from ..sticker_library import tiles_for_vision


class ImageUnderstanding(PluginBase):
    META = PluginMeta(
        id="image_understanding", name="图片与表情包识别", category="utility",
        description=("把收到的图片或表情包送给视觉模型识别，再由聊天模型按现有人设回复。"
                     "每条图片消息增加一次识图调用；沿用现有 API 地址与密钥。"
                     "DeepSeek 官方接口自动使用 deepseek-flash，其他接口需支持图片输入。"
                     "仅识别收到的图片，不提供发送表情包能力。"),
        enabled_by_default=True,
        config_schema={
            "enabled": FieldSchema(type="bool", label="启用图片识别"),
            "visionModel": FieldSchema(type="string", label="识图模型（可留空）",
                description="留空：DeepSeek 官方接口用 deepseek-flash，其他接口跟随聊天模型；可填写服务商的视觉模型名。"),
            "maxImages": FieldSchema(type="int", label="每条消息最多识别图片数", min=1, max=3),
            "timeoutSec": FieldSchema(type="int", label="图片下载与识别总等待上限（秒）", min=1, max=120,
                description="包含下载、长图分段和视觉请求；超时后交给正常回复流程说明未读，避免一直等不到回复。"),
        },
        default_settings={"enabled": True, "visionModel": "", "maxImages": 3, "timeoutSec": 30},
    )

    async def _prefer_larger_source(self, store, item, image, runtime):
        """协议元数据比下载图大很多时，尝试用 get_image 再取一次，避免只拿到缩略图。"""
        try:
            declared = int(item.get('width') or 0)
            actual = int(image.get('width') or 0)
        except (TypeError, ValueError):
            return image
        if declared <= 0 or actual <= 0 or declared <= actual * 1.25:
            return image
        file_id = item.get('file') or item.get('file_uuid')
        if runtime.mode not in ('onebot', 'qqbridge') or not file_id:
            return image
        try:
            peer = runtime.bridge if runtime.mode == 'qqbridge' else runtime.onebot
            info = await peer.call('get_image', {'file': str(file_id)})
            source = image_source(info.get('url')) if isinstance(info, dict) else ''
            if not source:
                return image
            alternate = await store.resolve(dict(item, url=source, file=''), runtime)
            if int(alternate.get('width') or 0) > actual:
                runtime.note(f"图片源比协议元数据小（{actual}/{declared}px），已换用更大的原图候选")
                return alternate
        except Exception:
            return image
        return image

    async def describe(self, message, runtime):
        images = [item for item in message.get("media", [])
                  if isinstance(item, dict) and item.get("type") == "image"]
        if not images:
            return
        # Failure must erase any stale annotation before retrying this message.
        message.pop("vision_description", None)
        message.pop("vision_cache_hit", None)
        message["vision_status"] = "失败：未取得可读取的图片地址"
        if not self.enabled:
            message["vision_status"] = "未启用图片识别"
            return
        long_reader=self._manager._plugins.get('long_image_reader')
        first=images[0]
        w=first.get('width',0);h=first.get('height',0)
        if long_reader and long_reader.enabled and len(images)==1 and isinstance(w,(int,float)) and isinstance(h,(int,float)) and w>0 and h>2048 and h/w>=3:
            model=str(self.get('visionModel','')).strip() or ('deepseek-flash' if urlsplit(runtime.ai.api_url).hostname=='api.deepseek.com' else runtime.ai.model)
            if await long_reader.describe(message,runtime,model):return
        memory = self._manager._plugins.get('sticker_memory')
        chat_key = f"{runtime.mode}|{getattr(runtime,'account',lambda:'')()}|{message.get('kind','')}:{message.get('target','')}"
        if len(images)==1 and memory and memory.enabled and not getattr(runtime,'stickers',None):
            cached = memory.cached(images[0],chat_key)
            if cached:
                message['vision_description'] = cached
                message['vision_status'] = 'ok'
                message['vision_cache_hit'] = True
                return
        limit = int(self.get("maxImages", 3))
        sources = []
        indices = []
        cache_item=images[0]
        store=getattr(runtime,'stickers',None)
        for index, item in enumerate(images[:limit], 1):
            if len(sources)>=3:break
            # 基本识图不能依赖收藏和长图插件；模型服务未必能下载 QQ 临时链接。
            if store:
                try:
                    image=await store.resolve(item,runtime)
                    image=await self._prefer_larger_source(store,item,image,runtime)
                    if len(images)==1 and long_reader and long_reader.enabled:
                        parts=long_reader.tiles(image['data'])
                        if parts and await long_reader.describe_parts(message,runtime,parts,str(self.get('visionModel','')).strip() or ('deepseek-flash' if urlsplit(runtime.ai.api_url).hostname=='api.deepseek.com' else runtime.ai.model)):return
                    import base64
                    from ..sticker_library import frames_for_vision
                    normalized=dict(item,url='',file='base64://'+base64.b64encode(image['data']).decode(),
                                    width=image['width'],height=image['height'],size=len(image['data']))
                    if len(images)==1:
                        cache_item=normalized
                        if memory and memory.enabled:
                            cached=memory.cached(normalized,chat_key)
                            if cached:
                                message.update(vision_description=cached,vision_status='ok',vision_cache_hit=True)
                                store.record_description(item,cached)
                                return
                    admin=self._manager._plugins.get('sticker_admin')
                    want=int(admin.get('gifFrames',2)) if admin else 2
                    # 基础包没有 long_image_reader 时，核心仍要把长图切成可读块；
                    # 否则 900×9000 会被压成 102×1024，字完全看不清。
                    tall = (len(images)==1 and image.get('height',0) > 2400
                            and image.get('height',0) / max(1, image.get('width',0)) >= 2.5)
                    if tall and len(sources)==0:
                        frames=tiles_for_vision(image['data'],max_tiles=6)
                        message['_vision_tiles']=len(frames)
                    else:
                        frames=frames_for_vision(image['data'],min(want,3-len(sources)))
                    sources.extend(frames);indices.extend([index]*len(frames))
                    continue
                except Exception:
                    # 下载失败不能把同一临时链接再交给模型，导致二次等待或假装已保存。
                    message['vision_status']='失败：图片下载或本地保存失败，请检查链接是否过期、磁盘空间及协议端连接'
                    continue
            source = image_source(item.get("url")) or image_source(item.get("file"))
            if not source and runtime.mode in ('onebot','qqbridge') and item.get("file"):
                try:
                    peer=runtime.bridge if runtime.mode=='qqbridge' else runtime.onebot
                    info = await peer.call("get_image", {"file": str(item["file"])})
                    if isinstance(info, dict):
                        source = image_source(info.get("url"))
                except Exception:
                    pass  # Never report file names, temporary URL tokens, or credentials.
            if source:
                sources.append(source)
                indices.append(index)
        if not sources:
            return
        model = str(self.get("visionModel", "")).strip()
        if not model:
            model = ("deepseek-flash" if urlsplit(runtime.ai.api_url).hostname == "api.deepseek.com"
                     else runtime.ai.model)
        vision_error = ""
        try:
            description = await runtime.ai.describe_images(
                sources, model=model, timeout=max(1, min(120, float(self.get('timeoutSec', 30)))),
                **({'text_only':True} if message.get('_vision_ocr_first') else {}))
        except Exception as exc:
            description = ""
            vision_error = f"{type(exc).__name__}: {str(exc)[:120]}"
        if not description:
            # 以前这里只给一句"请检查视觉模型、链接、API 状态"，用户无从下手。
            # 现在把**真实原因**（引擎的 last_error 或异常文本）一并带出来。
            reason = vision_error or str(getattr(runtime.ai, 'last_error', '') or '').strip()
            message["vision_status"] = ("失败：视觉模型未返回内容"
                                        + (f"（{reason}）" if reason else
                                           "，请检查插件里的识图模型、接口支持和额度"))
            runtime.note("图片识别失败：" + (reason or "请检查视觉模型、图片链接是否过期及 API 状态")
                         + f"｜模型={model}")
            return
        recognized=len(set(indices))
        tile_count=int(message.get('_vision_tiles') or 0)
        if tile_count:
            frame_note=f'（长图切成{tile_count}块，只描述这些块内的内容）'
        else:
            frame_note=f'（动图共抽取{len(sources)}帧，只描述这些画面）' if len(sources)>recognized else ''
        message["vision_description"] = ("以下按顺序对应原消息第 " + "、".join(map(str, sorted(set(indices))))
                                         + " 张图片"+frame_note+"：\n" + str(description))[:min(12000,int(message.get('_vision_ocr_chars',3000)))]
        missing = len(images) - recognized
        message["vision_status"] = (f"部分缺失：只识别了 {recognized}/{len(images)} 张图片，"
                                    "其余未识别，不能推断其内容" if missing else "ok")
        if len(images)==1 and not missing and memory and memory.enabled:
            memory.record_description(cache_item,message['vision_description'],chat_key)
        if len(images)==1 and not missing and store:
            store.record_description(images[0],message['vision_description'])

