"""early_finish.py - 提前收尾：已交付完毕的这一轮，不再为"锦上添花"多花一次模型调用。

移植自 JS 插件 ``early-finish``（QQ Agent，MIT；作者 小肥鱼，1.2.0），判定规则逐条照搬
``lib/policy.js``。**但落地方式必须说清楚**：

JS 宿主的形态是"一次运行分多个批次调 LLM，最后一批只为调一次 finish"，所以那个插件
直接砍掉最后一轮。本运行时**每条消息只调一次聊天模型**，没有批次/finish 循环，
因此这里把同一套规则接到我们**真正会多花一次调用**的两个可选动作上：

- "该不该主动接话"的**判定调用**（context_chime：每条消息一次判定请求）；
- "让 AI 从表情池里挑一个"的**选择调用**（msg_reaction 的 ai 模式）。

规则仍然是"宁可少省一次，也不打断她做事"：只有**话确实发出去了、这一批没有遗留动作**
才允许省；任何失败、预告（"我去看看"）、预告要交东西却没交（"给你发张图"却没图），
一律按正常流程走。不碰提示词、不改人设、不拦发言，所以对"她像不像人"零影响。

设计上默认**不启用**（不批量替用户打开插件）；设置里还有"试运行"，只记日志不真的省。
"""

from __future__ import annotations

import re
import time

from . import PluginBase, PluginMeta, FieldSchema

# 预告式结尾：说了"我去看看/等下告诉你"但这一批没真的做完 —— 保留循环
# 比 JS 原版多认了"等下/等会儿/稍等"这类写法（原版 `等一下?` 只认"等一…"，
# 实测"等下告诉你"会漏判）。方向是**更保守**：多认一种预告 = 多保留一轮循环，
# 宁可少省一次调用，也不打断她做事。
PREVIEW = re.compile(
    r'我(?:先|再|就|马上|立刻|待会|一会儿)?(?:去|来)?(?:看看|查查|查一下|找找|问问|听听|翻翻)'
    r'|等(?:一下|下|会儿|会|一)?(?:再)?(?:说|告诉|回)'
    r'|待会(?:儿)?(?:说|告诉|回)|回头(?:再)?(?:说|告诉|回)|稍等'
)
# 预告要交东西（图/表情/语音/画）但这一批没交 —— 保留循环
PROMISE_ASSET = re.compile(r'发(?:张|个|一)?(?:图|表情|照片|链接)|画(?:一|张)|给你(?:看|听)一?(?:下|张|段)|唱(?:一)?段')

# 只对"刚刚交付"的这一轮生效：隔太久说明是别的轮次的残留，不能拿它当依据
RECENT_SECONDS = 120


def decide_turn_end(*, delivered: bool, text: str = "", sent_asset: bool = False,
                    tool_failed: bool = False) -> tuple[bool, str]:
    """这一轮是否已交付完毕。判定顺序照搬 ``lib/policy.js``。

    返回 ``(should_end, reason)``：任何一条不满足都返回 False（宁可少省一次调用）。
    """
    if not delivered:
        return False, '本批没有发送动作'
    if tool_failed:
        return False, '本批有工具失败，保留循环'
    body = str(text or '')
    if PREVIEW.search(body):
        return False, '预告式结尾，保留循环'
    if PROMISE_ASSET.search(body) and not sent_asset:
        return False, '预告要交东西却没交，保留循环'
    return True, '本批已交付完毕（话已发出、无遗留动作），省掉多余的一轮'


def digest_intent(reasoning) -> str:
    """从模型的思考里抽一句"她打算怎么说"，当收尾说明用（照搬 JS 版，纯本地零成本）。

    本运行时目前不把 reasoning 交给插件，所以这里取不到就返回空串、退回普通说明；
    保留这段是为了跟 JS 版行为一致，将来运行时暴露思考时不用再改插件。
    """
    text = str(reasoning or '').replace('\r', '').strip()
    if not text:
        return ''

    def clean(value: str) -> str:
        value = re.sub(r'^[\s"\'\u201c\u201d\u2018\u2019\-\u2022\u00b7]+', '', str(value or ''))
        value = re.sub(r'[\s"\'\u201c\u201d\u2018\u2019]+$', '', value)
        return re.sub(r'\s+', ' ', value).strip()

    primary = ['回法：', '回法:', '回复：', '回复:', '方案：', '方案:', '要说：', '要说:']
    secondary = ['就这么', '打算', '于是我', '我发']
    pos, marker_len = -1, 0
    for group in (primary, secondary):
        for marker in group:
            found = text.rfind(marker)
            if found > pos:
                pos, marker_len = found, len(marker)
        if pos >= 0:
            break
    if pos >= 0:
        segment = clean(text[pos + marker_len:pos + marker_len + 200])
        if len(segment) >= 15:
            return segment[:120]
    lines = [line.strip() for line in text.split('\n') if len(line.strip()) >= 8]
    return clean(' '.join(lines[-2:]))[:120]


