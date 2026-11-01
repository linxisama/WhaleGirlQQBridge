# -*- coding: utf-8 -*-
"""隔离通道（.wsb）单测：XML 转义、幂等写入、manifest、就绪标记、启动四态。

全部只碰临时目录与假 launcher，不真的启动 Windows Sandbox。
"""
import json
import os
import tempfile
import types
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from unittest import mock

from src import windows_sandbox as ws


def parse(text):
    return ElementTree.fromstring(text)


def text_of(root, path):
    node = root.find(path)
    return "" if node is None else (node.text or "")


class ChannelTestBase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.tmp = Path(self._temp.name)
        self.host = self.tmp / "工作 目录 & 数据"
        self.host.mkdir()
        self.channels = self.tmp / "channels"

    def config(self, **overrides):
        return ws.default_config(self.host, **overrides)


class WsbContentTests(ChannelTestBase):
    def test_structure_and_defaults(self):
        root = parse(ws.build_wsb(self.config()))
        self.assertEqual(root.tag, "Configuration")
        self.assertEqual(text_of(root, "./MappedFolders/MappedFolder/HostFolder"),
                         os.path.abspath(str(self.host)))
        self.assertEqual(text_of(root, "./MappedFolders/MappedFolder/SandboxFolder"),
                         ws.DEFAULT_SANDBOX_FOLDER)
        self.assertEqual(text_of(root, "./MappedFolders/MappedFolder/ReadOnly"), "false")
        self.assertEqual(text_of(root, "./Networking"), "Disable")
        self.assertEqual(text_of(root, "./ClipboardRedirection"), "Disable")
        self.assertIsNone(root.find("./MemoryInMB"), "memory_mb=0 时不应写 MemoryInMB")

    def test_networking_and_clipboard_can_be_enabled(self):
        root = parse(ws.build_wsb(self.config(networking=True, clipboard=True)))
        self.assertEqual(text_of(root, "./Networking"), "Enable")
        self.assertEqual(text_of(root, "./ClipboardRedirection"), "Enable")

    def test_read_only_and_memory(self):
        root = parse(ws.build_wsb(self.config(read_only=True, memory_mb=4096)))
        self.assertEqual(text_of(root, "./MappedFolders/MappedFolder/ReadOnly"), "true")
        self.assertEqual(text_of(root, "./MemoryInMB"), "4096")

    def test_default_logon_command_writes_ready_marker(self):
        root = parse(ws.build_wsb(self.config()))
        command = text_of(root, "./LogonCommand/Command")
        self.assertIn("WhaleGirl sandbox ready", command)
        self.assertTrue(command.endswith(ws.DEFAULT_SANDBOX_FOLDER + "\\" + ws.DEFAULT_READY_MARKER),
                        command)

    def test_host_path_is_xml_escaped(self):
        tricky = 'D:\\工作 & <测试>"引号"'
        text = ws.build_wsb(ws.default_config(tricky))
        for raw in ("<测试>", '"引号"', " & "):
            self.assertNotIn(raw, text, "宿主路径必须转义，不能原样落进 XML")
        self.assertIn("&amp;", text)
        self.assertIn("&lt;", text)
        self.assertIn("&quot;", text)
        self.assertEqual(text_of(parse(text), "./MappedFolders/MappedFolder/HostFolder"),
                         os.path.abspath(tricky), "转义后解析回来必须还是原路径")

    def test_logon_command_is_escaped_and_round_trips(self):
        command = 'cmd.exe /c echo ready>marker.txt & copy a b && echo "done"'
        root = parse(ws.build_wsb(self.config(logon_command=command)))
        self.assertEqual(text_of(root, "./LogonCommand/Command"), command)

    def test_empty_host_folder_rejected(self):
        with self.assertRaises(ValueError):
            ws.build_wsb(ws.default_config(""))

    def test_sandbox_folder_must_be_absolute(self):
        with self.assertRaises(ValueError):
            ws.build_wsb(self.config(sandbox_folder="WhaleGirl"))

    def test_ready_marker_must_be_file_name(self):
        for marker in ("sub/marker.txt", "sub\\marker.txt", ""):
            with self.assertRaises(ValueError):
                ws.build_wsb(self.config(ready_marker=marker))

    def test_trailing_separator_is_stripped(self):
        root = parse(ws.build_wsb(self.config(sandbox_folder="C:\\WhaleGirl\\")))
        self.assertEqual(text_of(root, "./MappedFolders/MappedFolder/SandboxFolder"), "C:\\WhaleGirl")

    def test_channel_dir_rejects_path_traversal(self):
        for name in ("a/b", "a\\b", ".."):
            with self.assertRaises(ValueError):
                ws.channel_dir(self.channels, name)


