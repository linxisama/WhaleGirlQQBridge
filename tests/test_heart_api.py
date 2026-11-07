"""心情面板路由用例：/heart、/api/heart/state、/api/heart/action。

真的起 ASGI（TestClient），所以鉴权、状态码、JSON 形状都是端到端看到的。
默认用一个替身插件验证路由契约（不依赖插件包是否已安装）；最后一个用例才去
加载真实插件包，包不在就 skip。
"""

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src import heart_api  # noqa: E402

PLUGIN_DIR = Path('D:/插件/dsh-heart-qq')
CSRF = {heart_api.CSRF_HEADER: '1'}


class StubPlugin:
    """够路由用的替身：enabled / get / snapshot / apply_action。"""

    def __init__(self, enabled=True, panel=True):
        self.enabled = enabled
        self._settings = {'panel': panel}
        self.calls = []
        self.state = {'ok': True, 'clock': {'text': '此刻 22 点 · 晚上'}}
        self.reply = (True, '', self.state)
        self.crash = None

    def get(self, key, default=None):
        return self._settings.get(key, default)

    def snapshot(self):
        if self.crash:
            raise RuntimeError(self.crash)
        return self.state

    def apply_action(self, action):
        self.calls.append(action)
        if self.crash:
            raise RuntimeError(self.crash)
        return self.reply


class FakeManager:
    """只实现路由会碰的那几个口子。"""

    def __init__(self, page_dir=None, state_dir=None):
        self.state_dir = Path(state_dir) if state_dir else Path(tempfile.mkdtemp(prefix='heart-state-'))
        self._plugins = {}
        self._page_dir = page_dir

    def custom_mod_path(self, mod_id):
        return self._page_dir if self._page_dir else Path('__missing__')

    def get_settings(self, plugin_id):
        return {}


class HeartApiHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='heart-api-')
        self.tmp = Path(self._tmp.name)
        self.page_dir = self.tmp / 'dsh-heart-qq'
        (self.page_dir / 'web').mkdir(parents=True)
        (self.page_dir / 'web' / 'heart.html').write_text(
            '<!doctype html><title>心情系统 · 全景</title>', encoding='utf-8')
        self.manager = FakeManager(self.page_dir)
        self.plugin = StubPlugin()
        self.manager._plugins['dsh_heart'] = self.plugin
        self.app = FastAPI()
        self.app.state.bot = SimpleNamespace(plugins=self.manager)
        heart_api.register(self.app)
        self.client = TestClient(self.app)

    def tearDown(self):
        self._tmp.cleanup()

    def post(self, body, headers=CSRF):
        return self.client.post(heart_api.API_PREFIX + '/action', json=body, headers=headers)


class PageTest(HeartApiHarness):
    def test_page_comes_from_the_plugin_directory(self):
        res = self.client.get(heart_api.PAGE_PATH)
        self.assertEqual(res.status_code, 200)
        self.assertIn('心情系统', res.text)
        self.assertTrue(res.headers['content-type'].startswith('text/html'))

    def test_page_is_503_with_a_reason_when_the_bundle_is_incomplete(self):
        (self.page_dir / 'web' / 'heart.html').unlink()
        res = self.client.get(heart_api.PAGE_PATH)
        self.assertEqual(res.status_code, 503)
        self.assertIn('插件包不完整', res.text)

    def test_page_is_503_when_the_plugin_is_missing(self):
        self.manager._plugins.clear()
        res = self.client.get(heart_api.PAGE_PATH)
        self.assertEqual(res.status_code, 503)
        self.assertIn('没加载', res.text)


