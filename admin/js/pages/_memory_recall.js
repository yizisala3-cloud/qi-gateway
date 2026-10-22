// Read-only snapshots of actual memory injections; no polling or background reads.
import { query, esc } from '../api.js?v=20261021-qqbot-mount-r10';
import { loading, empty, errorBlock, tag, fmtDate, pagerHtml, icon } from '../ui.js?v=20261021-qqbot-mount-r10';

export function createRecallHistoryView({ body, panel, onOpenMemory, onSelectSnapshot, typeLabel }) {
  const pageSize = 20;
  let rows = [];
  let page = 0;
  let sequence = 0;
  let disposed = false;
  let selectedId = null;

  function shell() {
    body.innerHTML = `
      <div class="toolbar"><span class="grow muted">最近召回 · 最多保留150条</span>
        <button class="btn btn-secondary" data-act="recall-refresh">${icon('refresh')}刷新</button></div>
      <div id="recall-list">${loading()}</div><div id="recall-pager"></div>`;
  }

  function render() {
    const list = body.querySelector('#recall-list');
    if (!list || disposed) return;
    page = Math.min(page, Math.max(0, Math.ceil(rows.length / pageSize) - 1));
    list.innerHTML = rows.length ? rows.slice(page * pageSize, (page + 1) * pageSize).map(row => `
      <div class="mem-card ${String(row.id) === selectedId ? 'selected' : ''}" data-act="recall-open" data-id="${esc(row.id)}">
        <div class="mem-title">#${esc(row.memory_id)} · <span class="copyable-text">${esc(row.title || '(未命名)')}</span></div>
        <div class="mem-snippet">${esc(row.injection_text.slice(0, 160))}</div>
        <div class="tag-row mt8">${tag(esc(typeLabel(row.continuity_type)), 'slate')}</div>
        <div class="card-meta">召回于 ${esc(fmtDate(row.recalled_at))} · 本轮第${esc(row.position)}条</div>
      </div>`).join('') : empty('暂无召回记录', '后续聊天实际注入的记忆将显示在这里');
    body.querySelector('#recall-pager').innerHTML = pagerHtml(
      page, Math.max(1, Math.ceil(rows.length / pageSize)), rows.length,
    ).replaceAll('data-act="page"', 'data-act="recall-page"');
  }

  async function reload() {
    const request = ++sequence;
    shell();
    try {
      const data = await query('memory_recall_history', {
        select: 'id,batch_id,memory_id,recalled_at,position,title,continuity_type,injection_text',
        order: { col: 'recalled_at', asc: false }, limit: 150,
      });
      if (disposed || request !== sequence) return;
      rows = data;
      render();
    } catch (error) {
      if (disposed || request !== sequence) return;
      body.querySelector('#recall-list').innerHTML = errorBlock(`召回记录读取失败：${esc(error.message)}`);
    }
  }

  function open(id) {
    const row = rows.find(item => String(item.id) === String(id));
    if (!row || disposed) return;
    onSelectSnapshot();
    selectedId = String(row.id);
    render();
    panel.render({
      title: `召回快照 · #${esc(row.memory_id)} · <span class="copyable-text">${esc(row.title || '(未命名)')}</span>`,
      badges: tag(esc(typeLabel(row.continuity_type)), 'slate'),
      html: `<div class="kv"><span class="k">召回时间</span><span class="v">${esc(fmtDate(row.recalled_at))}</span></div>
        <div class="kv"><span class="k">当时注入的内容</span></div>
        <div class="kv-block"><span class="v copyable-text" style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(row.injection_text)}</span></div>`,
      actions: `<button class="btn btn-secondary btn-sm" data-act="recall-current" data-id="${esc(row.memory_id)}">${icon('book')}查看当前记忆</button>`,
    });
  }

  function onClick(event) {
    const el = event.target.closest('[data-act]');
    if (!el || el.disabled || disposed) return;
    if (el.dataset.act === 'recall-refresh') reload();
    else if (el.dataset.act === 'recall-open') open(el.dataset.id);
    else if (el.dataset.act === 'recall-page') { page = Number(el.dataset.p); render(); }
  }
  body.addEventListener('click', onClick);
  // Detail actions live outside body, so parent delegation invokes this method.
  return { mount: reload, reload, open, openCurrent: onOpenMemory,
    dispose() { disposed = true; ++sequence; body.removeEventListener('click', onClick); } };
}
