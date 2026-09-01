// pages/digest.js - 记忆总结：仅连续感总结
import { gw, esc } from '../api.js?v=20260902-adminmem1';
import { loading, empty, errorBlock, tag, toast, modal, confirm, delegate, icon, fmtDate } from '../ui.js?v=20260902-adminmem1';

const TIME_PRECISION_LABELS = {
  minute: '精确到分钟', day: '精确到日期', approximate: '大概时间', unknown: '时间未知',
};
const COMMIT_STATUS_LABELS = {
  inserted_pending: '已进入记忆申请',
  skipped_existing_request: '已由记忆工具处理，已跳过',
  skipped_existing_todo: '已由待办工具处理，已跳过',
  skipped_active_memory: '正式记忆已存在，已跳过',
  skipped_active_content: '相同申请已存在，已跳过',
};

function fmtMinute(value) {
  if (!value) return '-';
  try {
    return new Intl.DateTimeFormat('zh-CN', {
      year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(new Date(value));
  } catch { return String(value); }
}

function fmtTimeRange(start, end) {
  if (!start && !end) return '-';
  const startText = fmtMinute(start || end);
  const endText = fmtMinute(end || start);
  return startText === endText ? startText : `${startText} ～ ${endText}`;
}

function statusTag(status) {
  const tone = status === 'succeeded' || status === 'ready' ? 'green'
    : status === 'failed' ? 'red'
      : status === 'running' || status === 'paused_empty' ? 'amber' : 'muted';
  return tag(esc(status || 'unknown'), tone);
}

function cooldownStatus(value) {
  if (!value) return '可用';
  const until = new Date(value);
  if (Number.isNaN(until.getTime()) || until <= new Date()) return '可用';
  return `冷却至 ${fmtMinute(value)}`;
}

function commitTag(memory) {
  if (!memory.commit_status) return '';
  const skipped = memory.commit_status.startsWith('skipped_');
  return tag(COMMIT_STATUS_LABELS[memory.commit_status] || memory.commit_status, skipped ? 'muted' : 'green');
}

function candidateCards(candidates) {
  if (!candidates?.length) return '<p class="muted">本批聊天没有提取到连续感候选。</p>';
  return candidates.map((candidate) => {
    const evidence = (candidate.evidence_message_ids || []).map((id) => `#${esc(id)}`).join('、') || '-';
    return `
      <div class="mem-card" style="cursor:default">
        <div class="mem-title">${esc(candidate.title || '(无标题)')}</div>
        <div class="mem-snippet">${esc(candidate.content || '')}</div>
        <div class="mt8">
          <div class="kv"><span class="k">连续感类型</span><span class="v">${esc(candidate.continuity_type || '-')}</span></div>
          <div class="kv"><span class="k">来源类型</span><span class="v">${esc(candidate.source_type || '-')}</span></div>
          ${candidate.thread_state ? `<div class="kv"><span class="k">Thread 状态</span><span class="v">${esc(candidate.thread_state)}</span></div>` : ''}
          <div class="kv"><span class="k">重要性</span><span class="v">${esc(candidate.importance ?? '-')}</span></div>
          <div class="kv"><span class="k">置信度</span><span class="v">${esc(candidate.confidence ?? '-')}</span></div>
          <div class="kv"><span class="k">证据消息</span><span class="v">${evidence}</span></div>
          <div class="kv"><span class="k">对话时间</span><span class="v">${esc(fmtTimeRange(candidate.evidence_start_time, candidate.evidence_end_time))}</span></div>
          <div class="kv"><span class="k">记忆时间</span><span class="v">${esc(fmtMinute(candidate.memory_time))}</span></div>
          <div class="kv"><span class="k">提取理由</span><span class="v">${esc(candidate.reason || '-')}</span></div>
        </div>
        <div class="tag-row mt8">
          ${commitTag(candidate)}
          ${candidate.dedupe_state === 'possible_duplicate' ? tag('疑似重复，保留审核', 'amber') : ''}
          ${tag(esc(candidate.continuity_type || '-'), 'gold')}
          ${candidate.update_mode === 'replace' ? tag('替换当前状态', 'plum') : tag('新增长期记忆', 'muted')}
          ${candidate.memory_key ? tag(`<span class="mono">${esc(candidate.memory_key)}</span>`, 'slate') : ''}
          ${tag(`imp ${candidate.importance ?? '-'}`, 'plum')}
          ${tag(`confidence ${Number(candidate.confidence ?? 0).toFixed(2)}`, 'slate')}
          ${(candidate.tags || []).map((t) => tag(esc(t), 'muted')).join('')}
        </div>
      </div>`;
  }).join('');
}

export default {
  busy: false,
  data: null,

  async mount(root) {
    this.root = root;
    root.innerHTML = `
      <div class="page-with-detail">
        <div class="page-main">
          <div id="digest-warning"></div>
          <div class="toolbar">
            <label class="inline">批次消息数（1-100）<input id="digest-limit" type="number" min="1" max="100" value="60" style="width:90px"></label>
            <span class="grow"></span>
            <button class="btn btn-secondary" data-act="refresh">${icon('refresh')}刷新</button>
            <button class="btn btn-secondary" data-act="skip" disabled style="display:none">${icon('x')}跳过暂停批次</button>
            <button class="btn btn-secondary" data-act="preview" disabled>${icon('search')}连续感预览</button>
            <button class="btn btn-primary" data-act="execute" disabled>${icon('check')}执行连续感总结</button>
          </div>
          <div id="continuity-status">${loading()}</div>
          <div class="section-title">最近连续感运行</div>
          <div id="continuity-runs">${loading()}</div>
        </div>
        <aside class="side-panel" aria-label="状态概览">
          <div class="panel-title">${icon('scroll')}连续感概览</div>
          <div id="continuity-overview">${loading()}</div>
        </aside>
      </div>`;
    delegate(root, {
      refresh: () => this.load(),
      preview: () => this.runPreview(),
      execute: () => this.runExecute(),
      skip: () => this.skipBatch(),
      detail: (el) => this.openRun(el.dataset.id),
    });
    await this.load();
  },

  syncControls() {
    const paused = this.data?.status === 'paused_empty';
    const ready = Boolean(this.data?.analysis_configured);
    this.root.querySelectorAll('[data-act="preview"],[data-act="execute"]').forEach((button) => {
      button.disabled = this.busy || !ready;
    });
    const execute = this.root.querySelector('[data-act="execute"]');
    if (execute) execute.innerHTML = `${icon('check')}${paused ? '重试暂停批次' : '执行连续感总结'}`;
    const skip = this.root.querySelector('[data-act="skip"]');
    if (skip) {
      skip.disabled = this.busy || !paused;
      skip.style.display = paused ? '' : 'none';
    }
  },

  async load() {
    const status = this.root.querySelector('#continuity-status');
    const runs = this.root.querySelector('#continuity-runs');
    const overview = this.root.querySelector('#continuity-overview');
    const warning = this.root.querySelector('#digest-warning');
    status.innerHTML = loading();
    runs.innerHTML = loading();
    overview.innerHTML = loading();
    try {
      const data = await gw('/admin/api/memory-continuity/status');
      this.data = data;
      this.syncControls();
      warning.innerHTML = data.analysis_configured ? '' : `
        <div class="banner banner-danger">
          <span class="banner-ico">${icon('alert')}</span>
          <div><strong>连续感提取模型未配置。</strong>请在 qi-gateway 服务环境配置相应密钥后重新部署；配置完成前预览与执行按钮保持禁用。</div>
        </div>`;
      this.renderStatusCard(data);
      this.renderRuns(data.recent_runs || []);
      this.renderOverview(data);
    } catch (error) {
      this.data = null;
      this.syncControls();
      warning.innerHTML = '';
      status.innerHTML = errorBlock(`连续感状态读取失败：${esc(error.message)}`);
      runs.innerHTML = empty('无法读取运行记录');
      overview.innerHTML = empty('无法读取状态概览');
    }
  },

  renderStatusCard(data) {
    const paused = data.status === 'paused_empty';
    const blockedRange = data.blocked_first_message_id == null
      ? '-'
      : `${esc(data.blocked_first_message_id)} → ${esc(data.blocked_last_message_id)}`;
    this.root.querySelector('#continuity-status').innerHTML = `
      <div class="card">
        <div class="card-head">
          <div>
            <div class="card-title">${icon('scroll')}连续感总结 ${statusTag(data.status)}</div>
            <div class="card-sub">候选只写入 pending 审核队列，审核通过后才会成为正式记忆；自动阈值 ${esc(data.auto_threshold ?? 80)} 条，手动与自动冷却彼此独立。</div>
          </div>
        </div>
        ${paused ? `<div class="banner banner-danger">本批未生成候选，连续感自动总结已暂停。请重试或确认跳过本批。</div>` : ''}
        <div class="kv"><span class="k">暂停原因</span><span class="v">${esc(data.pause_reason || '-')}</span></div>
        <div class="kv"><span class="k">阻塞批次</span><span class="v">${blockedRange} · ${esc(data.blocked_message_count ?? 0)} 条</span></div>
        <div class="kv"><span class="k">手动执行</span><span class="v">${esc(cooldownStatus(data.manual_cooldown_until))}</span></div>
        <div class="kv"><span class="k">自动执行</span><span class="v">${esc(cooldownStatus(data.auto_cooldown_until))}</span></div>
        <div class="kv"><span class="k">提取模型</span><span class="v mono text-sm">${esc(data.analysis_model || data.continuity_model || '-')}</span></div>
      </div>`;
  },

  renderRuns(runs) {
    const box = this.root.querySelector('#continuity-runs');
    if (!runs.length) {
      box.innerHTML = empty('暂无连续感运行记录', '执行一次连续感总结后会在这里显示');
      return;
    }
    box.innerHTML = runs.map((run) => `
      <div class="mem-card" data-act="detail" data-id="${run.id}">
        <div class="card-top">
          <div class="card-main">
            <div class="mem-title">#${esc(run.id)} · ${esc(run.trigger)} ${statusTag(run.status)}</div>
            <div class="card-meta">
              来源 ${esc(run.source_first_message_id ?? '-')} → ${esc(run.source_last_message_id ?? '-')}
              · ${esc(run.message_count ?? 0)} 条消息 · 写入 ${esc(run.inserted_count ?? 0)} 条 pending
              · ${esc(fmtDate(run.started_at))}
            </div>
            ${run.error_code ? `<div class="card-meta" style="color:var(--red)">${esc(run.error_code)}: ${esc(run.error_message || '')}</div>` : ''}
          </div>
          <div class="card-side"><button class="btn btn-quiet btn-sm">${icon('info')}运行详情</button></div>
        </div>
      </div>`).join('');
  },

  renderOverview(data) {
    this.root.querySelector('#continuity-overview').innerHTML = `
      <div class="kv"><span class="k">待处理消息</span><span class="v overview-num ${data.backlog_count ? '' : ''}" style="color:${data.backlog_count ? 'var(--amber)' : 'var(--green-ink)'}">${esc(data.backlog_count ?? 0)}</span></div>
      <div class="kv"><span class="k">总结 cursor</span><span class="v overview-num">${esc(data.cursor ?? '-')}</span></div>
      <div class="kv"><span class="k">最新消息</span><span class="v overview-num">${esc(data.latest_message_id ?? '-')}</span></div>
      <div class="kv"><span class="k">自动阈值</span><span class="v overview-num">${esc(data.auto_threshold ?? 80)}</span></div>
      <div class="kv"><span class="k">运行状态</span><span class="v">${statusTag(data.status)}</span></div>
      <div class="kv"><span class="k">手动冷却</span><span class="v">${esc(cooldownStatus(data.manual_cooldown_until))}</span></div>
      <div class="kv"><span class="k">自动冷却</span><span class="v">${esc(cooldownStatus(data.auto_cooldown_until))}</span></div>
      <div class="kv"><span class="k">模型就绪</span><span class="v">${data.analysis_configured ? tag('已配置', 'green') : tag('未配置', 'red')}</span></div>`;
  },

  async runPreview() {
    if (this.busy) return;
    if (!this.data?.analysis_configured) {
      toast('连续感提取模型未配置，无法预览', 'err');
      return;
    }
    const limit = this.readLimit();
    this.setBusy(true);
    toast('正在提取连续感候选……');
    try {
      const result = await gw('/admin/api/memory-continuity/shadow-preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ max_messages: limit, max_chars: 16000 }),
      });
      this.showPreviewResult(result);
    } catch (error) {
      toast(`连续感预览失败：${error.message}`, 'err');
    } finally {
      this.setBusy(false);
    }
  },

  async runExecute() {
    if (this.busy) return;
    if (!this.data?.analysis_configured) {
      toast('连续感提取模型未配置，无法执行', 'err');
      return;
    }
    const paused = this.data?.status === 'paused_empty';
    const backlog = Number(this.data?.backlog_count || 0);
    if (!paused && backlog < 10) {
      const ok = await confirm(`当前只有 ${backlog} 条新消息，仍要执行连续感总结吗？`, { okText: '仍然执行', danger: false });
      if (!ok) return;
    }
    this.setBusy(true);
    toast(paused ? '正在重试固定批次……' : '正在执行连续感总结……');
    try {
      const result = await gw('/admin/api/memory-continuity/execute', { method: 'POST' });
      this.showExecutionResult(result);
      await this.load();
      if (result.paused_empty) {
        toast('本批未生成候选，连续感自动总结已暂停。请重试或确认跳过本批。', 'err');
      } else {
        toast(`已写入 ${result.inserted_count || 0} 条 pending 记忆申请`);
      }
    } catch (error) {
      toast(`连续感总结失败：${error.message}`, 'err');
    } finally {
      this.setBusy(false);
    }
  },

  async skipBatch() {
    if (this.busy || this.data?.status !== 'paused_empty') return;
    const first = this.data.blocked_first_message_id ?? '-';
    const last = this.data.blocked_last_message_id ?? '-';
    const count = this.data.blocked_message_count ?? 0;
    const ok = await confirm(`确定跳过固定批次 ${esc(first)} → ${esc(last)}（${count} 条消息）并恢复自动总结吗？此操作不会调用模型，也不会写入记忆申请。`, { okText: '跳过本批' });
    if (!ok) return;
    this.setBusy(true);
    try {
      await gw('/admin/api/memory-continuity/skip-blocked', { method: 'POST' });
      toast('已跳过固定批次并恢复连续感自动总结');
      await this.load();
    } catch (error) {
      toast(`跳过批次失败：${error.message}`, 'err');
    } finally {
      this.setBusy(false);
    }
  },

  readLimit() {
    return Math.max(1, Math.min(100, Number(this.root.querySelector('#digest-limit')?.value) || 60));
  },

  setBusy(value) {
    this.busy = value;
    this.syncControls();
  },

  showExecutionResult(result) {
    const candidates = Array.isArray(result.preview_memories) ? result.preview_memories : [];
    const { root, close } = modal({
      title: result.paused_empty ? '连续感总结已暂停' : `连续感总结 #${esc(result.id || '-')}`,
      body: `
        <div class="kv"><span class="k">状态</span><span class="v">${esc(result.status || '-')}</span></div>
        <div class="kv"><span class="k">来源范围</span><span class="v">${esc(result.source_first_message_id ?? '-')} → ${esc(result.source_last_message_id ?? '-')}</span></div>
        <div class="kv"><span class="k">Cursor</span><span class="v">${esc(result.cursor_before ?? '-')} → ${esc(result.cursor_after ?? '-')}</span></div>
        <div class="kv"><span class="k">写入 pending</span><span class="v">${esc(result.inserted_count ?? 0)}</span></div>
        ${result.paused_empty ? '<div class="banner banner-danger mt16">本批没有生成候选，cursor 未推进。</div>' : ''}
        <div class="mt16">${candidateCards(candidates)}</div>`,
      footer: '<button class="btn btn-secondary" data-close>关闭</button>',
      wide: true,
      draggable: true,
    });
    root.querySelector('[data-close]').onclick = close;
  },

  showPreviewResult(result) {
    const candidates = Array.isArray(result.candidates) ? result.candidates : [];
    const warnings = Array.isArray(result.warnings) ? result.warnings : [];
    const { root, close } = modal({
      title: '连续感预览',
      body: `
        <div class="kv"><span class="k">持久化</span><span class="v">${esc(result.persistence || '-')}</span></div>
        <div class="kv"><span class="k">来源范围</span><span class="v">${esc(result.source_first_message_id ?? '-')} → ${esc(result.source_last_message_id ?? '-')}</span></div>
        <div class="kv"><span class="k">消息数量</span><span class="v">${esc(result.message_count ?? 0)}</span></div>
        <div class="kv"><span class="k">候选数量</span><span class="v">${esc(candidates.length)}</span></div>
        ${warnings.length ? `<div class="banner mt16"><span class="banner-ico">${icon('info')}</span><div>${warnings.map((item) => esc(item)).join('<br>')}</div></div>` : ''}
        <div class="mt16">${candidateCards(candidates)}</div>`,
      footer: '<button class="btn btn-secondary" data-close>关闭</button>',
      wide: true,
      draggable: true,
    });
    root.querySelector('[data-close]').onclick = close;
  },

  openRun(id) {
    const run = (this.data?.recent_runs || []).find((item) => String(item.id) === String(id));
    if (!run) { toast('运行记录不在当前列表中', 'err'); return; }
    const { root, close } = modal({
      title: `连续感运行 #${esc(run.id)} · ${esc(run.trigger)}`,
      body: `
        <div class="kv"><span class="k">状态</span><span class="v">${statusTag(run.status)}</span></div>
        <div class="kv"><span class="k">来源范围</span><span class="v">${esc(run.source_first_message_id ?? '-')} → ${esc(run.source_last_message_id ?? '-')}</span></div>
        <div class="kv"><span class="k">消息数量</span><span class="v">${esc(run.message_count ?? 0)}</span></div>
        <div class="kv"><span class="k">写入 pending</span><span class="v">${esc(run.inserted_count ?? 0)}</span></div>
        <div class="kv"><span class="k">开始时间</span><span class="v">${esc(fmtDate(run.started_at))}</span></div>
        ${run.error_code ? `<div class="banner banner-danger mt16">${esc(run.error_code)}: ${esc(run.error_message || '')}</div>` : ''}
        ${run.preview_memories?.length ? `<div class="section-title">候选记忆详情</div>${candidateCards(run.preview_memories)}` : ''}`,
      footer: '<button class="btn btn-secondary" data-close>关闭</button>',
      wide: true,
      draggable: true,
    });
    root.querySelector('[data-close]').onclick = close;
  },
};