class StateTest(HeartApiHarness):
    def test_state_returns_the_snapshot(self):
        body = self.client.get(heart_api.API_PREFIX + '/state').json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['state']['clock']['text'], '此刻 22 点 · 晚上')

    def test_state_is_503_when_disabled(self):
        self.plugin.enabled = False
        res = self.client.get(heart_api.API_PREFIX + '/state')
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()['code'], 'unavailable')
        self.assertIn('停用', res.json()['message'])

    def test_state_is_503_when_the_panel_switch_is_off(self):
        self.plugin._settings['panel'] = False
        res = self.client.get(heart_api.API_PREFIX + '/state')
        self.assertEqual(res.status_code, 503)
        self.assertIn('面板开关', res.json()['message'])

    def test_state_is_500_when_the_snapshot_raises(self):
        self.plugin.crash = '磁盘炸了'
        res = self.client.get(heart_api.API_PREFIX + '/state')
        self.assertEqual(res.status_code, 500)
        self.assertEqual(res.json()['code'], 'state_failed')
        self.assertIn('磁盘炸了', res.json()['message'])


class ActionTest(HeartApiHarness):
    def test_action_forwards_the_whole_body(self):
        res = self.post({'action': 'value', 'key': 'happy', 'value': 66})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['ok'])
        self.assertEqual(self.plugin.calls, [{'action': 'value', 'key': 'happy', 'value': 66}])

    def test_missing_csrf_header_is_403(self):
        res = self.post({'action': 'value'}, headers={})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()['code'], 'csrf')
        self.assertEqual(self.plugin.calls, [])

    def test_empty_action_is_400(self):
        res = self.post({'action': '   '})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()['code'], 'bad_request')

    def test_broken_json_is_400_not_500(self):
        res = self.client.post(heart_api.API_PREFIX + '/action', content=b'{oops',
                               headers={**CSRF, 'content-type': 'application/json'})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()['code'], 'bad_request')

    def test_rejected_action_keeps_the_state_for_the_panel(self):
        self.plugin.reply = (False, 'unknown gauge', self.plugin.state)
        res = self.post({'action': 'value', 'key': 'nope', 'value': 1})
        self.assertEqual(res.status_code, 400)
        body = res.json()
        self.assertEqual(body['code'], 'rejected')
        self.assertEqual(body['message'], 'unknown gauge')
        self.assertIn('state', body)

    def test_action_is_500_when_the_plugin_raises(self):
        self.plugin.crash = '锁死了'
        res = self.post({'action': 'mode', 'key': 'cold', 'on': True})
        self.assertEqual(res.status_code, 500)
        self.assertEqual(res.json()['code'], 'action_failed')

    def test_action_is_503_when_the_plugin_is_missing(self):
        self.manager._plugins.clear()
        res = self.post({'action': 'value'})
        self.assertEqual(res.status_code, 503)


class GuardTest(HeartApiHarness):
    def test_cross_site_origin_is_403(self):
        res = self.client.get(heart_api.API_PREFIX + '/state',
                              headers={'origin': 'http://evil.example'})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()['code'], 'auth_denied')

    def test_same_origin_is_allowed(self):
        res = self.client.get(heart_api.API_PREFIX + '/state',
                              headers={'origin': 'http://testserver'})
        self.assertEqual(res.status_code, 200)

    def test_forwarded_header_is_403(self):
        res = self.client.get(heart_api.API_PREFIX + '/state',
                              headers={'x-forwarded-for': '10.0.0.9'})
        self.assertEqual(res.status_code, 403)
        self.assertIn('x-forwarded-for', res.json()['message'])

    def test_registering_twice_does_not_duplicate_routes(self):
        before = len(self.app.routes)
        heart_api.register(self.app)
        self.assertEqual(len(self.app.routes), before)

    def test_every_route_has_a_path(self):
        # include_router 会在 app.routes 里塞没有 .path 的占位符，冒烟测试会炸。
        for route in self.app.routes:
            self.assertTrue(hasattr(route, 'path'), route)