class EarlyFinish(PluginBase):
    META = PluginMeta(
        id='early_finish', name='提前收尾（省 token）', category='utility',
        enabled_by_default=False,
        description=('移植自 JS 插件 early-finish。已交付完毕、没有遗留动作的这一轮，'
                     '不再为可选的锦上添花动作多花一次模型调用：跳过“该不该接话”的判定调用'
                     '和“让 AI 选表情”的选择调用。有工具失败、结尾是“我去看看”这类预告、'
                     '或预告要交图却没交时，一律按正常流程走。不碰提示词、不改人设、不拦发言；'
                     '“试运行”只记日志不真的省。'),
        config_schema={
            'enabled': FieldSchema('bool', '启用提前收尾'),
            'dryRun': FieldSchema('bool', '试运行（只记录，不真的省调用）'),
            'skipJudge': FieldSchema('bool', '跳过“该不该接话”的判定调用'),
            'skipAiReaction': FieldSchema('bool', '跳过“让 AI 选表情”的选择调用'),
            'windowSec': FieldSchema('int', '只对最近多少秒内交付的这一轮生效', min=10, max=1800),
        },
        default_settings={'enabled': False, 'dryRun': False, 'skipJudge': True,
                          'skipAiReaction': True, 'windowSec': RECENT_SECONDS})

    def __init__(self, manager):
        super().__init__(manager)
        self._last: dict | None = None

    # ── 记录"刚刚交付的这一轮"────────────────────────────────────────────────
    def on_sent(self, message, reply, runtime):
        """把刚发出去的内容记下来：判定下一轮要不要花额外调用时要用它。"""
        try:
            meta = message.get('_reply_meta') or {}
            media = meta.get('media') if isinstance(meta, dict) else None
            self._last = {
                'room': str(message.get('target') or ''),
                'text': str(reply or ''),
                'asset': bool(media),
                'failed': bool(message.get('_error_feedback')),
                'at': time.time(),
            }
        except Exception:
            self._last = None

    # ── 对外的小接口：其它插件在花额外调用之前问一句 ──────────────────────────
    def settled(self, runtime, message=None) -> bool:
        last = self._last
        if not last or not self.get('enabled'):
            return False
        window = max(10, int(self.get('windowSec', RECENT_SECONDS) or RECENT_SECONDS))
        if time.time() - last['at'] > window:
            return False
        if message is not None and str(message.get('target') or '') != last['room']:
            return False
        should_end, reason = decide_turn_end(
            delivered=bool(last['text'] or last['asset']), text=last['text'],
            sent_asset=last['asset'], tool_failed=last['failed'])
        if not should_end:
            return False
        if self.get('dryRun', False):
            runtime.note(f'[提前收尾] 试运行：这一轮本来可以省掉额外调用（{reason}）')
            return False
        runtime.note(f'[提前收尾] {reason}')
        return True

    def allow_extra(self, runtime, message, kind: str) -> bool:
        """``kind`` 取 ``judge``（接话判定）或 ``reaction``（AI 选表情）。

        返回 True = 照常花这次调用；False = 这一轮已经交付完毕，跳过。
        """
        if kind == 'judge' and not self.get('skipJudge', True):
            return True
        if kind == 'reaction' and not self.get('skipAiReaction', True):
            return True
        return not self.settled(runtime, message)


def early_finish_plugin(manager):
    """给同仓库插件用的小工具：拿到 early_finish 实例（没装/没启用就返回 None）。"""
    plugin = getattr(manager, '_plugins', {}).get('early_finish')
    if plugin is None or not plugin.enabled:
        return None
    if not plugin.get('enabled'):
        return None
    return plugin
