"""Static permission evidence is reproducible without running market code."""
import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools import audit_external_market as audit


class AuditPermissionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def package(self, permissions, *, entry=True):
        path = self.root / 'sample.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('sample/plugin.json', json.dumps({'id': 'sample', 'permissions': permissions}))
            if entry:
                archive.writestr('sample/index.js', 'throw new Error("must not execute");')
        return path

    def test_array_permissions_retained_without_node(self):
        path = self.package(['web_fetch', 'web_fetch', 'onebot_call', 123])
        with patch.object(audit.subprocess, 'run', side_effect=AssertionError('Node must not run')):
            result = audit.inspect_zip(str(path), check_syntax=False)
        summary = audit.permission_summary([{'path': str(path), 'result': result}])
        self.assertEqual(summary['permissions_arrays'], 1)
        self.assertEqual(summary['web_fetch_packages'], 1)
        self.assertEqual(summary['permission_word_occurrences'], {'onebot_call': 1, 'web_fetch': 2})
        self.assertEqual(len(result['archive_sha256']), 64)
        self.assertEqual(result['manifest_path'], 'sample/plugin.json')

    def test_non_arrays_never_count_as_permission(self):
        for value in ('web_fetch', None, {'web_fetch': True}, 1):
            result = audit.inspect_zip(str(self.package(value)), check_syntax=False)
            summary = audit.permission_summary([{'path': 'exists', 'result': result}])
            self.assertEqual(summary['permissions_arrays'], 0)
            self.assertEqual(summary['web_fetch_packages'], 0)

    def test_manifest_count_independent_of_js_entry(self):
        result = audit.inspect_zip(str(self.package(['web_fetch'], entry=False)), check_syntax=False)
        self.assertFalse(result['ok'])
        self.assertTrue(result['permissions_array'])

    def test_missing_manifest_is_reported_without_crash(self):
        path = self.root / 'missing.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('index.js', 'export {};')
        result = audit.inspect_zip(str(path), check_syntax=False)
        self.assertFalse(result['ok'])
        self.assertFalse(result['permissions_array'])

    def test_invalid_manifest_is_not_reported_as_zero_permissions(self):
        for manifest in ('{broken', '[]', 'null'):
            path = self.root / 'bad-manifest.zip'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('plugin.json', manifest)
                archive.writestr('index.js', 'export {};')
            result = audit.inspect_zip(str(path), check_syntax=False)
            self.assertFalse(result['ok'])
            summary = audit.permission_summary([{'path': str(path), 'result': result}])
            self.assertEqual(summary['unreadable_manifests'], 1)

    def test_invalid_zip_is_reported_without_node(self):
        path = self.root / 'invalid.zip'
        path.write_bytes(b'not a zip')
        result = audit.inspect_zip(str(path), check_syntax=False)
        self.assertFalse(result['ok'])
        self.assertFalse(result['manifest_readable'])

    def test_offline_exact_snapshot_and_missing_cache(self):
        cache, output = self.root / 'cache', self.root / 'out'
        cache.mkdir()
        output.mkdir()
        self.package(['web_fetch']).replace(cache / 'sample.zip')
        (output / 'market-list.json').write_text(json.dumps({'items': [
            {'id': 'sample', 'status': 'approved'}, {'id': 'absent', 'status': 'approved'}]}), encoding='utf-8')
        with (patch.object(audit, 'CACHE', cache),
              patch.object(audit, 'fetch_json', side_effect=AssertionError('network')),
              contextlib.redirect_stdout(io.StringIO())):
            status = audit.main(['--offline', '--permissions-only', '--output-dir', str(output)])
        report = json.loads((output / 'external-market-audit.json').read_text(encoding='utf-8'))
        self.assertEqual(status, 1)
        self.assertEqual(report['permissions_summary']['packages'], 2)
        self.assertEqual(report['permissions_summary']['download_failures'], 1)
        self.assertEqual(report['permissions_summary']['web_fetch_packages'], 1)
        self.assertIn('absent', report['items'][0]['problems'][0])

    def test_limit_does_not_change_approved_denominator(self):
        output = self.root / 'out'
        output.mkdir()
        (output / 'market-list.json').write_text(json.dumps({'items': [
            {'id': 'sample', 'status': 'approved'}, {'id': 'not-selected', 'status': 'approved'}]}), encoding='utf-8')
        self.package([])
        with (patch.object(audit, 'CACHE', self.root), contextlib.redirect_stdout(io.StringIO())):
            status = audit.main(['--offline', '--permissions-only', '--limit', '1', '--output-dir', str(output)])
        self.assertEqual(status, 0)
        report = json.loads((output / 'external-market-audit.json').read_text(encoding='utf-8'))
        self.assertEqual(report['approved_total'], 2)
        self.assertEqual(report['tested'], 1)

    def test_verified_cache_reuse_and_version_change(self):
        payload = self.package(['web_fetch']).read_bytes()
        item = {'id': 'sample', 'version': '1', 'downloadUrl': '/api/community/plugins/sample/download'}
        with (patch.object(audit, 'CACHE', self.root),
              patch.object(audit.urllib.request, 'urlopen', side_effect=lambda *a, **kw: io.BytesIO(payload)) as request):
            audit.download(item)
            audit.download(item)
            self.assertEqual(request.call_count, 1)
            audit.download({**item, 'version': '2'})
            self.assertEqual(request.call_count, 2)
            audit.download({**item, 'version': '2'}, force=True)
            self.assertEqual(request.call_count, 3)
        metadata = json.loads((self.root / 'sample.cache.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata['version'], '2')

    def test_corrupt_cache_is_repaired_online(self):
        payload = self.package([]).read_bytes()
        (self.root / 'sample.zip').write_bytes(b'partial-download')
        with (patch.object(audit, 'CACHE', self.root),
              patch.object(audit.urllib.request, 'urlopen', return_value=io.BytesIO(payload)) as request):
            audit.download({'id': 'sample', 'downloadUrl': '/api/community/plugins/sample/download'})
            self.assertEqual(request.call_count, 1)
        self.assertTrue(audit.valid_zip(self.root / 'sample.zip'))

    def test_invalid_response_keeps_previous_cache_and_cleans_parts(self):
        original = self.package(['web_fetch']).read_bytes()
        with (patch.object(audit, 'CACHE', self.root), patch.object(audit.time, 'sleep'),
              patch.object(audit.urllib.request, 'urlopen', side_effect=lambda *a, **kw: io.BytesIO(b'not a zip')) as request):
            with self.assertRaises(ValueError):
                audit.download({'id': 'sample', 'downloadUrl': '/api/community/plugins/sample/download'}, force=True)
            self.assertEqual(request.call_count, 3)
        self.assertEqual((self.root / 'sample.zip').read_bytes(), original)
        self.assertEqual(list(self.root.glob('*.part')), [])

    def test_broken_stream_keeps_previous_cache(self):
        class Broken(io.BytesIO):
            def read(self, *args):
                raise OSError('connection interrupted')
        original = self.package([]).read_bytes()
        with (patch.object(audit, 'CACHE', self.root),
              patch.object(audit.urllib.request, 'urlopen', return_value=Broken(b'partial'))):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                audit.download({'id': 'sample', 'downloadUrl': '/api/community/plugins/sample/download'}, retries=0, force=True)
        self.assertEqual((self.root / 'sample.zip').read_bytes(), original)
        self.assertEqual(list(self.root.glob('*.part')), [])

    def test_offline_change_preserves_baseline(self):
        output = self.root / 'out'
        output.mkdir()
        self.package(['web_fetch'])
        (output / 'market-list.json').write_text(json.dumps({'items': [
            {'id': 'sample', 'version': '1', 'status': 'approved'}]}), encoding='utf-8')
        argv = ['--offline', '--permissions-only', '--output-dir', str(output)]
        with patch.object(audit, 'CACHE', self.root), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(audit.main(argv), 0)
            baseline = (output / 'external-market-audit.json').read_bytes()
            self.assertEqual(audit.main(argv), 0)
            report = json.loads((output / 'external-market-audit.json').read_text(encoding='utf-8'))
            self.assertEqual(report['baseline_verified_packages'], 1)
            baseline = (output / 'external-market-audit.json').read_bytes()
            self.package(['onebot_call'])
            self.assertEqual(audit.main(argv), 1)
            self.assertEqual((output / 'external-market-audit.json').read_bytes(), baseline)
            failed = json.loads((output / 'external-market-audit.failed.json').read_text(encoding='utf-8'))
            self.assertFalse(failed['complete'])
            self.assertIn('基线不一致', failed['items'][0]['problems'][0])
            self.assertEqual(audit.main(argv), 1)  # A failed rerun cannot bless the changed cache.

    def test_offline_corrupt_cache_does_not_use_network(self):
        output = self.root / 'out'
        output.mkdir()
        (output / 'market-list.json').write_text(json.dumps({'items': [
            {'id': 'sample', 'status': 'approved'}]}), encoding='utf-8')
        (self.root / 'sample.zip').write_bytes(b'partial')
        with (patch.object(audit, 'CACHE', self.root), contextlib.redirect_stdout(io.StringIO()),
              patch.object(audit.urllib.request, 'urlopen', side_effect=AssertionError('network'))):
            self.assertEqual(audit.main(['--offline', '--permissions-only', '--output-dir', str(output)]), 1)
        report = json.loads((output / 'external-market-audit.json').read_text(encoding='utf-8'))
        self.assertIn('ZIP损坏', report['items'][0]['problems'][0])

    def test_remote_filename_cannot_escape_cache(self):
        for identifier in ('../outside', 'C:escape', '..', 'foo/bar', r'foo\bar'):
            with self.assertRaises(ValueError):
                audit.cache_path({'id': identifier})

    def test_invalid_options_fail_before_network(self):
        for argv in (['--offline', '--refresh'], ['--workers', '0'], ['--limit', '-1']):
            with (contextlib.redirect_stderr(io.StringIO()),
                  patch.object(audit, 'fetch_json', side_effect=AssertionError('network'))):
                with self.assertRaises(SystemExit):
                    audit.main(argv)

    def test_modified_valid_zip_is_not_reused_online(self):
        payload = self.package(['web_fetch']).read_bytes()
        item = {'id': 'sample', 'version': '1', 'downloadUrl': '/api/community/plugins/sample/download'}
        with (patch.object(audit, 'CACHE', self.root),
              patch.object(audit.urllib.request, 'urlopen', side_effect=lambda *a, **kw: io.BytesIO(payload)) as request):
            audit.download(item)
            self.package(['onebot_call'])
            audit.download(item)
            self.assertEqual(request.call_count, 2)
        self.assertEqual((self.root / 'sample.zip').read_bytes(), payload)

    def test_modified_snapshot_cannot_silently_rewrite_baseline(self):
        output = self.root / 'out'
        output.mkdir()
        snapshot = output / 'market-list.json'
        snapshot.write_text(json.dumps({'items': []}), encoding='utf-8')
        argv = ['--offline', '--permissions-only', '--output-dir', str(output)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(audit.main(argv), 0)
        before = (output / 'external-market-audit.json').read_bytes()
        snapshot.write_text(json.dumps({'items': [{'id': 'new', 'status': 'approved'}]}), encoding='utf-8')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            audit.main(argv)
        self.assertEqual((output / 'external-market-audit.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
