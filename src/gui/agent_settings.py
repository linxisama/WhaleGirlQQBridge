"""Agent 工具环 / 夜间固化 / 自画像的界面开关（都只写 config.qq.yaml 的 ai 段）。

这些键决定「主体 agent 能不能自己一轮轮调工具」，以及「夜里要不要固化记忆、重写自画像」。
后端读的是 config.yaml 的 ai 段（src/agent_tools.py 的 loop_settings、src/agent_scheduler.py）；
但设置页其它字段走的是 POST /api/settings，那条路的请求模型是固定字段表，只认它自己列出来
的键 —— 所以这里不绕道后端，直接用后端自己读写的同一个模块 src.config_manager 写**既有
ai 段**的键（save_atomic：先 .yaml.tmp 再 replace），不新增配置段。

工具环默认 false / 3 / 20，轮数夹 1..5、超时夹 1..60 秒；夜间固化默认关、回看 3 天
（1..30）、每夜最多 3 条主题（1..10）、命中概率 0.35（0..1）、自画像默认关。手改配置写进
脏值也不要紧，读取时一律归一化，界面永远能显示，绝不因为配置脏而抛崩。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QCheckBox, QDoubleSpinBox, QFormLayout, QGroupBox,
                             QLabel, QSpinBox, QVBoxLayout, QWidget)

try:                              # 单独跑纯逻辑时不需要后端那一整套依赖
    from src.config_manager import ConfigManager
except Exception:                 # pragma: no cover - 导入环境残缺时才发生
    ConfigManager = None          # type: ignore[assignment]

CONFIG_FILE_NAME = 'config.qq.yaml'
KEY_ENABLED = 'ai.agent_tools'
KEY_MAX_ROUNDS = 'ai.agent_tools_max_rounds'
KEY_TIMEOUT = 'ai.agent_tools_timeout'
#: 夜间固化 / 自画像的五个键（引擎在 src/agent_dream.py、src/agent_portrait.py，这里只管配置）
KEY_DREAM_ENABLED = 'ai.dream_enabled'
KEY_LOOKBACK_DAYS = 'ai.dream_lookback_days'
KEY_MAX_CLUSTERS = 'ai.dream_max_clusters'
KEY_DREAM_RATE = 'ai.dream_rate'
KEY_PORTRAIT_ENABLED = 'ai.portrait_enabled'

DEFAULTS = {'enabled': False, 'max_rounds': 3, 'timeout': 20}
ROUNDS_RANGE = (1, 5)
TIMEOUT_RANGE = (1, 60)

GROWTH_DEFAULTS = {'dream_enabled': False, 'lookback_days': 3, 'max_clusters': 3,
                   'dream_rate': 0.35, 'portrait_enabled': False}
LOOKBACK_RANGE = (1, 30)
CLUSTERS_RANGE = (1, 10)
RATE_RANGE = (0.0, 1.0)

SAVE_DEBOUNCE_MS = 600

#: 字段表 = (界面内部字段名, 配置键, 类型, 默认值)；类型决定用哪种归一化
_TOOL_FIELDS = (
    ('enabled', KEY_ENABLED, 'bool', DEFAULTS['enabled']),
    ('max_rounds', KEY_MAX_ROUNDS, 'int', DEFAULTS['max_rounds']),
    ('timeout', KEY_TIMEOUT, 'int', DEFAULTS['timeout']),
)
_GROWTH_FIELDS = (
    ('dream_enabled', KEY_DREAM_ENABLED, 'bool', GROWTH_DEFAULTS['dream_enabled']),
    ('lookback_days', KEY_LOOKBACK_DAYS, 'int', GROWTH_DEFAULTS['lookback_days']),
    ('max_clusters', KEY_MAX_CLUSTERS, 'int', GROWTH_DEFAULTS['max_clusters']),
    ('dream_rate', KEY_DREAM_RATE, 'float', GROWTH_DEFAULTS['dream_rate']),
    ('portrait_enabled', KEY_PORTRAIT_ENABLED, 'bool', GROWTH_DEFAULTS['portrait_enabled']),
)
#: 数字字段的夹取区间（键是字段名）
_RANGES = {'max_rounds': ROUNDS_RANGE, 'timeout': TIMEOUT_RANGE,
           'lookback_days': LOOKBACK_RANGE, 'max_clusters': CLUSTERS_RANGE,
           'dream_rate': RATE_RANGE}

#: 测试/嵌入方可以改这里，把落盘目标挪到临时目录，绝不写用户真实配置。
_CONFIG_PATH_OVERRIDE: Optional[Path] = None


def set_config_path(path: Optional[Path]) -> None:
    """改写落盘目标（测试用；传 None 恢复默认）。"""
    global _CONFIG_PATH_OVERRIDE
    _CONFIG_PATH_OVERRIDE = Path(path) if path else None


def default_config_path() -> Path:
    """config.qq.yaml 的位置：优先界面模块的 ROOT（冻结包里在 exe 旁边）。"""
    if _CONFIG_PATH_OVERRIDE is not None:
        return Path(_CONFIG_PATH_OVERRIDE)
    gui = sys.modules.get('qq_gui')
    root = getattr(gui, 'ROOT', None) if gui is not None else None
    if root:
        return Path(root) / CONFIG_FILE_NAME
    return Path(__file__).resolve().parents[2] / CONFIG_FILE_NAME


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value if value is not None else '').strip().lower()
    if text in ('1', 'true', 'yes', 'on', 'y'):
        return True
    if text in ('0', 'false', 'no', 'off', 'n', ''):
        return False
    return default


def _clamp_int(value: Any, bounds: tuple, default: int) -> int:
    """数字就夹进区间；不是数字（手改成的字符串/None/列表/.inf）就回退默认值。"""
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return int(default)
    low, high = bounds
    return max(int(low), min(int(high), number))


def _clamp_float(value: Any, bounds: tuple, default: float) -> float:
    """同 _clamp_int，只是给概率这类小数用：非数字回退默认，越界夹到边上，NaN/inf 也算脏值。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(number):
        return float(default)
    low, high = bounds
    return round(max(float(low), min(float(high), number)), 2)