class CreateChannelTests(ChannelTestBase):
    def test_first_create_writes_both_files(self):
        channel = ws.create_isolation_channel(self.config(), root=self.channels)
        self.assertTrue(channel.created)
        self.assertTrue(channel.changed)
        self.assertTrue(Path(channel.wsb_path).is_file())
        self.assertTrue(Path(channel.manifest_path).is_file())
        self.assertIsNone(channel.launched)

    def test_create_is_idempotent(self):
        first = ws.create_isolation_channel(self.config(), root=self.channels)
        wsb_mtime = Path(first.wsb_path).stat().st_mtime_ns
        manifest_mtime = Path(first.manifest_path).stat().st_mtime_ns
        second = ws.create_isolation_channel(self.config(), root=self.channels)
        self.assertFalse(second.created)
        self.assertFalse(second.changed)
        self.assertEqual(Path(second.wsb_path).stat().st_mtime_ns, wsb_mtime, "内容没变不该重写 .wsb")
        self.assertEqual(Path(second.manifest_path).stat().st_mtime_ns, manifest_mtime)
        self.assertTrue(any("未重写" in note for note in second.notes))

    def test_changed_config_rewrites(self):
        ws.create_isolation_channel(self.config(), root=self.channels)
        updated = ws.create_isolation_channel(self.config(networking=True), root=self.channels)
        self.assertTrue(updated.changed)
        self.assertIn("<Networking>Enable</Networking>", Path(updated.wsb_path).read_text("utf-8"))

    def test_manifest_is_consistent(self):
        channel = ws.create_isolation_channel(self.config(), root=self.channels)
        payload = json.loads(Path(channel.manifest_path).read_text("utf-8"))
        self.assertEqual(payload["schema"], ws.SCHEMA_VERSION)
        self.assertEqual(payload["host_folder"], os.path.abspath(str(self.host)))
        self.assertEqual(payload["content_sha256"], channel.content_sha256)
        self.assertEqual(payload["ready_marker"], ws.DEFAULT_READY_MARKER)
        self.assertTrue(payload["created_at"])
        self.assertTrue(payload["updated_at"])

    def test_host_folder_is_created_when_missing(self):
        missing = self.tmp / "还没有的目录"
        channel = ws.create_isolation_channel(ws.default_config(missing), root=self.channels)
        self.assertTrue(missing.is_dir())
        self.assertTrue(any("已创建" in note for note in channel.notes))

    def test_ampersand_path_round_trips_through_file(self):
        channel = ws.create_isolation_channel(self.config(), root=self.channels)
        folder = text_of(parse(Path(channel.wsb_path).read_text("utf-8")),
                         "./MappedFolders/MappedFolder/HostFolder")
        self.assertEqual(folder, os.path.abspath(str(self.host)))
        self.assertIn("&", folder)

    def test_custom_channel_name_lands_in_own_folder(self):
        channel = ws.create_isolation_channel(self.config(), root=self.channels, name="alpha")
        self.assertEqual(Path(channel.wsb_path).parent.name, "alpha")


