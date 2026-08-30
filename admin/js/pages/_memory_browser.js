// pages/_memory_browser.js - shared library/requests browser with detail panel
import { gw, query, update, count, esc } from '../api.js?v=20260830-retro1';
import {
  loading, empty, errorBlock, banner, tag, heatTag, impTag, pagerHtml,
  toast, modal, confirm, delegate, icon, fmtDate, createDetailPanel,
} from '../ui.js?v=20260830-retro1';

export const ASSET_VERSION = '20260830-retro1';

const PAGE_SIZE = 20;
const REQ_FETCH_LIMIT = 100;

export const CONTINUITY_TYPE_LABELS = {
  moment: '近期片段', thread: '未完线索', episode: '共同经历', inside_joke: '内部梗',
  profile: '用户资料', interaction_rule: '互动规则',
};
const TIME_PRECISION_LABELS = {
  minute: '精确到分钟', hour: '精确到小时', day: '精确到日期', approximate: '大概时间', unknown: '时间未知',
};
const MEMORY_FIELDS = 'id,title,content,tags,heat,importance,layer,source,verified,is_active,last_recalled_at,recall_count,emotion_weight,created_at,memory_key,supersedes_memory_id,superseded_by_memory_id,superseded_at,continuity_id,continuity_type,continuity_schema_version,continuity_data,subject,source_type,thread_state,continuity_value,retention_class,participants,evidence_start_time,evidence_end_time,evidence_message_ids,source_time,memory_time,time_precision,evidence_time_precision,recall_scene,recall_tags';
const REQUEST_FIELDS = 'id,assistant_id,conversation_id,source_message_id,content,title,tags,importance,reason,status,source,memory_id,memory_key,update_mode,related_memory_id,related_request_id,continuity_id,continuity_type,continuity_schema_version,continuity_data,thread_state,confidence,evidence_message_ids,source_time,memory_time,time_precision,digest_run_id,dedupe_state,dedupe_reason,evidence_time_precision,recall_scene,recall_tags,created_at,reviewed_at,reviewed_by,review_note';

export function continuityTypeLabel(value) {
  return CONTINUITY_TYPE_LABELS[value] || value || '未分类历史数据';
}

export function reqStatusMeta(status) {
  return {
    pending: { label: '待审核', tone: 'amber' },
    approved: { label: '已通过', tone: 'green' },
    merged: { label: '已合并', tone: 'gold' },
    rejected: { label: '已拒绝', tone: 'red' },
    conflict: { label: '冲突待处理', tone: 'red' },
    duplicate: { label: '重复', tone: 'muted' },
  }[status] || { label: status || '待审核', tone: 'amber' };
}

function verifiedMeta(memory) {
  if (!memory.is_active) return { label: '已归档', tone: 'muted' };
  return {
    verified: { label: '已确认', tone: 'green' },
    rejected: { label: '已驳回', tone: 'red' },
    pending: { label: '待确认', tone: 'amber' },
  }[memory.verified] || { label: memory.verified || '未知', tone: 'muted' };
}

function typeTag(value) {
  return tag(esc(continuityTypeLabel(value)), 'gold');
}

function fmtValue(value) {
  if (value === null || value === undefined || value === '') return '-';
  if (Array.isArray(value)) return value.length ? value.map(item => esc(item)).join('、') : '-';
  if (typeof value === 'object') return `<span class="mono text-sm">${esc(JSON.stringify(value, null, 1))}</span>`;
  return esc(value);
}

function evidenceRange(memory) {
  const start = memory.evidence_start_time || memory.source_time;
  const end = memory.evidence_end_time || memory.source_time;
  if (!start && !end) return '-';
  const range = `${esc(fmtDate(start))} ～ ${esc(fmtDate(end || start))}`;
  const label = TIME_PRECISION_LABELS[memory.evidence_time_precision];
  return label ? `${range}（${esc(label)}）` : range;
}

function memoryKvRows(m) {
  return `
    <div class="kv"><span class="k">正文</span></div>
    <div class="kv-block"><span class="v">${esc(m.content || '-')}</span></div>
    <div class="kv"><span class="k">状态</span><span class="v">${esc(verifiedMeta(m).label)}</span></div>
    <div class="kv"><span class="k">连续感类型</span><span class="v">${esc(continuityTypeLabel(m.continuity_type))}</span></div>
    <div class="kv"><span class="k">热度 / 重要性</span><span class="v">${Number(m.heat ?? 0).toFixed(1)} / ${esc(m.importance ?? '-')}</span></div>
    <div class="kv"><span class="k">层级</span><span class="v">${esc(m.layer || '-')}</span></div>
    <div class="kv"><span class="k">情感权重</span><span class="v">${esc(m.emotion_weight ?? '-')}</span></div>
    <div class="kv"><span class="k">标签</span><span class="v">${(m.tags || []).length ? (m.tags || []).map(t => esc(t)).join('、') : '-'}</span></div>
    <div class="kv"><span class="k">来源</span><span class="v">${esc(m.source || '-')}</span></div>
    ${m.memory_key ? `<div class="kv"><span class="k">主题键</span><span class="v mono text-sm">${esc(m.memory_key)}</span></div>` : ''}
    ${m.continuity_id ? `<div class="kv"><span class="k">连续感 ID</span><span class="v mono text-sm">${esc(m.continuity_id)}</span></div>` : ''}
    ${m.thread_state ? `<div class="kv"><span class="k">线索状态</span><span class="v">${esc(m.thread_state)}</span></div>` : ''}
    ${m.subject ? `<div class="kv"><span class="k">主体</span><span class="v">${esc(m.subject)}</span></div>` : ''}
    ${m.participants?.length ? `<div class="kv"><span class="k">参与者</span><span class="v">${m.participants.map(p => esc(p)).join('、')}</span></div>` : ''}
    ${m.retention_class ? `<div class="kv"><span class="k">保留级别</span><span class="v">${esc(m.retention_class)}</span></div>` : ''}
    ${m.continuity_value != null ? `<div class="kv"><span class="k">承接价值</span><span class="v">${esc(m.continuity_value)}</span></div>` : ''}
    <div class="kv"><span class="k">证据时间</span><span class="v">${evidenceRange(m)}</span></div>
    <div class="kv"><span class="k">记忆时间</span><span class="v">${esc(m.memory_time ? fmtDate(m.memory_time) : '-')}（${TIME_PRECISION_LABELS[m.time_precision] || TIME_PRECISION_LABELS.unknown}）</span></div>
    ${m.recall_scene ? `<div class="kv"><span class="k">召回场景</span></div><div class="kv-block"><span class="v">${esc(m.recall_scene)}</span></div>` : ''}
    ${(m.recall_tags || []).length ? `<div class="kv"><span class="k">召回标签</span><span class="v">${m.recall_tags.map(t => esc(t)).join('、')}</span></div>` : ''}
    <div class="kv"><span class="k">创建时间</span><span class="v">${esc(fmtDate(m.created_at))}</span></div>
    <div class="kv"><span class="k">最近召回</span><span class="v">${esc(fmtDate(m.last_recalled_at))} · ${esc(m.recall_count ?? 0)} 次</span></div>
  `;
}

