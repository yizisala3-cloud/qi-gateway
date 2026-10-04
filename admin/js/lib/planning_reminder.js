// Reminder state persists across mounts of the cached planning page.
import { modal, toast, esc, icon } from '../ui.js?v=20261004-ring-fix1';
import { fmtClock } from './planning_display.js?v=20261004-ring-fix1';

// 闹钟错过太久就静默跳过（只对未来 2 分钟内与刚过期的情况响铃）
const ALARM_GRACE_MS = 2 * 60 * 1000;

const ALARM_URL = '/admin/assets/audio/alarm-clock.mp3';
const TIMER_URL = '/admin/assets/audio/timer-done.ogg';

const RING_BLOCKED_HINT = '浏览器拦截了自动响铃，点一下页面即可恢复';

export function createPlanningReminder() {
  return {
    alarmAudio: null,
    timerAudio: null,
    ringModal: null,
    ringItems: [],
    firedKeys: new Set(),
    permissionNoticeShown: false,
    // 预热播放任务表：audio -> 本次预热令牌。结算回调凭令牌判断自己是否仍然
    // 有效，迟到的预热结算不会误停之后开始的真实响铃（#28）。
    warmJobs: new Map(),
    // 起播被浏览器拦截后待手势恢复的提醒（#30）：{ once }
    pendingRecovery: null,
    recoveryHandler: null,
    attach() {
      this.requestNotificationPermission();
      this.audioUnlockHandler = () => this.unlockAudio();
      window.addEventListener('pointerdown', this.audioUnlockHandler, { once: true });
    },

    listenForUnload() {
      window.addEventListener('beforeunload', this.onUnload = () => this.stopRinging());
    },

    dispose() {
      this.stopRinging();
      window.removeEventListener('beforeunload', this.onUnload);
      window.removeEventListener('pointerdown', this.audioUnlockHandler);
    },

    unlockAudio() {
      // 静音预热：play 只为取得自动播放授权，全程 muted 不出声（#28）。
      // 响铃弹窗存在（正在响或待恢复）时不预热，避免预热回调干扰真实提醒。
      if (this.ringModal) return;
      this.ensureAudio();
      const warm = (audio) => {
        const job = Symbol('warmup');
        this.warmJobs.set(audio, job);
        audio.muted = true;
        audio.play().then(() => {
          if (this.warmJobs.get(audio) !== job) return;  // 已被真实响铃或停止接管
          audio.pause();
          audio.currentTime = 0;
          audio.muted = false;
          this.warmJobs.delete(audio);
        }).catch(() => {
          if (this.warmJobs.get(audio) !== job) return;
          this.warmJobs.delete(audio);
          audio.muted = false;
          console.warn('[planning_reminder] 静音预热被拒绝，到点响铃可能需要一次页面点击恢复');
        });
      };
      warm(this.alarmAudio);
      warm(this.timerAudio);
    },

    requestNotificationPermission() {
      if (!('Notification' in window)) return;
      if (Notification.permission === 'default') {
        Notification.requestPermission().then((permission) => {
          if (permission === 'denied') toast('通知权限被拒，到点只在页面内响铃');
        });
      } else if (Notification.permission === 'denied' && !this.permissionNoticeShown) {
        this.permissionNoticeShown = true;
        toast('通知权限被拒，到点只在页面内响铃');
      }
    },

    ensureAudio() {
      if (!this.alarmAudio) {
        this.alarmAudio = new Audio(ALARM_URL);
        this.alarmAudio.loop = true;
      }
      if (!this.timerAudio) {
        this.timerAudio = new Audio(TIMER_URL);
      }
    },

    notify(title, body) {
      try {
        if ('Notification' in window && Notification.permission === 'granted') {
          new Notification(title, { body });
        }
      } catch { /* 通知失败不影响页面内响铃 */ }
    },

    checkAlarms(board) {
      const now = Date.now();
      for (const occ of board.progress) {
        if (occ.alarm_start && occ.est_start) {
          this.fireAlarm(occ, 'start', '待办开始', occ.content, occ.est_start, now);
        }
        if (occ.alarm_end && occ.est_end) {
          this.fireAlarm(occ, 'end', '待办时间到', occ.content, occ.est_end, now);
        }
        if (occ.timer_minutes && occ.status === 'in_progress' && occ.actual_start) {
          const due = new Date(occ.actual_start).getTime() + occ.timer_minutes * 60 * 1000;
          this.fireAlarm(occ, 'timer', '计时器时间到', occ.content, new Date(due).toISOString(), now, true);
        }
      }
    },

    fireAlarm(occ, kind, title, body, dueIso, nowMs, once = false) {
      const key = `${occ.id}:${kind}:${dueIso}`;
      if (this.firedKeys.has(key)) return;
      const due = new Date(dueIso).getTime();
      if (Number.isNaN(due)) return;
      if (nowMs - due > ALARM_GRACE_MS) {
        this.firedKeys.add(key);  // 错过太久，静默跳过
        return;
      }
      if (due > nowMs) return;  // 还没到点，等下次轮询
      this.firedKeys.add(key);
      this.notify(`${title}：${body}`, fmtClock(dueIso));
      this.showRingModal(`${title}：${body}`, fmtClock(dueIso), once);
    },

    showRingModal(message, timeText, once = false) {
      this.ensureAudio();
      // 同刻 / 先后到点的多项提醒复用同一个弹窗（#31）：条目合并进列表展示，
      // 不再另开新弹窗，避免旧弹窗脱离清理跟踪、旧按钮误操作新提醒。
      if (!this.ringModal) {
        const { root, close } = modal({
          title: '提醒',
          body: '<div data-ring-list></div>',
          footer: `<button class="btn btn-danger" data-act-ring-stop>${icon('x')}停止响铃</button>`,
          // 右上角 × 与遮罩关闭都接管为 stopRinging（#29）：任何关闭入口都
          // 停止音频并清理提醒状态，而不是只移除弹窗 DOM
          onMaskClose: () => this.stopRinging(),
        });
        root.querySelector('[data-act-ring-stop]').onclick = () => this.stopRinging();
        root.querySelector('.modal-close').onclick = () => this.stopRinging();
        this.ringModal = { root, close, listEl: root.querySelector('[data-ring-list]') };
      }
      this.ringItems.push({ message, timeText, once });
      this.renderRingList();
      this.startRingAudio(once);
    },

    renderRingList() {
      if (!this.ringModal) return;
      this.ringModal.listEl.innerHTML = this.ringItems.map((item) =>
        `<div class="ring-item"><p class="confirm-text">${esc(item.message)}</p>` +
        `<p class="muted text-sm">${esc(item.timeText)}${item.once ? ' · 计时结束' : ' · 将循环响铃直到点掉'}</p></div>`
      ).join('<hr class="ring-divider">');
    },

    startRingAudio(once) {
      this.ensureAudio();
      this.haltAudio();
      const audio = once ? this.timerAudio : this.alarmAudio;
      audio.muted = false;
      audio.currentTime = 0;
      audio.play().then(() => {
        // 真实起播成功：此前失败的待恢复提醒不再需要手势
        this.pendingRecovery = null;
        this.removeRecoveryListener();
      }).catch(() => {
        // 起播被拦截（#30）：登记待恢复提醒，等下一次有效手势重试；
        // 不重复创建弹窗或通知，只提示恢复方式
        this.pendingRecovery = { once };
        this.ensureRecoveryListener();
        toast(RING_BLOCKED_HINT);
      });
    },

    ensureRecoveryListener() {
      if (this.recoveryHandler) return;
      this.recoveryHandler = (event) => this.retryPendingRing(event);
      window.addEventListener('pointerdown', this.recoveryHandler);
    },

    removeRecoveryListener() {
      if (!this.recoveryHandler) return;
      window.removeEventListener('pointerdown', this.recoveryHandler);
      this.recoveryHandler = null;
    },

    async retryPendingRing(event) {
      const pending = this.pendingRecovery;
      if (!pending) return;
      // 弹窗内的按下（停止按钮等）不算恢复手势，避免停止前误起播放
      const root = this.ringModal?.root;
      if (root && event?.target && typeof root.contains === 'function' && root.contains(event.target)) return;
      const audio = pending.once ? this.timerAudio : this.alarmAudio;
      this.ensureAudio();
      audio.muted = false;
      audio.currentTime = 0;
      try {
        await audio.play();
        if (this.pendingRecovery !== pending) return;  // 期间被新提醒接管
        this.pendingRecovery = null;
        this.removeRecoveryListener();
      } catch {
        toast(RING_BLOCKED_HINT);
      }
    },

    haltAudio() {
      // 停掉当前所有播放（含静音预热）并使预热结算回调失效
      for (const audio of [this.alarmAudio, this.timerAudio]) {
        if (!audio) continue;
        try { audio.pause(); } catch { /* ignore */ }
        audio.currentTime = 0;
        audio.muted = false;
        this.warmJobs.delete(audio);
      }
    },

    stopRinging() {
      this.ensureAudio();
      this.haltAudio();
      // 主动停止：待恢复提醒一并取消，恢复手势失效，弹窗与条目清理
      this.pendingRecovery = null;
      this.removeRecoveryListener();
      this.ringItems = [];
      if (this.ringModal) {
        this.ringModal.close();
        this.ringModal = null;
      }
    },
  };
}
