// Pointer sorting controller. The page keeps reorderMode and refresh scheduling.
import { gw } from '../api.js?v=20261004-ring-fix1';
import { toast } from '../ui.js?v=20261004-ring-fix1';

export function createPlanningSort({
  getRoot, getProgressList, getBoard, getActiveTab, getActiveSection,
  selectSection, renderBoard, loadToday, getReorderMode, setReorderMode,
}) {
  const sort = {
    enterReorder() {
      if (getReorderMode()) return;
      if (getActiveTab() !== 'today') {
        // BUG-16：排列模式只在「当前待办」页签内可用
        toast('调整顺序只在「当前待办」页签可用');
        return;
      }
      const items = getBoard()?.progress || [];
      if (items.length < 2) {
        toast('至少两个待办才能调整顺序');
        return;
      }
      if (getActiveSection() !== 'progress') selectSection('progress');  // 可拖拽列表在「进度中」
      setReorderMode(true);
      getRoot().querySelector('#planning-reorder-bar').style.display = '';
      getRoot().querySelector('#planning-reorder-btn').disabled = true;
      renderBoard();
    },

    exitReorder() {
      setReorderMode(false);
      if (!getRoot()) return;
      getRoot().querySelector('#planning-reorder-bar').style.display = 'none';
      const btn = getRoot().querySelector('#planning-reorder-btn');
      if (btn) btn.disabled = false;
    },

    async confirmReorder() {
      const ids = [...getProgressList().querySelectorAll('.plan-item')]
        .map((el) => Number(el.dataset.occ)).filter(Boolean);
      try {
        await gw('/admin/api/planning/reorder', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ order: ids }),
        });
        // 自动重算关闭时不得提示「等待自动重算」（需求 16.3）：按配置给准确文案
        toast(getBoard()?.recompute?.enabled === false
          ? '顺序已保存；自动重算已关闭，时间未重算，可随时手动「重新计算时间」'
          : '顺序已保存，等待自动重算');
        sort.exitReorder();
        await loadToday();
      } catch (error) {
        if (String(error.message).includes('order must include every open occurrence')) {
          // 排列期间后台新生成了实例（如拆分 / 规则变更），确认被拒：
          // 刷新列表退出排列，让用户基于最新列表重新进入（BUG-10）
          toast('待办列表有变化，请重新进入排列');
          sort.exitReorder();
          await loadToday();
          return;
        }
        toast(`保存顺序失败：${error.message}`, 'err');
      }
    },

    async cancelReorder() {
      sort.exitReorder();
      await loadToday();
      toast('已撤销本次排列');
    },

    attachDragHandlers() {
      if (!getProgressList()) return;
      getProgressList().classList.add('reordering');
      getProgressList().querySelectorAll('.plan-item').forEach((el) => {
        el.addEventListener('pointerdown', (e) => sort.startDrag(e, el));
      });
    },

    startDrag(e, item) {
      if (e.button !== 0 && e.pointerType === 'mouse') return;
      e.preventDefault();
      const list = getProgressList();
      item.classList.add('is-dragging');
      item.setPointerCapture(e.pointerId);
      let anchorY = e.clientY;

      const onMove = (ev) => {
        item.style.transform = `translate(0, ${ev.clientY - anchorY}px)`;
        const rect = item.getBoundingClientRect();
        const mid = rect.top + rect.height / 2;
        for (const sib of [...list.children]) {
          if (sib === item) continue;
          const sr = sib.getBoundingClientRect();
          const sibMid = sr.top + sr.height / 2;
          const itemAfterSib = sib.compareDocumentPosition(item) & Node.DOCUMENT_POSITION_PRECEDING;
          if (mid < sibMid && !itemAfterSib) {
            list.insertBefore(item, sib);
            anchorY = ev.clientY;
            item.style.transform = '';
            break;
          }
          if (mid > sibMid && itemAfterSib) {
            list.insertBefore(item, sib.nextSibling);
            anchorY = ev.clientY;
            item.style.transform = '';
            break;
          }
        }
      };
      const onEnd = () => {
        item.classList.remove('is-dragging');
        item.style.transform = '';
        item.removeEventListener('pointermove', onMove);
        item.removeEventListener('pointerup', onEnd);
        item.removeEventListener('pointercancel', onEnd);
      };
      item.addEventListener('pointermove', onMove);
      item.addEventListener('pointerup', onEnd);
      item.addEventListener('pointercancel', onEnd);
    },
  };
  return sort;
}
