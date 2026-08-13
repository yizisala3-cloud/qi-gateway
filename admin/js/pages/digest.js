// pages/digest.js - observable memory extraction operations
import { gw, esc } from '../api.js?v=20260729-digest2';
import { loading, empty, badge, toast, modal, confirm, delegate } from '../ui.js?v=20260729-digest2';

function fmt(value) {
  if (!value) return '-';
  try { return new Date(value).toLocaleString(); } catch { return String(value); }
}

function statusBadge(status) {
  const kind = status === 'succeeded' ? 'accent' : status === 'failed' ? 'danger' : status === 'running' ? 'warn' : 'muted';
  return badge(status || 'unknown', kind);
}

const MEMORY_TYPE_LABELS = {
  profile: '用户资料',
  preference: '偏好与边界',
  relationship: '人物关系与约定',
  habit: '长期习惯',
  event: '重要经历',
  goal: '长期目标与项目',
  other: '其他长期事实',
};

function memoryTypeLabel(value) {
  return MEMORY_TYPE_LABELS[value] || MEMORY_TYPE_LABELS.other;
}

const TIME_PRECISION_LABELS = {
  minute: '精确到分钟',
  day: '精确到日期',
  approximate: '大概时间',
  unknown: '时间未知',
};

const COMMIT_STATUS_LABELS = {
  inserted_pending: '已进入记忆申请',
  skipped_existing_request: '已由记忆工具处理，已跳过',
  skipped_existing_todo: '已由待办工具处理，已跳过',
  skipped_active_memory: '正式记忆已存在，已跳过',
  skipped_active_content: '相同申请已存在，已跳过',
};

function commitBadge(memory) {
  if (!memory.commit_status) return '';
  const skipped = memory.commit_status.startsWith('skipped_');
  return badge(COMMIT_STATUS_LABELS[memory.commit_status] || memory.commit_status, skipped ? 'muted' : 'accent');
}

function memoryCards(memories) {
  if (!memories?.length) return '<p class="muted">No durable memories extracted from this batch.</p>';
  return memories.map(memory => `
    <div class="item">
      <div class="item-title">${esc(memory.title || '(untitled)')}</div>
      <div class="text-sm muted mt8">${esc(memory.content || '')}</div>
      <div class="text-sm muted mt8">
        原文证据：${(memory.evidence_message_ids || []).map(id => `#${esc(id)}`).join('、') || '-'}
        · 证据时间：${esc(memory.source_time || '-')}
        · 记忆时间：${esc(memory.memory_time || '-')}（${TIME_PRECISION_LABELS[memory.time_precision] || TIME_PRECISION_LABELS.unknown}）
      </div>
      <div class="btn-row mt8">
        ${commitBadge(memory)}
        ${memory.dedupe_state === 'possible_duplicate' ? badge('疑似重复，保留审核', 'warn') : ''}
        ${badge(memoryTypeLabel(memory.memory_type), 'accent')}
        ${memory.update_mode === 'replace' ? badge('替换当前状态', 'purple') : badge('新增长期记忆', 'muted')}
        ${memory.memory_key ? badge('主题键: ' + esc(memory.memory_key), 'info') : ''}
        ${badge('imp:' + (memory.importance ?? '-'), 'purple')}
        ${badge('confidence:' + Number(memory.confidence ?? 0).toFixed(2), 'info')}
        ${(memory.tags || []).map(tag => badge(esc(tag), 'muted')).join('')}
      </div>
    </div>
  `).join('');
}

function shadowCandidateCards(candidates) {
  if (!candidates?.length) return '<p class="muted">本批聊天没有提取到连续感候选。</p>';
  return candidates.map(candidate => {
    const evidence = (candidate.evidence_message_ids || []).map(id => `#${esc(id)}`).join('、') || '-';
    const participants = (candidate.participants || []).map(item => esc(item)).join('、') || '-';
    return `
      <div class="item">
        <div class="item-title">${esc(candidate.title || '(无标题)')}</div>
        <div class="text-sm muted mt8">${esc(candidate.content || '')}</div>
        <div class="kv mt16"><span class="k">连续感类型</span><span class="v">${esc(candidate.continuity_type || '-')}</span></div>
        <div class="kv"><span class="k">主体</span><span class="v">${esc(candidate.subject || '-')}</span></div>
        <div class="kv"><span class="k">来源类型</span><span class="v">${esc(candidate.source_type || '-')}</span></div>
        ${candidate.thread_state ? `<div class="kv"><span class="k">Thread 状态</span><span class="v">${esc(candidate.thread_state)}</span></div>` : ''}
        <div class="kv"><span class="k">重要性</span><span class="v">${esc(candidate.importance ?? '-')}</span></div>
        <div class="kv"><span class="k">承接价值</span><span class="v">${esc(candidate.continuity_value ?? '-')}</span></div>
        <div class="kv"><span class="k">置信度</span><span class="v">${esc(candidate.confidence ?? '-')}</span></div>
        <div class="kv"><span class="k">证据消息</span><span class="v">${evidence}</span></div>
        <div class="kv"><span class="k">来源时间</span><span class="v">${esc(candidate.source_time || '-')}</span></div>
        <div class="kv"><span class="k">记忆时间</span><span class="v">${esc(candidate.memory_time || '-')}</span></div>
        <div class="kv"><span class="k">参与者</span><span class="v">${participants}</span></div>
        <div class="kv"><span class="k">保留级别</span><span class="v">${esc(candidate.retention_class || '-')}</span></div>
        <div class="kv"><span class="k">提取理由</span><span class="v">${esc(candidate.reason || '-')}</span></div>
      </div>
    `;
  }).join('');
}

