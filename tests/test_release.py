import json
import tempfile
import unittest
from pathlib import Path
from tools.build_release import build

class ReleaseTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).resolve().parents[1]/'.tmp';base.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=base)
        self.root=Path(self.tmp.name)/'source';self.root.mkdir()
        for name in ['qq_main.py','start.bat','requirements.qq.txt','LICENSE','QQ版使用说明.md','src/app.py','styles/default.yaml',
                     'tools/whale_bot_mcp.py','tools/whale_memory_mcp.py','tools/whale_tools_mcp.py',
                     'tools/verify_whale_bot_mcp.py','tools/setup_dsh_memory.py','tools/e2e_extension_probe.py']:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('safe source',encoding='utf-8')
        for name in ['agents/gpt/reports/internal.md','shared/core/CHECKPOINT.json','.team-private/providers.json','data/chat.json','config.qq.yaml','tools/agent_apis.py','tools/team.py','tools/coordination.py']:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('private-content',encoding='utf-8')
        self.destination=Path(self.tmp.name)/'release'

    def tearDown(self):
        self.tmp.cleanup()

    def test_allowlist_excludes_agent_and_private_material(self):
        build(self.root,self.destination)
        self.assertTrue((self.destination/'src/app.py').is_file())
        for name in ('agents','shared','.team-private','data'):
            self.assertFalse((self.destination/name).exists())
        # tools/ 不再整目录禁带，也不再整目录放行：只认白名单里的 DSH 接入口文件名，
        # agent_apis.py / team.py / coordination.py 这些协作材料必须一个都出不去。
        # 三个 MCP 桥脚本（记忆/控制/工具）都必须在：setup_dsh_memory.py 登记的就是它们的发布包路径。
        self.assertEqual(sorted(p.name for p in (self.destination/'tools').iterdir()),
                         sorted(('whale_bot_mcp.py','whale_memory_mcp.py','whale_tools_mcp.py',
                                 'verify_whale_bot_mcp.py','setup_dsh_memory.py','e2e_extension_probe.py')))
        self.assertNotIn('private-content',(self.destination/'config.qq.yaml').read_text(encoding='utf-8'))
        manifest=json.loads((self.destination/'BUILD-MANIFEST.json').read_text(encoding='utf-8'))
        self.assertIn('src/app.py',manifest)
        self.assertIn('tools/whale_bot_mcp.py',manifest)
        self.assertNotIn('tools/agent_apis.py',manifest)

    def test_never_overwrites_existing_release(self):
        self.destination.mkdir()
        with self.assertRaises(FileExistsError):build(self.root,self.destination)

    def test_gui_resources_and_attribution_included_but_custom_persona_excluded(self):
        included=['qq_gui.py','gui_launcher.py','requirements.gui.txt','安装使用说明.txt',
                  '功能对照与使用说明.md','插件本地文件接入说明.md','icon.ico','assets/icons/app.png',
                  'assets/icon-map.json','third_party/estrella/LICENSE',
                  'assets/memes/images/luxun_say/0.jpg',
                  'third_party/estrella/NOTICE','third_party/estrella/SOURCE.md']
        excluded=['styles/custom/private.yaml','assets/private.txt','third_party/estrella/private.json']
        for name in included+excluded:
            path=self.root/name
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('resource',encoding='utf-8')
        build(self.root,self.destination)
        for name in included:
            self.assertTrue((self.destination/name).is_file(),name)
        for name in excluded:
            self.assertFalse((self.destination/name).exists(),name)

    def test_secret_in_source_prevents_release(self):
        (self.root/'src/app.py').write_text('sk-'+'z'*24)
        with self.assertRaises(ValueError):build(self.root,self.destination)
        self.assertFalse(self.destination.exists())

if __name__=='__main__':unittest.main()
