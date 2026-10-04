// lib/retro_time.js - 复古时间选择器（纸张卡片 + 金线日历，替换原生 date/time 弹窗）
// 从 pages/_memory_form.js 的 createRetroTimeField / openRetroTimePop 提取，
// 行为与原 datetime 实现一致；额外支持两种模式（BUG-14 统一原生控件）：
//   datetime（默认）：月历 + 时/分，产出 YYYY-MM-DDTHH:MM（原 _memory_form.js 行为）
//   date：只显示月历 + 清除/今天，产出 YYYY-MM-DD
//   time：隐藏月历，只留时/分 + 清除/确定，产出 HH:MM（tod 字符串，后端不变）
// 样式复用 style.css 的 .retro-time-*；input 上挂 _applyRetroValue 供外部程序化清空。
import { icon } from '../ui.js?v=20261004-memo-bugfix3';

let activeRetroTimePop = null;

function closeRetroTimePop() {
  if (activeRetroTimePop) {
    const pop = activeRetroTimePop;
    activeRetroTimePop = null;
    if (pop._cleanup) pop._cleanup();
    pop.remove();
  }
}

function pad2(n) {
  return String(n).padStart(2, '0');
}

/**
 * 时间弹层内的时/分下拉：纸色按钮 + 金线选项列表（与复古下拉同一视觉体系）。
 * 列表约显示 6 项，其余滚动；下方放不下自动向上弹。返回 set(value) 供程序化赋值。
 */
function createRtpUnit(host, values, initial, onChange) {
  host.innerHTML = `
    <button type="button" class="rtp-select-btn" aria-haspopup="listbox" title="${host.title}">
      <span class="rtp-select-text"></span>
      ${icon('chevron-down')}
    </button>
    <div class="rtp-select-pop" hidden></div>`;
  const btn = host.querySelector('.rtp-select-btn');
  const textEl = host.querySelector('.rtp-select-text');
  const list = host.querySelector('.rtp-select-pop');
  let current = String(initial);
  const render = () => {
    textEl.textContent = current;
    list.innerHTML = values.map((v) => `
      <button type="button" class="rtp-select-option${v === current ? ' is-selected' : ''}"
        data-value="${v}">${v}</button>`).join('');
  };
  const close = () => {
    list.hidden = true;
    host.classList.remove('is-open');
  };
  const open = () => {
    render();
    list.hidden = false;
    host.classList.add('is-open');
    const selected = list.querySelector('.is-selected');
    if (selected) selected.scrollIntoView({ block: 'nearest' });
    // 下方放不下就向上弹
    list.style.top = '';
    list.style.bottom = '';
    if (list.getBoundingClientRect().bottom > window.innerHeight - 8) {
      list.style.top = 'auto';
      list.style.bottom = 'calc(100% + 4px)';
    }
  };
  btn.addEventListener('click', () => {
    const wasOpen = !list.hidden;
    document.querySelectorAll('.rtp-select-pop:not([hidden])').forEach((p) => {
      if (p !== list) {
        p.hidden = true;
        p.parentElement.classList.remove('is-open');
      }
    });
    wasOpen ? close() : open();
  });
  list.addEventListener('click', (e) => {
    const opt = e.target.closest('.rtp-select-option');
    if (!opt) return;
    current = opt.dataset.value;
    onChange(current);
    render();
    close();
  });
  render();
  return {
    set(v) {
      current = String(v);
      render();
    },
  };
}

const SHANGHAI_OFFSET_MS = 8 * 3600 * 1000;

/** 当前时刻的 Asia/Shanghai datetime-local 表示（与系统时区无关）。 */
function nowShanghaiLocalInput() {
  const shifted = new Date(Date.now() + SHANGHAI_OFFSET_MS);
  return `${shifted.getUTCFullYear()}-${pad2(shifted.getUTCMonth() + 1)}-${pad2(shifted.getUTCDate())}T${pad2(shifted.getUTCHours())}:${pad2(shifted.getUTCMinutes())}`;
}

function fmtDisplay(mode, value) {
  const text = String(value || '');
  if (mode === 'date') {
    const m = text.match(/^(\d{4})-(\d{2})-(\d{2})$/);
    return m ? `${m[1]}年${m[2]}月${m[3]}日` : '';
  }
  if (mode === 'time') {
    // 真实 PostgREST time 列形状为 HH:MM:SS（批次 8 HIGH #1）：编辑表单
    // 会把任务现有窗口原样装进 data-retro-value，必须按同值显示而非空。
    const m = text.match(/^(\d{2}):(\d{2})(?::\d{2})?$/);
    return m ? `${m[1]}:${m[2]}` : '';
  }
  const m = text.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/);
  return m ? `${m[1]}年${m[2]}月${m[3]}日 ${m[4]}:${m[5]}` : '';
}