class ChannelStatusTests(ChannelTestBase):
    def test_status_before_create(self):
        status = ws.read_channel_status(root=self.channels)
        self.assertFalse(status.exists)
        self.assertFalse(status.ready)
        self.assertEqual(status.host_folder, "")

    def test_ready_flips_when_marker_appears(self):
        channel = ws.create_isolation_channel(self.config(), root=self.channels)
        before = ws.read_channel_status(root=self.channels)
        self.assertTrue(before.exists)
        self.assertFalse(before.ready)
        self.assertEqual(before.host_folder, channel.host_folder)

        Path(channel.ready_marker_path).write_text("WhaleGirl sandbox ready\n", encoding="utf-8")
        after = ws.read_channel_status(root=self.channels)
        self.assertTrue(after.ready)
        self.assertIn("WhaleGirl sandbox ready", after.marker_text)
        self.assertEqual(after.error, "")

    def test_manifest_loss_falls_back_to_wsb(self):
        channel = ws.create_isolation_channel(self.config(), root=self.channels)
        Path(channel.manifest_path).unlink()
        status = ws.read_channel_status(root=self.channels)
        self.assertTrue(status.exists)
        self.assertEqual(status.host_folder, channel.host_folder)
        self.assertTrue(status.error)


class LaunchTests(ChannelTestBase):
    def make_wsb(self):
        channel = ws.create_isolation_channel(self.config(), root=self.channels)
        return Path(channel.wsb_path)

    def make_exe(self):
        exe = self.tmp / "WindowsSandbox.exe"
        exe.write_bytes(b"stub")
        return exe

    def test_plan_does_not_launch(self):
        launcher = mock.Mock(side_effect=AssertionError("apply=False 不该启动进程"))
        result = ws.launch_sandbox(self.make_wsb(), apply=False, launcher=launcher,
                                   exe_path=self.make_exe())
        self.assertEqual(result.status, "planned")
        self.assertTrue(result.ok)
        self.assertIn("WindowsSandbox.exe", result.command)
        launcher.assert_not_called()

    def test_unavailable_when_exe_missing(self):
        result = ws.launch_sandbox(self.make_wsb(), exe_path=self.tmp / "nope.exe")
        self.assertEqual(result.status, "unavailable")
        self.assertFalse(result.ok)

    def test_missing_config(self):
        result = ws.launch_sandbox(self.tmp / "nope.wsb", exe_path=self.make_exe())
        self.assertEqual(result.status, "missing_config")
        self.assertFalse(result.ok)

    def test_launched_returns_pid(self):
        launcher = mock.Mock(return_value=4321)
        result = ws.launch_sandbox(self.make_wsb(), launcher=launcher, exe_path=self.make_exe())
        self.assertEqual(result.status, "launched")
        self.assertTrue(result.ok)
        self.assertEqual(result.pid, 4321)
        launcher.assert_called_once()

    def test_oserror_is_reported(self):
        launcher = mock.Mock(side_effect=OSError("拒绝访问"))
        result = ws.launch_sandbox(self.make_wsb(), launcher=launcher, exe_path=self.make_exe())
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.ok)
        self.assertIn("拒绝访问", result.detail)

    def test_default_spawn_uses_detached_process(self):
        wsb = self.make_wsb()
        exe = self.make_exe()
        seen = {}

        def fake_popen(argv, **kwargs):
            seen["argv"] = list(argv)
            seen["kwargs"] = kwargs
            return types.SimpleNamespace(pid=987)

        fake = types.SimpleNamespace(Popen=fake_popen, DETACHED_PROCESS=0x00000008,
                                     CREATE_NEW_PROCESS_GROUP=0x00000200)
        with mock.patch.object(ws, "subprocess", fake), \
                mock.patch.object(ws, "_sandbox_exe_path", lambda: exe):
            result = ws.launch_sandbox(wsb)
        self.assertEqual(result.status, "launched")
        self.assertEqual(seen["argv"], [str(exe), str(wsb)])
        self.assertEqual(seen["kwargs"]["creationflags"], 0x00000008 | 0x00000200)
        self.assertTrue(seen["kwargs"]["close_fds"])

    def test_create_with_launch_records_result(self):
        channel = ws.create_isolation_channel(self.config(), root=self.channels,
                                              launch=True, apply=False,
                                              launch_exe=self.make_exe())
        self.assertIsNotNone(channel.launched)
        self.assertEqual(channel.launched.status, "planned")


if __name__ == "__main__":
    unittest.main()
