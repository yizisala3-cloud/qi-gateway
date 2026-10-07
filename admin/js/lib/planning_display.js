// Planning display helpers: argument-only formatting and HTML; no DOM or network writes.
import { tag, icon, esc } from '../ui.js?v=20261007-button-anchored';

export const TASK_TYPE_LABELS = {
  daily: '每日', interval: '间歇', weekly: '每周', monthly: '每月', once: '单次', idle: '闲时',
};
export const TASK_TYPES = Object.keys(TASK_TYPE_LABELS);
export const WEEKDAY_NAMES = ['一', '二', '三', '四', '五', '六', '日'];
export const STATUS_META = {
  // BUG-12：pending/in_progress 显示名与今日分区名（待处理/进度中）撞车，改为「未开始/执行中」
  pending: { label: '未开始', tone: 'muted' },
  in_progress: { label: '执行中', tone: 'amber' },
  completed: { label: '已完成', tone: 'green' },
  partial: { label: '部分完成', tone: 'gold' },
  deferred: { label: '已延后', tone: 'slate' },
  discarded_this: { label: '此次废弃', tone: 'muted' },
  discarded: { label: '已删除', tone: 'red' },
  timeout: { label: '已超时', tone: 'red' },
};
export const CLOSED_STATUSES = ['completed', 'discarded_this', 'discarded'];

export function statusTag(status) {
  const meta = STATUS_META[status] || { label: status, tone: 'muted' };
  return tag(esc(meta.label), meta.tone);
}

export function fmtClock(value) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '-';
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }).format(date);
}

export function fmtRange(start, end) {
  if (!start && !end) return '未排时间';
  return `${fmtClock(start)} ～ ${fmtClock(end)}`;
}

export function fmtDue(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date);
}

export function taskTypeSummary(task) {
  switch (task.task_type) {
    case 'daily': return '每天出现';
    case 'interval': return `每 ${task.interval_days || '?'} 天（完成后起算）`;
    case 'weekly': {
      const days = (task.weekdays || []).map((d) => `周${WEEKDAY_NAMES[d] ?? d}`);
      return days.length ? days.join('、') : '每周（未选星期）';
    }
    case 'monthly': {
      const days = task.month_days || [];
      return days.length ? `每月 ${days.join('、')} 日` : '每月（未选日期）';
    }
    case 'once': return `单次 · ${task.target_date || '未定日期'}`;
    case 'idle': return '闲时处理，沉底显示';
    default: return '';
  }
}

export function typeSummaryTag(task) {
  return tag(esc(taskTypeSummary(task)), 'slate');
}

// BUG-13：三分区空态改为一行式小空态（小图标 + 纯文字，高度受限）
export function miniEmpty(msg) {
  return `<div class="plan-empty-mini">${icon('feather')}<span>${esc(msg)}</span></div>`;
}

export function itemMeta(occ) {
  const parts = [];
  if (occ.est_start || occ.est_end) parts.push(`预估 ${fmtRange(occ.est_start, occ.est_end)}`);
  if (occ.actual_start || occ.actual_end) {
    parts.push(`实际 ${fmtRange(occ.actual_start, occ.actual_end)}`);
  }
  const duration = durationText(occ);
  if (duration) parts.push(duration);
  // 可安排时段是 user 排程约束，与系统预估起止是两套独立语义，分开呈现
  const windowParts = [];
  if (occ.window_start_at) windowParts.push(`不早于 ${fmtClock(occ.window_start_at)}`);
  if (occ.window_end_at) windowParts.push(`最晚完成 ${fmtClock(occ.window_end_at)}`);
  if (windowParts.length) parts.push(`时段 ${windowParts.join('，')}`);
  if (occ.partial_note) parts.push(`说明：${esc(occ.partial_note)}`);
  return parts.join(' · ');
}

export function isClosedOcc(occ) {
  return CLOSED_STATUSES.includes(occ.status) || occ.status === 'timeout';
}

export function formatLoggedDuration(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const parts = [];
  if (h) parts.push(`${h}h`);
  if (m) parts.push(`${m}m`);
  if (s || !parts.length) parts.push(`${s}s`);
  return parts.join('');
}

export function durationText(occ) {
  if (isClosedOcc(occ)) {
    if (occ.actual_logged_seconds != null) {
      return `实际耗时 ${formatLoggedDuration(occ.actual_logged_seconds)}`;
    }
    return occ.estimated_minutes ? `预估耗时 ${occ.estimated_minutes}m` : '';
  }
  const parts = [];
  if (occ.actual_minutes != null) parts.push(`实际耗时 ${occ.actual_minutes}m`);
  if (occ.estimated_minutes) parts.push(`预估耗时 ${occ.estimated_minutes}m`);
  return parts.join(' · ');
}

export function durationDetailRows(occ) {
  if (isClosedOcc(occ)) {
    if (occ.actual_logged_seconds != null) {
      return '<div class="kv"><span class="k">实际耗时</span><span class="v">'
        + `${formatLoggedDuration(occ.actual_logged_seconds)}</span></div>`;
    }
    return '<div class="kv"><span class="k">预估耗时</span><span class="v">'
      + `${occ.estimated_minutes ? occ.estimated_minutes + 'm' : '-'}</span></div>`;
  }
  return '<div class="kv"><span class="k">实际耗时</span><span class="v">'
    + `${occ.actual_minutes != null ? occ.actual_minutes + 'm' : '-'}</span></div>`
    + '<div class="kv"><span class="k">预估耗时</span><span class="v">'
    + `${occ.estimated_minutes ? occ.estimated_minutes + 'm' : '-'}</span></div>`;
}

export function itemBadges(occ, conflictOccIds = null) {
  const badges = [statusTag(occ.status)];
  if (occ.schedule_label && occ.schedule_label !== '正常') {
    badges.push(tag(esc(occ.schedule_label), occ.schedule_label === '超时' ? 'red' : 'amber'));
  }
  if (occ.is_fixed) badges.push(tag('固定', 'slate'));
  if (occ.phase === 'start') badges.push(tag('开始阶段', 'plum'));
  if (occ.phase === 'end') badges.push(tag('结束阶段', 'plum'));
  if (conflictOccIds?.has(occ.id)) badges.push(tag('排程冲突', 'red'));
  if (occ.source === 'early') badges.push(tag('提前完成', 'muted'));
  return badges.join('');
}

export function itemHtml(occ, { draggable = false, closed = false, idle = false } = {}, conflictOccIds = null) {
  return `
    <div class="plan-item ${closed ? 'is-closed' : ''} ${idle ? 'is-idle' : ''}"
         data-occ="${occ.id}" role="button" tabindex="0">
      ${draggable ? `<span class="plan-handle" aria-hidden="true">${icon('menu')}</span>` : ''}
      <div class="plan-item-main">
        <div class="plan-item-title">${esc(occ.content)}</div>
        <div class="plan-item-meta"><span>${itemMeta(occ)}</span></div>
      </div>
      <div class="plan-item-side"><div class="tag-row">${itemBadges(occ, conflictOccIds)}</div></div>
    </div>`;
}
