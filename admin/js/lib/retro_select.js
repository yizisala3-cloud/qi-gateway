// 共享纸面下拉：手动字段与页面原有单选 select 共用同一浮层。
// 原 select 保留 id/name/options/value、校验及业务监听；展示按钮不提交表单。
import { esc, icon, registerFloatingSurface } from '../ui.js?v=20261015-planning-responsive-notes';

let activeRetroSelectPop = null;
let popSequence = 0;
const initializers = new WeakMap();

function closeRetroSelectPop(restoreFocus = false) {
  const pop = activeRetroSelectPop;
  if (!pop) return;
  activeRetroSelectPop = null;
  pop._cleanup();
  pop.remove();
  if (restoreFocus && pop._anchor.isConnected && !pop._anchor.disabled) {
    pop._anchor.focus({ preventScroll: true });
  }
}

function fieldName(input, host) {
  const labels = [...(input.labels || [])].map(label => {
    const copy = label.cloneNode(true);
    copy.querySelectorAll('select, input, textarea, button, .retro-select, .retro-time').forEach(control => control.remove());
    return copy.textContent.trim();
  }).filter(Boolean);
  return input.getAttribute('aria-label') || labels.join('、') || input.dataset.tooltip || input.title
    || host.closest('.field')?.querySelector('label')?.textContent.trim()
    || host.closest('label')?.childNodes[0]?.textContent.trim() || '选择';
}

function fieldButton(host) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'retro-select-field';
  button.setAttribute('aria-haspopup', 'listbox');
  button.setAttribute('aria-expanded', 'false');
  button.innerHTML = `<span class="retro-select-text"></span>${icon('chevron-down')}`;
  host.appendChild(button);
  return button;
}

function bindTrigger(anchor, input, apply, getOptions) {
  const open = () => {
    if (anchor.disabled || input.disabled) return;
    if (activeRetroSelectPop?._forInput === input) closeRetroSelectPop(true);
    else openRetroSelectPop(anchor, input, apply, getOptions());
  };
  const onKey = event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      open();
    }
  };
  anchor.addEventListener('click', open);
  anchor.addEventListener('keydown', onKey);
  return () => {
    anchor.removeEventListener('click', open);
    anchor.removeEventListener('keydown', onKey);
  };
}

/** 手动挂载，保留既有隐藏 input / _applyRetroValue(value, silent) 契约。 */
export function createRetroSelectField(host, { id, value = '', options = [] } = {}) {
  if (activeRetroSelectPop && host.contains(activeRetroSelectPop._anchor)) closeRetroSelectPop();
  host.classList.add('retro-select');
  host.innerHTML = `<input type="hidden" id="${esc(id)}" class="retro-select-value">`;
  const input = host.querySelector('input');
  const button = fieldButton(host);
  const text = button.querySelector('.retro-select-text');
  const name = fieldName(input, host);
  const apply = (localValue, silent) => {
    input.value = localValue;
    text.textContent = options.find(option => String(option.value) === String(localValue))?.label || '';
    button.setAttribute('aria-label', `${name}：${text.textContent}`);
    if (!silent) input.dispatchEvent(new Event('change', { bubbles: true }));
  };
  apply(value, true);
  input._applyRetroValue = apply;
  bindTrigger(button, input, apply, () => options);
  return input;
}

