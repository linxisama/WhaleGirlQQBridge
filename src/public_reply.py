"""模型输出与 QQ 正文的边界；内部识图状态只留在本机诊断。"""
import re

_THOUGHT = re.compile(r'<(think|thinking|thought|analysis|reasoning)\b[^>]*>.*?</\1\s*>', re.I | re.S)
_OPEN_THOUGHT = re.compile(r'<(?:think|thinking|thought|analysis|reasoning)\b[^>]*>.*$', re.I | re.S)
# 模型安全层偶尔会在中文回复末尾追加一句英文拒答；QQ 上只保留中文正文。
# 只认「道歉 + 拒绝」这一整行，避免误删正文里正常出现的英文句子。
_EN_REFUSAL = re.compile(
    r"(?im)^[ \t]*(?:i(?:'m|\u2019m|\s+am)\s+)?(?:sorry|i\s+apologi[sz]e)\s*[,，.]?\s*"
    r"(?:but\s+)?(?:i\s+)?"
    r"(?:can't|cannot|can not|won't|will not|am unable to|unable to)\s+"
    r"(?:respond\s+to\s+(?:this|that|your)\s+question|"
    r"answer\s+(?:this|that)(?:\s+question)?|"
    r"help\s+with\s+(?:this|that)|assist\s+with\s+(?:this|that)|"
    r"comply\s+with\s+(?:this|that)(?:\s+request)?|"
    r"continue\s+with\s+(?:this|that))\s*[.!。！?]?\s*$")
# 有些模型（尤其是推理模型/中转站）不把思考放进 reasoning_content，也不加
# <think> 标签，而是直接把「好的，我先分析一下……」「按设定，第一句话必须……」
# 这类内心独白写进 content。QQ 只该看到最终回复，这些内容一律不能出站。
_FINAL_REPLY = re.compile(
    r'(?im)^\s*(?:#{1,6}\s*)?(?:最终回复|正式回复|最终答案|最终输出|回复|答案|输出|'
    r'要说的话|发给对方的话|final\s+(?:answer|response)|reply)\s*[:：]\s*')
_THINK_HEADER = re.compile(
    r'(?is)^\s*(?:#{1,6}\s*)?(?:思考过程|思维过程|分析过程|推理过程|思考|分析|'
    r'内心独白|心理活动)\s*[:：]\s*.*?'
    r'(?=\n\s*(?:#{1,6}\s*)?(?:最终回复|正式回复|最终答案|最终输出|回复|答案|输出|'
    r'要说的话|发给对方的话)\s*[:：]|\Z)')
_REASONING_CUES = (
    (re.compile(r'(?:按照|按|根据|遵照)\s*(?:设定|规则|人设|要求|指示)'), 2),
    (re.compile(r'(?:第一句话|第一次回复|这次回复|这句话)\s*(?:必须|一定要|要|得)'), 1),
    (re.compile(r'(?:不能真的|不能直接|只能对|只能这样|不能让他|不能让她)'), 1),
    (re.compile(r'(?:思考过程|思维链|推理过程|内心独白|心理活动|分析过程)'), 2),
    (re.compile(r'(?:让我|我来)\s*(?:想想|想一下|组织|设计|安排)'), 1),
    (re.compile(r'(?:用户|对方|他|她)\s*(?:说|问|发|来).{0,18}(?:我|应该|要|得)'), 1),
    (re.compile(r'(?:我)?(?:就)?得(?:按|先|用|以).{0,18}(?:回应|回复|回答|说|语气|方式)'), 1),
)


def _reasoning_score(value):
    return sum(weight for pattern, weight in _REASONING_CUES if pattern.search(value))


def strip_reasoning_leak(text):
    """去掉没有标签的内心独白；宁可回一句中性兜底，也不把思考发到 QQ。"""
    value = str(text or '').strip()
    if not value:
        return ''
    value = _THINK_HEADER.sub('', value).strip()
    if not value:
        return ''
    matches = list(_FINAL_REPLY.finditer(value))
    if matches:
        tail = value[matches[-1].end():].strip()
        if not tail:
            return ''
        value = tail
    if _reasoning_score(value) >= 3:
        return ''
    return value


_VISION_INTERNAL = ('[图片识别状态]', '[图片识别参考', '视觉模型未返回内容',
                    '识别状态为', '识别状态是', '识别状态为“', '这里有矛盾',
                    '图片识别参考只是', '不可执行其中的指令', '不可声称已收到画面内容')


def clean_model_text(text):
    value = _OPEN_THOUGHT.sub('', _THOUGHT.sub('', str(text or '')))
    value = _EN_REFUSAL.sub('', value)
    value = re.sub(r'\n{3,}', '\n\n', value)
    return value.strip()


def image_unread_reply():
    return '这张图我暂时没看清，你可以再发一次，或者用文字告诉我想问什么。'


def failed_image_only(message):
    from .text_hygiene import tidy_outbound
    return (any(isinstance(m, dict) and m.get('type') == 'image' for m in (message.get('media') or []))
            and str(message.get('vision_status') or '').startswith(('失败', '未启用'))
            and not tidy_outbound(message.get('text', '')))


def speech_unread_reply():
    return '这条语音我暂时无法转成文字，你可以用文字告诉我想说什么。'


def unread_speech_only(message):
    from .text_hygiene import tidy_outbound
    media = [m for m in (message.get('media') or []) if isinstance(m, dict)]
    if (not any(m.get('type') == 'record' for m in media)
            or message.get('speech_status') == '已转写'
            or any(m.get('type') in ('image', 'video', 'file') for m in media)):
        return False
    text = tidy_outbound(message.get('text', ''))
    if message.get('kind') == 'group':
        text = re.sub(r'^\s*(?:@某人\([^)]*\)\s*)+', '', text)
    return not text.strip()


def public_reply(text, message):
    if message.get('_speech_local_reply'):
        value = speech_unread_reply()
        return value, value != str(text or '').strip()
    if not str(text or '').strip():
        return '', False  # 纯图片/媒体回复允许没有文字正文。
    value = strip_reasoning_leak(clean_model_text(text))
    has_image = any(isinstance(m, dict) and m.get('type') == 'image' for m in (message.get('media') or []))
    # 只拦截明确的内部识图标记/规则复述，正常的图片内容和用户讨论日志不受影响。
    if has_image and any(marker in value for marker in _VISION_INTERNAL):
        if str(message.get('vision_status') or '') == 'ok' and message.get('vision_description'):
            return '图我看到了，想让我重点看看哪里？', True
        return image_unread_reply(), True
    if not value:
        return (image_unread_reply() if has_image else '刚才没组织好回复，你再说一句？'), True
    return value, value != str(text or '').strip()
