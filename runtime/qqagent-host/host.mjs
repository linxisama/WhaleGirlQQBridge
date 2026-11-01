// QQ Agent 0.4 扩展宿主：加载 plugins/ 与 skills/，输出可用扩展清单。
// 用法：QQ_AGENT_SOURCE=<含 plugins/skills 的 0.4 目录> node host.mjs
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const source = path.resolve(process.env.QQ_AGENT_SOURCE || '.');
process.env.QQ_AGENT_DATA_DIR ||= path.join(source, 'data-host');
const { loadPlugins } = await import(pathToFileURL(path.join(source, 'src/plugin-loader.js')).href);
const { skillManager } = await import(pathToFileURL(path.join(source, 'src/skills/manager.js')).href);
const { buildToolDefs } = await import(pathToFileURL(path.join(source, 'src/tools.js')).href);

const loaded = await loadPlugins({ log: (...args) => console.error(...args) });
const extensions = skillManager.list().map((item) => ({
  id: item.id, name: item.name, version: item.version, kind: item.kind,
  source: item.source, dir: item.dir, tools: item.toolIds || [],
  capabilities: item.manifest?.capabilities || [], loadError: item.loadError || '',
}));
const tools = buildToolDefs().map((tool) => ({ id: tool.id, name: tool.name,
  description: tool.description, category: tool.category }));
console.log(JSON.stringify({ ok: loaded.failed.length === 0, loaded: loaded.loaded.length,
  failed: loaded.failed.map((item) => ({ id: item.id, error: item.error })),
  extensions, tools }, null, 2));
