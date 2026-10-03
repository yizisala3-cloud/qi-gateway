// lib/retro_select.js - 复古下拉选择器（纸张卡片 + 金线选项列表，替换原生 select 弹层）
// 与 lib/retro_time.js 同一套机制：隐藏 input 保留原 id/value 契约（change 事件照发），
// 展示层为纸色按钮，弹层为象牙纸卡；选中项深绿高亮（同日历选中日）。
// 选项来自调用方传入的 [{ value, label }]，键盘支持 Esc 关闭与上下箭头换选项。
import { esc, icon } from '../ui.js?v=20261003-planning-create-latency2';

let activeRetroSelectPop = null;

function closeRetroSelectPop() {
  if (activeRetroSelectPop) {
    const pop = activeRetroSelectPop;
    activeRetroSelectPop = null;
    if (pop._cleanup) pop._cleanup();
    pop.remove();
  }
}

/** 在 host 内挂载复古下拉；返回隐藏 input（原 id 契约不变）。 */
export function createRetroSelectField(host, { id, value = '', options = [] } = {}) {
  host.classList.add('retro-select');
  host.innerHTML = `
    <input type="hidden" id="${id}" class="retro-select-value">
    <button type="button" class="retro-select-field" aria-haspopup="listbox">
      <span class="retro-select-text"></span>
      ${icon('chevron-down')}
    </button>`;
  const input = host.querySelector('.retro-select-value');
  const textEl = host.querySelector('.retro-select-text');
  const labelOf = (v) => (options.find((o) => String(o.value) === String(v)) || {}).label || '';
  const apply = (localValue, silent) => {
    input.value = localValue;
    textEl.textContent = labelOf(localValue);
    if (!silent) input.dispatchEvent(new Event('change', { bubbles: true }));
  };
  apply(value, true);
  input._applyRetroValue = apply;
  host.querySelector('.retro-select-field').addEventListener('click', () => {
    if (activeRetroSelectPop && activeRetroSelectPop._forInput === input) {
      closeRetroSelectPop();
      return;
    }
    closeRetroSelectPop();
    const pop = openRetroSelectPop(host.querySelector('.retro-select-field'), input, apply, options);
    host.classList.add('is-open');
    const prevCleanup = pop._cleanup;
    pop._cleanup = () => {
      host.classList.remove('is-open');
      prevCleanup();
    };
  });
  return input;
}

export function openRetroSelectPop(anchor, input, apply, options) {
  const current = String(input.value ?? '');
  const pop = document.createElement('div');
  pop.className = 'retro-select-pop';
  pop._forInput = input;
  pop.innerHTML = options.map((o) => `
    <button type="button" class="retro-select-option${String(o.value) === current ? ' is-selected' : ''}"
      data-value="${esc(o.value)}">${esc(o.label)}</button>`).join('');
  document.body.appendChild(pop);
  activeRetroSelectPop = pop;

  pop.querySelectorAll('.retro-select-option').forEach((btn) => {
    btn.addEventListener('click', () => {
      apply(btn.dataset.value);
      closeRetroSelectPop();
      anchor.focus();
    });
  });
  // 弹层内上下箭头在选项间移动焦点（Tab 亦可用）
  pop.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    const opts = [...pop.querySelectorAll('.retro-select-option')];
    const idx = opts.indexOf(document.activeElement);
    const next = e.key === 'ArrowDown' ? Math.min(idx + 1, opts.length - 1) : Math.max(idx - 1, 0);
    if (opts[next]) opts[next].focus();
    e.preventDefault();
  });
  const selectedBtn = pop.querySelector('.retro-select-option.is-selected') || pop.querySelector('.retro-select-option');
  if (selectedBtn) selectedBtn.focus({ preventScroll: true });

  // 定位与回收机制与 retro_time 一致：字段正下方，放不下翻上方，双向夹紧视口；
  // 与字段同宽起算，选项多的窄字段也不会挤成一列窄条。
  anchor.scrollIntoView({ block: 'nearest' });
  pop.style.minWidth = `${anchor.getBoundingClientRect().width}px`;
  const rect = anchor.getBoundingClientRect();
  const popRect = pop.getBoundingClientRect();
  let left = rect.left;
  let top = rect.bottom + 6;
  if (top + popRect.height > window.innerHeight - 8) {
    top = rect.top - popRect.height - 6;
  }
  top = Math.min(Math.max(top, 8), Math.max(8, window.innerHeight - popRect.height - 8));
  left = Math.min(Math.max(left, 8), Math.max(8, window.innerWidth - popRect.width - 8));
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;

  const onOutside = (e) => {
    if (!pop.contains(e.target) && !anchor.contains(e.target)) closeRetroSelectPop();
  };
  const onKey = (e) => { if (e.key === 'Escape') closeRetroSelectPop(); };
  // 视口变化后固定定位不再贴合字段，直接关闭，避免弹层漂移出屏；
  // 打开瞬间的 scrollIntoView 自身引发的滚动豁免 300ms，否则弹层刚开即关。
  // 弹层自身选项列表的滚动不算视口变化（选项多时内部滚动不应关窗）。
  const openedAt = Date.now();
  const onViewportChange = (e) => {
    if (Date.now() - openedAt < 300) return;
    if (e && e.type === 'scroll' && e.target !== window && e.target !== document
      && pop.contains(e.target)) return;
    closeRetroSelectPop();
  };
  const cleanup = () => {
    document.removeEventListener('mousedown', onOutside);
    document.removeEventListener('keydown', onKey);
    window.removeEventListener('resize', onViewportChange);
    window.removeEventListener('scroll', onViewportChange, true);
  };
  setTimeout(() => {
    document.addEventListener('mousedown', onOutside);
    document.addEventListener('keydown', onKey);
    window.addEventListener('resize', onViewportChange);
    window.addEventListener('scroll', onViewportChange, true);
  });
  pop._cleanup = cleanup;

  // 外层容器（如表单 modal）被其父节点整体移除时，宿主按钮离开文档，
  // 立即回收弹层与其全局监听。
  const rootObserver = new MutationObserver(() => {
    if (!document.contains(anchor)) closeRetroSelectPop();
  });
  rootObserver.observe(document.body, { childList: true });
  const prevCleanup = pop._cleanup;
  pop._cleanup = () => { prevCleanup(); rootObserver.disconnect(); };
  return pop;
}
