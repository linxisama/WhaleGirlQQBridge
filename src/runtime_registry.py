"""多账号切片 2：同一进程内并发运行多个账号（每账号一个独立后台）。

做法：**每个账号一个自己的 HTTP 端口**，而不是在一个应用里塞多套运行时。
这样路由、插件、数据库天然隔离，不用改 60 多个接口的签名。

- 账号 0（默认账号）继续占 8090，保持旧行为兼容；
- 其余账号按顺序分配 8091、8092…（被占用就往后找）；
- 每个端口上跑一个独立 `create_app(QQRuntime(config, db))`，线程内 uvicorn；
- 停止某账号只关它自己的服务器，其它账号不受影响。
"""
from __future__ import annotations

import socket
import threading
from pathlib import Path

__all__ = ['RuntimeRegistry', 'AccountServer']


def _free_port(start: int = 8091) -> int:
    for port in range(start, start + 100):
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', port))
            except OSError:
                continue
            return port
    # Windows 可能把整段端口保留给 Hyper-V/WSL（netsh int ipv4 show excludedportrange
    # 能看到），这时退回系统临时端口，别让「端口段被系统占用」变成账号起不来。
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return int(probe.getsockname()[1])


class AccountServer:
    """一个账号的后台（线程内 uvicorn）。"""

    def __init__(self, account_id: str, port: int, config_path: Path, db_path: Path, root: Path):
        self.account_id = account_id
        self.port = port
        self.config_path = config_path
        self.db_path = db_path
        self.root = root
        self.thread = None
        self.server = None
        self.error = ''

    @property
    def running(self) -> bool:
        return bool(self.server and getattr(self.server, 'started', False)
                    and self.thread and self.thread.is_alive())

    def start(self) -> 'AccountServer':
        if self.running:
            return self
        import uvicorn

        from .qq_app import QQRuntime, create_app

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # 账号目录下的配置：插件状态/人设图片都放进该账号自己的目录
        data_root = None
        try:
            if 'accounts' in self.config_path.parts:
                data_root = self.config_path.parent / 'data' / 'qq-only'
        except Exception:
            data_root = None
        runtime = QQRuntime(root=self.root, config_path=self.config_path,
                            db_path=self.db_path, data_root=data_root)
        application = create_app(runtime)
        application.state.windowed = True
        self.server = uvicorn.Server(uvicorn.Config(application, host='127.0.0.1',
                                                    port=self.port, log_level='warning',
                                                    log_config=None))
        def _serve():
            try:
                self.server.run()
            except BaseException as exc:      # 线程里抛错没人接：记下来，界面才能说明原因
                import traceback
                self.error = f'{type(exc).__name__}: {exc}'
                traceback.print_exc()

        self.thread = threading.Thread(target=_serve, name=f'account-{self.account_id}',
                                       daemon=True)
        self.thread.start()
        # 等 uvicorn 真正 started：Server.started 是启动完成后才置位的，
        # 立刻返回会让调用方以为失败了（测试和界面都依赖"起来就能用"）。
        import time as _time
        deadline = _time.monotonic() + 20
        while _time.monotonic() < deadline:
            if getattr(self.server, 'started', False):
                break
            if not self.thread.is_alive():
                self.error = '后台线程提前退出'
                break
            _time.sleep(0.1)
        return self

    def stop(self, timeout: float = 8.0) -> None:
        if self.server is not None:
            try:
                self.server.should_exit = True
            except Exception:
                pass
        if self.thread is not None:
            self.thread.join(timeout=timeout)
        self.server = None
        self.thread = None

    def as_dict(self) -> dict:
        return {'account_id': self.account_id, 'port': self.port, 'running': self.running,
                'url': f'http://127.0.0.1:{self.port}', 'error': self.error}


class RuntimeRegistry:
    """账号 → 后台服务器。默认账号用 8090，其余自动分配端口。"""

    def __init__(self, root, book):
        self.root = Path(root)
        self.book = book
        self.servers: dict[str, AccountServer] = {}

    def _port_for(self, account_id: str) -> int:
        if account_id == 'default':
            return 8090
        used = {server.port for server in self.servers.values()}
        port = _free_port()
        while port in used:
            port = _free_port(port + 1)
        return port

    def ensure(self, account_id: str) -> AccountServer:
        """确保该账号的后台在跑（已在跑就直接返回）。"""
        if account_id in self.servers and self.servers[account_id].running:
            return self.servers[account_id]
        config_path, db_path = self.book.paths(account_id)
        server = AccountServer(account_id, self._port_for(account_id), config_path, db_path,
                               self.root)
        self.servers[account_id] = server
        try:
            server.start()
        except Exception as error:
            server.error = f'{type(error).__name__}: {error}'
        return server

    def stop(self, account_id: str) -> bool:
        server = self.servers.pop(account_id, None)
        if server is None:
            return False
        server.stop()
        return True

    def url(self, account_id: str) -> str:
        server = self.servers.get(account_id)
        return server.as_dict()['url'] if server else ''

    def status(self) -> list:
        return [server.as_dict() for server in self.servers.values()]

    def close(self) -> None:
        for account_id in list(self.servers):
            self.stop(account_id)
