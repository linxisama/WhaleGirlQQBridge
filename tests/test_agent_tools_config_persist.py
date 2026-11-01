"""界面上单独落盘的 ai.agent_tools* 三键不能被后端整份覆盖写回抹掉。

背景：设置页（src/gui/agent_settings.py）用 ConfigManager 单独写这三个键，
后端内存快照里没有它们；而 src/qq_app.py 的 _persist_config 是把内存快照整份
写回 config.qq.yaml。用户「设置页打开工具环」后再「切人设」或「存违禁词」，
三键就会被静默抹掉，重启后回到默认值 false。_preserve_gui_owned_ai 负责以磁盘为准补回。
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

from src import qq_app


def _bot(path, memory):
    return SimpleNamespace(
        config=SimpleNamespace(config_path=Path(path), config=memory))


class PersistConfigMergeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "config.qq.yaml"

    def _write_disk(self, data):
        self.path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    def _read_disk(self):
        return yaml.safe_load(self.path.read_text(encoding="utf-8"))

    def test_gui_keys_survive_backend_write(self):
        """磁盘上的三键（界面写的）在整份写回后仍然存在。"""
        self._write_disk({"ai": {"api_key": "sk", "agent_tools": True,
                                 "agent_tools_max_rounds": 4, "agent_tools_timeout": 45},
                          "style": {"active": "default"}})
        memory = {"ai": {"api_key": "sk"},
                  "style": {"active": "grumpy", "banned_words": ["坏词"]}}
        qq_app._persist_config(_bot(self.path, memory))
        disk = self._read_disk()
        self.assertIs(disk["ai"]["agent_tools"], True)
        self.assertEqual(disk["ai"]["agent_tools_max_rounds"], 4)
        self.assertEqual(disk["ai"]["agent_tools_timeout"], 45)
        # 后端这次写的字段照样生效，不被合并逻辑吃掉
        self.assertEqual(disk["style"]["active"], "grumpy")
        self.assertEqual(disk["style"]["banned_words"], ["坏词"])

    def test_disk_value_wins_over_stale_memory(self):
        """内存里还是启动时的旧值（甚至是 False），以磁盘上界面刚写的为准。"""
        self._write_disk({"ai": {"agent_tools": True, "agent_tools_max_rounds": 5}})
        memory = {"ai": {"agent_tools": False, "agent_tools_max_rounds": 3}}
        qq_app._persist_config(_bot(self.path, memory))
        disk = self._read_disk()
        self.assertIs(disk["ai"]["agent_tools"], True)
        self.assertEqual(disk["ai"]["agent_tools_max_rounds"], 5)
        # 磁盘没有 timeout 键 → 不凭空捏一个出来
        self.assertNotIn("agent_tools_timeout", disk["ai"])
        self.assertNotIn("agent_tools_timeout", memory["ai"])

    def test_corrupt_disk_still_writes_memory(self):
        """磁盘 YAML 坏掉时合并跳过，写回照做（不能因为读不了就丢这次修改）。"""
        self.path.write_text("ai: [unclosed", encoding="utf-8")
        memory = {"ai": {"api_key": "sk"}}
        qq_app._persist_config(_bot(self.path, memory))
        self.assertEqual(self._read_disk(), {"ai": {"api_key": "sk"}})

    def test_missing_file_does_not_crash(self):
        """首次启动还没有配置文件：不炸，正常写出内存快照。"""
        memory = {"ai": {"agent_tools": True}}
        qq_app._persist_config(_bot(self.path, memory))
        self.assertEqual(self._read_disk(), {"ai": {"agent_tools": True}})

    def test_no_phantom_ai_section(self):
        """磁盘没有 ai 段时不能凭空造一个空的 ai: {}。"""
        self._write_disk({"style": {"active": "default"}})
        memory = {"style": {"active": "grumpy"}}
        qq_app._persist_config(_bot(self.path, memory))
        self.assertEqual(self._read_disk(), {"style": {"active": "grumpy"}})

    def test_roundtrip_with_gui_writer(self):
        """真实链路：界面写三键 → 后端切人设写回 → 三键仍在，且界面读回一致。"""
        from src.gui import agent_settings

        self._write_disk({"ai": {"api_key": "sk"}})
        saved = agent_settings.write_values(
            {"enabled": True, "max_rounds": 4, "timeout": 45}, self.path)
        self.assertTrue(saved.get("ok"), saved)

        bot = _bot(self.path, {"ai": {"api_key": "sk"},
                               "style": {"active": "grumpy"}})
        bot.config.set = lambda key, value: None
        qq_app._persist_config(bot)

        self.assertEqual(agent_settings.read_values(self.path),
                         {"enabled": True, "max_rounds": 4, "timeout": 45})
        self.assertEqual(self._read_disk()["style"]["active"], "grumpy")


if __name__ == "__main__":
    unittest.main()
