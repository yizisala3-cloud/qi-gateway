// Shared field semantics and lifecycle. Values stay on the original-id hidden input;
// an unnamed validation input preserves native constraints without a system picker.
let serial = 0;
let fieldObserver = null;
const fields = new Set();
let activeOverlay = null;

export const retroId = (prefix = 'retro') => `${prefix}-${++serial}`;

export function restoreRetroFieldFocus(anchor, input) {
  // A selection can synchronously redraw its own field (for example thread
  // status changes). Restore to the replacement with the same business id.
  const target = anchor?.isConnected ? anchor : document.getElementById(input?.id)?._retroField?.button;
  if (target?.isConnected && !target.disabled && !target.matches(':disabled') && target.getClientRects().length) {
    target.focus({ preventScroll: true });
  }
}

export function retroFieldLabel(host, id, supplied = '') {
  if (supplied) return supplied;
  if (host.getAttribute('aria-label')) return host.getAttribute('aria-label');
  const explicit = [...document.querySelectorAll('label[for]')].find((label) => label.htmlFor === id || label.htmlFor === `${id}-button`);
  if (explicit) return explicit.textContent.trim();
  const previous = host.previousElementSibling;
  if (previous?.matches('label, .k, .muted')) return previous.textContent.trim();
  const container = host.closest('.field, .window-field, .kv');
  const label = container && [...container.children].find((el) => el.matches('label, .k'));
  return label?.textContent.trim() || '选择';
}

export function retroSourceOptions(source) {
  return {
    id: source.id || retroId('retro-value'), value: source.value,
    name: source.name, disabled: source.disabled || source.matches(':disabled'),
    required: source.required, label: source.getAttribute('aria-label') || source.title,
    tooltip: source.dataset.tooltip,
    labelledBy: source.getAttribute('aria-labelledby'),
    describedBy: source.getAttribute('aria-describedby'),
    min: source.min, max: source.max, step: source.step,
    form: source.getAttribute('form'),
  };
}

function watchFields() {
  if (fieldObserver) return;
  fieldObserver = new MutationObserver(() => {
    for (const field of [...fields]) {
      if (field.host.isConnected) field.wasConnected = true;
      if ((field.wasConnected && !field.host.isConnected)
        || !field.host.contains(field.input) || !field.host.contains(field.button)) field.destroy();
    }
  });
  fieldObserver.observe(document.body, { childList: true, subtree: true });
}

