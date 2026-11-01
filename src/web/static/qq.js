// 鲸鱼娘 QQ 控制台 JS
const $ = id => document.getElementById(id);
let busy = false, dirty = false;
let currentPlugin = null;  // 当前正在编辑的插件
let allPlugins = [];       // 所有插件数据缓存
let polling = true;

async function showCompatMode() {
  try {
    const identity = await api('runtime');
    if (identity.console_mode !== 'browser') return;
    $('compat-banner').hidden = false;
    if (identity.compat_reason) $('compat-reason').textContent = identity.compat_reason;
  } catch (e) { /* 正常状态刷新仍会显示后台连接错误。 */ }
}

$('quit-console').addEventListener('click', () => {
  action(async () => {
    await api('runtime/quit', {});
    polling = false;
    notice('控制台正在退出，可以关闭网页。');
  });
});

function notice(text, ok = true) {
  $('notice').textContent = text;
  $('notice').className = 'notice ' + (ok ? 'ok' : 'error');
}

async function api(path, body) {
  const res = await fetch('/api/' + path, body === undefined
    ? {}
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await res.json();
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请检查表单内容');
  return data;
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach((b, i) => {
    const panels = ['settings', 'plugins', 'persona', 'messages', 'logs'];
    b.classList.toggle('active', panels[i] === name);
  });
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  $('tab-' + name).classList.add('active');
  if (name === 'plugins') loadPlugins();
  if (name === 'persona') loadPersona();
  if (name === 'logs') enterLogs(); else exitLogs();
}

// ── Settings ──────────────────────────────────────────────────────────────────
function toggleMode() {
  const official = $('mode').value === 'official';
  $('official-fields').hidden = !official;
  $('onebot-fields').hidden = official;
}

async function load() {
  const s = await api('settings');
  $('mode').value = s.mode;
  $('app-id').value = s.app_id;
  $('sandbox').checked = s.sandbox;
  $('groups').value = s.groups.join('\n');
  $('private').checked = s.listen_private;
  $('api-url').value = s.api_url;
  $('model').value = s.model;
  $('api-style').value = s.api_style;
  for (const id of ['api-key', 'app-secret', 'onebot-token']) $(id).value = '';
  $('saved-secrets').textContent = `已保存密钥：API ${s.api_key_set ? '✓' : '未设置'} · 官方 QQ ${s.app_secret_set ? '✓' : '未设置'} · 普通 QQ ${s.onebot_token_set ? '✓' : '未设置'}`;
  toggleMode();
  dirty = false;
}

// ── Status refresh ────────────────────────────────────────────────────────────
function list(id, items, render) {
  const root = $(id);
  root.replaceChildren();
  if (!items.length) { root.textContent = '暂无记录'; return; }
  items.forEach(item => {
    const e = document.createElement('div');
    e.className = 'message';
    render(e, item);
    root.append(e);
  });
}
function line(parent, text, cls = '') {
  const e = document.createElement('div');
  e.className = cls;
  e.textContent = text;
  parent.append(e);
}
const states = { pending: '等待处理', sending: '发送中', sent: '已回复', failed: '处理失败', uncertain: '发送待核实', expired: '已过期' };

async function refresh() {
  if (!polling) return;
  try {
    const s = await api('status');
    if (!polling) return;
    $('running').textContent = s.running ? '运行中' : '已停止';
    $('model-status').textContent = s.model || '未配置模型';
    $('ai-status').textContent = s.ai_test;
    if (s.mode === 'official') {
      $('connection').textContent = s.official.authenticated ? '官方 API 鉴权成功' : '等待官方 API 鉴权';
      $('connection-detail').textContent = s.official.error || (s.official.last_callback ? '最近回调：' + new Date(s.official.last_callback * 1000).toLocaleString() : '尚未收到 QQ 回调');
    } else {
      $('connection').textContent = s.onebot.online ? 'QQ 已在线 · ' + s.onebot.self_id : (s.onebot.connected ? '客户端已连接，验证登录中' : '等待普通 QQ 客户端');
      $('connection-detail').textContent = s.onebot.error || '连接中断后由客户端自动重连';
    }
    list('messages', s.messages, (e, m) => {
      line(e, `${new Date(m.time * 1000).toLocaleTimeString()} · ${m.sender} · ${states[m.state] || m.state}`, 'meta');
      line(e, m.text);
      if (m.reply) line(e, m.reply, 'reply');
      if (m.error) line(e, m.error, 'error');
    });
    list('events', s.events.slice().reverse(), (e, m) => {
      line(e, new Date(m.time * 1000).toLocaleTimeString(), 'meta');
      line(e, m.text);
    });
  } catch (e) {
    if (!polling) return;
    $('running').textContent = '控制台连接中断';
    notice('暂时无法连接本地服务，将自动重试。', false);
  }
}

async function action(fn) {
  if (busy) return;
  busy = true;
  document.querySelectorAll('button').forEach(b => b.disabled = true);
  try { await fn(); if (polling) await refresh(); }
  catch (e) { notice(e.message, false); }
  finally { busy = false; document.querySelectorAll('button').forEach(b => b.disabled = false); }
}

// ── Plugin tab ────────────────────────────────────────────────────────────────
async function loadPlugins() {
  const list = $('plugin-list');
  try {
    allPlugins = await api('plugins');
    renderPluginList();
  } catch (e) {
    list.innerHTML = `<p class="error">加载插件失败：${e.message}</p>`;
  }
}

function renderPluginList() {
  const list = $('plugin-list');
  list.replaceChildren();
  const categoryLabels = {
    rhythm: '节奏', message: '消息', utility: '工具', trigger: '触发', proactive: '主动'
  };
  allPlugins.forEach(p => {
    const row = document.createElement('div');
    row.className = 'plugin-row';
    const enabled = p.settings.enabled !== false;
    row.innerHTML = `
      <label class="toggle">
        <input type="checkbox" ${enabled ? 'checked' : ''} onchange="togglePlugin('${p.id}', this.checked)">
        <span class="toggle-slider"></span>
      </label>
      <div class="plugin-info">
        <div class="plugin-name">${p.name}<span class="plugin-tag">${categoryLabels[p.category] || p.category}</span></div>
        <div class="plugin-desc">${p.description}</div>
      </div>
      <button class="secondary" style="font-size:12px;padding:7px 12px;flex-shrink:0" onclick="openPluginModal('${p.id}')">配置</button>
    `;
    list.append(row);
  });
}

async function togglePlugin(pluginId, enabled) {
  try {
    const p = allPlugins.find(x => x.id === pluginId);
    if (!p) return;
    const newSettings = { ...p.settings, enabled };
    await api('plugins/' + pluginId, newSettings);
    p.settings.enabled = enabled;
    notice(`插件「${p.name}」已${enabled ? '启用' : '禁用'}`, true);
  } catch (e) {
    notice('保存失败：' + e.message, false);
    loadPlugins();  // 刷新状态
  }
}

// ── Plugin config modal ───────────────────────────────────────────────────────
function openPluginModal(pluginId) {
  const p = allPlugins.find(x => x.id === pluginId);
  if (!p) return;
  currentPlugin = p;
  $('modal-title').textContent = `🧩 ${p.name}`;

  const body = $('modal-body');
  body.replaceChildren();

  // 描述
  const desc = document.createElement('p');
  desc.style.cssText = 'color:#7a94b0;font-size:13px;margin:0 0 16px';
  desc.textContent = p.description;
  body.append(desc);

  // 按 group 分组渲染
  const schema = p.schema || {};
  const settings = p.settings || {};
  const groups = {};

  Object.entries(schema).forEach(([key, field]) => {
    if (key === 'enabled') return;  // enabled 由列表页的开关控制
    const g = field.group || '_default';
    if (!groups[g]) groups[g] = [];
    groups[g].push([key, field]);
  });

  Object.entries(groups).forEach(([groupName, fields]) => {
    if (groupName !== '_default') {
      const gt = document.createElement('div');
      gt.className = 'field-group-title';
      gt.textContent = groupName;
      const gd = document.createElement('div');
      gd.className = 'field-group';
      gd.append(gt);
      fields.forEach(([key, field]) => gd.append(buildField(key, field, settings[key] ?? p.defaults[key])));
      body.append(gd);
    } else {
      fields.forEach(([key, field]) => body.append(buildField(key, field, settings[key] ?? p.defaults[key])));
    }
  });

  $('plugin-modal').hidden = false;
}

function buildField(key, field, value) {
  const wrap = document.createElement('div');
  wrap.className = 'schema-row';

  const labelEl = document.createElement('div');
  labelEl.className = 'schema-label';
  labelEl.textContent = field.label || key;

  if (field.description) {
    const descEl = document.createElement('div');
    descEl.className = 'schema-desc';
    descEl.textContent = field.description;
    wrap.append(labelEl, descEl);
  } else {
    wrap.append(labelEl);
  }

  let input;
  const type = schemaType(field);
  if (field.file_picker || field.editor) {
    const info = document.createElement('p');
    info.textContent = '此项的文件上传或专用编辑器请在桌面控制台使用；本页保留已有配置。';
    wrap.append(info);
    return wrap;
  }
  if (type === 'boolean') {
    const lbl = document.createElement('label');
    lbl.style.cssText = 'display:flex;align-items:center;gap:8px;cursor:pointer';
    input = document.createElement('input');
    input.type = 'checkbox';
    input.id = 'field_' + key;
    input.checked = Boolean(value);
    lbl.append(input, document.createTextNode(field.label || key));
    wrap.replaceChildren(lbl);
    if (field.description) {
      const d = document.createElement('div');
      d.className = 'schema-desc';
      d.textContent = field.description;
      wrap.append(d);
    }
  } else if (field.type === 'enum') {
    input = document.createElement('select');
    input.id = 'field_' + key;
    (field.options || (field.values || []).map(v => ({ value: v, label: v }))).forEach(opt => {
      const o = document.createElement('option');
      o.value = opt.value || opt;
      o.textContent = opt.label || opt.value || opt;
      if (String(value) === String(o.value)) o.selected = true;
      input.append(o);
    });
    wrap.append(input);
  } else if (type === 'number' && field.min !== undefined && field.max !== undefined) {
    const rowEl = document.createElement('div');
    rowEl.className = 'schema-range-row';
    input = document.createElement('input');
    input.type = 'range';
    input.id = 'field_' + key;
    input.min = field.min;
    input.max = field.max;
    input.step = field.step || (field.type === 'float' ? 0.1 : 1);
    input.value = value ?? field.min;
    const valEl = document.createElement('span');
    valEl.className = 'schema-range-val';
    valEl.textContent = input.value;
    input.oninput = () => valEl.textContent = input.value;
    rowEl.append(input, valEl);
    wrap.append(rowEl);
  } else if (field.multiline) {
    input = document.createElement('textarea');
    input.id = 'field_' + key;
    input.rows = 4;
    input.value = value ?? '';
    wrap.append(input);
  } else {
    input = document.createElement('input');
    input.id = 'field_' + key;
    input.type = type === 'number' ? 'number' : 'text';
    input.value = value ?? '';
    wrap.append(input);
  }
  return wrap;
}

function schemaType(field) {
  return ({ bool: 'boolean', int: 'number', float: 'number' })[field.type] || field.type;
}

async function savePluginSettings() {
  if (!currentPlugin) return;
  const schema = currentPlugin.schema || {};
  const newSettings = { ...currentPlugin.settings };

  Object.entries(schema).forEach(([key, field]) => {
    const el = $('field_' + key);
    if (!el) return;
    if (schemaType(field) === 'boolean') newSettings[key] = el.checked;
    else if (schemaType(field) === 'number') newSettings[key] = Number(el.value);
    else newSettings[key] = el.value;
  });

  try {
    await api('plugins/' + currentPlugin.id, newSettings);
    currentPlugin.settings = newSettings;
    notice(`「${currentPlugin.name}」配置已保存`, true);
    closePluginModal();
    renderPluginList();
  } catch (e) {
    notice('保存失败：' + e.message, false);
  }
}

function closePluginModal() {
  $('plugin-modal').hidden = true;
  currentPlugin = null;
}

// Close modal on backdrop click
$('plugin-modal').addEventListener('click', e => {
  if (e.target === $('plugin-modal')) closePluginModal();
});

// ── Persona tab ───────────────────────────────────────────────────────────────
async function loadPersona() {
  try {
    const data = await api('personas');
    const select = $('persona-select');
    select.replaceChildren();
    for (const persona of data.personas) {
      const option = document.createElement('option');
      option.value = persona.name;
      option.textContent = persona.display_name || persona.name;
      option.selected = persona.name === data.active;
      select.append(option);
    }
    await showPersona();
  } catch (e) {
    $('persona-prompt').textContent = '读取失败：' + e.message;
  }
}

async function showPersona() {
  try {
    const detail = await api('personas/' + encodeURIComponent($('persona-select').value));
    $('persona-prompt').textContent = detail.system_prompt || '（未填写人设正文）';
  } catch (e) { notice(e.message, false); }
}
$('persona-select').addEventListener('change', showPersona);
$('persona-activate').addEventListener('click', () => action(async () => {
  const result = await api('personas/' + encodeURIComponent($('persona-select').value) + '/activate', {});
  notice(result.message);
}));

// ── Event wiring ──────────────────────────────────────────────────────────────
$('settings').addEventListener('input', () => dirty = true);
$('mode').addEventListener('change', toggleMode);
$('settings').addEventListener('submit', e => {
  e.preventDefault();
  action(async () => {
    const result = await api('settings', {
      mode: $('mode').value, app_id: $('app-id').value, app_secret: $('app-secret').value,
      sandbox: $('sandbox').checked, onebot_token: $('onebot-token').value,
      groups: $('groups').value.split('\n').filter(x => x.trim()),
      listen_private: $('private').checked,
      api_url: $('api-url').value, api_key: $('api-key').value,
      model: $('model').value, api_style: $('api-style').value
    });
    await load();
    notice(result.message);
  });
});

for (const [id, path] of [['start', 'start'], ['stop', 'stop'], ['test-ai', 'test-ai'], ['test-qq', 'test-qq']]) {
  $(id).addEventListener('click', () => action(async () => {
    if (dirty && id !== 'stop') throw new Error('有未保存的修改，请先保存配置。');
    notice('正在处理，请稍候…');
    const r = await api(path, {});
    notice(r.message, r.success);
  }));
}

async function poll() {
  if (!polling) return;
  await refresh();
  if (polling) setTimeout(poll, 3000);
}
load()
  .then(() => notice('已读取配置。保存并测试后，即可启动 QQ 机器人。'))
  .catch(e => notice(e.message, false));
showCompatMode();
poll();

// ── 日志 tab ──────────────────────────────────────────────────────────────────
const LOG_LEVELS = { error: '错误', warning: '警告', info: '信息', debug: '调试' };
let logEntries = [];      // 当前筛选结果
let logAutoTimer = null;  // 自动刷新定时器
let logApiReady = null;   // null 未知 / true 可用 / false 未就绪

function reportClientError(level, message, detail, code) {
  try {
    fetch('/api/logs/client', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        level: level, source: 'web',
        message: String(message).slice(0, 500),
        detail: String(detail || '').slice(0, 8000),
        code: code || ''
      })
    }).catch(() => {});
  } catch (e) { /* 上报失败必须静默，不能因为上报再抛错。 */ }
}

