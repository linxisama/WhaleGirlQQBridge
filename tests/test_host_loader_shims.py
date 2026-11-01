# -*- coding: utf-8 -*-
"""W11-3 宿主加载器垫片的回归：清单容错 / 入口回退 / CJS 与缺 UI 的准确归因。

两条断言线：
  · node 侧 —— 用真 node 跑 plugin-loader.js 里导出的判型/归一函数；
  · Python 侧 —— 用 import_probe / install_extension 的真实返回，验
    installedButDisabled 与 disabledReason 这两个新增字段。
宿主根一律落在临时目录，不碰仓库 data/**。
"""
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import qqagent_host as Q  # noqa: E402


WORK = Path(tempfile.mkdtemp(prefix='w113-tests-'))
HOST_ROOT = None


def setUpModule():
    global HOST_ROOT
    materialized = Q.materialize_host_root(data_dir=WORK / 'data', smoke=False)
    HOST_ROOT = Path(str(materialized.get('root') or ''))
    if not Q.is_host_root(HOST_ROOT):
        raise RuntimeError('宿主根没有就绪：%s' % HOST_ROOT)


def _write_extension(name, manifest, entry_source, extra=None):
    """在宿主 plugins/ 下造一个扩展目录。"""
    folder = Path(HOST_ROOT) / 'plugins' / name
    folder.mkdir(parents=True, exist_ok=True)
    payload = dict(manifest)
    (folder / 'plugin.json').write_text(json.dumps(payload, ensure_ascii=False),
                                        encoding='utf-8')
    if entry_source is not None:
        (folder / 'index.js').write_text(entry_source, encoding='utf-8')
    for relative, text in (extra or {}).items():
        target = folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    return folder


def _probe(folder, timeout=60.0):
    return Q.import_probe(folder, HOST_ROOT, timeout=timeout)


def _error_text(outcome):
    error = outcome.get('error') or {}
    return '%s|%s' % (error.get('code') or '', error.get('message') or '')


ESM_TOOL = (
    "export async function setup(api) {\n"
    "  api.registerTool({ id: 'demo', name: 'demo', description: '演示工具',\n"
    "    parameters: { type: 'object', properties: {} },\n"
    "    execute: async () => ({ ok: true }) });\n"
    "}\n"
)

NO_REGISTRATION = "export async function setup(api) { await Promise.resolve(api); }\n"

CJS_ENTRY = "module.exports = { register(api) { api.registerTool({ id: 'cjs' }); } };\n"

UI_DEPENDENT = (
    "import fs from 'node:fs';\n"
    "import path from 'node:path';\n"
    "import { fileURLToPath } from 'node:url';\n"
    "const DIR = path.dirname(fileURLToPath(import.meta.url));\n"
    "const UI = path.join(DIR, 'ui/index.html');\n"
    "export async function setup(api) {\n"
    "  if (!fs.existsSync(UI)) throw new Error('找不到 ui/index.html，控制面板装不进去');\n"
    "  api.registerTool({ id: 'ui', name: 'ui', description: '',\n"
    "    execute: async () => 1 });\n"
    "}\n"
)