export function setupRetroField(host, input, button, config = {}) {
  input.id = config.id || retroId('retro-value');
  input.name = config.name || '';
  if (config.form) input.setAttribute('form', config.form);
  input.required = Boolean(config.required);
  button.id = `${input.id}-button`;
  button.setAttribute('role', 'combobox');
  const label = retroFieldLabel(host, input.id, config.label);
  const labelledBy = config.labelledBy || host.getAttribute('aria-labelledby');
  if (labelledBy) button.setAttribute('aria-labelledby', labelledBy);
  else button.setAttribute('aria-label', label);
  if (config.tooltip) button.dataset.tooltip = config.tooltip;
  button.setAttribute('aria-expanded', 'false');
  button.setAttribute('aria-controls', `${button.id}-pop`);
  button.setAttribute('aria-required', String(input.required));
  for (const labelEl of document.querySelectorAll('label[for]')) {
    if (labelEl.htmlFor === input.id) labelEl.htmlFor = button.id;
  }
  const proxy = document.createElement('input');
  proxy.type = config.validationType || 'text';
  proxy.className = 'retro-validation-input';
  proxy.tabIndex = -1;
  proxy.setAttribute('aria-hidden', 'true');
  if (config.form) proxy.setAttribute('form', config.form);
  proxy.required = input.required;
  for (const attr of ['min', 'max', 'step']) {
    if (config[attr] !== undefined && config[attr] !== '') proxy.setAttribute(attr, config[attr]);
  }
  const error = document.createElement('span');
  error.id = `${button.id}-error`;
  error.className = 'field-hint retro-field-error';
  error.hidden = true;
  error.setAttribute('aria-live', 'polite');
  const describedBy = config.describedBy || host.getAttribute('aria-describedby') || '';
  const valueText = host.querySelector('.retro-select-text,.retro-time-text');
  if (valueText) valueText.id = `${button.id}-value`;
  button.setAttribute('aria-describedby', `${describedBy} ${valueText?.id || ''} ${error.id}`.trim());
  host.append(proxy, error);
  let customMessage = '';
  let destroyed = false;
  const field = {
    host, input, button, label, proxy,
    wasConnected: host.isConnected,
    close() {},
    syncValue() {
      proxy.value = input.value;
      // A temporal input sanitizes malformed values to empty. Keep invalid program
      // values visible to validation rather than silently accepting that empty state.
      proxy.setCustomValidity(customMessage || (input.value && !proxy.value ? '请选择有效的日期或时间' : ''));
      if (!error.hidden) field.validate();
    },
    setDisabled(value) {
      const disabled = Boolean(value);
      if (input.disabled !== disabled) input.disabled = disabled;
      if (button.disabled !== disabled) button.disabled = disabled;
      if (proxy.disabled !== disabled) proxy.disabled = disabled;
      button.setAttribute('aria-disabled', String(disabled));
      host.classList.toggle('is-disabled', disabled);
      if (disabled) { field.close(); showError(''); }
    },
    validate() {
      proxy.disabled = input.disabled || button.disabled || button.matches(':disabled');
      const valid = proxy.disabled || proxy.validity.valid;
      let message = '';
      if (!valid) {
        if (proxy.validity.valueMissing) message = '此项必填';
        else if (proxy.validity.rangeUnderflow) message = `请选择不早于 ${proxy.min} 的值`;
        else if (proxy.validity.rangeOverflow) message = `请选择不晚于 ${proxy.max} 的值`;
        else if (proxy.validity.stepMismatch) message = '所选值不符合时间间隔要求';
        else message = customMessage || proxy.validationMessage;
      }
      showError(message);
      return valid;
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      field.close();
      stateObserver.disconnect();
      proxy.removeEventListener('invalid', onInvalid);
      form?.removeEventListener('reset', onReset);
      proxy.remove();
      error.remove();
      input.disabled = true;
      button.disabled = true;
      fields.delete(field);
      if (!fields.size && fieldObserver) { fieldObserver.disconnect(); fieldObserver = null; }
      if (host._retroField === field) delete host._retroField;
      if (input._retroField === field) delete input._retroField;
      delete input._applyRetroValue;
      for (const property of ['checkValidity', 'reportValidity', 'setCustomValidity', 'validity', 'validationMessage', 'willValidate']) delete input[property];
      field.onDestroy?.();
    },
  };
  function showError(message) {
    error.textContent = message;
    error.hidden = !message;
    button.setAttribute('aria-invalid', String(Boolean(message)));
    host.classList.toggle('is-invalid', Boolean(message));
  }
  const onInvalid = (event) => {
    event.preventDefault();
    field.validate();
    input.dispatchEvent(new Event('invalid', { cancelable: true }));
    if (!button.disabled && button.isConnected) button.focus({ preventScroll: true });
  };
  proxy.addEventListener('invalid', onInvalid);
  const form = input.form;
  const onReset = (event) => queueMicrotask(() => {
    if (destroyed || event.defaultPrevented) return;
    input._applyRetroValue?.(field.initialValue, true);
    showError('');
  });
  form?.addEventListener('reset', onReset);
  // Existing callers retain the hidden value contract while form.checkValidity()
  // sees the native proxy, and direct field checks see its actual validity.
  input.checkValidity = () => {
    proxy.disabled = input.disabled || button.disabled || button.matches(':disabled');
    field.syncValue();
    return proxy.checkValidity();
  };
  input.reportValidity = input.checkValidity;
  input.setCustomValidity = (message) => { customMessage = String(message || ''); field.syncValue(); };
  Object.defineProperties(input, {
    validity: { configurable: true, get: () => proxy.validity },
    validationMessage: { configurable: true, get: () => proxy.validationMessage },
    willValidate: { configurable: true, get: () => proxy.willValidate },
  });
  const stateObserver = new MutationObserver((records) => {
    const record = records.filter((item) => item.attributeName === 'disabled').at(-1);
    if (record) field.setDisabled(record.target.disabled);
    if (records.some((item) => item.attributeName === 'required')) {
      proxy.required = input.required;
      button.setAttribute('aria-required', String(input.required));
    }
  });
  stateObserver.observe(input, { attributes: true, attributeFilter: ['disabled', 'required'] });
  stateObserver.observe(button, { attributes: true, attributeFilter: ['disabled'] });
  host._retroField = field;
  input._retroField = field;
  field.setDisabled(config.disabled);
  fields.add(field);
  watchFields();
  return field;
}

