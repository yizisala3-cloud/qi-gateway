// Candidate order belongs to this controller until confirmation or cancellation.
import { gw } from '../api.js?v=20261016-memo-detail-latency';
import { toast } from '../ui.js?v=20261016-memo-detail-latency';

export function createPlanningSort({
  getRoot, getProgressList, getBoard, getActiveTab, getActiveSection,
  selectSection, renderBoard, loadToday, getReorderMode, setReorderMode,
  invalidateToday = () => {}, onSaved = null, captureSave = () => null,
}) {
  let session = 0;
  let pending = false;
  let request = null;
  let expectedOrder = [];
  let stopDrag = null;
  const feedback = (message = '') => {
    const host = getRoot()?.querySelector('[data-reorder-feedback]');
    if (host) { host.textContent = message; host.hidden = !message; }
  };
  const busy = (value) => {
    getRoot()?.querySelectorAll('[data-act="confirm-reorder"],[data-act="cancel-reorder"]')
      .forEach((button) => { button.disabled = value; });
  };
  const sort = {
    enterReorder() {
      if (getReorderMode() || pending) return;
      if (getActiveTab() !== 'today') {
        toast('调整顺序只在「当前待办」页签可用');
        return;
      }
      const items = getBoard()?.progress || [];
      if (items.length < 2) { toast('至少两个待办才能调整顺序'); return; }
      if (getActiveSection() !== 'progress') selectSection('progress');
      session += 1;
      request = null;
      expectedOrder = items.map(({ id, sort_order = 0, status = 'pending' }) => ({ id, sort_order, status }));
      invalidateToday();
      setReorderMode(true);
      feedback();
      busy(false);
      getRoot().querySelector('#planning-reorder-bar').style.display = '';
      getRoot().querySelector('#planning-reorder-btn').disabled = true;
      renderBoard();
    },

    exitReorder() {
      stopDrag?.(false);
      session += 1;
      invalidateToday();
      setReorderMode(false);
      getProgressList()?.classList.remove('reordering');
      if (!getRoot()) return;
      getRoot().querySelector('#planning-reorder-bar').style.display = 'none';
      getRoot().querySelector('#planning-reorder-btn').disabled = pending;
    },

    async confirmReorder() {
      if (pending || !getReorderMode()) return;
      stopDrag?.(true);
      const ids = [...getProgressList().querySelectorAll('.plan-item')]
        .map((el) => Number(el.dataset.occ)).filter(Boolean);
      const signature = JSON.stringify(ids);
      if (!request || request.signature !== signature) request = { signature, key: crypto.randomUUID() };
      const owner = session;
      const receipt = captureSave();
      const root = getRoot();
      pending = true;
      busy(true);
      feedback('正在检查并保存顺序…');
      let saved;
      try {
        saved = await gw('/admin/api/planning/reorder', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ order: ids, request_key: request.key, expected_order: expectedOrder }),
        });
      } catch (error) {
        if (owner !== session || root !== getRoot()) return;
        if (error.code === 'concurrent_modified'
            || /order (must include every open occurrence|includes occurrences outside)/.test(error.message)) {
          toast('待办列表有变化，请重新进入排列', 'warn');
          sort.exitReorder();
          await loadToday();
        } else {
          const conflicts = error.details?.conflicts;
          feedback(conflicts?.length
            ? conflicts.map((c) => `${getBoard()?.progress.find((o) => o.id === c.occurrence_id)?.content || '待办'}：${c.reason}`).join('；')
            : `顺序尚未确认：${error.message}。可重试确认，沿用本次请求身份。`);
        }
        return;
      } finally {
        pending = false;
        if (root === getRoot()) {
          busy(false);
          getRoot().querySelector('#planning-reorder-btn').disabled = getReorderMode();
        }
      }
      toast((saved?.recompute_enabled ?? getBoard()?.recompute?.enabled) === false
        ? '顺序已保存；自动重算已关闭，时间未重算，可随时手动「重新计算时间」'
        : saved?.schedule_applied ? '顺序和预估时间已更新' : '顺序已保存，等待自动重算');
      if (owner !== session || root !== getRoot()) {
        // A committed write still invalidates data after leaving this session;
        // its candidate must not overwrite a different page or a newer reorder.
        if (onSaved) onSaved(ids, saved, receipt);
        return;
      }
      sort.exitReorder();
      // Commit feedback is immediate; refresh has a separate page identity.
      if (onSaved) onSaved(ids, saved, receipt);
      else if (await loadToday() === false) toast('顺序已保存，列表更新失败，请刷新重试', 'warn');
    },

    async cancelReorder() {
      if (pending) return;
      sort.exitReorder();
      renderBoard();
      await loadToday();
      toast('已撤销本次排列');
    },

    attachDragHandlers() {
      const list = getProgressList();
      if (!list) return;
      list.classList.add('reordering');
      list.querySelectorAll('.plan-item').forEach((item) => {
        item.onpointerdown = (e) => {
          if (e.target.closest('.plan-handle')) sort.startDrag(e, item);
        };
      });
    },

    startDrag(e, item) {
      if (pending || stopDrag || !getReorderMode() || (e.pointerType === 'mouse' && e.button !== 0)) return;
      e.preventDefault();
      const list = getProgressList();
      const items = [...list.querySelectorAll('.plan-item')];
      const offsets = new Map(items.map((el, index) => [el, index]));
      const others = items.filter((el) => el !== item);
      const origin = items.indexOf(item);
      if (origin < 0) return;
      const rect = item.getBoundingClientRect();
      let scroller = list;
      while (scroller && scroller !== document.body
        && !(scroller.scrollHeight > scroller.clientHeight && /auto|scroll/.test(getComputedStyle(scroller).overflowY))) {
        scroller = scroller.parentElement;
      }
      if (!scroller || scroller === document.body) scroller = document.scrollingElement;
      const pageScroll = scroller === document.scrollingElement;
      const scrollY = () => pageScroll ? window.scrollY : scroller.scrollTop;
      let startScroll = scrollY();
      let geometry = items.map((el) => el.getBoundingClientRect());
      let bounds = pageScroll ? { top: 0, bottom: window.innerHeight } : scroller.getBoundingClientRect();
      let latestY = e.clientY;
      const grabY = e.clientY - rect.top;
      let index = origin;
      let candidate = items;
      const clone = item.cloneNode(true);
      clone.classList.add('plan-drag-copy');
      clone.removeAttribute('data-occ');
      clone.setAttribute('aria-hidden', 'true');
      Object.assign(clone.style, { position: 'fixed', left: `${rect.left}px`, top: `${rect.top}px`,
        width: `${rect.width}px`, height: `${rect.height}px`, margin: '0', pointerEvents: 'none' });
      document.body.appendChild(clone);
      item.classList.add('is-dragging');
      item.setPointerCapture(e.pointerId);
      let frame;
      let ended = false;
      const draw = () => {
        if (ended) return;
        const margin = Math.min(56, (bounds.bottom - bounds.top) / 4);
        const step = latestY < bounds.top + margin ? -Math.min(18, (bounds.top + margin - latestY) / 3)
          : latestY > bounds.bottom - margin ? Math.min(18, (latestY - bounds.bottom + margin) / 3) : 0;
        if (step) scroller.scrollTop += step;
        const delta = scrollY() - startScroll;
        const center = latestY - grabY + rect.height / 2 + delta;
        index = others.filter((el) => {
          const g = geometry[offsets.get(el)];
          return center > g.top + g.height / 2;
        }).length;
        candidate = [...others];
        candidate.splice(index, 0, item);
        let top = geometry[0].top;
        const gap = geometry.length > 1 ? geometry[1].top - geometry[0].bottom : 0;
        for (const el of candidate) {
          const g = geometry[offsets.get(el)];
          if (el !== item) el.style.transform = `translateY(${top - g.top}px)`;
          top += g.height + gap;
        }
        clone.style.transform = `translateY(${latestY - grabY - rect.top}px)`;
        frame = requestAnimationFrame(draw);
      };
      const move = (ev) => { if (ev.pointerId === e.pointerId) latestY = ev.clientY; };
      const resize = () => {
        items.forEach((el) => { el.style.transform = ''; });
        geometry = items.map((el) => el.getBoundingClientRect());
        bounds = pageScroll ? { top: 0, bottom: window.innerHeight } : scroller.getBoundingClientRect();
        startScroll = scrollY();
      };
      const finish = (commit) => {
        if (ended) return;
        ended = true;
        cancelAnimationFrame(frame);
        items.forEach((el) => { el.style.transform = ''; });
        item.classList.remove('is-dragging');
        clone.remove();
        item.removeEventListener('pointermove', move);
        item.removeEventListener('pointerup', up);
        item.removeEventListener('pointercancel', cancel);
        item.removeEventListener('lostpointercapture', cancel);
        window.removeEventListener('resize', resize);
        observer?.disconnect();
        if (item.hasPointerCapture(e.pointerId)) item.releasePointerCapture(e.pointerId);
        if (commit && index !== origin) candidate.forEach((el) => list.appendChild(el));
        stopDrag = null;
      };
      const up = (ev) => { if (ev.pointerId === e.pointerId) { latestY = ev.clientY; cancelAnimationFrame(frame); draw(); finish(true); } };
      const cancel = () => finish(false);
      item.addEventListener('pointermove', move);
      item.addEventListener('pointerup', up);
      item.addEventListener('pointercancel', cancel);
      item.addEventListener('lostpointercapture', cancel);
      window.addEventListener('resize', resize);
      const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(resize) : null;
      observer?.observe(list);
      stopDrag = finish;
      frame = requestAnimationFrame(draw);
    },
  };
  return sort;
}
