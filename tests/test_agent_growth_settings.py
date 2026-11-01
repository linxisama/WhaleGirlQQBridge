"""W7c1：设置页「做梦与自画像」五键的离线回归（需要 PyQt6，缺库时整组跳过）。

锁死四件事：
1. 五个键的默认值与夹取区间：normalize / read / write 一律归一化，脏值不许抛崩；
2. 界面落盘只碰自己那五个键 —— 工具环三键、其它 ai 键、其它配置段都不许动；
3. 后端整份覆盖写回时，界面写的五个键不能被抹掉（src/qq_app.py 的 GUI_OWNED_AI_KEYS）；
4. offscreen 下能构造 AgentGrowthGroup，collect() 落盘的值与 values() 一致。

落盘目标一律在临时目录（模块级 set_config_path + 显式 config_path），绝不碰仓库里的
config.qq.yaml 与 data/；不需要网络、不碰真实 QQ。
"""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

try:
    from PyQt6.QtWidgets import QApplication
    from src.gui import agent_settings
    from src import qq_app
    HAVE_QT = True
except Exception:                 # pragma: no cover - 环境缺 PyQt6 时跳过
    HAVE_QT = False

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_APP = None

#: 这五个键名同时是 ai 段里的键名和 qq_app.GUI_OWNED_AI_KEYS 里的名字
GROWTH_KEYS = ("dream_enabled", "dream_lookback_days", "dream_max_clusters",
               "dream_rate", "portrait_enabled")
TOOL_KEYS = ("agent_tools", "agent_tools_max_rounds", "agent_tools_timeout")


