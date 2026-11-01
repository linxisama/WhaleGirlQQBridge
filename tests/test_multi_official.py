"""官方机器人多开：配置解析、主/次路由、状态列表。不真的连腾讯。"""
import pathlib
import tempfile
import unittest

from src.qq_app import QQRuntime


class MultiOfficialTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        (self.root / 'data' / 'qq-only').mkdir(parents=True)

    def _runtime(self, config_text):
        (self.root / 'config.qq.yaml').write_text(config_text, encoding='utf-8')
        runtime = QQRuntime(root=self.root)
        self.addCleanup(runtime.db.close)
        return runtime

    def test_legacy_single_bot_still_works(self):
        runtime = self._runtime('qq:\n  app_id: "1001"\n')
        bots = runtime._official_bots_config()
        self.assertEqual(len(bots), 1)
        self.assertEqual(bots[0]['id'], 'default')
        self.assertEqual(bots[0]['app_id'], '1001')
        self.assertIn('default', runtime.official_bots)

    def test_multiple_bots_from_list(self):
        runtime = self._runtime(
            'qq:\n  official_bots:\n'
            '    - {id: bot1, label: 客服, app_id: "2001"}\n'
            '    - {id: bot2, label: 助手, app_id: "2002"}\n')
        bots = {item['id']: item for item in runtime._official_bots_config()}
        self.assertEqual(set(bots), {'bot1', 'bot2'})
        self.assertEqual(bots['bot2']['label'], '助手')
        self.assertEqual(len(runtime.official_bots), 2)
        self.assertEqual(runtime.official, runtime.official_bots['bot1'])

    def test_route_by_message_source(self):
        runtime = self._runtime(
            'qq:\n  official_bots:\n'
            '    - {id: bot1, app_id: "2001"}\n'
            '    - {id: bot2, app_id: "2002"}\n')
        self.assertIs(runtime._official_for({'_official_bot': 'bot2'}),
                      runtime.official_bots['bot2'])
        self.assertIs(runtime._official_for({}), runtime.official)

    def test_status_lists_every_bot(self):
        runtime = self._runtime(
            'qq:\n  official_bots:\n'
            '    - {id: a, app_id: "1"}\n'
            '    - {id: b, app_id: "2"}\n')
        status = runtime.status()
        self.assertEqual([item['id'] for item in status['official_bots']], ['a', 'b'])


if __name__ == '__main__':
    unittest.main()
