"""GitHub 自动更新的补充回归：进度回调、sha256 校验、Release 解析、批处理脚本安全。"""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import app_updater

EXE_NAME = "WhaleGirlQQBridge-Setup-1.0.1.exe"
EXE_URL = "https://github.com/Owner/Repo/releases/download/v1.0.1/" + EXE_NAME
EXE_BYTES = b"MZ" + b"\x00" * 24 + b"setup-payload"
EXE_SHA = hashlib.sha256(EXE_BYTES).hexdigest()
ZERO_SHA = "0" * 64


class _FakeResponse:
    def __init__(self, status_code=200, headers=None, text="", payload=None, url=""):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.text = text
        self.url = url
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %s" % self.status_code)


class _StreamResponse(_FakeResponse):
    def __init__(self, chunks, **kwargs):
        super().__init__(**kwargs)
        self.chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        for chunk in self.chunks:
            yield chunk


class _FakeClient:
    """同时支持 check_update 的 get() 和 download_update 的 stream()。"""

    def __init__(self, responses):
        self.responses = dict(responses)
        self.calls = []
        self.stream_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses[url]

    def stream(self, method, url):
        self.stream_calls.append((method, url))
        return self.responses[url]


def _info(digest="", assets=None, name=EXE_NAME, url=EXE_URL, tag="v1.0.1"):
    return {
        "ok": True,
        "repo": "Owner/Repo",
        "tag": tag,
        "latest": tag,
        "asset": {"name": name, "url": url, "size": len(EXE_BYTES), "digest": digest},
        "assets": list(assets or []),
    }


def _download(info, client, dest_dir, **kwargs):
    with mock.patch.object(app_updater.httpx, "Client", return_value=client):
        return app_updater.download_update(info, dest_dir, **kwargs)


def _release_payload(tag="v1.2.0", body="", assets=None):
    return {
        "tag_name": tag,
        "name": "WhaleGirl QQ 控制台",
        "html_url": "https://github.com/Owner/Repo/releases/tag/" + tag,
        "published_at": "2026-10-31T08:12:18Z",
        "body": body,
        "assets": assets if assets is not None else [{
            "name": EXE_NAME, "size": len(EXE_BYTES),
            "browser_download_url": EXE_URL, "digest": "sha256:" + ZERO_SHA,
        }],
    }


class RepoUrlTests(unittest.TestCase):
    def test_accepts_full_repository_and_release_urls(self):
        cases = {
            "https://github.com/Owner/Repo": "Owner/Repo",
            "https://github.com/Owner/Repo/": "Owner/Repo",
            "  https://www.github.com/Owner/Repo/releases/tag/v1.2.3  ": "Owner/Repo",
            "git@github.com:Owner/Repo.git": "Owner/Repo",
            "ssh://git@github.com/Owner/Repo": "Owner/Repo",
            "github.com/Owner/Repo.git": "Owner/Repo",
        }
        for raw, expected in cases.items():
            self.assertEqual(app_updater.normalize_repo(raw), expected, raw)

    def test_still_rejects_non_github_input(self):
        for value in ("https://gitlab.com/Owner/Repo", "Owner", "Owner/", "/Repo"):
            with self.assertRaises(ValueError):
                app_updater.normalize_repo(value)