/** 单一活动浮层；方向键只移动焦点，Enter/Space 才提交，Escape 取消。 */
export function openRetroSelectPop(anchor, input, apply, options) {
  closeRetroSelectPop();
  const pop = document.createElement('div');
  pop.className = 'retro-select-pop';
  pop.id = `retro-select-list-${++popSequence}`;
  pop.setAttribute('role', 'listbox');
  pop.setAttribute('aria-label', anchor.getAttribute('aria-label') || '选择');
  pop._forInput = input;
  pop._anchor = anchor;
  pop.innerHTML = options.map((option, index) => {
    const selected = option.selected ?? (String(option.value) === String(input.value ?? ''));
    return `<button type="button" role="option" tabindex="-1"
      aria-selected="${selected}" aria-disabled="${!!option.disabled}"
      class="retro-select-option${selected ? ' is-selected' : ''}"
      data-index="${index}" data-value="${esc(option.value)}" ${option.disabled ? 'disabled' : ''}>
      <span>${esc(option.label)}</span><span class="retro-select-check" aria-hidden="true">${icon('check')}</span>
    </button>`;
  }).join('');
  // 时间面板子列表留在父面板内，父级外点处理不会提前关闭日历。
  (anchor.closest('.retro-time-pop') || document.body).appendChild(pop);
  activeRetroSelectPop = pop;
  anchor.closest('.retro-select')?.classList.add('is-open');
  anchor.setAttribute('aria-expanded', 'true');
  anchor.setAttribute('aria-controls', pop.id);
  const buttons = [...pop.querySelectorAll('.retro-select-option:not(:disabled)')];
  const focus = button => {
    button?.focus({ preventScroll: true });
    button?.scrollIntoView({ block: 'nearest' });
  };
  buttons.forEach(button => button.addEventListener('click', () => {
    // 先回收再派发业务事件：事件可同步重绘字段或打开另一个面板。
    closeRetroSelectPop(true);
    apply(button.dataset.value, false, Number(button.dataset.index));
  }));
  let search = '';
  let lastTyped = 0;
  pop.addEventListener('keydown', event => {
    const index = buttons.indexOf(document.activeElement);
    let next;
    if (event.key === 'ArrowDown') next = buttons[Math.min(index + 1, buttons.length - 1)];
    else if (event.key === 'ArrowUp') next = buttons[Math.max(index - 1, 0)];
    else if (event.key === 'Home') next = buttons[0];
    else if (event.key === 'End') next = buttons.at(-1);
    else if (event.key.length === 1 && event.key !== ' ' && !event.ctrlKey && !event.metaKey && !event.altKey) {
      const typed = event.key.toLocaleLowerCase();
      search = Date.now() - lastTyped > 700 ? typed : search + typed;
      lastTyped = Date.now();
      if ([...search].every(char => char === typed)) search = typed;
      next = [...buttons.slice(index + 1), ...buttons.slice(0, index + 1)]
        .find(button => button.textContent.trim().toLocaleLowerCase().startsWith(search));
    } else return;
    event.preventDefault();
    event.stopPropagation();
    focus(next);
  });
  anchor.scrollIntoView({ block: 'nearest' });
  const rect = anchor.getBoundingClientRect();
  pop.style.minWidth = `${Math.min(Math.max(rect.width, 148), innerWidth - 16)}px`;
  pop.style.maxWidth = `${innerWidth - 16}px`;
  const size = pop.getBoundingClientRect();
  const top = rect.bottom + size.height + 6 > innerHeight - 8 ? rect.top - size.height - 6 : rect.bottom + 6;
  pop.style.left = `${Math.max(8, Math.min(rect.left, innerWidth - size.width - 8))}px`;
  pop.style.top = `${Math.max(8, Math.min(top, innerHeight - size.height - 8))}px`;
  const unregisterSurface = registerFloatingSurface({
    element: pop, anchor, onEscape: () => closeRetroSelectPop(true),
  });
  focus(pop.querySelector('.is-selected:not(:disabled)') || buttons[0]);

  const onOutside = event => {
    if (!pop.contains(event.target) && !anchor.contains(event.target)) closeRetroSelectPop();
  };
  const onKey = event => {
    if (event.key === 'Tab') {
      // 从触发按钮继续正常 Tab 顺序，而不是从 body 尾部的浮层跳走。
      closeRetroSelectPop(true);
    }
  };
  const openedAt = Date.now();
  const onViewportChange = event => {
    if (event.type === 'scroll' && (pop.contains(event.target) || Date.now() - openedAt < 300)) return;
    closeRetroSelectPop();
  };
  const rootObserver = new MutationObserver(() => {
    if (!anchor.isConnected || !pop.isConnected || anchor.disabled || input.disabled) closeRetroSelectPop();
  });
  rootObserver.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['disabled'] });
  document.addEventListener('pointerdown', onOutside, true);
  document.addEventListener('keydown', onKey, true);
  window.addEventListener('resize', onViewportChange);
  window.addEventListener('scroll', onViewportChange, true);
  pop._cleanup = () => {
    unregisterSurface();
    rootObserver.disconnect();
    document.removeEventListener('pointerdown', onOutside, true);
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('resize', onViewportChange);
    window.removeEventListener('scroll', onViewportChange, true);
    anchor.closest('.retro-select')?.classList.remove('is-open');
    anchor.setAttribute('aria-expanded', 'false');
    anchor.removeAttribute('aria-controls');
  };
  return pop;
}

