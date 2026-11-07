// 单次扩展宿主：仅载入被选扩展及本次明确允许的、已安装的 requires 能力提供者。
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { pathToFileURL } from 'node:url';
import { execFileSync } from 'node:child_process';

const source = path.resolve(process.env.QQ_AGENT_SOURCE || '.');
const input = path.resolve(process.argv[2] || '');
const kind = process.argv[3] === 'skill' ? 'skills' : 'plugins';
const ID = /^[a-z0-9][a-z0-9._-]{0,63}$/i;
const MAX_EXTENSIONS = 64;
const MAX_FILES = 4000;
const MAX_BYTES = 24 * 1024 * 1024;
let temp = null;
let targetRoot = null;
let manager = null;
let extension = null;
let dependencyPlan = [];

function failure(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}
function inside(root, candidate) {
  const relative = path.relative(root, candidate);
  return !path.isAbsolute(relative) && relative !== '..' && !relative.startsWith(`..${path.sep}`);
}
function hostContext() {
  try {
    const value = JSON.parse(process.env.QQ_AGENT_CTX || '{}');
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  } catch { return {}; }
}
// 条件能力：只有清单里声明了 web_fetch，工具 ctx 上的 fetch 才是真的；
// 其余情况给一个必 reject 的桩，口径同 src/plugin-loader.js:124-126。
// permissions 缺段、为 null、为字符串或其它非数组，一律按「未声明」处理。
function extensionPermissions(manifest) {
  const value = manifest && typeof manifest === 'object' ? manifest.permissions : null;
  return Array.isArray(value) ? value : [];
}
function toolFetch(manifest) {
  return extensionPermissions(manifest).includes('web_fetch')
    ? globalThis.fetch
    : () => Promise.reject(new Error('该扩展未声明 web_fetch 权限，无法出网'));
}
function allowedDependencyIds() {
  let value;
  try { value = JSON.parse(process.env.QQ_AGENT_DEPENDENCY_IDS || '[]'); }
  catch { throw failure('dependency-allowlist-invalid', 'QQ_AGENT_DEPENDENCY_IDS 必须是 JSON id 数组'); }
  if (!Array.isArray(value) || value.some((id) => typeof id !== 'string' || !ID.test(id))) {
    throw failure('dependency-allowlist-invalid', 'QQ_AGENT_DEPENDENCY_IDS 含非法扩展 id');
  }
  return new Set(value);
}
function hasManifest(dir) {
  return ['skill.json', 'plugin.json'].some((name) => fs.existsSync(path.join(dir, name)));
}
function extensionRoot(dir, depth = 0) {
  if (hasManifest(dir) || depth >= 4) return dir;
  const entries = fs.readdirSync(dir, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && !entry.name.startsWith('.') && entry.name !== '__MACOSX')
    .map((entry) => path.join(dir, entry.name));
  if (entries.length === 1) return extensionRoot(entries[0], depth + 1);
  const matching = entries.filter(hasManifest);
  return matching.length === 1 ? extensionRoot(matching[0], depth + 1) : dir;
}
function readManifest(dir, fallbackId) {
  const file = ['skill.json', 'plugin.json'].map((name) => path.join(dir, name))
    .find((name) => fs.existsSync(name));
  if (!file) throw failure('manifest-missing', `缺少 skill.json / plugin.json：${fallbackId}`);
  if (fs.lstatSync(file).isSymbolicLink() || fs.statSync(file).size > 256 * 1024) {
    throw failure('manifest-unsafe', `清单是链接或超过大小限制：${fallbackId}`);
  }
  let raw;
  try { raw = JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, '')); }
  catch (error) { throw failure('manifest-invalid', `清单解析失败 ${fallbackId}：${error.message}`); }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw failure('manifest-invalid', `清单必须是对象：${fallbackId}`);
  }
  const id = String(raw.id || fallbackId);
  if (!ID.test(id)) throw failure('manifest-invalid', `扩展 id 不合法：${id}`);
  const names = (key) => {
    const value = raw[key] ?? [];
    if (!Array.isArray(value) || value.length > 128 || value.some((name) => typeof name !== 'string' || !name.trim())) {
      throw failure('manifest-invalid', `${id}.${key} 必须是非空能力名数组`);
    }
    return [...new Set(value)];
  };
  return { id, dir, raw, requires: names('requires'), capabilities: names('capabilities') };
}
function assertCopyableTree(root) {
  let files = 0;
  let bytes = 0;
  const visit = (dir) => {
    if (fs.lstatSync(dir).isSymbolicLink()) throw failure('extension-unsafe-path', '扩展目录不能是符号链接');
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const child = path.join(dir, entry.name);
      const stat = fs.lstatSync(child);
      if (stat.isSymbolicLink() || (!stat.isDirectory() && !stat.isFile())) {
        throw failure('extension-unsafe-path', `扩展包含链接或特殊文件：${entry.name}`);
      }
      if (stat.isDirectory()) visit(child);
      else {
        files += 1;
        bytes += stat.size;
        if (files > MAX_FILES || bytes > MAX_BYTES) throw failure('extension-too-large', '扩展文件数量或大小超过限制');
      }
    }
  };
  visit(root);
}
function extractZip(archive, destination) {
  // 不先用 tar 解包：先验证 ZIP 全部条目，再写入临时目录；拒绝软链与路径穿越。
  const script = `import pathlib,re,stat,sys,zipfile
root=pathlib.Path(sys.argv[2]).resolve()
with zipfile.ZipFile(sys.argv[1]) as archive:
 infos=archive.infolist()
 if len(infos)>4000 or sum(i.file_size for i in infos)>24*1024*1024: raise ValueError('zip-too-large')
 for i in infos:
  name=i.filename.replace('\\\\','/')
  parts=pathlib.PurePosixPath(name).parts
  if not name or name.startswith('/') or re.match(r'^[A-Za-z]:',name) or '..' in parts or ':' in name or '\\x00' in name: raise ValueError('zip-unsafe-path')
  mode=(i.external_attr>>16)&0o170000
  if mode not in (0,stat.S_IFREG,stat.S_IFDIR): raise ValueError('zip-unsafe-type')
  if not pathlib.Path(root/name).resolve().is_relative_to(root): raise ValueError('zip-unsafe-path')
 archive.extractall(root)
`;
  try { execFileSync(process.env.PYTHON || 'python', ['-c', script, archive, destination], { stdio: 'pipe', timeout: 10000 }); }
  catch (error) { throw failure('zip-invalid', `ZIP 解包被拒绝：${String(error.stderr || error.message).trim().slice(-300)}`); }
}
function installedCandidates(selected) {
  const candidates = [selected];
  const realSource = fs.realpathSync(source);
  let count = 0;
  for (const category of ['plugins', 'skills']) {
    const root = path.join(source, category);
    if (!fs.existsSync(root)) continue;
    if (fs.lstatSync(root).isSymbolicLink() || !inside(realSource, fs.realpathSync(root))) {
      throw failure('dependency-unsafe-path', `依赖根目录越界：${category}`);
    }
    for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
      if (++count > 2048) throw failure('dependency-too-many', '已安装扩展目录过多');
      if (!entry.isDirectory() || entry.name.startsWith('.')) continue;
      const dir = path.join(root, entry.name);
      if (!inside(fs.realpathSync(root), fs.realpathSync(dir))) continue;
      try {
        const candidate = readManifest(dir, entry.name);
        // 本次传入的扩展覆盖同 id 的已安装副本；不执行那个副本。
        if (candidate.id !== selected.id) candidates.push(candidate);
      } catch { /* 无关坏清单不影响本次；需要它时会得到缺失依赖错误。 */ }
    }
  }
  return candidates;
}
function planDependencies(selected, allowed) {
  const candidates = selected.requires.length ? installedCandidates(selected) : [selected];
  const visiting = [];
  const complete = new Set();
  const ordered = [];
  const visit = (item) => {
    if (visiting.includes(item.id)) {
      throw failure('dependency-cycle', `依赖环：${[...visiting, item.id].join(' -> ')}`);
    }
    if (complete.has(item.id)) return;
    if (complete.size + visiting.length >= MAX_EXTENSIONS) throw failure('dependency-too-many', '依赖闭包超过 64 个扩展');
    visiting.push(item.id);
    item.dependencies = [];
    for (const capability of item.requires) {
      const installed = candidates.filter((candidate) => candidate.capabilities.includes(capability));
      if (!installed.length) throw failure('dependency-missing', `${item.id} 缺少已安装的能力提供者：${capability}`);
      const enabled = installed.filter((candidate) => candidate === selected || allowed.has(candidate.id));
      if (!enabled.length) throw failure('dependency-disabled', `${item.id} 的依赖未启用：${capability}（${installed.map((candidate) => candidate.id).join(', ')}）`);
      if (enabled.length !== 1) throw failure('dependency-ambiguous', `${item.id} 的能力有多个已启用提供者：${capability}（${enabled.map((candidate) => candidate.id).join(', ')}）`);
      const provider = enabled[0];
      item.dependencies.push({ capability, id: provider.id });
      visit(provider);
    }
    visiting.pop();
    complete.add(item.id);
    ordered.push(item);
  };
  visit(selected);
  return ordered;
}

