// 复古单选字段：隐藏值保持原 id/name，纸色按钮与金线列表承担展示。
import { esc, icon } from '../ui.js?v=20261003-planning-create-latency1';
import {
  setupRetroField, attachRetroOverlay, positionRetroOverlay,
  moveRetroOptionFocus, retroSourceOptions, restoreRetroFieldFocus,
} from './retro_fields.js?v=20261003-planning-create-latency1';

let activeRetroSelectPop = null;

export function closeRetroSelectPop({ restoreFocus = false } = {}) {
  const pop = activeRetroSelectPop;
  if (!pop) return;
  activeRetroSelectPop = null;
  pop._cleanup?.();
  pop.remove();
  if (restoreFocus) restoreRetroFieldFocus(pop._anchor, pop._forInput);
}

export function destroyRetroSelectField(target) {
  (target?._retroField || target?.closest?.('.retro-select')?._retroField)?.destroy();
}

/** 返回原 id 的隐藏 input；支持 disabled/required/name 与可访问标签。 */
export function createRetroSelectField(host, { id, value = '', options = [], ...config } = {}) {
  host._retroField?.destroy();
  host.classList.add('retro-select');
  host.innerHTML = `
    <input type="hidden" class="retro-select-value">
    <button type="button" class="retro-select-field" aria-haspopup="listbox">
      <span class="retro-select-text"></span>${icon('chevron-down')}
    </button>`;
  const input = host.querySelector('.retro-select-value');
  const button = host.querySelector('.retro-select-field');
  const text = host.querySelector('.retro-select-text');
  const field = setupRetroField(host, input, button, { id, ...config });
  const apply = (localValue, silent = false) => {
    input.value = String(localValue ?? '');
    const selected = options.find((option) => String(option.value) === input.value);
    text.textContent = selected?.label ?? '';
    text.classList.toggle('is-empty', !text.textContent);
    field.syncValue();
    if (activeRetroSelectPop?._forInput === input) {
      for (const option of activeRetroSelectPop.querySelectorAll('.retro-select-option')) {
        const chosen = option.dataset.value === input.value;
        option.classList.toggle('is-selected', chosen);
        option.setAttribute('aria-selected', String(chosen));
      }
    }
    if (!silent) input.dispatchEvent(new Event('change', { bubbles: true }));
  };
  input._applyRetroValue = apply;
  field.close = (settings) => {
    if (activeRetroSelectPop?._forInput === input) closeRetroSelectPop(settings);
  };
  const open = (last = false) => {
    if (input.disabled || button.disabled || button.matches(':disabled')) return;
    closeRetroSelectPop();
    openRetroSelectPop(button, input, apply, options, last);
  };
  const onClick = () => {
    if (activeRetroSelectPop?._forInput === input) field.close({ restoreFocus: true });
    else open();
  };
  const onKey = (event) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      open(event.key === 'ArrowUp');
    }
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

/** 将单选 select 原位接入共享字段；必须在业务监听绑定前调用。 */
export function initRetroSelectFields(root) {
  const sources = [...root.querySelectorAll('select:not([multiple])')];
  if (root.matches?.('select:not([multiple])')) sources.unshift(root);
  return sources.map((source) => {
    const config = retroSourceOptions(source);
    const options = [...source.options].map((option) => ({
      value: option.value, label: option.textContent.trim(),
      disabled: option.disabled || option.parentElement?.disabled,
    }));
    const host = document.createElement('span');
    host.className = `${source.className} retro-select`.trim();
    host.style.cssText = source.style.cssText;
    source.replaceWith(host);
    return createRetroSelectField(host, { ...config, options });
  });
}

export function openRetroSelectPop(anchor, input, apply, options, preferLast = false) {
  if (anchor.disabled || input.disabled || anchor.matches(':disabled')) return null;
  closeRetroSelectPop();
  const current = String(input.value ?? '');
  const pop = document.createElement('div');
  pop.className = 'retro-select-pop';
  pop.setAttribute('role', 'listbox');
  pop.setAttribute('aria-label', input._retroField?.label || anchor.getAttribute('aria-label') || '选项');
  pop._forInput = input;
  pop._anchor = anchor;
  pop.innerHTML = options.map((option) => `
    <button type="button" role="option" tabindex="-1"
      aria-selected="${String(option.value) === current}" aria-disabled="${Boolean(option.disabled)}"
      class="retro-select-option${String(option.value) === current ? ' is-selected' : ''}"
      data-value="${esc(option.value)}" ${option.disabled ? 'disabled' : ''}>${esc(option.label)}</button>`).join('');
  document.body.appendChild(pop);
  pop._cleanup = attachRetroOverlay(pop, anchor, closeRetroSelectPop);
  activeRetroSelectPop = pop;
  pop.addEventListener('click', (event) => {
    const option = event.target.closest('.retro-select-option');
    if (!option || option.disabled || input.disabled || anchor.disabled) return;
    apply(option.dataset.value);
    closeRetroSelectPop({ restoreFocus: true });
  });
  pop.addEventListener('keydown', (event) => {
    if (event.key === 'Tab') {
      closeRetroSelectPop({ restoreFocus: true });
      return;
    }
    moveRetroOptionFocus(pop, '.retro-select-option', event);
  });
  pop.style.minWidth = `${anchor.getBoundingClientRect().width}px`;
  positionRetroOverlay(pop, anchor);
  const enabled = [...pop.querySelectorAll('.retro-select-option')].filter((option) => !option.disabled);
  const selected = enabled.find((option) => option.dataset.value === current)
    || (preferLast ? enabled.at(-1) : enabled[0]);
  if (selected) {
    selected.tabIndex = 0;
    selected.focus({ preventScroll: true });
    selected.scrollIntoView({ block: 'nearest' });
  } else {
    pop.tabIndex = -1;
    pop.focus({ preventScroll: true });
  }
  return pop;
}
