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
    : status === 'rejected' || status === 'conflict'
      ? 'danger'
      : status === 'duplicate'
        ? 'muted'
      : 'warn';
  return badge(esc(status || 'pending'), type);
}

function relationLabel(action) {
  return {
    merge: '合并到现有记忆',
    duplicate: '标记为重复',
    conflict: '标记为冲突',
  }[action] || action;
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
      merge: (el) => this.openRelation(el.dataset.id, 'merge'),
      duplicate: (el) => this.openRelation(el.dataset.id, 'duplicate'),
      conflict: (el) => this.openRelation(el.dataset.id, 'conflict'),
      history: (el) => this.openHistory(el.dataset.id),
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
          <option value="duplicate">重复</option>
          <option value="conflict">冲突待处理</option>
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
        select: 'id,assistant_id,conversation_id,source_message_id,content,title,tags,importance,reason,status,source,memory_id,memory_key,update_mode,related_memory_id,created_at,reviewed_at,reviewed_by,review_note',
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
                ${row.related_memory_id ? badge(`关联 memory #${esc(row.related_memory_id)}`, row.status === 'conflict' ? 'warn' : 'info') : ''}
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
            ${row.status === 'pending' || row.status === 'conflict' ? `
              <div class="item-actions">
                <button class="btn btn-xs btn-soft" data-act="approve" data-id="${row.id}">编辑并通过</button>
                <button class="btn btn-xs btn-secondary" data-act="merge" data-id="${row.id}">人工合并</button>
                <button class="btn btn-xs btn-secondary" data-act="duplicate" data-id="${row.id}">标记重复</button>
                ${row.status === 'pending' ? `<button class="btn btn-xs btn-danger-soft" data-act="conflict" data-id="${row.id}">标记冲突</button>` : ''}
                <button class="btn btn-xs btn-danger-soft" data-act="reject" data-id="${row.id}">拒绝</button>
                ${row.reviewed_at ? `<button class="btn btn-xs btn-secondary" data-act="history" data-id="${row.id}">审核记录</button>` : ''}
              </div>` : row.reviewed_at ? `
              <div class="item-actions"><button class="btn btn-xs btn-secondary" data-act="history" data-id="${row.id}">审核记录</button></div>` : ''}
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
    if (!['pending', 'conflict'].includes(request.status)) {
      toast('这条申请已经审核过了', 'err');
      await this.loadList();
      return;
    }

    const { root, close } = modal({
      title: `${request.status === 'conflict' ? '解决冲突并通过' : '编辑并通过'}申请 #${id}`,
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

  async openRelation(id, action) {
    let request;
    try {
      request = await this.getRequest(id);
    } catch (error) {
      toast(error.message, 'err');
      return;
    }
    if (!['pending', 'conflict'].includes(request.status)) {
      toast('这条申请已经处理完毕', 'err');
      await this.loadList();
      return;
    }

    let selected = null;
    const { root, close } = modal({
      title: `${relationLabel(action)} · 申请 #${id}`,
      body: `
        <div class="banner">
          <span>🔎</span>
          <div>必须由你明确选择一条已审核且仍有效的记忆。系统不会按相似度自动覆盖。</div>
        </div>
        <div class="field"><label>搜索现有记忆</label><div class="btn-row"><input class="grow" type="search" id="relation-search" placeholder="输入标题或内容关键词"><button class="btn btn-secondary" id="relation-search-button">搜索</button></div></div>
        <div id="relation-results">${loading()}</div>
        <div id="relation-merge-fields"></div>
        <div class="field"><label>审核备注（可选）</label><textarea id="relation-note" rows="3" maxlength="500"></textarea></div>
      `,
      footer: `<button class="btn btn-secondary" data-cancel>取消</button><button class="btn btn-primary" data-submit>${relationLabel(action)}</button>`,
    });

    const results = root.querySelector('#relation-results');
    const mergeFields = root.querySelector('#relation-merge-fields');

    const renderSelection = () => {
      if (action !== 'merge' || !selected) {
        mergeFields.innerHTML = '';
        return;
      }
      const tags = [...new Set([...(selected.tags || []), ...(request.tags || [])])].slice(0, 5);
      const mergedContent = [selected.content, request.content].filter(Boolean).join('\n');
      mergeFields.innerHTML = `
        <div class="banner banner-warn"><div>请把两条内容整理成一条自然、准确的最终记忆。保存后旧 memory #${esc(selected.id)} 会软失效，但仍保留历史链接。</div></div>
        <div class="field"><label>合并后标题</label><input type="text" id="merge-title" maxlength="100" value="${esc(request.title || selected.title || '')}"></div>
        <div class="field"><label>合并后内容</label><textarea id="merge-content" rows="8" maxlength="600">${esc(mergedContent)}</textarea></div>
        <div class="field"><label>标签（逗号分隔，最多 5 个）</label><input type="text" id="merge-tags" value="${esc(tags.join(', '))}"></div>
        <div class="field"><label>重要性（1-10）</label><input type="number" id="merge-importance" min="1" max="10" value="${Math.max(Number(selected.importance) || 5, Number(request.importance) || 5)}"></div>
      `;
    };

    const loadMemories = async () => {
      results.innerHTML = loading();
      try {
        const rows = await query('memories', {
          select: 'id,title,content,tags,importance,layer,created_at',
          order: { col: 'created_at', asc: false },
          limit: 20,
          eq: { is_active: true, verified: 'verified' },
          search: root.querySelector('#relation-search').value.trim(),
        });
        if (!rows.length) {
          results.innerHTML = empty('没有找到可关联的已审核记忆');
          return;
        }
        results.innerHTML = rows.map((memory) => `
          <button type="button" class="item relation-target" data-memory-id="${memory.id}" style="width:100%;text-align:left;cursor:pointer">
            <div class="item-title">memory #${memory.id} · ${esc(memory.title || '未命名')}</div>
            <div class="text-sm muted mt8">${esc((memory.content || '').slice(0, 220))}</div>
            <div class="btn-row mt8">${badge(`重要性 ${Number(memory.importance) || 5}`, 'muted')}${badge(esc(memory.layer || '-'), 'info')}</div>
          </button>
        `).join('');
        for (const button of results.querySelectorAll('.relation-target')) {
          button.onclick = () => {
            selected = rows.find((memory) => String(memory.id) === button.dataset.memoryId) || null;
            for (const item of results.querySelectorAll('.relation-target')) item.classList.remove('selected');
            button.classList.add('selected');
            renderSelection();
          };
        }
      } catch (error) {
        results.innerHTML = `<div class="banner banner-danger">${esc(error.message)}</div>`;
      }
    };

    root.querySelector('[data-cancel]').onclick = close;
    root.querySelector('#relation-search-button').onclick = loadMemories;
    root.querySelector('#relation-search').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        loadMemories();
      }
    });
    root.querySelector('[data-submit]').onclick = async (event) => {
      if (!selected) {
        toast('请先选择一条现有记忆', 'err');
        return;
      }
      const payload = {
        action,
        related_memory_id: Number(selected.id),
        review_note: root.querySelector('#relation-note').value.trim(),
      };
      if (action === 'merge') {
        const content = root.querySelector('#merge-content')?.value.trim() || '';
        const importance = Number(root.querySelector('#merge-importance')?.value);
        if (content.length < 5) {
          toast('合并后内容至少需要 5 个字符', 'err');
          return;
        }
        if (!Number.isInteger(importance) || importance < 1 || importance > 10) {
          toast('重要性必须是 1 到 10 的整数', 'err');
          return;
        }
        Object.assign(payload, {
          content,
          title: root.querySelector('#merge-title')?.value.trim() || '',
          tags: (root.querySelector('#merge-tags')?.value || '').split(/[,，]/).map((tag) => tag.trim()).filter(Boolean),
          importance,
        });
      }
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await submitReview(id, payload);
        toast(action === 'merge' ? '记忆已人工合并' : action === 'duplicate' ? '申请已标记为重复' : '申请已进入冲突待处理队列');
        close();
        await this.loadList();
      } catch (error) {
        toast(`审核失败：${error.message}`, 'err');
        button.disabled = false;
      }
    };
    await loadMemories();
  },

  async openHistory(id) {
    const { root, close } = modal({
      title: `申请 #${id} · 审核记录`,
      body: `<div id="review-history">${loading()}</div>`,
      footer: '<button class="btn btn-secondary" data-cancel>关闭</button>',
    });
    root.querySelector('[data-cancel]').onclick = close;
    const container = root.querySelector('#review-history');
    try {
      const rows = await query('memory_request_review_events', {
        select: 'id,request_id,action,from_status,to_status,target_memory_id,result_memory_id,reviewed_by,review_note,created_at',
        order: { col: 'created_at', asc: false },
        limit: 100,
        eq: { request_id: Number(id) },
      });
      container.innerHTML = rows.length ? rows.map((row) => `
        <div class="item">
          <div class="item-title">${esc(relationLabel(row.action))} · ${esc(row.from_status)} → ${esc(row.to_status)}</div>
          <div class="text-sm muted mt8">
            ${esc(fmtDate(row.created_at))}
            · 操作者 ${esc(row.reviewed_by || '-')}
            ${row.target_memory_id ? ` · 目标 memory #${esc(row.target_memory_id)}` : ''}
            ${row.result_memory_id ? ` · 结果 memory #${esc(row.result_memory_id)}` : ''}
          </div>
          ${row.review_note ? `<div class="mt8">${esc(row.review_note)}</div>` : ''}
        </div>
      `).join('') : empty('暂无审核事件；旧版审核记录仍保留在申请本身');
    } catch (error) {
      container.innerHTML = `<div class="banner banner-danger">${esc(error.message)}</div>`;
    }
  },
};

