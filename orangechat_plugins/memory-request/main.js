// OrangeChat request_memory plugin for qi-gateway.

function getConfig() {
  return {
    gatewayUrl: String(config.gateway_url || '').trim().replace(/\/+$/, ''),
    pluginToken: String(config.plugin_token || '').trim(),
    assistantId: String(config.assistant_id || '').trim(),
  };
}

function failure(error, errorCode) {
  return {
    success: false,
    error: error,
    data: { error_code: errorCode || 'plugin_error' },
  };
}

async function request_memory(params) {
  const cfg = getConfig();
  const input = params || {};

  if (!cfg.gatewayUrl || !/^https?:\/\//i.test(cfg.gatewayUrl)) {
    return failure('请先配置有效的 qi-gateway HTTP(S) 地址', 'invalid_gateway_url');
  }
  if (!cfg.pluginToken) {
    return failure('请先配置插件专用 Token', 'missing_plugin_token');
  }
  if (!cfg.assistantId) {
    return failure('请先配置 assistant_id', 'missing_assistant_id');
  }

  const content = typeof input.content === 'string' ? input.content.trim() : '';
  const reason = typeof input.reason === 'string' ? input.reason.trim() : '';
  if (content.length < 5) {
    return failure('content 至少需要 5 个字符', 'invalid_content');
  }
  if (reason.length < 3) {
    return failure('reason 至少需要 3 个字符', 'invalid_reason');
  }

  const payload = {
    assistant_id: cfg.assistantId,
    content: content,
    reason: reason,
  };

  if (typeof input.title === 'string' && input.title.trim()) {
    payload.title = input.title.trim();
  }
  if (typeof input.tags === 'string' && input.tags.trim()) {
    payload.tags = input.tags.trim();
  }
  if (input.importance !== undefined && input.importance !== null && input.importance !== '') {
    const importance = Number(input.importance);
    if (!Number.isInteger(importance) || importance < 1 || importance > 10) {
      return failure('importance 必须是 1-10 的整数', 'invalid_importance');
    }
    payload.importance = importance;
  }
  if (typeof input.conversation_id === 'string' && input.conversation_id.trim()) {
    payload.conversation_id = input.conversation_id.trim();
  }
  if (input.source_message_id !== undefined && input.source_message_id !== null && input.source_message_id !== '') {
    const sourceMessageId = Number(input.source_message_id);
    if (!Number.isInteger(sourceMessageId) || sourceMessageId <= 0) {
      return failure('source_message_id 必须是正整数', 'invalid_source_message_id');
    }
    payload.source_message_id = sourceMessageId;
  }

  let response;
  try {
    response = await fetch(cfg.gatewayUrl + '/v1/memory-requests', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + cfg.pluginToken,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    });
  } catch (error) {
    return failure(
      '无法连接记忆网关：' + (error && error.message ? error.message : 'network error'),
      'network_error'
    );
  }

  let result;
  try {
    result = await response.json();
  } catch (_error) {
    return failure('记忆网关返回了无效 JSON', 'invalid_gateway_response');
  }

  if (!response.ok || !result.success) {
    return failure(
      result.error || ('记忆申请失败，HTTP ' + response.status),
      result.error_code || 'gateway_error'
    );
  }

  return {
    success: true,
    data: {
      request_id: result.request_id,
      status: result.status,
      deduplicated: Boolean(result.deduplicated),
      message: result.message || '记忆申请已进入用户审核队列',
    },
  };
}

exports.request_memory = request_memory;
