// One server-owned provider library, reused by every purpose configuration entry.
import { gw, esc } from '../api.js?v=20261025-console-latency-r1';
import { modal, toast, icon, tag, loading, errorBlock } from '../ui.js?v=20261025-console-latency-r1';

const API = '/admin/api/model-config';
let pendingSaves = 0, uncertainSave = false;
const option = (value, label, selected = false) => `<option value="${esc(value)}"${selected ? ' selected' : ''}>${esc(label)}</option>`;
const detail = current => `${esc(current.provider_name || '未配置')} · ${esc(current.key_name || '现有密钥')} ${esc(current.label || '')} · ${esc(current.model || '未配置模型')}`;

export function createModelSettings(host, { isCurrent = () => true, onSaved = () => {}, onStatus = () => {} } = {}) {
  let disposed = false, active = true, sequence = 0, data = null, view = 'purposes', opening = null;
  const dialogs = new Set(), reads = new Set();
  const live = () => !disposed && isCurrent();
  const interactive = () => live() && active;
  const notifyStatus = () => onStatus(pendingSaves ? '模型配置仍在保存，关闭窗口不会取消已提交的修改。' :
    uncertainSave ? '上次保存结果未确认，请刷新核对当前生效配置。' : '');
  const accept = value => { if (!data || value.revision >= data.revision) data = value; };

  async function read() {
    const abort = new AbortController();
    reads.add(abort);
    try { return await gw(API, { signal: abort.signal }); }
    finally { reads.delete(abort); }
  }

  function render() {
    if (!interactive() || !data) return;
    host.innerHTML = `<div class="toolbar model-config-toolbar">
      <div class="subtabs"><button class="subtab ${view === 'purposes' ? 'active' : ''}" data-model-view="purposes">用途配置</button><button class="subtab ${view === 'providers' ? 'active' : ''}" data-model-view="providers">供应商库</button></div>
      <span class="grow"></span><button class="btn btn-primary" data-model-new>${icon('plus')}添加供应商</button><button class="btn btn-secondary" data-model-refresh>${icon('refresh')}刷新</button></div>
      ${view === 'providers' ? renderProviders() : renderPurposes()}`;
    host.querySelectorAll('[data-model-view]').forEach(button => { button.onclick = () => { view = button.dataset.modelView; render(); }; });
    host.querySelector('[data-model-new]').onclick = () => openProvider();
    host.querySelector('[data-model-refresh]').onclick = () => load();
    host.querySelectorAll('[data-model-purpose]').forEach(button => { button.onclick = () => openPurpose(button.dataset.modelPurpose); });
    host.querySelectorAll('[data-model-edit]').forEach(button => { button.onclick = () => openProvider(button.dataset.modelEdit); });
  }

  function renderPurposes() {
    return `<p class="muted text-sm">各用途独立选择，共用供应商库。保存后用于后续调用。</p>
      <div class="model-config-list">${data.purposes.map(p => `<div class="model-config-row">
        <div class="model-config-main"><strong>${esc(p.name)}</strong><div class="model-config-summary">${detail(p.current)}</div><div class="muted text-sm model-config-url">${esc(p.current.base_url || '未配置 API 地址')}</div>${p.current.error ? `<div class="model-config-error">${esc(p.current.error)}</div>` : ''}</div>
        <div class="model-config-actions">${tag(p.current.available === false ? '配置失效' : p.current.configured ? '已配置' : '未配置', p.current.configured ? 'green' : 'red')}<button class="btn btn-secondary btn-sm" data-model-purpose="${esc(p.id)}">调整配置</button></div></div>`).join('')}</div>
      <div class="section-title">向量模型</div><div class="model-config-row">
        <div class="model-config-main"><strong>记忆向量 · Embedding</strong><div class="model-config-summary">${detail(data.vector)}</div><div class="muted text-sm model-config-url">${esc(data.vector.base_url || '未配置')} · ${data.vector.dimensions} 维</div></div><div class="model-config-actions">${tag('仅查看', 'muted')}</div></div>`;
  }

  function renderProviders() {
    return `<p class="muted text-sm">添加一次，各用途均可选择。现有服务配置保留为只读来源。</p><div class="model-config-list">${data.providers.map(p => `<div class="model-config-row">
      <div class="model-config-main"><strong>${esc(p.name)}</strong><div class="model-config-summary">${esc(p.base_url)}</div><div class="muted text-sm">${p.keys.map(k => `${esc(k.name)} ${esc(k.label)}`).join(' · ')}<br>${p.models.map(esc).join(' · ')}</div></div>
      <div class="model-config-actions">${p.readonly ? tag('现有配置', 'muted') : `<button class="btn btn-secondary btn-sm" data-model-edit="${esc(p.id)}">编辑</button>`}</div></div>`).join('') || '<p class="muted">暂无供应商，请先添加。</p>'}</div>`;
  }

  async function load(options = {}) {
    if (!live()) return;
    active = true;
    reads.forEach(abort => abort.abort());
    opening = null;
    const own = ++sequence;
    if (options.view) view = options.view;
    host.innerHTML = loading();
    try {
      const result = await read();
      if (!interactive() || own !== sequence) return;
      accept(result);
      uncertainSave = false;
      notifyStatus();
      render();
      if (options.purpose) await openPurpose(options.purpose);
    } catch (error) {
      if (!interactive() || own !== sequence || error.name === 'AbortError') return;
      host.innerHTML = `${errorBlock(`模型配置读取失败：${esc(error.message)}`)}<button class="btn btn-secondary" data-model-retry>重试</button>`;
      host.querySelector('[data-model-retry]').onclick = () => load(options);
    }
  }

  function dialog(options) {
    let entry;
    entry = modal({ ...options, onDismiss: () => {
      entry.root.querySelectorAll('input,textarea').forEach(input => { input.value = ''; });
      dialogs.delete(entry);
    } });
    dialogs.add(entry);
    return entry;
  }

  async function fresh() {
    if (opening || !interactive()) return null;
    const ticket = opening = Symbol('model-open'), own = sequence;
    try {
      const value = await read();
      if (!interactive() || own !== sequence || opening !== ticket) return null;
      accept(value);
      render();
      return data;
    } catch (error) {
      if (interactive() && own === sequence && error.name !== 'AbortError') toast(`读取配置失败：${error.message}`, 'err');
      return null;
    } finally { if (opening === ticket) opening = null; }
  }

  async function reconcile() {
    if (!live()) return;
    const own = sequence;
    try {
      const result = await read();
      if (!live() || own !== sequence) return;
      accept(result);
      render();
    } catch (error) {
      if (live() && error.name !== 'AbortError') {
        uncertainSave = true;
        notifyStatus();
      }
    }
  }

  async function save(entry, endpoint, revision, value) {
    const button = entry.root.querySelector('[data-model-save]');
    const feedback = entry.root.querySelector('[data-model-error]');
    if (button.disabled) return;
    button.disabled = true;
    feedback.textContent = '';
    pendingSaves++;
    notifyStatus();
    try {
      const result = await gw(API + endpoint, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ revision, value }) });
      // A closed editor does not cancel the server transaction. Always notify
      // the current page, including a newer mount, to reconcile authoritative state.
      if (live()) {
        accept(result);
        if (dialogs.has(entry)) entry.close();
        render();
        if (interactive()) toast('配置已保存');
      }
      onSaved();
    } catch (error) {
      const unknown = error.resultUnknown || !Number.isInteger(error.status) || error.status >= 500;
      if (unknown) uncertainSave = true;
      if (live() && dialogs.has(entry)) feedback.textContent = unknown ? '保存结果未确认，请关闭窗口并刷新核对后再操作。' : error.message;
      if (unknown || (live() && !dialogs.has(entry))) {
        onSaved();
        await reconcile();
      }
    } finally {
      pendingSaves--;
      notifyStatus();
      if (dialogs.has(entry)) button.disabled = false;
    }
  }

  async function openPurpose(purpose) {
    const current = await fresh();
    const item = current?.purposes.find(p => p.id === purpose);
    if (!item || !interactive()) return;
    const selected = item.selection;
    const entry = dialog({ title: `${esc(item.name)} · API配置`, body: `
      <p class="muted text-sm">当前：${detail(item.current)}</p>
      ${item.current.error ? `<p class="model-config-error">${esc(item.current.error)} 可选择有效供应商，或恢复现有配置。</p>` : ''}
      <div class="field"><label>供应商<select data-model-provider aria-label="供应商">${option('', '使用现有配置', !selected)}${current.providers.map(p => option(p.id, p.name, p.id === selected?.provider_id)).join('')}</select></label></div>
      <div class="field"><label>API Key<select data-model-key aria-label="API Key"></select></label></div>
      <div class="field"><label>模型<select data-model-name aria-label="模型"></select></label></div>
      <p class="muted text-sm">需要新增供应商？前往 <a href="#/config?tab=api&view=providers" data-model-library>供应商库</a>。</p><div class="model-config-error" data-model-error role="alert"></div>`,
      footer: '<button class="btn btn-secondary" data-model-cancel>取消</button><button class="btn btn-primary" data-model-save>保存</button>' });
    const select = entry.root.querySelector('[data-model-provider]');
    const key = entry.root.querySelector('[data-model-key]');
    const model = entry.root.querySelector('[data-model-name]');
    const fill = () => {
      const provider = current.providers.find(p => p.id === select.value);
      key.disabled = model.disabled = !provider;
      key.innerHTML = provider ? provider.keys.map(k => option(k.id, `${k.name} ${k.label}`, k.id === selected?.key_id)).join('') : option('', '沿用现有密钥');
      model.innerHTML = provider ? provider.models.map(m => option(m, m, m === selected?.model)).join('') : option('', '沿用现有模型');
    };
    select.onchange = fill;
    fill();
    entry.root.querySelector('[data-model-library]').onclick = entry.close;
    entry.root.querySelector('[data-model-cancel]').onclick = entry.close;
    entry.root.querySelector('[data-model-save]').onclick = () => save(entry, `/routes/${purpose}`, current.revision,
      select.value ? { provider_id: select.value, key_id: key.value, model: model.value } : null);
  }

  async function openProvider(id = null) {
    const current = await fresh();
    if (!current || !interactive()) return;
    const provider = current.providers.find(p => p.id === id);
    if (id && (!provider || provider.readonly)) return;
    const entry = dialog({ title: provider ? '编辑供应商' : '添加供应商', wide: true, body: `
      <div class="model-provider-form">
      <div class="field"><label>供应商名称<input type="text" data-provider-name maxlength="120" value="${esc(provider?.name || '')}" autocomplete="off"></label></div>
      <div class="field"><label>API 地址<input type="text" data-provider-url value="${esc(provider?.base_url || '')}" placeholder="https://api.example.com/v1" autocomplete="off"></label></div>
      <div class="field" role="group" aria-label="模型名称"><label>模型名称</label><div data-provider-models></div><button type="button" class="btn btn-secondary btn-sm" data-provider-add-model>添加模型</button></div>
      <div class="field"><label>API Keys</label><div data-provider-keys></div><button class="btn btn-secondary btn-sm" data-provider-add-key>添加 Key</button><p class="muted text-sm">已有 Key 留空保留；填写新值后替换。正在使用的 Key 和模型不能移除。</p></div>
      <div class="model-config-error" data-model-error role="alert"></div></div>`,
      footer: '<button class="btn btn-secondary" data-model-cancel>取消</button><button class="btn btn-primary" data-model-save>保存供应商</button>' });
    const models = entry.root.querySelector('[data-provider-models]');
    function addModel(value = '', focus = false) {
      const row = document.createElement('div');
      row.className = 'model-config-model-row';
      row.innerHTML = `<input type="text" data-provider-model aria-label="模型名称" placeholder="填写模型名称" value="${esc(value)}" autocomplete="off"><button type="button" class="btn btn-quiet btn-sm" data-provider-remove-model aria-label="移除这个模型">${icon('x')}</button>`;
      row.querySelector('[data-provider-remove-model]').onclick = () => {
        const neighbor = row.nextElementSibling || row.previousElementSibling;
        row.remove();
        (neighbor?.querySelector('input') || entry.root.querySelector('[data-provider-add-model]')).focus();
      };
      models.appendChild(row);
      if (focus) row.querySelector('input').focus();
    }
    (provider?.models || ['']).forEach(value => addModel(value));
    entry.root.querySelector('[data-provider-add-model]').onclick = () => addModel('', true);
    const keys = entry.root.querySelector('[data-provider-keys]');
    function addKey(key = {}) {
      const row = document.createElement('div');
      row.className = 'model-config-key-row';
      row.dataset.keyId = key.id || '';
      row.innerHTML = `<input type="text" data-key-alias aria-label="Key 别名" placeholder="Key 别名" value="${esc(key.name || '')}" maxlength="120" autocomplete="off"><input type="password" data-key-secret aria-label="API Key 值" placeholder="${key.id ? '留空保留现有 Key' : '填写 API Key'}" autocomplete="new-password"><button class="btn btn-quiet btn-sm" data-key-remove aria-label="移除这把 Key">${icon('x')}</button>`;
      row.querySelector('[data-key-remove]').onclick = () => { row.querySelector('[data-key-secret]').value = ''; row.remove(); };
      keys.appendChild(row);
    }
    (provider?.keys || [{}]).forEach(addKey);
    entry.root.querySelector('[data-provider-add-key]').onclick = () => addKey();
    entry.root.querySelector('[data-model-cancel]').onclick = entry.close;
    entry.root.querySelector('[data-model-save]').onclick = () => {
      const value = { name: entry.root.querySelector('[data-provider-name]').value.trim(),
        base_url: entry.root.querySelector('[data-provider-url]').value.trim(),
        models: [...models.querySelectorAll('[data-provider-model]')].map(input => input.value.trim()).filter(Boolean),
        keys: [...keys.children].map(row => ({ ...(row.dataset.keyId ? { id: row.dataset.keyId } : {}),
          name: row.querySelector('[data-key-alias]').value.trim(), value: row.querySelector('[data-key-secret]').value.trim() })) };
      if (id) value.id = id;
      save(entry, '/providers', current.revision, value);
    };
  }

  function deactivate() {
    active = false;
    sequence++;
    opening = null;
    reads.forEach(abort => abort.abort());
    [...dialogs].forEach(entry => entry.close());
  }

  return { load, openPurpose, reconcile, deactivate, dispose() {
    deactivate();
    disposed = true;
    data = null;
  } };
}