async function run() {
  if (!fs.existsSync(input)) throw failure('extension-missing', 'extension not found');
  const allowed = allowedDependencyIds();
  temp = fs.mkdtempSync(path.join(os.tmpdir(), 'qqagent-one-'));
  let root = input;
  if (input.toLowerCase().endsWith('.zip')) {
    root = path.join(temp, 'extract');
    fs.mkdirSync(root);
    extractZip(input, root);
  }
  if (!fs.statSync(root).isDirectory()) throw failure('extension-invalid', '扩展必须是目录或 ZIP');
  root = extensionRoot(root);
  const selected = readManifest(root, '_compat_one');
  const ordered = planDependencies(selected, allowed);
  dependencyPlan = ordered.filter((item) => item !== selected).map((item) => ({ id: item.id, requires: item.requires, capabilities: item.capabilities }));
  for (const item of ordered) assertCopyableTree(item.dir);
  targetRoot = path.join(source, `${kind === 'skills' ? 'skills' : 'plugins'}-host-${process.pid}`);
  // 不复用已有目录，避免覆盖另一轮残留内容或链接。
  fs.mkdirSync(targetRoot);
  fs.writeFileSync(path.join(targetRoot, '.whale-host-one-owner.json'), JSON.stringify({ pid: process.pid, source: path.resolve(source) }));
  const stagedNames = [];
  for (let index = 0; index < ordered.length; index += 1) {
    const item = ordered[index];
    // 无 id 的旧包保留 _compat_one fallback；此时它总是排在依赖后。
    const name = item === selected && !item.raw.id ? '_compat_one' : `${String(index).padStart(4, '0')}-${item.id}`;
    stagedNames.push(name);
    item.staged = path.join(targetRoot, name);
    fs.cpSync(item.dir, item.staged, { recursive: true, errorOnExist: true });
  }
  const actualOrder = fs.readdirSync(targetRoot, { withFileTypes: true }).filter((entry) => entry.isDirectory()).map((entry) => entry.name);
  if (JSON.stringify(actualOrder) !== JSON.stringify(stagedNames)) {
    throw failure('dependency-order-unsupported', '当前文件系统不能保证依赖拓扑加载顺序');
  }
  const empty = path.join(temp, 'empty');
  fs.mkdirSync(empty);
  process.env.QQ_AGENT_DATA_DIR ||= path.join(source, 'data-host-one');
  const { isSkillEnabledInConfig } = await import(pathToFileURL(path.join(source, 'src/skills/config.js')).href);
  // Python 权威启用表和宿主自身关闭状态都要通过；绝不写配置或自动开启。
  for (const item of ordered) {
    if (!isSkillEnabledInConfig(item.id, item.raw.enabledByDefault !== false)) {
      throw failure(item === selected ? 'extension-disabled' : 'dependency-disabled', `宿主配置未启用：${item.id}`);
    }
  }
  const { loadPlugins } = await import(pathToFileURL(path.join(source, 'src/plugin-loader.js')).href);
  const { skillManager } = await import(pathToFileURL(path.join(source, 'src/skills/manager.js')).href);
  const { getTool, listTools, getToolAvailability } = await import(pathToFileURL(path.join(source, 'src/tool-registry.js')).href);
  manager = skillManager;
  const loaded = await loadPlugins({ roots: { plugins: kind === 'plugins' ? targetRoot : empty, skills: kind === 'skills' ? targetRoot : empty }, log: (...args) => console.error(...args) });
  const result = loaded.loaded.find((item) => path.resolve(item.dir) === path.resolve(selected.staged));
  const item = result?.skill;
  if (loaded.failed.length || !item) {
    throw failure('extension-load-failed', loaded.failed.map((entry) => `${entry.id}: ${entry.error}`).join('; ') || '被选扩展没有加载成功');
  }
  // 使用 loader 已导入的实例，不能再次 import 无缓存戳的入口执行顶层代码。
  extension = { id: item.manifest.id, name: item.manifest.name, version: item.manifest.version,
    tools: item.toolIds || [], loadError: item.loadError || '',
    providers: Object.keys(item.providers || {}), hooks: Object.keys(item.hooks || {}) };
  const context = hostContext();
  // Loading registers providers; a dependency may only make them usable after
  // activate (for example command.dispatch installs its local ingress hook).
  // Activate in the same dependency order and await asynchronous setup. All
  // instances are deactivated/disposed in finally, including failed calls.
  for (const entry of ordered) {
    const instance = manager.registry.get(entry.id);
    if (typeof instance?.activate === 'function') {
      try { await instance.activate({ reason: 'host-call' }); }
      catch (error) { throw failure('extension-activation-failed', `${entry.id} 激活失败：${error.message}`); }
    }
  }
  for (const entry of ordered) {
    for (const dependency of entry.dependencies) {
      const provider = manager.registry.get(dependency.id);
      if (typeof provider?.providers?.[dependency.capability] !== 'function') {
        throw failure('dependency-unimplemented', `${dependency.id} 声明但没有实现能力：${dependency.capability}`);
      }
    }
    const status = manager.isActive(entry.id, context);
    if (!status.active) throw failure(entry === selected ? 'extension-unavailable' : 'dependency-unavailable', `${entry.id} 不可用：${status.reason}`);
  }
  const capability = process.argv[4] || '';
  let toolResult = null;
  let hookResult = null;
  let callResult = null;
  const owned = (tool) => tool?.skillId === item.manifest.id && item.toolIds.includes(tool.id);
  if (capability === 'tool') {
    const toolId = process.argv[5] || '';
    const payload = JSON.parse(process.argv[6] || '{}');
    const tool = getTool(toolId);
    if (!owned(tool)) throw failure('tool-not-owned', 'tool 不属于被选扩展：' + toolId);
    const availability = getToolAvailability(toolId, { runtimeContext: context });
    if (!availability.enabled) throw failure('tool-unavailable', availability.reason);
    // dual-shape 的摊平顺序：payload 在前，老式 execute(payload) 仍从 ctx 读到业务参数；
    // 可信的会话 ctx 与宿主回调在后覆盖——模型实参伪造 chatKey/senderId/target，
    // 或把 fetch/log/metadata 换成自己的，都不生效。新式 execute(ctx, args) 的第二参
    // 仍是原始 payload，业务参数不受影响。
    const toolCtx = { ...payload, ...context, signal: null, log: (...args) => console.error(...args), metadata: {}, fetch: toolFetch(selected.raw) };
    toolResult = await tool.execute(toolCtx, payload);
  } else if (capability === 'hook') {
    const hookName = process.argv[5] || '';
    const payload = JSON.parse(process.argv[6] || '{}');
    const hook = item.hooks?.[hookName];
    if (typeof hook !== 'function') throw failure('hook-not-found', 'hook not found: ' + hookName);
    // 0.4 的 hook 允许直接改 ctx（例如 before-llm-messages 往 messages 末尾加小抄）而不返回值。
    // 宿主是独立进程，Python 侧拿不到这次原地修改，所以把改过的顶层字段作为结果带回；
    // 没改就维持 undefined，避免把整个 payload 当结果。
    const before = JSON.stringify(payload);
    hookResult = await hook(payload, { source: 'whale-bridge' });
    if (hookResult === undefined) {
      const after = JSON.stringify(payload);
      if (after !== before) {
        const original = JSON.parse(before);
        const delta = {};
        for (const [key, value] of Object.entries(payload)) {
          if (JSON.stringify(value) !== JSON.stringify(original[key])) delta[key] = value;
        }
        if (Object.keys(delta).length) hookResult = delta;
      }
    }
  } else if (capability) {
    const provider = item.providers?.[capability];
    if (typeof provider !== 'function') throw failure('provider-not-found', 'provider not found: ' + capability);
    callResult = await provider(JSON.parse(process.argv[5] || '{}'));
  }
  return { ok: true, loadable: true, loaded: loaded.loaded.length, failed: [], extension, error: '',
    dependencies: dependencyPlan, provider: capability, provider_result: callResult,
    tool_result: toolResult, hook_result: hookResult,
    tools: process.env.QQAGENT_BRIEF === '1' ? [] : listTools().filter(owned).map((tool) => ({
      id: tool.id, name: tool.name, description: tool.description, parameters: tool.parameters, inputSchema: tool.inputSchema })) };
}

