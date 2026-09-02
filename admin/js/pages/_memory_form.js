// pages/_memory_form.js - 手工新增 / 完整编辑 / 修改类型 共用动态表单
// 六类连续感结构全部由本模块的中文动态表单生成，用户永不直接编辑 JSON；
// 普通标签与召回标签是两套独立控件；召回向量和 content_hash 均由服务端维护。
import { gw, esc } from '../api.js?v=20260902-adminmem2';
import { modal, confirm, toast, icon } from '../ui.js?v=20260902-adminmem2';
import { sameInstant, stableJson, buildEditPatch } from './_memory_patch.js?v=20260902-adminmem2';

/* ---------- 枚举与字段定义 ---------- */

export const CONTINUITY_TYPES = {
  moment: '近期片段',
  thread: '未完线索',
  episode: '共同经历',
  inside_joke: '内部梗',
  profile: '用户资料',
  interaction_rule: '互动规则',
};

const TYPE_INTRO = {
  moment: '一个具体时间点发生的小片段：场景里发生了什么、怎么回应、结果如何。',
  thread: '一条还没闭合的线索：待解问题、当前状态，闭合时必须补齐结束信息。',
  episode: '一段有起承转合的共同经历：开端、经过、结局与闭合质量。',
  inside_joke: '只属于你们的小暗号：来历、触发短语和共同含义。',
  profile: '对用户的一个稳定认知：侧面、描述、范围与判断依据。',
  interaction_rule: '一条明确的互动规则：触发条件、期望行为与优先级。',
};

const SOURCE_TYPE_OPTIONS = [
  { value: 'natural_chat', label: '自然聊天', desc: '来自日常对话的内容' },
  { value: 'persona_prompt', label: '人设提示', desc: '来自人设或系统提示词' },
  { value: 'code', label: '代码', desc: '来自代码或技术内容' },
  { value: 'document', label: '文档', desc: '来自文档或资料' },
  { value: 'quote', label: '引用', desc: '来自引用的语句' },
  { value: 'roleplay', label: '角色扮演', desc: '来自角色扮演对话' },
  { value: 'tool_result', label: '工具结果', desc: '来自工具执行结果' },
  { value: 'system_meta', label: '系统元数据', desc: '来自系统自身的记录' },
  { value: 'unknown', label: '未知', desc: '来源无法判断' },
];

const THREAD_STATES = [
  { value: 'open', label: '进行中' },
  { value: 'paused', label: '暂停' },
  { value: 'resolved', label: '已解决' },
  { value: 'dissolved', label: '已消解' },
  { value: 'abandoned', label: '已放弃' },
  { value: 'unknown', label: '未知' },
];
const CLOSED_THREAD_STATES = ['resolved', 'dissolved', 'abandoned'];

const TIME_PRECISIONS = [
  { value: 'minute', label: '精确到分钟' },
  { value: 'hour', label: '精确到小时' },
  { value: 'day', label: '精确到日期' },
  { value: 'approximate', label: '大概时间' },
  { value: 'unknown', label: '时间未知' },
];

const ARRAY_LIMIT = { count: 8, chars: 120 };

/* 每类字段：k=键名，label=中文名，kind=控件，req=必填，hint=用途说明，
   opts=枚举选项，cond=(state)=>是否显示（thread 结束字段专用）。 */
