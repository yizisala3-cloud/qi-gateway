// Shared paper calendar and time picker. Hidden values retain the caller's id/value
// and input-event contract; memory continues to normalize/preserve timestamps itself.
import { esc, icon, registerFloatingSurface } from '../ui.js?v=20261012-integrated-controls-sync';

let activeRetroTimePop = null;
let sequence = 0;
const fields = new WeakMap();
const pad2 = n => String(n).padStart(2, '0');
const padYear = n => String(n).padStart(4, '0');
const DATETIME = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?$/;
const TIME = /^(\d{2}):(\d{2})(?::\d{2})?$/;

function closeRetroTimePop(restoreFocus = false) {
  const pop = activeRetroTimePop;
  if (!pop) return;
  activeRetroTimePop = null;
  pop._cleanup();
  pop.remove();
  if (restoreFocus && pop._anchor.isConnected && !pop._anchor.matches(':disabled')) {
    pop._anchor.focus({ preventScroll: true });
  }
}

function nowLocalInput(clock) {
  const now = new Date(Date.now() + (clock === 'local' ? 0 : 8 * 3600 * 1000));
  const get = part => now[`${clock === 'local' ? 'get' : 'getUTC'}${part}`]();
  return `${padYear(get('FullYear'))}-${pad2(get('Month') + 1)}-${pad2(get('Date'))}T${pad2(get('Hours'))}:${pad2(get('Minutes'))}`;
}

function calendarDate(year, month, day) {
  // Date.UTC treats years 0..99 as 1900..1999. Explicit full year keeps wall dates.
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  return date;
}

function daysInMonth(year, month) {
  return calendarDate(year, month + 1, 0).getUTCDate();
}

function dateText(year, month, day) {
  return `${padYear(year)}-${pad2(month)}-${pad2(day)}`;
}

function fmtDisplay(mode, value) {
  const text = String(value || '');
  if (mode === 'time') {
    const m = text.match(TIME);
    return m ? `${m[1]}:${m[2]}` : '';
  }
  const m = text.match(mode === 'date' ? /^(\d{4})-(\d{2})-(\d{2})$/ : DATETIME);
  return m ? `${m[1]}年${m[2]}月${m[3]}日${mode === 'date' ? '' : ` ${m[4]}:${m[5]}`}` : '';
}

function fieldName(host, label, mode) {
  // Nested window endpoints and inline filter/card fields have their own nearby
  // label. An outer section's first label can otherwise name several controls alike.
  const wrapper = host.closest('label, .window-field, .field, .kv');
  const nearbyLabel = wrapper?.tagName === 'LABEL' ? wrapper
    : [...(wrapper?.children || [])].find(child => child.matches('label, .k'));
  return label || host.getAttribute('aria-label')
    || nearbyLabel?.textContent.trim()
    || (mode === 'date' ? '日期' : mode === 'time' ? '时间' : '日期时间');
}