class NodeShimTest(unittest.TestCase):
    """node 侧：判型/归一函数的真实行为。"""

    def _node(self, body):
        script = "import * as L from './src/plugin-loader.js';\n" + body
        proc = subprocess.run([Q._node_binary(None), '--input-type=module', '-e', script],
                              cwd=str(HOST_ROOT), capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=120)
        if proc.returncode != 0:
            raise AssertionError('node 执行失败：%s' % (proc.stderr or proc.stdout)[:800])
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_helpers_are_exported(self):
        names = self._node("console.log(JSON.stringify(['sanitizeRawManifest',"
                          "'declaredEntryField','pickEntryFallback','looksLikeCjsEntry',"
                           "'requiredConsoleUi'].map((n) => typeof L[n])));")
        self.assertEqual(names, ['function'] * 5)

    def test_enabled_by_default_missing_counts_as_enabled(self):
        flags = self._node("console.log(JSON.stringify(["
                           "L.sanitizeRawManifest({}).enabledByDefault,"
                           "L.sanitizeRawManifest({ enabledByDefault: 'false' }).enabledByDefault,"
                           "L.sanitizeRawManifest({ enabledByDefault: false }).enabledByDefault]));")
        self.assertEqual(flags, [True, False, False])

    def test_numeric_version_is_normalised(self):
        raw = self._node("const r = L.sanitizeRawManifest({ version: 1, id: 7 });"
                         "console.log(JSON.stringify([r.version, r.id]));")
        self.assertEqual(raw, ['1', '7'])

    def test_declared_entry_field_priority(self):
        picked = self._node("console.log(JSON.stringify(["
                            "L.declaredEntryField({ entry: 'a.js', main: 'b.js' }),"
                            "L.declaredEntryField({ main: 'lib/main.mjs' }),"
                            "L.declaredEntryField({ module: 'lib/m.mjs' }),"
                            "L.declaredEntryField({})]));")
        self.assertEqual(picked, ['a.js', 'lib/main.mjs', 'lib/m.mjs', 'index.js'])

    def test_entry_fallback_order(self):
        base = WORK / 'fallback'
        only_index = base / 'a'
        only_index.mkdir(parents=True, exist_ok=True)
        (only_index / 'index.js').write_text(ESM_TOOL, encoding='utf-8')
        esm_only = base / 'b'
        esm_only.mkdir(parents=True, exist_ok=True)
        (esm_only / 'index.mjs').write_text(ESM_TOOL, encoding='utf-8')
        unique = base / 'c'
        unique.mkdir(parents=True, exist_ok=True)
        (unique / 'main.js').write_text(ESM_TOOL, encoding='utf-8')
        ambiguous = base / 'd'
        ambiguous.mkdir(parents=True, exist_ok=True)
        (ambiguous / 'one.js').write_text('', encoding='utf-8')
        (ambiguous / 'two.js').write_text('', encoding='utf-8')
        dirs = [only_index, esm_only, unique, ambiguous]
        body = ("const pick = (p) => L.pickEntryFallback(p);"
                "console.log(JSON.stringify(%s.map(pick)));"
                % json.dumps([str(item) for item in dirs]))
        picked = self._node(body)
        self.assertEqual([Path(item).name if item else None for item in picked],
                         ['index.js', 'index.mjs', 'main.js', None])

    def test_cjs_detection(self):
        flags = self._node("console.log(JSON.stringify(["
                           "L.looksLikeCjsEntry(%s),"
                           "L.looksLikeCjsEntry('export async function setup(api){}'),"
                           "L.looksLikeCjsEntry('export default { setup(){} }; // module.exports 旧写法')"
                           "]));" % json.dumps(CJS_ENTRY))
        self.assertEqual(flags, [True, False, False])

    def test_console_ui_detection(self):
        found = self._node("console.log(JSON.stringify(["
                           "L.requiredConsoleUi({}, %s),"
                           "L.requiredConsoleUi({ ui: { entry: 'ui/panel.html' } }, ''),"
                           "L.requiredConsoleUi({}, 'export function setup(){}')]));"
                           % json.dumps("const page = path.join(dir, 'ui/index.html');"))
        self.assertEqual(found, ['ui/index.html', 'ui/panel.html', ''])


class LoaderBehaviourTest(unittest.TestCase):
    """真宿主：加载成功与四种失败各自的错误码。"""

    def test_entry_fallback_loads(self):
        folder = _write_extension('shim-fallback', {
            'id': 'shim-fallback', 'name': '入口回退', 'version': '1.0.0',
            'entry': 'dist/main.js', 'enabledByDefault': True}, ESM_TOOL)
        outcome = _probe(folder)
        self.assertTrue(outcome.get('ok'), _error_text(outcome))
        self.assertGreaterEqual(len(outcome.get('tools') or []), 1)

    def test_main_module_fields_are_honoured(self):
        folder = Path(HOST_ROOT) / 'plugins' / 'shim-main'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'plugin.json').write_text(json.dumps({
            'id': 'shim-main', 'name': 'main/module 混用', 'main': 'lib/main.mjs',
            'module': 'lib/main.mjs'}, ensure_ascii=False), encoding='utf-8')
        (folder / 'lib').mkdir(exist_ok=True)
        (folder / 'lib' / 'main.mjs').write_text(ESM_TOOL, encoding='utf-8')
        outcome = _probe(folder)
        self.assertTrue(outcome.get('ok'), _error_text(outcome))
        self.assertGreaterEqual(len(outcome.get('tools') or []), 1)

    def test_missing_entry_reports_entry_missing(self):
        folder = _write_extension('shim-noentry', {
            'id': 'shim-noentry', 'name': '没入口', 'entry': 'nope.js'}, None)
        text = _error_text(_probe(folder))
        self.assertIn('entry-missing', text)
        self.assertIn('入口文件不存在', text)

    def test_cjs_entry_is_reported_in_chinese(self):
        """宿主根是 type=module，.js 就是 ESM，CJS 入口只能给中文归因。

        module.exports 的扩展在**非** type=module 的根里能靠 Node 互操作跑起来，
        那条正向路径由 tests/test_qqagent_bridge_hostroot.py 的 wp1c-demo 盯着；
        这里只保证真跑不起来时用户看到的是中文怎么改，而不是 module is not defined。
        """
        folder = _write_extension('shim-cjs', {
            'id': 'shim-cjs', 'name': 'CJS', 'entry': 'index.js'}, CJS_ENTRY)
        text = _error_text(_probe(folder))
        self.assertIn('cjs-unsupported', text)
        self.assertIn('CommonJS 入口', text)
        self.assertIn('改成 export async function setup', text)

    def test_missing_console_ui_uses_its_own_code(self):
        folder = _write_extension('shim-ui', {
            'id': 'shim-ui', 'name': '缺 UI', 'entry': 'index.js'}, UI_DEPENDENT)
        text = _error_text(_probe(folder))
        self.assertIn('needs-console-ui', text)
        self.assertNotIn('extension-unavailable', text)

    def test_console_ui_present_still_loads(self):
        folder = _write_extension('shim-ui-ok', {
            'id': 'shim-ui-ok', 'name': '有 UI', 'entry': 'index.js'}, UI_DEPENDENT,
            extra={'ui/index.html': '<html><body>ok</body></html>'})
        outcome = _probe(folder)
        self.assertTrue(outcome.get('ok'), _error_text(outcome))

    def test_bad_json_manifest_is_reported(self):
        folder = Path(HOST_ROOT) / 'plugins' / 'shim-badjson'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'plugin.json').write_text('{"id": "shim-badjson", "name": ', encoding='utf-8')
        (folder / 'index.js').write_text(ESM_TOOL, encoding='utf-8')
        text = _error_text(_probe(folder))
        self.assertIn('清单解析失败', text)