const TYPE_FIELDS = {
  moment: [
    { k: 'scene', label: '场景', kind: 'text', req: true, hint: '这个片段发生在什么场景里' },
    { k: 'event', label: '事件', kind: 'text', req: true, hint: '场景里发生了什么' },
    { k: 'moment_state', label: '片段状态', kind: 'select', req: true, opts: [
      { value: 'standalone', label: '独立片段' },
      { value: 'linked', label: '已关联其他记忆' },
      { value: 'absorbed', label: '已并入其他记忆' },
    ], hint: '这条片段目前处于什么状态' },
    { k: 'response', label: '回应', kind: 'text', hint: '当时是怎么回应的' },
    { k: 'outcome', label: '结果', kind: 'text', hint: '这件事最终的结果' },
    { k: 'salience_reason', label: '重要原因', kind: 'text', hint: '为什么值得记住' },
  ],
  thread: [
    { k: 'open_question', label: '待解问题', kind: 'text', req: true, hint: '这条线索想解答什么' },
    { k: 'current_state', label: '当前状态', kind: 'text', req: true, hint: '线索现在推进到哪一步' },
    { k: 'thread_state', label: '线索状态', kind: 'thread_state', req: true, hint: '选择结束状态后必须补齐结束信息' },
    { k: 'next_expected', label: '下一步预期', kind: 'text', hint: '接下来期待发生什么' },
    { k: 'closure_criteria', label: '结束条件', kind: 'array', hint: `满足什么就算闭合；最多 ${ARRAY_LIMIT.count} 项、每项不超过 ${ARRAY_LIMIT.chars} 字，用逗号分隔` },
    { k: 'opened_at', label: '开始时间', kind: 'time', hint: '线索是什么时候打开的（可空）' },
    { k: 'abstract_retrieval_hints', label: '抽象召回提示', kind: 'array', hint: '什么话题下该想起它；最多 8 项' },
    { k: 'concrete_retrieval_hints', label: '具体召回提示', kind: 'array', hint: '具体关键词提示；最多 8 项' },
    { k: 'closure_summary', label: '结束总结', kind: 'text', req: 'closed', cond: (s) => CLOSED_THREAD_STATES.includes(s), hint: '线索最终如何收尾' },
    { k: 'closure_reason', label: '结束原因', kind: 'text', req: 'closed', cond: (s) => CLOSED_THREAD_STATES.includes(s), hint: '为什么会闭合' },
    { k: 'closed_at', label: '结束时间', kind: 'time', req: 'closed', cond: (s) => CLOSED_THREAD_STATES.includes(s), hint: '线索是什么时候闭合的' },
  ],
  episode: [
    { k: 'beginning', label: '开端', kind: 'text', req: true, hint: '这段经历怎么开始' },
    { k: 'development', label: '经过', kind: 'text', req: true, hint: '中间发生了什么' },
    { k: 'outcome', label: '结局', kind: 'text', req: true, hint: '最后怎么样了' },
    { k: 'closure_quality', label: '闭合质量', kind: 'select', req: true, opts: [
      { value: 'complete', label: '完整闭合' },
      { value: 'partial', label: '部分闭合' },
      { value: 'uncertain', label: '不确定' },
    ], hint: '这段经历收尾的完整程度' },
    { k: 'turning_point', label: '转折点', kind: 'text', hint: '有没有关键的转折' },
    { k: 'aftereffect', label: '后续影响', kind: 'text', hint: '这段经历留下了什么' },
    { k: 'episode_start_time', label: '经历开始时间', kind: 'time', hint: '可空' },
    { k: 'episode_end_time', label: '经历结束时间', kind: 'time', hint: '可空' },
  ],
  inside_joke: [
    { k: 'origin', label: '来历', kind: 'text', req: true, hint: '这个梗是怎么来的' },
    { k: 'trigger_phrases', label: '触发短语', kind: 'array', req: true, hint: `说什么会想起它；最多 ${ARRAY_LIMIT.count} 项、每项不超过 ${ARRAY_LIMIT.chars} 字，用逗号分隔` },
    { k: 'shared_meaning', label: '共同含义', kind: 'text', req: true, hint: '它对你们意味着什么' },
    { k: 'usage_context', label: '适用场合', kind: 'array', hint: '什么场合可以用；最多 8 项' },
    { k: 'avoid_context', label: '回避场合', kind: 'array', hint: '什么场合不该用；最多 8 项' },
    { k: 'response_style', label: '回应风格', kind: 'text', hint: '接梗时用什么语气' },
    { k: 'first_seen_at', label: '首次出现时间', kind: 'time', hint: '可空' },
    { k: 'last_reinforced_at', label: '最近强化时间', kind: 'time', hint: '可空' },
    { k: 'reinforcement_count', label: '强化次数', kind: 'int', hint: '这个梗被用了多少次（非负整数）' },
  ],
  profile: [
    { k: 'facet', label: '侧面', kind: 'text', req: true, hint: '这是用户画像的哪个侧面' },
    { k: 'statement', label: '描述', kind: 'text', req: true, hint: '具体认知是什么' },
    { k: 'scope', label: '适用范围', kind: 'text', req: true, hint: '这个认知在什么范围内成立' },
    { k: 'stability', label: '稳定性', kind: 'select', req: true, opts: [
      { value: 'stable', label: '稳定' },
      { value: 'contextual', label: '视情境而定' },
      { value: 'provisional', label: '暂定' },
    ], hint: '这个认知有多稳定' },
    { k: 'basis', label: '判断依据', kind: 'select', req: true, opts: [
      { value: 'explicit_self_report', label: '用户明确自述' },
      { value: 'explicit_preference', label: '用户明确偏好' },
      { value: 'repeated_observation', label: '反复观察' },
      { value: 'reviewed_summary', label: '审核总结' },
    ], hint: '这个认知是怎么得来的' },
    { k: 'effective_from', label: '生效开始', kind: 'time', hint: '可空' },
    { k: 'effective_until', label: '生效结束', kind: 'time', hint: '可空' },
    { k: 'exceptions', label: '例外情况', kind: 'array', hint: '有哪些例外；最多 8 项' },
  ],
  interaction_rule: [
    { k: 'trigger', label: '触发条件', kind: 'text', req: true, hint: '什么情况下触发这条规则' },
    { k: 'expected_behavior', label: '期望行为', kind: 'text', req: true, hint: '触发后应该怎么做' },
    { k: 'scope', label: '适用范围', kind: 'text', req: true, hint: '规则在什么范围内生效' },
    { k: 'priority', label: '优先级', kind: 'int', req: true, hint: '1 到 10，数字越大越优先' },
    { k: 'rule_state', label: '规则状态', kind: 'select', req: true, opts: [
      { value: 'active', label: '生效中' },
      { value: 'revoked', label: '已撤销' },
      { value: 'superseded', label: '已被替代' },
    ], hint: '规则目前是否生效' },
    { k: 'explicit_instruction', label: '明确指令', kind: 'text', req: true, hint: '用户给过的原话或明确要求' },
    { k: 'forbidden_behavior', label: '禁止行为', kind: 'array', hint: '触发时不该做什么；最多 8 项' },
    { k: 'effective_from', label: '生效开始', kind: 'time', hint: '可空' },
    { k: 'effective_until', label: '生效结束', kind: 'time', hint: '可空' },
    { k: 'exceptions', label: '例外情况', kind: 'array', hint: '有哪些例外；最多 8 项' },
  ],
};

