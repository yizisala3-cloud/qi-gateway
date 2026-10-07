"""Planning RPC emulators shared by both in-memory database variants.

These support behavioural tests; PostgreSQL suites verify real transactions.
"""

from datetime import datetime, time, timedelta

from gateway import planning


# 批次 6 二轮：planning_patch_occurrence_round（迁移 20260928020000）的 fake
# 仿真——白名单 / 同轮身份 / 逐行各自补丁 / 键缺省保持现值 / 锁内生命周期
# 二次校验 / 窗口一致性。fake 只服务行为测试；原子性权威证明由 pgserver
# 真 PostgreSQL 套件提供。
ROUND_PATCH_TARGET_FIELDS = frozenset({
    "window_start_at", "window_end_at", "est_start", "est_end", "nominal_start",
    "estimated_time_source", "fixed_source", "schedule_managed", "is_fixed",
    "partial_note", "actual_start", "actual_end", "actual_minutes",
    "status", "closed_at", "handled_at", "partial_at",
    "display_cycle_date", "display_reason", "updated_at",
})
ROUND_PATCH_SIBLING_FIELDS = frozenset({
    "window_start_at", "window_end_at", "est_start", "est_end", "nominal_start",
    "estimated_time_source", "fixed_source", "schedule_managed", "is_fixed",
    "display_cycle_date", "display_reason", "updated_at",
})
ROUND_PATCH_EXPECTED_FIELDS = frozenset({
    "id", "status", "window_start_at", "window_end_at", "est_start", "est_end",
    "estimated_time_source", "fixed_source", "is_fixed", "schedule_managed",
    "sort_order",
})


def emulate_planning_round_patch(rows, params):
    """按迁移函数语义把同轮两行补丁应用到内存行；违规抛 RuntimeError。"""
    target_patch = params.get("p_target_patch") or {}
    sibling_patch = params.get("p_sibling_patch") or {}
    if set(target_patch) - ROUND_PATCH_TARGET_FIELDS:
        raise RuntimeError(
            "planning_patch_occurrence_round: target patch contains unsupported field")
    if set(sibling_patch) - ROUND_PATCH_SIBLING_FIELDS:
        raise RuntimeError(
            "planning_patch_occurrence_round: sibling patch contains unsupported field")
    if "status" in target_patch and target_patch["status"] not in (
            "pending", "in_progress", "deferred", "partial"):
        raise RuntimeError(
            "planning_patch_occurrence_round: only open statuses can be written")
    target = next((r for r in rows if r.get("id") == params.get("p_target_id")), None)
    sibling = next((r for r in rows if r.get("id") == params.get("p_sibling_id")), None)
    if target is None or sibling is None:
        raise RuntimeError("planning_patch_occurrence_round: occurrence rows not found")
    if (target.get("task_id") != sibling.get("task_id")
            or target.get("round_key") != sibling.get("round_key")
            or target.get("phase_group") != sibling.get("phase_group")
            or not target.get("phase") or not sibling.get("phase")
            or target.get("phase") == sibling.get("phase")):
        raise RuntimeError(
            "planning_patch_occurrence_round: rows are not a hollow start/end pair")
    # expected snapshot 复核（最终验收修复问题 3；#13 契约收紧）：expected
    # 非 NULL 时必须是恰好两个元素的数组、每个元素为对象且携带全部必填键
    # （含 sort_order）、两个 id 恰为目标行与兄弟行各一次；漂移 → 拒绝。
    if params.get("p_expected") is not None:
        expected = params["p_expected"]
        if (not isinstance(expected, list) or len(expected) != 2
                or any(not isinstance(e, dict) for e in expected)):
            raise RuntimeError(
                "planning_patch_occurrence_round: invalid expected snapshot")
        if any(not ROUND_PATCH_EXPECTED_FIELDS <= set(e) for e in expected):
            raise RuntimeError(
                "planning_patch_occurrence_round: invalid expected snapshot")
        ids = sorted(e.get("id") for e in expected)
        if (ids != sorted((params.get("p_target_id"), params.get("p_sibling_id")))
                or len(set(ids)) != 2):
            raise RuntimeError(
                "planning_patch_occurrence_round: invalid expected snapshot")
        for e in params["p_expected"]:
            row = next((r for r in rows if r.get("id") == e.get("id")), None)
            if row is None:
                raise RuntimeError(
                    "planning_patch_occurrence_round: schedule inputs drifted")
            for key, value in e.items():
                if key == "id":
                    continue
                if row.get(key) != value:
                    raise RuntimeError(
                        "planning_patch_occurrence_round: schedule inputs drifted "
                        "(stale recompute result)")
    # 锁内生命周期二次校验（最终修复问题 1；问题 5 收紧）：窗口字段永远
    # 严格门（status 不能降低保护）；纯预估编辑严格门；仅不触及窗口的
    # 开放状态流转（延后等）用宽松门。
    window_touched = any(key in target_patch or key in sibling_patch
                         for key in ("window_start_at", "window_end_at"))
    if window_touched or "status" not in target_patch:
        gated = any(
            row.get("status") not in ("pending", "deferred")
            or any(row.get(field) is not None for field in (
                "actual_start", "actual_end", "partial_at", "handled_at", "closed_at"))
            for row in (target, sibling))
    else:
        gated = any(
            row.get("status") not in ("pending", "in_progress", "deferred", "partial")
            or row.get("closed_at") is not None or row.get("handled_at") is not None
            for row in (target, sibling))
    if gated:
        raise RuntimeError(
            "planning_patch_occurrence_round: round is no longer editable "
            "(concurrent lifecycle change)")
    # 窗口一致性（最终修复问题 7）
    if any(key in target_patch or key in sibling_patch
           for key in ("window_start_at", "window_end_at")):
        for key in ("window_start_at", "window_end_at"):
            expected = target_patch.get(key, target.get(key))
            sibling_value = sibling_patch.get(key, sibling.get(key))
            if expected != sibling_value:
                raise RuntimeError(
                    "planning_patch_occurrence_round: hollow phases disagree on window")
    for row, patch in ((target, target_patch), (sibling, sibling_patch)):
        for key, value in patch.items():
            row[key] = value