let timer = null;
let output;
try {
  const requested = Number(process.env.QQ_AGENT_HOST_TIMEOUT_MS);
  const milliseconds = Number.isFinite(requested) && requested > 0 ? Math.max(100, Math.min(60000, requested)) : 10000;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => reject(failure('host-timeout', `扩展宿主执行超时（${milliseconds}ms）`)), milliseconds);
  });
  output = await Promise.race([run(), deadline]);
} catch (error) {
  output = { ok: false, loadable: false, loaded: 0, failed: [], extension,
    dependencies: dependencyPlan, error: error.message || String(error), error_code: error.code || 'host-error' };
} finally {
  if (timer) clearTimeout(timer);
  if (manager) {
    for (const item of [...manager.registry.list()].reverse()) {
      try { manager.unregister(item.manifest.id, { reason: 'host-complete' }); } catch {}
    }
  }
  for (const dir of [targetRoot, temp]) {
    if (!dir) continue;
    try { fs.rmSync(dir, { recursive: true, force: true, maxRetries: 3, retryDelay: 50 }); }
    catch (error) { console.error('cleanup skipped:', error.code || error.message); }
  }
}
console.log('__WHALE_JSON__' + JSON.stringify(output));
// 扩展留下的 interval/watch/spawn 句柄不能延长单次宿主生命周期。
process.exit(0);