/** Idempotent mounting. Default Shanghai clock; local preserves native planning inputs. */
export function createRetroTimeField(host, {
  id, value = '', mode = 'datetime', align = 'left', clock = 'shanghai',
  disabled = false, readonly = false, label = '',
} = {}) {
  const previous = fields.get(host);
  if (previous && previous.input.id === id && host.contains(previous.input)) return previous.input;
  previous?.cleanup();
  if (activeRetroTimePop && host.contains(activeRetroTimePop._anchor)) closeRetroTimePop();
  host.classList.add('retro-time');
  host.innerHTML = `
    <input type="hidden" id="${esc(id)}" class="retro-time-value">
    <button type="button" class="retro-time-field" aria-haspopup="dialog" aria-expanded="false">
      <span class="retro-time-text"></span>${icon('clock')}
    </button>`;
  const input = host.querySelector('.retro-time-value');
  const button = host.querySelector('.retro-time-field');
  const textEl = host.querySelector('.retro-time-text');
  const name = fieldName(host, label, mode);
  const hints = [...(host.closest('.field')?.querySelectorAll('.field-hint') || [])];
  if (hints.length) {
    button.setAttribute('aria-describedby', hints.map(hint => {
      if (!hint.id) hint.id = `retro-time-hint-${++sequence}`;
      return hint.id;
    }).join(' '));
  }
  input.disabled = disabled;
  input.readOnly = readonly;
  const forwardedAttributes = new Map();
  const syncState = () => {
    button.disabled = input.disabled || input.readOnly;
    for (const attribute of ['aria-invalid', 'aria-describedby', 'aria-errormessage']) {
      const value = input.getAttribute(attribute);
      if (value !== null) {
        button.setAttribute(attribute, value);
        forwardedAttributes.set(attribute, value);
      } else if (forwardedAttributes.has(attribute)) {
        if (button.getAttribute(attribute) === forwardedAttributes.get(attribute)) button.removeAttribute(attribute);
        forwardedAttributes.delete(attribute);
      }
    }
  };
  const apply = (localValue, silent = false) => {
    if (activeRetroTimePop?._forInput === input) closeRetroTimePop();
    input.value = localValue || '';
    const shown = fmtDisplay(mode, input.value);
    textEl.textContent = shown;
    textEl.classList.toggle('is-empty', !shown);
    button.setAttribute('aria-label', `${name}：${shown || '未选择'}`);
    if (!silent) input.dispatchEvent(new Event('input', { bubbles: true }));
  };
  apply(value, true);
  syncState();
  input._applyRetroValue = apply;
  const toggle = () => {
    if (button.matches(':disabled') || input.matches(':disabled') || input.readOnly) return;
    if (activeRetroTimePop?._forInput === input) closeRetroTimePop(true);
    else openRetroTimePop(button, input, apply, mode, align, clock);
  };
  button.addEventListener('click', toggle);
  button.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (activeRetroTimePop?._forInput !== input) toggle();
    }
  });
  const observer = new MutationObserver(syncState);
  observer.observe(input, { attributes: true, attributeFilter: ['disabled', 'readonly', 'aria-invalid', 'aria-describedby', 'aria-errormessage'] });
  fields.set(host, { input, cleanup: () => observer.disconnect() });
  return input;
}