# 批次 6 最终修复（问题 3）：once 身份编辑 / 生成的任务行锁守护 RPC 仿真。
ONCE_TASK_PATCH_FIELDS = frozenset({
    "content", "task_type", "interval_days", "weekdays", "month_days",
    "target_date", "refresh_mode", "refresh_anchor_at", "refresh_enabled",
    "estimated_minutes", "window_start_tod", "window_end_tod",
    "alarm_start", "alarm_end", "timer_minutes", "is_active",
    "is_hollow", "hollow_start_content", "hollow_start_minutes",
    "hollow_wait_minutes", "hollow_wait_note", "hollow_end_content",
    "hollow_end_minutes", "time_mode", "est_start_tod", "est_end_tod",
    "is_fixed", "refresh_generated_through", "last_handled_at",
    "refresh_next_due_at", "updated_at",
})
_ONCE_LOCKED_MSG = ("planning_update_once_task_guarded: task missing or "
                    "once identity locked: occurrence exists")


def emulate_planning_once_task_guarded(task_rows, occ_rows, params):
    """锁内复核 once 无实例并原子保存任务补丁；违规抛 RuntimeError。"""
    patch = params.get("p_patch") or {}
    if set(patch) - ONCE_TASK_PATCH_FIELDS:
        raise RuntimeError(
            "planning_update_once_task_guarded: patch contains unsupported field")
    task = next((r for r in task_rows if r.get("id") == params.get("p_task_id")), None)
    if task is None or any(o.get("task_id") == task["id"] for o in occ_rows):
        raise RuntimeError(_ONCE_LOCKED_MSG)
    for key, value in patch.items():
        task[key] = value


ONCE_EXPECTED_TASK_FIELDS = frozenset({
    "content", "estimated_minutes", "time_mode", "is_hollow",
    "hollow_start_minutes", "hollow_wait_minutes", "hollow_end_minutes"})


