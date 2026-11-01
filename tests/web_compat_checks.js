const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const path = require('path');
const nodes = new Map();
function node(tag = 'div') {
  const result = { tag, children: [], value: '', style: {}, hidden: false, classList: { add() {}, remove() {}, toggle() {} },
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; }, addEventListener() {} };
  Object.defineProperty(result, 'id', { set(value) { nodes.set(value, result); }, get() { return ''; } });
  return result;
}
const root = path.join(__dirname, '..');
for (const match of fs.readFileSync(path.join(root, 'src/web/static/qq.html'), 'utf8').matchAll(/id="([^"]+)"/g)) {
  nodes.set(match[1], node());
}
const context = vm.createContext({ document: {
  getElementById: id => nodes.get(id) || null, createElement: node,
  createTextNode: text => ({ text }), querySelectorAll: () => [] },
  fetch: async () => { throw new Error('offline fixture'); }, setTimeout() {}, console });
vm.runInContext(fs.readFileSync(path.join(root, 'src/web/static/qq.js'), 'utf8'), context);

(async () => {
  assert.equal(context.schemaType({ type: 'bool' }), 'boolean');
  assert.equal(context.schemaType({ type: 'int' }), 'number');
  assert.equal(context.schemaType({ type: 'float' }), 'number');
  context.buildField('flag', { type: 'bool' }, true);
  assert.equal(nodes.get('field_flag').type, 'checkbox');
  context.buildField('limit', { type: 'int', min: 1, max: 120 }, 30);
  assert.equal(nodes.get('field_limit').step, 1);
  context.buildField('ratio', { type: 'float', min: 0, max: 1 }, .5);
  assert.equal(nodes.get('field_ratio').step, .1);
  context.buildField('file', { type: 'string', file_picker: 'file' }, 'keep-path');
  assert.equal(nodes.get('field_file'), undefined);
  nodes.get('field_flag').checked = false;
  nodes.get('field_limit').value = '15';
  nodes.get('field_ratio').value = '0.6';
  vm.runInContext(`currentPlugin = {id:'fixture', settings:{file:'keep-path'}, schema:{
    flag:{type:'bool'}, limit:{type:'int'}, ratio:{type:'float'}, file:{type:'string',file_picker:'file'}}};
    api = async (path, body) => { globalThis.saved = {path, body}; return {}; };`, context);
  await context.savePluginSettings();
  assert.deepEqual(JSON.parse(JSON.stringify(context.saved.body)), { file: 'keep-path', flag: false, limit: 15, ratio: .6 });
  vm.runInContext(`polling=false; globalThis.requests=[]; api=async path=>{requests.push(path);return {}};`, context);
  await context.action(async () => {});
  assert.equal(vm.runInContext('requests.length', context), 0, '退出后不能再次刷新已停止的服务');
  console.log('Browser schema types, preserved file settings, and quit refresh checks passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
