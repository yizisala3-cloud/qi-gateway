import { gw, query, esc } from '../api.js?v=20260802-memory-review2';
import { loading, empty, badge, toast, modal, delegate } from '../ui.js?v=20260802-memory-review2';

function fmtDate(value) {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function statusBadge(status) {
  const type = status === 'approved' || status === 'merged'
    ? 'accent'
    : status === 'rejected'
      ? 'danger'
      : 'warn';
  return badge(esc(status || 'pending'), type);
}

async function submitReview(id, payload) {
  return gw(`/admin/api/memory-requests/${encodeURIComponent(id)}/review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export default {
  state: { status: 'pending' },

  async mount(root) {
    this.root = root;
    this.renderShell();
    delegate(root, {
      refresh: () => this.loadList(),
      approve: (el) => this.openApprove(el.dataset.id),
      reject: (el) => this.openReject(el.dataset.id),
    });
    root.querySelector('#request-status')?.addEventListener('change', (event) => {
      this.state.status = event.target.value;
      this.loadList();
    });
    await this.loadList();
  },

  renderShell() {
    this.root.innerHTML = `
      <div class="banner">
        <span>🧠</span>
        <div>AI 提交的内容只会先进入申请队列。通过后才会写入正式记忆并参与召回；拒绝记录会保留用于审计。</div>
      </div>
      <div class="toolbar">
        <select id="request-status" style="width:150px">
          <option value="pending">待审核</option>
          <option value="approved">已通过</option>
          <option value="rejected">已拒绝</option>
          <option value="merged">已合并</option>
          <option value="">全部状态</option>
        </select>
        <button class="btn btn-secondary" data-act="refresh">刷新</button>
      </div>
      <div id="request-list">${loading()}</div>
    `;
  },

  async loadList() {
    const list = this.root.querySelector('#request-list');
    list.innerHTML = loading();
    try {
      const eq = this.state.status ? { status: this.state.status } : {};
      const rows = await query('memory_requests', {
        select: 'id,assistant_id,conversation_id,source_message_id,content,title,tags,importance,reason,status,source,memory_id,memory_key,update_mode,created_at,reviewed_at,reviewed_by,review_note',
        order: { col: 'created_at', asc: false },
        limit: 100,
        eq,
      });
      if (!rows.length) {
        list.innerHTML = empty(this.state.status === 'pending' ? '当前没有待审核的记忆申请' : '没有符合条件的申请');
        return;
      }
      list.innerHTML = rows.map((row) => `
        <div class="item ${row.status === 'rejected' ? 'item-dim' : ''}">
          <div class="item-row">
            <div style="flex:1;min-width:0">
              <div class="item-title">#${row.id} · ${esc(row.title || '未命名记忆')}</div>
              <div class="mt8">${esc(row.content || '')}</div>
              <div class="text-sm muted mt8">申请理由：${esc(row.reason || '-')}</div>
              ${row.review_note ? `<div class="text-sm muted mt8">审核备注：${esc(row.review_note)}</div>` : ''}
              <div class="btn-row mt8">
                ${statusBadge(row.status)}
                ${badge(`重要性 ${Number(row.importance) || 5}`, Number(row.importance) >= 8 ? 'purple' : 'muted')}
                ${row.update_mode === 'replace' ? badge('替换更新', 'purple') : badge('新增记忆', 'muted')}
                ${row.memory_key ? badge(`key: ${esc(row.memory_key)}`, 'info') : ''}
                ${(row.tags || []).map((tag) => badge(esc(tag), 'info')).join('')}
                ${badge(esc(row.source || 'unknown'), 'muted')}
              </div>
              <div class="text-sm muted mt8">
                assistant: <span class="mono">${esc(row.assistant_id || '-')}</span>
                · source message: ${esc(row.source_message_id ?? '-')}
                · 申请于 ${esc(fmtDate(row.created_at))}
                ${row.reviewed_at ? ` · 审核于 ${esc(fmtDate(row.reviewed_at))}` : ''}
                ${row.memory_id ? ` · memory #${esc(row.memory_id)}` : ''}
              </div>
            </div>
            ${row.status === 'pending' ? `
              <div class="item-actions">
                <button class="btn btn-xs btn-soft" data-act="approve" data-id="${row.id}">编辑并通过</button>
                <button class="btn btn-xs btn-danger-soft" data-act="reject" data-id="${row.id}">拒绝</button>
              </div>` : ''}
          </div>
        </div>
      `).join('');
    } catch (error) {
      list.innerHTML = `<div class="banner banner-danger">${esc(error.message)}</div>`;
    }
  },

  async getRequest(id) {
    const rows = await query('memory_requests', {
      eq: { id: Number(id) },
      limit: 1,
    });
    if (!rows[0]) throw new Error('申请不存在或已被删除');
    return rows[0];
  },

  async openApprove(id) {
    let request;
    try {
      request = await this.getRequest(id);
    } catch (error) {
      toast(error.message, 'err');
      return;
    }
    if (request.status !== 'pending') {
      toast('这条申请已经审核过了', 'err');
      await this.loadList();
      return;
    }

    const { root, close } = modal({
      title: `编辑并通过申请 #${id}`,
      body: `
        <div class="field"><label>标题</label><input type="text" id="review-title" maxlength="100" value="${esc(request.title || '')}"></div>
        <div class="field"><label>记忆内容</label><textarea id="review-content" rows="7" maxlength="600">${esc(request.content || '')}</textarea></div>
        <div class="field"><label>标签（逗号分隔，最多 5 个）</label><input type="text" id="review-tags" value="${esc((request.tags || []).join(', '))}"></div>
        <div class="field"><label>重要性（1-10）</label><input type="number" id="review-importance" min="1" max="10" value="${Number(request.importance) || 5}"></div>
        <div class="field"><label>写入方式</label><select id="review-update-mode"><option value="append" ${request.update_mode !== 'replace' ? 'selected' : ''}>新增独立记忆</option><option value="replace" ${request.update_mode === 'replace' ? 'selected' : ''}>替换同一可变事实的旧版本</option></select></div>
        <div class="field"><label>稳定主题键（替换时必填）</label><input type="text" id="review-memory-key" maxlength="120" value="${esc(request.memory_key || '')}" placeholder="例如 project.qi-gateway.progress"><div class="text-sm muted mt8">同一个进度、状态或位置后续更新必须使用完全相同的键。普通相似内容不要使用替换。</div></div>
        <div class="field"><label>审核备注（可选）</label><textarea id="review-note" rows="3" maxlength="500"></textarea></div>
      `,
      footer: '<button class="btn btn-secondary" data-cancel>取消</button><button class="btn btn-primary" data-save>通过并写入记忆</button>',
    });
    root.querySelector('[data-cancel]').onclick = close;
    root.querySelector('[data-save]').onclick = async (event) => {
      const content = root.querySelector('#review-content').value.trim();
      const importance = Number(root.querySelector('#review-importance').value);
      const updateMode = root.querySelector('#review-update-mode').value;
      const memoryKey = root.querySelector('#review-memory-key').value.trim().toLowerCase().replace(/\s+/g, '-');
      if (content.length < 5) {
        toast('记忆内容至少需要 5 个字符', 'err');
        return;
      }
      if (!Number.isInteger(importance) || importance < 1 || importance > 10) {
        toast('重要性必须是 1 到 10 的整数', 'err');
        return;
      }
      if (updateMode === 'replace' && !/^[a-z0-9][a-z0-9._:/-]{2,119}$/.test(memoryKey)) {
        toast('替换模式必须填写 3-120 位有效稳定主题键', 'err');
        return;
      }
      if (updateMode === 'append' && memoryKey) {
        toast('新增独立记忆时请清空稳定主题键', 'err');
        return;
      }
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await submitReview(id, {
          action: 'approve',
          title: root.querySelector('#review-title').value.trim(),
          content,
          tags: root.querySelector('#review-tags').value.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean),
          importance,
          update_mode: updateMode,
          memory_key: memoryKey || null,
          review_note: root.querySelector('#review-note').value.trim(),
        });
        toast('申请已通过，正式记忆已写入');
        close();
        await this.loadList();
      } catch (error) {
        toast(`审核失败：${error.message}`, 'err');
        button.disabled = false;
      }
    };
  },

  openReject(id) {
    const { root, close } = modal({
      title: `拒绝申请 #${id}`,
      body: '<div class="field"><label>拒绝原因（可选）</label><textarea id="reject-note" rows="4" maxlength="500" placeholder="该记录会保留在审核历史中"></textarea></div>',
      footer: '<button class="btn btn-secondary" data-cancel>取消</button><button class="btn btn-danger" data-reject>确认拒绝</button>',
    });
    root.querySelector('[data-cancel]').onclick = close;
    root.querySelector('[data-reject]').onclick = async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await submitReview(id, {
          action: 'reject',
          review_note: root.querySelector('#reject-note').value.trim(),
        });
        toast('申请已拒绝');
        close();
        await this.loadList();
      } catch (error) {
        toast(`审核失败：${error.message}`, 'err');
        button.disabled = false;
      }
    };
  },
};

