"""自动更新适配回归：未校验必须确认、预发布排序、日志五要素、失败提示可操作。"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from src import app_updater

EXE_NAME = "WhaleGirlQQBridge-Setup-1.0.1.exe"
EXE_URL = "https://github.com/Owner/Repo/releases/download/v1.0.1/" + EXE_NAME
EXE_BYTES = b"MZ" + b"\x00" * 8 + b"payload"
EXE_SHA = hashlib.sha256(EXE_BYTES).hexdigest()
API = "https://api.github.com/repos/Owner/Repo/releases/latest"
_BAD_JSON = object()

class _Response:
    """假的 httpx 响应：既能当上下文管理器，也能 iter_bytes 出整包。"""
    __enter__ = lambda self: self
    __exit__ = lambda self, *exc: False

    def __init__(self, status_code=200, headers=None, payload=None, url=EXE_URL):
        self.status_code, self.headers, self.url = status_code, dict(headers or {}), url
        self._payload = payload

    def json(self):
        if self._payload is _BAD_JSON:
            raise json.JSONDecodeError("Expecting value", "", 0)
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("HTTP %s" % self.status_code, request=httpx.Request(
                "GET", self.url), response=httpx.Response(self.status_code))

    def iter_bytes(self):
        yield EXE_BYTES

class _Client:
    """假 httpx.Client：一次只用一种故障，get_error 管检查、stream_error 管下载。"""
    __enter__ = lambda self: self
    __exit__ = lambda self, *exc: False

    def __init__(self, responses=None, get_error=None, stream_error=None):
        self.responses, self.get_error, self.stream_error = dict(responses or {}), get_error, stream_error

    def get(self, url, **kwargs):
        if self.get_error is not None:
            raise self.get_error
        return self.responses[url]

    def stream(self, method, url):
        if self.stream_error is not None:
            raise self.stream_error
        return self.responses[url]

def _info(**over):
    """一份带 digest 的正常检查结果，测试里只改需要的字段。"""
    base = {"repo": "Owner/Repo", "current": "5.2.84", "latest": "v5.3.0", "tag": "v5.3.0",
            "asset": {"name": EXE_NAME, "size": len(EXE_BYTES), "url": EXE_URL,
                      "digest": "sha256:" + EXE_SHA}, "assets": [], "needs_confirmation": False}
    base.update(over)
    return base

def _release(assets=None):
    assets = assets if assets is not None else [
        {"name": EXE_NAME, "size": len(EXE_BYTES), "browser_download_url": EXE_URL, "digest": ""}]
    return {"tag_name": "v1.0.1", "body": "notes", "html_url": "", "published_at": "",
            "assets": assets}

def _check(payload=None, client=None):
    client = client or _Client({API: _Response(payload=_release() if payload is None else payload)})
    with mock.patch.object(app_updater.httpx, "Client", return_value=client):
        return app_updater.check_update("Owner/Repo", "0.9.0")

def _download(info, tmp, **kwargs):
    """走真实 download_update，只把 httpx.Client 换成返回整包的假客户端。"""
    client = _Client({EXE_URL: _Response(headers={"content-length": str(len(EXE_BYTES))})})
    with mock.patch.object(app_updater.httpx, "Client", return_value=client):
        return app_updater.download_update(info, tmp, **kwargs)

class UnverifiedNeedsConfirmationTests(unittest.TestCase):
    """要求 1：拿不到任何发布方哈希时不能静默放行。"""

    def test_no_digest_and_no_checksum_asset_requires_confirmation(self):
        info = _check()
        self.assertEqual(info["verify_method"], app_updater.VERIFY_UNKNOWN)
        self.assertTrue(info["needs_confirmation"])
        self.assertIn("Releases", info["confirmation_reason"])

    def test_digest_or_checksum_asset_does_not_require_confirmation(self):
        digest = [{"name": EXE_NAME, "size": 1, "browser_download_url": EXE_URL,
                   "digest": "sha256:" + EXE_SHA}]
        info = _check(_release(assets=digest))
        self.assertFalse(info["needs_confirmation"])
        self.assertEqual(info["confirmation_reason"], "")
        self.assertEqual(info["verify_method"], app_updater.VERIFY_DIGEST)
        checksum = [{"name": EXE_NAME, "size": 1, "browser_download_url": EXE_URL, "digest": ""},
                    {"name": EXE_NAME + ".sha256", "size": 90, "browser_download_url":
                     "https://github.com/Owner/Repo/releases/download/v1/x.sha256"}]
        info = _check(_release(assets=checksum))
        self.assertFalse(info["needs_confirmation"])
        self.assertEqual(info["verify_method"], app_updater.VERIFY_CHECKSUM_ASSET)

    def test_download_refuses_unverified_release_until_confirmed(self):
        info = _check()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            with self.assertRaises(ValueError) as caught:
                _download(info, tmp)
            self.assertIn("Releases", str(caught.exception))
            self.assertEqual(list(Path(tmp).iterdir()), [])
            result = _download(info, tmp, allow_unverified=True)
        self.assertFalse(result["verified"])
        self.assertEqual(result["verifyStatus"], app_updater.VERIFY_UNKNOWN)

class PreReleaseVersionTests(unittest.TestCase):
    """要求 2：预发布版低于同号正式版。"""

    def test_beta_and_rc_are_older_than_same_release(self):
        self.assertTrue(app_updater.is_newer("5.2.112", "5.2.112-beta"))
        self.assertFalse(app_updater.is_newer("5.2.112-beta", "5.2.112"))
        self.assertTrue(app_updater.is_newer("5.2.113", "5.2.112"))
        self.assertTrue(app_updater.is_newer("5.2.113", "5.2.112-beta"))
        keys = [app_updater._version_sort_key(v) for v in
                ("5.2.112-alpha", "5.2.112-beta", "5.2.112-rc1", "5.2.112", "5.2.113")]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(set(keys)), len(keys))
        # 老写法把 rc1 的 1 当成第 4 段，于是 5.2.83-rc1 比 5.2.83 还新。
        self.assertEqual(app_updater.version_key("5.2.83-rc1"), (5, 2, 83))
        self.assertFalse(app_updater.is_newer("5.2.83-rc1", "5.2.83"))
        self.assertEqual(app_updater.version_key("v5.2.112"), (5, 2, 112))
        self.assertEqual(app_updater.version_key(""), ())

class UpdateLogTests(unittest.TestCase):
    """要求 3：日志五要素一项都不能少。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.setup = self.root / "WhaleGirlQQBridge-Setup-9.9.9.exe"
        self.setup.write_bytes(b"MZ")
        self.app = self.root / "WhaleGirlQQBridge.exe"
        self.app.write_bytes(b"MZ")

    def tearDown(self):
        self.temp.cleanup()

    def _log(self, pid, **kwargs):
        with mock.patch.object(app_updater, "update_script_dir", return_value=self.root):
            script = app_updater.build_update_script(self.setup, self.app, pid, **kwargs)
            return script, app_updater.update_log_path(pid).read_text(encoding="utf-8")

    def test_log_records_repo_current_latest_asset_and_verify_result(self):
        info = _info(verify_method=app_updater.VERIFY_DIGEST)
        script, text = self._log(4321, info=info)
        for token in ("Owner/Repo", "5.2.84", "v5.3.0", EXE_NAME,
                      app_updater.VERIFY_DIGEST, "未下载"):
            self.assertIn(token, text, token)
        self.assertIn(b"update-4321.log", script.read_bytes())

    def test_log_after_download_records_verify_passed_and_sha256(self):
        download = _download(_info(), self.root)
        # 真实调用链（src/qq_app.py:4097）传的就是 download["path"]，这里照抄。
        with mock.patch.object(app_updater, "update_script_dir", return_value=self.root):
            app_updater.build_update_script(download["path"], self.app, 77)
            text = app_updater.update_log_path(77).read_text(encoding="utf-8")
        self.assertIn("校验通过", text)
        self.assertIn(EXE_SHA, text)
        self.assertIn(app_updater.VERIFY_DIGEST, text)