/** Time sublists commit only on click/Enter/Space, while arrow keys move focus. */
function createRtpUnit(host, values, initial, label, onChange, closeSiblings) {
  const listId = `retro-time-list-${++sequence}`;
  host.innerHTML = `
    <button type="button" class="rtp-select-btn" aria-haspopup="listbox" aria-expanded="false" aria-controls="${listId}">
      <span class="rtp-select-text"></span>${icon('chevron-down')}
    </button>
    <div class="rtp-select-pop" id="${listId}" role="listbox" aria-label="${label}" hidden></div>`;
  const button = host.querySelector('.rtp-select-btn');
  const textEl = host.querySelector('.rtp-select-text');
  const list = host.querySelector('.rtp-select-pop');
  let current = String(initial);
  let unregister = null;
  let search = '';
  let lastTyped = 0;
  const render = () => {
    textEl.textContent = current;
    button.setAttribute('aria-label', `${label}：${current}`);
    list.innerHTML = values.map(v => `<button type="button" role="option" tabindex="-1"
      aria-selected="${v === current}" class="rtp-select-option${v === current ? ' is-selected' : ''}"
      data-value="${v}">${v}</button>`).join('');
  };
  const close = (restoreFocus = false) => {
    unregister?.();
    unregister = null;
    list.hidden = true;
    host.classList.remove('is-open');
    button.setAttribute('aria-expanded', 'false');
    if (restoreFocus && button.isConnected) button.focus({ preventScroll: true });
  };
  const focus = option => {
    if (!option) return;
    option.focus({ preventScroll: true });
    // scrollIntoView also walks scrollable ancestors, including the short
    // calendar. Only move this list so its parent's viewport guard stays quiet.
    const bounds = list.getBoundingClientRect();
    const itemBounds = option.getBoundingClientRect();
    const visibleTop = bounds.top + list.clientTop;
    const visibleBottom = visibleTop + list.clientHeight;
    if (itemBounds.top < visibleTop) list.scrollTop -= visibleTop - itemBounds.top;
    else if (itemBounds.bottom > visibleBottom) list.scrollTop += itemBounds.bottom - visibleBottom;
  };
  const open = () => {
    closeSiblings(host);
    render();
    list.hidden = false;
    host.classList.add('is-open');
    button.setAttribute('aria-expanded', 'true');
    // Fixed descendants can escape a short calendar's own scrolling viewport.
    const rect = button.getBoundingClientRect();
    list.style.position = 'fixed';
    list.style.minWidth = `${rect.width}px`;
    list.style.maxHeight = `${Math.max(32, Math.min(145, window.innerHeight - 16))}px`;
    const size = list.getBoundingClientRect();
    const top = rect.bottom + size.height + 4 > window.innerHeight - 8 ? rect.top - size.height - 4 : rect.bottom + 4;
    list.style.top = `${Math.max(8, Math.min(top, window.innerHeight - size.height - 8))}px`;
    list.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - size.width - 8))}px`;
    unregister = registerFloatingSurface({ element: list, anchor: button, onEscape: ({ restoreFocus = true } = {}) => close(restoreFocus) });
    focus(list.querySelector('.is-selected') || list.querySelector('.rtp-select-option'));
    search = '';
  };
  button.addEventListener('click', () => list.hidden ? open() : close(true));
  button.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      event.stopPropagation();
      if (list.hidden) open();
    }
  });
  list.addEventListener('click', event => {
    const option = event.target.closest('.rtp-select-option');
    if (!option) return;
    current = option.dataset.value;
    close(true);
    render();
    onChange(current);
  });
  list.addEventListener('keydown', event => {
    if (event.key === 'Tab') { close(true); return; }
    const options = [...list.querySelectorAll('.rtp-select-option')];
    const index = options.indexOf(document.activeElement);
    let next;
    if (event.key === 'ArrowDown') next = options[Math.min(index + 1, options.length - 1)];
    else if (event.key === 'ArrowUp') next = options[Math.max(index - 1, 0)];
    else if (event.key === 'Home') next = options[0];
    else if (event.key === 'End') next = options.at(-1);
    else if (/^\d$/.test(event.key) && !event.ctrlKey && !event.altKey && !event.metaKey) {
      search = Date.now() - lastTyped > 700 ? event.key : (search + event.key).slice(-2);
      lastTyped = Date.now();
      next = options.find(option => option.dataset.value.startsWith(search));
    } else return;
    event.preventDefault();
    event.stopPropagation();
    focus(next);
  });
  render();
  return {
    host, close,
    containsFocus: () => !list.hidden && list.contains(document.activeElement),
    set(v) { current = String(v); render(); },
  };
}

export function openRetroTimePop(anchor, input, apply, mode = 'datetime', align = 'left', clock = 'shanghai') {
  if (anchor.matches(':disabled') || input.matches(':disabled') || input.readOnly) return;
  closeRetroTimePop();
  const current = String(input.value || '');
  const nowText = nowLocalInput(clock);
  const now = nowText.match(DATETIME);
  const showCalendar = mode === 'datetime' || mode === 'date';
  const showTimeRow = mode === 'datetime' || mode === 'time';
  const parsed = current.match(mode === 'date' ? /^(\d{4})-(\d{2})-(\d{2})$/ : mode === 'time' ? TIME : DATETIME);
  const state = {
    year: Number(mode !== 'time' && parsed ? parsed[1] : now[1]),
    month: Number(mode !== 'time' && parsed ? parsed[2] : now[2]),
    day: mode !== 'time' && parsed ? Number(parsed[3]) : null,
    hour: mode === 'time' && parsed ? parsed[1] : mode === 'datetime' && parsed ? parsed[4] : now[4],
    minute: mode === 'time' && parsed ? parsed[2] : mode === 'datetime' && parsed ? parsed[5] : now[5],
  };
  const view = { year: state.year, month: state.month, day: state.day || Number(now[3]) };
  view.day = Math.min(view.day, daysInMonth(view.year, view.month));
  const pop = document.createElement('div');
  pop.className = `retro-time-pop${mode === 'time' ? ' is-time-only' : ''}`;
  pop.id = `retro-time-pop-${++sequence}`;
  pop.setAttribute('role', 'dialog');
  pop.setAttribute('aria-label', anchor.getAttribute('aria-label') || '选择日期时间');
  pop._forInput = input;
  pop._anchor = anchor;
  pop.innerHTML = `
    ${showCalendar ? `<div class="retro-time-head">
      <div class="retro-time-nav">
        <button type="button" data-nav="year-" data-tooltip="上一年" aria-label="上一年">${icon('chevron-left')}${icon('chevron-left')}</button>
        <button type="button" data-nav="month-" data-tooltip="上一月" aria-label="上一月">${icon('chevron-left')}</button>
      </div>
      <span class="retro-time-title" id="${pop.id}-month" aria-live="polite"></span>
      <div class="retro-time-nav">
        <button type="button" data-nav="month+" data-tooltip="下一月" aria-label="下一月">${icon('chevron-right')}</button>
        <button type="button" data-nav="year+" data-tooltip="下一年" aria-label="下一年">${icon('chevron-right')}${icon('chevron-right')}</button>
      </div>
    </div>
    <div class="retro-time-week" aria-hidden="true"><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span><span>日</span></div>
    <div class="retro-time-grid" role="grid" aria-labelledby="${pop.id}-month" aria-describedby="${pop.id}-help"></div>
    <p class="retro-time-keyboard-hint" id="${pop.id}-help">方向键选日期，翻页键切月份，回车选中。</p>` : ''}
    ${showTimeRow ? `<div class="retro-time-time">
      <div class="rtp-unit" data-unit="hour"></div><span class="rtp-colon" aria-hidden="true">:</span>
      <div class="rtp-unit" data-unit="minute"></div>
    </div>` : ''}
    <div class="retro-time-foot"><button type="button" class="btn btn-quiet btn-sm" data-act="clear">清除</button>
      <span class="rtp-foot-right">
        ${mode === 'datetime' ? '<button type="button" class="btn btn-quiet btn-sm" data-act="now">此刻</button>' : ''}
        ${mode === 'date' ? '<button type="button" class="btn btn-quiet btn-sm" data-act="today">今天</button>' : ''}
        <button type="button" class="btn btn-primary btn-sm" data-act="ok">确定</button>
      </span>
    </div>`;
  document.body.appendChild(pop);
  activeRetroTimePop = pop;
  anchor.setAttribute('aria-expanded', 'true');
  anchor.setAttribute('aria-controls', pop.id);

  const units = [];
  const closeUnits = (except, restoreFocus = false) => {
    const focused = restoreFocus ? units.find(unit => unit.containsFocus()) : null;
    units.forEach(unit => { if (unit.host !== except) unit.close(unit === focused); });
  };
  if (showTimeRow) {
    units.push(createRtpUnit(pop.querySelector('[data-unit="hour"]'), Array.from({ length: 24 }, (_, h) => pad2(h)), state.hour, '小时', v => { state.hour = v; }, closeUnits));
    units.push(createRtpUnit(pop.querySelector('[data-unit="minute"]'), Array.from({ length: 60 }, (_, m) => pad2(m)), state.minute, '分钟', v => { state.minute = v; }, closeUnits));
  }
  const grid = pop.querySelector('.retro-time-grid');
  const renderGrid = (focus = false) => {
    if (!showCalendar) return;
    pop.querySelector('.retro-time-title').textContent = `${view.year}年${view.month}月`;
    grid.innerHTML = '';
    const lead = (calendarDate(view.year, view.month, 1).getUTCDay() + 6) % 7;
    const total = daysInMonth(view.year, view.month);
    let row;
    for (let slot = 0; slot < Math.ceil((lead + total) / 7) * 7; slot++) {
      if (slot % 7 === 0) {
        row = document.createElement('div');
        row.className = 'retro-time-row';
        row.setAttribute('role', 'row');
        grid.appendChild(row);
      }
      const day = slot - lead + 1;
      if (day < 1 || day > total) {
        const blank = document.createElement('span');
        blank.setAttribute('role', 'gridcell');
        row.appendChild(blank);
        continue;
      }
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = String(day);
      button.dataset.day = String(day);
      button.tabIndex = day === view.day ? 0 : -1;
      const selected = state.day === day && state.year === view.year && state.month === view.month;
      button.setAttribute('role', 'gridcell');
      button.setAttribute('aria-label', `${view.year}年${view.month}月${day}日`);
      button.setAttribute('aria-selected', String(selected));
      button.classList.toggle('is-selected', selected);
      const today = dateText(view.year, view.month, day) === nowText.slice(0, 10);
      button.classList.toggle('is-today', today);
      if (today) button.setAttribute('aria-current', 'date');
      button.addEventListener('click', () => {
        state.year = view.year; state.month = view.month; state.day = day; view.day = day;
        renderGrid(true);
      });
      row.appendChild(button);
    }
    if (focus) grid.querySelector(`[data-day="${view.day}"]`)?.focus({ preventScroll: true });
  };
  const moveMonth = amount => {
    const next = calendarDate(view.year, view.month + amount, 1);
    if (next.getUTCFullYear() < 1 || next.getUTCFullYear() > 9999) return;
    view.year = next.getUTCFullYear(); view.month = next.getUTCMonth() + 1;
    view.day = Math.min(view.day, daysInMonth(view.year, view.month));
  };
  if (showCalendar) {
    renderGrid();
    grid.addEventListener('keydown', event => {
      const day = Number(event.target.closest('[data-day]')?.dataset.day);
      if (!day) return;
      view.day = day;
      const weekday = (calendarDate(view.year, view.month, day).getUTCDay() + 6) % 7;
      let delta;
      if (event.key === 'ArrowLeft') delta = -1;
      else if (event.key === 'ArrowRight') delta = 1;
      else if (event.key === 'ArrowUp') delta = -7;
      else if (event.key === 'ArrowDown') delta = 7;
      else if (event.key === 'Home') delta = -weekday;
      else if (event.key === 'End') delta = 6 - weekday;
      else if (event.key === 'PageUp' || event.key === 'PageDown') moveMonth((event.key === 'PageUp' ? -1 : 1) * (event.shiftKey ? 12 : 1));
      else return;
      if (delta !== undefined) {
        const next = calendarDate(view.year, view.month, day + delta);
        if (next.getUTCFullYear() >= 1 && next.getUTCFullYear() <= 9999) {
          view.year = next.getUTCFullYear(); view.month = next.getUTCMonth() + 1; view.day = next.getUTCDate();
        }
      }
      event.preventDefault();
      event.stopPropagation();
      renderGrid(true);
    });
    pop.querySelectorAll('[data-nav]').forEach(button => button.addEventListener('click', () => {
      const step = button.dataset.nav;
      moveMonth((step.endsWith('-') ? -1 : 1) * (step.startsWith('year') ? 12 : 1));
      renderGrid();
    }));
  }
  const setNow = () => {
    const value = nowLocalInput(clock).match(DATETIME);
    state.year = Number(value[1]); state.month = Number(value[2]); state.day = Number(value[3]);
    state.hour = value[4]; state.minute = value[5];
    Object.assign(view, { year: state.year, month: state.month, day: state.day });
    renderGrid();
    units[0]?.set(state.hour); units[1]?.set(state.minute);
  };
  pop.querySelector('[data-act="now"]')?.addEventListener('click', setNow);
  pop.querySelector('[data-act="today"]')?.addEventListener('click', setNow);
  const commit = value => { closeRetroTimePop(true); apply(value); };
  pop.querySelector('[data-act="clear"]').addEventListener('click', () => commit(''));
  pop.querySelector('[data-act="ok"]').addEventListener('click', () => {
    if (mode === 'time') { commit(`${state.hour}:${state.minute}`); return; }
    if (!state.day) { commit(''); return; }
    const date = dateText(state.year, state.month, state.day);
    commit(mode === 'date' ? date : `${date}T${state.hour}:${state.minute}`);
  });
  // Date picker is a nested keyboard context. Tab cycles through its controls;
  // Escape returns first from a unit list, then the calendar, then the modal.
  pop.addEventListener('keydown', event => {
    if (event.key !== 'Tab') return;
    const buttons = [...pop.querySelectorAll('button')].filter(button => button.tabIndex >= 0 && !button.disabled && !button.closest('[hidden]'));
    const index = buttons.indexOf(document.activeElement);
    const next = buttons[(index + (event.shiftKey ? -1 : 1) + buttons.length) % buttons.length];
    event.preventDefault();
    next?.focus({ preventScroll: true });
  });

  anchor.scrollIntoView({ block: 'nearest' });
  const rect = anchor.getBoundingClientRect();
  const size = pop.getBoundingClientRect();
  const left = align === 'right' ? Math.max(rect.right - size.width, rect.left) : rect.left;
  const top = rect.bottom + size.height + 6 > window.innerHeight - 8 ? rect.top - size.height - 6 : rect.bottom + 6;
  pop.style.left = `${Math.max(8, Math.min(left, window.innerWidth - size.width - 8))}px`;
  pop.style.top = `${Math.max(8, Math.min(top, window.innerHeight - size.height - 8))}px`;
  const onOutside = event => {
    if (!pop.contains(event.target) && !anchor.contains(event.target)) closeRetroTimePop();
    else if (pop.contains(event.target)) closeUnits(event.target.closest('.rtp-unit'));
  };
  const openedAt = Date.now();
  const onViewportChange = event => {
    if (event.type === 'scroll' && pop.contains(event.target)) {
      if (!event.target.closest('.rtp-select-pop')) closeUnits(undefined, true);
      return;
    }
    if (event.type === 'scroll' && Date.now() - openedAt < 300) return;
    closeRetroTimePop(true);
  };
  const rootObserver = new MutationObserver(() => {
    if (!anchor.isConnected || !pop.isConnected || anchor.matches(':disabled') || input.matches(':disabled') || input.readOnly) closeRetroTimePop();
  });
  rootObserver.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['disabled', 'readonly'] });
  const unregister = registerFloatingSurface({ element: pop, anchor, onEscape: ({ restoreFocus = true } = {}) => closeRetroTimePop(restoreFocus) });
  document.addEventListener('pointerdown', onOutside, true);
  window.addEventListener('resize', onViewportChange);
  window.addEventListener('scroll', onViewportChange, true);
  pop._cleanup = () => {
    closeUnits();
    unregister();
    rootObserver.disconnect();
    document.removeEventListener('pointerdown', onOutside, true);
    window.removeEventListener('resize', onViewportChange);
    window.removeEventListener('scroll', onViewportChange, true);
    anchor.setAttribute('aria-expanded', 'false');
    anchor.removeAttribute('aria-controls');
  };
  (showCalendar ? grid.querySelector(`[data-day="${view.day}"]`) : pop.querySelector('.rtp-select-btn'))?.focus({ preventScroll: true });
  return pop;
}
