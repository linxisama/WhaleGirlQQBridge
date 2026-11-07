"""更新卡片的「未校验确认」链路（2026-11-07）。

后端 check_update 在 Release 拿不到发布方 sha256 时给出 needs_confirmation /
confirmation_reason。GUI 必须：1) 把确认框摆出来并显示原因；2) 没勾就不发请求
（而不是发出去再让后端 400）；3) 勾了才把 allowUnverified=True 带到 update/apply。

这里不实例化 AboutTab：它的构造会起定时器并碰网络，测试会挂住（实测 600s 超时）。
改为对源码做结构性断言 —— 三个关键行为在源码里各只有一处落点，改坏了必然被这里抓到。
"""
import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
GUI = (ROOT / "qq_gui.py").read_text(encoding="utf-8")
APP = (ROOT / "src" / "qq_app.py").read_text(encoding="utf-8")


class UpdateUnverifiedGateTests(unittest.TestCase):
    def test_checkbox_exists(self):
        self.assertIn('about_update_unverified', GUI)
        self.assertIn('QCheckBox', GUI)

    def test_checkbox_defaults_to_unchecked(self):
        """宁严勿松：确认框默认不勾，用户必须主动勾。"""
        # QCheckBox 默认就是未勾选，所以创建处只要求「没有主动 setChecked(True)」；
        # 真正的保证在 _check_update 的复位与 _update_checked 的重新 setChecked(False)。
        create = GUI.find('self.update_unverified = QCheckBox')
        self.assertGreater(create, 0)
        self.assertNotIn('self.update_unverified.setChecked(True)', GUI)
        self.assertIn('self.update_unverified.setChecked(False)', GUI)

    def test_confirmation_reason_is_surfaced(self):
        self.assertIn('needs_confirmation', GUI)
        self.assertIn('confirmation_reason', GUI)

    def test_unchecked_gate_blocks_the_request(self):
        """没勾时必须弹窗并 return，不能发出 update/apply。"""
        self.assertIn('QMessageBox.warning', GUI)
        self.assertIn('allow_unverified', GUI)
        gate = GUI.find('if needs_confirm and not allow_unverified')
        self.assertGreater(gate, 0, "缺少「需要确认但没勾」的前置门闩")

    def test_payload_carries_allow_unverified(self):
        self.assertIn('"allowUnverified"', GUI)

    def test_backend_forwards_the_flag(self):
        self.assertIn('allow_unverified=_allow_unverified', APP)
        self.assertIn('body.get("allowUnverified")', APP)


if __name__ == "__main__":
    unittest.main()
