// Reminder state persists across mounts of the cached planning page.
import { modal, toast, esc, icon } from '../ui.js?v=20261003-memo-bugfix2';
import { fmtClock } from './planning_display.js?v=20261003-memo-bugfix2';

// 闹钟错过太久就静默跳过（只对未来 2 分钟内与刚过期的情况响铃）
const ALARM_GRACE_MS = 2 * 60 * 1000;

const ALARM_URL = '/admin/assets/audio/alarm-clock.mp3';
const TIMER_URL = '/admin/assets/audio/timer-done.ogg';

export function createPlanningReminder() {
  return {
    alarmAudio: null,
    timerAudio: null,
    ringModal: null,
    firedKeys: new Set(),
    permissionNoticeShown: false,
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
      // 静音 play + pause 预热一次，之后到点的响铃不再被 autoplay 策略拦下
      this.ensureAudio();
      const warm = (audio) => {
        audio.play().then(() => {
          audio.pause();
          audio.currentTime = 0;
        }).catch(() => {});
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
      this.stopRinging({ keepModal: true });
      const { root, close } = modal({
        title: '提醒',
        body: `<p class="confirm-text">${esc(message)}</p><p class="muted text-sm">${esc(timeText)}${once ? ' · 计时结束' : ' · 将循环响铃直到点掉'}</p>`,
        footer: `<button class="btn btn-danger" data-act-ring-stop>${icon('x')}停止响铃</button>`,
      });
      root.querySelector('[data-act-ring-stop]').onclick = () => {
        this.stopRinging();
        close();
      };
      this.ringModal = { root, close };
      if (once) {
        this.timerAudio.currentTime = 0;
        this.timerAudio.play().catch(() => toast('浏览器拦截了自动响铃，点一下页面即可恢复'));
      } else {
        this.alarmAudio.play().catch(() => toast('浏览器拦截了自动响铃，点一下页面即可恢复'));
      }
    },

    stopRinging({ keepModal = false } = {}) {
      this.ensureAudio();
      try { this.alarmAudio.pause(); } catch { /* ignore */ }
      try { this.timerAudio.pause(); } catch { /* ignore */ }
      this.alarmAudio.currentTime = 0;
      if (!keepModal && this.ringModal) {
        this.ringModal.close();
        this.ringModal = null;
      }
    },
  };
}