def _app():
    # 必须持有模块级引用：QApplication 一旦被回收，后面构造任何 QWidget 都会直接终止进程
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def _write_cfg(path, data):
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def _read_cfg(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _bot(path, memory):
    return SimpleNamespace(
        config=SimpleNamespace(config_path=Path(path), config=memory))


@unittest.skipUnless(HAVE_QT, "需要 PyQt6")
class _Case(unittest.TestCase):
    """每个用例一个临时目录；config.qq.yaml 只出现在临时目录里。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="w7c1-")
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "config.qq.yaml"


class NormalizeTests(_Case):
    """五个键的默认值与夹取：脏配置也不能抛崩。"""

    def test_defaults_when_nothing_is_configured(self):
        self.assertEqual(agent_settings.normalize_growth({}),
                         {"dream_enabled": False, "lookback_days": 3,
                          "max_clusters": 3, "dream_rate": 0.35,
                          "portrait_enabled": False})
        # 非 dict（接口传了 None / 列表）也回退默认
        self.assertEqual(agent_settings.normalize_growth(None),
                         agent_settings.normalize_growth({}))

    def test_int_fields_are_clamped_into_range(self):
        got = agent_settings.normalize_growth({
            "ai.dream_lookback_days": 99, "ai.dream_max_clusters": 0})
        self.assertEqual(got["lookback_days"], 30, "99 天夹到上限 30")
        self.assertEqual(got["max_clusters"], 1, "0 条夹到下限 1")
        got = agent_settings.normalize_growth({
            "ai.dream_lookback_days": 0, "ai.dream_max_clusters": 99})
        self.assertEqual(got["lookback_days"], 1)
        self.assertEqual(got["max_clusters"], 10, "99 条夹到上限 10")

    def test_rate_is_clamped_and_rounded(self):
        self.assertEqual(agent_settings.normalize_growth({"ai.dream_rate": -1})["dream_rate"], 0.0)
        self.assertEqual(agent_settings.normalize_growth({"ai.dream_rate": 9})["dream_rate"], 1.0)
        self.assertEqual(agent_settings.normalize_growth({"ai.dream_rate": 0.343})["dream_rate"], 0.34)
        self.assertIsInstance(agent_settings.normalize_growth({"ai.dream_rate": 1})["dream_rate"],
                              float)

    def test_garbage_falls_back_to_defaults(self):
        dirty = {"ai": {"dream_enabled": "maybe", "dream_lookback_days": "abc",
                        "dream_max_clusters": [1, 2], "dream_rate": float("nan"),
                        "portrait_enabled": None}}
        self.assertEqual(agent_settings.normalize_growth(dirty),
                         agent_settings.normalize_growth({}))
        # .inf 也要当脏值（int(float('inf')) 会抛 OverflowError，不能漏出去）
        self.assertEqual(
            agent_settings.normalize_growth({"ai.dream_lookback_days": float("inf")})["lookback_days"],
            3)

    def test_bool_fields_accept_yes_no(self):
        got = agent_settings.normalize_growth(
            {"ai.dream_enabled": "yes", "ai.portrait_enabled": "off"})
        self.assertIs(got["dream_enabled"], True)
        self.assertIs(got["portrait_enabled"], False)

    def test_nested_flat_and_short_names_are_all_accepted(self):
        nested = agent_settings.normalize_growth(
            {"ai": {"dream_enabled": True, "dream_lookback_days": 7}})
        flat = agent_settings.normalize_growth(
            {"ai.dream_enabled": True, "ai.dream_lookback_days": 7})
        short = agent_settings.normalize_growth({"dream_enabled": True, "lookback_days": 7})
        self.assertEqual(nested, flat)
        self.assertEqual(nested, short)
        self.assertEqual((nested["dream_enabled"], nested["lookback_days"]), (True, 7))


class ConfigFileTests(_Case):
    """读盘 / 落盘：五个键只写自己，别的键与原文件都不许被顺手改掉。"""

    def test_missing_file_reads_defaults_and_is_not_created(self):
        self.assertEqual(agent_settings.read_growth_values(self.path),
                         agent_settings.normalize_growth({}))
        result = agent_settings.write_growth_values({"dream_enabled": True}, self.path)
        self.assertFalse(result["ok"], result)
        self.assertIn("不存在", result["error"])
        self.assertFalse(self.path.exists(), "不能替用户凭空造一份配置")

    def test_write_then_read_round_trips(self):
        _write_cfg(self.path, {"ai": {"api_key": "sk"}})
        wanted = {"dream_enabled": True, "lookback_days": 7, "max_clusters": 5,
                  "dream_rate": 0.5, "portrait_enabled": True}
        result = agent_settings.write_growth_values(wanted, self.path)
        self.assertTrue(result["ok"], result)
        self.assertEqual(agent_settings.read_growth_values(self.path), wanted)
        disk = _read_cfg(self.path)["ai"]
        for key, want in (("dream_enabled", True), ("dream_lookback_days", 7),
                          ("dream_max_clusters", 5), ("dream_rate", 0.5),
                          ("portrait_enabled", True)):
            self.assertEqual(disk[key], want, key)

    def test_write_keeps_other_ai_keys_and_other_sections(self):
        """只管自己五个键：api_key/model 与 style 段原样，且不新造配置段。"""
        _write_cfg(self.path, {"ai": {"api_key": "sk", "model": "m", "temperature": 0.5},
                               "style": {"active": "grumpy", "banned_words": ["坏词"]}})
        agent_settings.write_growth_values({"dream_rate": 0.2}, self.path)
        disk = _read_cfg(self.path)
        self.assertEqual(disk["ai"]["api_key"], "sk")
        self.assertEqual(disk["ai"]["model"], "m")
        self.assertEqual(disk["ai"]["temperature"], 0.5)
        self.assertEqual(disk["style"]["active"], "grumpy")
        self.assertEqual(disk["style"]["banned_words"], ["坏词"])
        # 五个键只落在 ai 段里：顶层不许多出 dream / portrait，也不许多出同名字段
        for key in ("dream", "portrait"):
            self.assertNotIn(key, disk)
        for key in GROWTH_KEYS:
            self.assertIn(key, disk["ai"], key)
            self.assertNotIn(key, disk, key)

    def test_growth_write_does_not_touch_the_tool_keys(self):
        _write_cfg(self.path, {"ai": {"agent_tools": True, "agent_tools_max_rounds": 4,
                                      "agent_tools_timeout": 45}})
        agent_settings.write_growth_values(
            {"dream_enabled": True, "lookback_days": 5, "max_clusters": 2,
             "dream_rate": 0.1, "portrait_enabled": True}, self.path)
        disk = _read_cfg(self.path)["ai"]
        self.assertIs(disk["agent_tools"], True)
        self.assertEqual(disk["agent_tools_max_rounds"], 4)
        self.assertEqual(disk["agent_tools_timeout"], 45)
        self.assertEqual(agent_settings.read_values(self.path),
                         {"enabled": True, "max_rounds": 4, "timeout": 45})

    def test_tool_write_does_not_touch_the_growth_keys(self):
        """反过来也要成立：设置页保存工具环时不许把做梦开关打回默认值。"""
        _write_cfg(self.path, {"ai": {"dream_enabled": True, "dream_lookback_days": 9,
                                      "dream_max_clusters": 6, "dream_rate": 0.7,
                                      "portrait_enabled": True}})
        agent_settings.write_values({"enabled": False, "max_rounds": 3, "timeout": 20}, self.path)
        disk = _read_cfg(self.path)["ai"]
        self.assertIs(disk["dream_enabled"], True)
        self.assertEqual(disk["dream_lookback_days"], 9)
        self.assertEqual(disk["dream_max_clusters"], 6)
        self.assertEqual(disk["dream_rate"], 0.7)
        self.assertIs(disk["portrait_enabled"], True)
        self.assertEqual(len([k for k in disk if k in GROWTH_KEYS]), 5)


class GuiOwnedKeysTests(_Case):
    """后端整份写回时，界面写的五个键必须以磁盘为准补回去。"""

    def test_the_five_keys_are_whitelisted(self):
        for key in GROWTH_KEYS:
            self.assertIn(key, qq_app.GUI_OWNED_AI_KEYS, key)
        for key in TOOL_KEYS:               # 原有三键不能丢
            self.assertIn(key, qq_app.GUI_OWNED_AI_KEYS, key)

    def test_backend_full_write_keeps_gui_values(self):
        _write_cfg(self.path, {"ai": {"api_key": "sk", "dream_enabled": True,
                                      "dream_lookback_days": 7, "dream_max_clusters": 5,
                                      "dream_rate": 0.5, "portrait_enabled": True},
                               "style": {"active": "default"}})
        memory = {"ai": {"api_key": "sk"},
                  "style": {"active": "grumpy", "banned_words": ["坏词"]}}
        qq_app._persist_config(_bot(self.path, memory))
        disk = _read_cfg(self.path)["ai"]
        self.assertIs(disk["dream_enabled"], True)
        self.assertEqual(disk["dream_lookback_days"], 7)
        self.assertEqual(disk["dream_max_clusters"], 5)
        self.assertEqual(disk["dream_rate"], 0.5)
        self.assertIs(disk["portrait_enabled"], True)
        # 后端这次写的字段照样生效，不被合并逻辑吃掉
        self.assertEqual(_read_cfg(self.path)["style"]["active"], "grumpy")

    def test_disk_value_wins_over_stale_memory(self):
        _write_cfg(self.path, {"ai": {"dream_enabled": True, "dream_rate": 0.9}})
        memory = {"ai": {"dream_enabled": False, "dream_rate": 0.35}}
        qq_app._persist_config(_bot(self.path, memory))
        disk = _read_cfg(self.path)["ai"]
        self.assertIs(disk["dream_enabled"], True)
        self.assertEqual(disk["dream_rate"], 0.9)
        # 磁盘没有的键不许凭空捏一个出来
        self.assertNotIn("dream_max_clusters", disk)
        self.assertNotIn("dream_max_clusters", memory["ai"])

    def test_gui_save_survives_backend_write(self):
        """真实链路：界面写五键 → 后端切人设写回 → 五键仍在，且界面读回一致。"""
        _write_cfg(self.path, {"ai": {"api_key": "sk"}})
        wanted = {"dream_enabled": True, "lookback_days": 4, "max_clusters": 2,
                  "dream_rate": 0.45, "portrait_enabled": False}
        saved = agent_settings.write_growth_values(wanted, self.path)
        self.assertTrue(saved["ok"], saved)
        qq_app._persist_config(_bot(self.path, {"ai": {"api_key": "sk"},
                                                "style": {"active": "grumpy"}}))
        self.assertEqual(agent_settings.read_growth_values(self.path), wanted)
        self.assertEqual(_read_cfg(self.path)["style"]["active"], "grumpy")

    def test_no_phantom_ai_section(self):
        """磁盘没有 ai 段时不能凭空造一个空的 ai: {}。"""
        _write_cfg(self.path, {"style": {"active": "default"}})
        qq_app._persist_config(_bot(self.path, {"style": {"active": "grumpy"}}))
        self.assertEqual(_read_cfg(self.path), {"style": {"active": "grumpy"}})


class AgentGrowthGroupTests(_Case):
    """控件层：offscreen 下能构造、能落盘，collect() 与 values() 一致。"""

    @classmethod
    def setUpClass(cls):
        _app()
        import qq_gui
        cls.gui = qq_gui
        cls._root_tmp = tempfile.TemporaryDirectory(prefix="w7c1-gui-")
        cls.root = Path(cls._root_tmp.name)
        cls.sandbox = _write_cfg(cls.root / "config.qq.yaml", {"ai": {"api_url": ""}})
        # 兜底：即使有用例忘了传 config_path，默认落盘目标也在临时目录
        agent_settings.set_config_path(cls.sandbox)
        cls._real_cfg = Path(qq_gui.ROOT) / "config.qq.yaml"
        cls._real_digest = (hashlib.sha256(cls._real_cfg.read_bytes()).hexdigest()
                            if cls._real_cfg.exists() else "")

    @classmethod
    def tearDownClass(cls):
        agent_settings.set_config_path(None)
        cls._root_tmp.cleanup()

    def new_group(self, path=None):
        group = agent_settings.AgentGrowthGroup(config_path=path or self.path)
        self.addCleanup(group._pending.stop)
        self.addCleanup(group.deleteLater)
        return group

    def test_controls_ranges_and_hint(self):
        _write_cfg(self.path, {"ai": {}})
        group = self.new_group()
        self.assertEqual(group.chk_dream.objectName(), "dreamEnabled")
        self.assertEqual(group.chk_portrait.objectName(), "portraitEnabled")
        self.assertEqual(group.sp_lookback.objectName(), "dreamLookbackDays")
        self.assertEqual(group.sp_clusters.objectName(), "dreamMaxClusters")
        self.assertEqual(group.sp_rate.objectName(), "dreamRate")
        # 范围就是契约里的 1..30、1..10、0.00..1.00（步进 0.05）
        self.assertEqual((group.sp_lookback.minimum(), group.sp_lookback.maximum()), (1, 30))
        self.assertEqual((group.sp_clusters.minimum(), group.sp_clusters.maximum()), (1, 10))
        self.assertEqual((group.sp_rate.minimum(), group.sp_rate.maximum()), (0.0, 1.0))
        self.assertAlmostEqual(group.sp_rate.singleStep(), 0.05)
        # 文案要讲清「关掉就不跑」和手动跑一次的办法
        self.assertIn("关掉就不跑", group.hint.text())
        self.assertIn("立即运行", group.hint.text())
        self.assertEqual(group.title(), "做梦与自画像")

    def test_defaults_match_the_contract(self):
        _write_cfg(self.path, {"ai": {}})
        group = self.new_group()
        self.assertFalse(group.chk_dream.isChecked())
        self.assertFalse(group.chk_portrait.isChecked())
        self.assertEqual((group.sp_lookback.value(), group.sp_clusters.value()), (3, 3))
        self.assertAlmostEqual(group.sp_rate.value(), 0.35)
        self.assertEqual(group.values(), agent_settings.normalize_growth({}))

    def test_collect_persists_and_matches_values(self):
        _write_cfg(self.path, {"ai": {"api_key": "sk"}})
        group = self.new_group()
        group.chk_dream.setChecked(True)
        group.chk_portrait.setChecked(True)
        group.sp_lookback.setValue(9)
        group.sp_clusters.setValue(4)
        group.sp_rate.setValue(0.55)
        values = group.values()
        result = group.collect()
        self.assertTrue(result["ok"], result)
        self.assertEqual(values, {"dream_enabled": True, "lookback_days": 9,
                                  "max_clusters": 4, "dream_rate": 0.55,
                                  "portrait_enabled": True})
        self.assertEqual(agent_settings.read_growth_values(self.path), values)
        self.assertIn("已写入", group.path_hint.text())

    def test_dirty_file_is_clamped_into_the_widgets(self):
        self.path.write_text(
            "ai:\n  dream_enabled: \"yes\"\n  dream_lookback_days: 99\n"
            "  dream_max_clusters: 0\n  dream_rate: 5\n  portrait_enabled: \"no\"\n",
            encoding="utf-8")
        group = self.new_group()
        self.assertTrue(group.chk_dream.isChecked(), "\"yes\" 要当打开")
        self.assertFalse(group.chk_portrait.isChecked(), "\"no\" 要当关闭")
        self.assertEqual(group.sp_lookback.value(), 30, "99 天夹到上限 30")
        self.assertEqual(group.sp_clusters.value(), 1, "0 条夹到下限 1")
        self.assertAlmostEqual(group.sp_rate.value(), 1.0, "5 夹到上限 1.00")

    def test_group_save_keeps_the_tool_keys(self):
        _write_cfg(self.path, {"ai": {"agent_tools": True, "agent_tools_max_rounds": 4,
                                      "agent_tools_timeout": 45}})
        group = self.new_group()
        group.chk_dream.setChecked(True)
        self.assertTrue(group.save()["ok"])
        disk = _read_cfg(self.path)["ai"]
        self.assertIs(disk["agent_tools"], True)
        self.assertEqual(disk["agent_tools_max_rounds"], 4)
        self.assertEqual(disk["agent_tools_timeout"], 45)

    def test_tools_group_ignores_the_growth_keys(self):
        """工具环区块自己的 values()/save() 里不许出现做梦五键（否则保存会互相打回默认）。"""
        _write_cfg(self.path, {"ai": {"dream_enabled": True, "dream_portrait_x": 1}})
        group = agent_settings.AgentToolsGroup(config_path=self.path)
        self.addCleanup(group._pending.stop)
        self.addCleanup(group.deleteLater)
        self.assertEqual(sorted(group.values()), ["enabled", "max_rounds", "timeout"])
        group.chk_enabled.setChecked(True)
        self.assertTrue(group.save()["ok"])
        disk = _read_cfg(self.path)["ai"]
        self.assertIs(disk["dream_enabled"], True)
        self.assertEqual(disk["dream_portrait_x"], 1)

    def test_settings_tab_mounts_both_groups_on_one_page(self):
        tab = self.gui.SettingsTab()
        self.addCleanup(tab.deleteLater)
        self.assertTrue(hasattr(tab, "agent_growth"), "设置页要挂上做梦与自画像区块")
        self.assertIsNot(tab.agent_growth, tab.agent_tools)
        # 同一个父控件、同一个布局，紧跟在工具环区块后面（不新建 tab、不调换顺序）
        self.assertIs(tab.agent_growth.parent(), tab.agent_tools.parent())
        layout = tab.agent_tools.parent().layout()
        self.assertEqual(layout.indexOf(tab.agent_growth),
                         layout.indexOf(tab.agent_tools) + 1)

    def test_settings_tab_collect_persists_the_growth_keys(self):
        tab = self.gui.SettingsTab()
        self.addCleanup(tab.deleteLater)
        _write_cfg(self.path, {"ai": {"api_key": "sk"}})
        tab.agent_growth.set_config_path(self.path)
        tab.agent_growth.chk_dream.setChecked(True)
        tab.agent_growth.sp_lookback.setValue(6)
        tab.collect()
        self.assertEqual(agent_settings.read_growth_values(self.path),
                         tab.agent_growth.values())
        self.assertTrue(_read_cfg(self.path)["ai"]["dream_enabled"])

    def test_real_config_file_is_untouched_by_these_tests(self):
        if not self._real_cfg.exists():
            self.skipTest("仓库里没有 config.qq.yaml（临时工作区），跳过指纹比对")
        self.assertEqual(hashlib.sha256(self._real_cfg.read_bytes()).hexdigest(),
                         self._real_digest, "测试不许写仓库里的真配置")


if __name__ == "__main__":
    unittest.main()
