// Reminder state persists across mounts of the cached planning page.
import { modal, toast, esc, icon } from '../ui.js?v=20261007-planning-batch3';
import { fmtClock } from './planning_display.js?v=20261007-planning-batch3';

// 闹钟错过太久就静默跳过（只对未来 2 分钟内与刚过期的情况响铃）
const ALARM_GRACE_MS = 2 * 60 * 1000;

const ALARM_URL = '/admin/assets/audio/alarm-clock.mp3';
const TIMER_URL = '/admin/assets/audio/timer-done.ogg';

// 首个手势解锁业务元素用的静音素材（#33，20261007-planning-batch3）：内联 data URI
// 静音 WAV（16-bit PCM 8kHz 单声道 100ms 全零采样），不经网络、无冷加载窗口。
// WebKit / Safari 的自动播放授权按媒体元素管理：元素本人在用户手势内开始过
// 播放，之后无手势的 play() 才会放行，授权不跨元素共享。因此首个手势直接在
// 业务闹钟 / 计时器元素上播放本素材完成各自解锁——素材全零采样，即使内核
// 忽略任何静音手段也结构上不可听，不存在「静音标记被忽略而泄漏真实铃声」
// 的路径；解锁结算后把元素音源挂到真实铃声预加载（换源与 load 不需要手势、
// 不出声），到点起播不被冷加载拖慢。不使用任何独立预热元素——那会在
// WebKit 下留下「预热元素已授权、业务元素仍被拒」的路径（#33 根因）。
const SILENT_WAV = 'data:audio/wav;base64,UklGRsQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';

const RING_BLOCKED_HINT = '浏览器拦截了自动响铃，点击「恢复响铃」按钮即可恢复';