def emulate_planning_insert_once_occurrence(db, params):
    """锁内校验 once 定义未漂移后插入预构造行；漂移 / 违规抛 RuntimeError。"""
    task_rows, occ_rows = db.rows["planning_task"], db.rows["planning_occurrence"]
    task = next((r for r in task_rows if r.get("id") == params.get("p_task_id")), None)
    if task is None:
        raise RuntimeError("planning_insert_once_occurrence: task not found")
    if (task.get("target_date") != params.get("p_expected_target_date")
            or task.get("window_start_tod") != params.get("p_expected_window_start_tod")
            or task.get("window_end_tod") != params.get("p_expected_window_end_tod")):
        raise RuntimeError(
            "planning_insert_once_occurrence: once definition changed during generation")
    expected_task = params.get("p_expected_task") or {}
    if set(expected_task) - ONCE_EXPECTED_TASK_FIELDS:
        raise RuntimeError(
            "planning_insert_once_occurrence: expected task contains unsupported field")
    for key, value in expected_task.items():
        # is_hollow 在真实表为 NOT NULL DEFAULT false；fake 行缺键得 None，
        # 与 False 同语义（与 real RPC 的 boolean 比较一致）。
        actual = bool(task.get(key)) if key == "is_hollow" else task.get(key)
        if actual != value:
            raise RuntimeError(
                "planning_insert_once_occurrence: once definition changed "
                "during generation")
    for item in params.get("p_rows") or []:
        if item.get("status") != "pending" or item.get("source") != "schedule":
            raise RuntimeError(
                "planning_insert_once_occurrence: rows must be pending schedule occurrences")
        # 轮次唯一键（真实库由 planning_occurrence_round_phase_uq 拒绝）
        for existing in occ_rows:
            if (existing.get("task_id") == item.get("task_id")
                    and existing.get("round_key") == item.get("round_key")
                    and existing.get("phase") == item.get("phase")):
                raise RuntimeError("planning_occurrence_round_phase_uq")
        row = dict(item)
        row.setdefault("id", db.next_id("planning_occurrence"))
        occ_rows.append(row)


ROUND_DISCARD_TARGET_FIELDS = frozenset({
    "actual_start", "actual_end", "actual_minutes", "actual_time_source",
    "updated_at"})
ROUND_EXPECTED_TASK_FIELDS = frozenset({
    "task_type", "refresh_mode", "refresh_enabled", "is_active",
    "request_state", "content", "time_mode", "estimated_minutes",
    "is_hollow", "hollow_start_minutes", "hollow_wait_minutes",
    "hollow_end_minutes", "hollow_start_content", "hollow_end_content",
    "interval_days", "weekdays", "month_days", "target_date",
    "created_at", "refresh_anchor_at",
    "last_handled_at", "refresh_next_due_at", "window_start_tod",
    "window_end_tod", "after_completion_minutes",
})


def emulate_planning_insert_round_occurrence(db, params):
    """非 once 生成锁内复核：active/refresh_enabled/定义漂移/唯一键。"""
    task_rows, occ_rows = db.rows["planning_task"], db.rows["planning_occurrence"]
    task = next((r for r in task_rows if r.get("id") == params.get("p_task_id")), None)
    if task is None:
        raise RuntimeError("planning_insert_round_occurrence: task not found")
    if (task.get("is_active", True) is not True
            or task.get("refresh_enabled", True) is False):
        raise RuntimeError("planning_insert_round_occurrence: task no longer active")
    expected = params.get("p_expected_task") or {}
    if not ROUND_EXPECTED_TASK_FIELDS <= set(expected):
        raise RuntimeError("planning_insert_round_occurrence: expected task incomplete")
    for field in ROUND_EXPECTED_TASK_FIELDS:
        actual = task.get(field)
        if field == "is_hollow" and actual is not None:
            actual = bool(actual)
        if field in ("window_start_tod", "window_end_tod"):
            actual = str(actual)[:5] if actual is not None else None
            wanted = str(expected[field])[:5] if expected[field] is not None else None
        else:
            wanted = expected[field]
        if actual != wanted:
            raise RuntimeError(
                "planning_insert_round_occurrence: task definition changed during generation")
    for item in params.get("p_rows") or []:
        if item.get("content_snapshot") != task.get("content"):
            raise RuntimeError(
                "planning_insert_round_occurrence: task definition changed during generation")
        # planned_minutes 快照与任务定义一致（hollow 阶段行按各自阶段分钟；
        # 普通行按 estimated_minutes）
        if item.get("phase"):
            expected_planned = (
                task.get("hollow_start_minutes") if item["phase"] == "start"
                else task.get("hollow_end_minutes"))
        else:
            expected_planned = task.get("estimated_minutes")
        if (item.get("planned_minutes") or None) != (expected_planned or None):
            raise RuntimeError(
                "planning_insert_round_occurrence: task definition changed during generation")
        if item.get("status") != "pending" or item.get("source") != "schedule":
            raise RuntimeError(
                "planning_insert_round_occurrence: rows must be pending schedule occurrences")
        for existing in occ_rows:
            if (existing.get("task_id") == item.get("task_id")
                    and existing.get("round_key") == item.get("round_key")
                    and existing.get("phase") == item.get("phase")):
                raise RuntimeError("planning_occurrence_round_phase_uq")
        row = dict(item)
        row.setdefault("id", db.next_id("planning_occurrence"))
        occ_rows.append(row)