/** app 启动调用一次；观察动态页面/表单。返回停止观察并还原字段的函数。 */
export function initRetroSelects(root = document.body) {
  if (initializers.has(root)) return initializers.get(root);
  const fields = new Map();
  const isSingle = select => !select.multiple && select.size <= 1;
  const enhance = select => {
    if (fields.has(select) || !isSingle(select)) return;
    const host = document.createElement('span');
    host.className = 'retro-select retro-select-native';
    select.before(host);
    host.appendChild(select);
    const previous = { tabindex: select.getAttribute('tabindex'), ariaHidden: select.getAttribute('aria-hidden') };
    select.classList.add('retro-select-source');
    select.setAttribute('tabindex', '-1');
    select.setAttribute('aria-hidden', 'true');
    const button = fieldButton(host);
    if (select.id) button.dataset.selectFor = select.id;
    const text = button.querySelector('.retro-select-text');
    const sync = () => {
      const label = select.selectedOptions[0]?.label || '';
      if (text.textContent !== label) text.textContent = label;
      button.disabled = select.matches(':disabled');
      button.setAttribute('aria-label', `${fieldName(select, host)}${select.required ? '（必填）' : ''}：${label}`);
      if (select.hasAttribute('aria-labelledby')) button.setAttribute('aria-labelledby', select.getAttribute('aria-labelledby'));
      else button.removeAttribute('aria-labelledby');
      for (const attribute of ['aria-describedby', 'aria-invalid']) {
        if (select.hasAttribute(attribute)) button.setAttribute(attribute, select.getAttribute(attribute));
        else button.removeAttribute(attribute);
      }
      if (activeRetroSelectPop?._forInput === select) closeRetroSelectPop();
    };
    const apply = (value, silent, index) => {
      select.selectedIndex = index;
      sync();
      if (!silent) {
        select.dispatchEvent(new Event('input', { bubbles: true }));
        select.dispatchEvent(new Event('change', { bubbles: true }));
      }
    };
    const unbind = bindTrigger(button, select, apply, () => [...select.options].map(option => ({
      value: option.value, label: option.label, selected: option.selected,
      disabled: option.disabled || (option.parentElement.tagName === 'OPTGROUP' && option.parentElement.disabled),
    })));
    // 仅代理此字段，不修改 DOM 全局原型。程序赋值无需另派发 change。
    const descriptors = new Map();
    for (const property of ['value', 'selectedIndex']) {
      const original = Object.getOwnPropertyDescriptor(select, property);
      const native = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, property);
      if (original) continue;
      descriptors.set(property, original);
      Object.defineProperty(select, property, {
        configurable: true, get() { return native.get.call(this); },
        set(value) { native.set.call(this, value); sync(); },
      });
    }
    const onFocus = () => button.focus({ preventScroll: true });
    const onNativeClick = event => { event.preventDefault(); button.click(); };
    select.addEventListener('input', sync);
    select.addEventListener('change', sync);
    select.addEventListener('focus', onFocus);
    select.addEventListener('click', onNativeClick);
    const observer = new MutationObserver(sync);
    observer.observe(select, { childList: true, subtree: true, characterData: true, attributes: true,
      attributeFilter: ['disabled', 'required', 'selected', 'label', 'value', 'title', 'data-tooltip', 'aria-label', 'aria-labelledby', 'aria-describedby', 'aria-invalid'] });
    const dispose = () => {
      if (activeRetroSelectPop?._forInput === select) closeRetroSelectPop();
      observer.disconnect();
      unbind();
      select.removeEventListener('input', sync);
      select.removeEventListener('change', sync);
      select.removeEventListener('focus', onFocus);
      select.removeEventListener('click', onNativeClick);
      for (const property of descriptors.keys()) delete select[property];
      select.classList.remove('retro-select-source');
      for (const [attribute, value] of [['tabindex', previous.tabindex], ['aria-hidden', previous.ariaHidden]]) {
        if (value === null) select.removeAttribute(attribute); else select.setAttribute(attribute, value);
      }
      if (select.parentNode === host) host.replaceWith(select);
      else host.remove();
      fields.delete(select);
    };
    fields.set(select, { host, sync, dispose });
    sync();
  };
  const scan = node => {
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    if (node.matches('select')) enhance(node);
    node.querySelectorAll('select').forEach(enhance);
  };
  scan(root);
  const observer = new MutationObserver(records => {
    for (const [select, field] of fields) {
      if (!root.contains(select) || !root.contains(field.host) || select.parentNode !== field.host || !isSingle(select)) field.dispose();
    }
    for (const record of records) {
      if (record.type === 'childList') record.addedNodes.forEach(node => { if (root.contains(node)) scan(node); });
      else {
        if (record.target.matches('select') && isSingle(record.target)) enhance(record.target);
        for (const [select, field] of fields) if (record.target.contains(select)) field.sync();
      }
    }
  });
  observer.observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ['disabled', 'multiple', 'size'] });
  const onReset = event => setTimeout(() => {
    for (const [select, field] of fields) if (select.form === event.target) field.sync();
  }, 0);
  root.addEventListener('reset', onReset, true);
  const dispose = () => {
    observer.disconnect();
    root.removeEventListener('reset', onReset, true);
    for (const field of [...fields.values()]) field.dispose();
    initializers.delete(root);
  };
  initializers.set(root, dispose);
  return dispose;
}
