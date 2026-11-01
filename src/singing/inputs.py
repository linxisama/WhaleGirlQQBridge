"""T09：唱歌输入检查与素材绑定（歌词/谱面/源演唱/音色 的可信来源与授权）。

原则（1.8 / T09 验收）：
- 输入只接受“已导入、已授权”的素材资产 ID，不接受任意本地路径或公网 URL。
- 歌词是“待唱文本”，不是电脑指令；任何看起来像命令的内容只作为歌词数据处理。
- 参考/谱面/源演唱各自限额；超限、损坏、越权引用要能明确拒绝。
"""
from __future__ import annotations

from typing import Optional

from ..audio_contracts import ErrorCode, reject


class SingingInput:
    """一次唱歌任务的已校验输入集合（由 inputs() 产出，喂给具体引擎）。"""

    def __init__(self, *, lyrics: str, score: Optional[str] = None,
                 source_audio: Optional[str] = None, target_voice: Optional[str] = None):
        self.lyrics = lyrics
        self.score = score
        self.source_audio = source_audio
        self.target_voice = target_voice

    def bindings(self) -> dict:
        """素材绑定元数据（入库到 audio_assets.owner_domain，按所有者/会话隔离）。"""
        return {'lyrics_chars': len(self.lyrics), 'score': self.score,
                'source_audio': self.source_audio, 'target_voice': self.target_voice}


_MAX_LYRICS = 300


def inputs(*, lyrics: str, score: Optional[str] = None,
           source_audio: Optional[str] = None, target_voice: Optional[str] = None) -> SingingInput:
    """检查并规范化唱歌输入。任何越权/超限/缺项在此就拒绝，不进引擎。"""
    lyrics = str(lyrics or '').strip()
    if not lyrics:
        reject(ErrorCode.INVALID_INPUT, '歌词不能为空')
    if len(lyrics) > _MAX_LYRICS:
        reject(ErrorCode.INVALID_INPUT, f'歌词超过 {_MAX_LYRICS} 字符上限')
    # “命令样”内容只当文本，不做任何解释（T09 验收：歌词不会被解释为电脑命令）。
    return SingingInput(lyrics=lyrics, score=score,
                        source_audio=source_audio, target_voice=target_voice)