def _discard_dt(value):
    """fake 行 / RPC 参数里的时间值（ISO 字符串或 datetime）统一为 datetime。"""
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


def emulate_planning_discard_task(task_rows, occ_rows, params, *,
                                  fact_rows=None, tombstone_rows=None):
    """删除整个任务（§25，2026-10-07）：按执行事实保留全部历史或物理删除。

    与 20261007010000 真库 SQL 同语义（不同步替身会掩盖新分支）：
    * 已删除任务（deleted_at 非空）重复删除返回稳定结果，零改写；
    * 事实判定 = 持久事实门槛（planning_task_completion_fact，经 fact_rows
      传入）或当前行证据（status='completed' / partial_at 非空）任一成立；
    * 历史保留分支：可选 pending 目标行事实补齐（actual 字段白名单，
      含 actual_time_source）→ 执行中行补齐起止 / 耗时（来源 system）→
      关闭全部开放行（discarded + closed_at）→ 停用 + deleted_at；
    * 物理删除分支：删除全部实例行 + 完成事实门槛行 + 任务行 + 登记创建
      请求最小身份（tombstone：request_key / content_digest / deleted_at，
      经 tombstone_rows——R09：不保存业务正文，摘要由调用方传入
      p_creation_digest）；
    * 并发已停用（is_active=false 且未删除）→ RuntimeError（应用层 409）。
    真库锁扫描覆盖全部行并在锁内复核最新版本，fake 单线程按最终状态
    等价仿真。返回 {deleted, history_preserved, already_deleted, closed}。
    """
    task = next((r for r in task_rows if r.get("id") == params.get("p_task_id")), None)
    if task is None:
        raise RuntimeError("planning_discard_task: task not found")
    if task.get("deleted_at"):
        return {"deleted": True, "history_preserved": True,
                "already_deleted": True, "closed": 0}
    now = params.get("p_now")
    target_patch = params.get("p_target_patch") or {}
    if set(target_patch) - ROUND_DISCARD_TARGET_FIELDS:
        raise RuntimeError(
            "planning_discard_task: target patch contains unsupported field")
    task_rows_scope = [r for r in occ_rows if r.get("task_id") == task["id"]]
    has_fact = any(
        r.get("task_id") == task["id"] for r in (fact_rows or []))
    has_fact = has_fact or any(
        r.get("status") == "completed" or r.get("partial_at") is not None
        for r in task_rows_scope)
    if not has_fact:
        # ── 物理删除分支：删实例 + 删门槛 + 删任务 + 登记最小身份 ──────
        for row in list(occ_rows):
            if row.get("task_id") == task["id"]:
                occ_rows.remove(row)
        if fact_rows is not None:
            fact_rows[:] = [r for r in fact_rows if r.get("task_id") != task["id"]]
        task_rows.remove(task)
        closed = len(task_rows_scope)
        key = task.get("creation_request_key")
        if key and tombstone_rows is not None:
            # 与真库同事务登记一致：同键幂等（on conflict do nothing）；
            # 只存请求键 + 语义摘要（R09：正文随任务行真正删除）。
            if not any(r.get("request_key") == key for r in tombstone_rows):
                tombstone_rows.append({
                    "request_key": key,
                    "content_digest": params.get("p_creation_digest") or "",
                    "task_id": task["id"],
                    "deleted_at": now,
                })
        return {"deleted": True, "history_preserved": False,
                "already_deleted": False, "closed": closed}
    # ── 历史保留分支：收口开放行 + 停用 + deleted_at ──────────────────
    if not task.get("is_active"):
        raise RuntimeError("planning_discard_task: task already inactive (concurrent change)")
    if params.get("p_target_id") is not None:
        target = next((r for r in occ_rows
                       if r.get("id") == params.get("p_target_id")
                       and r.get("task_id") == task["id"]), None)
        if (target is not None and target.get("status") == "pending"
                and all(target.get(key) is None for key in
                        ("actual_start", "actual_end", "closed_at", "handled_at"))):
            target.update(target_patch)
    for row in occ_rows:
        if (row.get("task_id") == task["id"]
                and row.get("status") == "in_progress"
                and row.get("closed_at") is None
                and row.get("handled_at") is None):
            start = row.get("actual_start")
            end = row.get("actual_end")
            if row.get("id") == params.get("p_target_id"):
                if start is None and target_patch.get("actual_start") is not None:
                    start = target_patch["actual_start"]
                if end is None and target_patch.get("actual_end") is not None:
                    end = target_patch["actual_end"]
            if end is None:
                end = now
            start_dt = _discard_dt(start)
            end_dt = _discard_dt(end)
            if start_dt is not None and end_dt < start_dt:
                raise RuntimeError(
                    "planning_discard_task: actual_end must not precede actual_start")
            row["actual_start"] = start
            row["actual_end"] = end
            row["actual_minutes"] = (
                None if start_dt is None
                else max(0, round((end_dt - start_dt).total_seconds() / 60)))
            # 系统收口的结束时间不冒充用户计时（§12.3）。
            row["actual_time_source"] = "system"
    closed = 0
    for row in occ_rows:
        if (row.get("task_id") == task["id"]
                and row.get("status") in ("pending", "in_progress", "deferred", "partial")):
            row["status"] = "discarded"
            row["closed_at"] = now
            row["updated_at"] = now
            closed += 1
    task["is_active"] = False
    task["deleted_at"] = now
    return {"deleted": True, "history_preserved": True,
            "already_deleted": False, "closed": closed}