class CheckUpdateTests(unittest.TestCase):
    API = "https://api.github.com/repos/Owner/Repo/releases/latest"

    def _check(self, payload, repo="Owner/Repo", current="5.2.84"):
        client = _FakeClient({self.API: _FakeResponse(payload=payload)})
        with mock.patch.object(app_updater.httpx, "Client", return_value=client):
            return app_updater.check_update(repo, current), client

    def test_no_update_when_latest_is_current(self):
        info, _ = self._check(_release_payload(tag="v5.2.84"))
        self.assertFalse(info["hasUpdate"])
        self.assertEqual(info["latest"], "v5.2.84")

    def test_update_available_parses_full_notes_and_assets(self):
        body = "更新内容\n" + "细节修复 " * 1200
        self.assertGreater(len(body), 4000)
        payload = _release_payload(tag="v5.3.0", body=body, assets=[
            {"name": EXE_NAME, "size": len(EXE_BYTES),
             "browser_download_url": EXE_URL, "digest": "sha256:" + EXE_SHA},
            {"name": EXE_NAME + ".sha256", "size": 80,
             "browser_download_url": "https://github.com/Owner/Repo/releases/download/v5.3.0/x.sha256"},
            {"name": "notes.txt", "size": 3,
             "browser_download_url": "https://evil.example/notes.txt"},
        ])
        info, _ = self._check(payload)
        self.assertTrue(info["hasUpdate"])
        self.assertEqual(info["latest"], "v5.3.0")
        self.assertEqual(info["tag"], "v5.3.0")
        self.assertEqual(info["notes"], body)
        self.assertFalse(info["notesTruncated"])
        self.assertEqual(info["publishedAt"], "2026-10-31T08:12:18Z")
        self.assertEqual(info["htmlUrl"],
                         "https://github.com/Owner/Repo/releases/tag/v5.3.0")
        self.assertEqual(info["releaseUrl"], info["htmlUrl"])
        self.assertEqual(info["assets"][0]["digest"], "sha256:" + EXE_SHA)
        # 非 GitHub 主机的资产不会进入 assets 列表
        self.assertEqual([a["name"] for a in info["assets"]],
                         [EXE_NAME, EXE_NAME + ".sha256"])
        self.assertEqual(info["asset"]["name"], EXE_NAME)

    def test_full_repository_url_is_normalized_before_request(self):
        info, client = self._check(_release_payload(), repo="https://github.com/Owner/Repo/releases")
        self.assertEqual(client.calls[0][0], self.API)
        self.assertEqual(info["repo"], "Owner/Repo")

    def test_missing_release_raises_value_error(self):
        client = _FakeClient({self.API: _FakeResponse(status_code=404)})
        with mock.patch.object(app_updater.httpx, "Client", return_value=client):
            with self.assertRaises(ValueError):
                app_updater.check_update("Owner/Repo", "5.2.84")


class DigestParsingTests(unittest.TestCase):
    def test_sha256_from_digest(self):
        self.assertEqual(app_updater.sha256_from_digest("sha256:" + ZERO_SHA), ZERO_SHA)
        self.assertEqual(app_updater.sha256_from_digest("SHA256:" + ZERO_SHA.upper()), ZERO_SHA)
        self.assertEqual(app_updater.sha256_from_digest(""), "")
        self.assertEqual(app_updater.sha256_from_digest(None), "")
        for bad in ("sha256:abc", "md5:" + ZERO_SHA, "not-a-digest"):
            with self.assertRaises(ValueError):
                app_updater.sha256_from_digest(bad)

    def test_parse_sha256_text(self):
        self.assertEqual(app_updater.parse_sha256_text(EXE_SHA + "  setup.exe\n"), EXE_SHA)
        self.assertEqual(app_updater.parse_sha256_text("SHA256 (setup.exe) = "
                                                       + EXE_SHA.upper()), EXE_SHA)
        self.assertEqual(app_updater.parse_sha256_text("no hash here"), "")


class DownloadProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _client(self, chunks, headers=None):
        return _FakeClient({EXE_URL: _StreamResponse(
            chunks, url=EXE_URL,
            headers=headers if headers is not None
            else {"content-length": str(len(EXE_BYTES))})})

    def test_progress_callback_reports_bytes(self):
        records = []
        info = _info(digest="sha256:" + EXE_SHA)
        result = _download(info, self._client([EXE_BYTES[:8], EXE_BYTES[8:]]), self.root,
                           progress=lambda done, total: records.append((done, total)))
        self.assertEqual(records[0], (0, 0))
        self.assertIn((8, len(EXE_BYTES)), records)
        self.assertEqual(records[-1], (len(EXE_BYTES), len(EXE_BYTES)))
        self.assertEqual(result["bytes"], len(EXE_BYTES))
        self.assertTrue(result["verified"])

    def test_progress_callback_errors_do_not_break_download(self):
        def boom(done, total):
            raise RuntimeError("回调炸了")
        result = _download(_info(digest="sha256:" + EXE_SHA),
                           self._client([EXE_BYTES]), self.root, progress=boom)
        self.assertEqual(result["sha256"], EXE_SHA)
        self.assertTrue(Path(result["path"]).is_file())

    def test_update_progress_snapshot_is_pollable(self):
        progress = app_updater.UpdateProgress()
        info = _info(digest="sha256:" + EXE_SHA)
        result = _download(info, self._client([EXE_BYTES]), self.root, progress=progress)
        snapshot = progress.snapshot()
        self.assertEqual(snapshot["state"], "done")
        self.assertEqual(snapshot["done"], len(EXE_BYTES))
        self.assertEqual(snapshot["total"], len(EXE_BYTES))
        self.assertEqual(snapshot["percent"], 100.0)
        self.assertTrue(snapshot["verified"])
        self.assertEqual(snapshot["verifyStatus"], app_updater.VERIFY_DIGEST)
        self.assertEqual(snapshot["sha256"], EXE_SHA)
        self.assertEqual(snapshot["path"], result["path"])

    def test_snapshot_before_any_download_is_idle(self):
        snapshot = app_updater.UpdateProgress().snapshot()
        self.assertEqual(snapshot["state"], "idle")
        self.assertEqual(snapshot["done"], 0)
        self.assertIsNone(snapshot["percent"])

    def test_legacy_call_without_progress_still_works(self):
        result = _download(_info(), self._client([EXE_BYTES]), self.root)
        self.assertEqual(result["sha256"], EXE_SHA)
        self.assertFalse(result["verified"])
        self.assertEqual(result["verifyStatus"], app_updater.VERIFY_UNKNOWN)
        self.assertTrue(Path(result["path"]).is_file())


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _client(self, chunks=None, extra=None):
        responses = {EXE_URL: _StreamResponse(chunks or [EXE_BYTES], url=EXE_URL,
                                              headers={"content-length": str(len(EXE_BYTES))})}
        responses.update(extra or {})
        return _FakeClient(responses)

    def test_digest_match_marks_verified(self):
        result = _download(_info(digest="sha256:" + EXE_SHA), self._client(), self.root)
        self.assertTrue(result["verified"])
        self.assertEqual(result["verifyStatus"], app_updater.VERIFY_DIGEST)
        self.assertEqual(result["expectedSha256"], EXE_SHA)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         [Path(result["path"]).name])

    def test_digest_mismatch_raises_and_deletes_files(self):
        progress = app_updater.UpdateProgress()
        with self.assertRaises(ValueError) as caught:
            _download(_info(digest="sha256:" + ZERO_SHA), self._client(), self.root,
                      progress=progress)
        self.assertIn("sha256", str(caught.exception))
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(progress.snapshot()["state"], "error")

    def test_checksum_asset_match_marks_verified(self):
        checksum_url = "https://github.com/Owner/Repo/releases/download/v1.0.1/x.sha256"
        assets = [{"name": EXE_NAME + ".sha256", "url": checksum_url, "size": 90}]
        client = self._client(extra={checksum_url: _FakeResponse(
            text=EXE_SHA + "  " + EXE_NAME + "\n")})
        result = _download(_info(assets=assets), client, self.root)
        self.assertTrue(result["verified"])
        self.assertEqual(result["verifyStatus"], app_updater.VERIFY_CHECKSUM_ASSET)
        self.assertEqual(result["expectedSha256"], EXE_SHA)

    def test_checksum_asset_mismatch_is_rejected(self):
        checksum_url = "https://github.com/Owner/Repo/releases/download/v1.0.1/x.sha256"
        assets = [{"name": EXE_NAME + ".sha256", "url": checksum_url, "size": 90}]
        client = self._client(extra={checksum_url: _FakeResponse(text=ZERO_SHA + "\n")})
        with self.assertRaises(ValueError) as caught:
            _download(_info(assets=assets), client, self.root)
        self.assertIn("sha256", str(caught.exception))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_checksum_asset_without_hash_is_rejected(self):
        checksum_url = "https://github.com/Owner/Repo/releases/download/v1.0.1/x.sha256"
        assets = [{"name": EXE_NAME + ".sha256", "url": checksum_url, "size": 12}]
        client = self._client(extra={checksum_url: _FakeResponse(text="not a checksum")})
        with self.assertRaises(ValueError):
            _download(_info(assets=assets), client, self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unknown_digest_algorithm_is_rejected(self):
        with self.assertRaises(ValueError):
            _download(_info(digest="md5:" + ZERO_SHA), self._client(), self.root)

    def test_empty_download_is_rejected(self):
        client = _FakeClient({EXE_URL: _StreamResponse([], url=EXE_URL, headers={})})
        with self.assertRaises(ValueError) as caught:
            _download(_info(), client, self.root)
        self.assertIn("空", str(caught.exception))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_oversized_installer_is_rejected(self):
        client = _FakeClient({EXE_URL: _StreamResponse(
            [EXE_BYTES], url=EXE_URL,
            headers={"content-length": str(app_updater.MAX_UPDATE_BYTES + 1)})})
        with self.assertRaises(ValueError) as caught:
            _download(_info(), client, self.root)
        self.assertIn("300 MB", str(caught.exception))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_redirect_to_non_github_host_is_rejected(self):
        client = _FakeClient({EXE_URL: _StreamResponse(
            [EXE_BYTES], url="https://evil.example/payload.exe",
            headers={"content-length": str(len(EXE_BYTES))})})
        with self.assertRaises(ValueError) as caught:
            _download(_info(), client, self.root)
        self.assertIn("非 GitHub", str(caught.exception))
        self.assertEqual(list(self.root.iterdir()), [])


class AssetNameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_path_traversal_name_stays_inside_dest_dir(self):
        evil = "..\\..\\evil.exe"
        client = _FakeClient({EXE_URL: _StreamResponse(
            [EXE_BYTES], url=EXE_URL, headers={"content-length": str(len(EXE_BYTES))})})
        result = _download(_info(name=evil), client, self.root)
        target = Path(result["path"]).resolve()
        self.assertEqual(target.parent, self.root.resolve())
        self.assertNotIn("..", result["name"])
        self.assertTrue(target.is_file())

    def test_non_exe_asset_name_is_rejected(self):
        for name in ("update.zip", "whalegirl.7z", "setup.exe.zip"):
            with self.assertRaises(ValueError):
                _download(_info(name=name), _FakeClient({}), self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_asset_is_rejected(self):
        with self.assertRaises(ValueError):
            _download({"ok": True, "asset": None}, _FakeClient({}), self.root)
        with self.assertRaises(ValueError):
            _download(_info(url="https://evil.example/setup.exe"), _FakeClient({}), self.root)


class UpdateScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.setup = self.root / "WhaleGirlQQBridge-Setup-9.9.9.exe"
        self.setup.write_bytes(b"MZ")
        self.app = self.root / "WhaleGirlQQBridge.exe"
        self.app.write_bytes(b"MZ")

    def tearDown(self):
        self.temp.cleanup()

    def test_script_logs_app_dir_and_exit_code(self):
        script = app_updater.build_update_script(self.setup, self.app, 4321)
        raw = script.read_bytes()
        for token in (b"PID=4321", b"/VERYSILENT", b"/SUPPRESSMSGBOXES",
                      b"start \"\" \"%APP%\"", b"ERRORLEVEL", b"exitcode",
                      b"[update] app=", b"[update] dir=", b"[update] setup=",
                      b"update-4321.log", b"WhaleGirlQQBridge-Setup-9.9.9.exe"):
            self.assertIn(token, raw, token)
        # 只记录路径，不复制安装目录
        self.assertFalse((Path(tempfile.gettempdir()) / "WhaleGirlQQBridge-updates"
                          / "WhaleGirlQQBridge").exists())

    def test_wait_loop_is_bounded(self):
        raw = app_updater.build_update_script(self.setup, self.app, 11).read_bytes()
        self.assertIn(b'set "TRIES=0"', raw)
        self.assertIn(b"if %TRIES% GTR 300 goto waittimeout", raw)
        self.assertIn(b"[WARN] waited too long", raw)
        self.assertIn(b":install", raw)
        self.assertIn(b":wait", raw)

    def test_percent_in_install_dir_is_escaped(self):
        script = app_updater.build_update_script(self.setup, self.app, 7,
                                                 install_dir="C:\\100%\\WhaleGirl")
        raw = script.read_bytes()
        self.assertIn(b"DIR=C:\\100%%\\WhaleGirl", raw)

    def test_quote_in_path_is_refused(self):
        self.assertEqual(app_updater._batch_quote("C:\\100%\\x.exe"), "C:\\100%%\\x.exe")
        for bad in ('C:\\a"b.exe', "C:\\a\rb.exe", "C:\\a\nb.exe"):
            with self.assertRaises(ValueError):
                app_updater._batch_quote(bad)
        with self.assertRaises(ValueError):
            app_updater.build_update_script(self.setup, self.app, 1,
                                            install_dir='C:\\a"b')

    def test_pid_cannot_inject_commands(self):
        script = app_updater.build_update_script(self.setup, self.app, "1 & calc")
        raw = script.read_bytes()
        self.assertIn(b"PID=0", raw)
        self.assertNotIn(b"calc", raw)


if __name__ == "__main__":
    unittest.main()