// 只在真浏览器里挂全局兜底：node vm 夹具和别的宿主没有 window，
// 顶层直接摸 window 会让整份脚本加载失败（比丢掉上报严重得多）。
if (typeof window !== 'undefined' && window.addEventListener) {
  window.addEventListener('error', e => {
    const err = e && e.error;
    reportClientError('error', (e && e.message) || '脚本错误', (err && err.stack) || '', 'web-error');
  });
  window.addEventListener('unhandledrejection', e => {
    const reason = e && e.reason;
    reportClientError('error', (reason && reason.message) || String(reason || '未处理的 Promise 拒绝'), (reason && reason.stack) || '', 'web-rejection');
  });
}

function logQuery(limit) {
  const params = new URLSearchParams();
  const level = $('log-level').value;
  const source = $('log-source').value;
  const query = $('log-query').value.trim();
  if (level) params.set('level', level);
  if (source) params.set('source', source);
  if (query) params.set('query', query);
  params.set('limit', String(limit));
  return params.toString();
}

function setLogStatus(text, ok) {
  const el = $('log-status');
  if (!el) return;
  el.textContent = text;
  el.className = 'log-status' + (ok === false ? ' error' : '');
}

function formatLogTime(ts) {
  const d = new Date(ts);
  if (isNaN(d.getTime())) return String(ts || '');
  const p = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' '
    + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

function levelClass(level) { return 'log-' + (LOG_LEVELS[level] ? level : 'debug'); }

function logLine(entry) {
  return formatLogTime(entry.ts) + ' ' + (entry.level || '') + ' ' + (entry.source || '') + ' ' + (entry.message || '');
}

function logText(entry) { return entry.detail ? logLine(entry) + '\n' + entry.detail : logLine(entry); }

async function loadLogs() {
  if (!$('log-list')) return;
  try {
    const res = await fetch('/api/logs?' + logQuery(200));
    if (res.status === 404) throw new Error('not-ready');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    logApiReady = true;
    logEntries = Array.isArray(data && data.entries) ? data.entries : [];
    renderLogs();
    refreshLogSources();
    const total = (data && typeof data.total === 'number') ? data.total : logEntries.length;
    setLogStatus('共 ' + total + ' 条，显示 ' + logEntries.length + ' 条' + (data && data.truncated ? '（结果已截断）' : ''));
  } catch (e) {
    logApiReady = false;
    logEntries = [];
    renderLogs();
    setLogStatus('日志接口还没就绪', false);
  }
}

function renderLogs() {
  const root = $('log-list');
  if (!root) return;
  root.replaceChildren();
  if (!logEntries.length) {
    const empty = document.createElement('p');
    empty.className = 'hint';
    empty.style.padding = '12px';
    empty.textContent = '暂无日志。';
    root.append(empty);
    return;
  }
  logEntries.forEach(entry => {
    const row = document.createElement('div');
    row.className = 'log-row ' + levelClass(entry.level);
    const cells = [
      [formatLogTime(entry.ts), 'log-time'],
      [LOG_LEVELS[entry.level] || entry.level || '-', 'log-level'],
      [entry.source || '-', 'log-source-cell'],
      [entry.message || '', 'log-msg']
    ];
    cells.forEach(([text, cls]) => {
      const span = document.createElement('span');
      span.className = cls;
      span.textContent = text;
      row.append(span);
    });
    row.addEventListener('click', () => toggleLogDetail(entry));
    root.append(row, buildLogDetail(entry));
  });
}

function buildLogDetail(entry) {
  const box = document.createElement('div');
  box.className = 'log-detail';
  box.dataset.id = entry.id || '';
  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.textContent = '#' + (entry.id || '-')
    + (entry.code ? ' · 错误码 ' + entry.code : '')
    + ' · ' + formatLogTime(entry.ts);
  const pre = document.createElement('pre');
  pre.textContent = entry.detail || '（无详细信息）';
  const btn = document.createElement('button');
  btn.className = 'secondary';
  btn.textContent = '复制这条';
  btn.addEventListener('click', ev => { ev.stopPropagation(); copyLogEntry(entry); });
  box.append(meta, pre, btn);
  return box;
}

function logDetailFor(id) {
  const root = $('log-list');
  if (!root) return null;
  const key = id || '';
  return Array.from(root.children).find(el => el.classList.contains('log-detail') && el.dataset.id === key) || null;
}

function toggleLogDetail(entry) {
  const box = logDetailFor(entry.id);
  if (!box) return;
  const willOpen = !box.classList.contains('open');
  document.querySelectorAll('#log-list .log-detail.open').forEach(el => el.classList.remove('open'));
  if (willOpen) box.classList.add('open');
}

function refreshLogSources() {
  const select = $('log-source');
  if (!select) return;
  const current = select.value;
  const names = [];
  logEntries.forEach(entry => {
    if (entry.source && names.indexOf(entry.source) < 0) names.push(entry.source);
  });
  if (current && names.indexOf(current) < 0) names.push(current);
  names.sort();
  select.replaceChildren();
  const all = document.createElement('option');
  all.value = '';
  all.textContent = '全部';
  select.append(all);
  names.forEach(name => {
    const option = document.createElement('option');
    option.value = name;
    option.textContent = name;
    option.selected = name === current;
    select.append(option);
  });
}

async function copyText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) { /* 权限或非安全上下文，回落到 execCommand。 */ }
  try {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
    document.body.append(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    return ok;
  } catch (e) { return false; }
}