export default {
  busy: false,
  modelReady: false,

  async mount(root) {
    this.root = root;
    this.renderShell();
    delegate(root, {
      refresh: () => this.load(),
      preview: () => this.run('preview'),
      execute: () => this.run('execute'),
      continuity: () => this.runContinuityPreview(),
      detail: el => this.openRun(el.dataset.id),
    });
    await this.load();
  },

  renderShell() {
    this.root.innerHTML = `
      <div class="banner">
        <span>ℹ️</span>
        <div>
          <strong>chat_messages is read-only.</strong> Preview calls the extraction model without writing or advancing the cursor. Execute creates pending memory applications and advances the cursor only after an atomic successful commit.
          <div class="mt8">“连续感预览”独立读取最近聊天，只用于观察新的连续感提取效果；不会写入记忆申请、不会推进正式总结游标。</div>
        </div>
      </div>
      <div id="digest-config-warning"></div>
      <div class="toolbar">
        <div style="width:180px">
          <label for="digest-limit">Batch messages (1-100)</label>
          <input id="digest-limit" type="number" min="1" max="100" value="60">
        </div>
        <span style="flex:1"></span>
        <button class="btn btn-secondary" data-act="refresh">Refresh</button>
        <button class="btn btn-soft" data-act="preview" disabled>Dry-run Preview</button>
        <button class="btn btn-soft" data-act="continuity" disabled>连续感预览</button>
        <button class="btn btn-primary" data-act="execute" disabled>Execute & Save Pending</button>
      </div>
      <div id="digest-summary">${loading()}</div>
      <div class="card mt16">
        <div class="card-head"><div class="card-title">Run History</div></div>
        <div id="digest-runs">${loading()}</div>
      </div>
    `;
  },

  syncControls() {
    this.root.querySelectorAll('[data-act="preview"],[data-act="continuity"],[data-act="execute"]').forEach(button => {
      button.disabled = this.busy || !this.modelReady;
    });
  },

  setBusy(value) {
    this.busy = value;
    this.syncControls();
  },

  async load() {
    const summary = this.root.querySelector('#digest-summary');
    const runs = this.root.querySelector('#digest-runs');
    const warning = this.root.querySelector('#digest-config-warning');
    summary.innerHTML = loading();
    runs.innerHTML = loading();
    try {
      const data = await gw('/admin/api/memory-digest/status');
      this.data = data;
      this.modelReady = Boolean(data.analysis_configured);
      this.syncControls();
      warning.innerHTML = this.modelReady ? '' : `
        <div class="banner banner-danger">
          <span>⚠️</span>
          <div><strong>Extraction model is not configured.</strong> Add ANALYSIS_API_KEY to the qi-gateway service environment and redeploy. Preview and Execute stay disabled until the server confirms readiness.</div>
        </div>`;
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
      this.modelReady = false;
      this.syncControls();
      warning.innerHTML = '';
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
    if (!this.modelReady) {
      toast('ANALYSIS_API_KEY is not configured on qi-gateway', 'err');
      return;
    }
    const limit = Math.max(1, Math.min(100, Number(this.root.querySelector('#digest-limit').value) || 60));
    if (mode === 'execute') {
      const ok = await confirm(`Execute memory extraction for up to ${limit} unprocessed source messages? Candidates will enter the memory application review queue.`);
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
      else toast(mode === 'preview' ? `Preview extracted ${result.extracted_count || 0}` : `Created ${result.inserted_count || 0} pending applications`);
    } catch (error) {
      toast('Digest request failed: ' + error.message, 'err');
    } finally {
      this.setBusy(false);
    }
  },

  async runContinuityPreview() {
    if (this.busy) return;
    if (!this.modelReady) {
      toast('ANALYSIS_API_KEY is not configured on qi-gateway', 'err');
      return;
    }
    const limit = Math.max(1, Math.min(100, Number(this.root.querySelector('#digest-limit').value) || 60));

    this.setBusy(true);
    toast('正在提取连续感候选……');
    try {
      const result = await gw('/admin/api/memory-continuity/shadow-preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          max_messages: limit,
          max_chars: 16000,
        }),
      });
      this.showContinuityResult(result);
    } catch (error) {
      toast('连续感预览失败：' + error.message, 'err');
    } finally {
      this.setBusy(false);
    }
  },

  showContinuityResult(result) {
    const candidates = Array.isArray(result.candidates) ? result.candidates : [];
    const warnings = Array.isArray(result.warnings) ? result.warnings : [];
    const warningBlock = warnings.length ? `
      <div class="banner mt16">
        <span>ℹ️</span>
        <div>${warnings.map(item => esc(item)).join('<br>')}</div>
      </div>` : '';
    const { root, close } = modal({
      title: '连续感预览',
      body: `
        <div class="kv"><span class="k">Persistence</span><span class="v">${esc(result.persistence || '-')}</span></div>
        <div class="kv"><span class="k">Source range</span><span class="v">${esc(result.source_first_message_id ?? '-')} → ${esc(result.source_last_message_id ?? '-')}</span></div>
        <div class="kv"><span class="k">Message count</span><span class="v">${esc(result.message_count ?? 0)}</span></div>
        <div class="kv"><span class="k">Candidates</span><span class="v">${esc(candidates.length)}</span></div>
        ${warningBlock}
        <div class="mt16">${shadowCandidateCards(candidates)}</div>
      `,
      footer: '<button class="btn btn-secondary" data-close>Close</button>',
    });
    root.querySelector('[data-close]').onclick = close;
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
