// Small OrangeChat memory client. Validation, deduplication and persistence belong to qi-gateway.

const CONTINUITY_TYPES = ['moment', 'thread', 'episode', 'inside_joke', 'profile', 'interaction_rule'];
const USER_REVIEW_TYPES = ['episode', 'profile', 'interaction_rule'];
const THREAD_STATES = ['open', 'paused', 'resolved', 'dissolved', 'abandoned', 'unknown'];
const REVIEW_ACTIONS = ['list', 'approve', 'reject', 'merge', 'duplicate', 'conflict'];

function getConfig() {
  return {
    gatewayUrl: String(config.gateway_url || '').trim().replace(/\/+$/, ''),
    pluginToken: String(config.plugin_token || '').trim(),
    assistantId: String(config.assistant_id || '').trim(),
  };
}

function failure(error, errorCode, status) {
  return {
    success: false,
    error: error,
    data: { error_code: errorCode || 'plugin_error', http_status: status || null },
  };
}

function validateConfig(cfg) {
  if (!/^https?:\/\/[^\s]+$/i.test(cfg.gatewayUrl)) {
    return failure('请先配置有效的 qi-gateway HTTP(S) 地址', 'invalid_gateway_url');
  }
  if (!cfg.pluginToken) {
    return failure('请先配置记忆插件 Token', 'missing_plugin_token');
  }
  if (!cfg.assistantId) {
    return failure('请先配置 assistant_id', 'missing_assistant_id');
  }
  return null;
}

function text(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function integer(value, fallback) {
  if (value === undefined || value === null || value === '') return fallback;
  const parsed = Number(value);
  return Number.isInteger(parsed) ? parsed : null;
}

function stringList(value, allowed, fallback) {
  const source = Array.isArray(value) ? value : text(value).split(/[,，]/);
  const result = [];
  for (const item of source) {
    const cleaned = String(item || '').trim().toLowerCase();
    if (cleaned && (!allowed || allowed.indexOf(cleaned) >= 0) && result.indexOf(cleaned) < 0) {
      result.push(cleaned);
    }
  }
  return result.length ? result : (fallback || []);
}

function parseContinuityData(value) {
  if (value && typeof value === 'object' && !Array.isArray(value)) return value;
  if (typeof value !== 'string' || !value.trim()) return null;
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
  } catch (_error) {
    return null;
  }
}

function hasFields(data, fields) {
  return fields.every(function (name) {
    const value = data[name];
    return value !== undefined && value !== null && value !== '';
  });
}

function validateContinuityData(kind, data) {
  const required = {
    moment: ['scene', 'event', 'moment_state'],
    thread: ['open_question', 'current_state'],
    episode: ['beginning', 'development', 'outcome', 'closure_quality'],
    inside_joke: ['origin', 'trigger_phrases', 'shared_meaning'],
    profile: ['facet', 'statement', 'scope', 'stability', 'basis'],
    interaction_rule: ['trigger', 'expected_behavior', 'scope', 'priority', 'rule_state', 'explicit_instruction'],
  };
  if (!data || !hasFields(data, required[kind] || [])) {
    return failure('continuity_data 缺少 ' + kind + ' 所需的完整字段', 'invalid_continuity_data');
  }
  if (kind === 'inside_joke' && (!Array.isArray(data.trigger_phrases) || !data.trigger_phrases.length)) {
    return failure('inside_joke.trigger_phrases 必须是非空数组', 'invalid_continuity_data');
  }
  if (kind === 'interaction_rule' && !text(data.explicit_instruction)) {
    return failure('interaction_rule 必须包含叶子明确指令的摘要', 'missing_explicit_instruction');
  }
  return null;
}