#: 键各自也认「配置文件里的短名」和「界面内部用的短名」，调用方不必记拼法
_ALIASES = {
    KEY_ENABLED: ('agent_tools', 'enabled'),
    KEY_MAX_ROUNDS: ('agent_tools_max_rounds', 'max_rounds'),
    KEY_TIMEOUT: ('agent_tools_timeout', 'timeout'),
    KEY_DREAM_ENABLED: ('dream_enabled',),
    KEY_LOOKBACK_DAYS: ('dream_lookback_days', 'lookback_days'),
    KEY_MAX_CLUSTERS: ('dream_max_clusters', 'max_clusters'),
    KEY_DREAM_RATE: ('dream_rate',),
    KEY_PORTRAIT_ENABLED: ('portrait_enabled',),
}


def _pick(data: dict, key: str, default: Any = None) -> Any:
    """取键值：扁平 {'ai.agent_tools': x} 和嵌套 {'ai': {'agent_tools': x}} 都认。"""
    if key in data:
        return data[key]
    node: Any = data
    for part in key.split('.'):
        if not isinstance(node, dict) or part not in node:
            break
        node = node[part]
    else:
        return node
    for short in _ALIASES.get(key, (key.rsplit('.', 1)[-1],)):
        if short in data:
            return data[short]
    return default


def _field_value(field: str, key: str, kind: str, default: Any, data: dict) -> Any:
    """按字段类型取一个值：布尔认 yes/on，数字夹区间，脏值回退默认。"""
    value = _pick(data, key, default)
    if kind == 'bool':
        return _as_bool(value, bool(default))
    if kind == 'int':
        return _clamp_int(value, _RANGES[field], int(default))
    return _clamp_float(value, _RANGES[field], float(default))


def _defaults_of(fields: tuple) -> dict:
    return {field: default for field, _key, _kind, default in fields}


def normalize(raw: Any, fields: tuple = _TOOL_FIELDS) -> dict:
    """把配置/接口来的值归一化成界面可用值（脏值一律回退默认）。

    默认归一是工具环三键；传 _GROWTH_FIELDS 就是夜间固化 / 自画像那五个键。
    """
    data = raw if isinstance(raw, dict) else {}
    return {field: _field_value(field, key, kind, default, data)
            for field, key, kind, default in fields}


