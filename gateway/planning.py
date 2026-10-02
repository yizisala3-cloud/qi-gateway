"""Public planning compatibility facade, queries and maintenance orchestration.

Implementations live in responsibility-specific planning_* modules. Existing
function signatures and exported error classes remain stable. For tests, patch
the owning module's runtime or helper seam: these compatibility aliases do not
forward attribute replacement into another module's function globals."""
from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from . import db
from .db import get_client
from .planning_domain import (
    BoundaryTransition,
    CALENDAR_FIXED_MODES,
    DEFAULT_REFRESH_BOUNDARY,
    EARLY_CAPABLE_MODES,
    EstimatedTimeOwnership,
    OccurrenceIdentity,
    PlanningCycle,
    calendar_round_key,
    cycle_start_boundary,
    fixed_round_key,
    parse_refresh_boundary,
    planning_cycle_at,
    round_phase_group,
    timed_round_key,
    validate_task_refresh_mode,
)
from .planning_window import (
    ResolvedWindow,
    WindowTemplate,
    hollow_envelope_duration,
    hollow_envelope_minutes,
    resolve_window,
    resolve_window_on_date,
    validate_template_window,
    window_at_crosses_boundary,
    window_crosses_boundary,
    window_feasible,
)
from . import planning_common as common
from . import planning_runtime as runtime
from . import planning_cycles as cycles
from . import planning_schedule as scheduler
from . import planning_recompute as recompute
from . import planning_generation as generation
from . import planning_tasks as task_service
from . import planning_occurrences as occurrences
from . import planning_reschedule as reschedule
from . import planning_serialization as presentation

log = logging.getLogger("gateway.planning")


