"""多账号接口测试：新建 / 切换 / 重命名 / 删除（都在单进程内，不重启）。"""
import pathlib
import tempfile
import unittest

from fastapi.testclient import TestClient

from src.account_book import AccountBook


class AccountsApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        (self.root / 'config.qq.yaml').write_text('ai:\n  model: demo\nqq:\n  mode: mock\n',
                                                  encoding='utf-8')
        (self.root / 'data' / 'qq-only').mkdir(parents=True)

    def _app(self):
        """只测账号接口：不构造真实 QQRuntime（那会读人设/插件，太重）。"""
        from fastapi import FastAPI
        from src.qq_app import create_app
        from src.account_book import AccountBook

        app = create_app()
        # 覆盖为临时账号簿，避免测试写到用户真实 data 目录
        app.state.account_book = AccountBook(self.root)
        return app

    def test_list_add_select_remove(self):
        from src.qq_app import create_app
        app = create_app()
        app.state.account_book = AccountBook(self.root)
        # 用 TestClient 但不触发 lifespan（不构造真实运行时）
        client = TestClient(app)
        listed = client.get('/api/accounts').json()
        self.assertEqual(len(listed['items']), 1)
        self.assertEqual(listed['active'], 'default')

        created = client.post('/api/accounts', json={'label': '小号'}).json()
        self.assertTrue(created['success'])
        account_id = created['item']['id']
        self.assertEqual(created['active'], account_id)

        renamed = client.post('/api/accounts/rename',
                              json={'id': account_id, 'label': '主号'}).json()
        self.assertEqual(renamed['item']['label'], '主号')

        removed = client.post('/api/accounts/remove', json={'id': account_id}).json()
        self.assertTrue(removed['success'])
        self.assertEqual(removed['active'], 'default')
        self.assertEqual(len(client.get('/api/accounts').json()['items']), 1)

    def test_remove_last_is_409(self):
        from src.qq_app import create_app
        app = create_app()
        app.state.account_book = AccountBook(self.root)
        client = TestClient(app)
        response = client.post('/api/accounts/remove', json={'id': 'default'})
        self.assertEqual(response.status_code, 409)


if __name__ == '__main__':
    unittest.main()