def read_values(path: Optional[Path] = None, fields: tuple = _TOOL_FIELDS) -> dict:
    """从 config.yaml 读字段表里的键（文件不存在/读坏都退回默认值，不抛）。"""
    target = Path(path) if path else default_config_path()
    if ConfigManager is None or not target.exists():
        return _defaults_of(fields)
    try:
        data = ConfigManager(str(target)).load() or {}
    except Exception:
        return _defaults_of(fields)
    return normalize(data, fields)


def write_values(values: dict, path: Optional[Path] = None,
                 fields: tuple = _TOOL_FIELDS) -> dict:
    """把字段表里的键写回 config.yaml 的 ai 段（既有键位、原子落盘，其它键不碰）。"""
    target = Path(path) if path else default_config_path()
    clean = normalize(values, fields)
    if ConfigManager is None:
        return {'ok': False, 'error': '配置模块不可用', 'path': str(target)}
    if not target.exists():
        return {'ok': False, 'error': target.name + ' 还不存在（先启动一次控制台）',
                'path': str(target)}
    try:
        manager = ConfigManager(str(target))
        manager.load()
        for field, key, _kind, _default in fields:
            manager.set(key, clean[field])
        manager.save_atomic()
    except Exception as exc:
        return {'ok': False, 'error': type(exc).__name__ + ': ' + str(exc),
                'path': str(target)}
    return {'ok': True, 'path': str(target), 'values': clean}


def normalize_growth(raw: Any) -> dict:
    """夜间固化 / 自画像五键的归一化（同 normalize，只是换字段表）。"""
    return normalize(raw, _GROWTH_FIELDS)


def read_growth_values(path: Optional[Path] = None) -> dict:
    """从 config.yaml 读夜间固化 / 自画像五键（读不到就默认值：两个开关都是关）。"""
    return read_values(path, _GROWTH_FIELDS)


def write_growth_values(values: dict, path: Optional[Path] = None) -> dict:
    """把夜间固化 / 自画像五键写回 ai 段；工具环那三个键一个都不碰。"""
    return write_values(values, path, _GROWTH_FIELDS)