# Compatibility exports; patch the owning module in tests.
_CST = common._CST
TASK_TYPES = common.TASK_TYPES
REPEATING_TASK_TYPES = common.REPEATING_TASK_TYPES
TIME_MODES = common.TIME_MODES
OCCURRENCE_STATUSES = common.OCCURRENCE_STATUSES
OPEN_STATUSES = common.OPEN_STATUSES
CLOSED_STATUSES = common.CLOSED_STATUSES
RECOMPUTE_WAIT = common.RECOMPUTE_WAIT
DISCARD_RETENTION = common.DISCARD_RETENTION
SWEEP_PAGE_SIZE = common.SWEEP_PAGE_SIZE
_FIXED_EXPIRING_MODES = common._FIXED_EXPIRING_MODES
EARLY_DEDUPE_WINDOW = common.EARLY_DEDUPE_WINDOW
MAX_CONTENT_LENGTH = common.MAX_CONTENT_LENGTH
MAX_NOTE_LENGTH = common.MAX_NOTE_LENGTH
MAX_LIST_ROWS = common.MAX_LIST_ROWS
DEFAULT_LIST_ROWS = common.DEFAULT_LIST_ROWS
_DATE_RE = common._DATE_RE
_TIME_RE = common._TIME_RE
_TIME_WITH_SECONDS_RE = common._TIME_WITH_SECONDS_RE
_SHORTHAND_RE = common._SHORTHAND_RE
_maintenance_lock = runtime._maintenance_lock
PLANNING_BOUNDARY_STATE_KEY = common.PLANNING_BOUNDARY_STATE_KEY
PLANNING_DAILY_REFRESH_KEY = common.PLANNING_DAILY_REFRESH_KEY
PLANNING_AUTO_RECOMPUTE_ENABLED_KEY = common.PLANNING_AUTO_RECOMPUTE_ENABLED_KEY
PLANNING_AUTO_RECOMPUTE_WAIT_KEY = common.PLANNING_AUTO_RECOMPUTE_WAIT_KEY
PlanningError = common.PlanningError
_now = runtime._now
_cst_date = common._cst_date
_iso = common._iso
_parse_dt = common._parse_dt
_parse_date = common._parse_date
_parse_tod = common._parse_tod
_tod_str = common._tod_str
parse_duration_shorthand = common.parse_duration_shorthand
parse_logged_duration_seconds = common.parse_logged_duration_seconds
_combine = common._combine
_minutes_between = common._minutes_between
_default_boundary_state = cycles._default_boundary_state
_load_boundary_state = cycles._load_boundary_state
get_cycle_settings = cycles.get_cycle_settings
_load_raw_boundary_state = cycles._load_raw_boundary_state
_parse_boundary_adjustments = cycles._parse_boundary_adjustments
_boundary_window_conflicts = cycles._boundary_window_conflicts
_save_cycle_boundary = cycles._save_cycle_boundary
set_cycle_settings = cycles.set_cycle_settings
_current_cycle = cycles._current_cycle
_require_client = runtime._require_client
_rows = runtime._rows
_fetch_task = runtime._fetch_task
_fetch_occurrence = runtime._fetch_occurrence
_task_map = runtime._task_map
_clean_text = common._clean_text
_clean_int = common._clean_int
_clean_bool = common._clean_bool
_clean_int_list = common._clean_int_list
validate_task_payload = task_service.validate_task_payload
_display_content = presentation._display_content
_deadline_for = presentation._deadline_for
schedule_label = presentation.schedule_label
serialize_occurrence = presentation.serialize_occurrence
serialize_task = presentation.serialize_task
_task_window_template = common._task_window_template
_window_occupancy_minutes = common._window_occupancy_minutes
_validate_template_window_constraints = task_service._validate_template_window_constraints
_validate_window_creation = task_service._validate_window_creation
_validate_once_date_window_pair = task_service._validate_once_date_window_pair
_ROUND_SKIP_REFRESH_MODES = common._ROUND_SKIP_REFRESH_MODES
_round_deadline_passed = task_service._round_deadline_passed
_fixed_interval_anchor_due = task_service._fixed_interval_anchor_due
_first_round_settlement = task_service._first_round_settlement
_creation_window_outcome = task_service._creation_window_outcome
create_task = task_service.create_task
_prepare_refresh_definition = task_service._prepare_refresh_definition
_should_recompute_after_generation = common._should_recompute_after_generation
_generate_due_quietly = task_service._generate_due_quietly
SCHEDULE_FIELDS = common.SCHEDULE_FIELDS
_RECURRENCE_SWITCH_FIELDS = common._RECURRENCE_SWITCH_FIELDS
_ONCE_LOCKED_TEMPLATE_FIELDS = common._ONCE_LOCKED_TEMPLATE_FIELDS
_canonical_template_value = common._canonical_template_value
_close_out_recurrence_before_switch = task_service._close_out_recurrence_before_switch
_first_rule_event_after = task_service._first_rule_event_after
_recurrence_switch_cursor = task_service._recurrence_switch_cursor
update_task = task_service.update_task
_update_task = task_service._update_task
_ensure_type_requirements = task_service._ensure_type_requirements
list_tasks = task_service.list_tasks
_once_schedule_date = generation._once_schedule_date
_resolve_generation_window = generation._resolve_generation_window
_generation_snapshots = generation._generation_snapshots
_occurrence_row = generation._occurrence_row
_create_occurrences = generation._create_occurrences
_should_occur = generation._should_occur
_fixed_rounds = generation._fixed_rounds
_following_fixed_event = generation._following_fixed_event
_task_open_rows = generation._task_open_rows
_carry_open_rounds = generation._carry_open_rounds
_fixed_death_boundary = generation._fixed_death_boundary
_expire_fixed_rounds = generation._expire_fixed_rounds
_after_completion_due = generation._after_completion_due
_reconcile_task_rounds = generation._reconcile_task_rounds
generate_due = generation.generate_due
sweep_timeouts = generation.sweep_timeouts
_freely_schedulable = scheduler._freely_schedulable
_slot_range = scheduler._slot_range
_occurrence_window = scheduler._occurrence_window
_window_conflict = scheduler._window_conflict
_has_schedulable_duration_source = scheduler._has_schedulable_duration_source
_format_duration = common._format_duration
_hollow_sibling = scheduler._hollow_sibling
_frozen_slot_window_conflict = scheduler._frozen_slot_window_conflict
_hollow_movable_envelope_duration = scheduler._hollow_movable_envelope_duration
_hollow_start_envelope_conflict = scheduler._hollow_start_envelope_conflict
_hollow_start_connection_conflict = scheduler._hollow_start_connection_conflict
ScheduleResult = scheduler.ScheduleResult
compute_schedule = scheduler.compute_schedule
CONCURRENCY_ERRCODE = common.CONCURRENCY_ERRCODE
_CONCURRENCY_REJECTION_MESSAGES = common._CONCURRENCY_REJECTION_MESSAGES
ConcurrencyRejected = common.ConcurrencyRejected
_is_rpc_concurrency_rejection = common._is_rpc_concurrency_rejection
_conditional_lifecycle_update = recompute._conditional_lifecycle_update
_recompute_expected_snapshot = recompute._recompute_expected_snapshot
_atomic_round_write = recompute._atomic_round_write
recompute_today = recompute.recompute_today
_auto_recompute_config = recompute._auto_recompute_config
request_recompute = recompute.request_recompute
_request_recompute_quietly = recompute._request_recompute_quietly
clear_recompute_mark = recompute.clear_recompute_mark
get_recompute_state = recompute.get_recompute_state
trigger_recompute = recompute.trigger_recompute
save_order = recompute.save_order
save_order_from_payload = recompute.save_order_from_payload
_effective_minutes = common._effective_minutes
_duration_of = common._duration_of
_compute_actual_minutes = common._compute_actual_minutes
_add_actual_staleness_guards = occurrences._add_actual_staleness_guards
_estimate_patch = common._estimate_patch
_manual_estimate_patch = recompute._manual_estimate_patch
_hollow_display_patch = occurrences._hollow_display_patch
_hollow_sibling_of = occurrences._hollow_sibling_of
_occurrence_round_rows = occurrences._occurrence_round_rows
_occurrence_window_occupancy = occurrences._occurrence_window_occupancy
LIFECYCLE_FACT_FIELDS = common.LIFECYCLE_FACT_FIELDS
UNTOUCHED_OPEN_STATUSES = common.UNTOUCHED_OPEN_STATUSES
OPEN_ONLY_STATUSES = common.OPEN_ONLY_STATUSES
_has_lifecycle_fact = common._has_lifecycle_fact
_is_untouched_open = common._is_untouched_open
_window_edit_gate = occurrences._window_edit_gate
_occurrence_window_edit = occurrences._occurrence_window_edit
_reschedule_occurrence = occurrences._reschedule_occurrence
_shift_sibling_phase = occurrences._shift_sibling_phase
reschedule_timeout_as_new = reschedule.reschedule_timeout_as_new
_is_reschedule_convergence_conflict = reschedule._is_reschedule_convergence_conflict
_is_anchor_rejection = reschedule._is_anchor_rejection
_rpc = reschedule._rpc
_reschedule_still_pending = reschedule._reschedule_still_pending
_reschedule_request_family = reschedule._reschedule_request_family
_converge_reschedule_request = reschedule._converge_reschedule_request
_adoptable_reschedule_tasks = reschedule._adoptable_reschedule_tasks
_replay_reschedule_result = reschedule._replay_reschedule_result
_resume_reschedule_request = reschedule._resume_reschedule_request
_adopt_reschedule_task = reschedule._adopt_reschedule_task
_apply_adopted_intent = reschedule._apply_adopted_intent
_finalize_reschedule_occurrence = reschedule._finalize_reschedule_occurrence
_discard_task_atomically = runtime._discard_task_atomically
set_occurrence_status = occurrences.set_occurrence_status
start_occurrence = occurrences.start_occurrence
finish_occurrence = occurrences.finish_occurrence
patch_occurrence = occurrences.patch_occurrence
split_occurrence = occurrences.split_occurrence
_after_completion_duplicate = occurrences._after_completion_duplicate
_repair_after_completion_baseline = occurrences._repair_after_completion_baseline
complete_task_early = occurrences.complete_task_early
get_client = runtime.get_client


