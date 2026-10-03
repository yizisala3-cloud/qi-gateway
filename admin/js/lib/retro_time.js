// 复古时间字段：date YYYY-MM-DD、time HH:MM、datetime YYYY-MM-DDTHH:MM。
// 已有 time HH:MM:SS 可读取；“此刻”始终采用 Asia/Shanghai 的墙上时钟。
import { icon } from '../ui.js?v=20261003-planning-create-latency1';
import {
  setupRetroField, attachRetroOverlay, positionRetroOverlay,
  moveRetroOptionFocus, retroSourceOptions, retroId, restoreRetroFieldFocus,
} from './retro_fields.js?v=20261003-planning-create-latency1';

let activeRetroTimePop = null;

export function closeRetroTimePop({ restoreFocus = false } = {}) {
  const pop = activeRetroTimePop;
  if (!pop) return;
  activeRetroTimePop = null;
  pop._cleanup?.();
  pop.remove();
  if (restoreFocus) restoreRetroFieldFocus(pop._anchor, pop._forInput);
}

export function destroyRetroTimeField(target) {
  (target?._retroField || target?.closest?.('.retro-time')?._retroField)?.destroy();
}

function pad2(number) { return String(number).padStart(2, '0'); }

/** 时分子列表使用独立焦点；第一层 Escape 收子列表，下一层才收日历。 */
function createRtpUnit(host, values, initial, label, onChange, closeOthers) {
  const id = retroId('rtp-unit');
  host.innerHTML = `
    <button type="button" class="rtp-select-btn" aria-haspopup="listbox"
      aria-expanded="false" aria-controls="${id}" aria-label="${label}">
      <span class="rtp-unit-label">${label}</span><span class="rtp-select-text"></span>
      ${icon('chevron-down')}
    </button>
    <div class="rtp-select-pop" id="${id}" role="listbox" aria-label="${label}" hidden></div>`;
  const button = host.querySelector('.rtp-select-btn');
  const text = host.querySelector('.rtp-select-text');
  const list = host.querySelector('.rtp-select-pop');
  let current = String(initial);
  const render = () => {
    text.textContent = current;
    list.innerHTML = values.map((value) => `
      <button type="button" role="option" tabindex="-1"
        aria-selected="${value === current}"
        class="rtp-select-option${value === current ? ' is-selected' : ''}"
        data-value="${value}">${value}</button>`).join('');
  };
  const close = ({ restoreFocus = false } = {}) => {
    list.hidden = true;
    host.classList.remove('is-open');
    button.setAttribute('aria-expanded', 'false');
    if (restoreFocus) button.focus({ preventScroll: true });
  };
  const open = (last = false) => {
    closeOthers();
    render();
    list.hidden = false;
    host.classList.add('is-open');
    button.setAttribute('aria-expanded', 'true');
    // A fixed child list escapes the calendar's short-viewport scroll clipping.
    // It stays a DOM child, so modal ownership and the Escape order stay intact.
    const rect = button.getBoundingClientRect();
    list.style.position = 'fixed';
    list.style.width = `${rect.width}px`;
    list.style.minWidth = `${rect.width}px`;
    list.style.maxHeight = `${Math.min(145, window.innerHeight - 16)}px`;
    list.style.bottom = '';
    list.style.zIndex = '2200';
    const size = list.getBoundingClientRect();
    const preferredTop = rect.bottom + 4 + size.height <= window.innerHeight - 8
      ? rect.bottom + 4 : rect.top - size.height - 4;
    list.style.top = `${Math.max(8, Math.min(preferredTop, window.innerHeight - size.height - 8))}px`;
    list.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - size.width - 8))}px`;
    const options = [...list.querySelectorAll('.rtp-select-option')];
    const selected = options.find((option) => option.dataset.value === current)
      || (last ? options.at(-1) : options[0]);
    if (selected) {
      selected.tabIndex = 0;
      selected.focus({ preventScroll: true });
      selected.scrollIntoView({ block: 'nearest' });
    }
  };
  button.addEventListener('click', () => list.hidden ? open() : close());
  button.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    event.preventDefault();
    open(event.key === 'ArrowUp');
  });
  list.addEventListener('click', (event) => {
    const option = event.target.closest('.rtp-select-option');
    if (!option) return;
    current = option.dataset.value;
    onChange(current);
    render();
    close({ restoreFocus: true });
  });
  list.addEventListener('keydown', (event) => {
    if (event.key === 'Tab') {
      close({ restoreFocus: true });
      return;
    }
    moveRetroOptionFocus(list, '.rtp-select-option', event);
  });
  render();
  return {
    host, button, close,
    get isOpen() { return !list.hidden; },
    set(value) { current = String(value); render(); },
  };
}

const SHANGHAI_OFFSET_MS = 8 * 3600 * 1000;
function nowShanghaiLocalInput() {
  const shifted = new Date(Date.now() + SHANGHAI_OFFSET_MS);
  return `${shifted.getUTCFullYear()}-${pad2(shifted.getUTCMonth() + 1)}-${pad2(shifted.getUTCDate())}T${pad2(shifted.getUTCHours())}:${pad2(shifted.getUTCMinutes())}`;
}

function fmtDisplay(mode, value) {
  const text = String(value || '');
  if (mode === 'date') {
    const match = text.match(/^(\d{4})-(\d{2})-(\d{2})$/);
    return match ? `${match[1]}年${match[2]}月${match[3]}日` : '';
  }
  if (mode === 'time') {
    const match = text.match(/^(\d{2}):(\d{2})(?::\d{2})?$/);
    return match ? `${match[1]}:${match[2]}` : '';
  }
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/);
  return match ? `${match[1]}年${match[2]}月${match[3]}日 ${match[4]}:${match[5]}` : '';
}

/** 返回原 id 的隐藏 input；非静默更新派发 input，保留 _applyRetroValue。 */
export function createRetroTimeField(host, { id, value = '', mode = 'datetime', align = 'left', ...config } = {}) {
  host._retroField?.destroy();
  host.classList.add('retro-time');
  host.innerHTML = `
    <input type="hidden" class="retro-time-value">
    <button type="button" class="retro-time-field" aria-haspopup="dialog">
      <span class="retro-time-text"></span>${icon('clock')}
    </button>`;
  const input = host.querySelector('.retro-time-value');
  const button = host.querySelector('.retro-time-field');
  const text = host.querySelector('.retro-time-text');
  const field = setupRetroField(host, input, button, {
    id, ...config, validationType: mode === 'datetime' ? 'datetime-local' : mode,
  });
  const apply = (localValue, silent = false) => {
    input.value = String(localValue || '');
    const shown = fmtDisplay(mode, input.value);
    text.textContent = shown;
    text.classList.toggle('is-empty', !shown);
    field.syncValue();
    if (!silent) input.dispatchEvent(new Event('input', { bubbles: true }));
  };
  input._applyRetroValue = apply;
  field.close = (settings) => {
    if (activeRetroTimePop?._forInput === input) closeRetroTimePop(settings);
  };
  const onClick = () => {
    if (input.disabled || button.disabled || button.matches(':disabled')) return;
    if (activeRetroTimePop?._forInput === input) field.close({ restoreFocus: true });
    else openRetroTimePop(button, input, apply, mode, align);
  };
  const onKey = (event) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    event.preventDefault();
    if (!input.disabled && !button.disabled) openRetroTimePop(button, input, apply, mode, align);
  };
  button.addEventListener('click', onClick);
  button.addEventListener('keydown', onKey);
  field.onDestroy = () => {
    button.removeEventListener('click', onClick);
    button.removeEventListener('keydown', onKey);
  };
  apply(value, true);
  field.initialValue = input.value;
  input.defaultValue = input.value;
  return input;
}

/** 同时覆盖原生日期/时间字段与既有 data-retro 宿主；重复初始化不重绑。 */
export function initRetroTimeFields(root) {
  const selector = 'input[type="date"], input[type="time"], input[type="datetime-local"]';
  const sources = [...root.querySelectorAll(selector)].filter((source) => !source.classList.contains('retro-validation-input'));
  if (root.matches?.(selector) && !root.classList.contains('retro-validation-input')) sources.unshift(root);
  const inputs = sources.map((source) => {
    const config = retroSourceOptions(source);
    const host = document.createElement('span');
    host.className = `${source.className} retro-time`.trim();
    host.style.cssText = source.style.cssText;
    const mode = source.type === 'datetime-local' ? 'datetime' : source.type;
    source.replaceWith(host);
    return createRetroTimeField(host, { ...config, mode });
  });
  const hosts = [...root.querySelectorAll('.retro-time[data-retro-for]')];
  if (root.matches?.('.retro-time[data-retro-for]')) hosts.unshift(root);
  for (const host of hosts) {
    if (host._retroField) continue;
    inputs.push(createRetroTimeField(host, {
      id: host.dataset.retroFor, value: host.dataset.retroValue || '',
      mode: host.dataset.retroMode || 'datetime', align: host.dataset.retroAlign || 'left',
      required: host.dataset.retroRequired === 'true' || host.hasAttribute('required'),
      disabled: host.dataset.retroDisabled === 'true' || host.hasAttribute('disabled'),
      label: host.dataset.retroLabel, name: host.dataset.retroName,
      min: host.dataset.retroMin, max: host.dataset.retroMax, step: host.dataset.retroStep,
    }));
  }
  return inputs;
}

export function openRetroTimePop(anchor, input, apply, mode = 'datetime', align = 'left') {
  if (anchor.disabled || input.disabled || anchor.matches(':disabled')) return null;
  closeRetroTimePop();
  const current = String(input.value || '');
  const nowText = nowShanghaiLocalInput();
  const now = nowText.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/);
  const calendar = mode === 'datetime' || mode === 'date';
  const timeRow = mode === 'datetime' || mode === 'time';
  const dateMatch = current.match(/^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?$/);
  const timeMatch = current.match(/^(\d{2}):(\d{2})(?::\d{2})?$/);
  const state = {
    year: Number(dateMatch?.[1] || now[1]), month: Number(dateMatch?.[2] || now[2]),
    day: dateMatch ? Number(dateMatch[3]) : null,
    hour: mode === 'time' ? timeMatch?.[1] || now[4] : dateMatch?.[4] || now[4],
    minute: mode === 'time' ? timeMatch?.[2] || now[5] : dateMatch?.[5] || now[5],
  };
  let focusDay = state.day || (state.year === Number(now[1]) && state.month === Number(now[2]) ? Number(now[3]) : 1);
  const pop = document.createElement('div');
  pop.className = `retro-time-pop${mode === 'time' ? ' is-time-only' : ''}`;
  pop.setAttribute('role', 'dialog');
  pop.setAttribute('aria-label', `${input._retroField?.label || anchor.getAttribute('aria-label') || '时间'}选择器`);
  pop._forInput = input;
  pop._anchor = anchor;
  pop.innerHTML = `
    ${calendar ? `
    <div class="retro-time-head">
      <div class="retro-time-nav">
        <button type="button" data-nav="year-" aria-label="上一年" data-tooltip="上一年">${icon('chevron-left')}${icon('chevron-left')}</button>
        <button type="button" data-nav="month-" aria-label="上一月" data-tooltip="上一月">${icon('chevron-left')}</button>
      </div>
      <span class="retro-time-title" aria-live="polite"></span>
      <div class="retro-time-nav">
        <button type="button" data-nav="month+" aria-label="下一月" data-tooltip="下一月">${icon('chevron-right')}</button>
        <button type="button" data-nav="year+" aria-label="下一年" data-tooltip="下一年">${icon('chevron-right')}${icon('chevron-right')}</button>
      </div>
    </div>
    <div class="retro-time-week" aria-hidden="true">
      <span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span><span>日</span>
    </div>
    <div class="retro-time-grid" role="group" aria-label="日期"></div>` : ''}
    ${timeRow ? `
    <div class="retro-time-time">
      <div class="rtp-unit" data-unit="hour"></div><span class="rtp-colon" aria-hidden="true">:</span>
      <div class="rtp-unit" data-unit="minute"></div>
    </div>` : ''}
    <div class="retro-time-foot">
      <button type="button" class="btn btn-quiet btn-sm" data-act="clear">清除</button>
      <span class="rtp-foot-right">
        ${mode === 'datetime' ? '<button type="button" class="btn btn-quiet btn-sm" data-act="now">此刻</button>' : ''}
        ${mode === 'date' ? '<button type="button" class="btn btn-quiet btn-sm" data-act="today">今天</button>' : ''}
        <button type="button" class="btn btn-primary btn-sm" data-act="ok">确定</button>
      </span>
    </div>`;
  document.body.appendChild(pop);
  pop._cleanup = attachRetroOverlay(pop, anchor, closeRetroTimePop);
  activeRetroTimePop = pop;
  const units = [];
  let hourPick;
  let minutePick;
  const closeUnits = () => units.forEach((unit) => unit.close());
  if (timeRow) {
    hourPick = createRtpUnit(pop.querySelector('[data-unit="hour"]'),
      Array.from({ length: 24 }, (_, hour) => pad2(hour)), state.hour, '小时',
      (value) => { state.hour = value; }, closeUnits);
    units.push(hourPick);
    minutePick = createRtpUnit(pop.querySelector('[data-unit="minute"]'),
      Array.from({ length: 60 }, (_, minute) => pad2(minute)), state.minute, '分钟',
      (value) => { state.minute = value; }, closeUnits);
    units.push(minutePick);
    pop.addEventListener('mousedown', (event) => {
      for (const unit of units) if (!unit.host.contains(event.target)) unit.close();
    });
  }
  pop._closeSubpanel = () => {
    const unit = units.find((item) => item.isOpen);
    if (!unit) return false;
    unit.close({ restoreFocus: true });
    return true;
  };

  const grid = pop.querySelector('.retro-time-grid');
  const title = pop.querySelector('.retro-time-title');
  const renderGrid = () => {
    if (!calendar) return;
    title.textContent = `${state.year}年${state.month}月`;
    grid.setAttribute('aria-label', `${state.year}年${state.month}月日期`);
    grid.innerHTML = '';
    const first = new Date(Date.UTC(state.year, state.month - 1, 1));
    const leading = (first.getUTCDay() + 6) % 7;
    for (let i = 0; i < leading; i++) grid.insertAdjacentHTML('beforeend', '<span aria-hidden="true"></span>');
    const days = new Date(Date.UTC(state.year, state.month, 0)).getUTCDate();
    focusDay = Math.min(Math.max(focusDay, 1), days);
    for (let day = 1; day <= days; day++) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = String(day);
      button.dataset.day = String(day);
      button.tabIndex = day === focusDay ? 0 : -1;
      button.setAttribute('aria-label', `${state.year}年${state.month}月${day}日`);
      button.setAttribute('aria-pressed', String(state.day === day));
      if (state.day === day) button.classList.add('is-selected');
      if (`${state.year}-${pad2(state.month)}-${pad2(day)}` === nowText.slice(0, 10)) {
        button.classList.add('is-today');
        button.setAttribute('aria-current', 'date');
      }
      button.addEventListener('click', () => {
        state.day = day;
        focusDay = day;
        for (const option of grid.querySelectorAll('button')) {
          const selected = Number(option.dataset.day) === day;
          option.classList.toggle('is-selected', selected);
          option.setAttribute('aria-pressed', String(selected));
          option.tabIndex = selected ? 0 : -1;
        }
      });
      grid.appendChild(button);
    }
  };
  if (calendar) {
    renderGrid();
    grid.addEventListener('keydown', (event) => {
      const button = event.target.closest('button[data-day]');
      if (!button) return;
      let date = new Date(Date.UTC(state.year, state.month - 1, Number(button.dataset.day)));
      const day = date.getUTCDate();
      if (event.key === 'ArrowLeft') date.setUTCDate(day - 1);
      else if (event.key === 'ArrowRight') date.setUTCDate(day + 1);
      else if (event.key === 'ArrowUp') date.setUTCDate(day - 7);
      else if (event.key === 'ArrowDown') date.setUTCDate(day + 7);
      else if (event.key === 'Home') date.setUTCDate(day - (date.getUTCDay() + 6) % 7);
      else if (event.key === 'End') date.setUTCDate(day + 6 - (date.getUTCDay() + 6) % 7);
      else if (event.key === 'PageUp' || event.key === 'PageDown') {
        const shift = event.key === 'PageUp' ? -1 : 1;
        const target = new Date(Date.UTC(state.year + (event.shiftKey ? shift : 0),
          state.month - 1 + (event.shiftKey ? 0 : shift), 1));
        const last = new Date(Date.UTC(target.getUTCFullYear(), target.getUTCMonth() + 1, 0)).getUTCDate();
        target.setUTCDate(Math.min(day, last));
        date = target;
      } else return;
      event.preventDefault();
      if (date.getUTCFullYear() !== state.year || date.getUTCMonth() + 1 !== state.month) state.day = null;
      state.year = date.getUTCFullYear();
      state.month = date.getUTCMonth() + 1;
      focusDay = date.getUTCDate();
      renderGrid();
      grid.querySelector(`[data-day="${focusDay}"]`).focus({ preventScroll: true });
    });
    pop.querySelectorAll('[data-nav]').forEach((button) => {
      button.addEventListener('click', () => {
        const delta = button.dataset.nav.endsWith('+') ? 1 : -1;
        const date = new Date(Date.UTC(state.year + (button.dataset.nav.startsWith('year') ? delta : 0),
          state.month - 1 + (button.dataset.nav.startsWith('month') ? delta : 0), 1));
        state.year = date.getUTCFullYear();
        state.month = date.getUTCMonth() + 1;
        state.day = null;
        renderGrid();
      });
    });
  }
  pop.querySelector('[data-act="clear"]').addEventListener('click', () => {
    apply('');
    closeRetroTimePop({ restoreFocus: true });
  });
  if (mode === 'datetime' || mode === 'date') {
    pop.querySelector(mode === 'datetime' ? '[data-act="now"]' : '[data-act="today"]')
      .addEventListener('click', () => {
        const fresh = nowShanghaiLocalInput().match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/);
        state.year = Number(fresh[1]); state.month = Number(fresh[2]); state.day = Number(fresh[3]);
        focusDay = state.day;
        renderGrid();
        if (timeRow) {
          state.hour = fresh[4]; state.minute = fresh[5];
          hourPick.set(state.hour); minutePick.set(state.minute);
        }
      });
  }
  pop.querySelector('[data-act="ok"]').addEventListener('click', () => {
    const date = `${state.year}-${pad2(state.month)}-${pad2(state.day)}`;
    const value = mode === 'time' ? `${state.hour}:${state.minute}`
      : !state.day ? '' : mode === 'date' ? date : `${date}T${state.hour}:${state.minute}`;
    apply(value);
    if (input._retroField && !input._retroField.validate()) return;
    closeRetroTimePop({ restoreFocus: true });
  });
  pop.addEventListener('keydown', (event) => {
    if (event.key !== 'Tab') return;
    const tabbable = [...pop.querySelectorAll('button')].filter((button) =>
      !button.disabled && button.tabIndex >= 0 && !button.closest('[hidden]'));
    if ((!event.shiftKey && document.activeElement === tabbable.at(-1))
      || (event.shiftKey && document.activeElement === tabbable[0])) closeRetroTimePop({ restoreFocus: true });
  });
  pop.addEventListener('scroll', (event) => { if (event.target === pop) closeUnits(); });
  positionRetroOverlay(pop, anchor, align);
  const initialFocus = calendar ? grid.querySelector(`[data-day="${focusDay}"]`) : hourPick.button;
  initialFocus?.focus({ preventScroll: true });
  return pop;
}
