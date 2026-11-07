"""Adapt full panel API paths to the existing console's relative-path callbacks."""


class ConsoleApiError(RuntimeError):
    pass


class ConsoleApiClient:
    def __init__(self, get, post):
        self._get, self._post = get, post
        self._last_error = ''

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
            # detail 为空（None / ''）时说明后端只是把字段带出来了，不是失败原因；
            # 早先这里会退化成「请求未完成」，把一次正常返回报成错误。
            detail = result.get('detail') or result.get('message')
            if detail is None or detail == '':
                if result.get('success') is False:
                    detail = '请求未完成'
                else:
                    return result
            if isinstance(detail, dict):
                detail = detail.get('message') or detail.get('code') or '请求参数不正确'
            if not isinstance(detail, str):
                detail = '请求参数不正确，请检查输入'
            raise ConsoleApiError(detail[:240])
        return result

    def last_error(self):
        """最近一次失败的文案；成功后清空。界面用它做状态行，不必自己捕获异常。"""
        return self._last_error

    def _call(self, fn, path, payload=None):
        try:
            result = fn(self.relative(path), payload) if payload is not None else fn(self.relative(path))
        except ConsoleApiError as error:
            self._last_error = str(error)
            raise
        except Exception as error:
            self._last_error = '%s: %s' % (type(error).__name__, error)
            raise ConsoleApiError(self._last_error[:240]) from None
        try:
            checked = self.checked(result)
        except ConsoleApiError as error:
            # checked() 抛出的才是后端真正的原因（令牌过期、参数不对），
            # 比传输层异常更该被界面读到，所以同样记进 last_error。
            self._last_error = str(error)
            raise
        self._last_error = ''
        return checked

    def get(self, path):
        return self._call(self._get, path)

    def post(self, path, data=None):
        # Probes are bounded to two seconds on the server; synthesis stays queued.
        return self._call(self._post, path, data or {})