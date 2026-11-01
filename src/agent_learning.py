"""控制台「学习 Agent」的本地学习库。

设计目标（用户要求：控制台助手换成智能学习 agent，后续功能以导入为主）：

1. **可学习**：每次成功应用的操作方案都记下来（意图 → 步骤），失败也记；
   下次遇到相似说法先检索历史经验，再交给模型，模型不必从零猜。
2. **可纠错**：用户点「这次不对」会把该经验标成失败，置信度下降；反复失败的不再推荐。
3. **可导入**：技能包（JSON）提供触发词与操作说明，导入后无需改代码即可扩展能力。
4. **纯本地**：只写一个 JSON 文件，不上传、不执行技能包里的代码。

数据结构::

    {
      "version": 1,
      "preferences": {"称呼": {"value": "老鲸", "note": "", "updated": 169...}},
      "lessons": [
        {"id": "…", "intent": "把回复调快一点", "trigrams": [...],
         "actions": [{"type": "settings", "values": {...}}],
         "summary": "回复等待上限 20 → 15",
         "ok": 3, "fail": 0, "note": "", "created": 169..., "updated": 169...}
      ],
      "skills": [
        {"id": "…", "name": "…", "version": 1, "instructions": "…",
         "triggers": ["…"], "file": "…", "imported": 169...}
      ]
    }
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from pathlib import Path

MAX_LESSONS = 200
MAX_SKILLS = 100
MAX_PREFERENCES = 100
MAX_BYTES = 512 * 1024
SKILL_FORMAT = 'whale-agent-skill'
SKILL_PERMISSIONS = frozenset({'read_memory', 'modify_settings', 'toggle_plugins',
                               'switch_persona', 'send_message', 'read_history',
                               'network', 'write_files'})

_SPACE = re.compile(r'\s+')
_PUNCT = re.compile(r'[，。！？、；：,.!?;:~～…“”"\'（）()\[\]【】<>《》]+')


def normalize(text: str) -> str:
    value = _PUNCT.sub(' ', str(text or '').lower())
    return _SPACE.sub(' ', value).strip()[:500]


def bigrams(text: str) -> list[str]:
    value = normalize(text).replace(' ', '')
    if len(value) < 2:
        return [value] if value else []
    return [value[i:i + 2] for i in range(len(value) - 1)]


def similarity(left: str, right: str) -> float:
    """字符二元组重合度，适合中文短句；完全一致直接给 1。"""
    a, b = normalize(left), normalize(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ga, gb = set(bigrams(a)), set(bigrams(b))
    if not ga or not gb:
        return 0.0
    shared = len(ga & gb)
    return shared / math.sqrt(len(ga) * len(gb))


def clean_actions(actions) -> list[dict]:
    """只接受结构化操作列表；具体白名单仍由 ConsoleAgent 校验。"""
    if not isinstance(actions, list) or not 1 <= len(actions) <= 3:
        raise ValueError('学习记录里的步骤数量不正确')
    result = []
    for action in actions:
        if not isinstance(action, dict) or not isinstance(action.get('type'), str):
            raise ValueError('学习记录里的步骤格式不正确')
        result.append(json.loads(json.dumps(action, ensure_ascii=False)))
    return result


class AgentLearning:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._state = {'version': 1, 'preferences': {}, 'lessons': [], 'skills': []}
        self.load()

    # ---------- 持久化 ----------

    def load(self):
        with self._lock:
            try:
                if self.path.is_file() and self.path.stat().st_size <= MAX_BYTES:
                    data = json.loads(self.path.read_text(encoding='utf-8'))
                    if isinstance(data, dict):
                        self._state = {
                            'version': 1,
                            'preferences': data.get('preferences') if isinstance(data.get('preferences'), dict) else {},
                            'lessons': data.get('lessons') if isinstance(data.get('lessons'), list) else [],
                            'skills': data.get('skills') if isinstance(data.get('skills'), list) else [],
                        }
            except (OSError, ValueError, RecursionError):
                # 学习库坏了不能拖垮控制台：保留空库，下次写入覆盖。
                self._state = {'version': 1, 'preferences': {}, 'lessons': [], 'skills': []}
            self._trim()

    def save(self):
        with self._lock:
            self._trim()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(self._state, ensure_ascii=False, indent=1), encoding='utf-8')
            os.replace(temp, self.path)

    def _trim(self):
        self._state['lessons'] = self._state['lessons'][-MAX_LESSONS:]
        self._state['skills'] = self._state['skills'][-MAX_SKILLS:]
        prefs = self._state['preferences']
        if len(prefs) > MAX_PREFERENCES:
            keep = sorted(prefs.items(), key=lambda kv: float((kv[1] or {}).get('updated') or 0))[-MAX_PREFERENCES:]
            self._state['preferences'] = dict(keep)

    # ---------- 偏好 ----------

    def set_preference(self, key, value, note=''):
        key = str(key or '').strip()[:40]
        value = str(value or '').strip()[:500]
        if not key or not value:
            raise ValueError('偏好需要有名称和内容')
        with self._lock:
            self._state['preferences'][key] = {'value': value, 'note': str(note or '')[:200],
                                               'updated': time.time()}
            self.save()

    def get_preference(self, key):
        with self._lock:
            return dict(self._state['preferences'].get(str(key or '')) or {})

    def forget_preference(self, key):
        with self._lock:
            existed = self._state['preferences'].pop(str(key or ''), None)
            self.save()
        return bool(existed)

    # ---------- 经验 ----------

    def _lessons(self):
        return self._state['lessons']

    def record(self, intent, actions, summary='', ok=True, note=''):
        intent = str(intent or '').strip()[:200]
        actions = clean_actions(actions)
        if not intent:
            raise ValueError('经验缺少意图描述')
        fingerprint = self._fingerprint(actions)
        now = time.time()
        with self._lock:
            for lesson in self._lessons():
                if lesson.get('actions') and self._fingerprint(lesson['actions']) == fingerprint \
                        and similarity(lesson.get('intent', ''), intent) >= .8:
                    lesson['ok'] = int(lesson.get('ok') or 0) + (1 if ok else 0)
                    lesson['fail'] = int(lesson.get('fail') or 0) + (0 if ok else 1)
                    lesson['summary'] = str(summary or lesson.get('summary') or '')[:300]
                    lesson['note'] = str(note or lesson.get('note') or '')[:300]
                    lesson['updated'] = now
                    lesson['intent'] = intent
                    self.save()
                    return lesson
            lesson = {'id': uuid_like(intent + fingerprint), 'intent': intent,
                      'bigrams': bigrams(intent)[:60], 'actions': actions,
                      'summary': str(summary or '')[:300], 'ok': 1 if ok else 0,
                      'fail': 0 if ok else 1, 'note': str(note or '')[:300],
                      'created': now, 'updated': now}
            self._lessons().append(lesson)
            self.save()
            return lesson

    def learn_from_source(self, source, summary, detail=''):
        """自主学习入口：source=chat/web；只写摘要，不保存原文/URL/密钥。"""
        if source not in ('chat', 'web'):
            raise ValueError('学习来源只支持 chat/web')
        summary = str(summary or '').strip()[:300]
        if not summary:
            raise ValueError('学习摘要不能为空')
        now = time.time()
        with self._lock:
            lesson = {'id': uuid_like(source + summary + str(now)), 'intent': summary,
                      'bigrams': bigrams(summary)[:60], 'actions': [], 'summary': summary,
                      'ok': 0, 'fail': 0, 'note': str(detail or '')[:300],
                      'source': source, 'created': now, 'updated': now}
            self._lessons().append(lesson)
            self.save()
            return lesson

    @staticmethod
    def _fingerprint(actions):
        raw = json.dumps(actions, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]

    def feedback(self, lesson_id=None, actions=None, intent='', score=1, note=''):
        """用户反馈：-1 = 这次不对（压低置信度），1 = 这次对了（加一分）。"""
        if score not in (-1, 1):
            raise ValueError('反馈分数只能是 1 或 -1')
        with self._lock:
            target = None
            if lesson_id:
                target = next((l for l in self._lessons() if l.get('id') == lesson_id), None)
            if target is None and actions:
                fingerprint = self._fingerprint(clean_actions(actions))
                target = next((l for l in self._lessons()
                               if self._fingerprint(l.get('actions') or []) == fingerprint), None)
            if target is None:
                if score < 0:
                    return None
                return self.record(intent or '用户确认的做法', actions or [], note=note, ok=True)
            target['ok'] = int(target.get('ok') or 0) + (1 if score > 0 else 0)
            target['fail'] = int(target.get('fail') or 0) + (1 if score < 0 else 0)
            target['note'] = str(note or target.get('note') or '')[:300]
            target['updated'] = time.time()
            self.save()
            return target

    def forget_lesson(self, identifier):
        with self._lock:
            before = len(self._lessons())
            self._state['lessons'] = [l for l in self._lessons() if l.get('id') != identifier]
            self.save()
        return len(self._state['lessons']) != before

    def match_lessons(self, text, limit=4):
        """按相似度取历史经验；失败多的降权，但保留可解释性。"""
        scored = []
        for lesson in self._lessons():
            score = similarity(text, lesson.get('intent', ''))
            if score <= 0:
                continue
            ok = int(lesson.get('ok') or 0)
            fail = int(lesson.get('fail') or 0)
            confidence = (ok + 1) / (ok + fail + 2)
            age_days = max(0.0, (time.time() - float(lesson.get('updated') or 0)) / 86400)
            freshness = 1.0 / (1.0 + age_days / 60.0)
            ranked = score * (0.4 + 0.6 * confidence) * (0.75 + 0.25 * freshness)
            scored.append((ranked, score, lesson))
        scored.sort(key=lambda item: item[0], reverse=True)
        result = []
        for ranked, score, lesson in scored[:max(0, int(limit))]:
            if score < .28:
                continue
            result.append({'id': lesson.get('id'), 'intent': lesson.get('intent'),
                           'summary': lesson.get('summary'), 'actions': lesson.get('actions'),
                           'ok': lesson.get('ok', 0), 'fail': lesson.get('fail', 0),
                           'score': round(score, 2)})
        return result

    # ---------- 维护 ----------

    def maintain(self, now=None):
        """清理低价值经验：反复失败、长期没用且没成功过的，避免越攒越乱。"""
        now = time.time() if now is None else float(now)
        removed_failed = removed_stale = 0
        with self._lock:
            kept = []
            for lesson in self._lessons():
                ok = int(lesson.get('ok') or 0)
                fail = int(lesson.get('fail') or 0)
                updated = float(lesson.get('updated') or 0)
                if ok == 0 and fail >= 3:
                    removed_failed += 1
                    continue
                if ok == 0 and updated and now - updated > 180 * 86400:
                    removed_stale += 1
                    continue
                kept.append(lesson)
            self._state['lessons'] = kept
            self.save()
            return {'removed_failed': removed_failed, 'removed_stale': removed_stale,
                    'lessons': len(kept), 'skills': len(self._state['skills']),
                    'preferences': len(self._state['preferences'])}

    # ---------- 技能包 ----------

    def preview_skill(self, package):
        """校验技能包并返回展示信息，不写入学习库。"""
        record = self._skill_record(package, '')
        return record

    def _skill_record(self, package, source=''):
        if isinstance(package, str):
            if len(package) > MAX_BYTES:
                raise ValueError('技能包过大')
            try:
                package = json.loads(package)
            except (ValueError, RecursionError):
                raise ValueError('技能包不是有效的 JSON') from None
        if not isinstance(package, dict):
            raise ValueError('技能包必须是 JSON 对象')
        if package.get('format') != SKILL_FORMAT:
            raise ValueError(f'技能包格式不对，应为 {SKILL_FORMAT}')
        if type(package.get('version')) is not int or package['version'] != 1:
            raise ValueError('只支持 version=1 的技能包')
        name = str(package.get('name') or '').strip()[:60]
        instructions = str(package.get('instructions') or '').strip()[:8000]
        if not name or not instructions:
            raise ValueError('技能包需要 name 与 instructions')
        triggers = package.get('triggers') or []
        if not isinstance(triggers, list) or len(triggers) > 30:
            raise ValueError('技能包触发词最多 30 个')
        triggers = [str(word).strip()[:40] for word in triggers if str(word).strip()]
        permissions = package.get('permissions') or []
        if not isinstance(permissions, list) or any(str(p) not in SKILL_PERMISSIONS for p in permissions):
            raise ValueError('技能包声明了不支持的权限：' + '、'.join(sorted(set(map(str, permissions)) - SKILL_PERMISSIONS)))
        identifier = str(package.get('id') or '').strip()[:60] or 'skill_' + uuid_like(name)[:12]
        if not re.fullmatch(r'[A-Za-z0-9_\-]{1,60}', identifier):
            raise ValueError('技能包 id 只能包含字母、数字、下划线和短横线')
        return {'id': identifier, 'name': name,
                'description': str(package.get('description') or '')[:500],
                'instructions': instructions, 'triggers': triggers,
                'permissions': [str(p) for p in permissions],
                'enabled': True, 'source': str(source or '')[:300], 'imported': time.time()}

    def set_skill_enabled(self, identifier, enabled):
        if type(enabled) is not bool:
            raise ValueError('启用状态必须是布尔值')
        with self._lock:
            skill = next((s for s in self._state['skills'] if s.get('id') == identifier), None)
            if skill is None:
                raise ValueError('技能不存在')
            skill['enabled'] = enabled
            self.save()
            return skill

    def import_skill(self, package, source=''):
        """导入技能包（纯数据，不执行）。返回写入后的技能记录。"""
        record = self._skill_record(package, source)
        with self._lock:
            old = next((s for s in self._state['skills'] if s.get('id') == record['id']), None)
            if old is not None:
                record['enabled'] = bool(old.get('enabled', True))
                record['imported'] = old.get('imported') or record['imported']
            self._state['skills'] = [s for s in self._state['skills'] if s.get('id') != record['id']]
            self._state['skills'].append(record)
            self.save()
        return record

    def forget_skill(self, identifier):
        with self._lock:
            before = len(self._state['skills'])
            self._state['skills'] = [s for s in self._state['skills'] if s.get('id') != identifier]
            self.save()
        return len(self._state['skills']) != before

    def match_skills(self, text, limit=3):
        scored = []
        for skill in self._state['skills']:
            if not skill.get('enabled', True):
                continue
            score = max([similarity(text, trigger) for trigger in skill.get('triggers') or []] or [0.0])
            if score >= .34:
                scored.append((score, skill))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [{'id': s.get('id'), 'name': s.get('name'), 'instructions': s.get('instructions'),
                 'score': round(score, 2)} for score, s in scored[:limit]]

    # ---------- 概览 ----------

    def stats(self):
        with self._lock:
            lessons = self._lessons()
            ok = sum(int(l.get('ok') or 0) for l in lessons)
            fail = sum(int(l.get('fail') or 0) for l in lessons)
            return {'lessons': len(lessons), 'skills': len(self._state['skills']),
                    'preferences': len(self._state['preferences']),
                    'success': ok, 'failure': fail,
                    'path': str(self.path)}

    def overview(self, limit=50):
        with self._lock:
            lessons = sorted(self._lessons(), key=lambda l: float(l.get('updated') or 0), reverse=True)
            return {'stats': self.stats(),
                    'preferences': [{'key': key, **value} for key, value in
                                    sorted(self._state['preferences'].items(),
                                           key=lambda kv: float((kv[1] or {}).get('updated') or 0),
                                           reverse=True)],
                    'lessons': [{'id': l.get('id'), 'intent': l.get('intent'),
                                 'summary': l.get('summary'), 'actions': l.get('actions'),
                                 'ok': l.get('ok', 0), 'fail': l.get('fail', 0),
                                 'note': l.get('note'), 'updated': l.get('updated')}
                                for l in lessons[:max(1, int(limit))]],
                    'skills': [{'id': s.get('id'), 'name': s.get('name'),
                                'description': s.get('description'), 'triggers': s.get('triggers'),
                                'permissions': s.get('permissions') or [],
                                'enabled': bool(s.get('enabled', True)),
                                'imported': s.get('imported')}
                               for s in reversed(self._state['skills'])]}


def uuid_like(seed: str) -> str:
    return hashlib.sha256(str(seed).encode('utf-8')).hexdigest()[:16]
