// OrangeChat todo plugin for qi-gateway. Uses ordinary HTTP only.

function getConfig() {
  const offset = Number(config.timezone_offset_minutes || 480);
  return {
    gatewayUrl: String(config.gateway_url || '').trim().replace(/\/+$/, ''),
    pluginToken: String(config.plugin_token || '').trim(),
    userName: String(config.user_name || '').trim(),
    aiName: String(config.ai_name || '').trim(),
    timezoneOffsetMinutes: Number.isInteger(offset) ? offset : 480,
  };
}

function failure(error, errorCode) {
  return {
    success: false,
    error: error,
    data: { error_code: errorCode || 'plugin_error' },
  };
}

function validateConfig(cfg) {
  if (!cfg.gatewayUrl || !/^https?:\/\//i.test(cfg.gatewayUrl)) {
    return failure('请先配置有效的 qi-gateway HTTP(S) 地址', 'invalid_gateway_url');
  }
  if (!cfg.pluginToken) {
    return failure('请先配置待办插件专用 Token', 'missing_plugin_token');
  }
  if (!cfg.userName || !cfg.aiName) {
    return failure('请先配置 user_name 和 ai_name', 'missing_role_scope');
  }
  if (cfg.timezoneOffsetMinutes < -720 || cfg.timezoneOffsetMinutes > 840) {
    return failure('时区偏移分钟必须在 -720 到 840 之间', 'invalid_timezone');
  }
  return null;
}

async function callGateway(path, payload) {
  const cfg = getConfig();
  const configError = validateConfig(cfg);
  if (configError) {
    return configError;
  }

  let response;
  try {
    response = await fetch(cfg.gatewayUrl + path, {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + cfg.pluginToken,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        user_name: cfg.userName,
        ai_name: cfg.aiName,
        ...payload,
      }),
    });
  } catch (error) {
    return failure(
      '无法连接待办网关：' + (error && error.message ? error.message : 'network error'),
      'network_error'
    );
  }

  let result;
  try {
    result = await response.json();
  } catch (_error) {
    return failure('待办网关返回了无效 JSON', 'invalid_gateway_response');
  }
  if (!response.ok || !result.success) {
    return failure(
      result.error || ('待办操作失败，HTTP ' + response.status),
      result.error_code || 'gateway_error'
    );
  }
  return { success: true, data: result };
}

function optionalText(input, name, payload) {
  if (typeof input[name] === 'string' && input[name].trim()) {
    payload[name] = input[name].trim();
  }
}

async function create_todo(params) {
  const input = params || {};
  const content = typeof input.content === 'string' ? input.content.trim() : '';
  if (!content) {
    return failure('content 不能为空', 'invalid_content');
  }
  const payload = { content: content };
  for (const name of ['scheduled_start', 'scheduled_end', 'todo_type', 'status', 'estimated_time', 'note']) {
    optionalText(input, name, payload);
  }
  if (input.is_private !== undefined) {
    if (typeof input.is_private !== 'boolean') {
      return failure('is_private 必须是布尔值', 'invalid_is_private');
    }
    payload.is_private = input.is_private;
  }
  return callGateway('/v1/todos', payload);
}

async function list_today_todos(_params) {
  const cfg = getConfig();
  return callGateway('/v1/todos/query', {
    scope: 'today',
    timezone_offset_minutes: cfg.timezoneOffsetMinutes,
    limit: 50,
  });
}

function cleanTodoId(params) {
  const value = params && typeof params.todo_id === 'string' ? params.todo_id.trim() : '';
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)) {
    return '';
  }
  return value;
}

async function complete_todo(params) {
  const todoId = cleanTodoId(params);
  if (!todoId) {
    return failure('todo_id 必须是有效 UUID', 'invalid_todo_id');
  }
  return callGateway('/v1/todos/' + encodeURIComponent(todoId) + '/complete', {});
}

async function snooze_todo(params) {
  const input = params || {};
  const todoId = cleanTodoId(input);
  if (!todoId) {
    return failure('todo_id 必须是有效 UUID', 'invalid_todo_id');
  }
  const scheduledStart = typeof input.scheduled_start === 'string'
    ? input.scheduled_start.trim()
    : '';
  if (!scheduledStart) {
    return failure('scheduled_start 不能为空', 'invalid_scheduled_start');
  }
  const payload = { scheduled_start: scheduledStart };
  optionalText(input, 'scheduled_end', payload);
  return callGateway('/v1/todos/' + encodeURIComponent(todoId) + '/snooze', payload);
}

async function cancel_todo(params) {
  const todoId = cleanTodoId(params);
  if (!todoId) {
    return failure('todo_id 必须是有效 UUID', 'invalid_todo_id');
  }
  return callGateway('/v1/todos/' + encodeURIComponent(todoId) + '/cancel', {});
}

exports.create_todo = create_todo;
exports.list_today_todos = list_today_todos;
exports.complete_todo = complete_todo;
exports.snooze_todo = snooze_todo;
exports.cancel_todo = cancel_todo;

