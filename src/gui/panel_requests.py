"""Panel I/O runs without widget access; results are applied by a GUI-thread timer."""
from concurrent.futures import ThreadPoolExecutor

from PyQt6.QtCore import QObject, QTimer


class PanelRequests(QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='console-panel')
        self._pending = None
        self._closed = False
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self._poll)

    @property
    def busy(self):
        return self._pending is not None

    def start(self, work, apply, error):
        if self._closed or self.busy:
            return False
        self._pending = (self._pool.submit(work), apply, error)
        self.timer.start()
        return True

    def _poll(self):
        if self._pending is None or not self._pending[0].done():
            return
        future, apply, error = self._pending
        self._pending = None
        self.timer.stop()
        try:
            result = future.result()
        except Exception as exc:
            error(exc)
            return
        try:
            apply(result)
        except Exception as exc:
            error(exc)

    def close(self):
        # Caller must keep the panel alive until its last result is delivered.
        if self.busy:
            return False
        self.timer.stop()
        self._closed = True
        self._pool.shutdown(wait=True)
        return True