SPLIT_PART_FIELDS = frozenset({"content", "estimated_minutes"})


def emulate_planning_split_occurrence(db, params):
    """#1（2026-10-02）：单事务原子拆分的 fake 仿真——与 20261002030000
    真库 SQL 同语义（不同步替身会掩盖事务边界缺陷）：
    * 任务行存在；parts 形状（1～10 个对象、恰含 content / estimated_minutes、
      内容非空 ≤500、耗时 1–1440 整数）写前校验，违规零写入；
    * 条件关闭当前轮（开放状态集合内命中，discarded_this + handled_at）——
      0 行命中 = 已被并发操作关闭 / 已拆分收口 → RuntimeError（应用层映射
      409），零任务创建；
    * 1～10 个 once 任务创建（fake 单线程顺序追加 = 事务内语句）；
    * after_completion 基准：锁内按关闭后的轮次行重算——全部行均有
      handled_at 时按 max 推进（与应用层旧「关闭后读行再写」同语义）。
    """
    task = next((r for r in db.rows["planning_task"]
                 if r.get("id") == params.get("p_task_id")), None)
    if task is None:
        raise RuntimeError("planning_split_occurrence: task not found")
    parts = params.get("p_parts")
    if (not isinstance(parts, list) or not 1 <= len(parts) <= 10
            or any(
                not isinstance(part, dict)
                or set(part) - SPLIT_PART_FIELDS
                or not SPLIT_PART_FIELDS <= set(part)
                or not isinstance(part.get("content"), str)
                or not part["content"].strip()
                or len(part["content"]) > 500
                or not isinstance(part.get("estimated_minutes"), int)
                or isinstance(part.get("estimated_minutes"), bool)
                or not 1 <= part["estimated_minutes"] <= 1440
                for part in parts)):
        raise RuntimeError("planning_split_occurrence: invalid part item")
    minutes = params.get("p_after_completion_minutes")
    if minutes is not None and (not isinstance(minutes, int)
                                 or isinstance(minutes, bool)
                                 or not 1 <= minutes <= 525600):
        raise RuntimeError("planning_split_occurrence: invalid after_completion interval")
    open_states = ("pending", "in_progress", "deferred", "partial")
    round_rows = [row for row in db.rows["planning_occurrence"]
                  if row.get("task_id") == task["id"]
                  and row.get("round_key") == params.get("p_round_key")
                  and row.get("status") in open_states]
    if not round_rows:
        raise RuntimeError(
            "planning_split_occurrence: round already closed (concurrent change)")
    now = params.get("p_now")
    for row in round_rows:
        row["status"] = "discarded_this"
        row["closed_at"] = now
        row["handled_at"] = now
        row["updated_at"] = now
    created_ids = []
    for part in parts:
        new_task = {
            "id": db.next_id("planning_task"),
            "content": part["content"].strip(),
            "task_type": "once",
            "time_mode": "duration",
            "estimated_minutes": part["estimated_minutes"],
            "target_date": params.get("p_target_date"),
            "refresh_mode": "none",
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
        db.rows["planning_task"].append(new_task)
        created_ids.append(new_task["id"])
    if minutes is not None:
        round_all = [row for row in db.rows["planning_occurrence"]
                     if row.get("task_id") == task["id"]
                     and row.get("round_key") == params.get("p_round_key")]
        if round_all and all(row.get("handled_at") for row in round_all):
            handled = max(_discard_dt(row["handled_at"]) for row in round_all)
            if task.get("is_active"):
                task["last_handled_at"] = handled.isoformat()
                task["refresh_next_due_at"] = (
                    handled + timedelta(minutes=minutes)).isoformat()
            else:
                raise RuntimeError(
                    "planning_split_occurrence: task already inactive (concurrent change)")
    return created_ids


RECOMPUTE_SINGLE_FIELDS = frozenset({
    "id", "est_start", "est_end", "nominal_start", "estimated_time_source",
    "fixed_source", "schedule_managed", "is_fixed", "updated_at",
})
RECOMPUTE_ROUND_FIELDS = frozenset({
    "target_id", "sibling_id", "target_patch", "sibling_patch",
})
# #25（2026-10-02）：批量重算 expected 必填键 = 既有排程输入键 + 生命周期
# 事实字段（与 planning.LIFECYCLE_FACT_FIELDS 同一集合，单一来源）。
RECOMPUTE_EXPECTED_FIELDS = ROUND_PATCH_EXPECTED_FIELDS | set(
    planning.LIFECYCLE_FACT_FIELDS)


def emulate_planning_recompute_batch(db, params):
    """#6（2026-10-02）：重算整批事务的 fake 仿真——与 20261002040000
    真库 SQL 同语义（不同步替身会掩盖整批回滚缺陷）：
    * expected 快照（全部参与计算行）形状与必填键（#13 同契约，含
      sort_order；#25 补生命周期事实），任一行缺失或漂移 → RuntimeError
      （应用层映射并发拒绝，整批放弃）；
    * singles / rounds 键硬白名单，待写行 id 必须已在 expected 集合内；
    * singles 补丁按键应用（缺键 = 保持现值），写入前锁内重核同一可排程
      条件（#25：状态 pending + 生命周期事实全空 + 可重排所有权元组，
      未命中 → RuntimeError 整批放弃）；
    * rounds 逐轮复用 emulate_planning_round_patch（expected 传 None——
      全量复核已在批级完成）；
    * 事务边界（复审 26.10.2.15.01 R1）：任一后续 single / round 失败，
      本次调用内已执行的修改**整体撤回**——调用前已提交的 actual_start 等
      事实不在本次写集合内、原样保留，本次新增的字段一并移除；恢复就地
      作用于原 row 字典（外部持有的引用不得指向半修改对象），原异常向上
      传播。成功路径照常返回写集。
    """
    occ_rows = db.rows["planning_occurrence"]
    expected = params.get("p_expected")
    singles = params.get("p_singles") or []
    rounds = params.get("p_rounds") or []
    if (not isinstance(expected, list) or not expected
            or any(not isinstance(e, dict)
                   or not RECOMPUTE_EXPECTED_FIELDS <= set(e) for e in expected)):
        raise RuntimeError(
            "planning_apply_recompute_batch: invalid expected snapshot")
    if (not isinstance(singles, list) or not isinstance(rounds, list)
            or any(not isinstance(e, dict)
                   or not {"id", "updated_at"} <= set(e)
                   or set(e) - RECOMPUTE_SINGLE_FIELDS for e in singles)
            or any(not isinstance(e, dict)
                   or not RECOMPUTE_ROUND_FIELDS <= set(e)
                   or set(e) - RECOMPUTE_ROUND_FIELDS for e in rounds)):
        raise RuntimeError(
            "planning_apply_recompute_batch: invalid batch payload")
    expected_ids = {e.get("id") for e in expected}
    if any(e.get("id") not in expected_ids for e in singles):
        raise RuntimeError(
            "planning_apply_recompute_batch: invalid single write")
    if any(e.get("target_id") not in expected_ids
           or e.get("sibling_id") not in expected_ids for e in rounds):
        raise RuntimeError(
            "planning_apply_recompute_batch: invalid round write")
    rows_by_id = {row.get("id"): row for row in occ_rows}
    for e in expected:
        row = rows_by_id.get(e.get("id"))
        if row is None:
            raise RuntimeError(
                "planning_apply_recompute_batch: schedule inputs drifted "
                "(stale recompute result)")
        for key, value in e.items():
            if key == "id":
                continue
            if row.get(key) != value:
                raise RuntimeError(
                    "planning_apply_recompute_batch: schedule inputs drifted "
                    "(stale recompute result)")
    written = []
    # 写集合事务恢复：首次触及某行前快照其调用前完整状态（浅拷贝含「字段
    # 不存在」形状）；失败时 clear + update 就地还原——字段值回原值、本次
    # 新增字段被移除、字典对象身份不变。未触及行（含调用前已提交的并发
    # 事实）不动。
    pre_call: dict[int, dict] = {}

    def _remember(row_id) -> None:
        if row_id not in pre_call:
            pre_call[row_id] = dict(rows_by_id[row_id])

    try:
        for entry in singles:
            row = rows_by_id[entry["id"]]
            # #25：写入前重核同一可排程条件（迁移语句 5 的 WHERE 同形）。
            if (row.get("status") != "pending"
                    or any(row.get(field)
                           for field in planning.LIFECYCLE_FACT_FIELDS)
                    or row.get("is_fixed")
                    or row.get("schedule_managed") is not True
                    or row.get("fixed_source") is not None
                    or row.get("estimated_time_source")
                    not in ("unassigned", "automatic", "rule")):
                raise RuntimeError(
                    "planning_apply_recompute_batch: schedule inputs drifted "
                    "(stale recompute result)")
            _remember(entry["id"])
            for key, value in entry.items():
                if key == "id":
                    continue
                row[key] = value
            written.append(entry["id"])
        for entry in rounds:
            _remember(entry["target_id"])
            _remember(entry["sibling_id"])
            emulate_planning_round_patch(occ_rows, {
                "p_target_id": entry["target_id"],
                "p_sibling_id": entry["sibling_id"],
                "p_target_patch": entry["target_patch"],
                "p_sibling_patch": entry["sibling_patch"],
                "p_expected": None,
            })
            written.extend([entry["target_id"], entry["sibling_id"]])
    except BaseException:
        for row_id, snapshot in pre_call.items():
            row = rows_by_id[row_id]
            row.clear()
            row.update(snapshot)
        raise
    return written


def emulate_planning_request_recompute(state_rows, params):
    """批次 6 A1：登记重算请求——数据库原子消费身份的 fake 仿真。

    每次调用生成新的 request_token（真库 gen_random_uuid 在函数体内生成，
    同一 requested_at 的两次登记必然得到不同 token）；fake 用自增整数 +
    uuid 混合保证可读与唯一。
    """
    row = next((r for r in state_rows if r.get("id") == 1), None)
    if row is None:
        row = {"id": 1}
        state_rows.append(row)
    token = emulate_planning_request_recompute._counter = (
        getattr(emulate_planning_request_recompute, "_counter", 0) + 1)
    row["requested_at"] = params.get("p_requested_at")
    row["reason"] = (params.get("p_reason") or "")[:100]
    row["request_token"] = f"token-{token}"
    row["updated_at"] = params.get("p_requested_at")
    return row["request_token"]


def emulate_planning_clear_recompute_mark(state_rows, params):
    """批次 6 A1：条件清除——只命中 request_token 等值的行（0 行 = 新请求
    保留给下一轮消费）。token 为 NULL 时不清除（捕获时本无待处理请求）。"""
    token = params.get("p_request_token")
    if not token:
        return 0
    row = next((r for r in state_rows if r.get("id") == 1), None)
    if row is not None and row.get("request_token") == token:
        row["requested_at"] = None
        row["reason"] = None
        row["request_token"] = None
        row["updated_at"] = None
        return 1
    return 0


def emulate_planning_update_cycle_boundary(db, params):
    """批次 7：boundary 原子保存 RPC 的 fake 仿真（真库语义由 pgserver 验证）。

    镜像迁移 20260930020000 的单事务语义：状态行 CAS → 调整项校验 →
    锁内按新 boundary 全量校验启用中模板（调整者用提交值）→ 冲突零写入
    返回 → 全部通过才一次性提交（任务更新 + 状态写入，任一失败整体
    不生效——fake 以「先验证后提交」表达数据库事务回滚）。boundary 状态
    经 planning.db.load_app_setting / save_app_setting（与调用方同一份
    patched 设置存储）；跨越判断复用批次 1 领域函数（与 SQL 同一规则）。
    """
    from gateway.planning_window import WindowTemplate, window_crosses_boundary
    state_key = planning.PLANNING_BOUNDARY_STATE_KEY
    current = planning.db.load_app_setting(state_key)
    if not isinstance(current, dict):
        current = {"boundary": "06:00", "transition": None, "absorbed": []}
    expected = params.get("p_expected_state") or {}

    def _transition_identity(t):
        if not isinstance(t, dict):
            return ("", "")
        return (t.get("spanning_key") or "", t.get("change_at") or "")

    cur_t = _transition_identity(current.get("transition"))
    exp_t = _transition_identity(expected.get("transition"))
    if ((current.get("boundary") or "06:00") != (expected.get("boundary") or "06:00")
            or cur_t != exp_t):
        return {"status": "stale_state"}

    boundary = params.get("p_new_boundary") or "06:00"
    adjustments = params.get("p_adjustments") or []
    adj_by_id = {}
    for item in adjustments:
        task_id = item["task_id"]
        if task_id in adj_by_id:
            raise RuntimeError("planning_update_cycle_boundary: duplicate adjustment task")
        start, end = item.get("window_start_tod"), item.get("window_end_tod")
        if start and end and time.fromisoformat(str(start)) == time.fromisoformat(str(end)):
            raise RuntimeError(
                "planning_update_cycle_boundary: window start and end must differ")
        adj_by_id[task_id] = (start, end)

    conflicts = []
    pending_updates = []
    for task in db.rows["planning_task"]:
        if not task.get("is_active"):
            continue
        if task["id"] in adj_by_id:
            start, end = adj_by_id[task["id"]]
            # 调整项无论形状（双侧 / 单侧 / 清空）都参与同一事务提交
            pending_updates.append((task, (start, end)))
        else:
            start, end = task.get("window_start_tod"), task.get("window_end_tod")
        if not start or not end:
            continue  # 无窗口 / 单侧约束不构成区间（§6.7）
        template = WindowTemplate(
            start_tod=time.fromisoformat(str(start)) if start else None,
            end_tod=time.fromisoformat(str(end)) if end else None)
        if window_crosses_boundary(template, boundary):
            conflicts.append({
                "task_id": task["id"], "content": task.get("content"),
                "window_start_tod": start, "window_end_tod": end,
            })
    if conflicts:
        return {"status": "conflicts", "conflicts": conflicts}

    new_state = {
        "boundary": boundary,
        "transition": params.get("p_transition"),
        "absorbed": params.get("p_absorbed") or [],
    }
    # 提交（单事务语义）：状态写入失败 → 抛出且任务更新不落 fake 行；
    # 状态写入成功 → 任务窗口模板更新一并生效。
    if not planning.db.save_app_setting(state_key, new_state):
        raise RuntimeError("planning_update_cycle_boundary: state save failed")
    updated = 0
    for task, (start, end) in pending_updates:
        if (task.get("window_start_tod") or None) != (start or None) or (
                task.get("window_end_tod") or None) != (end or None):
            task["window_start_tod"] = start
            task["window_end_tod"] = end
            updated += 1
    return {"status": "ok", "updated_tasks": updated}
