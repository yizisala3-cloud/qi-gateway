"""Eventide 身体状态桥接。

封装 Eventide 库的调用：状态创建、时间推进、事件触发、状态卡渲染。
Eventide 通过 pip install git+https://github.com/chuli1122/Eventide.git 安装。
"""
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("gateway.eventide")

# 延迟导入 Eventide，安装失败时不阻断网关
_eventide_available = None


def _check_eventide():
    global _eventide_available
    if _eventide_available is None:
        try:
            from eventide import EventideRuntime
            _eventide_available = True
        except ImportError:
            log.warning("Eventide 未安装，身体状态功能不可用")
            _eventide_available = False
    return _eventide_available


def get_runtime():
    """获取 EventideRuntime 实例。"""
    if not _check_eventide():
        return None
    from eventide import EventideRuntime, EngineSettings
    return EventideRuntime(
        settings=EngineSettings(
            body_cycle_enabled=True,
            inject_body_state_context=True,
            adult_private_mode_enabled=True,
        )
    )


def create_initial_state() -> dict[str, Any] | None:
    """创建 Eventide 初始身体状态。"""
    runtime = get_runtime()
    if not runtime:
        return None
    now = datetime.now(timezone.utc)
    state = runtime.create_state(now)
    return runtime.dump_state(state)


def advance_and_render(
    state_data: dict[str, Any],
    last_counterpart_message_at: datetime | None = None,
) -> tuple[dict[str, Any], str | None]:
    """推进身体状态并渲染状态卡。

    返回: (更新后的 state_data, 状态卡文本 或 None)
    """
    runtime = get_runtime()
    if not runtime or not state_data:
        return state_data or {}, None

    try:
        state = runtime.load_state(state_data)
        now = datetime.now(timezone.utc)

        # tick 推进
        card = runtime.tick_and_render(
            state,
            now,
            last_counterpart_message_at=last_counterpart_message_at,
        )

        # 导出更新后的状态
        new_data = runtime.dump_state(state)
        return new_data, card

    except Exception as e:
        log.error(f"Eventide advance 失败: {e}")
        return state_data, None


def _iso(value: Any) -> str | None:
    """BodyState 里的 datetime/字符串统一转 ISO 文本；空值给 None。"""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def get_body_overview(state_data: dict[str, Any]) -> dict[str, Any] | None:
    """读取结构化身体状态总览（admin 只读展示用，不推进、不落库）。

    在 payload 七项数值之外补充周期/事件标签与状态内部时间戳：标签从
    runtime.config 的注册表按 key 解析，解析不出时回退 key；任何一层
    失败都降级为 None/缺省，绝不伪造数值。
    """
    runtime = get_runtime()
    if not runtime or not state_data:
        return None

    try:
        state = runtime.load_state(state_data)
    except Exception as e:
        log.error(f"Eventide overview 状态加载失败: {e}")
        return None

    try:
        fields = runtime.payload(state) or {}
    except Exception as e:
        log.error(f"Eventide overview payload 失败: {e}")
        fields = {}

    config = getattr(runtime, "config", None)
    cycles = getattr(config, "cycles", None) or {}
    events = getattr(config, "events", None) or {}

    cycle_key = getattr(state, "cycle_key", None)
    event_key = getattr(state, "active_event_key", None)
    cycle_def = cycles.get(cycle_key)
    event_def = events.get(event_key) if event_key else None

    return {
        "fields": fields,
        "cycle_label": getattr(cycle_def, "label", None) or cycle_key,
        "cycle_expires_at": _iso(getattr(state, "cycle_expires_at", None)),
        "event_label": getattr(event_def, "label", None) if event_def else None,
        "event_description": getattr(event_def, "description", None),
        "event_expires_at": _iso(getattr(state, "active_event_expires_at", None)),
        "last_tick_at": _iso(getattr(state, "last_tick_at", None)),
    }