# ── 当天 / 全部列表 ───────────────────────────────────────────────

def today_board(now: datetime | None = None) -> dict[str, Any]:
    """当前待办三分区（进度中 / 待处理 / 已完成）+ 重算等待状态。"""
    now = now or runtime._now()
    cycle = cycles._current_cycle(now)
    today = cycle.key
    client = runtime._require_client()
    today_rows = runtime._rows(
        client, "planning_occurrence",
        lambda q: q.eq("display_cycle_date", today.isoformat()),
    )
    timeout_rows = [row for row in runtime._rows(
        client, "planning_occurrence", lambda q: q.eq("status", "timeout"),
    ) if row.get("round_key")]
    merged: dict[int, dict[str, Any]] = {row["id"]: row for row in today_rows + timeout_rows}
    tasks = runtime._task_map(client, {row["task_id"] for row in merged.values()})

    progress, done = [], []
    for row in today_rows:
        task = tasks.get(row["task_id"])
        if not task:
            continue
        item = presentation.serialize_occurrence(row, task, now)
        if row["status"] in common.OPEN_STATUSES:
            progress.append(item)
        elif row["status"] in common.CLOSED_STATUSES:
            done.append(item)
    progress.sort(key=lambda item: (item["sort_order"], item["id"]))
    done.sort(key=lambda item: (item.get("closed_at") or "", item["id"]), reverse=True)

    attention = []
    for row in timeout_rows:
        task = tasks.get(row["task_id"])
        if not task:
            continue
        attention.append(presentation.serialize_occurrence(row, task, now))
    attention.sort(key=lambda item: (item["schedule_date"], item["id"]))

    # 窗口批次（§19）：排程冲突是读取时只读派生结果（复用 compute_schedule
    # 同一纯函数，不落库、无持久化冲突缓存），仅对当前周期开放实例计算，
    # 与 recompute_today 的排程语义完全同源。
    open_rows = [row for row in today_rows if row["status"] in common.OPEN_STATUSES]
    conflicts = (
        scheduler.compute_schedule(open_rows, tasks, now).conflicts if open_rows else []
    )

    return {
        "date": today.isoformat(),
        "cycle_start": cycle.start.isoformat(),
        "cycle_end": cycle.end.isoformat(),
        "now": common._iso(now),
        "recompute": recompute.get_recompute_state(now),
        "conflicts": conflicts,
        "progress": progress,
        "attention": attention,
        "done": done,
    }


