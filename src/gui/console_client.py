"""Adapt full panel API paths to the existing console's relative-path callbacks."""


class ConsoleApiError(RuntimeError):
    pass


class ConsoleApiClient:
    def __init__(self, get, post):
        self._get, self._post = get, post

    @staticmethod
    def relative(path):
        if not isinstance(path, str) or not path.startswith('/api/'):
            raise ConsoleApiError('控制台接口路径无效')
        return path[len('/api/'):]

    @staticmethod
    def checked(result):
        if not isinstance(result, dict) or not result:
            raise ConsoleApiError('后台没有返回有效结果，请确认后台已连接')
        if result.get('success') is False or 'detail' in result:
            detail = result.get('detail') or result.get('message') or '请求未完成'
            if isinstance(detail, dict):
                detail = detail.get('message') or detail.get('code') or '请求参数不正确'
            if not isinstance(detail, str):
                detail = '请求参数不正确，请检查输入'
            raise ConsoleApiError(detail[:240])
        return result

    def get(self, path):
        return self.checked(self._get(self.relative(path)))

    def post(self, path, data=None):
        # Probes are bounded to two seconds on the server; synthesis stays queued.
        return self.checked(self._post(self.relative(path), data or {}))
