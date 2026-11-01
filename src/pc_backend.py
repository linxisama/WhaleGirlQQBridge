"""Retired host-control interface; rejects legacy calls."""
APPS={}
def allowed_apps(runtime,raw):return {}
class WindowsBackend:
    def execute(self,*args):raise RuntimeError('宿主电脑操作已移除；请使用机器人控制台管理')
