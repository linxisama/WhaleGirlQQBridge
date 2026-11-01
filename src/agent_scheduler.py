"""Agent 定时任务：连接检查、日报，结果写回控制台。

只在本机运行；任务定义存 `agent_schedules.json`，不执行任意代码。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

ACTIONS = {
    'connection_check': '检查 QQ 连接',
    'daily_digest': '生成近 24 小时日报',
    'daily_diary': '写今天的日记',
    'memory_maintain': '维护记忆（遗忘与整理）',
    'memory_daily': '记忆每日维护（体检+报告）',
    'learn_chat': '从聊天记忆学习',
    'learn_web_queue': '处理一条网络学习任务',
    'bili_daily': 'B站账号每日复盘',
    'dream_consolidate': '夜间固化记忆',
    'self_portrait': '重写自画像',
}


def action_choices() -> list[tuple[str, str]]:
    """(中文标签, action键) 列表，顺序与 ACTIONS 一致；GUI 下拉的唯一真源。"""
    return [(label, key) for key, label in ACTIONS.items()]


def _dream_line(info):
    """agent_dream.consolidate 的结果 → 一句中文；未知 reason 也落到失败文案。"""
    data = info if isinstance(info, dict) else {}
    reason = str(data.get('reason') or '')
    if reason == 'disabled':
        return '夜间固化未启用（设置页开关关着）'
    if reason == 'no_items':
        return '近几天没有可固化的素材'
    if reason == 'rate_skipped':
        return '这次概率门没放行（今晚不固化）'
    if reason == 'ok':
        return f"夜间固化：写 {int(data.get('written') or 0)} 条，聚类 {int(data.get('clusters') or 0)} 簇"
    return f"夜间固化失败：{data.get('detail') or reason or '未知原因'}"


def _portrait_line(info):
    """agent_portrait.refresh 的结果 → 一句中文。"""
    data = info if isinstance(info, dict) else {}
    reason = str(data.get('reason') or '')
    if data.get('ok') or reason == 'ok':
        tail = '旧版已快照' if data.get('snapshot') else '首次生成'
        return f"自画像已重写：{int(data.get('chars') or 0)} 字（{tail}）"
    if reason == 'disabled':
        return '自画像开关关着，未重写'
    if reason == 'too_short':
        return '模型回的正文太短（<80 字），未覆盖'
    if reason == 'generate_failed':
        return '模型生成失败，自画像未重写'
    if reason == 'generate_async_unsupported':
        return '模型返回异步结果，暂不支持，自画像未重写'
    return f"自画像重写失败：{data.get('detail') or reason or '未知原因'}"


class AgentScheduler:
    def __init__(self, bot, path=None):
        self.bot = bot
        base = Path(getattr(bot.config, 'config_path', 'config.yaml')).parent
        self.path = Path(path) if path else base / 'agent_schedules.json'
        self._lock = asyncio.Lock()
        self._task = None
        self._state = {'version': 1, 'tasks': []}
        self.load()

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(data, dict) and isinstance(data.get('tasks'), list):
                self._state = {'version': 1, 'tasks': data['tasks']}
        except (OSError, ValueError, RecursionError):
            self._state = {'version': 1, 'tasks': []}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self._state, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(temp, self.path)

    def list(self):
        return [dict(item) for item in self._state['tasks']]

    def _validate(self, action, every_minutes, name):
        action = str(action or '').strip()
        if action not in ACTIONS:
            raise ValueError('未知定时任务类型')
        try:
            minutes = int(every_minutes)
        except (TypeError, ValueError):
            raise ValueError('间隔必须是整数分钟') from None
        if not 5 <= minutes <= 1440:
            raise ValueError('间隔需在 5～1440 分钟之间')
        return str(name or ACTIONS[action])[:60], action, minutes

    def create(self, name, action, every_minutes, enabled=True):
        name, action, minutes = self._validate(action, every_minutes, name)
        task = {'id': uuid.uuid4().hex[:12], 'name': name, 'action': action,
                'every_minutes': minutes, 'enabled': bool(enabled),
                'created': time.time(), 'last_run': 0, 'last_result': ''}
        self._state['tasks'].append(task)
        self.save()
        return task

    def set_enabled(self, identifier, enabled):
        if type(enabled) is not bool:
            raise ValueError('启用状态必须是布尔值')
        task = next((item for item in self._state['tasks'] if item.get('id') == identifier), None)
        if task is None:
            raise ValueError('定时任务不存在')
        task['enabled'] = enabled
        self.save()
        return task

    def delete(self, identifier):
        before = len(self._state['tasks'])
        self._state['tasks'] = [item for item in self._state['tasks'] if item.get('id') != identifier]
        self.save()
        return len(self._state['tasks']) != before

    def _data_dir(self):
        """成长类任务的数据目录：多账号用 data_root，否则回落 <root>/data。"""
        root = getattr(self.bot, 'data_root', None)
        if root:
            return Path(root)
        return Path(getattr(self.bot, 'root', '.') or '.') / 'data'

    def _connection_text(self):
        state = self.bot.status()
        mode = str(state.get('mode') or '')
        onebot = state.get('onebot') or {}
        bridge = state.get('qqbridge') or {}
        official = state.get('official') or {}
        if mode == 'onebot':
            connected = bool(onebot.get('connected') or onebot.get('online'))
            detail = f"普通 QQ（OneBot）：{'在线' if connected else '未连接'}"
        elif mode == 'qqbridge':
            connected = bool(bridge.get('connected') or bridge.get('online'))
            detail = f"普通 QQ（bridge）：{'在线' if connected else '未连接'}"
        else:
            connected = bool(official.get('authenticated') and official.get('ws_connected'))
            detail = f"官方 QQ：{'已连接' if connected else '未连接或鉴权未通过'}"
        return ('连接正常：' if connected else '连接异常：') + detail + \
               f"\n机器人：{'运行中' if state.get('running') else '已停止'}" \
               f"；回复：{'已暂停' if state.get('reply_paused') else '正常'}"

    def _digest_text(self):
        import datetime
        cutoff = time.time() - 24 * 3600
        counts = {}
        tokens = 0
        try:
            rows = self.bot.db.execute(
                "SELECT state,count(*) FROM inbox WHERE created>? GROUP BY state", (cutoff,)).fetchall()
            counts = {str(row[0]): int(row[1]) for row in rows}
            row = self.bot.db.execute(
                "SELECT COALESCE(sum(json_extract(payload,'$._usage.total_tokens')),0) "
                "FROM inbox WHERE created>?", (cutoff,)).fetchone()
            tokens = int((row or [0])[0] or 0)
        except Exception:
            pass
        total = sum(counts.values())
        failed = counts.get('failed', 0) + counts.get('uncertain', 0)
        text = (f"近 24 小时：收到 {total} 条，失败/待核实 {failed} 条，"
                f"token 约 {tokens}；模型 {getattr(self.bot.ai, 'model', '')}；"
                f"当前人设 {self.bot.style.active_name}。")
        try:
            folder = Path(self.bot.root) / 'data' / 'agent_reports'
            folder.mkdir(parents=True, exist_ok=True)
            name = 'daily-' + datetime.datetime.now().strftime('%Y%m%d') + '.txt'
            (folder / name).write_text(text + '\n', encoding='utf-8')
        except OSError:
            pass
        return text

    async def _diary_text(self):
        """让模型按当天聊天和记忆写日记；不生成统计模板，也不替它编。"""
        from . import agent_diary
        day = agent_diary.today()
        try:
            if agent_diary.has_day(self.bot, day):
                return f'{day} 的日记已经写过了（{agent_diary.diary_file(self.bot, day)}）'
        except Exception:
            pass
        maker = getattr(self.bot, '_make_ai', None)
        try:
            engine = maker() if callable(maker) else None
        except Exception:
            engine = None
        if engine is None:
            return '没有可用的模型配置，今天的日记没有写（不会用统计模板冒充日记）。'
        try:
            text = await agent_diary.compose(self.bot, engine, day)
            info = agent_diary.save(self.bot, day, text)
            return f'已完成 {day} 的日记：{info["chars"]} 字，保存到 {info["path"]}'
        except Exception as exc:
            return f'今天的日记没有写：{type(exc).__name__}: {exc}'
        finally:
            try:
                closer = getattr(engine, 'close', None)
                if callable(closer):
                    await closer()
            except Exception:
                pass

    def _ensure_daily_diary(self):
        """只有真的配了模型、也有日记目录时才自动排一条，避免空跑。"""
        if any(item.get('action') == 'daily_diary' for item in self._state['tasks']):
            return None
        bot = self.bot
        if not getattr(bot, 'diary_dir', None):
            return None
        ai = getattr(bot, 'ai', None)
        if ai is None or not str(getattr(ai, 'api_url', '') or '').strip():
            return None
        if not (getattr(ai, 'api_key_set', False) or getattr(ai, 'api_key', '')):
            return None
        try:
            return self.create('每天写日记', 'daily_diary', 1440, True)
        except Exception:
            return None

    async def _learn_web_once(self):
        from .learning_queue import LearningQueue
        from .public_web import read_public
        import re as _re
        queue = LearningQueue(self.bot.db)
        item = queue.next()
        if not item:
            return '网络学习队列为空。'
        try:
            html = await read_public(item['target'], timeout=15)
            text = _re.sub(r'<script[\s\S]*?</script>|<style[\s\S]*?</style>', ' ', html, flags=_re.I)
            text = _re.sub(r'<[^>]+>', ' ', text)
            text = _re.sub(r'\s+', ' ', text).strip()[:1200]
            if not text:
                raise ValueError('网页没有可读正文')
            from .agent_learning import AgentLearning
            from pathlib import Path
            base = Path(getattr(self.bot.config, 'config_path', 'config.yaml')).parent
            learning = AgentLearning(base / 'agent_learning.json')
            learning.learn_from_source('web', text[:300], detail='学习队列公开网页')
            try:
                from .unified_memory import UnifiedMemory
                memory = UnifiedMemory(self.bot.db, getattr(self.bot, 'diary_path', None))
                memory.remember(
                    f"网页学习：{item['target']}\n{text[:500]}", source='web',
                    layer='lesson', importance=55, confidence=55,
                    project='网络学习', tags=['web', 'learning'], evidence=item['target'])
            except Exception:
                pass
            queue.finish(item['id'], text[:300])
            return f"网络学习完成：{item['target']}，已写入学习库和分层记忆。"
        except Exception as exc:
            queue.fail(item['id'], str(exc))
            return f"网络学习失败：{type(exc).__name__}（任务已标记失败，可重试）。"

    def _learn_chat_text(self):
        from pathlib import Path
        from .agent_learning import AgentLearning
        from .unified_memory import UnifiedMemory
        base = Path(getattr(self.bot.config, 'config_path', 'config.yaml')).parent
        learning = AgentLearning(base / 'agent_learning.json')
        memory = UnifiedMemory(self.bot.db, getattr(self.bot, 'diary_path', None))
        memory._sync_legacy()
        try:
            rows = self.bot.db.execute(
                "SELECT count(*) FROM chat_memories WHERE status='confirmed'").fetchone()
            count = int((rows or [0])[0] or 0)
        except Exception:
            count = 0
        lesson = learning.learn_from_source(
            'chat', f'已从聊天记忆整理 {count} 条确认事实',
            detail='只读取 chat_memories，不保存完整聊天原文')
        try:
            memory.remember(
                f'聊天学习：已把 {count} 条确认事实同步到分层记忆',
                source='chat', layer='topic', importance=40, confidence=70,
                project='聊天', tags=['chat', 'learning'], evidence='chat_memories confirmed')
        except Exception:
            pass
        return f"聊天学习：确认档案 {count} 条，已写入学习库和分层记忆（{lesson.get('id', '')}）。"

    async def run_now(self, identifier):
        task = next((item for item in self._state['tasks'] if item.get('id') == identifier), None)
        if task is None:
            raise ValueError('定时任务不存在')
        if task['action'] == 'connection_check':
            result = self._connection_text()
        elif task['action'] == 'daily_diary':
            result = await self._diary_text()
        elif task['action'] == 'learn_chat':
            result = self._learn_chat_text()
        elif task['action'] == 'learn_web_queue':
            result = await self._learn_web_once()
        elif task['action'] == 'bili_daily':
            plugin = self.bot.plugins._plugins.get('bilibili_account')
            if not plugin or not plugin.enabled:
                result = 'B站看板插件未安装或未启用。'
            else:
                data = await plugin._fetch()
                result = plugin._report(data)
        elif task['action'] == 'memory_maintain':
            from . import memory_guard
            from .unified_memory import UnifiedMemory
            diary = getattr(self.bot, 'diary_path', None)
            memory = UnifiedMemory(self.bot.db, diary)
            folder = (Path(diary).parent / '记忆' / '备份') if diary else (self._data_dir() / '记忆' / '备份')
            try:
                backup = memory_guard.backup(
                    memory, folder,
                    db_path=getattr(self.bot, 'db_path', '') or '')
            except Exception as exc:
                backup = {'skipped': type(exc).__name__}
            stats = memory.maintain()
            tail = ('；备份 ' + backup['name']) if backup.get('name') else (
                '；未备份（' + str(backup.get('skipped') or '未知') + '）')
            result = (f"记忆维护：清理过期 {stats['expired']} 条、长期未用 {stats['stale']} 条；"
                      f"剩余 {stats['remaining']} 条{tail}。")
        elif task['action'] == 'memory_daily':
            from . import memory_guard
            from .unified_memory import UnifiedMemory
            try:
                diary = getattr(self.bot, 'diary_path', None)
                folder = (Path(diary).parent / '记忆') if diary else (self._data_dir() / '记忆')
                info = memory_guard.daily(
                    UnifiedMemory(self.bot.db, diary), out_dir=folder,
                    db_path=getattr(self.bot, 'db_path', '') or '')
                guard = info.get('guard') or {}
                result = ('记忆每日维护：检查 {0} 条，截断 {1} 条，打码 {2} 条，'
                          '归档重复 {3} 条，降权归档 {4} 条，关键词偏少 {5} 条；报告：{6}').format(
                    int(guard.get('checked') or 0), int(guard.get('truncated') or 0),
                    int(guard.get('masked') or 0), int(guard.get('archived_dup') or 0),
                    int(guard.get('decayed') or 0), len(guard.get('weak_keywords') or []),
                    info.get('report') or '未生成')
            except Exception as exc:
                result = f'记忆每日维护失败：{type(exc).__name__}: {exc}'
        elif task['action'] == 'dream_consolidate':
            from . import agent_dream
            from .unified_memory import UnifiedMemory
            try:
                memory = UnifiedMemory(self.bot.db, getattr(self.bot, 'diary_path', None))
                # 不传 force：设置页开关与 rate 概率门都要真的生效。
                info = agent_dream.consolidate(memory, self._data_dir(),
                                               config=getattr(self.bot, 'config', None))
                result = _dream_line(info)
            except Exception as exc:
                result = _dream_line({'reason': 'error',
                                      'detail': f'{type(exc).__name__}: {exc}'})
        elif task['action'] == 'self_portrait':
            from . import agent_portrait
            from .unified_memory import UnifiedMemory
            try:
                maker = getattr(self.bot, '_make_ai', None)
                engine = maker() if callable(maker) else None
            except Exception:
                engine = None
            if engine is None:
                result = '没有可用的模型配置，自画像未重写'
            else:
                try:
                    memory = UnifiedMemory(self.bot.db, getattr(self.bot, 'diary_path', None))
                    data_dir = self._data_dir()
                    prompt = agent_portrait.build_prompt(memory, data_dir)
                    text = await asyncio.wait_for(engine.chat(
                        [{'role': 'user', 'content': prompt}],
                        system_prompt='你在重写自己的「我是谁」，用第一人称，只输出正文。',
                        temperature=0.6, max_tokens=600), timeout=90)
                    # 用 refresh 复用它的清洗、too_short 与落盘逻辑。
                    result = _portrait_line(agent_portrait.refresh(
                        memory, data_dir, generate=lambda _prompt: text,
                        config=getattr(self.bot, 'config', None)))
                except Exception as exc:
                    result = f'自画像重写失败：{type(exc).__name__}: {exc}'
                finally:
                    try:
                        closer = getattr(engine, 'close', None)
                        if callable(closer):
                            await closer()
                    except Exception:
                        pass
        else:
            result = self._digest_text()
        task['last_run'] = time.time()
        task['last_result'] = result[:2000]
        self.save()
        try:
            self.bot.note(f"定时任务「{task['name']}」：{result.splitlines()[0]}")
        except Exception:
            pass
        return task

    async def run_due(self, now=None):
        now = time.time() if now is None else float(now)
        results = []
        for task in list(self._state['tasks']):
            if not task.get('enabled'):
                continue
            last = float(task.get('last_run') or 0)
            if now - last >= int(task.get('every_minutes') or 60) * 60:
                async with self._lock:
                    results.append(await self.run_now(task['id']))
        return results

    async def _loop(self):
        while True:
            try:
                await self.run_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(30)

    async def start(self):
        self._ensure_daily_diary()
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
