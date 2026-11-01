"""音色库（T05/T06/T14）：把参考音频/标签表导入成可路由的 voice profile。

约定（AGENTS.md 1.8）：
- 上传文件复制到运行数据目录 ``data/plugin_uploads/voice_profiles/<profile_id>/``，
  之后删除原文件仍可用；
- 标签表用 ``labeled_files``、多参考音频用 ``files``、单个参考音频用 ``file``；
- 只接受本机文件，不下载 URL；源文件超限/损坏/扩展名不符都明确拒绝。

一个 profile 的 ``provider_map`` 就是各引擎的参数覆盖：
- audio.cpp：``{"model": ..., "voice": ...}``
- GPT-SoVITS：``{"ref_audio_path": ..., "prompt_text": ..., "text_lang": ...}``
- Qwen WebUI：``{"mode": "voice_clone"|"custom_voice", "ref_audio_path": ...,
  "speaker": ..., "model": ...}``
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Iterable, Optional

from .audio_contracts import ErrorCode, reject
from .audio_contracts_db import initialize as initialize_schema

MAX_REFERENCE_BYTES = 8 * 1024 * 1024
MAX_AUDIO_FILES = 20
MAX_TABLE_ROWS = 200
AUDIO_SUFFIXES = {'.wav', '.flac', '.mp3', '.m4a'}
ENGINES = ('audio_cpp', 'gpt_sovits_api_v2', 'qwen_webui')
_ID = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
_LANG = re.compile(r'^[A-Za-z-]{2,16}$')


def _slug(text: str) -> str:
    raw = re.sub(r'[^A-Za-z0-9_-]+', '-', str(text or '').strip()).strip('-_').lower()
    if raw:
        return raw[:64]
    digest = hashlib.sha256(str(text or '').encode('utf-8')).hexdigest()[:12]
    return 'voice-' + digest


def _split_engines(value: str) -> list[str]:
    if not value:
        return list(ENGINES)
    parts = [part.strip() for part in re.split(r'[,|+; ]+', str(value)) if part.strip()]
    bad = [part for part in parts if part not in ENGINES]
    if bad:
        reject(ErrorCode.INVALID_INPUT, '音色引擎须为 audio_cpp/gpt_sovits_api_v2/qwen_webui',
               value=','.join(bad))
    return parts


def _truthy(value) -> bool:
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'y', 'on', '是'}


class VoiceProfileStore:
    def __init__(self, root, db: sqlite3.Connection):
        self.root = Path(root)
        self.db = db
        self.directory = self.root / 'data' / 'plugin_uploads' / 'voice_profiles'
        initialize_schema(db)

    # ── 查询 ────────────────────────────────────────────────────────────────
    def list(self) -> list[dict]:
        rows = self.db.execute(
            'SELECT profile_id,persona_id,provider_map,ref_asset_hash,language,'
            'licensed_use,allow_cloud,model_version,weight_version FROM voice_profiles '
            'ORDER BY profile_id').fetchall()
        return [self._row(row) for row in rows]

    def get(self, profile_id: str) -> Optional[dict]:
        row = self.db.execute(
            'SELECT profile_id,persona_id,provider_map,ref_asset_hash,language,'
            'licensed_use,allow_cloud,model_version,weight_version FROM voice_profiles '
            'WHERE profile_id=?', (str(profile_id or ''),)).fetchone()
        return self._row(row) if row else None

    def delete(self, profile_id: str) -> bool:
        profile_id = str(profile_id or '')
        row = self.get(profile_id)
        if row is None:
            return False
        self.db.execute('DELETE FROM voice_profiles WHERE profile_id=?', (profile_id,))
        self.db.commit()
        target = (self.directory / profile_id).resolve()
        if target.is_relative_to(self.directory.resolve()) and target.is_dir():
            for child in target.glob('*'):
                try:
                    child.unlink()
                except OSError:
                    pass
            try:
                target.rmdir()
            except OSError:
                pass
        return True

    @staticmethod
    def _row(row) -> dict:
        try:
            mapping = json.loads(row[2] or '{}')
        except (TypeError, ValueError):
            mapping = {}
        if not isinstance(mapping, dict):
            mapping = {}
        return {'profile_id': row[0], 'name': row[1] or row[0],
                'provider_map': mapping, 'ref_audio_hash': row[3] or '',
                'language': row[4] or 'zh',
                'licensed': str(row[5] or '').strip().lower() not in (
                    '', '0', 'false', 'unverified', 'none'),
                'allow_cloud': bool(row[6]),
                'model_version': row[7] or '', 'weight_version': row[8] or '',
                'ref_audio_path': VoiceProfileStore._ref_path(mapping)}

    @staticmethod
    def _ref_path(mapping: dict) -> str:
        for entry in mapping.values():
            if isinstance(entry, dict) and entry.get('ref_audio_path'):
                return str(entry['ref_audio_path'])
        return ''

    # ── 导入：多个参考音频 ─────────────────────────────────────────────────
    def import_audio(self, paths: Iterable[str], *, provider: str = '',
                     language: str = 'zh', licensed: bool = False,
                     allow_cloud: bool = False) -> list[dict]:
        """每个音频 = 一个音色；文件名（去扩展名）作为音色名/编号。"""
        sources = [Path(str(item)) for item in (paths or [])]
        if not sources:
            reject(ErrorCode.INVALID_INPUT, '没有选择参考音频')
        if len(sources) > MAX_AUDIO_FILES:
            reject(ErrorCode.INVALID_INPUT, f'一次最多导入 {MAX_AUDIO_FILES} 个音色')
        engines = _split_engines(provider)
        imported = []
        used: set[str] = set()
        for source in sources:
            reference = self._validate_reference(source)
            profile_id = _slug(source.stem)
            base = profile_id
            index = 2
            while profile_id in used:
                profile_id = f'{base}-{index}'
                index += 1
            used.add(profile_id)
            copied, digest = self._copy_reference(reference, profile_id)
            profile = self._build_profile(
                profile_id=profile_id, name=source.stem, engines=engines,
                language=language, reference=copied, digest=digest,
                model='', voice=profile_id, speaker='', ref_text='',
                prompt_text='', licensed=licensed, allow_cloud=allow_cloud)
            imported.append(self._save(profile))
        return imported

    # ── 导入：标签表 ────────────────────────────────────────────────────────
    def import_table(self, table_path: str, *, language: str = 'zh') -> list[dict]:
        """读取 CSV/TSV/JSON 标签表；参考音频路径相对表所在目录解析。"""
        path = Path(str(table_path or ''))
        if not path.is_file():
            reject(ErrorCode.INVALID_INPUT, '音色标签表不存在或不可读')
        if path.stat().st_size > 4 * 1024 * 1024:
            reject(ErrorCode.INVALID_INPUT, '音色标签表超过 4MB 上限')
        rows = self._read_table(path)
        if not rows:
            reject(ErrorCode.INVALID_INPUT, '音色标签表没有数据行')
        if len(rows) > MAX_TABLE_ROWS:
            reject(ErrorCode.INVALID_INPUT, f'音色标签表最多 {MAX_TABLE_ROWS} 行')
        imported = []
        for index, raw in enumerate(rows, start=1):
            if not isinstance(raw, dict) or not raw:
                continue
            normalized = {str(key).strip().lower(): value for key, value in raw.items()}
            name = str(normalized.get('name') or normalized.get('label')
                       or normalized.get('id') or normalized.get('voice_id') or '').strip()
            profile_id = str(normalized.get('id') or normalized.get('profile_id')
                             or normalised_voice(normalized) or _slug(name or f'voice-{index}'))
            if not _ID.fullmatch(profile_id):
                profile_id = _slug(profile_id or name or f'voice-{index}')
            engines = _split_engines(str(normalized.get('engine')
                                          or normalized.get('provider')
                                          or normalized.get('engines') or ''))
            ref_value = str(normalized.get('ref_audio') or normalized.get('reference')
                            or normalized.get('audio') or normalized.get('sample') or '').strip()
            reference, digest = None, ''
            if ref_value:
                source = Path(ref_value)
                if not source.is_absolute():
                    source = path.parent / source
                reference = self._validate_reference(source)
                reference, digest = self._copy_reference(reference, profile_id)
            row_language = str(normalized.get('language') or normalized.get('lang')
                               or language or 'zh')
            if not _LANG.fullmatch(row_language):
                row_language = 'zh'
            profile = self._build_profile(
                profile_id=profile_id, name=name or profile_id, engines=engines,
                language=row_language, reference=reference, digest=digest,
                model=str(normalized.get('model') or normalized.get('model_name') or ''),
                voice=str(normalized.get('voice') or normalized.get('speaker') or ''),
                speaker=str(normalized.get('speaker') or normalized.get('voice') or ''),
                ref_text=str(normalized.get('ref_text') or ''),
                prompt_text=str(normalized.get('prompt_text') or ''),
                licensed=_truthy(normalized.get('licensed') or normalized.get('license')),
                allow_cloud=_truthy(normalized.get('allow_cloud')))
            imported.append(self._save(profile))
        return imported

    # ── 构造/落库 ───────────────────────────────────────────────────────────
    def _build_profile(self, *, profile_id: str, name: str, engines: list[str],
                       language: str, reference, digest: str, model: str,
                       voice: str, speaker: str, ref_text: str, prompt_text: str,
                       licensed: bool, allow_cloud: bool) -> dict:
        language = language if _LANG.fullmatch(str(language or '')) else 'zh'
        mapping: dict[str, dict] = {}
        for engine in engines:
            if engine == 'audio_cpp':
                mapping[engine] = {'model': model, 'voice': voice or profile_id}
            elif engine == 'gpt_sovits_api_v2':
                mapping[engine] = {'ref_audio_path': str(reference or ''),
                                   'prompt_text': prompt_text or ref_text,
                                   'text_lang': language, 'prompt_lang': language}
            elif engine == 'qwen_webui':
                mapping[engine] = {
                    'mode': 'voice_clone' if reference else 'custom_voice',
                    'ref_audio_path': str(reference or ''),
                    'speaker': speaker or voice or profile_id,
                    'model': model,
                    'instruct': '',
                }
        return {'profile_id': profile_id, 'name': name, 'provider_map': mapping,
                'ref_audio_hash': digest, 'language': language,
                'licensed': bool(licensed), 'allow_cloud': bool(allow_cloud),
                'model_version': model, 'weight_version': ''}

    def _save(self, profile: dict) -> dict:
        mapping = json.dumps(profile['provider_map'], ensure_ascii=False, sort_keys=True)
        self.db.execute(
            'INSERT INTO voice_profiles (profile_id,persona_id,provider_map,model_version,'
            'weight_version,ref_asset_id,ref_asset_hash,language,licensed_use,allow_cloud) '
            'VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(profile_id) DO UPDATE SET '
            'persona_id=excluded.persona_id,provider_map=excluded.provider_map,'
            'model_version=excluded.model_version,weight_version=excluded.weight_version,'
            'ref_asset_id=excluded.ref_asset_id,ref_asset_hash=excluded.ref_asset_hash,'
            'language=excluded.language,licensed_use=excluded.licensed_use,'
            'allow_cloud=excluded.allow_cloud',
            (profile['profile_id'], str(profile['name'] or profile['profile_id']), mapping,
             str(profile['model_version'] or ''), str(profile['weight_version'] or ''),
             str(profile['ref_audio_hash'] or ''), str(profile['ref_audio_hash'] or ''),
             str(profile['language'] or 'zh'),
             'imported' if profile['licensed'] else 'unverified',
             1 if profile['allow_cloud'] else 0))
        self.db.commit()
        saved = self.get(profile['profile_id'])
        return saved or profile

    # ── 标签表解析 ──────────────────────────────────────────────────────────
    @staticmethod
    def _read_table(path: Path) -> list[dict]:
        suffix = path.suffix.lower()
        if suffix == '.json':
            try:
                payload = json.loads(path.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError) as exc:
                reject(ErrorCode.INVALID_INPUT, '音色标签表不是合法 JSON', error=str(exc))
            if isinstance(payload, dict):
                payload = payload.get('voices') or payload.get('profiles') or []
            if not isinstance(payload, list):
                reject(ErrorCode.INVALID_INPUT, '音色 JSON 须为对象数组或 {"voices": [...]}')
            return [item for item in payload if isinstance(item, dict)]
        delimiter = '\t' if suffix == '.tsv' else ','
        try:
            text = path.read_text(encoding='utf-8-sig')
        except OSError as exc:
            reject(ErrorCode.INVALID_INPUT, '音色标签表不可读', error=str(exc))
        try:
            return [dict(row) for row in csv.DictReader(io.StringIO(text),
                                                        delimiter=delimiter)]
        except csv.Error as exc:
            reject(ErrorCode.INVALID_INPUT, '音色标签表无法解析', error=str(exc))

    # ── 文件复制/校验 ───────────────────────────────────────────────────────
    @staticmethod
    def _validate_reference(path: Path) -> Path:
        if not path.is_file():
            reject(ErrorCode.ASSET_INVALID, '参考音频不存在或不可读', path=path.name)
        if path.suffix.lower() not in AUDIO_SUFFIXES:
            reject(ErrorCode.ASSET_INVALID,
                   '参考音频格式须为 wav/flac/mp3/m4a', path=path.name)
        size = path.stat().st_size
        if not 44 <= size <= MAX_REFERENCE_BYTES:
            reject(ErrorCode.ASSET_INVALID,
                   '参考音频大小须在 44B–8MB', path=path.name)
        return path

    def _copy_reference(self, source: Path, profile_id: str) -> tuple[Path, str]:
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        target_dir = (self.directory / profile_id).resolve()
        if not target_dir.is_relative_to(self.directory.resolve()):
            reject(ErrorCode.ASSET_INVALID, '音色目录越出数据目录')
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / (digest[:16] + source.suffix.lower())
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            return target, digest
        temporary = target_dir / (uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_bytes(data)
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return target, digest


def normalised_voice(row: dict) -> str:
    """标签表里可能用 voice/voice_id 作为编号。"""
    return str(row.get('voice_id') or row.get('voice') or '').strip()
