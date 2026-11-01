"""Real Node host coverage: bounded, explicit capability dependencies."""
import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import extension_host, extension_registry, skill_tools, qqagent_host

REPO = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')


@unittest.skipUnless(NODE, 'Node runtime required')
class HostDependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.host = self.root / 'host'
        shutil.copytree(REPO / 'runtime/qqagent-host', self.host)
        self.data = self.root / 'data'

    def package(self, ident, *, requires=(), capabilities=(), body=None):
        folder = self.host / 'plugins' / ident
        folder.mkdir(parents=True)
        meta = {'id': ident, 'name': ident, 'version': '1.0.0', 'apiVersion': 1,
                'entry': 'index.js', 'enabledByDefault': True,
                'requires': list(requires), 'capabilities': list(capabilities)}
        (folder / 'plugin.json').write_text(json.dumps(meta), encoding='utf-8')
        (folder / 'index.js').write_text(body or 'export function setup(api) {}', encoding='utf-8')
        return folder

    def run_host(self, folder, tool='', args=None, timeout=5):
        return asyncio.run(extension_host.run_capability(
            node=NODE, host_script=str(self.host / 'host-one.mjs'), source_dir=str(self.host),
            extension=str(folder), extension_id=folder.name,
            capability='tool' if tool else '', capability_arg=tool,
            payload=args or {}, kind='tool' if tool else 'list',
            data_dir=self.data, timeout=timeout,
            extra_env={'QQ_AGENT_DATA_DIR': str(self.root / 'node-data')}))

    def enable(self, *ids):
        for ident in ids:
            extension_registry.set_enabled(ident, True, self.data)

    def pair(self):
        provider = self.package('fixture-provider', capabilities=['fixture.value'], body="""
export const providers = {'fixture.value': ({value = 'dependency'}) => value};
export function setup(api) { api.registerTool({id:'private',name:'private',execute(){return 'secret'}}); }
""")
        consumer = self.package('fixture-consumer', requires=['fixture.value'], body="""
export function setup(api) {
 const value = api.capability('fixture.value', {value:'setup saw dependency'});
 api.registerTool({id:'read', name:'read', execute(ctx,args){
   return {value, query:args.query, flattened:ctx.query, kind:ctx.kind}; }});
}
""")
        return provider, consumer

    def test_dependency_loaded_before_consumer_and_only_own_tools_exposed(self):
        provider, consumer = self.pair()
        self.enable(provider.name)
        self.package('unrelated', body="throw new Error('must not import unrelated')")
        result = self.run_host(consumer, 'fixture-consumer__read', {'query': 'argument'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['value']['value'], 'setup saw dependency')
        self.assertEqual(result['value']['query'], 'argument')
        self.assertEqual(result['value']['flattened'], 'argument')
        self.assertEqual(result['raw']['extension']['id'], consumer.name)
        self.assertEqual([item['id'] for item in result['raw']['dependencies']], [provider.name])
        self.assertEqual([item['id'] for item in result['raw']['tools']], ['fixture-consumer__read'])
        self.assertFalse(list(self.host.glob('plugins-host-*')))
        rows = [json.loads(line) for line in (self.data / extension_host.LOG_NAME).read_text(encoding='utf-8').splitlines()]
        self.assertTrue(rows[-1]['ok'])

    def test_missing_and_disabled_dependencies_fail_before_setup(self):
        provider, consumer = self.pair()
        result = self.run_host(consumer)
        self.assertEqual(result['raw']['error_code'], 'dependency-disabled', result)
        shutil.rmtree(provider)
        result = self.run_host(consumer)
        self.assertEqual(result['raw']['error_code'], 'dependency-missing', result)

    def test_cycle_and_multiple_enabled_providers_are_rejected(self):
        first = self.package('first', requires=['second.cap'], capabilities=['first.cap'])
        self.package('second', requires=['first.cap'], capabilities=['second.cap'])
        self.enable('first', 'second')
        self.assertEqual(self.run_host(first)['raw']['error_code'], 'dependency-cycle')
        shutil.rmtree(self.host / 'plugins/first')
        shutil.rmtree(self.host / 'plugins/second')
        provider, consumer = self.pair()
        self.package('another', capabilities=['fixture.value'])
        self.enable(provider.name, 'another')
        self.assertEqual(self.run_host(consumer)['raw']['error_code'], 'dependency-ambiguous')

    def test_dependency_tool_cannot_be_called_as_consumer(self):
        provider, consumer = self.pair()
        self.enable(provider.name)
        result = self.run_host(consumer, 'fixture-provider__private')
        self.assertFalse(result['ok'])
        self.assertEqual(result['raw']['error_code'], 'tool-not-owned')

    def test_declared_but_unimplemented_provider_is_rejected(self):
        provider, consumer = self.pair()
        (provider / 'index.js').write_text('export function setup(api) {}', encoding='utf-8')
        self.enable(provider.name)
        self.assertEqual(self.run_host(consumer)['raw']['error_code'], 'dependency-unimplemented')

    def test_async_dependency_activation_completes_before_tool(self):
        provider, consumer = self.pair()
        (provider / 'index.js').write_text("""
let ready = false;
export async function activate(){await new Promise(r=>setTimeout(r,20));ready=true;}
export const providers = {'fixture.value': () => ready ? 'activated' : 'not-ready'};
export function setup(api) {}
""", encoding='utf-8')
        (consumer / 'index.js').write_text("""
export function setup(api){api.registerTool({id:'read',name:'read',execute(){
 return api.capability('fixture.value');}})}
""", encoding='utf-8')
        self.enable(provider.name)
        result = self.run_host(consumer, 'fixture-consumer__read')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['value'], 'activated')

    def test_stale_parent_allowlist_cannot_enable_dependency(self):
        _, consumer = self.pair()
        with mock.patch.dict(os.environ, {'QQ_AGENT_DEPENDENCY_IDS': '["fixture-provider"]'}):
            self.assertEqual(self.run_host(consumer)['raw']['error_code'], 'dependency-disabled')

    def test_real_registered_handler_receives_bound_ingress_context(self):
        package = self.package('fixture-ctx', body="""
export function setup(api){api.registerTool({id:'read',name:'read',execute(ctx,args){
 return {chatKey:ctx.chatKey, senderId:ctx.senderId, query:args.query};}})}
""")
        runtime = type('Runtime', (), {'account': lambda self: 'account-A'})()
        handler = skill_tools.build_tool_handler(
            node=NODE, host_script=str(self.host/'host-one.mjs'), source_dir=str(self.host),
            extension=str(package), extension_id=package.name,
            mapping={'read': 'fixture-ctx__read'}, timeout=5, data_dir=self.data,
            context=lambda: skill_tools.runtime_tool_context(runtime), required_context={'chatKey'})
        with skill_tools.tool_context(runtime, {'kind':'group','target':'42','sender_id':'99'}):
            result = asyncio.run(handler('read', {'query':'ok'}))
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['result'], {'chatKey':'group:42','senderId':'99','query':'ok'})

    def test_cancelled_process_is_terminated_and_owned_root_removed(self):
        package = self.package('fixture-hang', body='export async function setup(api){await new Promise(()=>{});} ')
        async def run():
            task = asyncio.create_task(extension_host.run_capability(
                node=NODE, host_script=str(self.host/'host-one.mjs'), source_dir=str(self.host),
                extension=str(package), capability='', timeout=10, data_dir=self.data))
            for _ in range(100):
                if list(self.host.glob('plugins-host-*')):
                    break
                await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(run())
        self.assertFalse(list(self.host.glob('plugins-host-*')))


if __name__ == '__main__':
    unittest.main()
