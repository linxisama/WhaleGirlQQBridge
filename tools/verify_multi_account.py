"""多账号并发回归（不依赖真实 QQ 号）：两个 mock 账号同时在线，逐项验证隔离。

做的事：
1. 临时目录建一个账号簿，放两个账号（配置为 mock 模式，不连 QQ）；
2. 用 RuntimeRegistry 同时起两个后台（各自端口）；
3. 验证：端口不同、都能响应、数据库文件不同、插件目录不同、
   在一个账号的插件目录写文件不会出现在另一个账号、停掉一个后另一个仍响应；
4. 全过则输出 PASS，并清理临时目录。

用法：`.venv-build\\Scripts\\python.exe tools\\verify_multi_account.py`
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from src.account_book import AccountBook  # noqa: E402
from src.qq_app import QQRuntime  # noqa: E402
from src.runtime_registry import RuntimeRegistry  # noqa: E402

CONFIG = """ai:
  model: demo
qq:
  mode: mock
qq_only:
  mode: mock
  auto_start: false
"""


def wait_status(port, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = requests.get(f'http://127.0.0.1:{port}/api/status', timeout=2,
                                    proxies={'http': None, 'https': None, 'all': None})
            if response.status_code == 200:
                return response.json()
        except Exception:
            pass
        time.sleep(0.2)
    return None


def main() -> int:
    report = {'checks': [], 'ok': True}

    def check(name, condition, detail=''):
        report['checks'].append({'name': name, 'ok': bool(condition), 'detail': detail})
        if not condition:
            report['ok'] = False

    with tempfile.TemporaryDirectory(prefix='multi-account-') as folder:
        root = pathlib.Path(folder)
        (root / 'data' / 'qq-only').mkdir(parents=True)
        (root / 'config.qq.yaml').write_text(CONFIG, encoding='utf-8')
        book = AccountBook(root)
        first = book.add('账号A')
        second = book.add('账号B')
        registry = RuntimeRegistry(root, book)
        try:
            server_a = registry.ensure(first['id'])
            server_b = registry.ensure(second['id'])
            check('两个账号端口不同', server_a.port != server_b.port,
                  f"{server_a.port} vs {server_b.port}")
            status_a = wait_status(server_a.port)
            status_b = wait_status(server_b.port)
            check('账号A后台可响应', status_a is not None)
            check('账号B后台可响应', status_b is not None)

            db_a = pathlib.Path(book.paths(first['id'])[1]).resolve()
            db_b = pathlib.Path(book.paths(second['id'])[1]).resolve()
            check('数据库文件不同', db_a != db_b, f'{db_a.name} vs {db_b.name}')

            config_a, _ = book.paths(first['id'])
            runtime_a = QQRuntime(root=root, config_path=config_a,
                                  db_path=db_a,
                                  data_root=config_a.parent / 'data' / 'qq-only')
            config_b, _ = book.paths(second['id'])
            runtime_b = QQRuntime(root=root, config_path=config_b,
                                  db_path=db_b,
                                  data_root=config_b.parent / 'data' / 'qq-only')
            dir_a = pathlib.Path(runtime_a.plugins.data_dir).resolve()
            dir_b = pathlib.Path(runtime_b.plugins.data_dir).resolve()
            check('插件目录不同', dir_a != dir_b, f'{dir_a} vs {dir_b}')
            marker = dir_a / 'isolation-probe.json'
            marker.write_text('{"from": "A"}', encoding='utf-8')
            check('写A不影响B', not (dir_b / 'isolation-probe.json').exists())
            runtime_a.db.close()
            runtime_b.db.close()

            registry.stop(first['id'])
            check('停A后A已停止', not server_a.running)
            check('停A不影响B', wait_status(server_b.port) is not None)
        finally:
            registry.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