/* ---------- 小工具 ---------- */

function toDatetimeLocal(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function parseArrayInput(text) {
  return String(text || '')
    .split(/[,，\n]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function fieldLabel(field) {
  const reqMark = field.req ? '<span class="req-mark" title="必填">*</span>' : '';
  return `${esc(field.label)}${reqMark}`;
}

/* ---------- 标签输入控件（普通标签 / 召回标签 两套独立实例） ---------- */

function createTagInput(host, initialValues, { label, maxLen = 200 } = {}) {
  host.classList.add('tag-input');
  host.innerHTML = `
    <span class="tag-input-chips"></span>
    <input type="text" class="tag-input-editor" placeholder="${esc(label || '输入后回车添加')}">`;
  const chips = host.querySelector('.tag-input-chips');
  const editor = host.querySelector('.tag-input-editor');
  const values = [];
  (initialValues || []).forEach((v) => {
    const t = String(v || '').trim();
    if (t && !values.includes(t) && t.length <= maxLen) values.push(t);
  });

  const render = () => {
    chips.innerHTML = values.map((v) => `
      <span class="chip" data-tag="${esc(v)}">${esc(v)}<button type="button" class="chip-x" aria-label="移除标签">${icon('x')}</button></span>`).join('');
    chips.querySelectorAll('.chip-x').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        const chip = btn.closest('.chip');
        values.splice(values.indexOf(chip.dataset.tag), 1);
        render();
      });
    });
  };

  const addFromEditor = () => {
    const parts = editor.value.split(/[,，]/);
    editor.value = '';
    for (const raw of parts) {
      const tag = raw.trim();
      if (!tag) continue;
      if (tag.length > maxLen) { toast(`「${tag.slice(0, 12)}…」超过 ${maxLen} 个字符，已阻止`, 'err'); continue; }
      if (values.includes(tag)) { toast(`标签「${tag}」已存在，同一组内不允许重复`, 'err'); continue; }
      values.push(tag);
    }
    render();
  };

  editor.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      addFromEditor();
    } else if (e.key === 'Backspace' && !editor.value && values.length) {
      values.pop();
      render();
    }
  });
  editor.addEventListener('blur', addFromEditor);
  render();
  return { get: () => [...values] };
}

/* ---------- 连续感动态区块 ---------- */

