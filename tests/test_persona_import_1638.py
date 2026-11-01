"""跨端人设文件、默认无引用气泡及透明主题文件框回归。"""
import asyncio
import base64
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from tests import test_persona_files as fixtures
from tests import test_official_quote_and_card as quote_fixtures
from src.persona_import import parse_persona, read_persona_file

PERSONA = {'name': 'whale_mom', 'display_name': '鲸鱼妈妈',
           'system_prompt': '你是温柔、可靠的鲸鱼妈妈，按语境自然接话。',
           'temperature': 0.75, 'max_tokens': 1200,
           'keywords': ['鲸妈妈'], 'examples': [{'user': '累了', 'assistant': '过来歇会儿。'}]}


class ParserTests(unittest.TestCase):
    def test_flat_nested_and_json_equivalent(self):
        import json
        for value in (PERSONA, {'format': 'WhaleGirlPersona/v1', 'persona': PERSONA}):
            for content in (yaml.safe_dump(value, allow_unicode=True), json.dumps(value)):
                self.assertEqual(parse_persona(content)['system_prompt'], PERSONA['system_prompt'])

    def test_txt_plain_prompt_and_structured_txt(self):
        self.assertEqual(parse_persona('你是温柔的鲸鱼妈妈。\n说话自然。', '鲸鱼妈妈.txt')['display_name'], '鲸鱼妈妈')
        self.assertEqual(parse_persona(yaml.safe_dump(PERSONA), 'a.txt')['name'], 'whale_mom')

    def test_rejects_wrong_version_missing_prompt_and_bad_yaml(self):
        for value in ({'format': 'WhaleGirlPersona/v99', 'persona': PERSONA},
                      {'format': 'WhaleGirlPersona/v1', 'persona': []},
                      {'persona': {'system_prompt': {}}}, {'system_prompt': ''}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_persona(yaml.safe_dump(value))
        with self.assertRaisesRegex(ValueError, 'YAML'):
            parse_persona('name: [', 'bad.yaml')

    def test_bom_encodings_and_file_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / '人设.yaml'
            for encoding in ('utf-8-sig', 'utf-16'):
                file.write_text(yaml.safe_dump(PERSONA, allow_unicode=True), encoding=encoding)
                self.assertEqual(parse_persona(read_persona_file(file))['name'], 'whale_mom')
            file.write_bytes(b'x' * (8 * 1024 * 1024 + 1))
            with self.assertRaisesRegex(ValueError, '8 MB'):
                read_persona_file(file)


class ImportRouteTests(unittest.TestCase):
    setUp = fixtures.PersonaExportImportRouteTests.setUp
    tearDown = fixtures.PersonaExportImportRouteTests.tearDown

    def upload(self, value):
        return self.client.post('/api/personas/import', json={
            'filename': '鲸鱼妈妈.yaml', 'content': yaml.safe_dump(value, allow_unicode=True)})

    def test_import_activate_prompt_and_restart(self):
        response = self.upload({'format': 'WhaleGirlPersona/v1', 'persona': PERSONA})
        self.assertEqual(response.status_code, 200, response.text)
        name = response.json()['name']
        self.assertEqual(self.client.post(f'/api/personas/{name}/activate').status_code, 200)
        prompt = self.bot.style.full_prompt()
        self.assertIn(PERSONA['system_prompt'], prompt)
        self.assertIn('过来歇会儿', prompt)
        restarted = fixtures.QQRuntime(root=self.root, db_path=self.root / 'restart.sqlite')
        try:
            self.assertEqual(restarted.style.active_name, name)
            self.assertIn(PERSONA['system_prompt'], restarted.style.full_prompt())
        finally:
            restarted.db.close()

    def test_repeated_import_never_overwrites(self):
        body = {'format': 'WhaleGirlPersona/v1', 'persona': PERSONA}
        first, second = self.upload(body), self.upload(body)
        self.assertEqual((first.json()['name'], second.json()['name']), ('whale_mom', 'whale_mom-2'))
        self.assertEqual(self.client.get('/api/personas/whale_mom').json()['system_prompt'], PERSONA['system_prompt'])

    def test_wrapper_preserves_embedded_image_and_ignores_foreign_image_path(self):
        raw = base64.b64decode('R0lGODlhAQABAIABAAAAAP///yH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==')
        body = {'format': 'WhaleGirlPersona/v1', 'persona': dict(PERSONA, image='C:/private.png'),
                'image_data': base64.b64encode(raw).decode(), 'image_ext': 'gif'}
        response = self.upload(body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['image'])
        self.assertEqual(self.client.get('/api/personas/whale_mom/image').status_code, 200)

    def test_bad_file_does_not_change_current_or_write_persona(self):
        old = self.bot.style.active_name
        before = set((self.root / 'styles/custom').glob('*'))
        self.assertEqual(self.upload({'persona': []}).status_code, 400)
        self.assertEqual(self.bot.style.active_name, old)
        self.assertEqual(set((self.root / 'styles/custom').glob('*')), before)


class QuoteTests(unittest.TestCase):
    def test_ordinary_index_never_adds_native_reference_but_retains_passive_id(self):
        for kind in ('group', 'private'):
            transport, captured = quote_fixtures.OutboundReferenceTests().transport()
            asyncio.run(transport.send({'kind': kind, 'target': quote_fixtures.GROUP,
                'id': 'M1', '_quote_ref': 'INDEX', '_passive_seq': 2, '_is_quote': True}, '自然回复'))
            body = captured['body']
            self.assertNotIn('message_reference', body)
            self.assertEqual((body['msg_id'], body['msg_seq']), ('M1', 2))


class FileDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = fixtures.QApplication.instance() or fixtures.QApplication([])

    def test_file_view_is_readable_under_inherited_dark_stylesheet(self):
        from PyQt6.QtWidgets import QWidget, QFileDialog, QListView
        from src.gui.file_dialogs import _styled
        parent = QWidget();parent.setStyleSheet('QWidget { background: transparent; color: #142c49; } QTreeView, QListView { background: black; color: black; }')
        dialog = _styled(QFileDialog(parent))
        dialog.show();self.app.processEvents()
        views = [v for v in dialog.findChildren(QListView) if v.objectName() == 'listView']
        self.assertTrue(views)
        color = views[0].viewport().grab().toImage().pixelColor(3, 3)
        self.assertEqual(color.alpha(), 255)
        self.assertGreater(color.lightness(), 220)
        dialog.close();parent.close()

    def test_import_selects_new_item_and_keeps_existing_active_persona(self):
        from PyQt6.QtCore import Qt
        gui = fixtures.qq_gui
        tab = gui.PersonaTab()
        tab._current = 'default'
        def get(path):
            if path == 'personas':
                return {'active': 'default', 'personas': [{'name': 'default'}, PERSONA]}
            if path == 'personas/banned-words':
                return {'words': []}
            return dict(PERSONA)
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / '妈妈.yaml'
            file.write_text(yaml.safe_dump({'format': 'WhaleGirlPersona/v1', 'persona': PERSONA}), encoding='utf-8-sig')
            with patch.object(gui.file_dialogs, 'open_file', return_value=(str(file), '')), \
                 patch.object(gui, 'api_post', return_value={'success': True, 'name': 'whale_mom'}) as post, \
                 patch.object(gui, 'api_get', side_effect=get), patch.object(tab, '_refresh_image_preview'):
                tab._import_persona()
            self.assertEqual(tab.list.currentItem().data(Qt.ItemDataRole.UserRole), 'whale_mom')
            self.assertEqual(tab.ed_prompt.toPlainText(), PERSONA['system_prompt'])
            self.assertIn('设为当前', tab.notice.text())
            self.assertEqual(post.call_count, 1)
        tab.close()


class ModelPipelineTests(unittest.IsolatedAsyncioTestCase):
    from tests import test_core_pipeline_1618 as flow
    asyncSetUp = flow.MixedPluginPipelineTests.asyncSetUp
    asyncTearDown = flow.MixedPluginPipelineTests.asyncTearDown
    message = flow.MixedPluginPipelineTests.message
    run_message = flow.MixedPluginPipelineTests.run_message

    async def test_imported_persona_reaches_real_worker_model_arguments(self):
        import httpx
        from src.qq_app import create_app
        body = {'filename': '妈妈.yaml', 'content': yaml.safe_dump({'format': 'WhaleGirlPersona/v1', 'persona': PERSONA})}
        app = create_app(self.bot)
        app.state.bot = self.bot
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            response = await client.post('/api/personas/import', json=body)
            self.assertEqual(response.status_code, 200, response.text)
            name = response.json()['name']
            self.assertEqual((await client.post(f'/api/personas/{name}/activate')).status_code, 200)
        self.bot.running = True
        self.worker = asyncio.create_task(self.bot._work())
        await self.run_message(self.message('official', 'private', 'persona-638'))
        self.assertIn(PERSONA['system_prompt'], self.bot.ai.chat.call_args.kwargs['system_prompt'])
        self.assertIn(PERSONA['examples'][0]['assistant'], self.bot.ai.chat.call_args.kwargs['system_prompt'])