class AgentToolsGroup(QGroupBox):
    """设置页里的「Agent 工具环」区块：一个开关 + 轮数 + 超时。"""

    def __init__(self, config_path: Optional[Path] = None,
                 parent: Optional[QWidget] = None):
        super().__init__('Agent 工具环', parent)
        self._config_path = Path(config_path) if config_path else None
        self.setObjectName('AgentToolsGroup')

        self.chk_enabled = QCheckBox('允许 agent 自己调用工具（多轮工具循环）')
        self.chk_enabled.setObjectName('agentToolsEnabled')
        self.chk_enabled.setToolTip('打开后 agent 会自己一轮轮调工具，直到做完或到轮数上限；默认关闭。')

        self.sp_rounds = QSpinBox()
        self.sp_rounds.setObjectName('agentToolsMaxRounds')
        self.sp_rounds.setRange(*ROUNDS_RANGE)
        self.sp_rounds.setSuffix(' 轮')
        self.sp_rounds.setToolTip('单次对话最多连续几轮工具调用（1..5）。')

        self.sp_timeout = QSpinBox()
        self.sp_timeout.setObjectName('agentToolsTimeout')
        self.sp_timeout.setRange(*TIMEOUT_RANGE)
        self.sp_timeout.setSuffix(' 秒')
        self.sp_timeout.setToolTip('单轮工具调用的最长等待时间（1..60 秒）。')

        self.hint = QLabel('开了之后 agent 会自己调工具（多轮）。默认关闭：关闭时 agent 只回话，不动作。')
        self.hint.setObjectName('agentToolsHint')
        self.hint.setWordWrap(True)
        self.hint.setTextFormat(Qt.TextFormat.PlainText)

        self.path_hint = QLabel('')
        self.path_hint.setObjectName('agentToolsPathHint')
        self.path_hint.setWordWrap(True)

        form = QFormLayout()
        form.addRow('轮数上限', self.sp_rounds)
        form.addRow('单轮超时', self.sp_timeout)
        layout = QVBoxLayout(self)
        layout.addWidget(self.chk_enabled)
        layout.addWidget(self.hint)
        layout.addLayout(form)
        layout.addWidget(self.path_hint)

        self._pending = QTimer(self)
        self._pending.setSingleShot(True)
        self._pending.setInterval(SAVE_DEBOUNCE_MS)
        self._pending.timeout.connect(self.save)
        self.chk_enabled.toggled.connect(self._schedule_save)
        self.sp_rounds.valueChanged.connect(self._schedule_save)
        self.sp_timeout.valueChanged.connect(self._schedule_save)

        self.load()

    # ── 路径 ────────────────────────────────────────────────────────────
    def config_path(self) -> Path:
        return self._config_path or default_config_path()

    def set_config_path(self, path: Optional[Path]) -> None:
        self._config_path = Path(path) if path else None
        self.load()

    # ── 读写 ────────────────────────────────────────────────────────────
    def values(self) -> dict:
        """控件当前值（已夹取，任何时候都是合法值）。"""
        return normalize({
            KEY_ENABLED: self.chk_enabled.isChecked(),
            KEY_MAX_ROUNDS: self.sp_rounds.value(),
            KEY_TIMEOUT: self.sp_timeout.value(),
        })

    def load(self) -> dict:
        values = read_values(self.config_path())
        self._apply(values)
        return values

    def populate(self, data: Any = None) -> dict:
        """设置页 populate 的委托入口：以磁盘上的 config.yaml 为准。"""
        return self.load()

    def collect(self) -> dict:
        """设置页 collect 的委托入口：把控件值落盘（返回落盘结果）。"""
        return self.save()

    def save(self) -> dict:
        self._pending.stop()
        result = write_values(self.values(), self.config_path())
        if result.get('ok'):
            name = Path(result['path']).name
            self.path_hint.setText('已写入 ' + name + ' 的 ai.agent_tools*；重启控制台后生效。')
        else:
            self.path_hint.setText('没写进配置：' + str(result.get('error')))
        return result

    def _apply(self, values: dict) -> None:
        widgets = (self.chk_enabled, self.sp_rounds, self.sp_timeout)
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self.chk_enabled.setChecked(bool(values['enabled']))
            self.sp_rounds.setValue(int(values['max_rounds']))
            self.sp_timeout.setValue(int(values['timeout']))
        finally:
            for widget in widgets:
                widget.blockSignals(False)

    def _schedule_save(self, *_args: Any) -> None:
        self._pending.start()