/** 在 host 内挂载复古时间字段：隐藏 input 保留原 id/value 契约，展示层为纸色按钮。 */
export function createRetroTimeField(host, { id, value = '', mode = 'datetime', align = 'left' } = {}) {
  host.classList.add('retro-time');
  host.innerHTML = `
    <input type="hidden" id="${id}" class="retro-time-value">
    <button type="button" class="retro-time-field" aria-haspopup="dialog">
      <span class="retro-time-text"></span>
      ${icon('clock')}
    </button>`;
  const input = host.querySelector('.retro-time-value');
  const textEl = host.querySelector('.retro-time-text');
  const apply = (localValue, silent) => {
    input.value = localValue || '';
    const shown = fmtDisplay(mode, localValue);
    textEl.textContent = shown;
    textEl.classList.toggle('is-empty', !shown);
    if (!silent) input.dispatchEvent(new Event('input', { bubbles: true }));
  };
  apply(value, true);
  input._applyRetroValue = apply;
  host.querySelector('.retro-time-field').addEventListener('click', () => {
    if (activeRetroTimePop && activeRetroTimePop._forInput === input) {
      closeRetroTimePop();
      return;
    }
    closeRetroTimePop();
    openRetroTimePop(host.querySelector('.retro-time-field'), input, apply, mode, align);
  });
  return input;
}

