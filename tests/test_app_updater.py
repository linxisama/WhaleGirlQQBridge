"""GitHub 自动更新：仓库归一化、版本比较、资产挑选、下载脚本生成。"""
import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from src import app_updater


class NormalizeRepoTests(unittest.TestCase):
    def test_accepts_owner_repo_and_github_links(self):
        self.assertEqual(app_updater.normalize_repo("Owner/Repo"), "Owner/Repo")
        self.assertEqual(app_updater.normalize_repo("https://github.com/Owner/Repo.git"),
                         "Owner/Repo")
        self.assertEqual(app_updater.normalize_repo("github.com/Owner/Repo/releases"), "Owner/Repo")

    def test_rejects_blank_and_bad_text(self):
        self.assertEqual(app_updater.normalize_repo(""), "")
        for value in ("repo-only", "https://example.com/a/b", "Owner/"):
            with self.assertRaises(ValueError):
                app_updater.normalize_repo(value)


class VersionTests(unittest.TestCase):
    def test_version_key_and_newer(self):
        self.assertEqual(app_updater.version_key("v5.2.83-beta"), (5, 2, 83))
        self.assertTrue(app_updater.is_newer("5.2.83", "5.2.82"))
        self.assertTrue(app_updater.is_newer("5.2.83.1", "5.2.83"))
        self.assertFalse(app_updater.is_newer("5.2.82", "5.2.83"))
        self.assertFalse(app_updater.is_newer("5.2.83", "5.2.83"))


class AssetTests(unittest.TestCase):
    def test_picks_setup_exe_and_rejects_unsafe_urls(self):
        release = {"assets": [
            {"name": "README.txt", "browser_download_url": "https://github.com/a/b/readme"},
            {"name": "WhaleGirlQQBridge-Setup-5.2.83.exe", "size": 10,
             "browser_download_url": "https://github.com/a/b/releases/download/v1/setup.exe"},
            {"name": "uninstall.exe", "browser_download_url": "https://github.com/a/b/uninstall.exe"},
        ]}
        asset = app_updater.pick_asset(release)
        self.assertEqual(asset["name"], "WhaleGirlQQBridge-Setup-5.2.83.exe")

    def test_asset_url_must_be_github_https(self):
        self.assertEqual(app_updater._safe_asset_url(""), "")
        self.assertEqual(app_updater._safe_asset_url("http://github.com/a/b"), "")
        self.assertEqual(app_updater._safe_asset_url("https://example.com/a.exe"), "")
        self.assertTrue(app_updater._safe_asset_url(
            "https://objects.githubusercontent.com/releases/a.exe"))

    def test_pick_asset_returns_none_without_safe_exe(self):
        self.assertIsNone(app_updater.pick_asset({"assets": [
            {"name": "setup.exe", "browser_download_url": "https://evil.example/setup.exe"}]}))
        self.assertIsNone(app_updater.pick_asset({}))


class _FakeResponse:
    def __init__(self, status_code=200, headers=None, text="", payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %s" % self.status_code)


class _FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses[url]


class HtmlFallbackTests(unittest.TestCase):
    def test_api_rate_limit_falls_back_to_public_release_page(self):
        api = "https://api.github.com/repos/Owner/Repo/releases/latest"
        latest = "https://github.com/Owner/Repo/releases/latest"
        assets = "https://github.com/Owner/Repo/releases/expanded_assets/v9.9.9"
        client = _FakeClient({
            api: _FakeResponse(403),
            latest: _FakeResponse(302, {"location": "https://github.com/Owner/Repo/releases/tag/v9.9.9"}),
            assets: _FakeResponse(200, text=(
                '<a href="/Owner/Repo/releases/download/v9.9.9/'
                'WhaleGirlQQBridge-Setup-9.9.9.exe">setup</a>'
                '<a href="/Owner/Repo/releases/download/v9.9.9/notes.txt">notes</a>'
            )),
        })
        with mock.patch.object(app_updater.httpx, "Client", return_value=client):
            info = app_updater.check_update("Owner/Repo", "5.2.84")
        self.assertEqual(info["latest"], "v9.9.9")
        self.assertTrue(info["hasUpdate"])
        self.assertEqual(info["source"], "release-page")
        self.assertEqual(info["asset"]["name"], "WhaleGirlQQBridge-Setup-9.9.9.exe")
        self.assertTrue(info["asset"]["url"].endswith("/WhaleGirlQQBridge-Setup-9.9.9.exe"))
        self.assertIn((assets, {"follow_redirects": True}), client.calls)

    def test_fallback_without_release_reports_both_reasons(self):
        api = "https://api.github.com/repos/Owner/Repo/releases/latest"
        latest = "https://github.com/Owner/Repo/releases/latest"
        client = _FakeClient({
            api: _FakeResponse(403),
            latest: _FakeResponse(302, {"location": "https://github.com/Owner/Repo/releases"}),
        })
        with mock.patch.object(app_updater.httpx, "Client", return_value=client):
            with self.assertRaises(ValueError) as caught:
                app_updater.check_update("Owner/Repo", "5.2.84")
        message = str(caught.exception)
        self.assertIn("HTTP 403", message)
        self.assertIn("备用 Release 页面", message)


class ScriptTests(unittest.TestCase):
    def test_build_script_waits_for_app_then_installs(self):
        root = Path(tempfile.mkdtemp(prefix="wp-update-"))
        setup = root / "WhaleGirlQQBridge-Setup-9.9.9.exe"
        app = root / "WhaleGirlQQBridge.exe"
        setup.write_bytes(b"MZ")
        app.write_bytes(b"MZ")
        script = app_updater.build_update_script(setup, app, 1234)
        raw = script.read_bytes()
        self.assertIn(b"PID=1234", raw)
        self.assertIn(b"WhaleGirlQQBridge-Setup-9.9.9.exe", raw)
        self.assertIn(b"/VERYSILENT", raw)
        self.assertIn(b"APPNAME", raw)
        self.assertIn(b"start \"\" \"%APP%\"", raw)

    def test_build_script_requires_existing_installer(self):
        root = Path(tempfile.mkdtemp(prefix="wp-update-"))
        app = root / "WhaleGirlQQBridge.exe"
        app.write_bytes(b"MZ")
        with self.assertRaises(ValueError):
            app_updater.build_update_script(root / "missing.exe", app, 1)


if __name__ == "__main__":
    unittest.main()
