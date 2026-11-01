"""中文文件名/纯文本人设导入：标识用 ASCII slug，展示名保留中文。"""
import pathlib
import tempfile
import unittest

from fastapi.testclient import TestClient


class PersonaImportChineseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = pathlib.Path(self._tmp.name)
        (root / 'data' / 'qq-only').mkdir(parents=True)
        (root / 'config.qq.yaml').write_text('qq_only:\n  mode: mock\n', encoding='utf-8')
        from src.qq_app import QQRuntime, create_app
        self.runtime = QQRuntime(root=root)
        self.addCleanup(self.runtime.db.close)
        app = create_app(self.runtime)
        app.state.bot = self.runtime      # 不跑 lifespan 时直接挂运行时
        self.client = TestClient(app)

    def test_plain_text_with_chinese_filename(self):
        response = self.client.post('/api/personas/import', json={
            'filename': '我的老婆.txt',
            'content': '你是一只温柔的鲸鱼娘，说话软软的。'})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        name = body.get('name') or ''
        self.assertTrue(name, body)
        self.assertRegex(name, r'^[A-Za-z0-9_\-]{1,48}$',
                         '标识必须是 ASCII slug')
        personas = self.client.get('/api/personas').json().get('personas') or []
        matched = [item for item in personas if item.get('name') == name]
        self.assertTrue(matched, personas)
        self.assertEqual(matched[0].get('display_name'), '我的老婆')

    def test_yaml_with_chinese_name_field(self):
        content = 'name: 鲸鱼娘二号\nsystem_prompt: 你说话很慢。\n'
        response = self.client.post('/api/personas/import',
                                    json={'filename': 'p.yaml', 'content': content})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertRegex(response.json()['name'], r'^[A-Za-z0-9_\-]{1,48}$')


if __name__ == '__main__':
    unittest.main()