def list_occurrences(
    *,
    task_type: str | None = None,
    status: str | None = None,
    for_date: str | None = None,
    schedule_date: str | None = None,
    display_cycle_date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = common.DEFAULT_LIST_ROWS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or runtime._now()
    if status is not None and status not in common.OCCURRENCE_STATUSES:
        raise common.PlanningError("invalid_payload", f"unknown status: {status}")
    if task_type is not None and task_type not in common.TASK_TYPES:
        raise common.PlanningError("invalid_payload", f"unknown task_type: {task_type}")
    if for_date and schedule_date and for_date != schedule_date:
        raise common.PlanningError("invalid_payload", "for_date 与 schedule_date 筛选条件不一致", 400)
    try:
        limit = max(1, min(common.MAX_LIST_ROWS, int(limit or common.DEFAULT_LIST_ROWS)))
    except (TypeError, ValueError) as exc:
        raise common.PlanningError("invalid_payload", "limit must be an integer") from exc

    client = runtime._require_client()

    # 类型筛选下推到 SQL：先取该类型的 task_id 集合，避免「先 limit 后
    # 内存过滤」把更早的命中记录静默挤掉。
    type_task_ids: list[int] | None = None
    if task_type:
        type_task_ids = [
            row["id"] for row in runtime._rows(client, "planning_task", lambda q: q.eq("task_type", task_type))
        ]
        if not type_task_ids:
            return []

    def query(q):
        if type_task_ids is not None:
            q = q.in_("task_id", type_task_ids)
        if for_date or schedule_date:
            # for_date is an old parameter name for the immutable schedule date.
            q = q.eq("schedule_date", common._parse_date(schedule_date or for_date, "schedule_date").isoformat())
        if display_cycle_date:
            q = q.eq("display_cycle_date", common._parse_date(display_cycle_date, "display_cycle_date").isoformat())
        if date_from:
            q = q.gte("schedule_date", common._parse_date(date_from, "date_from").isoformat())
        if date_to:
            q = q.lte("schedule_date", common._parse_date(date_to, "date_to").isoformat())
        if status:
            q = q.eq("status", status)
        return q.order("schedule_date", desc=True).order("id", desc=True).limit(limit)

    rows = runtime._rows(client, "planning_occurrence", query)
    tasks = runtime._task_map(client, {row["task_id"] for row in rows})
    result = []
    for row in rows:
        task = tasks.get(row["task_id"])
        if not task:
            continue
        result.append(presentation.serialize_occurrence(row, task, now))
    return result


# ── 72 小时清理 ───────────────────────────────────────────────────

def cleanup_discarded(now: datetime | None = None) -> dict[str, int]:
    """Legacy cleanup only; new business-round closure history is permanent."""
    now = now or runtime._now()
    client = runtime._require_client()
    threshold = common._iso(now - common.DISCARD_RETENTION)
    rows = runtime._rows(
        client, "planning_occurrence",
        lambda q: q.in_("status", ["discarded", "discarded_this"]).lt("closed_at", threshold),
    )
    rows = [row for row in rows if not row.get("round_key")]
    deleted = 0
    by_round: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for row in rows:
        by_round.setdefault((row["task_id"], row.get("round_key") or f"legacy:{row['id']}"), []).append(row)
    for round_rows in by_round.values():
        first = round_rows[0]
        if first.get("phase_group"):
            all_phases = runtime._rows(client, "planning_occurrence", lambda q: q.eq("task_id", first["task_id"]).eq("round_key", first["round_key"]))
            if len(all_phases) != 2 or len(round_rows) != 2:
                continue
            client.table("planning_occurrence").delete().eq("task_id", first["task_id"]).eq("round_key", first["round_key"]).execute()
            deleted += 2
        else:
            client.table("planning_occurrence").delete().eq("id", first["id"]).execute()
            deleted += 1
    return {"deleted": deleted}


# ── 后台维护循环（约 1 分钟粒度） ─────────────────────────────────

def run_maintenance(now: datetime | None = None) -> dict[str, Any]:
    """At the configured cycle boundary, maintain rounds and legacy services.

    单实例锁防止并发重入；单步失败只记日志，不影响其余步骤。
    """
    if not runtime._maintenance_lock.acquire(blocking=False):
        return {"status": "skipped_busy"}
    try:
        now = now or runtime._now()
        results: dict[str, Any] = {"status": "ok", "at": common._iso(now)}
        try:
            results["generation"] = generation.generate_due(now)
            # 新生成的当天实例需要立刻拿到预估起止；
            # 重算以列表顺序与固定槽为准，不会动用户已固定的内容。
            # 五轮 / 六轮 Review：触发条件与 _generate_due_quietly 共用
            # `_should_recompute_after_generation`（created > 0 或 errors
            # 非空——partial create 的 created 计数会丢失，保守幂等重算）。
            if common._should_recompute_after_generation(results["generation"]):
                results["generation_recompute"] = recompute.recompute_today(now)
        except Exception as exc:
            log.exception("planning 生成失败: %s", type(exc).__name__)
            results["generation"] = {"error": type(exc).__name__}
        try:
            results["timeouts"] = generation.sweep_timeouts(now)
        except Exception as exc:
            log.exception("planning 超时打标失败: %s", type(exc).__name__)
            results["timeouts"] = {"error": type(exc).__name__}
        try:
            enabled, wait = recompute._auto_recompute_config(now)
            state = recompute.get_recompute_state(now)
            requested_at = state.get("requested_at")
            request_token = state.get("request_token")
            if enabled and requested_at and (now - common._parse_dt(requested_at, "requested_at")) >= wait:
                auto = recompute.recompute_today(now)
                results["auto_recompute"] = auto
                # 窗口批次修复轮（2026-09-28 Review MEDIUM-1）：仅零冲突（成功）
                # 清空等待标记；冲突本轮整体未生效，标记保留，等待条件改变后
                # 由后续维护循环按既有语义再次执行（不新增状态 / 重试机制）。
                # 批次 6 收尾（BUG A → A1 升级）：清除以本次消费的
                # request_token 为条件——执行期间并发写入的新请求（即使其
                # requested_at 与捕获值相同）不被本次清除，保留给下一轮
                # 维护循环消费。
                if not auto.get("conflicts") and not auto.get("stale_skipped"):
                    recompute.clear_recompute_mark(now, expected_request_token=request_token)
            else:
                results["auto_recompute"] = {"skipped": True}
        except Exception as exc:
            log.exception("planning 自动重算失败: %s", type(exc).__name__)
            results["auto_recompute"] = {"error": type(exc).__name__}
        try:
            results["cleanup"] = cleanup_discarded(now)
        except Exception as exc:
            log.exception("planning 72 小时清理失败: %s", type(exc).__name__)
            results["cleanup"] = {"error": type(exc).__name__}
        return results
    finally:
        runtime._maintenance_lock.release()


def get_task(task_id: int, now: datetime | None = None) -> dict[str, Any]:
    """Read one task through the service boundary used by the HTTP adapter."""
    client = runtime._require_client()
    task = runtime._fetch_task(client, task_id)
    if not task:
        raise common.PlanningError("not_found", "planning task not found", 404)
    return presentation.serialize_task(task, now or runtime._now())


def get_occurrence(occurrence_id: int, now: datetime | None = None) -> dict[str, Any]:
    """Read one occurrence and its task without exposing private DB helpers."""
    client = runtime._require_client()
    occ = runtime._fetch_occurrence(client, occurrence_id)
    if not occ:
        raise common.PlanningError("not_found", "planning occurrence not found", 404)
    task = runtime._fetch_task(client, occ["task_id"])
    if not task:
        raise common.PlanningError("not_found", "planning task not found", 404)
    return presentation.serialize_occurrence(occ, task, now or runtime._now())