export function openRetroTimePop(anchor, input, apply, mode = 'datetime', align = 'left') {
  const current = String(input.value || '');
  const nowText = nowShanghaiLocalInput();
  const nowM = nowText.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/);
  const showCalendar = mode === 'datetime' || mode === 'date';
  const showTimeRow = mode === 'datetime' || mode === 'time';

  let state;
  if (mode === 'date') {
    const m = current.match(/^(\d{4})-(\d{2})-(\d{2})$/);
    state = m
      ? { year: Number(m[1]), month: Number(m[2]), day: Number(m[3]) }
      : { year: Number(nowM[1]), month: Number(nowM[2]), day: null };
  } else if (mode === 'time') {
    // HH:MM:SS 初始值（真实 PostgREST time 列形状）按 HH:MM 打开选择器，
    // 避免已有窗口在编辑时回退为「当前时刻」初始态（批次 8 HIGH #1）。
    const m = current.match(/^(\d{2}):(\d{2})(?::\d{2})?$/);
    state = { hour: m ? m[1] : nowM[4], minute: m ? m[2] : nowM[5] };
  } else {
    const m = current.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/);
    state = m
      ? { year: Number(m[1]), month: Number(m[2]), day: Number(m[3]), hour: m[4], minute: m[5] }
      : { year: Number(nowM[1]), month: Number(nowM[2]), day: null, hour: nowM[4], minute: nowM[5] };
  }

  const pop = document.createElement('div');
  pop.className = `retro-time-pop${mode === 'time' ? ' is-time-only' : ''}`;
  pop._forInput = input;
  pop.innerHTML = `
    ${showCalendar ? `
    <div class="retro-time-head">
      <div class="retro-time-nav">
        <button type="button" data-nav="year-" title="上一年">${icon('chevron-left')}${icon('chevron-left')}</button>
        <button type="button" data-nav="month-" title="上一月">${icon('chevron-left')}</button>
      </div>
      <span class="retro-time-title"></span>
      <div class="retro-time-nav">
        <button type="button" data-nav="month+" title="下一月">${icon('chevron-right')}</button>
        <button type="button" data-nav="year+" title="下一年">${icon('chevron-right')}${icon('chevron-right')}</button>
      </div>
    </div>
    <div class="retro-time-week">
      <span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span><span>日</span>
    </div>
    <div class="retro-time-grid"></div>` : ''}
    ${showTimeRow ? `
    <div class="retro-time-time">
      <div class="rtp-unit" data-unit="hour" title="小时"></div>
      <span class="rtp-colon">:</span>
      <div class="rtp-unit" data-unit="minute" title="分钟"></div>
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
  activeRetroTimePop = pop;

  const grid = pop.querySelector('.retro-time-grid');
  let hourPick = null;
  let minutePick = null;
  if (showTimeRow) {
    hourPick = createRtpUnit(
      pop.querySelector('[data-unit="hour"]'),
      Array.from({ length: 24 }, (_, h) => pad2(h)),
      state.hour,
      (v) => { state.hour = v; },
    );
    minutePick = createRtpUnit(
      pop.querySelector('[data-unit="minute"]'),
      Array.from({ length: 60 }, (_, m) => pad2(m)),
      state.minute,
      (v) => { state.minute = v; },
    );
    // 点击弹层内其他区域时收起打开的时/分下拉（点在单元内交给其自身开关处理）
    pop.addEventListener('mousedown', (e) => {
      pop.querySelectorAll('.rtp-unit').forEach((unit) => {
        if (unit.contains(e.target)) return;
        const list = unit.querySelector('.rtp-select-pop');
        if (list && !list.hidden) {
          list.hidden = true;
          unit.classList.remove('is-open');
        }
      });
    });
  }

  let titleEl = null;
  if (showCalendar) {
    titleEl = pop.querySelector('.retro-time-title');
    const renderGrid = () => {
      titleEl.textContent = `${state.year}年${state.month}月`;
      grid.innerHTML = '';
      const first = new Date(Date.UTC(state.year, state.month - 1, 1));
      // 周一为一周之首：getUTCDay() 周日=0 → 位移 (day+6)%7
      const lead = (first.getUTCDay() + 6) % 7;
      for (let i = 0; i < lead; i++) grid.insertAdjacentHTML('beforeend', '<span></span>');
      const daysInMonth = new Date(Date.UTC(state.year, state.month, 0)).getUTCDate();
      const todayStr = nowText.slice(0, 10);
      for (let d = 1; d <= daysInMonth; d++) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = String(d);
        const dateStr = `${state.year}-${pad2(state.month)}-${pad2(d)}`;
        if (state.day === d) btn.classList.add('is-selected');
        if (dateStr === todayStr) btn.classList.add('is-today');
        btn.addEventListener('click', () => {
          state.day = d;
          grid.querySelectorAll('.is-selected').forEach((el) => el.classList.remove('is-selected'));
          btn.classList.add('is-selected');
        });
        grid.appendChild(btn);
      }
    };
    renderGrid();

    pop.querySelectorAll('[data-nav]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const step = btn.dataset.nav;
        if (step === 'month-') { state.month -= 1; if (state.month < 1) { state.month = 12; state.year -= 1; } }
        if (step === 'month+') { state.month += 1; if (state.month > 12) { state.month = 1; state.year += 1; } }
        if (step === 'year-') state.year -= 1;
        if (step === 'year+') state.year += 1;
        state.day = null;
        renderGrid();
      });
    });
    if (mode === 'date') {
      pop.querySelector('[data-act="today"]').addEventListener('click', () => {
        state.year = Number(nowM[1]); state.month = Number(nowM[2]); state.day = Number(nowM[3]);
        renderGrid();
      });
    }
  }

  pop.querySelector('[data-act="clear"]').addEventListener('click', () => {
    if (showCalendar) state.day = null;
    apply('');
    closeRetroTimePop();
  });
  if (mode === 'datetime') {
    pop.querySelector('[data-act="now"]').addEventListener('click', () => {
      const now = nowShanghaiLocalInput().match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/);
      state.year = Number(now[1]); state.month = Number(now[2]); state.day = Number(now[3]);
      state.hour = now[4]; state.minute = now[5];
      renderGrid();
      hourPick.set(state.hour);
      minutePick.set(state.minute);
    });
  }
  pop.querySelector('[data-act="ok"]').addEventListener('click', () => {
    if (mode === 'time') {
      apply(`${state.hour}:${state.minute}`);
      closeRetroTimePop();
      return;
    }
    if (!state.day) {
      apply('');
      closeRetroTimePop();
      return;
    }
    const date = `${state.year}-${pad2(state.month)}-${pad2(state.day)}`;
    apply(mode === 'date' ? date : `${date}T${state.hour}:${state.minute}`);
    closeRetroTimePop();
  });

  // 定位：先把字段滚入视口，再放字段正下方；下方放不下翻到上方，
  // 最终双向夹紧到视口内（字段被表单滚动移出视口时也不会漂出屏幕）。
  // align="right" 时右缘对齐字段（时钟图标一侧），宽字段下弹层贴着图标；
  // 窄字段回退左对齐，避免弹层伸到字段左侧之外。
  anchor.scrollIntoView({ block: 'nearest' });
  const rect = anchor.getBoundingClientRect();
  const popRect = pop.getBoundingClientRect();
  let left = align === 'right'
    ? Math.max(rect.right - popRect.width, rect.left)
    : rect.left;
  let top = rect.bottom + 6;
  if (top + popRect.height > window.innerHeight - 8) {
    top = rect.top - popRect.height - 6;
  }
  top = Math.min(Math.max(top, 8), Math.max(8, window.innerHeight - popRect.height - 8));
  left = Math.min(Math.max(left, 8), Math.max(8, window.innerWidth - popRect.width - 8));
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;

  const onOutside = (e) => {
    if (!pop.contains(e.target) && !anchor.contains(e.target)) closeRetroTimePop();
  };
  const onKey = (e) => { if (e.key === 'Escape') closeRetroTimePop(); };
  // 视口变化后固定定位不再贴合字段，直接关闭，避免弹层漂移出屏；
  // 打开瞬间的 scrollIntoView 自身引发的滚动豁免 300ms，否则弹层刚开即关。
  // 弹层内部列表（时/分下拉）自身的滚动不在此列——那不是视口变化。
  const openedAt = Date.now();
  const onViewportChange = (e) => {
    if (Date.now() - openedAt < 300) return;
    if (e && e.type === 'scroll' && e.target !== window && e.target !== document
      && pop.contains(e.target)) return;
    closeRetroTimePop();
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
    if (!document.contains(anchor)) closeRetroTimePop();
  });
  rootObserver.observe(document.body, { childList: true });
  const prevCleanup = pop._cleanup;
  pop._cleanup = () => { prevCleanup(); rootObserver.disconnect(); };
}