class InstalledButDisabledTest(unittest.TestCase):
    """Python 侧：装了但没生效必须看得见。"""

    def test_disabled_by_default_is_flagged(self):
        folder = _write_extension('shim-off', {
            'id': 'shim-off', 'name': '默认关', 'enabledByDefault': False}, ESM_TOOL)
        outcome = _probe(folder)
        self.assertIs(outcome.get('installedButDisabled'), True)
        self.assertIn('enabledByDefault=false', outcome.get('disabledReason') or '')

    def test_normal_extension_is_not_flagged(self):
        folder = _write_extension('shim-on', {
            'id': 'shim-on', 'name': '正常', 'enabledByDefault': True}, ESM_TOOL)
        outcome = _probe(folder)
        self.assertTrue(outcome.get('ok'), _error_text(outcome))
        self.assertIs(outcome.get('installedButDisabled'), False)
        self.assertEqual(outcome.get('disabledReason'), '')

    def test_zero_registration_is_flagged(self):
        folder = _write_extension('shim-empty', {
            'id': 'shim-empty', 'name': '什么都不注册', 'enabledByDefault': True},
            NO_REGISTRATION)
        outcome = _probe(folder)
        self.assertTrue(outcome.get('ok'), _error_text(outcome))
        self.assertEqual(len(outcome.get('tools') or []), 0)
        self.assertIs(outcome.get('installedButDisabled'), True)
        self.assertIn('什么都没注册', outcome.get('disabledReason') or '')

    def test_fields_exist_on_failure_outcomes(self):
        outcome = Q.import_probe(WORK / 'no-such-dir', HOST_ROOT, timeout=10.0)
        self.assertFalse(outcome.get('ok'))
        self.assertIn('installedButDisabled', outcome)
        self.assertIn('disabledReason', outcome)

    def test_install_extension_reports_disabled(self):
        zip_path = WORK / 'shim-zip.zip'
        with zipfile.ZipFile(str(zip_path), 'w') as archive:
            archive.writestr('shim-zip/plugin.json', json.dumps({
                'id': 'shim-zip', 'name': '默认关闭的包', 'entry': 'index.js',
                'enabledByDefault': False}, ensure_ascii=False))
            archive.writestr('shim-zip/index.js', ESM_TOOL)
        outcome = Q.install_extension(zip_path, HOST_ROOT, force=True)
        self.assertTrue(outcome.get('ok'), outcome.get('error'))
        self.assertIs(outcome.get('installedButDisabled'), True)
        self.assertIn('enabledByDefault=false', outcome.get('disabledReason') or '')

    def test_install_extension_keeps_old_keys(self):
        zip_path = WORK / 'shim-zip-ok.zip'
        with zipfile.ZipFile(str(zip_path), 'w') as archive:
            archive.writestr('shim-zip-ok/plugin.json', json.dumps({
                'id': 'shim-zip-ok', 'name': '正常包', 'entry': 'index.js',
                'enabledByDefault': True}, ensure_ascii=False))
            archive.writestr('shim-zip-ok/index.js', ESM_TOOL)
        outcome = Q.install_extension(zip_path, HOST_ROOT, force=True)
        self.assertTrue(outcome.get('ok'), outcome.get('error'))
        for key in ('ok', 'id', 'root', 'files', 'bytes', 'error', 'manifest'):
            self.assertIn(key, outcome)
        self.assertIs(outcome.get('installedButDisabled'), False)


if __name__ == '__main__':
    unittest.main(verbosity=2)