function requestKvRows(r) {
  return `
    <div class="kv"><span class="k">申请内容</span></div>
    <div class="kv-block"><span class="v">${esc(r.content || '-')}</span></div>
    ${r.reason ? `<div class="kv"><span class="k">申请理由</span></div><div class="kv-block"><span class="v">${esc(r.reason)}</span></div>` : ''}
    ${r.review_note ? `<div class="kv"><span class="k">审核备注</span></div><div class="kv-block"><span class="v">${esc(r.review_note)}</span></div>` : ''}
    <div class="kv"><span class="k">连续感类型</span><span class="v">${esc(continuityTypeLabel(r.continuity_type))}</span></div>
    <div class="kv"><span class="k">重要性 / 置信度</span><span class="v">${esc(r.importance ?? '-')} / ${r.confidence != null ? Number(r.confidence).toFixed(2) : '-'}</span></div>
    <div class="kv"><span class="k">写入方式</span><span class="v">${r.update_mode === 'replace' ? '替换同一可变事实' : '新增独立记忆'}</span></div>
    ${r.memory_key ? `<div class="kv"><span class="k">主题键</span><span class="v mono text-sm">${esc(r.memory_key)}</span></div>` : ''}
    ${(r.tags || []).length ? `<div class="kv"><span class="k">标签</span><span class="v">${r.tags.map(t => esc(t)).join('、')}</span></div>` : ''}
    ${r.thread_state ? `<div class="kv"><span class="k">线索状态</span><span class="v">${esc(r.thread_state)}</span></div>` : ''}
    ${r.dedupe_state ? `<div class="kv"><span class="k">查重状态</span><span class="v">${esc(r.dedupe_state)}${r.dedupe_reason ? ` · ${esc(r.dedupe_reason)}` : ''}</span></div>` : ''}
    ${r.related_memory_id ? `<div class="kv"><span class="k">关联记忆</span><span class="v">memory #${esc(r.related_memory_id)}</span></div>` : ''}
    ${r.related_request_id ? `<div class="kv"><span class="k">相似申请</span><span class="v">#${esc(r.related_request_id)}</span></div>` : ''}
    <div class="kv"><span class="k">原文证据</span><span class="v">${(r.evidence_message_ids || []).map(id => `#${esc(id)}`).join('、') || '-'}</span></div>
    <div class="kv"><span class="k">证据时间</span><span class="v">${evidenceRange(r)}</span></div>
    <div class="kv"><span class="k">记忆时间</span><span class="v">${esc(r.memory_time ? fmtDate(r.memory_time) : '-')}（${TIME_PRECISION_LABELS[r.time_precision] || TIME_PRECISION_LABELS.unknown}）</span></div>
    ${r.recall_scene ? `<div class="kv"><span class="k">召回场景</span></div><div class="kv-block"><span class="v">${esc(r.recall_scene)}</span></div>` : ''}
    ${(r.recall_tags || []).length ? `<div class="kv"><span class="k">召回标签</span><span class="v">${r.recall_tags.map(t => esc(t)).join('、')}</span></div>` : ''}
    <div class="kv"><span class="k">来源消息</span><span class="v">#${esc(r.source_message_id ?? '-')} · 会话 ${esc(r.conversation_id || '-')}</span></div>
    ${r.digest_run_id ? `<div class="kv"><span class="k">来源总结</span><span class="v">#${esc(r.digest_run_id)}</span></div>` : ''}
    <div class="kv"><span class="k">申请时间</span><span class="v">${esc(fmtDate(r.created_at))}</span></div>
    ${r.reviewed_at ? `<div class="kv"><span class="k">审核时间</span><span class="v">${esc(fmtDate(r.reviewed_at))} · ${esc(r.reviewed_by || '-')}</span></div>` : ''}
    ${r.memory_id ? `<div class="kv"><span class="k">结果记忆</span><span class="v">memory #${esc(r.memory_id)}</span></div>` : ''}
  `;
}

/* ---------- 共享弹窗：原文证据 / 版本关系 / 审核记录 ---------- */

export async function fetchEvidenceMessages(ids) {
  const list = (ids || []).slice(0, 8);
  const rows = await Promise.all(list.map(async (id) => {
    try {
      const result = await gw(`/admin/api/data/chat_messages/${encodeURIComponent(id)}`);
      return result.data?.[0] || null;
    } catch {
      return { id, missing: true };
    }
  }));
  return rows.filter(Boolean);
}

export async function showEvidenceModal(ids, subject = '') {
  const { root, close } = modal({
    title: `原文证据${subject ? ` · ${subject}` : ''}`,
    body: `<div id="evidence-body">${loading('正在读取原文消息…')}</div>`,
    footer: '<button class="btn btn-secondary" data-close>关闭</button>',
    wide: true,
  });
  root.querySelector('[data-close]').onclick = close;
  const box = root.querySelector('#evidence-body');
  try {
    const rows = await fetchEvidenceMessages(ids);
    if (!rows.length) { box.innerHTML = empty('这条记录没有原文证据'); return; }
    const roleLabel = { user: '用户', assistant: '助手', system: '系统' };
    box.innerHTML = rows.map((m) => `
      <div class="mem-card" style="cursor:default">
        <div class="card-top">
          <div class="card-main">
            <div class="tag-row">
              ${tag(`#${esc(m.id)}`, 'muted')}
              ${tag(roleLabel[m.role] || esc(m.role || '未知'), 'slate')}
              ${tag(esc(fmtDate(m.created_at)), 'muted')}
            </div>
            <div class="mt8" style="white-space:pre-wrap">${m.missing ? '<span class="muted">该消息已不存在或读取失败</span>' : esc(m.content || '')}</div>
          </div>
        </div>
      </div>`).join('')
      + ((ids || []).length > 8 ? `<p class="muted text-sm">证据消息共 ${ids.length} 条，此处仅显示前 8 条。</p>` : '');
  } catch (error) {
    box.innerHTML = errorBlock(`读取原文证据失败：${esc(error.message)}`);
  }
}

export async function showVersionModal(memoryId) {
  const { root, close } = modal({
    title: `版本关系 · memory #${memoryId}`,
    body: `<div id="version-body">${loading('正在读取版本链…')}</div>`,
    footer: '<button class="btn btn-secondary" data-close>关闭</button>',
    wide: true,
  });
  root.querySelector('[data-close]').onclick = close;
  const box = root.querySelector('#version-body');
  const fetchOne = async (id) => {
    try {
      const rows = await query('memories', { select: MEMORY_FIELDS, eq: { id: Number(id) }, limit: 1 });
      return rows[0] || null;
    } catch { return null; }
  };
  try {
    const memory = (await fetchOne(memoryId));
    if (!memory) { box.innerHTML = empty('记忆不存在或已被删除'); return; }
    const prev = memory.supersedes_memory_id ? await fetchOne(memory.supersedes_memory_id) : null;
    const next = memory.superseded_by_memory_id ? await fetchOne(memory.superseded_by_memory_id) : null;
    let chain = [];
    if (memory.memory_key) {
      try {
        chain = await query('memories', {
          select: 'id,title,created_at,superseded_at,is_active',
          eq: { memory_key: memory.memory_key },
          order: { col: 'created_at', asc: true },
          limit: 100,
        });
      } catch { chain = []; }
    }
    const card = (m, note) => `
      <div class="mem-card" style="cursor:default">
        <div class="mem-title">memory #${esc(m.id)} · ${esc(m.title || '(未命名)')}</div>
        <div class="mem-snippet">${esc((m.content || '').slice(0, 160))}</div>
        <div class="card-meta">${note ? `${esc(note)} · ` : ''}创建于 ${esc(fmtDate(m.created_at))}${m.superseded_at ? ` · 于 ${esc(fmtDate(m.superseded_at))} 被取代` : ''}</div>
      </div>`;
    const chainHtml = chain.length > 1 ? `
      <div class="section-title">同主题键版本链（${esc(memory.memory_key)}）</div>
      ${chain.map((m) => `
        <div class="kv"><span class="k">memory #${esc(m.id)}${m.id === memory.id ? '（当前）' : ''}</span>
        <span class="v">${esc(m.title || '(未命名)')} · ${esc(fmtDate(m.created_at))}${m.is_active ? '' : ' · 已失效'}</span></div>`).join('')}` : '';
    box.innerHTML = `
      ${prev ? `<div class="section-title">上游（被本记忆取代）</div>${card(prev, '上游版本')}` : ''}
      <div class="section-title">当前记忆</div>${card(memory, verifiedMeta(memory).label)}
      ${next ? `<div class="section-title">下游（取代本记忆）</div>${card(next, '下游版本')}` : ''}
      ${!prev && !next && chain.length <= 1 ? `<p class="muted">这条记忆没有登记版本替换关系。</p>` : ''}
      ${chainHtml}`;
  } catch (error) {
    box.innerHTML = errorBlock(`读取版本关系失败：${esc(error.message)}`);
  }
}

