// pages/digest.js - observable memory extraction operations
import { gw, esc } from '../api.js?v=20260729-digest1';
import { loading, empty, badge, toast, modal, confirm, delegate } from '../ui.js?v=20260729-digest1';

function fmt(value) {
  if (!value) return '-';
  try { return new Date(value).toLocaleString(); } catch { return String(value); }
}

function statusBadge(status) {
  const kind = status === 'succeeded' ? 'accent' : status === 'failed' ? 'danger' : status === 'running' ? 'warn' : 'muted';
  return badge(status || 'unknown', kind);
}

function memoryCards(memories) {
  if (!memories?.length) return '<p class="muted">No durable memories extracted from this batch.</p>';
  return memories.map(memory => `
    <div class="item">
      <div class="item-title">${esc(memory.title || '(untitled)')}</div>
      <div class="text-sm muted mt8">${esc(memory.content || '')}</div>
      <div class="btn-row mt8">
        ${badge('imp:' + (memory.importance ?? '-'), 'purple')}
        ${badge('confidence:' + Number(memory.confidence ?? 0).toFixed(2), 'info')}
        ${(memory.tags || []).map(tag => badge(esc(tag), 'muted')).join('')}
      </div>
    </div>
  `).join('');
}

export default {
  busy: false,

  async mount(root) {
    this.root = root;
    this.renderShell();
    delegate(root, {
      refresh: () => this.load(),
      preview: () => this.run('preview'),
      execute: () => this.run('execute'),
      detail: el => this.openRun(el.dataset.id),
    });
    await this.load();
  },

  renderShell() {
    this.root.innerHTML = `
      <div class="banner">
        <span>ℹ️</span>
        <div><strong>chat_messages is read-only.</strong> Preview calls the extraction model but does not write memories or advance the cursor. Execute writes pending memories and advances the cursor only after an atomic successful commit.</div>
      </div>
      <div class="toolbar">
        <div style="width:180px">
          <label for="digest-limit">Batch messages (1-100)</label>
          <input id="digest-limit" type="number" min="1" max="100" value="60">
        </div>
        <span style="flex:1"></span>
        <button class="btn btn-secondary" data-act="refresh">Refresh</button>
        <button class="btn btn-soft" data-act="preview">Dry-run Preview</button>
        <button class="btn btn-primary" data-act="execute">Execute & Save Pending</button>
      </div>
      <div id="digest-summary">${loading()}</div>
      <div class="card mt16">
        <div class="card-head"><div class="card-title">Run History</div></div>
        <div id="digest-runs">${loading()}</div>
      </div>
    `;
  },

  setBusy(value) {
    this.busy = value;
    this.root.querySelectorAll('[data-act="preview"],[data-act="execute"]').forEach(button => {
      button.disabled = value;
    });
  },

  async load() {
    const summary = this.root.querySelector('#digest-summary');
    const runs = this.root.querySelector('#digest-runs');
    summary.innerHTML = loading();
    runs.innerHTML = loading();
    try {
      const data = await gw('/admin/api/memory-digest/status');
      this.data = data;
      const cursor = data.cursor || {};
      summary.innerHTML = `
        <div class="grid grid-4">
          <div class="stat"><div class="label">BACKLOG</div><div class="value ${data.backlog_count ? 'warn' : 'accent'}">${data.backlog_count ?? 0}</div></div>
          <div class="stat"><div class="label">CURSOR</div><div class="value">${cursor.last_processed_message_id ?? 0}</div></div>
          <div class="stat"><div class="label">LATEST MESSAGE</div><div class="value">${data.latest_message_id ?? 0}</div></div>
          <div class="stat"><div class="label">MODEL READY</div><div class="value ${data.analysis_configured ? 'accent' : 'warn'}">${data.analysis_configured ? 'YES' : 'NO'}</div></div>
        </div>
        <div class="card mt16">
          <div class="kv"><span class="k">Assistant ID</span><span class="v mono text-sm">${esc(data.assistant_id || '-')}</span></div>
          <div class="kv"><span class="k">Analysis model</span><span class="v mono text-sm">${esc(data.analysis_model || '-')}</span></div>
          <div class="kv"><span class="k">Latest source message</span><span class="v">${fmt(data.latest_message_at)}</span></div>
          <div class="kv"><span class="k">Last successful commit</span><span class="v">${fmt(cursor.last_success_at)}</span></div>
        </div>
      `;
      this.renderRuns(data.recent_runs || []);
    } catch (error) {
      summary.innerHTML = `<div class="banner banner-danger">${esc(error.message)}</div>`;
      runs.innerHTML = empty('Unable to read digest history');
    }
  },

  renderRuns(items) {
    const runs = this.root.querySelector('#digest-runs');
    if (!items.length) {
      runs.innerHTML = empty('No digest runs yet');
      return;
    }
    runs.innerHTML = items.map(run => `
      <div class="item">
        <div class="item-row">
          <div style="flex:1;min-width:0">
            <div class="item-title">#${run.id} · ${esc(run.trigger)} · ${esc(run.mode)} ${statusBadge(run.status)}</div>
            <div class="text-sm muted mt8">
              Source ${run.source_first_message_id ?? '-'} → ${run.source_last_message_id ?? '-'} ·
              ${run.message_count ?? 0} messages · ${run.extracted_count ?? 0} extracted · ${run.inserted_count ?? 0} inserted
            </div>
            ${run.error_code ? `<div class="text-sm mt8" style="color:var(--danger)">${esc(run.error_code)}: ${esc(run.error_message || '')}</div>` : ''}
            <div class="text-sm muted mt8">${fmt(run.started_at)}</div>
          </div>
          <button class="btn btn-xs btn-secondary" data-act="detail" data-id="${run.id}">Details</button>
        </div>
      </div>
    `).join('');
  },

  async run(mode) {
    if (this.busy) return;
    const limit = Math.max(1, Math.min(100, Number(this.root.querySelector('#digest-limit').value) || 60));
    if (mode === 'execute') {
      const ok = await confirm(`Execute memory extraction for up to ${limit} unprocessed source messages? Extracted memories will be stored as pending.`);
      if (!ok) return;
    }

    this.setBusy(true);
    toast(mode === 'preview' ? 'Running dry-run preview…' : 'Executing memory digest…');
    try {
      const result = await gw(`/admin/api/memory-digest/${mode}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ max_messages: limit }),
      });
      this.showResult(result);
      await this.load();
      if (result.status === 'failed') toast(`Failed: ${result.error_code}`, 'err');
      else if (result.status === 'skipped') toast('No unprocessed messages');
      else toast(mode === 'preview' ? `Preview extracted ${result.extracted_count || 0}` : `Saved ${result.inserted_count || 0} pending memories`);
    } catch (error) {
      toast('Digest request failed: ' + error.message, 'err');
    } finally {
      this.setBusy(false);
    }
  },

  showResult(run) {
    const { root, close } = modal({
      title: `Digest Run #${run.id || '-'} · ${run.status || 'unknown'}`,
      body: `
        <div class="kv"><span class="k">Mode</span><span class="v">${esc(run.mode || '-')}</span></div>
        <div class="kv"><span class="k">Source range</span><span class="v">${run.source_first_message_id ?? '-'} → ${run.source_last_message_id ?? '-'}</span></div>
        <div class="kv"><span class="k">Cursor</span><span class="v">${run.cursor_before ?? '-'} → ${run.cursor_after ?? '-'}</span></div>
        <div class="kv"><span class="k">Counts</span><span class="v">${run.message_count || 0} messages / ${run.extracted_count || 0} extracted / ${run.inserted_count || 0} inserted</span></div>
        ${run.error_code ? `<div class="banner banner-danger mt16">${esc(run.error_code)}: ${esc(run.error_message || '')}</div>` : ''}
        <div class="mt16">${memoryCards(run.preview_memories || [])}</div>
      `,
      footer: '<button class="btn btn-secondary" data-close>Close</button>',
    });
    root.querySelector('[data-close]').onclick = close;
  },

  openRun(id) {
    const run = (this.data?.recent_runs || []).find(item => String(item.id) === String(id));
    if (run) this.showResult(run);
  },
};