class AgentGrowthGroup(QGroupBox):
    """设置页里的「做梦与自画像」区块：两个开关 + 回看天数 + 每晚上限 + 命中概率。

    写的是 ai.dream_enabled / ai.dream_lookback_days / ai.dream_max_clusters /
    ai.dream_rate / ai.portrait_enabled —— 同一份 ai 段，与工具环区块互不覆盖。
    """

    def __init__(self, config_path: Optional[Path] = None,
                 parent: Optional[QWidget] = None):
        super().__init__('做梦与自画像', parent)
        self._config_path = Path(config_path) if config_path else None
        self.setObjectName('AgentGrowthGroup')

        self.chk_dream = QCheckBox('夜间固化记忆（把当天的聊天压成记忆）')
        self.chk_dream.setObjectName('dreamEnabled')
        self.chk_dream.setToolTip('默认关闭；关掉就不跑，夜里不会去翻聊天记录。')

        self.chk_portrait = QCheckBox('每晚重写自画像')
        self.chk_portrait.setObjectName('portraitEnabled')
        self.chk_portrait.setToolTip('默认关闭；关掉就不跑，自画像文件保持原样。')

        self.sp_lookback = QSpinBox()
        self.sp_lookback.setObjectName('dreamLookbackDays')
        self.sp_lookback.setRange(*LOOKBACK_RANGE)
        self.sp_lookback.setSuffix(' 天')
        self.sp_lookback.setToolTip('回看最近几天的聊天来固化记忆（1..30 天）。')

        self.sp_clusters = QSpinBox()
        self.sp_clusters.setObjectName('dreamMaxClusters')
        self.sp_clusters.setRange(*CLUSTERS_RANGE)
        self.sp_clusters.setSuffix(' 条')
        self.sp_clusters.setToolTip('每晚最多固化几条主题（1..10）。')

        self.sp_rate = QDoubleSpinBox()
        self.sp_rate.setObjectName('dreamRate')
        self.sp_rate.setDecimals(2)
        self.sp_rate.setRange(*RATE_RANGE)
        self.sp_rate.setSingleStep(0.05)
        self.sp_rate.setToolTip('命中概率：0 = 每晚都不跑，1 = 每晚都跑（默认 0.35）。')

        self.hint = QLabel('关掉就不跑；想手动跑一次：先建一个定时任务，再点「立即运行」。')
        self.hint.setObjectName('agentGrowthHint')
        self.hint.setWordWrap(True)
        self.hint.setTextFormat(Qt.TextFormat.PlainText)

        self.path_hint = QLabel('')
        self.path_hint.setObjectName('agentGrowthPathHint')
        self.path_hint.setWordWrap(True)

        form = QFormLayout()
        form.addRow('回看天数', self.sp_lookback)
        form.addRow('每晚上限', self.sp_clusters)
        form.addRow('命中概率', self.sp_rate)
        layout = QVBoxLayout(self)
        layout.addWidget(self.chk_dream)
        layout.addWidget(self.chk_portrait)
        layout.addWidget(self.hint)
        layout.addLayout(form)
        layout.addWidget(self.path_hint)

        self._pending = QTimer(self)
        self._pending.setSingleShot(True)
        self._pending.setInterval(SAVE_DEBOUNCE_MS)
        self._pending.timeout.connect(self.save)
        self.chk_dream.toggled.connect(self._schedule_save)
        self.chk_portrait.toggled.connect(self._schedule_save)
        self.sp_lookback.valueChanged.connect(self._schedule_save)
        self.sp_clusters.valueChanged.connect(self._schedule_save)
        self.sp_rate.valueChanged.connect(self._schedule_save)

        self.load()

    # ── 路径 ────────────────────────────────────────────────────────────
    def config_path(self) -> Path:
        return self._config_path or default_config_path()

    def set_config_path(self, path: Optional[Path]) -> None:
        self._config_path = Path(path) if path else None
        self.load()

    # ── 读写 ────────────────────────────────────────────────────────────
    def values(self) -> dict:
        """控件当前值（已夹取，任何时候都是合法值）。"""
        return normalize_growth({
            KEY_DREAM_ENABLED: self.chk_dream.isChecked(),
            KEY_LOOKBACK_DAYS: self.sp_lookback.value(),
            KEY_MAX_CLUSTERS: self.sp_clusters.value(),
            KEY_DREAM_RATE: self.sp_rate.value(),
            KEY_PORTRAIT_ENABLED: self.chk_portrait.isChecked(),
        })

    def load(self) -> dict:
        values = read_growth_values(self.config_path())
        self._apply(values)
        return values

    def populate(self, data: Any = None) -> dict:
        """设置页 populate 的委托入口：以磁盘上的 config.yaml 为准。"""
        return self.load()

    def collect(self) -> dict:
        """设置页 collect 的委托入口：把控件值落盘（返回落盘结果）。"""
        return self.save()

    def save(self) -> dict:
        self._pending.stop()
        result = write_growth_values(self.values(), self.config_path())
        if result.get('ok'):
            name = Path(result['path']).name
            self.path_hint.setText('已写入 ' + name + ' 的 ai.dream_* / ai.portrait_enabled；重启控制台后生效。')
        else:
            self.path_hint.setText('没写进配置：' + str(result.get('error')))
        return result

    def _apply(self, values: dict) -> None:
        widgets = (self.chk_dream, self.chk_portrait, self.sp_lookback,
                   self.sp_clusters, self.sp_rate)
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self.chk_dream.setChecked(bool(values['dream_enabled']))
            self.chk_portrait.setChecked(bool(values['portrait_enabled']))
            self.sp_lookback.setValue(int(values['lookback_days']))
            self.sp_clusters.setValue(int(values['max_clusters']))
            self.sp_rate.setValue(float(values['dream_rate']))
        finally:
            for widget in widgets:
                widget.blockSignals(False)

    def _schedule_save(self, *_args: Any) -> None:
        self._pending.start()
