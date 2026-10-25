// Auxiliary paper hints. Required/locked/unavailable explanations belong in visible page text.
import { registerFloatingSurface } from '../ui.js?v=20261025-console-latency-r1';

const initializers = new WeakMap();
let descriptionSequence = 0;

export function initRetroTooltips(root = document) {
  if (initializers.has(root)) return initializers.get(root);
  const doc = root.ownerDocument || root;
  const win = doc.defaultView;
  const records = new Map();
  let active = null;
  let unregister = null;
  let hovered = null;
  let focused = null;
  let overTip = false;
  let showTimer = null;
  let hideTimer = null;

  const belongs = element => !!element && root.contains(element);
  const clearTimers = () => {
    win.clearTimeout(showTimer);
    win.clearTimeout(hideTimer);
    showTimer = hideTimer = null;
  };
  const hide = () => {
    clearTimers();
    unregister?.();
    unregister = null;
    if (active) active.tip.hidden = true;
    active = null;
    overTip = false;
  };
  const describe = record => {
    const ids = (record.anchor.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
    if (!ids.includes(record.tip.id)) {
      record.anchor.setAttribute('aria-describedby', [...ids, record.tip.id].join(' '));
    }
  };
  const forget = anchor => {
    const record = records.get(anchor);
    if (!record) return;
    if (active === record) hide();
    if (hovered === anchor) hovered = null;
    if (focused === anchor) focused = null;
    const ids = (anchor.getAttribute('aria-describedby') || '').split(/\s+/)
      .filter(id => id && id !== record.tip.id);
    if (ids.length) anchor.setAttribute('aria-describedby', ids.join(' '));
    else anchor.removeAttribute('aria-describedby');
    record.tip.remove();
    records.delete(anchor);
  };
  const prepare = anchor => {
    // Also covers sanitized Markdown link/image titles and later dynamic callers.
    const nativeTitle = anchor.getAttribute('title');
    if (nativeTitle !== null) {
      if (!anchor.hasAttribute('data-tooltip') && nativeTitle.trim()) {
        anchor.setAttribute('data-tooltip', nativeTitle.trim());
      }
      anchor.removeAttribute('title');
    }
    const text = (anchor.getAttribute('data-tooltip') || '').trim();
    if (!text || anchor.matches('option')) { forget(anchor); return; }
    let record = records.get(anchor);
    if (!record) {
      const tip = doc.createElement('div');
      tip.className = 'retro-tooltip';
      tip.id = `retro-tooltip-${++descriptionSequence}`;
      tip.setAttribute('role', 'tooltip');
      tip.hidden = true;
      doc.body.appendChild(tip);
      record = { anchor, tip };
      records.set(anchor, record);
    }
    if (record.tip.textContent !== text) record.tip.textContent = text;
    // Description is available before focus; the control keeps its existing accessible name.
    describe(record);
    return record;
  };
  const scan = node => {
    if (node.nodeType !== 1 && node.nodeType !== 9) return;
    if (node.matches?.('[data-tooltip], [title]')) prepare(node);
    node.querySelectorAll('[data-tooltip], [title]').forEach(prepare);
  };
  const usable = anchor => belongs(anchor) && anchor.isConnected
    && !anchor.matches(':disabled, [aria-disabled="true"]')
    && !anchor.closest('[inert], [aria-hidden="true"]')
    && anchor.getClientRects().length > 0;
  const position = record => {
    const viewport = win.visualViewport;
    const leftEdge = (viewport?.offsetLeft || 0) + 8;
    const topEdge = (viewport?.offsetTop || 0) + 8;
    const width = viewport?.width || win.innerWidth;
    const height = viewport?.height || win.innerHeight;
    const rightEdge = leftEdge + width - 16;
    const bottomEdge = topEdge + height - 16;
    record.tip.style.maxWidth = `${Math.max(1, Math.min(320, width - 16))}px`;
    record.tip.style.maxHeight = `${Math.max(1, height - 16)}px`;
    const anchorRect = record.anchor.getBoundingClientRect();
    const rect = record.tip.getBoundingClientRect();
    const left = Math.max(leftEdge, Math.min(anchorRect.left + (anchorRect.width - rect.width) / 2,
      rightEdge - rect.width));
    const below = anchorRect.bottom + 6;
    const top = Math.max(topEdge, Math.min(below + rect.height <= bottomEdge
      ? below : anchorRect.top - rect.height - 6, bottomEdge - rect.height));
    record.tip.style.left = `${left}px`;
    record.tip.style.top = `${top}px`;
  };
  const show = anchor => {
    hide();
    if (!usable(anchor)) return;
    const record = prepare(anchor);
    if (!record) return;
    active = record;
    record.tip.hidden = false;
    position(record);
    unregister = registerFloatingSurface({ element: record.tip, anchor, onEscape: hide });
  };
  const requestShow = (anchor, immediate = false) => {
    clearTimers();
    if (active?.anchor === anchor) return;
    if (immediate) show(anchor);
    else showTimer = win.setTimeout(() => show(anchor), 350);
  };
  const requestHide = () => {
    win.clearTimeout(showTimer);
    win.clearTimeout(hideTimer);
    if (active && (focused === active.anchor || hovered === active.anchor || overTip)) return;
    // A short bridge lets the pointer cross from its control to the noninteractive hint.
    hideTimer = win.setTimeout(hide, 160);
  };
  const trigger = node => {
    const anchor = node?.closest?.('[data-tooltip]');
    return belongs(anchor) ? anchor : null;
  };
  const onOver = event => {
    if (event.pointerType === 'touch') return;
    if (active?.tip.contains(event.target)) {
      overTip = true;
      clearTimers();
      return;
    }
    const anchor = trigger(event.target);
    if (!anchor || anchor.contains(event.relatedTarget)) return;
    hovered = anchor;
    overTip = false;
    requestShow(anchor);
  };
  const onOut = event => {
    if (active?.tip.contains(event.target)) {
      if (active.tip.contains(event.relatedTarget)) return;
      overTip = false;
      hovered = active.anchor.contains(event.relatedTarget) ? active.anchor : null;
      requestHide();
      return;
    }
    const anchor = trigger(event.target);
    if (!anchor || anchor.contains(event.relatedTarget)) return;
    hovered = null;
    requestHide();
  };
  const onFocus = event => {
    focused = trigger(event.target);
    if (focused) requestShow(focused, true);
    else requestHide();
  };
  const onBlur = event => {
    focused = trigger(event.relatedTarget);
    requestHide();
  };
  const onViewportChange = () => { hovered = null; hide(); };
  const observer = new MutationObserver(mutations => {
    for (const mutation of mutations) {
      if (mutation.type === 'attributes' && mutation.target.matches('[data-tooltip], [title]')) {
        prepare(mutation.target);
      } else if (mutation.type === 'attributes') {
        forget(mutation.target);
      } else {
        mutation.addedNodes.forEach(scan);
      }
    }
    for (const anchor of records.keys()) if (!belongs(anchor)) forget(anchor);
    if (active) {
      if (!usable(active.anchor)) hide();
      else position(active);
    }
  });
  scan(root);
  observer.observe(root, { childList: true, subtree: true, attributes: true,
    attributeFilter: ['data-tooltip', 'title', 'disabled', 'hidden', 'aria-hidden', 'aria-describedby'] });
  doc.addEventListener('pointerover', onOver);
  doc.addEventListener('pointerout', onOut);
  doc.addEventListener('focusin', onFocus);
  doc.addEventListener('focusout', onBlur);
  // Drag handles can capture the pointer before a click is ever dispatched.
  doc.addEventListener('pointerdown', hide, true);
  doc.addEventListener('click', hide, true);
  doc.addEventListener('scroll', onViewportChange, true);
  win.addEventListener('resize', onViewportChange);
  win.addEventListener('hashchange', onViewportChange);
  win.visualViewport?.addEventListener('resize', onViewportChange);
  win.visualViewport?.addEventListener('scroll', onViewportChange);
  const disconnect = () => {
    observer.disconnect();
    hide();
    for (const anchor of records.keys()) forget(anchor);
    doc.removeEventListener('pointerover', onOver);
    doc.removeEventListener('pointerout', onOut);
    doc.removeEventListener('focusin', onFocus);
    doc.removeEventListener('focusout', onBlur);
    doc.removeEventListener('pointerdown', hide, true);
    doc.removeEventListener('click', hide, true);
    doc.removeEventListener('scroll', onViewportChange, true);
    win.removeEventListener('resize', onViewportChange);
    win.removeEventListener('hashchange', onViewportChange);
    win.visualViewport?.removeEventListener('resize', onViewportChange);
    win.visualViewport?.removeEventListener('scroll', onViewportChange);
    initializers.delete(root);
  };
  initializers.set(root, disconnect);
  return disconnect;
}