async function callGateway(path, options) {
  const cfg = getConfig();
  const configError = validateConfig(cfg);
  if (configError) return configError;

  const headers = {
    'Authorization': 'Bearer ' + cfg.pluginToken,
    'Content-Type': 'application/json',
  };
  const extraHeaders = options && options.headers ? options.headers : {};
  Object.keys(extraHeaders).forEach(function (name) { headers[name] = extraHeaders[name]; });

  let response;
  try {
    response = await fetch(cfg.gatewayUrl + path, {
      method: (options && options.method) || 'POST',
      headers: headers,
      body: JSON.stringify((options && options.body) || {}),
    });
  } catch (error) {
    return failure('无法连接记忆网关：' + (error && error.message ? error.message : 'network error'), 'network_error');
  }

  let result;
  try {
    result = await response.json();
  } catch (_error) {
    return failure('记忆网关返回了无效 JSON', 'invalid_gateway_response', response.status);
  }
  if (!response.ok || !result.success) {
    return failure(result.error || ('记忆操作失败，HTTP ' + response.status), result.error_code || 'gateway_error', response.status);
  }
  return { success: true, data: result };
}

function cleanMemoryPayload(params) {
  const input = params || {};
  const kind = text(input.continuity_type).toLowerCase();
  if (CONTINUITY_TYPES.indexOf(kind) < 0) {
    return failure('continuity_type 必须是六类连续感记忆之一', 'invalid_continuity_type');
  }
  const content = text(input.content);
  const reason = text(input.reason);
  if (content.length < 5 || reason.length < 3) {
    return failure('content 至少 5 个字符，reason 至少 3 个字符', 'invalid_content');
  }

  const data = parseContinuityData(input.continuity_data);
  const dataError = validateContinuityData(kind, data);
  if (dataError) return dataError;

  const threadState = text(input.thread_state).toLowerCase();
  if (kind === 'thread' && THREAD_STATES.indexOf(threadState) < 0) {
    return failure('thread 必须提供有效 thread_state', 'invalid_thread_state');
  }
  if (kind !== 'thread' && threadState) {
    return failure('只有 thread 可以提供 thread_state', 'invalid_thread_state');
  }

  const importance = integer(input.importance, 5);
  if (importance === null || importance < 1 || importance > 10) {
    return failure('importance 必须是 1-10 的整数', 'invalid_importance');
  }

  const mode = text(input.update_mode).toLowerCase() || 'append';
  const key = text(input.memory_key).toLowerCase();
  if (mode !== 'append' && mode !== 'replace') {
    return failure('update_mode 必须是 append 或 replace', 'invalid_update_mode');
  }
  if (mode === 'replace' && !/^[a-z0-9][a-z0-9._:/-]{2,119}$/.test(key)) {
    return failure('replace 必须提供有效的稳定 memory_key', 'invalid_memory_key');
  }
  if (mode === 'append' && key) {
    return failure('append 时不要提供 memory_key', 'invalid_memory_key');
  }
  if (kind === 'interaction_rule' && mode !== 'replace') {
    return failure('interaction_rule 必须使用 replace 和稳定 memory_key', 'invalid_interaction_rule_mode');
  }

  const sourceType = text(input.source_type).toLowerCase() || 'natural_chat';
  const sourceTypes = ['natural_chat', 'persona_prompt', 'code', 'document', 'quote', 'roleplay', 'tool_result', 'system_meta', 'unknown'];
  if (sourceTypes.indexOf(sourceType) < 0) {
    return failure('source_type 无效', 'invalid_source_type');
  }
  const cfg = getConfig();
  const payload = {
    assistant_id: cfg.assistantId,
    content: content,
    reason: reason,
    continuity_type: kind,
    thread_state: kind === 'thread' ? threadState : null,
    continuity_data: data,
    importance: importance,
    source_type: sourceType,
    update_mode: mode,
  };
  if (key) payload.memory_key = key;
  if (text(input.title)) payload.title = text(input.title);
  if (text(input.tags)) payload.tags = stringList(input.tags, null, []).slice(0, 5);
  if (text(input.conversation_id)) payload.conversation_id = text(input.conversation_id);
  if (input.source_message_id !== undefined && input.source_message_id !== null && input.source_message_id !== '') {
    const sourceMessageId = integer(input.source_message_id, null);
    if (sourceMessageId === null || sourceMessageId <= 0) {
      return failure('source_message_id 必须是正整数', 'invalid_source_message_id');
    }
    payload.source_message_id = sourceMessageId;
  }
  return payload;
}

