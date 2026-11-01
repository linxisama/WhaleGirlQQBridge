"""Local library administration is exposed by the native console and local API."""
from . import PluginBase,PluginMeta,FieldSchema

class StickerAdmin(PluginBase):
    META=PluginMeta(id='sticker_admin',name='表情包管理',category='media',
        description='点击“管理表情库”查看缩略图、搜索、上传、修改标签与含义、让AI识图及删除本地收藏。只管理机器人自己的本地库，不同步删除QQ收藏，也不改写核心程序。手动图库仍在“表情包与自定义图库”中配置。',
        enabled_by_default=True,
        config_schema={'enabled':FieldSchema('bool','启用本地图库管理'),
                       'gifFrames':FieldSchema('int','动图识别最多抽帧数',min=1,max=3)},
        default_settings={'enabled':True,'gifFrames':2})
