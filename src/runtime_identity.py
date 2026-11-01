"""Local backend identity shared by the desktop UI and QQ service."""
from pathlib import Path
APP_VERSION = '5.2.103'
APP_ID='WhaleGirlQQ'

def backend_identity_error(identity,root):
    if not isinstance(identity,dict) or identity.get('app')!=APP_ID:
        return '8090端口上不是当前版本的鲸鱼娘后台。请关闭旧后台后重新打开控制台。'
    if identity.get('version')!=APP_VERSION:
        return f'界面为{APP_VERSION}，后台版本不一致。请关闭旧控制台和旧后台后重新打开。'
    try:
        matches=bool(identity.get('data_root')) and Path(identity['data_root']).resolve()==Path(root).resolve()
    except (ValueError,TypeError,OSError):
        matches=False
    if not matches:
        return '后台使用了其他目录的配置。请关闭旧后台，再从当前安装版启动。'
    return ''
