"""零配置首启：告警/日志必须打印真正被读取的那个文件名。

背景：首次运行把 source 回落成 root/config.yaml，于是 config_manager.py:311-312 打出
「配置文件 …\\config.yaml 不存在，使用默认配置」，而程序实际用 config.qq.yaml、
保存后生成的也是 config.qq.yaml —— 第一条日志就把人带偏。
"""
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from loguru import logger

from src import qq_app
from src.config_manager import ConfigManager


class _Sink:
    def __init__(self):
        self.messages = []

    def write(self, message):
        self.messages.append(str(message).strip())


@contextmanager
def capture(level='DEBUG'):
    """接住 loguru 的日志行（config_manager 与 qq_app 都用 loguru）。"""
    sink = _Sink()
    handler = logger.add(sink, level=level, format='{message}')
    try:
        yield sink.messages
    finally:
        logger.remove(handler)


def write_config(path, model):
    path.write_text(f'ai:\n  model: {model}\nqq:\n  mode: mock\n', encoding='utf-8')


class ResolveConfigFileTests(unittest.TestCase):
    def test_first_run_points_at_config_qq_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(qq_app.resolve_config_file(root), root / 'config.qq.yaml')

    def test_existing_config_qq_yaml_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root / 'config.qq.yaml', 'modern')
            self.assertEqual(qq_app.resolve_config_file(root), root / 'config.qq.yaml')

    def test_legacy_config_yaml_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root / 'config.yaml', 'legacy')
            self.assertEqual(qq_app.resolve_config_file(root), root / 'config.yaml')

    def test_config_qq_yaml_wins_over_legacy_config_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root / 'config.qq.yaml', 'modern')
            write_config(root / 'config.yaml', 'legacy')
            root_path = qq_app.resolve_config_file(root)
            self.assertEqual(root_path, root / 'config.qq.yaml')
            self.assertEqual(ConfigManager(str(root_path)).load('default')['ai']['model'], 'modern')

    def test_explicit_config_path_wins_over_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root / 'config.yaml', 'legacy')
            chosen = root / 'mine.yaml'
            self.assertEqual(qq_app.resolve_config_file(root, chosen), chosen)


class FirstRunWarningTests(unittest.TestCase):
    def test_warning_names_the_file_that_will_be_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with capture() as messages:
                ConfigManager(str(qq_app.resolve_config_file(root))).load('default')
            missing = [line for line in messages if '不存在' in line]
            self.assertTrue(missing, '首启要有一条"配置文件不存在"的告警')
            self.assertIn('config.qq.yaml', missing[0])
            self.assertIn('会创建', missing[0], '告警要顺带说清保存后生成的就是这个文件')
            self.assertNotIn(str(root / 'config.yaml'), missing[0],
                             '告警不能再指着程序从不使用的 config.yaml')

    def test_save_writes_the_file_the_warning_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = ConfigManager(str(qq_app.resolve_config_file(root)))
            config.load('default')
            config.save()
            self.assertTrue((root / 'config.qq.yaml').is_file())
            self.assertFalse((root / 'config.yaml').exists())

    def test_legacy_load_logs_which_file_it_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root / 'config.yaml', 'legacy')
            source = qq_app.resolve_config_file(root)
            with capture() as messages:
                loaded = ConfigManager(str(source)).load('default')
            self.assertEqual(loaded['ai']['model'], 'legacy')
            loaded_lines = [line for line in messages if 'config.yaml' in line]
            self.assertTrue(loaded_lines)
            self.assertIn('config.yaml', loaded_lines[0])


class RuntimeFirstRunTests(unittest.TestCase):
    """真正造一个 QQRuntime：日志里的文件名就是用户首启第一条日志。"""

    def setUp(self):
        # sqlite 句柄在 Windows 上会挡住 TemporaryDirectory 的清理，用 mkdtemp 自己删。
        self.root = Path(tempfile.mkdtemp(prefix='qq-firstrun-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.bot = None
        self.addCleanup(self._close)

    def _close(self):
        if self.bot is not None:
            try:
                self.bot.db.close()
            except Exception:
                pass

    def test_runtime_first_run_warns_about_config_qq_yaml(self):
        with capture() as messages:
            self.bot = qq_app.QQRuntime(root=self.root, db_path=self.root / 'messages.sqlite')
        self.assertEqual(self.bot.config.config_path, self.root / 'config.qq.yaml')
        missing = [line for line in messages if '不存在' in line]
        self.assertTrue(missing, '首启要有"配置文件不存在"的告警')
        self.assertIn('config.qq.yaml', missing[0])
        self.assertNotIn(str(self.root / 'config.yaml'), missing[0])

    def test_runtime_reads_legacy_config_yaml_and_says_so(self):
        write_config(self.root / 'config.yaml', 'legacy-model')
        with capture() as messages:
            self.bot = qq_app.QQRuntime(root=self.root, db_path=self.root / 'messages.sqlite')
        self.assertEqual(self.bot.config.get('ai.model'), 'legacy-model',
                         '旧用户目录里的 config.yaml 还要能读到')
        self.assertEqual(self.bot.config.config_path, self.root / 'config.qq.yaml',
                         '新设置仍保存到 config.qq.yaml')
        notice = [line for line in messages if '旧版' in line]
        self.assertTrue(notice, '读了旧版文件就要说清读的是哪个')
        self.assertIn('config.yaml', notice[0])
        self.assertIn('config.qq.yaml', notice[0])

    def test_runtime_uses_the_shared_resolver(self):
        import inspect
        self.assertIn('resolve_config_file(', inspect.getsource(qq_app.QQRuntime.__init__))


if __name__ == '__main__':
    unittest.main()