@unittest.skipUnless(PLUGIN_DIR.is_dir(), '插件包不在本机：%s' % PLUGIN_DIR)
class RealPluginTest(unittest.TestCase):
    """真插件包 + 替身管理器：验证路由与插件实现的接口对得上。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='heart-real-')
        self.tmp = Path(self._tmp.name)
        self.plugin = self._load_plugin()
        self.manager = FakeManager(PLUGIN_DIR)
        self.manager._plugins['dsh_heart'] = self.plugin
        self.app = FastAPI()
        self.app.state.bot = SimpleNamespace(plugins=self.manager)
        heart_api.register(self.app)
        self.client = TestClient(self.app)

    def tearDown(self):
        self._tmp.cleanup()

    def _load_plugin(self):
        source = PLUGIN_DIR / 'heart_qq.py'
        alias = 'src_custom_plugin_dsh_heart_qq_test'
        spec = importlib.util.spec_from_file_location(alias, source)
        module = importlib.util.module_from_spec(spec)
        module.__package__ = 'src.plugins'
        sys.modules[alias] = module
        spec.loader.exec_module(module)
        manager = FakeManager(self.tmp, state_dir=self.tmp / 'plugin_state')
        manager.get_settings = lambda pid: {'enabled': True, 'injectPrompt': True, 'panel': True}
        return module.HeartPlugin(manager)

    def test_state_has_the_fourteen_gauges(self):
        body = self.client.get(heart_api.API_PREFIX + '/state').json()
        self.assertTrue(body['ok'])
        state = body['state']
        self.assertEqual(len(state['gauges']), 14)
        self.assertEqual(len(state['dimensions']), 5)
        self.assertIn('clock', state)
        self.assertIn('gates', state)

    def test_page_is_served_from_the_real_bundle(self):
        res = self.client.get(heart_api.PAGE_PATH)
        self.assertEqual(res.status_code, 200)
        self.assertIn('心情系统', res.text)

    def test_setting_a_value_lands_on_disk(self):
        res = self.client.post(heart_api.API_PREFIX + '/action',
                               json={'action': 'value', 'key': 'happy', 'value': 88},
                               headers=CSRF)
        self.assertEqual(res.status_code, 200)
        value = next(g for g in res.json()['state']['gauges'] if g['key'] == 'happy')['value']
        self.assertAlmostEqual(value, 88.0, places=1)
        saved = json.loads((self.tmp / 'plugin_state' / 'dsh_heart' / 'state.json')
                           .read_text('utf-8'))
        self.assertAlmostEqual(saved['values']['happy'], 88.0, places=1)

    def test_unknown_gauge_is_400_with_state(self):
        res = self.client.post(heart_api.API_PREFIX + '/action',
                               json={'action': 'value', 'key': 'nope', 'value': 1},
                               headers=CSRF)
        self.assertEqual(res.status_code, 400)
        self.assertIn('state', res.json())


class ModHookIntegrationTest(unittest.TestCase):
    """真插件包 + 真 PluginManager：验证 mod 钩子路径下静态规则与动态心情都在。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='heart-mod-')
        self.tmp = Path(self._tmp.name)
        self.data = self.tmp / 'data'
        custom = self.data / 'custom_plugins'
        custom.mkdir(parents=True)
        target = custom / 'dsh-heart-qq'
        target.mkdir()
        shutil.copy2(PLUGIN_DIR / 'heart_qq.py', target / 'heart_qq.py')
        shutil.copy2(PLUGIN_DIR / 'plugin.json', target / 'plugin.json')
        shutil.copytree(PLUGIN_DIR / 'web', target / 'web')
        from src.plugins.manager import PluginManager
        self.manager = PluginManager(self.data, custom_plugins_dir=custom)
        self.plugin = self.manager._plugins['dsh_heart']
        self.runtime = SimpleNamespace(note=lambda msg: None)

    def tearDown(self):
        self._tmp.cleanup()

    def prompt(self, text='你今天真好看'):
        hint = self.manager.run_on_pre_ai({'text': text}, [], self.runtime)
        return hint

    def test_static_rule_and_dynamic_state_both_reach_the_prompt(self):
        hint = self.prompt()
        text = (hint['extra_system'] or '') + (hint['volatile_system'] or '')
        self.assertIn('## 心情', text)
        self.assertIn('## 当前心情', text)
        # 静态在前：前缀稳定，缓存才命得中。
        self.assertLess(text.index('## 心情'), text.index('## 当前心情'))
        # 两段之间不能夹着别的插件的字，否则前缀每轮都变。
        between = text[text.index('## 心情'):text.index('## 当前心情')]
        self.assertEqual(between.count('##'), 1)

    def test_master_switch_off_sends_nothing(self):
        self.plugin.apply_action({'action': 'mode', 'key': 'master', 'on': False})
        text = self.prompt()['extra_system'] + self.prompt()['volatile_system']
        # 其它内置插件也会往 extra_system 里写，这里只认自己的两段。
        self.assertNotIn('## 心情', text)
        self.assertNotIn('## 当前心情', text)

    def test_inject_prompt_off_sends_nothing(self):
        self.manager.update_settings('dsh_heart', {'injectPrompt': False})
        text = self.prompt()['extra_system'] + self.prompt()['volatile_system']
        self.assertNotIn('## 心情', text)
        self.assertNotIn('## 当前心情', text)

    def test_sleeping_state_shows_up_in_the_prompt(self):
        self.plugin.apply_action({'action': 'sleep', 'act': 'sleep'})
        text = self.prompt()['extra_system']
        self.assertIn('你睡着了', text)

    def test_praise_moves_happy_up(self):
        before = self.plugin.snapshot()['gauges'][0]['value']
        self.prompt('你太棒了，好喜欢你好厉害')
        after = self.plugin.snapshot()['gauges'][0]['value']
        self.assertGreater(after, before)

    def test_status_command_answers_with_one_line(self):
        line = self.plugin.on_ingest({'text': '/心情'}, self.runtime)
        self.assertTrue(line.startswith('🫀 '), line)
        self.assertIn('此刻', line)
        self.assertNotIn('\n', line)

    def test_sleeping_is_woken_by_wake_words(self):
        self.plugin.apply_action({'action': 'sleep', 'act': 'sleep'})
        self.plugin.on_ingest({'text': '起床啦，别睡了'}, self.runtime)
        self.assertEqual(self.plugin.snapshot()['sleep']['state'], 'awake')

    def test_ordinary_message_does_not_wake_her(self):
        self.plugin.apply_action({'action': 'sleep', 'act': 'sleep'})
        self.plugin.on_ingest({'text': '今天天气不错'}, self.runtime)
        self.assertEqual(self.plugin.snapshot()['sleep']['state'], 'sleeping')

    def test_worst_case_still_fits_host_plugin_cap(self):
        """宿主给插件块 800 字硬顶（qq_app.py:2511 起 compose 截断）。

        截断是从尾部切的，最坏情况（多个值偏离 + 三个模式 + 在忙）一旦超了，
        先丢的就是动态段末尾（在忙、模式），而静态规则还占着前缀 —— 面板看着
        正常、模型却收不到当前心情。所以按最坏情况守住这个上限。
        """
        from src.prompt_budget import compose

        state = self.plugin._load()
        module = sys.modules[type(self.plugin).__module__]
        for key, value in (('anger', 72), ('fatigue', 61), ('happy', 88),
                           ('depress', 47), ('affection', 55), ('libido', 66),
                           ('jealous', 58), ('quarrel', 44), ('sad', 39)):
            module.set_value(state, key, value)
        for key in ('cold', 'love', 'lust'):
            module.set_mode(state, key, True)
        module.set_busy(state, '写代码修 bug，还要回一堆群消息')
        self.plugin._save()

        hint = self.prompt()
        text = '\n'.join(str(hint.get(k) or '') for k in ('extra_system', 'volatile_system')).strip()
        block, stats = compose([('插件', text, 800, 1)], limit=6000)
        self.assertFalse(stats['blocks'][0]['truncated'], '插件块被宿主截断，动态段会丢尾巴')
        self.assertIn('## 心情', block)
        self.assertIn('## 当前心情', block)
        self.assertIn('别当借口', block)


if __name__ == '__main__':
    unittest.main()