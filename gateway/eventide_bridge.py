"""Eventide 身体状态桥接。

封装 Eventide 库的调用：状态创建、时间推进、事件触发、状态卡渲染。
Eventide 通过 pip install git+https://github.com/chuli1122/Eventide.git 安装。
"""
import logging
import time
from datetime import datetime, timezone, timedelta
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


def apply_settlement(state_data: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """应用互动结算结果。"""
    runtime = get_runtime()
    if not runtime or not state_data:
        return state_data or {}

    try:
        state = runtime.load_state(state_data)
        runtime.settle(state, result)
        return runtime.dump_state(state)
    except Exception as e:
        log.error(f"Eventide settlement 失败: {e}")
        return state_data


def get_body_payload(state_data: dict[str, Any]) -> dict[str, Any] | None:
    """获取结构化身体状态（用于调试/前端）。"""
    runtime = get_runtime()
    if not runtime or not state_data:
        return None

    try:
        state = runtime.load_state(state_data)
        return runtime.payload(state)
    except Exception as e:
        log.error(f"Eventide payload 失败: {e}")
        return None