export function createPlanningReminder() {
  return {
    alarmAudio: null,
    timerAudio: null,
    ringModal: null,
    ringItems: [],
    firedKeys: new Set(),
    permissionNoticeShown: false,
    // 在途静音解锁任务表：业务元素 -> 本次解锁令牌。结算回调凭令牌判断自己
    // 是否仍然有效，迟到的解锁结算不会误停之后开始的真实响铃（#33，同旧
    // warmJobs 语义，但令牌作用在业务元素本人身上）。
    unlockJobs: new Map(),
    // 起播被浏览器拦截后待手势恢复的提醒（#30）：{ once, epoch }，epoch 为
    // 登记时的播放代次；重试只作用于当前代次的待恢复（#29）
    pendingRecovery: null,
    recoveryHandler: null,
    // 真实起播 / 恢复重试的播放代次（#29）：停止、卸载或新提醒接管时递增，
    // 在途 play() 的迟到回调凭代次判断自己是否仍然有效，过期回调不得
    // 重新登记待恢复、重注册监听、覆盖新提醒或并播旧音轨
    ringEpoch: 0,
    // 当前代次的恢复重试在途标记：按所属操作代次设置与收尾
    recoveryAttemptEpoch: null,
    attach() {
      this.requestNotificationPermission();
      // 进页面不播放任何声音（#28）；首个手势在业务元素本人身上完成静音
      // 解锁（#33，WebKit 按元素授权），真实铃声到点直接自动播放
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
      // 首个手势解锁（#33）：静音初始化直接发生在业务闹钟 / 计时器元素本人
      // 身上——「已授权元素」必须就是「未来真实响铃元素」，不依赖任何
      // 「一个元素解锁、其他元素共享授权」的假设；响铃弹窗存在（正在响或
      // 待恢复）时不预热，避免与手势恢复交叠。
      if (this.ringModal) return;
      this.ensureAudio();
      this.warmBusinessAudio(this.alarmAudio, ALARM_URL, true);
      this.warmBusinessAudio(this.timerAudio, TIMER_URL, false);
    },

    // 业务元素静音解锁（#33）：解锁播放固定使用内联静音 WAV（全零采样，
    // 结构上不可听，不依赖 muted / volume 等可被内核忽略的静音手段）。即使
    // 元素此前已被真实铃声占用（未解锁时的到点尝试会先挂上铃声），也先把
    // 音源换回静音素材再播，杜绝手势内泄漏真实铃声或其首帧；解锁成功后把
    // 元素挂回自己的真实铃声预加载（换源与 load 不需要手势、不出声）。
    // 已解锁元素直接跳过，重复手势零播放。
    warmBusinessAudio(audio, ringUrl, loop) {
      if (audio.unlocked) return;
      const job = Symbol('unlock');
      this.unlockJobs.set(audio, job);
      audio.loop = false;
      audio.ringSource = null;
      audio.src = SILENT_WAV;
      audio.play().then(() => {
        if (this.unlockJobs.get(audio) !== job) return;  // 已被真实响铃或停止接管
        audio.pause();
        audio.currentTime = 0;
        audio.unlocked = true;
        this.unlockJobs.delete(audio);
        this.armRingSource(audio, ringUrl, loop);
      }).catch(() => {
        if (this.unlockJobs.get(audio) !== job) return;
        this.unlockJobs.delete(audio);
        console.warn('[planning_reminder] 静音解锁被拒绝，到点响铃可能需要一次页面点击恢复');
      });
    },

    // 把元素音源挂到指定真实铃声并预加载；loop 由铃声种类决定：闹钟循环、
    // 计时器单次。已挂同一铃声时跳过，不重复打断加载。
    armRingSource(audio, ringUrl, loop) {
      if (audio.ringSource === ringUrl) return;
      audio.loop = loop;
      audio.src = ringUrl;
      audio.ringSource = ringUrl;
      try { audio.load(); } catch { /* 预加载失败不影响到点播放 */ }
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
      // 业务元素创建即挂静音音源（#33）：解锁前元素上不存在真实铃声，杜绝
      // 任何「手势内短暂播出真实铃声首帧」的路径；真实铃声只在解锁结算或
      // 到点起播时经 armRingSource 挂载。
      if (!this.alarmAudio) {
        this.alarmAudio = new Audio(SILENT_WAV);
        this.alarmAudio.ringSource = null;
      }
      if (!this.timerAudio) {
        this.timerAudio = new Audio(SILENT_WAV);
        this.timerAudio.ringSource = null;
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
        const { root, close, mask } = modal({
          title: '提醒',
          body: '<div data-ring-list></div>' +
            '<p class="muted text-sm" data-ring-hint hidden>浏览器拦截了自动响铃，点击「恢复响铃」即可恢复</p>',
          footer:
            `<button class="btn btn-primary" data-act-ring-recover hidden>恢复响铃</button>` +
            `<button class="btn btn-danger" data-act-ring-stop>${icon('x')}停止响铃</button>`,
          // 右上角 × 与遮罩关闭都接管为 stopRinging（#29）：任何关闭入口都
          // 停止音频并清理提醒状态，而不是只移除弹窗 DOM
          onMaskClose: () => this.stopRinging(),
        });
        root.querySelector('[data-act-ring-stop]').onclick = () => this.stopRinging();
        root.querySelector('[data-act-ring-recover]').onclick = () => this.retryPendingRing();
        root.querySelector('.modal-close').onclick = () => this.stopRinging();
        this.ringModal = { root, close, mask, listEl: root.querySelector('[data-ring-list]') };
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
      // 新提醒接管（#29）：递增播放代次使旧起播 / 恢复回调全部失效，并立即
      // 取消上一代待恢复数据、监听与入口——新音轨结算前不允许旧恢复入口起播
      this.ringEpoch += 1;
      this.pendingRecovery = null;
      this.recoveryAttemptEpoch = null;
      this.removeRecoveryListener();
      this.syncRecoveryEntry();
      const epoch = this.ringEpoch;
      const audio = once ? this.timerAudio : this.alarmAudio;
      // 到点起播前先把音源挂到真实铃声（元素未解锁时仍在静音素材上；换源与
      // load 不需要手势），随后 play；被浏览器拒绝仍走待恢复兜底（#30）
      this.armRingSource(audio, once ? TIMER_URL : ALARM_URL, !once);
      audio.currentTime = 0;
      audio.play().then(() => {
        if (epoch !== this.ringEpoch) return;  // 已被接管或停止：迟到成功不改写状态
        // 真实起播成功：此前失败的待恢复提醒不再需要手势
        this.pendingRecovery = null;
        this.removeRecoveryListener();
        this.syncRecoveryEntry();
      }).catch(() => {
        if (epoch !== this.ringEpoch) return;  // 主动取消 / 被取代的迟到拒绝不进入恢复路径
        // 起播被拦截（#30）：登记属于当前代次的待恢复提醒并显示弹窗内恢复
        // 入口，不重复创建弹窗或通知
        this.pendingRecovery = { once, epoch };
        this.ensureRecoveryListener();
        this.syncRecoveryEntry();
        toast(RING_BLOCKED_HINT);
      });
    },

    // 恢复入口与提示行只反映当前待恢复状态；播放代次回调结算后同步显隐
    syncRecoveryEntry() {
      if (!this.ringModal) return;
      const recover = this.ringModal.root.querySelector('[data-act-ring-recover]');
      const hint = this.ringModal.root.querySelector('[data-ring-hint]');
      if (!recover || !hint) return;
      recover.hidden = !this.pendingRecovery;
      hint.hidden = !this.pendingRecovery;
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
      // 恢复数据与操作绑定所属播放身份（#29）：只重试当前代次的待恢复；
      // 当前代次已有恢复尝试在途时等待其结算
      if (!pending || pending.epoch !== this.ringEpoch || this.recoveryAttemptEpoch === this.ringEpoch) return;
      // 弹窗遮罩内的按下交给弹窗自身控件（#30）：恢复走「恢复响铃」按钮，
      // 停止 / × / 遮罩保持关闭语义，任何关闭入口都不能先恢复再停止
      const mask = this.ringModal?.mask;
      if (mask && event?.target && typeof mask.contains === 'function' && mask.contains(event.target)) return;
      const epoch = this.ringEpoch;
      const audio = pending.once ? this.timerAudio : this.alarmAudio;
      this.ensureAudio();
      audio.currentTime = 0;
      this.recoveryAttemptEpoch = epoch;
      try {
        await audio.play();
        if (epoch !== this.ringEpoch) return;  // 期间被接管或停止：迟到结果失效
        // 恢复播放发生在用户手势内：该元素由此获得手势授权（#33），
        // 后续手势不再对其重复静音解锁
        audio.unlocked = true;
        this.pendingRecovery = null;
        this.removeRecoveryListener();
        this.syncRecoveryEntry();
      } catch {
        if (epoch !== this.ringEpoch) return;  // 恢复期间被停止 / 接管：不弹过期提示
        toast(RING_BLOCKED_HINT);  // 仍被拦截：保留待恢复，可继续重试
      } finally {
        // 只收尾自己所属代次的在途标记，不动接管后新操作的标记
        if (this.recoveryAttemptEpoch === epoch) this.recoveryAttemptEpoch = null;
      }
    },

    haltAudio() {
      // 停掉当前所有业务播放，并使在途静音解锁结算失效——解锁的迟到结算
      // 不得暂停之后开始的真实响铃（#33）
      for (const audio of [this.alarmAudio, this.timerAudio]) {
        if (!audio) continue;
        try { audio.pause(); } catch { /* ignore */ }
        audio.currentTime = 0;
        this.unlockJobs.delete(audio);
      }
    },

    stopRinging() {
      this.ensureAudio();
      this.haltAudio();
      // 主动停止（#29）：递增播放代次使在途真实起播 / 恢复回调失效，
      // 主动取消引发的迟到 AbortError 不得重新登记待恢复或重注册监听
      this.ringEpoch += 1;
      // 主动停止：待恢复提醒一并取消，恢复手势失效，弹窗与条目清理
      this.pendingRecovery = null;
      this.recoveryAttemptEpoch = null;
      this.removeRecoveryListener();
      this.ringItems = [];
      if (this.ringModal) {
        this.ringModal.close();
        this.ringModal = null;
      }
    },
  };
}
