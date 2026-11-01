"""发布载荷白名单：0.4 宿主运行时 + 四个 DSH 接入口 + 四份桥接文档进包，协作材料不进包。"""
import hashlib
import tempfile
import unittest
from pathlib import Path

from tools.build_release import DSH_DOCS, DSH_TOOLS, RUNTIME_PAYLOAD, ROOT, build, collect_paths

PAYLOAD_FILES=['runtime/qqagent-host/src/config.js','runtime/qqagent-host/src/plugin-loader.js',
               'runtime/qqagent-host/node_modules/undici/types/formdata.d.ts',
               'runtime/qqagent-host/node_modules/ws/wrapper.mjs','runtime/qqagent-host/host-one.mjs',
               'runtime/qqagent-host/package.json','runtime/qqagent-host/node_modules/ws/LICENSE']
FORBIDDEN=['tools/agent_apis.py','tools/team.py','tools/coordination.py','tools/build_release.py']

class PayloadWhitelistTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).resolve().parents[1]/'.tmp';base.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=base)
        self.root=Path(self.tmp.name)/'source';self.root.mkdir()
        for name in ['qq_main.py','start.bat','requirements.qq.txt','LICENSE','QQ版使用说明.md','src/app.py','styles/default.yaml']:
            self.write(name)
        self.destination=Path(self.tmp.name)/'release'

    def tearDown(self):
        self.tmp.cleanup()

    def write(self,name,text='safe source'):
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8');return path

    def test_real_tree_lists_payload_dsh_tools_and_docs(self):
        listed={p.relative_to(ROOT).as_posix() for p in collect_paths(ROOT)}
        payload=ROOT.joinpath(*RUNTIME_PAYLOAD)
        on_disk={p.relative_to(ROOT).as_posix() for p in payload.rglob('*')
                 if p.is_file() and '__pycache__' not in p.parts and p.suffix.lower()!='.pyc'}
        self.assertTrue(on_disk,'运行时载荷目录不存在或为空')
        self.assertEqual(on_disk-listed,set(),'载荷里有文件没进白名单')
        for name in PAYLOAD_FILES:
            self.assertTrue(name in listed,'载荷文件没进白名单: '+name)
        self.assertTrue(any(name.endswith('.d.ts') for name in listed),'.ts 不能被后缀白名单吃掉')
        self.assertTrue(any(name.endswith('.mjs') for name in listed),'.mjs 不能被后缀白名单吃掉')
        self.assertTrue(any(Path(name).suffix=='' for name in on_disk),'载荷里应有不带后缀的文件')
        for name in DSH_TOOLS:
            self.assertTrue('tools/'+name in listed,'接入口没进白名单: '+name)
        for name in DSH_DOCS:
            self.assertTrue('docs/'+name in listed,'文档没进白名单: '+name)
        for name in FORBIDDEN:
            self.assertNotIn(name,listed)
        for prefix in ('agents/','shared/','.team-private/','data/','.tmp/','插件市场/','release/'):
            self.assertFalse([name for name in listed if name.startswith(prefix)],prefix)

    def test_payload_copied_verbatim_and_tools_file_whitelist(self):
        for name in PAYLOAD_FILES+['runtime/qqagent-host/__pycache__/junk.pyc','runtime/qqagent-host/src/stale.pyc']+FORBIDDEN:
            self.write(name,'payload body')
        for name in DSH_TOOLS:
            self.write('tools/'+name,'tool body')
        for name in DSH_DOCS:
            self.write('docs/'+name,'doc body')
        self.write('docs/内部排障.md','internal')
        build(self.root,self.destination)
        for name in PAYLOAD_FILES:
            target=self.destination/name
            self.assertTrue(target.is_file(),name)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),
                             hashlib.sha256((self.root/name).read_bytes()).hexdigest(),name)
        self.assertFalse((self.destination/'runtime/qqagent-host/__pycache__').exists())
        self.assertFalse((self.destination/'runtime/qqagent-host/src/stale.pyc').exists())
        self.assertEqual(sorted(p.name for p in (self.destination/'tools').iterdir()),sorted(DSH_TOOLS))
        self.assertEqual(sorted(p.name for p in (self.destination/'docs').iterdir()),sorted(DSH_DOCS))

    def test_secret_in_payload_prevents_release(self):
        self.write('runtime/qqagent-host/src/config.js','sk-'+'z'*24)
        with self.assertRaises(ValueError):build(self.root,self.destination)
        self.assertFalse(self.destination.exists())

if __name__=='__main__':unittest.main()