class FailureMessageTests(unittest.TestCase):
    """要求 4：网络异常/超时/超大响应都不崩，且提示能照着做。"""

    def _fails(self, client):
        with mock.patch.object(app_updater.httpx, "Client", return_value=client), \
                self.assertRaises(ValueError) as caught:
            app_updater.check_update("Owner/Repo", "5.2.84")
        self.assertIn("Releases", str(caught.exception))
        return str(caught.exception)

    def test_check_failures_are_actionable(self):
        self.assertIn("超时", self._fails(_Client(get_error=httpx.ReadTimeout("boom"))))
        self.assertIn("失败", self._fails(_Client(get_error=httpx.ConnectError("boom"))))
        self.assertIn("JSON", self._fails(_Client({API: _Response(payload=_BAD_JSON)})))
        oversized = _Client({API: _Response(headers={
            "content-length": str(app_updater.MAX_RELEASES_BYTES + 1)})})
        self.assertIn("2 MB", self._fails(oversized))

    def test_download_timeout_is_actionable(self):
        client = _Client(stream_error=httpx.ReadTimeout("boom"))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp, \
                mock.patch.object(app_updater.httpx, "Client", return_value=client):
            with self.assertRaises(ValueError) as caught:
                app_updater.download_update(_info(), tmp)
            self.assertIn("超时", str(caught.exception))
            self.assertEqual(list(Path(tmp).iterdir()), [])