export async function showRequestHistoryModal(requestId) {
  const { root, close } = modal({
    title: `申请 #${requestId} · 审核记录`,
    body: `<div id="review-history">${loading()}</div>`,
    footer: '<button class="btn btn-secondary" data-close>关闭</button>',
  });
  root.querySelector('[data-close]').onclick = close;
  const box = root.querySelector('#review-history');
  try {
    const rows = await query('memory_request_review_events', {
      select: 'id,request_id,action,from_status,to_status,target_memory_id,result_memory_id,reviewed_by,review_note,created_at',
      order: { col: 'created_at', asc: false },
      limit: 100,
      eq: { request_id: Number(requestId) },
    });
    box.innerHTML = rows.length ? rows.map((row) => `
      <div class="mem-card" style="cursor:default">
        <div class="mem-title">${esc(actionLabel(row.action))} · ${esc(row.from_status)} → ${esc(row.to_status)}</div>
        <div class="card-meta">
          ${esc(fmtDate(row.created_at))} · 操作者 ${esc(row.reviewed_by || '-')}
          ${row.target_memory_id ? ` · 目标 memory #${esc(row.target_memory_id)}` : ''}
          ${row.result_memory_id ? ` · 结果 memory #${esc(row.result_memory_id)}` : ''}
        </div>
        ${row.review_note ? `<div class="mt8 text-sm">${esc(row.review_note)}</div>` : ''}
      </div>`).join('') : empty('暂无审核事件', '旧版审核记录仍保留在申请本身');
  } catch (error) {
    box.innerHTML = errorBlock(`读取审核记录失败：${esc(error.message)}`);
  }
}

function actionLabel(action) {
  return {
    approve: '通过', reject: '拒绝', merge: '合并到现有记忆',
    duplicate: '标记为重复', conflict: '标记为冲突',
  }[action] || action;
}

/** 正式记忆的审核轨迹：反查引用它的申请与事件 */
export async function showMemoryTrailModal(memoryId) {
  const { root, close } = modal({
    title: `memory #${memoryId} · 审核记录`,
    body: `<div id="memory-trail">${loading()}</div>`,
    footer: '<button class="btn btn-secondary" data-close>关闭</button>',
  });
  root.querySelector('[data-close]').onclick = close;
  const box = root.querySelector('#memory-trail');
  try {
    const requests = await query('memory_requests', {
      select: 'id,status,created_at,reviewed_at,reviewed_by,review_note',
      order: { col: 'created_at', asc: false },
      limit: 20,
      eq: { memory_id: Number(memoryId) },
    });
    if (!requests.length) { box.innerHTML = empty('没有找到引用这条记忆的审核申请'); return; }
    const blocks = await Promise.all(requests.map(async (r) => {
      let events = [];
      try {
        events = await query('memory_request_review_events', {
          select: 'id,action,from_status,to_status,result_memory_id,reviewed_by,review_note,created_at',
          order: { col: 'created_at', asc: false },
          limit: 20,
          eq: { request_id: r.id },
        });
      } catch { events = []; }
      return `
        <div class="mem-card" style="cursor:default">
          <div class="mem-title">申请 #${esc(r.id)} <span class="tag tag-${reqStatusMeta(r.status).tone}">${esc(reqStatusMeta(r.status).label)}</span></div>
          <div class="card-meta">申请于 ${esc(fmtDate(r.created_at))}${r.reviewed_at ? ` · 审核于 ${esc(fmtDate(r.reviewed_at))} · ${esc(r.reviewed_by || '-')}` : ''}</div>
          ${r.review_note ? `<div class="mt8 text-sm">${esc(r.review_note)}</div>` : ''}
          ${events.length ? `<div class="mt8">${events.map((e) => `
            <div class="text-sm muted">${esc(fmtDate(e.created_at))} · ${esc(actionLabel(e.action))} ${esc(e.from_status)} → ${esc(e.to_status)}</div>`).join('')}</div>` : ''}
        </div>`;
    }));
    box.innerHTML = blocks.join('');
  } catch (error) {
    box.innerHTML = errorBlock(`读取审核记录失败：${esc(error.message)}`);
  }
}