export function attachRetroOverlay(pop, anchor, close) {
  activeOverlay?.close();
  const overlay = { pop, anchor, close };
  activeOverlay = overlay;
  if (!anchor.id) anchor.id = retroId('retro-button');
  pop.id = `${anchor.id}-pop`;
  pop.dataset.retroOverlay = '';
  pop.dataset.retroOwner = anchor.id;
  pop._retroClose = close;
  anchor.setAttribute('aria-controls', pop.id);
  anchor.setAttribute('aria-expanded', 'true');
  const host = anchor.closest('.retro-select, .retro-time');
  host?.classList.add('is-open');
  const openedAt = Date.now();
  const onOutside = (event) => {
    if (!pop.contains(event.target) && !anchor.contains(event.target)) close();
  };
  const onFocus = (event) => {
    if (!pop.contains(event.target) && !anchor.contains(event.target)) close();
  };
  const onKey = (event) => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    event.preventDefault();
    event.stopPropagation();
    if (!pop._closeSubpanel?.()) close({ restoreFocus: true });
  };
  const onViewport = (event) => {
    if (event.type === 'scroll' && Date.now() - openedAt < 300) return;
    if (event.type === 'scroll' && event.target !== window && event.target !== document
      && pop.contains(event.target)) return;
    close();
  };
  const observer = new MutationObserver(() => {
    if (!anchor.isConnected || !pop.isConnected || anchor.disabled || pop._forInput?.disabled
      || anchor.closest('[hidden]') || !anchor.getClientRects().length) close();
  });
  observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['disabled', 'hidden', 'style'] });
  document.addEventListener('mousedown', onOutside);
  document.addEventListener('focusin', onFocus);
  document.addEventListener('keydown', onKey, true);
  window.addEventListener('resize', onViewport);
  window.addEventListener('scroll', onViewport, true);
  return () => {
    document.removeEventListener('mousedown', onOutside);
    document.removeEventListener('focusin', onFocus);
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('resize', onViewport);
    window.removeEventListener('scroll', onViewport, true);
    observer.disconnect();
    anchor.setAttribute('aria-expanded', 'false');
    host?.classList.remove('is-open');
    if (activeOverlay === overlay) activeOverlay = null;
  };
}

export function positionRetroOverlay(pop, anchor, align = 'left') {
  anchor.scrollIntoView({ block: 'nearest' });
  const rect = anchor.getBoundingClientRect();
  const popRect = pop.getBoundingClientRect();
  let left = align === 'right' ? Math.max(rect.right - popRect.width, rect.left) : rect.left;
  let top = rect.bottom + 6;
  if (top + popRect.height > window.innerHeight - 8) top = rect.top - popRect.height - 6;
  top = Math.min(Math.max(top, 8), Math.max(8, window.innerHeight - popRect.height - 8));
  left = Math.min(Math.max(left, 8), Math.max(8, window.innerWidth - popRect.width - 8));
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
}

export function moveRetroOptionFocus(list, selector, event) {
  const options = [...list.querySelectorAll(selector)].filter((option) => !option.disabled);
  const index = options.indexOf(document.activeElement);
  let next;
  if (event.key === 'ArrowDown') next = Math.min(index + 1, options.length - 1);
  else if (event.key === 'ArrowUp') next = Math.max(index - 1, 0);
  else if (event.key === 'Home') next = 0;
  else if (event.key === 'End') next = options.length - 1;
  else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && event.key !== ' ') {
    const now = Date.now();
    list._searchText = now - (list._searchAt || 0) < 700 ? (list._searchText || '') + event.key : event.key;
    list._searchAt = now;
    const search = list._searchText.toLocaleLowerCase();
    const ordered = [...options.slice(index + 1), ...options.slice(0, index + 1)];
    const match = ordered.find((option) => option.textContent.trim().toLocaleLowerCase().startsWith(search));
    next = options.indexOf(match);
  } else return false;
  if (options[next]) {
    for (const option of options) option.tabIndex = option === options[next] ? 0 : -1;
    options[next].focus({ preventScroll: true });
    options[next].scrollIntoView({ block: 'nearest' });
  }
  event.preventDefault();
  return true;
}
