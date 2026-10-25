"""Proactive evaluation pacing and safe, persistent fault descriptions."""
from __future__ import annotations

from datetime import timezone
from email.utils import parsedate_to_datetime
import math
import time

import httpx

SILENT_DELAYS = (600, 1200, 2400, 3600)
FAILURE_DELAYS = (300, 900, 3600, 7200)
ERROR_MESSAGES = {
    'timeout': '主动评估请求超时',
    'network': '主动评估上游连接失败',
    'rate_limited': '主动评估被上游限流',
    'auth_failed': '主动评估鉴权或权限失败，请检查模型配置',
    'upstream_http': '主动评估上游返回错误状态',
    'invalid_response': '主动评估返回内容无效',
    'interrupted': '上次主动评估意外中断',
    'internal_error': '主动评估处理失败',
    'state_unavailable': '主动评估状态无法保存或读取，请检查本地存储',
}


class QQModelHTTPError(RuntimeError):
    def __init__(self, status_code, retry_after=None):
        super().__init__('qq_model_http_error')
        self.status_code = status_code
        self.retry_after = retry_after


def parse_retry_after(value, now=None):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            at = parsedate_to_datetime(value)
            at = at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at
            seconds = at.timestamp() - (time.time() if now is None else now)
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0, seconds) if math.isfinite(seconds) else None


def classify_failure(exc):
    status = getattr(exc, 'status_code', None)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    retry_after = getattr(exc, 'retry_after', None)
    if isinstance(exc, httpx.HTTPStatusError):
        retry_after = parse_retry_after(exc.response.headers.get('Retry-After'))
    if status in (401, 403):
        code = 'auth_failed'
    elif status == 429:
        code = 'rate_limited'
    elif isinstance(exc, httpx.TimeoutException):
        code = 'timeout'
    elif isinstance(exc, httpx.RequestError):
        code = 'network'
    elif isinstance(status, int):
        code = 'upstream_http'
    elif isinstance(exc, (ValueError, TypeError)):
        code = 'invalid_response'
    else:
        code = 'internal_error'
    return code, code == 'auth_failed', retry_after, status


def initial_state():
    return dict(silent_streak=0, failure_streak=0, fault_stopped=False,
        next_check_at=0, last_finished_at=0, last_error_at=0, last_error_code='',
        in_flight=False, attempt_id='', attempt_revision=0, generation=0)


def validate_state(raw):
    if not isinstance(raw, dict):
        raise ValueError('qq_evaluation_state_invalid')
    state = {**initial_state(), **raw}
    valid = (all(type(state[key]) is int and state[key] >= 0 for key in
        ('silent_streak','failure_streak','attempt_revision','generation'))
        and state['silent_streak'] <= len(SILENT_DELAYS) and state['failure_streak'] <= 5
        and (state['failure_streak'] < 5 or state['fault_stopped'])
        and all(type(state[key]) is bool for key in ('fault_stopped','in_flight'))
        and all(type(state[key]) in (int,float) and math.isfinite(state[key]) and state[key] >= 0
            for key in ('next_check_at','last_finished_at','last_error_at'))
        and state['last_error_code'] in {'', *ERROR_MESSAGES}
        and isinstance(state['attempt_id'],str) and len(state['attempt_id']) <= 64
        and (not state['in_flight'] or bool(state['attempt_id']))
        and not (state['in_flight'] and state['fault_stopped']))
    if not valid:
        raise ValueError('qq_evaluation_state_invalid')
    return state


def complete(state, outcome, now, base_delay, *, code='', fatal=False, retry_after=None):
    state = dict(state)
    state.update(in_flight=False, attempt_id='', last_finished_at=now)
    if outcome == 'error':
        state['failure_streak'] = min(5, state['failure_streak'] + 1)
        state.update(last_error_code=code, last_error_at=now)
        if fatal or state['failure_streak'] >= 5:
            state.update(fault_stopped=True, next_check_at=0, generation=state['generation']+1)
        else:
            delay = max(base_delay,FAILURE_DELAYS[state['failure_streak']-1])
            if isinstance(retry_after,(int,float)) and math.isfinite(retry_after):
                delay = max(delay, retry_after)
            state['next_check_at'] = now + delay
    elif outcome == 'silent':
        state['failure_streak'] = 0
        state['silent_streak'] = min(len(SILENT_DELAYS),state['silent_streak']+1)
        state['next_check_at'] = now + max(base_delay,SILENT_DELAYS[state['silent_streak']-1])
    elif outcome == 'ready':
        state.update(failure_streak=0,silent_streak=0,next_check_at=now+base_delay)
    elif outcome == 'cancelled':
        state['next_check_at'] = max(state['next_check_at'],now+base_delay)
    else:
        raise ValueError('qq_evaluation_outcome_invalid')
    return state


def public_status(state):
    return {key:state[key] for key in ('silent_streak','failure_streak','fault_stopped',
        'next_check_at','last_finished_at','last_error_at','last_error_code','in_flight')} | {
        'available':True, 'fault_version':state['generation'],
        'last_error_message':ERROR_MESSAGES.get(state['last_error_code'],'')}