/* ---------- 记忆浏览器工厂 ---------- */
export function createMemoryBrowser({
  host,
  lockedType = null,
  showTypeFilter = true,
  showViewTabs = true,
  defaultView = 'library',
  titlePrefix = '',
}) {
  const LIB_TABS = [
    { key: 'all', label: '全部' },
    { key: 'verified', label: '已确认' },
    { key: 'archived', label: '已归档' },
  ];
  const REQ_STATUSES = [
    { key: 'pending', label: '待审核' },
    { key: 'approved', label: '已通过' },
    { key: 'rejected', label: '已拒绝' },
    { key: 'merged', label: '已合并' },
    { key: 'duplicate', label: '重复' },
    { key: 'conflict', label: '冲突待处理' },
    { key: '', label: '全部状态' },
  ];
  const TYPE_OPTIONS = Object.entries(CONTINUITY_TYPE_LABELS);

  const state = {
    view: defaultView,
    libTab: 'all',
    type: lockedType || '',
    sort: 'created_at',
    search: '',
    page: 0,
    reqStatus: 'pending',
    reqSearch: '',
    reqPage: 0,
    reqRows: [],
    selected: null, // {kind:'mem'|'req', id}
  };

  host.classList.add('page-with-detail');
  host.innerHTML = `
    <div class="page-main">
      ${showViewTabs ? `
      <div class="toolbar" style="margin-bottom:14px">
        <div class="tabs" role="tablist">
          <button class="tab" data-act="view" data-view="library">${icon('book')}记忆库</button>
          <button class="tab" data-act="view" data-view="requests">${icon('inbox')}审核申请</button>
        </div>
      </div>` : ''}
      <div id="browser-body">${loading()}</div>
    </div>`;
  const body = host.querySelector('#browser-body');
  const panel = createDetailPanel(host);
  const pwd = host;

  /* ----- 渲染：工具栏与列表 ----- */
  function renderShell() {
    if (showViewTabs) {
      host.querySelectorAll('.tab[data-view]').forEach((el) => {
        el.classList.toggle('active', el.dataset.view === state.view);
      });
    }
    body.innerHTML = state.view === 'library' ? libraryShell() : requestsShell();
    wireControls();
    return state.view === 'library' ? loadLibrary() : loadRequests();
  }

  function libraryShell() {
    return `
      <div class="subtabs">
        ${LIB_TABS.map((t) => `<button class="subtab ${state.libTab === t.key ? 'active' : ''}" data-act="lib-tab" data-tab="${t.key}">${t.label}</button>`).join('')}
      </div>
      <div class="toolbar">
        <div class="search-box">${icon('search')}<input type="search" id="lib-search" placeholder="搜索标题或内容…" value="${esc(state.search)}"></div>
        ${showTypeFilter && !lockedType ? `
        <select id="lib-type" title="连续感类型">
          <option value="">全部类型</option>
          ${TYPE_OPTIONS.map(([k, v]) => `<option value="${k}" ${state.type === k ? 'selected' : ''}>${v}</option>`).join('')}
        </select>` : ''}
        <select id="lib-sort" title="排序">
          <option value="created_at" ${state.sort === 'created_at' ? 'selected' : ''}>按创建时间</option>
          <option value="heat" ${state.sort === 'heat' ? 'selected' : ''}>按热度</option>
          <option value="importance" ${state.sort === 'importance' ? 'selected' : ''}>按重要性</option>
        </select>
        <button class="btn btn-secondary" data-act="refresh">${icon('refresh')}刷新</button>
      </div>
      <div id="lib-list">${loading()}</div>
      <div id="lib-pager"></div>`;
  }

  function requestsShell() {
    return `
      <div class="toolbar">
        <select id="req-status" title="申请状态">
          ${REQ_STATUSES.map((s) => `<option value="${s.key}" ${state.reqStatus === s.key ? 'selected' : ''}>${s.label}</option>`).join('')}
        </select>
        ${showTypeFilter && !lockedType ? `
        <select id="req-type" title="连续感类型">
          <option value="">全部类型</option>
          ${TYPE_OPTIONS.map(([k, v]) => `<option value="${k}" ${state.type === k ? 'selected' : ''}>${v}</option>`).join('')}
        </select>` : ''}
        <div class="search-box">${icon('search')}<input type="search" id="req-search" placeholder="搜索当前结果内的标题 / 内容 / 编号…" value="${esc(state.reqSearch)}"></div>
        <button class="btn btn-secondary" data-act="refresh">${icon('refresh')}刷新</button>
      </div>
      <p class="muted text-sm" style="margin:0 0 12px">episode、profile、interaction_rule 会进入审核队列；moment、thread、inside_joke 校验通过后直接写入正式记忆。搜索作用于最近 ${REQ_FETCH_LIMIT} 条申请。</p>
      <div id="req-list">${loading()}</div>
      <div id="req-pager"></div>`;
  }

  function wireControls() {
    const libSearch = body.querySelector('#lib-search');
    if (libSearch) {
      libSearch.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { state.search = libSearch.value.trim(); state.page = 0; loadLibrary(); }
      });
      body.querySelector('#lib-type')?.addEventListener('change', (e) => { state.type = e.target.value; state.page = 0; loadLibrary(); });
      body.querySelector('#lib-sort')?.addEventListener('change', (e) => { state.sort = e.target.value; state.page = 0; loadLibrary(); });
    }
    const reqSearch = body.querySelector('#req-search');
    if (reqSearch) {
      reqSearch.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { state.reqSearch = reqSearch.value.trim(); state.reqPage = 0; renderRequestList(); }
      });
      body.querySelector('#req-status')?.addEventListener('change', (e) => { state.reqStatus = e.target.value; state.reqPage = 0; loadRequests(); });
      body.querySelector('#req-type')?.addEventListener('change', (e) => { state.type = e.target.value; state.reqPage = 0; loadRequests(); });
    }
  }

  /* ----- 记忆库 ----- */
  async function loadLibrary() {
    const list = body.querySelector('#lib-list');
    const pager = body.querySelector('#lib-pager');
    if (!list) return;
    list.innerHTML = loading();
    pager.innerHTML = '';
    try {
      const eq = { is_active: state.libTab !== 'archived' };
      if (state.libTab === 'verified') eq.verified = 'verified';
      if (state.type) eq.continuity_type = state.type;
      const [data, total] = await Promise.all([
        query('memories', {
          select: MEMORY_FIELDS,
          order: { col: state.sort, asc: false },
          limit: PAGE_SIZE,
          offset: state.page * PAGE_SIZE,
          eq,
          search: state.search,
        }),
        count('memories', { eq, search: state.search }),
      ]);
      list.innerHTML = data.length ? data.map(renderMemoryCard).join('') : empty('没有符合条件的记忆');
      pager.innerHTML = pagerHtml(state.page, Math.max(1, Math.ceil(total / PAGE_SIZE)), total);
      highlightSelected();
    } catch (error) {
      list.innerHTML = errorBlock(`记忆列表读取失败：${esc(error.message)}`);
    }
  }

  function renderMemoryCard(m) {
    const vm = verifiedMeta(m);
    return `
      <div class="mem-card ${m.id === state.selected?.id && state.selected?.kind === 'mem' ? 'selected' : ''}" data-act="open-mem" data-id="${m.id}">
        <div class="card-top">
          <div class="card-main">
            <div class="mem-title">#${m.id} · ${esc(m.title || '(未命名)')}</div>
            <div class="mem-snippet">${esc((m.content || '').slice(0, 160))}</div>
            <div class="tag-row mt8">
              ${tag(vm.label, vm.tone)}
              ${heatTag(m.heat)}
              ${impTag(m.importance)}
              ${typeTag(m.continuity_type)}
              ${m.layer ? tag(esc(m.layer), 'muted') : ''}
              ${(m.tags || []).slice(0, 3).map((t) => tag(esc(t), 'slate')).join('')}
              ${m.memory_key ? tag(`<span class="mono">${esc(m.memory_key)}</span>`, 'slate') : ''}
            </div>
            <div class="card-meta">创建于 ${esc(fmtDate(m.created_at))}${m.superseded_by_memory_id ? ` · 已被 memory #${esc(m.superseded_by_memory_id)} 取代` : ''}</div>
          </div>
        </div>
      </div>`;
  }

  /* ----- 审核申请 ----- */
  async function loadRequests() {
    const list = body.querySelector('#req-list');
    if (!list) return;
    list.innerHTML = loading();
    try {
      const eq = {};
      if (state.reqStatus) eq.status = state.reqStatus;
      if (state.type) eq.continuity_type = state.type;
      state.reqRows = await query('memory_requests', {
        select: REQUEST_FIELDS,
        order: { col: 'created_at', asc: false },
        limit: REQ_FETCH_LIMIT,
        eq,
      });
      state.reqPage = Math.min(state.reqPage, Math.max(0, Math.ceil(state.reqRows.length / PAGE_SIZE) - 1));
      renderRequestList();
    } catch (error) {
      list.innerHTML = errorBlock(`申请列表读取失败：${esc(error.message)}`);
    }
  }

  function filteredRequests() {
    const kw = state.reqSearch.toLowerCase();
    if (!kw) return state.reqRows;
    return state.reqRows.filter((r) => (
      String(r.id).includes(kw)
      || (r.title || '').toLowerCase().includes(kw)
      || (r.content || '').toLowerCase().includes(kw)
      || (r.memory_key || '').toLowerCase().includes(kw)
    ));
  }

  function renderRequestList() {
    const list = body.querySelector('#req-list');
    const pager = body.querySelector('#req-pager');
    if (!list) return;
    const rows = filteredRequests();
    const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
    const slice = rows.slice(state.reqPage * PAGE_SIZE, (state.reqPage + 1) * PAGE_SIZE);
    list.innerHTML = slice.length ? slice.map(renderRequestCard).join('')
      : (state.reqRows.length ? empty('当前结果中没有匹配的申请', '试试调整搜索关键词')
        : empty(state.reqStatus === 'pending' ? '当前没有待审核的记忆申请' : '没有符合条件的申请'));
    pager.innerHTML = rows.length > PAGE_SIZE
      ? pagerHtml(state.reqPage, pages, rows.length)
      : (rows.length ? `<div class="pagination"><span class="page-info">共 ${rows.length} 条</span></div>` : '');
    if (state.reqRows.length >= REQ_FETCH_LIMIT) {
      pager.insertAdjacentHTML('beforeend', '<span class="page-info">（仅加载最近 100 条，可用状态筛选缩小范围）</span>');
    }
    highlightSelected();
  }

  function renderRequestCard(r) {
    const sm = reqStatusMeta(r.status);
    const actionable = r.status === 'pending' || r.status === 'conflict';
    return `
      <div class="mem-card ${r.status === 'rejected' ? 'is-dim' : ''} ${r.id === state.selected?.id && state.selected?.kind === 'req' ? 'selected' : ''}" data-act="open-req" data-id="${r.id}">
        <div class="card-top">
          <div class="card-main">
            <div class="mem-title">#${r.id} · ${esc(r.title || '未命名记忆')}</div>
            <div class="mem-snippet">${esc(r.content || '')}</div>
            <div class="tag-row mt8">
              ${tag(sm.label, sm.tone)}
              ${typeTag(r.continuity_type)}
              ${impTag(r.importance)}
              ${r.confidence != null ? tag(`置信度 ${Number(r.confidence).toFixed(2)}`, 'slate') : ''}
              ${r.update_mode === 'replace' ? tag('替换更新', 'plum') : tag('新增记忆', 'muted')}
              ${r.memory_key ? tag(`<span class="mono">${esc(r.memory_key)}</span>`, 'slate') : ''}
              ${r.dedupe_state === 'possible_duplicate' ? tag('疑似重复，请人工判断', 'amber') : ''}
              ${r.related_request_id ? tag(`相似申请 #${esc(r.related_request_id)}`, 'amber') : ''}
              ${r.related_memory_id ? tag(`关联 memory #${esc(r.related_memory_id)}`, r.status === 'conflict' ? 'amber' : 'slate') : ''}
              ${(r.tags || []).map((t) => tag(esc(t), 'slate')).join('')}
            </div>
            <div class="card-meta">申请于 ${esc(fmtDate(r.created_at))}${r.reviewed_at ? ` · 审核于 ${esc(fmtDate(r.reviewed_at))}` : ''}${r.review_note ? ` · 备注：${esc(r.review_note)}` : ''}</div>
          </div>
          <div class="card-side">${actionable ? tag('待处理', 'amber') : ''}</div>
        </div>
      </div>`;
  }

  function highlightSelected() {
    body.querySelectorAll('.mem-card.selected').forEach((el) => el.classList.remove('selected'));
  }

  /* ----- 数据获取 ----- */
  async function fetchMemory(id) {
    const rows = await query('memories', { select: MEMORY_FIELDS, eq: { id: Number(id) }, limit: 1 });
    if (!rows[0]) throw new Error('记忆不存在或已被删除');
    return rows[0];
  }
  async function fetchRequest(id) {
    const rows = await query('memory_requests', { select: REQUEST_FIELDS, eq: { id: Number(id) }, limit: 1 });
    if (!rows[0]) throw new Error('申请不存在或已被删除');
    return rows[0];
  }

  /* ----- 详情渲染 ----- */
  function renderMemoryDetail(m) {
    const vm = verifiedMeta(m);
    const evidence = m.evidence_message_ids || [];
    const hasVersions = m.memory_key || m.supersedes_memory_id || m.superseded_by_memory_id;
    panel.render({
      title: `#${m.id} · ${esc(m.title || '(未命名)')}`,
      badges: tag(vm.label, vm.tone) + typeTag(m.continuity_type) + (m.layer ? tag(esc(m.layer), 'muted') : ''),
      html: memoryKvRows(m),
      actions: `
        <button class="btn btn-secondary btn-sm" data-act="mem-edit" data-id="${m.id}">${icon('edit')}编辑</button>
        ${m.verified === 'pending' ? `
          <button class="btn btn-primary btn-sm" data-act="mem-verify" data-id="${m.id}">${icon('check')}通过</button>
          <button class="btn btn-danger-line btn-sm" data-act="mem-reject" data-id="${m.id}">${icon('x')}驳回</button>` : ''}
        ${m.is_active
          ? `<button class="btn btn-danger-line btn-sm" data-act="mem-archive" data-id="${m.id}">${icon('archive')}归档</button>`
          : `<button class="btn btn-secondary btn-sm" data-act="mem-restore" data-id="${m.id}">${icon('refresh')}恢复</button>`}
        ${evidence.length ? `<button class="btn btn-quiet btn-sm" data-act="mem-evidence" data-id="${m.id}">${icon('message')}查看原文证据</button>` : ''}
        ${hasVersions ? `<button class="btn btn-quiet btn-sm" data-act="mem-versions" data-id="${m.id}">${icon('layers')}查看版本关系</button>` : ''}
        <button class="btn btn-quiet btn-sm" data-act="mem-trail" data-id="${m.id}">${icon('clock')}查看审核记录</button>`,
    });
  }

  function renderRequestDetail(r) {
    const sm = reqStatusMeta(r.status);
    const actionable = r.status === 'pending' || r.status === 'conflict';
    const evidenceBtn = (r.evidence_message_ids || []).length
      ? `<button class="btn btn-quiet btn-sm" data-act="req-evidence" data-id="${r.id}">${icon('message')}查看原文证据</button>` : '';
    panel.render({
      title: `#${r.id} · ${esc(r.title || '未命名记忆')}`,
      badges: tag(sm.label, sm.tone) + typeTag(r.continuity_type),
      html: requestKvRows(r),
      actions: actionable ? `
        <button class="btn btn-primary btn-sm" data-act="req-approve" data-id="${r.id}">${icon('check')}${r.status === 'conflict' ? '解决冲突并通过' : '编辑并通过'}</button>
        <button class="btn btn-secondary btn-sm" data-act="req-merge" data-id="${r.id}">${icon('merge')}人工合并</button>
        <button class="btn btn-quiet btn-sm" data-act="req-duplicate" data-id="${r.id}">${icon('copy')}标记重复</button>
        ${r.status === 'pending' ? `<button class="btn btn-danger-line btn-sm" data-act="req-conflict" data-id="${r.id}">${icon('alert')}标记冲突</button>` : ''}
        <button class="btn btn-danger-line btn-sm" data-act="req-reject" data-id="${r.id}">${icon('x')}拒绝</button>
        ${evidenceBtn}
        ${r.reviewed_at ? `<button class="btn btn-quiet btn-sm" data-act="req-history" data-id="${r.id}">${icon('clock')}审核记录</button>` : ''}` : `
        ${r.memory_id ? `<button class="btn btn-secondary btn-sm" data-act="req-open-memory" data-id="${r.id}">${icon('book')}查看结果记忆</button>` : ''}
        ${evidenceBtn}
        <button class="btn btn-quiet btn-sm" data-act="req-history" data-id="${r.id}">${icon('clock')}审核记录</button>`,
    });
  }

  /* ----- 面板内编辑表单 ----- */
  function renderMemoryEdit(m) {
    panel.render({
      title: `编辑 memory #${m.id}`,
      badges: tag('编辑模式', 'gold'),
      html: `
        <div class="field"><label>标题</label><input type="text" id="ed-title" maxlength="100" value="${esc(m.title || '')}"></div>
        <div class="field"><label>内容</label><textarea id="ed-content" rows="7" maxlength="600">${esc(m.content || '')}</textarea></div>
        <div class="field"><label>标签（逗号分隔，最多 5 个）</label><input type="text" id="ed-tags" value="${esc((m.tags || []).join(', '))}"></div>
        <div class="grid grid-3">
          <div class="field"><label>重要性（1-10）</label><input type="number" id="ed-imp" min="1" max="10" value="${esc(m.importance ?? 5)}"></div>
          <div class="field"><label>层级</label><select id="ed-layer"><option value="碎片">碎片</option><option value="场景">场景</option><option value="核心">核心</option></select></div>
          <div class="field"><label>情感权重（0-1）</label><input type="number" id="ed-emo" min="0" max="1" step="0.1" value="${esc(m.emotion_weight ?? 0.5)}"></div>
        </div>
        <div class="field"><label>召回场景（可空；保存后由服务端重新生成召回向量）</label><textarea id="ed-recall-scene" rows="2">${esc(m.recall_scene || '')}</textarea></div>
        <div class="field"><label>召回标签（逗号分隔，自由填写）</label><input type="text" id="ed-recall-tags" value="${esc((m.recall_tags || []).join(', '))}"></div>
        <div class="grid grid-2">
          <div class="field"><label>最后证据时间（可空，ISO 格式）</label><input type="text" id="ed-evidence-time" maxlength="40" value="${esc(m.evidence_end_time || '')}"></div>
          <div class="field"><label>证据时间精度</label><select id="ed-evidence-precision">
            <option value="">未指定</option>
            <option value="minute">精确到分钟</option>
            <option value="hour">精确到小时</option>
            <option value="day">精确到日期</option>
            <option value="approximate">大概时间</option>
            <option value="unknown">时间未知</option>
          </select></div>
        </div>`,
      actions: `
        <button class="btn btn-primary btn-sm" data-act="mem-edit-save" data-id="${m.id}">${icon('check')}保存</button>
        <button class="btn btn-secondary btn-sm" data-act="mem-edit-cancel" data-id="${m.id}">取消</button>`,
    });
    panel.el.querySelector('#ed-layer').value = m.layer || '碎片';
    panel.el.querySelector('#ed-evidence-precision').value = m.evidence_time_precision || '';
  }

  async function saveMemoryEdit(id) {
    const root = panel.el;
    const emotionWeight = Number(root.querySelector('#ed-emo').value);
    const row = {
      title: root.querySelector('#ed-title').value.trim(),
      content: root.querySelector('#ed-content').value.trim(),
      tags: root.querySelector('#ed-tags').value.split(/[,，]/).map((s) => s.trim()).filter(Boolean).slice(0, 5),
      importance: Number(root.querySelector('#ed-imp').value) || 5,
      layer: root.querySelector('#ed-layer').value,
      emotion_weight: Number.isFinite(emotionWeight) ? emotionWeight : 0.5,
    };
    if (!row.content) { toast('内容不能为空', 'err'); return; }
    if (!Number.isInteger(row.importance) || row.importance < 1 || row.importance > 10) {
      toast('重要性必须是 1 到 10 的整数', 'err');
      return;
    }
    // 召回字段走专用原子端点：服务端在同一写入里重算召回向量，
    // 场景与向量不可能出现新旧不一致。
    const recallPayload = {
      recall_scene: root.querySelector('#ed-recall-scene').value.trim(),
      recall_tags: root.querySelector('#ed-recall-tags').value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
      evidence_end_time: root.querySelector('#ed-evidence-time').value.trim() || null,
      evidence_time_precision: root.querySelector('#ed-evidence-precision').value || null,
    };
    try {
      await gw(`/admin/api/memories/${encodeURIComponent(id)}/recall`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(recallPayload),
      });
      await update('memories', id, row);
      toast('记忆已更新');
      await Promise.all([loadCurrentList(), showMemory(id)]);
    } catch (error) {
      toast(`保存失败：${error.message}`, 'err');
    }
  }

  function renderRequestApprove(r) {
    panel.render({
      title: `${r.status === 'conflict' ? '解决冲突并通过' : '编辑并通过'} · 申请 #${r.id}`,
      badges: tag('审核表单', 'gold') + typeTag(r.continuity_type),
      html: `
        <div class="field"><label>标题</label><input type="text" id="rv-title" maxlength="100" value="${esc(r.title || '')}"></div>
        <div class="field"><label>记忆内容</label><textarea id="rv-content" rows="7" maxlength="600">${esc(r.content || '')}</textarea></div>
        <div class="field"><label>标签（逗号分隔，最多 5 个）</label><input type="text" id="rv-tags" value="${esc((r.tags || []).join(', '))}"></div>
        <div class="field"><label>重要性（1-10）</label><input type="number" id="rv-importance" min="1" max="10" value="${esc(Number(r.importance) || 5)}"></div>
        <div class="field"><label>写入方式</label><select id="rv-update-mode">
          <option value="append" ${r.update_mode !== 'replace' ? 'selected' : ''}>新增独立记忆</option>
          <option value="replace" ${r.update_mode === 'replace' ? 'selected' : ''}>替换同一可变事实的旧版本</option>
        </select></div>
        <div class="field"><label>稳定主题键（替换时必填）</label><input type="text" id="rv-memory-key" maxlength="120" value="${esc(r.memory_key || '')}" placeholder="例如 project.qi-gateway.progress">
          <div class="disabled-note" style="margin-top:4px">同一进度、状态或位置的后续更新必须使用完全相同的键；普通相似内容不要使用替换。</div></div>
        <div class="field"><label>召回场景（可空；保存后由服务端生成召回向量）</label><textarea id="rv-recall-scene" rows="2">${esc(r.recall_scene || '')}</textarea></div>
        <div class="field"><label>召回标签（逗号分隔，自由填写）</label><input type="text" id="rv-recall-tags" value="${esc((r.recall_tags || []).join(', '))}"></div>
        <div class="grid grid-2">
          <div class="field"><label>最后证据时间（可空，ISO 格式）</label><input type="text" id="rv-evidence-time" maxlength="40" value="${esc(r.evidence_end_time || '')}"></div>
          <div class="field"><label>证据时间精度</label><select id="rv-evidence-precision">
            <option value="">未指定</option>
            <option value="minute">精确到分钟</option>
            <option value="hour">精确到小时</option>
            <option value="day">精确到日期</option>
            <option value="approximate">大概时间</option>
            <option value="unknown">时间未知</option>
          </select></div>
        </div>
        <div class="field"><label>审核备注（可选）</label><textarea id="rv-note" rows="3" maxlength="500"></textarea></div>`,
      actions: `
        <button class="btn btn-primary btn-sm" data-act="req-approve-save" data-id="${r.id}">${icon('check')}通过并写入记忆</button>
        <button class="btn btn-secondary btn-sm" data-act="req-detail-back" data-id="${r.id}">取消</button>`,
    });
  }

  async function saveRequestApprove(id) {
    const root = panel.el;
    const content = root.querySelector('#rv-content').value.trim();
    const importance = Number(root.querySelector('#rv-importance').value);
    const updateMode = root.querySelector('#rv-update-mode').value;
    const memoryKey = root.querySelector('#rv-memory-key').value.trim().toLowerCase().replace(/\s+/g, '-');
    if (content.length < 5) { toast('记忆内容至少需要 5 个字符', 'err'); return; }
    if (!Number.isInteger(importance) || importance < 1 || importance > 10) {
      toast('重要性必须是 1 到 10 的整数', 'err');
      return;
    }
    if (updateMode === 'replace' && !/^[a-z0-9][a-z0-9._:/-]{2,119}$/.test(memoryKey)) {
      toast('替换模式必须填写 3-120 位有效稳定主题键', 'err');
      return;
    }
    if (updateMode === 'append' && memoryKey) {
      toast('新增独立记忆时请清空稳定主题键', 'err');
      return;
    }
    const button = panel.el.querySelector('[data-act="req-approve-save"]');
    button.disabled = true;
    try {
      await submitReview(id, {
        action: 'approve',
        title: root.querySelector('#rv-title').value.trim(),
        content,
        tags: root.querySelector('#rv-tags').value.split(/[,，]/).map((t) => t.trim()).filter(Boolean),
        importance,
        update_mode: updateMode,
        memory_key: memoryKey || null,
        recall_scene: root.querySelector('#rv-recall-scene').value.trim(),
        recall_tags: root.querySelector('#rv-recall-tags').value.split(/[,，]/).map((t) => t.trim()).filter(Boolean),
        evidence_end_time: root.querySelector('#rv-evidence-time').value.trim() || null,
        evidence_time_precision: root.querySelector('#rv-evidence-precision').value || null,
        review_note: root.querySelector('#rv-note').value.trim(),
      });
      toast('申请已通过，正式记忆已写入');
      state.selected = null;
      await Promise.all([loadCurrentList(), reloadPanel()]);
    } catch (error) {
      toast(`审核失败：${error.message}`, 'err');
      button.disabled = false;
    }
  }

  function renderRequestReject(r) {
    panel.render({
      title: `拒绝申请 #${r.id}`,
      badges: tag('审核表单', 'gold'),
      html: `
        ${banner('拒绝后该申请会保留在审核历史中，不会写入正式记忆。', 'danger')}
        <div class="field"><label>拒绝原因（可选）</label><textarea id="rj-note" rows="4" maxlength="500" placeholder="该记录会保留在审核历史中"></textarea></div>`,
      actions: `
        <button class="btn btn-danger btn-sm" data-act="req-reject-save" data-id="${r.id}">${icon('x')}确认拒绝</button>
        <button class="btn btn-secondary btn-sm" data-act="req-detail-back" data-id="${r.id}">取消</button>`,
    });
  }

  async function saveRequestReject(id) {
    const button = panel.el.querySelector('[data-act="req-reject-save"]');
    button.disabled = true;
    try {
      await submitReview(id, {
        action: 'reject',
        review_note: panel.el.querySelector('#rj-note').value.trim(),
      });
      toast('申请已拒绝');
      state.selected = null;
      await Promise.all([loadCurrentList(), reloadPanel()]);
    } catch (error) {
      toast(`审核失败：${error.message}`, 'err');
      button.disabled = false;
    }
  }

  /* ----- 人工合并 / 标记重复 / 标记冲突（复杂弹窗） ----- */
  function relationLabel(action) {
    return { merge: '人工合并', duplicate: '标记为重复', conflict: '标记为冲突' }[action] || action;
  }

  async function openRelation(id, action) {
    let request;
    try {
      request = await fetchRequest(id);
    } catch (error) {
      toast(error.message, 'err');
      return;
    }
    if (!['pending', 'conflict'].includes(request.status)) {
      toast('这条申请已经处理完毕', 'err');
      return;
    }

    let selected = null;
    const { root, close } = modal({
      title: `${relationLabel(action)} · 申请 #${id}`,
      body: `
        ${banner('必须由你明确选择一条已审核且仍有效的记忆。系统不会按相似度自动覆盖。')}
        <div class="field"><label>搜索现有记忆</label>
          <div class="toolbar" style="margin-bottom:10px">
            <div class="search-box">${icon('search')}<input type="search" id="relation-search" placeholder="输入标题或内容关键词"></div>
            <button class="btn btn-secondary" id="relation-search-button">${icon('search')}搜索</button>
          </div>
        </div>
        <div id="relation-results">${loading()}</div>
        <div id="relation-merge-fields"></div>
        <div class="field"><label>审核备注（可选）</label><textarea id="relation-note" rows="3" maxlength="500"></textarea></div>`,
      footer: `<button class="btn btn-secondary" data-cancel>取消</button><button class="btn btn-primary" data-submit>${relationLabel(action)}</button>`,
      wide: true,
      draggable: true,
    });

    const results = root.querySelector('#relation-results');
    const mergeFields = root.querySelector('#relation-merge-fields');

    const renderSelection = () => {
      if (action !== 'merge' || !selected) { mergeFields.innerHTML = ''; return; }
      const tags = [...new Set([...(selected.tags || []), ...(request.tags || [])])].slice(0, 5);
      const mergedContent = [selected.content, request.content].filter(Boolean).join('\n');
      mergeFields.innerHTML = `
        ${banner(`请把两条内容整理成一条自然、准确的最终记忆。保存后旧 memory #${esc(selected.id)} 会软失效，但仍保留历史链接。`, 'danger')}
        <div class="field"><label>合并后标题</label><input type="text" id="merge-title" maxlength="100" value="${esc(request.title || selected.title || '')}"></div>
        <div class="field"><label>合并后内容</label><textarea id="merge-content" rows="8" maxlength="600">${esc(mergedContent)}</textarea></div>
        <div class="field"><label>标签（逗号分隔，最多 5 个）</label><input type="text" id="merge-tags" value="${esc(tags.join(', '))}"></div>
        <div class="field"><label>重要性（1-10）</label><input type="number" id="merge-importance" min="1" max="10" value="${Math.max(Number(selected.importance) || 5, Number(request.importance) || 5)}"></div>`;
    };

    const loadMemories = async () => {
      results.innerHTML = loading();
      try {
        const rows = await query('memories', {
          select: 'id,title,content,tags,importance,layer,created_at',
          order: { col: 'created_at', asc: false },
          limit: 20,
          eq: { is_active: true, verified: 'verified' },
          search: root.querySelector('#relation-search').value.trim(),
        });
        if (!rows.length) { results.innerHTML = empty('没有找到可关联的已审核记忆'); return; }
        results.innerHTML = rows.map((memory) => `
          <button type="button" class="mem-card relation-target" data-memory-id="${memory.id}">
            <div class="mem-title">memory #${memory.id} · ${esc(memory.title || '未命名')}</div>
            <div class="mem-snippet">${esc((memory.content || '').slice(0, 220))}</div>
            <div class="tag-row mt8">${impTag(memory.importance)}${memory.layer ? tag(esc(memory.layer), 'slate') : ''}</div>
          </button>`).join('');
        for (const button of results.querySelectorAll('.relation-target')) {
          button.onclick = () => {
            selected = rows.find((memory) => String(memory.id) === button.dataset.memoryId) || null;
            for (const item of results.querySelectorAll('.relation-target')) item.classList.remove('selected');
            button.classList.add('selected');
            renderSelection();
          };
        }
      } catch (error) {
        results.innerHTML = errorBlock(`候选记忆读取失败：${esc(error.message)}`);
      }
    };

    root.querySelector('[data-cancel]').onclick = close;
    root.querySelector('#relation-search-button').onclick = loadMemories;
    root.querySelector('#relation-search').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); loadMemories(); }
    });
    root.querySelector('[data-submit]').onclick = async (event) => {
      if (!selected) { toast('请先选择一条现有记忆', 'err'); return; }
      const payload = {
        action,
        related_memory_id: Number(selected.id),
        review_note: root.querySelector('#relation-note').value.trim(),
      };
      if (action === 'merge') {
        const content = root.querySelector('#merge-content')?.value.trim() || '';
        const importance = Number(root.querySelector('#merge-importance')?.value);
        if (content.length < 5) { toast('合并后内容至少需要 5 个字符', 'err'); return; }
        if (!Number.isInteger(importance) || importance < 1 || importance > 10) {
          toast('重要性必须是 1 到 10 的整数', 'err');
          return;
        }
        Object.assign(payload, {
          content,
          title: root.querySelector('#merge-title')?.value.trim() || '',
          tags: (root.querySelector('#merge-tags')?.value || '').split(/[,，]/).map((t) => t.trim()).filter(Boolean),
          importance,
        });
      }
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await submitReview(id, payload);
        toast(action === 'merge' ? '记忆已人工合并' : action === 'duplicate' ? '申请已标记为重复' : '申请已进入冲突待处理队列');
        close();
        state.selected = null;
        await Promise.all([loadCurrentList(), reloadPanel()]);
      } catch (error) {
        toast(`审核失败：${error.message}`, 'err');
        button.disabled = false;
      }
    };
    await loadMemories();
  }

  async function submitReview(id, payload) {
    return gw(`/admin/api/memory-requests/${encodeURIComponent(id)}/review`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  }

  /* ----- 打开详情 / 刷新 ----- */
  async function showMemory(id) {
    state.selected = { kind: 'mem', id: Number(id) };
    highlightCard('mem', id);
    try {
      renderMemoryDetail(await fetchMemory(id));
    } catch (error) {
      panel.render({ title: '记忆详情', html: errorBlock(esc(error.message)), actions: '' });
    }
  }

  async function showRequest(id) {
    state.selected = { kind: 'req', id: Number(id) };
    highlightCard('req', id);
    try {
      renderRequestDetail(await fetchRequest(id));
    } catch (error) {
      panel.render({ title: '申请详情', html: errorBlock(esc(error.message)), actions: '' });
    }
  }

  function highlightCard(kind, id) {
    body.querySelectorAll('.mem-card').forEach((el) => {
      const act = el.dataset.act;
      const match = (kind === 'mem' && act === 'open-mem' && Number(el.dataset.id) === Number(id))
        || (kind === 'req' && act === 'open-req' && Number(el.dataset.id) === Number(id));
      el.classList.toggle('selected', match);
    });
  }

  async function reloadPanel() {
    if (!state.selected) {
      panel.render({ title: '', html: '', actions: '' });
      return;
    }
    if (state.selected.kind === 'mem') await showMemory(state.selected.id);
    else await showRequest(state.selected.id);
  }

  function loadCurrentList() {
    return state.view === 'library' ? loadLibrary() : loadRequests();
  }

  async function setView(view) {
    if (state.view === view) return;
    state.view = view;
    state.selected = null;
    panel.render({ title: '', html: '', actions: '' });
    await renderShell();
  }

  /* ----- 事件绑定 ----- */
  delegate(host, {
    view: (el) => setView(el.dataset.view),
    'lib-tab': (el) => {
      state.libTab = el.dataset.tab;
      state.page = 0;
      body.querySelectorAll('.subtab').forEach((t) => t.classList.toggle('active', t.dataset.tab === state.libTab));
      loadLibrary();
    },
    refresh: () => loadCurrentList(),
    page: (el) => {
      if (state.view === 'library') { state.page = Number(el.dataset.p); loadLibrary(); }
      else { state.reqPage = Number(el.dataset.p); renderRequestList(); }
    },
    'open-mem': (el) => showMemory(el.dataset.id),
    'open-req': (el) => showRequest(el.dataset.id),
    'mem-edit': (el) => fetchMemory(el.dataset.id).then(renderMemoryEdit).catch((e) => toast(e.message, 'err')),
    'mem-edit-cancel': (el) => showMemory(el.dataset.id),
    'mem-edit-save': (el) => saveMemoryEdit(el.dataset.id),
    'mem-archive': async (el) => {
      if (!(await confirm('归档这条记忆？归档后不再参与召回，但仍保留在数据库中。', { okText: '归档' }))) return;
      try {
        await update('memories', el.dataset.id, { is_active: false });
        toast('已归档');
        await Promise.all([loadCurrentList(), showMemory(el.dataset.id)]);
      } catch (error) { toast(`操作失败：${error.message}`, 'err'); }
    },
    'mem-restore': async (el) => {
      try {
        await update('memories', el.dataset.id, { is_active: true });
        toast('已恢复');
        await Promise.all([loadCurrentList(), showMemory(el.dataset.id)]);
      } catch (error) { toast(`操作失败：${error.message}`, 'err'); }
    },
    'mem-verify': async (el) => {
      try {
        await update('memories', el.dataset.id, { verified: 'verified' });
        toast('已标记为已确认');
        await Promise.all([loadCurrentList(), showMemory(el.dataset.id)]);
      } catch (error) { toast(`操作失败：${error.message}`, 'err'); }
    },
    'mem-reject': async (el) => {
      try {
        await update('memories', el.dataset.id, { verified: 'rejected' });
        toast('已驳回该记忆');
        await Promise.all([loadCurrentList(), showMemory(el.dataset.id)]);
      } catch (error) { toast(`操作失败：${error.message}`, 'err'); }
    },
    'mem-evidence': async (el) => {
      try {
        const m = await fetchMemory(el.dataset.id);
        await showEvidenceModal(m.evidence_message_ids || [], `memory #${m.id}`);
      } catch (error) { toast(error.message, 'err'); }
    },
    'mem-versions': (el) => showVersionModal(el.dataset.id),
    'mem-trail': (el) => showMemoryTrailModal(el.dataset.id),
    'req-approve': async (el) => {
      try { renderRequestApprove(await fetchRequest(el.dataset.id)); }
      catch (error) { toast(error.message, 'err'); }
    },
    'req-approve-save': (el) => saveRequestApprove(el.dataset.id),
    'req-reject': async (el) => {
      try { renderRequestReject(await fetchRequest(el.dataset.id)); }
      catch (error) { toast(error.message, 'err'); }
    },
    'req-reject-save': (el) => saveRequestReject(el.dataset.id),
    'req-detail-back': (el) => showRequest(el.dataset.id),
    'req-merge': (el) => openRelation(el.dataset.id, 'merge'),
    'req-duplicate': (el) => openRelation(el.dataset.id, 'duplicate'),
    'req-conflict': (el) => openRelation(el.dataset.id, 'conflict'),
    'req-history': (el) => showRequestHistoryModal(el.dataset.id),
    'req-evidence': async (el) => {
      try {
        const r = await fetchRequest(el.dataset.id);
        await showEvidenceModal(r.evidence_message_ids || [], `申请 #${r.id}`);
      } catch (error) { toast(error.message, 'err'); }
    },
    'req-open-memory': async (el) => {
      try {
        const r = await fetchRequest(el.dataset.id);
        if (!r.memory_id) { toast('这条申请还没有关联的正式记忆', 'err'); return; }
        await setView('library');
        state.libTab = 'all';
        state.search = '';
        state.page = 0;
        await renderShell();
        await showMemory(r.memory_id);
      } catch (error) { toast(error.message, 'err'); }
    },
  });

  /* ----- 启动与外部入口 ----- */
  async function mount() {
    await renderShell();
  }

  async function openMemory(id) {
    if (state.view !== 'library') { state.view = 'library'; await renderShell(); }
    await showMemory(id);
  }

  async function openRequest(id) {
    if (state.view !== 'requests') { state.view = 'requests'; await renderShell(); }
    await showRequest(id);
  }

  return { mount, reload: loadCurrentList, openMemory, openRequest, state, panel };
}
