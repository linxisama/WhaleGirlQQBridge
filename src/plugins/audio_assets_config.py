"""T14：音色/路由配置 + 素材上传（原生配置窗 FieldSchema 自动生成上传入口）。

按项目约定（AGENTS.md / 1.8）：需要本地文件的插件字段必须声明 file_picker，
由原生配置窗自动生成按钮，不再只留手填路径。上传文件**复制到**运行数据目录
data/plugin_uploads/<插件>/<字段>/ 后使用，不依赖原文件一直在（1.8）。

字段用途对应（1.8）：
- file          单参考音频（单个参考 WAV）
- files         多素材（歌曲/伴奏等多文件）
- labeled_files 带音色/歌曲标签表
- directory     目录授权（只登记授权目录，不递归导入）

本插件**默认关闭全部新能力**（云出网/自动语音/电脑控制），只新增键，
不整体替换现有配置、人设、模型/API、登录状态（1.8 / T14 验收）。
"""
from __future__ import annotations

from . import PluginBase, PluginMeta, FieldSchema


class AudioAssetsConfig(PluginBase):
    META = PluginMeta(
        id='audio_assets_config', name='音频素材与路由', category='voice',
        enabled_by_default=False,
        description='配置 TTS 引擎路由、参考/歌曲素材（原生上传入口）与数据去向。'
                    '云出网、自动语音、电脑控制默认关闭；上传文件复制到运行数据目录后使用。',
        config_schema={
            'enabled': FieldSchema(type='bool', label='启用', group='路由'),
            'engine': FieldSchema(type='select', label='主引擎',
                                  values=['audio_cpp', 'gpt_sovits_api_v2', 'qwen_webui'],
                                  group='路由'),
            'routePolicy': FieldSchema(type='select', label='路由策略',
                                       values=['local_only', 'cloud_only', 'local_preferred'],
                                       group='路由'),
            'allowOutbound': FieldSchema(type='bool', label='允许云出网', group='路由',
                                        description='默认关；开前需配预算并人工允许该供应商出网。'),
            'cloudModel': FieldSchema(type='string', label='云模型 ID', group='路由'),
            'cloudVoice': FieldSchema(type='string', label='云音色 ID', group='路由'),
            # 素材：单文件 / 多文件 / 标签表 / 目录，分别用对应 file_picker。
            'refAudio': FieldSchema(type='string', label='角色参考音频', group='素材',
                                   file_picker='file', file_extensions=['.wav', '.flac', '.mp3'],
                                   file_max_mb=8),
            'voiceSamples': FieldSchema(type='string', label='音色参考音频（可多选）', group='素材',
                                       file_picker='files',
                                       file_extensions=['.wav', '.flac', '.mp3', '.m4a'],
                                       file_max_mb=8,
                                       description='每个文件导入为一个音色；复制到运行数据目录后使用。'),
            'songFiles': FieldSchema(type='string', label='歌曲/伴奏素材', group='素材',
                                    file_picker='files',
                                    file_extensions=['.wav', '.flac', '.mp3', '.m4a', '.mid'],
                                    file_max_mb=50),
            'voiceTable': FieldSchema(type='string', label='音色/歌曲标签表', group='素材',
                                     file_picker='labeled_files',
                                     file_extensions=['.csv', '.tsv', '.json'],
                                     file_max_mb=4),
            'assetDir': FieldSchema(type='string', label='授权素材目录', group='素材',
                                   file_picker='directory',
                                   description='只登记授权目录，不递归导入；素材复制到运行数据目录使用。'),
        },
        default_settings={
            'enabled': False,
            'engine': 'audio_cpp', 'routePolicy': 'local_only', 'allowOutbound': False,
            'cloudModel': '', 'cloudVoice': '',
            'refAudio': '', 'voiceSamples': '', 'songFiles': '', 'voiceTable': '', 'assetDir': ''})

    def validate_settings(self, settings: dict) -> None:
        """保存前校验：云出网开但没配云模型/预算 → 给可理解提示（T14 验收：取消配置不改设置）。

        初版保守：routePolicy=cloud_only / local_preferred 时，allowOutbound 必须为 True
        才允许使用云端；否则该字段视为 local_only（不静默出网，4.5）。
        """
        if settings.get('routePolicy') in ('cloud_only', 'local_preferred') and not settings.get('allowOutbound'):
            settings['routePolicy'] = 'local_only'