async function request_memory(params) {
  const payload = cleanMemoryPayload(params);
  if (payload && payload.success === false) return payload;
  const needsUserReview = USER_REVIEW_TYPES.indexOf(payload.continuity_type) >= 0;
  const result = await callGateway('/v1/memory-requests', { body: payload });
  if (!result.success) return result;
  const gatewayResult = result.data;
  return {
    success: true,
    data: {
      request_id: gatewayResult.request_id || null,
      memory_id: gatewayResult.memory_id || null,
      status: gatewayResult.status || null,
      created: Boolean(gatewayResult.created),
      updated: Boolean(gatewayResult.updated),
      deduplicated: Boolean(gatewayResult.deduplicated),
      requires_user_review: needsUserReview,
      message: gatewayResult.message || '网关已处理记忆请求',
    },
  };
}

function extractReviewable(result) {
  const rows = Array.isArray(result.requests)
    ? result.requests
    : (result.data && Array.isArray(result.data.requests) ? result.data.requests : []);
  return rows.filter(function (item) {
    return USER_REVIEW_TYPES.indexOf(String(item.continuity_type || '').toLowerCase()) < 0;
  });
}

async function fetchReviewableRequests() {
  const cfg = getConfig();
  const result = await callGateway('/v1/memory-requests/reviewable', {
    body: { assistant_id: cfg.assistantId, excluded_continuity_types: USER_REVIEW_TYPES, limit: 50 },
  });
  if (!result.success) return result;
  return { success: true, data: { requests: extractReviewable(result.data) } };
}

async function review_memory_requests(params) {
  const cfg = getConfig();
  const input = params || {};
  const action = text(input.action).toLowerCase();
  if (REVIEW_ACTIONS.indexOf(action) < 0) {
    return failure('action 必须是 list、approve、reject、merge、duplicate 或 conflict', 'invalid_review_action');
  }
  const reviewable = await fetchReviewableRequests();
  if (!reviewable.success || action === 'list') return reviewable;

  const requestId = integer(input.request_id, null);
  if (requestId === null || requestId <= 0) {
    return failure('审核动作必须提供有效 request_id', 'invalid_request_id');
  }
  const selected = reviewable.data.requests.find(function (item) { return Number(item.id) === requestId; });
  if (!selected) {
    return failure('该申请不存在、已处理，或属于只能由叶子审核的分类', 'request_not_reviewable');
  }

  const body = { action: action, assistant_id: cfg.assistantId };
  ['content', 'title', 'review_note', 'update_mode', 'memory_key'].forEach(function (name) {
    if (text(input[name])) body[name] = text(input[name]);
  });
  if (text(input.tags)) body.tags = stringList(input.tags, null, []).slice(0, 5);
  if (input.importance !== undefined && input.importance !== null && input.importance !== '') {
    const importance = integer(input.importance, null);
    if (importance === null || importance < 1 || importance > 10) {
      return failure('importance 必须是 1-10 的整数', 'invalid_importance');
    }
    body.importance = importance;
  }
  if (input.related_memory_id !== undefined && input.related_memory_id !== null && input.related_memory_id !== '') {
    const relatedMemoryId = integer(input.related_memory_id, null);
    if (relatedMemoryId === null || relatedMemoryId <= 0) {
      return failure('related_memory_id 必须是正整数', 'invalid_related_memory_id');
    }
    body.related_memory_id = relatedMemoryId;
  }
  return callGateway('/v1/memory-requests/' + encodeURIComponent(String(requestId)) + '/review', { body: body });
}

exports.request_memory = request_memory;
exports.review_memory_requests = review_memory_requests;
