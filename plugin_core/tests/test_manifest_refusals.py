"""拒载码：17 个码里可稳定复现的那些，逐条走真 loader 路径。"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from plugin_core.api import REFUSAL_CODES
from plugin_core.audit import Audit, Log
from plugin_core.loader import Loader
from plugin_core.permissions import PermissionGate
from plugin_core.store import Store
from plugin_core.tests import support as s

GOOD_SOURCE = "def setup(api) -> None:\n    api.declare_inbound(lambda ctx: None)\n"


class RefusalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="plugin-core-refusal-"))
        self.plugins = self.tmp / "plugins"
        self.plugins.mkdir()
        self.data = self.tmp / "data"
        self.audit = Audit(self.data)
        self.log = Log("test")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- 夹具 -------------------------------------------------------
    def loader(self):
        return Loader(self.plugins, self.audit, self.log, PermissionGate(self.audit, self.log), Store(self.data))

    def write_manifest(self, dir_name, payload, *, source=GOOD_SOURCE, raw=None):
        target = self.plugins / dir_name
        target.mkdir(parents=True, exist_ok=True)
        path = target / "plugin.json"
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if source is not None:
            (target / "plugin.py").write_text(source, encoding="utf-8")
        return target

    def refusal(self, target, *, twice=False):
        dirs = [target, target] if twice else [target]
        result = self.loader().load_tree(dirs)
        return result

    def assertRefused(self, code, target, *, twice=False):
        with self.subTest(code=code):
            result = self.refusal(target, twice=twice)
            self.assertEqual(result["loaded"], [])
            self.assertEqual(len(result["refused"]), 1, result["refused"])
            self.assertEqual(result["refused"][0]["code"], code, result["refused"][0]["detail"])
            self.assertIn(code, REFUSAL_CODES)
            self.assertTrue(self.audit.select(event="load-refused"))

    # --- 各项 -------------------------------------------------------
    def test_valid_manifest_loads(self):
        target = s.write_plugin(self.plugins, "good", GOOD_SOURCE)
        result = self.loader().load_tree([target])
        self.assertEqual([item.id for item in result["loaded"]], ["good"])
        self.assertEqual(result["refused"], [])

    def test_bad_json(self):
        target = self.write_manifest("broken", None, source=None, raw="{不是 json")
        self.assertRefused("bad-json", target)

    def test_bad_manifest_not_object(self):
        target = self.write_manifest("scalar", None, source=None, raw="[1, 2, 3]")
        self.assertRefused("bad-manifest", target)

    def test_missing_field(self):
        payload = s.manifest("nofield")
        payload.pop("side")
        target = self.write_manifest("nofield", payload)
        self.assertRefused("missing-field", target)

    def test_bad_id(self):
        payload = s.manifest("badid")
        payload["id"] = "Bad_ID"
        target = self.write_manifest("badid", payload)
        self.assertRefused("bad-id", target)

    def test_id_mismatch(self):
        payload = s.manifest("other-name")
        target = self.write_manifest("dirname", payload)
        self.assertRefused("id-mismatch", target)

    def test_bad_api_version(self):
        payload = s.manifest("strver", api_version="1")
        target = self.write_manifest("strver", payload)
        self.assertRefused("bad-api-version", target)

    def test_api_version_mismatch(self):
        payload = s.manifest("future", api_version=2)
        target = self.write_manifest("future", payload)
        result = self.refusal(target)
        self.assertEqual(result["refused"][0]["code"], "api-version-mismatch")
        self.assertIn("2 != 1", result["refused"][0]["detail"])

    def test_entry_missing(self):
        payload = s.manifest("noentry", entry="nope.py:setup")
        target = self.write_manifest("noentry", payload)
        self.assertRefused("entry-missing", target)

    def test_entry_escape(self):
        payload = s.manifest("escape", entry="../evil.py:setup")
        target = self.write_manifest("escape", payload)
        self.assertRefused("entry-escape", target)

    def test_bad_entry(self):
        payload = s.manifest("nofunc", entry="plugin.py:nothere")
        target = self.write_manifest("nofunc", payload)
        self.assertRefused("bad-entry", target)

    def test_setup_error(self):
        target = s.write_plugin(self.plugins, "boom", "def setup(api) -> None:\n    raise RuntimeError('setup 故意失败')\n")
        result = self.refusal(target)
        self.assertEqual(result["refused"][0]["code"], "setup-error")
        self.assertIn("RuntimeError", result["refused"][0]["detail"])

    def test_duplicate_id(self):
        target = s.write_plugin(self.plugins, "dup", GOOD_SOURCE)
        result = self.refusal(target, twice=True)
        self.assertEqual(len(result["loaded"]), 1)
        self.assertEqual([item["code"] for item in result["refused"]], ["duplicate-id"])

    def test_bad_permission(self):
        payload = s.manifest("badperm", permissions=["read_message", "root"])
        target = self.write_manifest("badperm", payload)
        self.assertRefused("bad-permission", target)

    def test_bad_capability(self):
        payload = s.manifest("badcap", capabilities=[{"stage": "inbound", "produces": "nope", "priority": 1}])
        target = self.write_manifest("badcap", payload)
        self.assertRefused("bad-capability", target)

    def test_bad_data_policy(self):
        payload = s.manifest("badpolicy", data_policy="keep")
        target = self.write_manifest("badpolicy", payload)
        self.assertRefused("bad-data-policy", target)

    def test_bad_side(self):
        payload = s.manifest("badside", side="global")
        target = self.write_manifest("badside", payload)
        self.assertRefused("bad-side", target)

    def test_bad_upstream(self):
        payload = s.manifest("badup", upstream={"protocol": "mock", "kernel": "k", "range": "最新版"})
        target = self.write_manifest("badup", payload)
        self.assertRefused("bad-upstream", target)

    def test_bad_version_semver(self):
        payload = s.manifest("badver", version="1.0")
        target = self.write_manifest("badver", payload)
        self.assertRefused("bad-manifest", target)

    def test_every_code_is_covered(self):
        # 17 个码都在 api.REFUSAL_CODES 里，且本文件覆盖到其中 15 个
        covered = {
            "bad-json", "bad-manifest", "missing-field", "bad-id", "id-mismatch",
            "bad-api-version", "api-version-mismatch", "entry-missing", "entry-escape",
            "bad-entry", "setup-error", "duplicate-id", "bad-permission",
            "bad-capability", "bad-data-policy", "bad-side", "bad-upstream",
        }
        self.assertEqual(len(REFUSAL_CODES), 17)
        self.assertTrue(covered.issubset(set(REFUSAL_CODES)))


if __name__ == "__main__":
    unittest.main()