async function copyLogEntry(entry) {
  const ok = await copyText(logText(entry));
  setLogStatus(ok ? '已复制这一条。' : '复制失败，请手动选择文本。', ok);
}

async function copyAllLogs() {
  if (!logEntries.length) { setLogStatus('当前没有可复制的日志。', false); return; }
  const ok = await copyText(logEntries.map(logLine).join('\n'));
  setLogStatus(ok ? '已复制 ' + logEntries.length + ' 条日志。' : '复制失败，请手动选择文本。', ok);
}

async function exportLogs() {
  const url = '/api/logs/export?' + logQuery(1000);
  if (logApiReady === false) { setLogStatus('导出失败：日志接口未就绪', false); return; }
  try {
    const probe = await fetch(url, { method: 'HEAD' });
    if (!probe.ok) throw new Error('HTTP ' + probe.status);
  } catch (e) {
    logApiReady = false;
    setLogStatus('导出失败：日志接口未就绪', false);
    return;
  }
  window.open(url);
  setLogStatus('已开始下载导出文件。');
}

function stopLogAuto() {
  if (logAutoTimer) { clearInterval(logAutoTimer); logAutoTimer = null; }
}

function startLogAuto() {
  stopLogAuto();
  const toggle = $('log-auto');
  const panel = $('tab-logs');
  if (!toggle || !toggle.checked || !panel || !panel.classList.contains('active')) return;
  logAutoTimer = setInterval(() => {
    if (document.visibilityState === 'hidden') return;
    loadLogs();
  }, 5000);
}

function enterLogs() { loadLogs(); startLogAuto(); }
function exitLogs() { stopLogAuto(); }

if ($('log-level')) $('log-level').addEventListener('change', loadLogs);
if ($('log-source')) $('log-source').addEventListener('change', loadLogs);
if ($('log-refresh')) $('log-refresh').addEventListener('click', loadLogs);
if ($('log-auto')) $('log-auto').addEventListener('change', startLogAuto);
if ($('log-copy-all')) $('log-copy-all').addEventListener('click', copyAllLogs);
if ($('log-export')) $('log-export').addEventListener('click', exportLogs);
if ($('log-query')) $('log-query').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); loadLogs(); }
});

