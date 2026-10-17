"""Selected-round edits and parking, committed together with optimistic guards.

The existing task/window validators remain authoritative. A durable receipt makes
an uncertain response retryable without applying an old edit to newer facts.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from uuid import UUID

from . import planning_common as common
from . import planning_occurrences as occurrences
from . import planning_runtime as runtime
from . import planning_serialization as presentation
from . import planning_tasks as tasks
from .planning_window import ResolvedWindow, resolve_window_on_date

EDIT_FIELDS = frozenset((
    "content", "note", "estimated_minutes", "window_start_tod", "window_end_tod",
    "hollow_start_minutes", "hollow_end_minutes",
    "hollow_start_content", "hollow_end_content",
    "alarm_start", "alarm_end", "timer_minutes",
))
DURATION_FIELDS = frozenset(("estimated_minutes", "hollow_start_minutes", "hollow_end_minutes"))
WINDOW_FIELDS = frozenset(("window_start_tod", "window_end_tod"))
REMINDER_FIELDS = frozenset(("alarm_start", "alarm_end", "timer_minutes"))


def _scope(rows):
    return {
        "text": "current_and_future" if any(r["status"] in common.OPEN_STATUSES for r in rows) else "future",
        "time": "current_and_future" if rows and all(common._is_untouched_open(r) for r in rows) else "future",
    }


def _token(task, rows):
    raw = json.dumps([task, sorted(rows, key=lambda r: r["id"])], sort_keys=True,
                     ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read(client, task_id, occurrence_id):
    task = runtime._fetch_task(client, task_id)
    if not task:
        raise common.PlanningError("not_found", "待办任务不存在", 404)
    rows = []
    if occurrence_id is not None:
        if type(occurrence_id) is not int or occurrence_id < 1:
            raise common.PlanningError("invalid_payload", "当前轮次身份无效")
        target = runtime._fetch_occurrence(client, occurrence_id)
        if not target or target["task_id"] != task_id:
            raise common.PlanningError("not_found", "所选待办轮次不存在", 404)
        rows = occurrences._occurrence_round_rows(client, target)
    return task, rows


def get_context(task_id: int, occurrence_id: int | None = None, now=None):
    # Settle overdue old-axis rounds before freezing the optimistic snapshot.
    # A cursor repair here must not invalidate the user's subsequent first save.
    with runtime._maintenance_lock:
        return _get_context(task_id, occurrence_id, now)


def _get_context(task_id, occurrence_id, now):
    now = now or runtime._now()
    client = runtime._require_client()
    task, rows = _read(client, task_id, occurrence_id)
    if task.get("is_active") and task.get("refresh_mode") in common._FIXED_EXPIRING_MODES:
        tasks._close_out_recurrence_before_switch(client, task, now)
        task, rows = _read(client, task_id, occurrence_id)
    if occurrence_id is None and task["task_type"] == "once":
        existing = runtime._rows(client, "planning_occurrence", lambda q: q.eq("task_id", task_id).order("id").limit(1))
        if existing:
            occurrence_id = existing[0]["id"]
            task, rows = _read(client, task_id, occurrence_id)
    scope = _scope(rows)
    selected = next((r for r in rows if r["id"] == occurrence_id), None)
    scope["name"] = ("current_and_future" if selected["status"] in common.OPEN_STATUSES else "future") if selected and selected.get("phase") else scope["text"]
    return {"task": presentation.serialize_task(task, now),
            "occurrences": [presentation.serialize_occurrence(r, task, now) for r in rows],
            "token": _token(task, rows), "scope": scope, "occurrence_id": occurrence_id}


def _result(raw, now):
    task = raw["task"]
    return {"task": presentation.serialize_task(task, now),
            "occurrences": [presentation.serialize_occurrence(r, task, now) for r in raw["occurrences"]],
            "scope": raw["scope"], "replayed": raw.get("replayed", False)}


class DetailEdit:
    def __init__(self, client, task, rows, occurrence_id, request_key, intent):
        self.client, self.task, self.rows = client, task, rows
        self.occurrence_id, self.request_key, self.intent = occurrence_id, request_key, intent

    def _write(self, task_patch, patches, now):
        try:
            raw = self.client.rpc("planning_commit_detail_edit", {
                "p_expected_task": self.task, "p_expected_round": self.rows,
                "p_task_patch": task_patch, "p_occurrence_patches": patches,
                "p_request_key": self.request_key, "p_intent": self.intent,
                "p_now": common._iso(now),
            }).execute().data
        except Exception as exc:
            if common._is_rpc_concurrency_rejection(exc):
                raise common.ConcurrencyRejected("concurrent_modified",
                    "待办或生效范围已改变，本次未保存。请重读生效范围，草稿会保留", 409) from exc
            raise
        return _result(raw, now)

    def commit(self, task, patch, now):
        if task != self.task:
            raise common.ConcurrencyRejected("concurrent_modified", "待办定义已改变，请重读生效范围", 409)
        merged = {**task, **patch}
        if set(patch) & DURATION_FIELDS:
            tasks._validate_template_window_constraints(merged, now)
        current_time = _scope(self.rows)["time"] == "current_and_future"
        time_changed = bool(set(patch) & (DURATION_FIELDS | WINDOW_FIELDS))
        proposed = [dict(r) for r in self.rows]
        by_id = {r["id"]: {"id": r["id"], "updated_at": common._iso(now)} for r in self.rows}
        if set(patch) & {"content", "hollow_start_content", "hollow_end_content"}:
            for r in proposed:
                if r["status"] in common.OPEN_STATUSES and (
                    "content" in patch or r.get("phase") and f"hollow_{r['phase']}_content" in patch):
                    display = merged["content"]
                    if r.get("phase"):
                        display = (merged.get(f"hollow_{r['phase']}_content") or display) + ("·开始" if r["phase"] == "start" else "·结束")
                    by_id[r["id"]]["display_content"] = display
                    if "content" in patch:
                        by_id[r["id"]]["content_snapshot"] = merged["content"]
        if time_changed and current_time:
            occurrences._window_edit_gate(proposed, task)
            for r in proposed:
                duration_field = f"hollow_{r['phase']}_minutes" if r.get("phase") else "estimated_minutes"
                if duration_field in patch:
                    minutes = merged[duration_field]
                    by_id[r["id"]]["planned_minutes"] = minutes
                    r["planned_minutes"] = minutes
                    if r.get("est_start") and (r.get("is_fixed") or r["status"] == "deferred"):
                        # Preserve the owned start anchor; validate its new end below.
                        r["est_end"] = common._iso(common._parse_dt(r["est_start"]) + timedelta(minutes=minutes))
                        by_id[r["id"]]["est_end"] = r["est_end"]
                    else:
                        reset = common._estimate_patch(None, None, source="unassigned")
                        r.update(reset)
                        by_id[r["id"]].update(reset)
            window_patch = {}
            if set(patch) & WINDOW_FIELDS:
                # A current round keeps its own date, including carried/deferred
                # rounds. Never use today's date or roll an expired window forward.
                anchor = date.fromisoformat(self.rows[0]["schedule_date"])
                original_start = self.rows[0].get("window_start_at")
                original = original_start or self.rows[0].get("window_end_at")
                if original:
                    anchor = common._parse_dt(original).astimezone(common._CST).date()
                template = common._task_window_template(merged)
                if original and not original_start and template and template.is_bounded and template.end_tod < template.start_tod:
                    anchor -= timedelta(days=1)
                resolved = resolve_window_on_date(template, anchor) if template else ResolvedWindow()
                window_patch = {"window_start_at": common._iso(resolved.start_at) if resolved.start_at else None,
                                "window_end_at": common._iso(resolved.end_at) if resolved.end_at else None}
            main, sibling = occurrences._occurrence_window_edit(
                self.client, proposed[0], merged, window_patch, now, round_rows=proposed)
            by_id[proposed[0]["id"]].update(main)
            if sibling is not None:
                by_id[proposed[1]["id"]].update(sibling)
            if len(proposed) == 2:
                applied = [{**r, **by_id[r["id"]]} for r in proposed]
                start = next(r for r in applied if r["phase"] == "start")
                end = next(r for r in applied if r["phase"] == "end")
                if start.get("est_end") and end.get("est_start") and (
                    common._parse_dt(start["est_end"]) + timedelta(minutes=end.get("planned_wait_minutes") or task.get("hollow_wait_minutes") or 0)
                    > common._parse_dt(end["est_start"])):
                    raise common.PlanningError("invalid_payload", "修改后中空开始、等待与固定结束阶段无法衔接，请先调整固定时间", 400)
        # Historical rows receive no patch, including updated_at. Notes retain
        # the established database trigger that synchronizes all open rounds.
        patches = [p for p in by_id.values() if len(p) > 2]
        result = self._write({k: v for k, v in patch.items() if k in EDIT_FIELDS or k == "updated_at"}, patches, now)
        return result


def command(task_id: int, payload, *, idempotency_key: str | None, now=None):
    now = now or runtime._now()
    if not isinstance(payload, dict) or set(payload) - {"occurrence_id", "token", "changes", "parked"}:
        raise common.PlanningError("invalid_payload", "编辑请求格式无效")
    try:
        key = str(UUID(idempotency_key))
    except (ValueError, TypeError, AttributeError) as exc:
        raise common.PlanningError("invalid_payload", "需要有效的 Idempotency-Key") from exc
    intent = {"task_id": task_id, **payload}
    with runtime._maintenance_lock:
        client = runtime._require_client()
        replay = runtime._rows(client, "planning_detail_edit_request", lambda q: q.eq("request_key", key).limit(1))
        if replay:
            if replay[0]["intent"] != intent:
                raise common.PlanningError("concurrent_modified", "该请求身份已用于其他编辑", 409)
            return _result({**replay[0]["result"], "replayed": True}, now)
        occurrence_id = payload.get("occurrence_id")
        task, rows = _read(client, task_id, occurrence_id)
        if not payload.get("token") or payload["token"] != _token(task, rows):
            raise common.ConcurrencyRejected("concurrent_modified", "待办或生效范围已改变，请重读生效范围；草稿已保留", 409)
        edit = DetailEdit(client, task, rows, occurrence_id, key, intent)
        if "parked" in payload:
            if type(payload["parked"]) is not bool or "changes" in payload or not rows:
                raise common.PlanningError("invalid_payload", "沉底操作需要明确的当前轮及布尔值")
            occurrences._window_edit_gate(rows, task, "沉底或恢复排程")
            for r in rows:
                if any(r.get(k) and common._parse_dt(r[k]) <= now for k in ("window_end_at", "fixed_expires_at")):
                    raise common.PlanningError("invalid_transition", "当前轮已到截止，不能沉底或恢复，请刷新", 409)
                if not payload["parked"] and (r.get("is_fixed") or r["status"] == "deferred") and r.get("est_end") and common._parse_dt(r["est_end"]) <= now:
                    raise common.PlanningError("invalid_transition", "原固定预估时间已失效，请先调整当前轮时段，再恢复排程", 409)
            patches = []
            for r in rows:
                patch = {"id": r["id"], "is_parked": payload["parked"], "updated_at": common._iso(now)}
                if not payload["parked"] and r.get("is_parked") and not r.get("is_fixed") and r["status"] == "pending":
                    patch.update(common._estimate_patch(None, None, source="unassigned"))
                patches.append(patch)
            result = edit._write({}, patches, now)
            return result
        changes = payload.get("changes")
        if not isinstance(changes, dict) or not changes or set(changes) - EDIT_FIELDS:
            raise common.PlanningError("invalid_payload", "详情编辑字段无效")
        if task["task_type"] == "once" and rows and _scope(rows)["time"] == "future" and set(changes) & (DURATION_FIELDS | WINDOW_FIELDS):
            raise common.PlanningError("invalid_transition", "该单次待办已经开始或结束，没有可修改的未来轮次；当前时间保持原值", 422)
        if task["task_type"] == "once" and rows and _scope(rows)["text"] == "future" and set(changes) - REMINDER_FIELDS:
            raise common.PlanningError("invalid_transition", "该单次待办已经结束，历史记录不可修改，也没有未来轮次", 422)
        return tasks._update_task(task_id, changes, now, detail_edit=edit)
