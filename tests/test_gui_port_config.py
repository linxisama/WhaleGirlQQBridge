"""控制台端口可配置：--port > WHALEGIRL_PORT > config.qq.yaml 的 qq.console_port > 27499。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PyQt6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])

import qq_gui  # noqa: E402


class PortResolutionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name) / "config.qq.yaml"

    def write_config(self, port, section="qq"):
        self.config.write_text("%s:\n  console_port: %s\n" % (section, port), encoding="utf-8")

    def resolve(self, argv=(), environ=None, config=None):
        return qq_gui.resolve_port(argv=list(argv),
                                   environ=dict(environ or {}),
                                   config_path=self.config if config is None else config)

    def test_argv_beats_env_and_config(self):
        self.write_config(27603)
        port = self.resolve(["--port", "27601"], {"WHALEGIRL_PORT": "27602"})
        self.assertEqual(port, 27601)

    def test_argv_equals_form_is_accepted(self):
        self.write_config(27603)
        port = self.resolve(["--port=27604"], {"WHALEGIRL_PORT": "27605"})
        self.assertEqual(port, 27604)

    def test_env_beats_config(self):
        self.write_config(27606)
        self.assertEqual(self.resolve([], {"WHALEGIRL_PORT": "27605"}), 27605)

    def test_config_used_when_no_argv_no_env(self):
        self.write_config(27607)
        self.assertEqual(self.resolve(), 27607)

    def test_fallback_is_27499(self):
        self.config.write_text("ai:\n  provider: x\n", encoding="utf-8")
        self.assertEqual(self.resolve(), 27499)

    def test_console_port_must_live_under_qq_section(self):
        self.write_config(27609, section="top_level")
        self.assertEqual(self.resolve(), 27499)

    def test_invalid_env_is_ignored_with_one_log_line(self):
        self.write_config(27608)
        with self.assertLogs("qq_gui", level="WARNING") as captured:
            port = self.resolve([], {"WHALEGIRL_PORT": "abc"})
        self.assertEqual(port, 27608)
        self.assertEqual(len(captured.output), 1)
        self.assertIn("WHALEGIRL_PORT", captured.output[0])

    def test_out_of_range_value_falls_back_to_default(self):
        with self.assertLogs("qq_gui", level="WARNING"):
            port = self.resolve([], {"WHALEGIRL_PORT": "70000"})
        self.assertEqual(port, 27499)

    def test_non_numeric_and_zero_fall_back_to_default(self):
        with self.assertLogs("qq_gui", level="WARNING"):
            self.assertEqual(self.resolve(["--port", "0"]), 27499)

    def test_module_port_and_base_url_agree(self):
        self.assertEqual(qq_gui.BASE_URL, "http://127.0.0.1:%d" % qq_gui.PORT)
        self.assertEqual(qq_gui.PORT,
                         qq_gui.resolve_port(argv=[], environ={}, config_path=self.config))

    def test_backend_argv_carries_resolved_port(self):
        port = self.resolve([], {"WHALEGIRL_PORT": "27511"})
        argv = qq_gui.backend_argv(port)
        self.assertEqual(argv[-4:], ["--host", "127.0.0.1", "--port", "27511"])
        self.assertIn("src.qq_app:create_app", argv)
        self.assertEqual(qq_gui.backend_argv(qq_gui.PORT)[-1], str(qq_gui.PORT))

    def test_no_hardcoded_port_left_in_gui_source(self):
        source = Path(qq_gui.__file__).read_text(encoding="utf-8")
        for literal in ('"--port", "27499"', "port=27499", "127.0.0.1:27499"):
            self.assertNotIn(literal, source)


if __name__ == "__main__":
    unittest.main()
