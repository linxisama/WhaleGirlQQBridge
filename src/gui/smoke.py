"""Offline packaged runtime smoke check; never connects to QQ or the model API."""
import asyncio
import json
from .qt_locale import (ensure_chinese, should_use_native_dialogs,
                        translation_status)
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

def check_native_resize_edges(window):
    """验证真正接收鼠标的可见边缘，不能只验证程序调用 resize()。"""
    import ctypes
    import threading
    from ctypes import wintypes
    from PyQt6.QtCore import QAbstractNativeEventFilter
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.WindowFromPoint.argtypes = [wintypes.POINT]
    user.WindowFromPoint.restype = wintypes.HWND
    user.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user.ClientToScreen.restype = wintypes.BOOL
    user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.SendMessageW.restype = ctypes.c_ssize_t
    user.PostMessageW.argtypes = user.SendMessageW.argtypes
    user.PostMessageW.restype = wintypes.BOOL
    user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user.SetWindowPos.restype = wintypes.BOOL
    hwnd = int(window.winId())
    # 只让隔离自检窗口暂时置顶，排除其他测试窗口遮挡；不操作用户控制台。
    user.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x13)
    QTest.qWait(60)
    class SizingEvents(QAbstractNativeEventFilter):
        def __init__(self):
            super().__init__()
            self.events = []
        def nativeEventFilter(self, event_type, message):
            msg = wintypes.MSG.from_address(int(message))
            if msg.hWnd == hwnd and msg.message in (0x231, 0x232):
                self.events.append(msg.message)
            return False, 0
    tracker = SizingEvents()
    app = QApplication.instance()
    app.installNativeEventFilter(tracker)
    results = []
    width, height = window.width(), window.height()
    inset = window.layout().contentsMargins().left() + 3
    points = ((inset, height//2, 10), (width-inset, height//2, 11),
              (width//2, inset, 12), (width//2, height-inset, 15),
              (inset, inset, 13), (width-inset, inset, 14),
              (inset, height-inset, 16), (width-inset, height-inset, 17))
    try:
        for x, y, expected in points:
            dpi = window.devicePixelRatioF()
            point = wintypes.POINT(round(x*dpi), round(y*dpi))
            assert user.ClientToScreen(hwnd, ctypes.byref(point))
            assert user.WindowFromPoint(point) == hwnd, (x, y, '鼠标穿透或被遮挡')
            packed = (point.x & 0xffff) | ((point.y & 0xffff) << 16)
            hit = user.SendMessageW(hwnd, 0x84, 0, packed)
            assert hit == expected, (x, y, hit, expected)
            tracker.events.clear()
            def cancel():
                user.PostMessageW(hwnd, 0x1f, 0, 0)  # WM_CANCELMODE：结束隔离窗口的缩放循环。
                user.PostMessageW(hwnd, 0x202, 0, 0)
            timer = threading.Timer(.10, cancel)
            timer.start()
            try:
                user.SendMessageW(hwnd, 0xa1, hit, packed)  # WM_NCLBUTTONDOWN：真实原生边缘按下路径。
            finally:
                timer.join(timeout=1)
            assert tracker.events == [0x231, 0x232], tracker.events
            results.append({'hit': hit, 'mouse_target': True, 'native_sizing_loop': True})
    finally:
        app.removeNativeEventFilter(tracker)
        user.SetWindowPos(hwnd, wintypes.HWND(-2), 0, 0, 0, 0, 0x13)
    return results


def check_window_resize(window, app, native):
    """在实际显示后检查，离屏 resize 不能覆盖 Windows 的原生高度约束。"""
    from PyQt6.QtTest import QTest
    from src.runtime_identity import APP_VERSION
    QTest.qWait(80)
    assert window.title_bar.isHidden() == window.safe_mode
    assert APP_VERSION in window.windowTitle()
    sizes = []
    for index in range(window.tabs.count()):
        window.tabs.setCurrentIndex(index)
        for width, height in ((900, 600), (1100, 780), (800, 500)):
            window.resize(width, height)
            QTest.qWait(30)
            actual = (window.width(), window.height())
            assert actual == (width, height), (index, (width, height), actual)
        sizes.append({'tab': window.tabs.tabText(index), 'size': actual})
    window.tabs.setCurrentWidget(window.console_tab)
    window.showMaximized()
    QTest.qWait(60)
    assert window.isMaximized()
    window.showNormal()
    window.resize(900, 600)
    QTest.qWait(60)
    assert (window.width(), window.height()) == (900, 600)
    assert window.title_bar.isVisible() != window.safe_mode
    if native:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        rect = window.frameGeometry()
        # WM_SIZING 与真实拖底边走同一约束路径，必须允许只改变高度。
        target = wintypes.RECT(rect.x(), rect.y(), rect.x()+rect.width(), rect.y()+rect.height()-40)
        wanted = (target.left, target.top, target.right, target.bottom)
        user32.SendMessageW(int(window.winId()), 0x214, 6, ctypes.addressof(target))
        assert (target.left, target.top, target.right, target.bottom) == wanted
    app.processEvents()
    return sizes


async def check_packaged_ingest(runtime):
    """Exercise actual event receipt with no optional plugins, AI or QQ sends."""
    original_plugins = runtime.plugins._plugins
    original_mode, original_running = runtime.mode, runtime.running
    try:
        runtime.plugins._plugins = {}
        runtime.config.set('qq_only.mode', 'qqbridge')
        runtime.running = True
        for kind in ('private', 'group'):
            message = {'id': 'packaged_ingest_' + kind, 'kind': kind,
                       'target': '900001', 'sender_id': '900002',
                       'text': '隔离入站验收', 'at': True, 'media': []}
            await runtime._bridge_on_event(message)
            await runtime._bridge_on_event(message)
        rows = runtime.db.execute("SELECT payload,state FROM inbox WHERE mode='qqbridge'").fetchall()
        probes = [row for row in rows if json.loads(row[0]).get('id', '').startswith('packaged_ingest_')]
        assert len(probes) == 2, '直连私聊和群消息未能各入队一次（请检查缺失模块或入站错误）'
        assert all(row[1] == 'pending' for row in probes), '隔离消息入队状态异常'
        return {'private_queued': True, 'group_queued': True, 'deduplicated': True,
                'real_messages_sent': 0, 'model_requests': 0}
    finally:
        runtime.plugins._plugins = original_plugins
        runtime.config.set('qq_only.mode', original_mode)
        runtime.running = original_running


async def check_packaged_reply(runtime):
    """冻结包必须走到生成和发送边界；只入队无法发现可选插件的硬依赖。"""
    from unittest.mock import AsyncMock
    from src.plugin_catalog import BASIC_PLUGINS
    original_plugins=runtime.plugins._plugins
    original_mode,original_running=runtime.mode,runtime.running
    original_online,original_self=runtime.bridge.online,runtime.bridge.self_id
    worker=None
    try:
        runtime.plugins._plugins={k:v for k,v in original_plugins.items() if k in BASIC_PLUGINS}
        runtime.config.set('qq_only.mode','qqbridge')
        runtime.running=True;runtime.bridge.online=True;runtime.bridge.self_id='900000'
        # 上一项入队探针已经验收，隔离到本项之外，避免它们影响本项计数。
        runtime.db.execute("UPDATE inbox SET state='verified' WHERE json_extract(payload,'$.id') LIKE 'packaged_ingest_%'")
        runtime.db.commit()
        sent=[]
        async def send(message,text,meta=None):
            sent.append((message['id'],text));return {'message_id':'offline-'+message['id']}
        answers=['你好，私聊文字通道测试成功。','群聊点名可以进入完整处理流程。',
                 '收到语音转写内容，今天记得带伞。','这句来自群聊语音，晚些时候再聊吧。']
        model=AsyncMock(side_effect=lambda *a,**k:answers[len(sent)])
        with patch.object(runtime.ai,'chat',model),patch.object(runtime.bridge,'send',send):
            worker=asyncio.create_task(runtime._work())
            for index,(kind,voice) in enumerate((('private',False),('group',False),('private',True),('group',True))):
                message={'id':'packaged_reply_'+str(index),'kind':kind,'target':str(910000+index),
                         'sender_id':'900002','text':'隔离问题'+str(index),'at':True,'media':[]}
                if voice:
                    message.update(text='[语音]',media=[{'type':'record','asr_refer_text':'语音测试'+str(index)}])
                runtime.ingest(message,'qqbridge')
                before=len(sent);deadline=asyncio.get_running_loop().time()+8
                while len(sent)==before and asyncio.get_running_loop().time()<deadline:
                    await asyncio.sleep(.02)
                states=runtime.db.execute('SELECT state,error FROM inbox').fetchall()
                assert len(sent)==before+1, '安装包入队后没有完成回复：'+repr(states)+'；'+repr(runtime.decisions[-3:])+'；'+repr(runtime.events[-3:])
            assert model.await_count==4 and len(sent)==4
            assert [text for _,text in sent]==answers, '安装包返回了兜底提示而非测试答案'
        return {'private_text':True,'group_at_text':True,'private_supplied_asr':True,
                'group_at_supplied_asr':True,'model_requests_mocked':4,'real_messages_sent':0}
    finally:
        runtime.running=False
        if worker:
            worker.cancel();await asyncio.gather(worker,return_exceptions=True)
        runtime.plugins._plugins=original_plugins
        runtime.config.set('qq_only.mode',original_mode);runtime.running=original_running
        runtime.bridge.online, runtime.bridge.self_id=original_online,original_self


def check_plugin_import(root):
    """冻结版也实际导入 BOM/中文 ZIP、启用、调用、重启与失败回滚。"""
    import zipfile
    from src import plugin_installer as installer
    from src.plugin_install_service import finish_install, PluginLoadError
    from src.plugins.manager import PluginManager
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    code = '''from src.plugins import PluginBase, PluginMeta, FieldSchema
class ImportedProbe(PluginBase):
    META = PluginMeta(id='packaged_import_probe', name='导入自检', description='隔离验证',
        config_schema={'enabled': FieldSchema('bool','启用'), 'suffix': FieldSchema('string','后缀')},
        default_settings={'enabled': False, 'suffix': '甲'})
    async def on_post_reply(self, message, reply, runtime):
        return reply + self.get('suffix', '')
'''
    source = root / '中文插件（导入自检）.zip'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('plugin.json', '{"entry":"main.py"}')
        archive.writestr('main.py', code.encode('utf-8-sig'))
    with patch('src.plugins.manager.PLUGIN_MODULES', []):
        manager = PluginManager(root / 'runtime')
        assert installer.inspect_local(source)['plugin_id'] == 'packaged_import_probe'
        result = installer.install_from_file(source, manager.custom_plugins_dir, confirmed=True)
        finish_install(manager, result, enable=True)
        manager.update_settings('packaged_import_probe', {'suffix': '【导入生效】'})
        assert asyncio.run(manager.run_on_post_reply({}, '测试', None)) == '测试【导入生效】'
        manager = PluginManager(manager.data_dir)
        assert manager.get_settings('packaged_import_probe')['suffix'] == '【导入生效】'
        assert manager._plugins['packaged_import_probe'].enabled
        bad = root / '无效更新.py'
        bad.write_text(code + "\nraise RuntimeError('测试失败回滚')\n", encoding='utf-8')
        result = installer.install_from_file(bad, manager.custom_plugins_dir, confirmed=True, overwrite=True)
        try:
            finish_install(manager, result, enable=True)
        except PluginLoadError:
            pass
        else:
            raise AssertionError('无效插件更新被误报为成功')
        assert asyncio.run(manager.run_on_post_reply({}, '恢复', None)) == '恢复【导入生效】'
    report = {'imported': True, 'enabled': True, 'called': True, 'restarted': True, 'rollback': True}
    # 仅在显式离线验收时，按用户指定的本地市场目录检查真实包。每包使用独立数据目录。
    market = os.environ.get('WHALEGIRL_SELFTEST_PLUGIN_MARKET', '').strip()
    if market:
        packages = []
        for index, source in enumerate(sorted(Path(market).glob('*.zip'))):
            info = installer.inspect_local(source)
            if not info['ok']:
                packages.append({'file': source.name, 'id': info['plugin_id'], 'skipped': info['problems']})
                continue
            manager = PluginManager(root / ('market-' + str(index)))
            result = installer.install_from_file(source, manager.custom_plugins_dir, confirmed=True)
            finish_install(manager, result)
            restarted = PluginManager(manager.data_dir)
            assert info['plugin_id'] in restarted._plugins, source.name
            packages.append({'file': source.name, 'id': info['plugin_id'], 'loaded': True, 'restarted': True})
        assert packages, '市场验收目录为空'
        report['market'] = packages
    return report


def selfcheck_root(data_root=None):
    """自检专用临时目录：放在可写数据根下，with 退出时删除。

    冻结模式下 gui.BUNDLE 与 __file__ 都在 _MEIPASS 解包区（装到 Program Files
    时只读）：自检的运行期数据（config / db / agent_workspace 等）一律不能写在那里，
    否则安装目录只读时会静默失败，而自检仍然报 ok=true。
    """
    if data_root is None:
        from ..qq_app import DATA_ROOT
        data_root = DATA_ROOT
    return tempfile.TemporaryDirectory(prefix='qq-selfcheck-', dir=str(data_root))


def run(gui, output, *, native=False):
    if not native:
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    else:
        from src.desktop_runtime import configure
        configure(gui.SAFE_MODE, native_test=True)
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QColor, QPixmap, QFontDatabase
    from src.gui.config import save_gui_config, load_gui_config
    from src.plugins.manager import PluginManager
    from src.plugin_catalog import BASIC_PLUGINS
    from src.qq_app import QQRuntime, create_app
    import uvicorn
    app = QApplication.instance() or QApplication([])
    from PyQt6.QtCore import qVersion, PYQT_VERSION_STR
    platform_check = {'qt_runtime': qVersion(), 'pyqt': PYQT_VERSION_STR,
                      'platform': app.platformName(), 'native_window_test': native, 'safe_mode': gui.SAFE_MODE}
    if native:
        assert app.platformName() == 'windows'
    for font in ("msyh.ttc", "seguiemj.ttf", "segoeui.ttf"):
        QFontDatabase.addApplicationFont(str(Path(os.environ.get("WINDIR", "C:/Windows"))/"Fonts"/font))
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with selfcheck_root() as tmp:
        root = Path(tmp)
        shutil.copytree(gui.BUNDLE / 'styles', root / 'styles')
        # Exercise packaged imports and writable runtime construction without starting services.
        runtime = QQRuntime(root=root, config_path=root/'config.qq.yaml', db_path=root/'messages.sqlite')
        backend = create_app(runtime)
        backend.state.bot=runtime
        identity=asyncio.run(next(route.endpoint for route in backend.routes if route.path=='/api/runtime')())
        catalog=asyncio.run(next(route.endpoint for route in backend.routes if route.path=='/api/plugins')())
        # 安装包只带基础插件；市场插件（如 poke_counter）不在冻结包里也属正常。
        poke=next((p for p in catalog if p['id']=='poke_counter'), None)
        if poke is not None:
            assert poke['enabled_label']=='指令模式' and not poke['gesture_available']
        platform_check['poke_counter_present'] = poke is not None
        platform_check['packaged_ingest'] = asyncio.run(check_packaged_ingest(runtime))
        platform_check['packaged_reply'] = asyncio.run(check_packaged_reply(runtime))
        command_catalog=asyncio.run(next(route.endpoint for route in backend.routes if route.path=='/api/command-catalog')())
        assert command_catalog.get('commands'), '冻结包指令目录为空'
        platform_check['command_catalog_check']=True
        from src.runtime_identity import APP_VERSION,backend_identity_error
        assert identity['version']==APP_VERSION
        assert not backend_identity_error(identity,root)
        assert backend_identity_error(identity,root/'other')
        # 冻结包也实际经过人设导入与激活路由，不只验证解析器可以 import。
        async def persona_import_check():
            import httpx
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=backend), base_url='http://127.0.0.1') as client:
                result = await client.post('/api/personas/import', json={'filename': '妈妈.yaml',
                    'content': 'format: WhaleGirlPersona/v1\npersona:\n  name: packaged_import_probe\n  system_prompt: 你是温柔可靠的鲸鱼妈妈，正常接话。\n'})
                assert result.status_code == 200, result.text
                name = result.json()['name']
                result = await client.post(f'/api/personas/{name}/activate')
                assert result.status_code == 200, result.text
                assert '温柔可靠的鲸鱼妈妈' in runtime.style.full_prompt()
        asyncio.run(persona_import_check())
        platform_check['persona_import_check'] = True
        # 新助手在冻结包里也经过真实路由、持久化和启用；模型仅在本地替换。
        async def assistant_check():
            import httpx
            from unittest.mock import AsyncMock
            probe = {'display_name': '助手验收人设', 'description': '隔离自检',
                     'system_prompt': '你是温柔自然的助手验收人设。', 'examples': [], 'keywords': [],
                     'temperature': .7, 'max_tokens': 500}
            runtime.ai.api_url = 'https://mock.example.test/v1/chat/completions'
            runtime.ai.api_key = 'isolated-test-key'
            raw = json.dumps({'action': {'type': 'create_persona', 'persona': probe}}, ensure_ascii=False)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=backend), base_url='http://127.0.0.1') as client:
                async def wait(identifier):
                    for _ in range(100):
                        response = (await client.get('/api/assistant/jobs/' + identifier)).json()
                        if response['state'] not in ('planning', 'applying'):
                            return response
                        await asyncio.sleep(.01)
                    raise AssertionError('助手任务未完成')
                async def run(prompt, mode=None, reply=raw):
                    with patch('src.console_agent.AIEngine.chat', AsyncMock(return_value=reply)):
                        payload = {'prompt': prompt}
                        if mode:
                            payload['mode'] = mode
                        response = await client.post('/api/assistant/jobs', json=payload)
                        assert response.status_code == 200, response.text
                        return await wait(response.json()['id'])
                # 「方案」确认页已下线：人设动作提交即执行，不再有 ready→apply 两步。
                job = await run('生成一个温柔人设', mode='persona')
                assert job['state'] == 'applied' and job['result']['persona_name'], job
                activated = await run('启用刚才的人设', reply=json.dumps(
                    {'action': {'type': 'activate_persona', 'name': job['result']['persona_name']}},
                    ensure_ascii=False))
                assert activated['state'] == 'applied', activated
                assert probe['system_prompt'] in runtime.style.full_prompt()
                await backend.state.console_agent.close()
                return job
        assistant_preview = asyncio.run(assistant_check())
        platform_check['assistant_generate_apply_check'] = True
        async def bindings_check():
            import httpx
            from .features import FeaturesDialog
            from ..persona_bindings import resolve_persona
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=backend),base_url='http://127.0.0.1') as client:
                initial=(await client.get('/api/persona-bindings')).json()
                selected=initial['personas'][0]['name']
                response=await client.post('/api/persona-bindings',json={'revision':initial['revision'],
                    'rows':[{'kind':'group','target':'packaged_group','user':'packaged_user','persona':selected}]})
                assert response.status_code==200,response.text
                m={'kind':'group','target':'packaged_group','sender_id':'packaged_user','text':'你好'}
                assert resolve_persona(runtime,m).name==selected
                cache={path:(await client.get('/api/'+path)).json() for path in ('persona-bindings','command-catalog','group-diagnostics')}
                assert any(item['command']=='/菜单' for item in cache['command-catalog']['commands'])
                return cache
        # 安装包现在把 archive_isolation 作为基础插件；页面检查仍按运行时有它才跑，兼容老版本。
        has_bindings = 'archive_isolation' in getattr(runtime.plugins, '_plugins', {})
        if has_bindings:
            feature_cache=asyncio.run(bindings_check())
            platform_check['persona_bindings_routes_check']=True
        else:
            feature_cache={'persona-bindings':{},'command-catalog':{'commands':[]},'group-diagnostics':{}}
            platform_check['persona_bindings_routes_check']=False
        asyncio.run(runtime.close())
        platform_check['plugin_import_check'] = check_plugin_import(root/'import-acceptance')
        manager = PluginManager(root/'plugins')
        assert set(BASIC_PLUGINS)<=set(p['id'] for p in manager.list_plugins()), '基础插件漏包'

        class _BasicEngine:
            model = 'selftest'
            last_error = ''
            async def chat(self, messages, **kwargs):
                return '自检回复：基础插件链路可用。'

        basic_reply = asyncio.run(manager.generate_reply(
            _BasicEngine(), [{'role': 'user', 'content': '自检'}], timeout=3))
        assert basic_reply and '自检回复' in basic_reply, '基础插件完整回复自检失败'
        vision = next(p for p in manager.list_plugins() if p['id'] == 'image_understanding')
        assert vision['enabled'] and 'visionModel' in vision['schema'] and 'timeoutSec' in vision['schema']
        by_id = {p['id']:p for p in manager.list_plugins()}
        if 'memes' in by_id:
            assert by_id['memes']['enabled']
            from src.meme_service import catalog as meme_catalog
            assert len(meme_catalog()) == 300
        for pid in ('conversation_memory','chat_controls','sticker_memory'):
            assert by_id[pid]['enabled'], pid
        if 'emotion_state' in by_id:
            assert by_id['emotion_state']['enabled']
        if 'image_library' in by_id:
            assert by_id['image_library']['enabled']
        if 'scheduled_announcer' in by_id:
            assert not by_id['scheduled_announcer']['enabled']
        from src.image_input import image_parts
        assert image_parts(['https://example.test/a.png'], 'openai')[0]['type'] == 'image_url'
        config = {'transparency': 0.0, 'panel_transparency': .65, 'blur': 0.0}
        pixmap = QPixmap(1200, 760)
        pixmap.fill(QColor('#366fa1'))
        background = root/'background.png'
        pixmap.save(str(background))
        config['background_image'] = 'background.png'
        save_gui_config(root/'gui.json', config)
        with patch.object(gui, 'ROOT', root), patch.object(gui, 'GUI_CFG', root/'gui.json'), patch.object(gui, 'api_get', return_value={}), patch.object(sys, 'argv', ['gui', '--no-backend']):
            window = gui.MainWindow()
            window._timer.stop()
            window.show()
            # 透明主题下实际绘制 Qt 文件选择框，冻结版也不能再出现黑底黑字。
            ensure_chinese(app)
            from .file_dialogs import _styled
            from PyQt6.QtWidgets import QFileDialog, QListView
            file_dialog = _styled(QFileDialog(window, '导入人设文件', str(root), '人设文件 (*.yaml *.yml *.json *.txt)'))
            file_dialog.show()
            app.processEvents()
            file_view = next(view for view in file_dialog.findChildren(QListView) if view.objectName() == 'listView')
            background_color = file_view.viewport().grab().toImage().pixelColor(3, 3)
            assert background_color.alpha() == 255 and background_color.lightness() > 220
            file_dialog.grab().save(str(output.with_suffix('.import-dialog.png')))
            file_dialog.close()
            platform_check['persona_file_dialog_readable'] = True
            resize_checks = check_window_resize(window, app, native)
            # 冻结包也验证关闭只隐藏且恢复尺寸，不发真实QQ启停/聊天请求。
            tray=window._tray;tray.enabled=True;window._timer.start(60000)
            from PyQt6.QtWidgets import QSystemTrayIcon
            with patch.object(QSystemTrayIcon,'isSystemTrayAvailable',return_value=True):
                assert not window.close() and not window.isVisible()
                assert window._timer.isActive() and not window.hang_watchdog._stop
                tray.show_window();app.processEvents();assert window.isVisible()
            window._timer.stop()
            tray.update({'running':True});assert tray.stop_action.isEnabled() and not tray.start_action.isEnabled()
            tray.menu.show();app.processEvents()
            color=tray.menu.grab().toImage().pixelColor(4,4)
            assert color.alpha()==255 and color.lightness()>220
            tray.menu.grab().save(str(output.with_suffix('.tray-menu.png')))
            tray.menu.hide();tray.enabled=False;tray.icon.hide()
            platform_check['tray_hide_restore_check']=True
            platform_check['tray_menu_actions_check']=True
            window.tabs.setCurrentWidget(window.assistant_tab)
            assistant=window.assistant_tab
            window.resize(800,500);app.processEvents()
            from PyQt6.QtCore import QPoint
            # 「方案」面板已下线：这些控件根本不存在，助手页只剩对话。
            assert not any(hasattr(assistant, name) for name in
                           ('preview_title', 'preview', 'empty_preview', 'apply_button', 'persona_prompt'))
            assert not assistant.confirm_button.isEnabled()
            assert not assistant.send_button.visibleRegion().isEmpty()
            window.grab().save(str(output.with_suffix('.assistant-empty.png')))
            platform_check['assistant_empty_layout_check']=True
            window.resize(1100, 780)
            # 一次性确认：只有危险动作会停在 confirm，按钮此时才可用。
            window.assistant_tab.present({'id': str((assistant_preview or {}).get('id') or 'smoke'),
                                          'state': 'confirm',
                                          'message': '这一步要操作你的电脑，需要你点一次确认：\n打开网页'})
            app.processEvents()
            assert assistant.confirm_button.isEnabled()
            platform_check['assistant_confirm_button_check']=True
            window.grab().save(str(output.with_suffix('.assistant-confirm.png')))
            window.assistant_tab.present(assistant_preview)
            app.processEvents()
            def assistant_panel_color():
                view=assistant.chat_scroll.viewport();point=view.mapTo(window,QPoint(view.width()-20,view.height()-20))
                shot=window.grab().toImage();scale=shot.devicePixelRatio()
                return shot.pixelColor(round(point.x()*scale),round(point.y()*scale))
            window._apply_panel_transparency(0);app.processEvents();assistant_opaque=assistant_panel_color()
            window._apply_panel_transparency(.65);app.processEvents();assistant_clear=assistant_panel_color()
            assert assistant_opaque.red()>assistant_clear.red()+20
            platform_check['assistant_appearance_sync_check']=True
            assert not window.assistant_tab.confirm_button.isEnabled()
            window.grab().save(str(output.with_suffix('.assistant.png')))
            platform_check['assistant_native_preview'] = True
            assistant._show_skills({'skills':[]});app.processEvents()
            skills=assistant.skills_dialog
            color=skills.grab().toImage().pixelColor(8,8)
            assert color.lightness()>180 and assistant.skills_empty.isVisible()
            skills.grab().save(str(output.with_suffix('.skills.png')))
            skills.hide()
            platform_check['assistant_skills_surface_check']=True
            if has_bindings:
                from .features import FeaturesDialog
                features=FeaturesDialog(lambda path:feature_cache[path],lambda *args: {'message':'自检'},lambda pid:None,window)
                features.show();app.processEvents()
                assert features.table.rowCount()==1 and features.table.cellWidget(0,3).currentData()
                features.grab().save(str(output.with_suffix('.features.png')))
                features.close();features.deleteLater();app.processEvents()
                platform_check['native_persona_bindings_preview']=True
            window.tabs.setCurrentWidget(window.persona_tab)
            window.resize(800, 500)
            app.processEvents()
            window.persona_tab.scroll.verticalScrollBar().setValue(
                window.persona_tab.scroll.verticalScrollBar().maximum())
            assert window.persona_tab.btn_save.isVisible()
            assert not window.persona_tab.btn_save.visibleRegion().isEmpty()
            assert '保存' in window.persona_tab.btn_image_save.text()
            window.grab().save(str(output.with_suffix('.persona.png')))
            # 外部后端页：空态、表格行、按钮使能都要在 900x600 下真的画出来。
            from PyQt6.QtTest import QTest
            window.tabs.setCurrentWidget(window.agent_backend_tab)
            window.resize(900, 600)
            app.processEvents()
            backend_tab = window.agent_backend_tab
            for _ in range(60):
                app.processEvents()
                if not backend_tab.requests.busy:
                    break
                QTest.qWait(10)
            backend_tab.apply_list({'ok': True, 'backends': []})
            app.processEvents()
            assert backend_tab.empty.text() and backend_tab.empty.isVisible(), '空后端时必须是空态文案'
            assert not backend_tab.table.isVisible() and backend_tab.table.rowCount() == 0
            assert not any(backend_tab.button_state().values()), '没有后端时按钮必须全禁用'
            window.grab().save(str(output.with_suffix('.agent-backends-empty.png')))
            backend_tab.apply_list({'ok': True, 'backends': [{
                'id': 'smoke-cli', 'kind': 'cli', 'state': 'idle', 'running': False, 'pid': None,
                'command': 'python.exe', 'args': ['-c', 'print(1)'], 'workdir': str(root),
                'timeout': 30.0, 'workspace': str(root / 'agent_workspace' / 'smoke-cli'),
                'data_dir_ready': True}]})
            app.processEvents()
            assert backend_tab.table.rowCount() == 1 and backend_tab.table.isVisible()
            assert backend_tab.table.horizontalHeaderItem(1).text() == '状态'
            backend_tab.select('smoke-cli')
            app.processEvents()
            idle_buttons = backend_tab.button_state()
            assert idle_buttons['start'] and not idle_buttons['stop'] and idle_buttons['delete']
            assert not backend_tab.empty.isVisible()
            window.grab().save(str(output.with_suffix('.agent-backends.png')))
            shot = backend_tab.table.grab().toImage()
            assert shot.width() > 200 and shot.height() > 100, '后端表格没有画出像素'
            ink = sum(1 for x in range(0, shot.width(), 3) for y in range(0, shot.height(), 3)
                      if shot.pixelColor(x, y).lightness() < 120)
            assert ink > 20, ('后端表格里没有画出文字像素', ink)
            shot.save(str(output.with_suffix('.agent-backends-table.png')))
            platform_check['agent_backend_panel_check'] = {'rows': 1, 'empty_state_text': True,
                                                            'buttons': idle_buttons,
                                                            'ink_pixels': ink,
                                                            'screenshot': 'agent-backends.png'}
            backend_tab.apply_list({'ok': True, 'backends': []})
            backend_tab.stop_polling()
            app.processEvents()
            window.tabs.setCurrentWidget(window.console_tab)
            window.resize(900, 600)
            app.processEvents()
            native_edge_checks = check_native_resize_edges(window) if native and not window.safe_mode else []
            window._apply_panel_transparency(.65)
            app.processEvents()
            translucent = window.console_tab.msg_view.grab().toImage().pixelColor(20, 20)
            window._apply_panel_transparency(0)
            app.processEvents()
            opaque = window.console_tab.msg_view.grab().toImage().pixelColor(20, 20)
            assert opaque != translucent, (opaque.getRgb(), translucent.getRgb())
            assert opaque.alpha() > translucent.alpha()
            window._apply_panel_transparency(.65)
            app.processEvents()
            window.grab().save(str(output.with_suffix('.console.png')))
            window.resize(1200, 900)
            window.tabs.setCurrentWidget(window.appearance_tab)
            window.show()
            app.processEvents()
            window.appearance_tab.panel_transparency_slider.setValue(65)
            window.appearance_tab.transparency_slider.setValue(0)
            window.appearance_tab._apply()
            assert window._cfg['panel_transparency'] == .65
            assert window._cfg['transparency'] == 0
            expected_white = round(255 * gui.panel_alpha(.65, window._container.has_background_image()))
            assert f'rgba(255, 255, 255, {expected_white})' in window.styleSheet(), expected_white
            panel = next(w for w in window.appearance_tab.findChildren(gui.QFrame) if w.property('panel'))
            before = panel.grab().toImage().pixelColor(8, 50)
            window.appearance_tab.panel_transparency_slider.setValue(0)
            window.appearance_tab._apply()
            app.processEvents()
            after = panel.grab().toImage().pixelColor(8, 50)
            assert before != after, (before.name(), after.name())
            assert after.alpha() > before.alpha(), (before.getRgb(), after.getRgb())
            window.appearance_tab.panel_transparency_slider.setValue(65)
            window.appearance_tab._apply()
            app.processEvents()
            window.grab().save(str(output.with_suffix('.png')))
            with patch.object(gui, 'api_get', return_value=manager.list_plugins()):
                window.tabs.setCurrentWidget(window.plugin_tab)
                window.plugin_tab.refresh()
            assert len(window.plugin_tab._rows) >= len(BASIC_PLUGINS) - 1
            window.plugin_tab.search.setText('ambient_reply')
            assert sum(not row['frame'].isHidden() for row in window.plugin_tab._rows.values()) == 1
            window.plugin_tab.search.clear()
            window.plugin_tab.search.setText('image_understanding')
            assert sum(not row['frame'].isHidden() for row in window.plugin_tab._rows.values()) == 1
            window.plugin_tab.search.clear()
            app.processEvents()
            window.grab().save(str(output.with_suffix('.plugins.png')))
            wakeup=gui.PluginConfigDialog(by_id['chat_controls'])
            wakeup.show();app.processEvents()
            assert wakeup.values()==by_id['chat_controls']['settings']
            assert wakeup.findChild(gui.QPushButton,'wakeup_examples').isVisible()
            wakeup.grab().save(str(output.with_suffix('.wakeup.png')))
            wakeup.close();wakeup.deleteLater()
            # 关于作者页：头像是随包资源，冻结后最容易变成空白，所以必须切过去、
            # 断言头像真的画出来、作者/QQ/致谢/声明都在，并留一张截图。
            window.tabs.setCurrentWidget(window.about_tab)
            app.processEvents()
            about = window.about_tab
            assert not about.avatar.pixmap().isNull(), \
                '关于作者页的头像没加载（检查 assets/author/linxi.jpg 是否打进包）'
            about_text = '\n'.join(label.text() or '' for label in about.findChildren(gui.QLabel))
            for expected in (gui.AUTHOR_NAME, gui.AUTHOR_QQ, '你携秋水揽星河', gui.COPYRIGHT_NOTICE):
                assert expected in about_text, f'关于作者页缺少：{expected}'
            window.grab().save(str(output.with_suffix('.about.png')))
            if poke is not None:
                poke['enabled']=True  # Render the user's enabled official-mode card.
            with patch.object(gui, 'api_get', return_value=catalog):
                window.plugin_tab.refresh()
            if 'poke_counter' in by_id:
                window.plugin_tab.search.setText('poke_counter')
                app.processEvents()
                poke_row=window.plugin_tab._rows['poke_counter']['frame']
                assert any('/戳一戳' in w.text() for w in poke_row.findChildren(gui.QLabel))
                window.grab().save(str(output.with_suffix('.poke.png')))
            window.plugin_tab.search.clear()
            window.plugin_tab.state_filter.setCurrentIndex(window.plugin_tab.state_filter.findData('unavailable'))
            app.processEvents()
            # 基础插件可能全部可用，此时筛选结果为空是正常的；只验证筛选器本身生效。
            assert window.plugin_tab.state_filter.currentData() == 'unavailable'
            window.grab().save(str(output.with_suffix('.capabilities.png')))
            window.plugin_tab.state_filter.setCurrentIndex(0)
            if 'image_library' in by_id:
                upload_dialog=gui.PluginConfigDialog(by_id['image_library'])
                assert upload_dialog.values()['separateMessages'] is True
            if 'image_library' in by_id:
                upload_dialog.resize(860,650)
                with patch.object(gui.file_dialogs,'open_files',return_value=([str(background)],'')):
                    upload_dialog._file_buttons['library'].click()
                uploaded=upload_dialog.values()['library']
                assert uploaded.startswith('background | ')
                assert Path(uploaded.partition('|')[2].strip()).is_file()
                upload_dialog.show()
                app.processEvents()
                upload_button=upload_dialog._file_buttons['library']
                button_color=upload_button.grab().toImage().pixelColor(5,upload_button.height()//2)
                assert button_color.green()>button_color.red(), button_color.getRgb()
                upload_dialog.grab().save(str(output.with_suffix('.uploads.png')))
                def save_plugin(path,data):
                    manager.update_settings('image_library',data)
                    return {'success':True}
                with patch.object(gui,'api_post',side_effect=save_plugin):
                    upload_dialog._save()
                assert upload_dialog.saved and manager.get_settings('image_library')['library']==uploaded
                from types import SimpleNamespace
                local_media=manager._plugins['image_library'].media_for('background',SimpleNamespace(mode='official'))
                assert local_media['data']['file'].startswith('base64://')
                selected=asyncio.run(manager._plugins['image_library'].on_post_reply({},'给你～ [[表情:background]]',SimpleNamespace(mode='official')))
                assert selected[1]['media']==[local_media]
                upload_dialog.close()
            keyword_dialog=gui.PluginConfigDialog(by_id['keyword_reply'])
            rule_editor=keyword_dialog._widgets['rules'][0]
            rule_editor.example_button.click()
            rule_editor.table.cellWidget(0,1).setPlainText('你好呀～\n今天过得怎么样？')
            keyword_dialog._widgets['enabled'][0].setChecked(True)
            keyword_dialog.show()
            app.processEvents()
            add_color=rule_editor.add_button.grab().toImage().pixelColor(5,rule_editor.add_button.height()//2)
            assert add_color.green()>add_color.red(),add_color.getRgb()
            keyword_dialog.grab().save(str(output.with_suffix('.keywords.png')))
            def save_keyword(path,data):
                manager.update_settings('keyword_reply',data)
                return {'success':True}
            with patch.object(gui,'api_post',side_effect=save_keyword):
                keyword_dialog._save()
            assert keyword_dialog.saved
            assert manager._plugins['keyword_reply'].on_ingest({'text':'你好'},None)=='你好呀～\n今天过得怎么样？'
            keyword_dialog.close()
            from src.sticker_library import StickerLibrary,frames_for_vision
            import base64
            store=StickerLibrary(root/'data/sticker_library')
            entry,_=store.put(background.read_bytes(),'开心','测试图片说明')
            def sticker_get(path,**kwargs):
                item=dict(store.entries()[0],thumbnail=base64.b64encode(background.read_bytes()).decode())
                return {'items':[item],'total':1,'page':1,'pages':1}
            library_dialog=gui.StickerLibraryDialog(root,sticker_get,lambda *a,**k:{'success':True},lambda *a,**k:{'success':True},window)
            library_dialog.show();app.processEvents()
            assert library_dialog.table.rowCount()==1
            assert not library_dialog.table.cellWidget(0,0).pixmap().isNull()
            library_color=library_dialog.grab().toImage().pixelColor(5,5)
            assert library_color.alpha()==255 and library_color.red()>200,library_color.getRgb()
            library_dialog.grab().save(str(output.with_suffix('.sticker-library.png')))
            library_dialog.close()
            if 'sticker_cadence' in manager._plugins:
                manager.update_settings('sticker_cadence',{'enabled':True})
                delivery=SimpleNamespace(mode='official',account=lambda:'test',stickers=store,reply_meta=None)
                cadence=manager._plugins['sticker_cadence']
                event={'kind':'group','target':'test'}
                cadence.on_sent(event,'第一句',delivery);cadence.on_sent(event,'第二句',delivery)
                asyncio.run(cadence.prepare_delivery(event,'第三句',delivery))
                assert delivery.reply_meta['media'][0]['type']=='image'
            raw=base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
            animated=raw[:-1]+raw[raw.index(b'\x21\xf9'):-1]+b';'
            assert len(frames_for_vision(animated,2))==2
            memory_data={'success':True,'persona':'鲸鱼娘','autoRemember':True,
                'manual':[{'id':'fixture','content':'谈到加班时先关心对方的状态','enabled':True}],
                'structured':[{'id':'fixture','scope':'official|示例|group:测试群','subject':'小鱼','content':'喜欢蓝色','status':'confirmed','evidence':2}]}
            window.memory_tab.get=lambda p: memory_data if p.startswith('memory') else {'decisions':[{'kind':'group','target':'测试群','sender':'小鱼','reason':'未被 @ 或昵称点名，跟聊窗口未开启'}]}
            window.tabs.setCurrentWidget(window.memory_tab);window.memory_tab.refresh();app.processEvents()
            assert window.memory_tab.manual_table.rowCount()==1
            window.grab().save(str(output.with_suffix('.memory.png')))
            window.pc_tab.get=lambda p:{'success':True,'owners':'示例主人','candidates':[{'id':'示例主人','name':'小鱼'}],'enabled':True,'robot':{'running':True,'reply_paused':False,'qq_connected':True,'model':'deepseek-chat','persona':'鲸鱼娘','queue':{'pending':0,'failed':0,'uncertain':0},'plugins':[{'enabled':True}]},
                'jobs':[{'id':'fixture','action':'pause','arg':'','state':'ready','time':0,'result':''}]}
            window.tabs.setCurrentWidget(window.pc_tab);window.pc_tab.refresh();app.processEvents()
            assert window.pc_tab.table.rowCount()==1
            window.grab().save(str(output.with_suffix('.pc.png')))
            window.pc_tab.timer.stop()
            window.close()
            saved = load_gui_config(root/'gui.json')
            assert saved['panel_transparency'] == .65 and saved['transparency'] == 0
        # Qt 中文翻译：非原生对话框（安全模式）靠它变中文；
        # 冻结包里漏了 .qm 时，这里会直接报 loaded=False。
        ensure_chinese(QApplication.instance())
        locale = translation_status()
        output.write_text(json.dumps({'ok':True, 'qt_zh_loaded':bool(locale.get('loaded')), 'qt_zh_path':str(locale.get('path') or locale.get('reason') or ''), 'native_dialogs':should_use_native_dialogs(False), 'frozen':gui.FROZEN, 'plugins':len(manager.list_plugins()), 'plugin_ids':[p['id'] for p in manager.list_plugins()], 'plugin_problems':manager.take_load_problems(), 'basic_reply':bool(basic_reply), 'plugin_file_upload':True, 'keyword_rule_editor':True, 'panel_transparency':saved['panel_transparency'], 'window_transparency':saved['transparency'], 'rendered_pixels':[before.getRgb(),after.getRgb()], 'backend_routes':len(backend.routes),'native_memory':True,'native_pc_review':True,'plugin_capabilities':len(catalog),'native_capability_filter':True, 'single_title_bar':True, 'resize_checks':resize_checks, 'native_edge_checks':native_edge_checks, 'device_pixel_ratio':app.devicePixelRatio(), 'console_panel_transparency':True, **platform_check}, indent=2), encoding='utf-8')