function renderContinuitySection(type, data, threadState) {
  const fields = TYPE_FIELDS[type] || [];
  return fields.map((f) => {
    if (f.cond && !f.cond(threadState)) return '';
    const value = data ? data[f.k] : undefined;
    const id = `cf-${f.k}`;
    let control = '';
    if (f.kind === 'select') {
      control = `<select id="${id}">
        <option value="">请选择</option>
        ${(f.opts || []).map((o) => `<option value="${o.value}" ${value === o.value ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}
      </select>`;
    } else if (f.kind === 'thread_state') {
      // 选中态来自独立的 threadState 参数，而不是 continuity_data。
      control = `<select id="${id}">
        <option value="">请选择</option>
        ${THREAD_STATES.map((o) => `<option value="${o.value}" ${threadState === o.value ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}
      </select>`;
    } else if (f.kind === 'array') {
      control = `<input type="text" id="${id}" value="${esc(Array.isArray(value) ? value.join('，') : '')}">`;
    } else if (f.kind === 'time') {
      control = `<input type="datetime-local" id="${id}" value="${esc(toDatetimeLocal(value))}">`;
    } else if (f.kind === 'int') {
      control = `<input type="number" id="${id}" step="1" min="0" value="${value === undefined || value === null ? '' : Number(value)}">`;
    } else {
      control = `<input type="text" id="${id}" maxlength="600" value="${esc(value === undefined || value === null ? '' : String(value))}">`;
    }
    const reqNote = f.req === 'closed'
      ? '<span class="field-hint req-note">线索结束后必填</span>'
      : '';
    return `
      <div class="field" data-cf="${f.k}">
        <label>${fieldLabel(f)}</label>
        ${control}
        ${f.hint ? `<div class="field-hint">${esc(f.hint)}</div>` : ''}
        ${reqNote}
      </div>`;
  }).join('');
}

function readContinuitySection(root, type) {
  const data = {};
  let threadState = null;
  for (const f of TYPE_FIELDS[type] || []) {
    const input = root.querySelector(`#cf-${f.k}`);
    if (!input) continue;
    if (f.kind === 'thread_state') {
      threadState = input.value || null;
      continue;
    }
    if (f.kind === 'select') {
      if (input.value) data[f.k] = input.value;
    } else if (f.kind === 'array') {
      const items = parseArrayInput(input.value);
      if (items.length) data[f.k] = items;
    } else if (f.kind === 'time') {
      if (input.value) data[f.k] = input.value;
    } else if (f.kind === 'int') {
      if (input.value !== '') data[f.k] = Number(input.value);
    } else if (input.value.trim()) {
      data[f.k] = input.value.trim();
    }
  }
  return { data, threadState };
}

function validateContinuity(type, data, threadState) {
  const typeName = CONTINUITY_TYPES[type];
  for (const f of TYPE_FIELDS[type] || []) {
    if (f.cond && !f.cond(threadState)) continue;
    const requiredNow = f.req === true || (f.req === 'closed' && CLOSED_THREAD_STATES.includes(threadState));
    const present = f.kind === 'thread_state' ? Boolean(threadState) : data[f.k] !== undefined && data[f.k] !== '' && !(Array.isArray(data[f.k]) && !data[f.k].length);
    if (requiredNow && !present) {
      return `「${typeName} · ${f.label}」为必填项`;
    }
    if (f.kind === 'array' && data[f.k]) {
      if (data[f.k].length > ARRAY_LIMIT.count) return `「${f.label}」最多 ${ARRAY_LIMIT.count} 项`;
      if (data[f.k].some((item) => item.length > ARRAY_LIMIT.chars)) return `「${f.label}」每项不超过 ${ARRAY_LIMIT.chars} 个字符`;
    }
    if (f.kind === 'int' && data[f.k] !== undefined) {
      if (!Number.isInteger(data[f.k]) || data[f.k] < 0) return `「${f.label}」必须是非负整数`;
    }
  }
  if (type === 'thread') {
    if (!threadState) return '「未完线索 · 线索状态」为必填项';
    if (CLOSED_THREAD_STATES.includes(threadState)) {
      for (const k of ['closure_summary', 'closure_reason', 'closed_at']) {
        if (!data[k]) return `线索状态为已结束（${THREAD_STATES.find((s) => s.value === threadState).label}）时，「${TYPE_FIELDS.thread.find((f) => f.k === k).label}」必须填写`;
      }
    } else {
      for (const k of ['closure_summary', 'closure_reason', 'closed_at']) {
        if (data[k]) return `线索状态为进行中或暂停时，不允许填写结束字段「${TYPE_FIELDS.thread.find((f) => f.k === k).label}」`;
      }
    }
  }
  if (type === 'interaction_rule' && data.priority !== undefined) {
    if (!Number.isInteger(data.priority) || data.priority < 1 || data.priority > 10) {
      return '「互动规则 · 优先级」必须是 1 到 10 的整数';
    }
  }
  return null;
}

/* ---------- 弹窗装配 ---------- */

function commonSectionHtml(mode, memory) {
  const sourceOptions = SOURCE_TYPE_OPTIONS.map((o) => {
    const selected = memory && memory.source_type === o.value ? 'selected' : '';
    return `<option value="${o.value}" ${selected} title="${esc(o.desc)}">${esc(o.label)} · ${esc(o.desc)}</option>`;
  }).join('');
  const memoryTime = memory ? toDatetimeLocal(memory.memory_time) : '';
  const precision = memory ? (memory.time_precision || '') : '';
  return `
    <div class="form-section">
      <div class="form-section-title">通用信息</div>
      <div class="field"><label>标题<span class="field-hint-inline">（可空，最多 100 字）</span></label>
        <input type="text" id="mf-title" maxlength="100" value="${esc(memory ? memory.title || '' : '')}"></div>
      <div class="field"><label>正文<span class="req-mark" title="必填">*</span><span class="char-count" id="mf-content-count"></span></label>
        <textarea id="mf-content" rows="6" maxlength="600">${esc(memory ? memory.content || '' : '')}</textarea>
        <div class="field-hint">5 到 600 个字符；修改正文后服务端会重新计算内容哈希并按现有规则处理向量</div></div>
      <div class="field"><label>标签<span class="field-hint-inline">（普通标签，回车添加，可空）</span></label>
        <div id="mf-tags"></div></div>
      <div class="grid grid-2">
        <div class="field"><label>重要性<span class="field-hint-inline">（1-10）</span></label>
          <input type="number" id="mf-importance" min="1" max="10" step="1" value="${memory ? Number(memory.importance) || 5 : 5}"></div>
        <div class="field"><label>来源类型<span class="field-hint-inline">（可空，空值保存为空）</span></label>
          <select id="mf-source-type"><option value="">未指定</option>${sourceOptions}</select></div>
      </div>
      <div class="grid grid-2">
        <div class="field"><label>记忆时间<span class="field-hint-inline">（可空，精确到分钟）</span></label>
          <input type="datetime-local" id="mf-memory-time" value="${esc(memoryTime)}"></div>
        <div class="field"><label>时间精度</label>
          <select id="mf-time-precision">
            <option value="">未指定</option>
            ${TIME_PRECISIONS.map((p) => `<option value="${p.value}" ${precision === p.value ? 'selected' : ''}>${esc(p.label)}</option>`).join('')}
          </select></div>
      </div>
      <div class="field"><label>证据消息 ID<span class="field-hint-inline">（可空，多个用逗号分隔）</span></label>
        <input type="text" id="mf-evidence" value="${esc(memory && memory.evidence_message_ids ? memory.evidence_message_ids.join(', ') : '')}">
        <div class="field-hint">填写后服务端会从聊天原文推导证据时间；留空则证据时间为空，绝不伪造</div></div>
      <div class="field"><label>召回场景<span class="field-hint-inline">（可空；保存后由服务端生成召回向量）</span></label>
        <textarea id="mf-recall-scene" rows="2">${esc(memory ? memory.recall_scene || '' : '')}</textarea>
        <div class="field-hint">在什么情境下应该想起这条记忆</div></div>
      <div class="field"><label>召回标签<span class="field-hint-inline">（召回标签，独立于普通标签，回车添加）</span></label>
        <div id="mf-recall-tags"></div></div>
    </div>`;
}

function typeSectionHtml(mode, memory) {
  if (mode === 'edit') {
    const classified = Boolean(memory.continuity_type);
    if (classified) {
      return `
        <div class="form-section">
          <div class="form-section-title">连续感结构 · ${esc(CONTINUITY_TYPES[memory.continuity_type] || memory.continuity_type)}</div>
          <div class="field-hint" style="margin-bottom:10px">修改类型请使用详情栏的「修改类型」按钮；此处仅编辑当前类型的内容。</div>
          <div id="mf-continuity"></div>
        </div>`;
    }
    return `
      <div class="form-section">
        <div class="form-section-title">补充连续感类型（可选）</div>
        <div class="field"><label>连续感类型</label>
          <select id="mf-type">
            <option value="">保持未分类（仅编辑普通字段）</option>
            ${Object.entries(CONTINUITY_TYPES).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join('')}
          </select>
          <div class="field-hint">补充类型后必须填写该类型全部必填字段并通过完整校验；此操作不能撤销。</div></div>
        <div id="mf-continuity"></div>
      </div>`;
  }
  return `
    <div class="form-section">
      <div class="form-section-title">${mode === 'change' ? '新的连续感类型' : '连续感类型'}</div>
      <div class="field"><label>类型<span class="req-mark" title="必填">*</span></label>
        <select id="mf-type">
          <option value="">请选择类型</option>
          ${Object.entries(CONTINUITY_TYPES).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join('')}
        </select>
        ${mode === 'change' ? '<div class="field-hint">新类型不能与当前类型相同；保存成功后会生成新版本，原版本保留为直接上一版本。</div>' : '<div class="field-hint">选择类型后，表单会按类型展开对应的中文结构字段。</div>'}
      </div>
      <div id="mf-continuity"></div>
    </div>`;
}

function buildContinuityDataFromMemory(memory) {
  return {
    data: memory && memory.continuity_data && typeof memory.continuity_data === 'object'
      ? { ...memory.continuity_data } : null,
    threadState: memory ? memory.thread_state || null : null,
  };
}

/**
 * 打开记忆表单弹窗。
 * mode: 'create' | 'edit' | 'change'
 * memory: edit/change 模式的原记忆行
 * onSaved: 保存成功后的回调（刷新列表与详情）
 */
export async function openMemoryForm({ mode = 'create', memory = null, onSaved = null } = {}) {
  const titles = {
    create: '新增记忆',
    edit: memory ? `编辑 memory #${memory.id}` : '编辑记忆',
    change: memory ? `修改类型 · 新条目（原 memory #${memory.id}）` : '修改类型',
  };
  const banners = {
    create: '新记忆直接写入正式记忆库：来源固定为「用户手工写入」，立即生效并参与召回。',
    edit: '普通编辑直接更新原记录，不生成版本，也不提供撤销。',
    change: '修改类型会创建新条目：下方为继承原记忆的通用字段草稿，旧类型的结构不会被复制；保存成功前不写数据库。',
  };
  const { root, close } = modal({
    title: titles[mode],
    body: `
      ${mode === 'change' ? `<div class="banner banner-danger"><span class="banner-ico">${icon('alert')}</span><div>${esc(banners.change)}</div></div>` : ''}
      <div class="form-section">
        <div class="form-section-title">本条记忆是什么</div>
        <p class="muted text-sm" style="margin:0">${esc(banners[mode])}</p>
      </div>
      ${typeSectionHtml(mode, mode === 'create' ? null : memory)}
      ${commonSectionHtml(mode, mode === 'create' ? null : memory)}`,
    footer: `
      <button class="btn btn-secondary" data-cancel>取消</button>
      <button class="btn btn-primary" data-submit>${icon('check')}${mode === 'create' ? '写入正式记忆' : mode === 'change' ? '保存为新版本' : '保存修改'}</button>`,
    wide: true,
  });

  const rootEl = root;
  const typeSelect = rootEl.querySelector('#mf-type');
  const continuityHost = rootEl.querySelector('#mf-continuity');
  const contentEl = rootEl.querySelector('#mf-content');
  const contentCount = rootEl.querySelector('#mf-content-count');
  const tagsControl = createTagInput(rootEl.querySelector('#mf-tags'), memory ? memory.tags : []);
  const recallTagsControl = createTagInput(rootEl.querySelector('#mf-recall-tags'), memory ? memory.recall_tags : []);
  const submitBtn = rootEl.querySelector('[data-submit]');
  const cancelBtn = rootEl.querySelector('[data-cancel]');
  cancelBtn.onclick = close;

  const isThreadStateField = () => continuityHost.querySelector('#cf-thread_state');

  const rerenderContinuity = (type, preset) => {
    if (!type) {
      continuityHost.innerHTML = '<p class="muted text-sm" style="margin:0">请先选择类型，表单会按类型展开对应的中文结构字段。</p>';
      return;
    }
    const intro = `<p class="muted text-sm" style="margin:0 0 10px">${esc(TYPE_INTRO[type])}</p>`;
    continuityHost.innerHTML = intro + renderContinuitySection(type, preset ? preset.data : null, preset ? preset.threadState : null);
    const stateSelect = isThreadStateField();
    if (stateSelect) {
      stateSelect.addEventListener('change', () => {
        // 状态切换后重建结束字段的可视性，但保留已填内容。
        const current = readContinuitySection(rootEl, type);
        rerenderContinuity(type, current);
      });
    }
  };

  const initialType = mode === 'edit' ? memory.continuity_type || '' : '';
  if (mode === 'edit' && memory.continuity_type) {
    rerenderContinuity(initialType, buildContinuityDataFromMemory(memory));
    if (typeSelect) typeSelect.value = initialType;
  } else {
    rerenderContinuity('');
  }
  if (typeSelect) {
    typeSelect.addEventListener('change', () => {
      const type = typeSelect.value;
      rerenderContinuity(type, type === initialType ? buildContinuityDataFromMemory(memory) : null);
    });
  }

  const updateCount = () => {
    contentCount.textContent = `${contentEl.value.length}/600`;
  };
  contentEl.addEventListener('input', updateCount);
  updateCount();
  // 清空记忆时间时，精度不再保留 minute/hour/day 这类伪造精度。
  const timeInput = rootEl.querySelector('#mf-memory-time');
  const precisionSelect = rootEl.querySelector('#mf-time-precision');
  if (timeInput && precisionSelect) {
    timeInput.addEventListener('input', () => {
      if (!timeInput.value && ['minute', 'hour', 'day'].includes(precisionSelect.value)) {
        precisionSelect.value = '';
      }
    });
  }

  /* ----- 读取与校验 ----- */
  const readCommon = () => {
    const title = rootEl.querySelector('#mf-title').value.trim();
    const content = contentEl.value.trim();
    const importance = Number(rootEl.querySelector('#mf-importance').value);
    const sourceType = rootEl.querySelector('#mf-source-type').value;
    const memoryTime = rootEl.querySelector('#mf-memory-time').value;
    const precision = rootEl.querySelector('#mf-time-precision').value;
    const recallScene = rootEl.querySelector('#mf-recall-scene').value.trim();
    const evidenceRaw = rootEl.querySelector('#mf-evidence').value.trim();
    return {
      title, content, importance, sourceType, memoryTime, precision, recallScene,
      tags: tagsControl.get(),
      recallTags: recallTagsControl.get(),
      evidenceIds: evidenceRaw ? evidenceRaw.split(/[,，\s]+/).filter(Boolean) : [],
    };
  };

  const validateCommon = (values) => {
    if (values.content.length < 5 || values.content.length > 600) {
      return '正文长度必须在 5 到 600 个字符之间';
    }
    if (!Number.isInteger(values.importance) || values.importance < 1 || values.importance > 10) {
      return '重要性必须是 1 到 10 的整数';
    }
    for (const idText of values.evidenceIds) {
      if (!/^\d+$/.test(idText)) return `证据消息 ID 必须是正整数：「${idText}」`;
    }
    return null;
  };

  const buildContinuityPart = (type) => {
    const { data, threadState } = readContinuitySection(rootEl, type);
    const problem = validateContinuity(type, data, threadState);
    return { data, threadState, problem };
  };

  const submit = async () => {
    const values = readCommon();
    const problem = validateCommon(values);
    if (problem) { toast(problem, 'err'); return; }

    let payload = null;
    let url = '';
    let successText = '';

    if (mode === 'create') {
      const type = typeSelect.value;
      if (!type) { toast('请选择六类连续感类型之一', 'err'); return; }
      const { data, threadState, problem: cProblem } = buildContinuityPart(type);
      if (cProblem) { toast(cProblem, 'err'); return; }
      payload = {
        title: values.title || null,
        content: values.content,
        tags: values.tags,
        importance: values.importance,
        source_type: values.sourceType || null,
        memory_time: values.memoryTime || null,
        time_precision: values.precision || null,
        recall_scene: values.recallScene || null,
        recall_tags: values.recallTags,
        evidence_message_ids: values.evidenceIds.map(Number),
        continuity_type: type,
        thread_state: threadState,
        continuity_data: data,
      };
      url = '/admin/api/memories/manual';
      successText = '记忆已写入正式记忆库';
    } else if (mode === 'change') {
      const type = typeSelect.value;
      if (!type) { toast('请选择新的连续感类型', 'err'); return; }
      if (type === memory.continuity_type) { toast('新类型不能与当前类型相同；同类型内容请使用「编辑」', 'err'); return; }
      const { data, threadState, problem: cProblem } = buildContinuityPart(type);
      if (cProblem) { toast(cProblem, 'err'); return; }
      payload = {
        title: values.title || null,
        content: values.content,
        tags: values.tags,
        importance: values.importance,
        source_type: values.sourceType || null,
        memory_time: values.memoryTime || null,
        time_precision: values.precision || null,
        recall_scene: values.recallScene || null,
        recall_tags: values.recallTags,
        evidence_message_ids: values.evidenceIds.map(Number),
        continuity_type: type,
        thread_state: threadState,
        continuity_data: data,
      };
      url = `/admin/api/memories/${encodeURIComponent(memory.id)}/change-type`;
      successText = '已保存为新版本，原版本保留为直接上一版本';
    } else {
      // edit：只提交真正发生变化的字段（时间与精度分别比较），避免把
      // 未触碰的证据时间清空。
      const patch = buildEditPatch(memory, values);

      if (memory.continuity_type) {
        // 已分类：类型固定，只同步真正改动的结构；未触碰就不校验也不提交，
        // 结构不完整的旧记忆只编辑普通字段时不会被强迫补全。结构变化必须
        // 同时携带当前 continuity_type，thread 类型带完整 thread_state。
        const type = memory.continuity_type;
        const { data, threadState } = readContinuitySection(rootEl, type);
        const continuityDirty = stableJson(data) !== stableJson(memory.continuity_data || {})
          || (threadState || null) !== (memory.thread_state || null);
        if (continuityDirty) {
          const problem = validateContinuity(type, data, threadState);
          if (problem) { toast(problem, 'err'); return; }
          patch.continuity_type = type;
          patch.continuity_data = data;
          if (type === 'thread') patch.thread_state = threadState;
        }
      } else if (typeSelect && typeSelect.value) {
        // 未分类记忆补充类型：必须填完整结构并通过校验。
        const type = typeSelect.value;
        const { data, threadState, problem } = buildContinuityPart(type);
        if (problem) { toast(problem, 'err'); return; }
        patch.continuity_type = type;
        patch.continuity_data = data;
        if (threadState) patch.thread_state = threadState;
      }

      if (!Object.keys(patch).length) {
        toast('没有需要保存的修改', 'err');
        return;
      }
      payload = patch;
      url = `/admin/api/memories/${encodeURIComponent(memory.id)}/edit`;
      successText = '修改已保存';
    }

    // 召回标签为空时先确认（新增 / 编辑 / 修改类型一律适用）。
    const finalRecallTags = mode === 'edit'
      ? (payload.recall_tags !== undefined ? payload.recall_tags : (memory.recall_tags || []))
      : payload.recall_tags;
    if (!finalRecallTags.length) {
      const ok = await confirm(
        '缺少召回标签可能降低这条记忆被准确召回的机会。仍要继续吗？',
        { title: '缺少召回标签', okText: '仍然提交' },
      );
      if (!ok) return;
    }

    submitBtn.disabled = true;
    try {
      const result = await gw(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      toast(successText);
      close();
      if (onSaved) await onSaved(result);
    } catch (error) {
      submitBtn.disabled = false;
      const raw = String(error.message || '保存失败');
      // gw() 的错误形如 "409 Conflict: 中文说明"；业务冲突用弹窗说明。
      const detail = raw.includes(': ') ? raw.split(': ').slice(1).join(': ') : raw;
      if (/\b409\b|\b404\b/.test(raw)) {
        const errModal = modal({
          title: '操作无法完成',
          body: `<p class="confirm-text">${esc(detail)}</p>`,
          footer: '<button class="btn btn-primary" data-ok>知道了</button>',
        });
        errModal.root.querySelector('[data-ok]').onclick = errModal.close;
      } else {
        toast(`保存失败：${detail}`, 'err');
      }
    }
  };

  submitBtn.onclick = submit;
  contentEl.focus();
  return { close };
}
