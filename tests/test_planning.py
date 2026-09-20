"""规划管理核心逻辑单元测试（假 supabase 查询构造器，stdlib unittest）。"""

import importlib.util
import sys
import types
import unittest
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch


if "dotenv" not in sys.modules and importlib.util.find_spec("dotenv") is None:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv

from gateway import planning
from gateway.planning import PlanningError

MODULE = "gateway.planning"
CST = timezone(timedelta(hours=8))

UNIQUE_VIOLATION = (
    'duplicate key value violates unique constraint '
    '"planning_occurrence_schedule_slot_uq"'
)


def _cst(*args) -> datetime:
    return datetime(*args, tzinfo=CST)


class _Query:
    def __init__(self, client, table):
        self.client = client
        self.table = table
        self.filters = []
        self.orders = []
        self.row_limit = None
        self.op = "select"
        self.payload = None
        self.ignore_duplicates = False

    def select(self, fields="*"):
        self.op = "select"
        return self

    def insert(self, data):
        self.op = "insert"
        self.payload = dict(data)
        return self

    def update(self, data):
        self.op = "update"
        self.payload = dict(data)
        return self

    def delete(self):
        self.op = "delete"
        return self

    def upsert(self, data, ignore_duplicates=False):
        self.op = "upsert"
        self.payload = dict(data)
        self.ignore_duplicates = ignore_duplicates
        return self

    def eq(self, field, value):
        self.filters.append(("eq", field, value))
        return self

    def in_(self, field, values):
        self.filters.append(("in", field, list(values)))
        return self

    def gte(self, field, value):
        self.filters.append(("gte", field, value))
        return self

    def lte(self, field, value):
        self.filters.append(("lte", field, value))
        return self

    def lt(self, field, value):
        self.filters.append(("lt", field, value))
        return self

    def order(self, field, desc=False):
        self.orders.append((field, desc))
        return self

    def limit(self, value):
        self.row_limit = value
        return self

    def _compare(self, row_value, kind, value):
        if row_value is None:
            return False
        if kind == "gte":
            return row_value >= value
        if kind == "lte":
            return row_value <= value
        if kind == "lt":
            return row_value < value
        return False

    def _matches(self, row):
        for kind, field, value in self.filters:
            row_value = row.get(field)
            if kind == "eq" and row_value != value:
                return False
            if kind == "in" and row_value not in value:
                return False
            if kind in ("gte", "lte", "lt") and not self._compare(row_value, kind, value):
                return False
        return True

    def execute(self):
        rows = self.client.rows.setdefault(self.table, [])
        if self.op == "insert":
            if self.table == "planning_occurrence" and self.payload.get("source") == "schedule":
                for existing in rows:
                    if (
                        existing.get("task_id") == self.payload.get("task_id")
                        and existing.get("for_date") == self.payload.get("for_date")
                        and existing.get("phase") == self.payload.get("phase")
                    ):
                        raise RuntimeError(UNIQUE_VIOLATION)
            row = dict(self.payload)
            row["id"] = self.client.next_id(self.table)
            rows.append(row)
            return SimpleNamespace(data=[dict(row)])
        if self.op == "upsert":
            key = self.payload.get("id")
            matched = [row for row in rows if row.get("id") == key]
            if matched and not self.ignore_duplicates:
                for row in matched:
                    row.update(self.payload)
                return SimpleNamespace(data=[dict(row) for row in matched])
            if matched and self.ignore_duplicates:
                return SimpleNamespace(data=[dict(row) for row in matched])
            row = dict(self.payload)
            row.setdefault("id", self.client.next_id(self.table))
            rows.append(row)
            return SimpleNamespace(data=[dict(row)])
        matched = [row for row in rows if self._matches(row)]
        for field, desc in self.orders:
            matched.sort(key=lambda row: row.get(field) or "", reverse=desc)
        if self.row_limit is not None:
            matched = matched[: self.row_limit]
        if self.op == "update":
            for row in matched:
                row.update(self.payload)
        if self.op == "delete":
            for row in matched:
                rows.remove(row)
        return SimpleNamespace(data=[dict(row) for row in matched])


class _Client:
    """最小 supabase 查询构造器替身：内存行 + 乐观 id + 唯一槽位约束。"""

    def __init__(self):
        self.rows = {
            "planning_task": [],
            "planning_occurrence": [],
            "planning_recompute_state": [{"id": 1, "requested_at": None, "reason": None}],
        }
        self._counters = {}

    def next_id(self, table):
        self._counters[table] = self._counters.get(table, 0) + 1
        return self._counters[table]

    def table(self, name):
        if name not in self.rows:
            raise AssertionError(f"unexpected table access: {name}")
        return _Query(self, name)


def _setup(module_now=None):
    """返回 (client, contextmanager)，统一 patch get_client 与时间。"""
    client = _Client()

    class _Patches:
        def __init__(self, now):
            self._now = now
            self._tokens = []

        def __enter__(self):
            self._tokens.append(patch(f"{MODULE}.get_client", return_value=client))
            if self._now is not None:
                self._tokens.append(patch.object(planning, "_now", lambda: self._now))
            for token in self._tokens:
                token.start()
            return client

        def __exit__(self, *exc):
            for token in reversed(self._tokens):
                token.stop()
            return False

    return client, (lambda now=module_now: _Patches(now))


class _Base(unittest.TestCase):
    NOW = _cst(2026, 9, 20, 14, 7)

    def setUp(self):
        self.client, self.ctx = _setup()

    def _seed(self, client, rows):
        for index, row in enumerate(rows, start=1):
            occurrence = {
                "id": index, "task_id": row["task_id"], "for_date": "2026-09-20",
                "phase": None, "est_start": None, "est_end": None,
                "nominal_start": None, "actual_start": None, "actual_end": None,
                "actual_minutes": None, "status": "pending", "partial_note": None,
                "sort_order": row.get("sort_order", index * 10),
                "is_fixed": row.get("is_fixed", False), "is_limited": False,
                "closed_at": None, "source": "schedule",
                "created_at": planning._iso(self.NOW), "updated_at": planning._iso(self.NOW),
            }
            occurrence.update({k: v for k, v in row.items() if k not in ("task_id", "sort_order")})
            client.rows["planning_occurrence"].append(occurrence)

    def _seed_tasks(self, client, rows):
        for row in rows:
            client.rows["planning_task"].append({
                "id": row["id"], "content": row.get("content", "任务"),
                "task_type": row.get("task_type", "daily"), "interval_days": None,
                "weekdays": None, "month_days": None, "target_date": None,
                "time_mode": row.get("time_mode", "duration"),
                "estimated_minutes": row.get("estimated_minutes", 30),
                "est_start_tod": row.get("est_start_tod"),
                "est_end_tod": row.get("est_end_tod"),
                "is_fixed": row.get("is_fixed", False),
                "deadline_tod": None, "deadline_end_tod": None,
                "is_hollow": row.get("is_hollow", False),
                "hollow_start_content": row.get("hollow_start_content"),
                "hollow_start_minutes": row.get("hollow_start_minutes"),
                "hollow_wait_minutes": row.get("hollow_wait_minutes"),
                "hollow_wait_note": None, "hollow_end_content": None,
                "hollow_end_minutes": row.get("hollow_end_minutes"),
                "alarm_start": False, "alarm_end": False, "timer_minutes": None,
                "is_active": True, "cursor_date": "2026-09-19",
                "next_due": None, "created_at": planning._iso(self.NOW),
                "updated_at": planning._iso(self.NOW),
            })

    # helper -----------------------------------------------------------------
    def run_with(self, fn):
        with self.ctx(self.NOW):
            return fn(self.client)

    def create_task(self, client, **overrides):
        cursor = overrides.pop("cursor_date", None)
        next_due = overrides.pop("next_due", None)
        payload = {"content": "背单词", "task_type": "daily", "estimated_minutes": 30}
        payload.update(overrides)
        task = planning.create_task(payload, self.NOW)
        stored = next(row for row in client.rows["planning_task"] if row["id"] == task["id"])
        # 游标是系统管理的字段，测试里直接改存储行模拟历史状态。
        if cursor is not None:
            stored["cursor_date"] = cursor
        if next_due is not None:
            stored["next_due"] = next_due
        return task


class TaskValidationTests(_Base):
    def test_daily_minimal_create(self):
        def run(client):
            task = self.create_task(client)
            self.assertEqual(task["task_type"], "daily")
            self.assertEqual(task["time_mode"], "duration")
            self.assertTrue(task["is_active"])
            self.assertIsNone(task["cursor_date"])
        self.run_with(run)

    def test_type_required_fields(self):
        cases = [
            ({"task_type": "interval", "content": "x"}, "interval_days"),
            ({"task_type": "weekly", "content": "x", "weekdays": []}, "weekdays"),
            ({"task_type": "monthly", "content": "x", "month_days": []}, "month_days"),
            ({"task_type": "once", "content": "x"}, "target_date"),
        ]
        for payload, field in cases:
            with self.subTest(task_type=payload["task_type"]):
                def run(client):
                    with self.assertRaises(PlanningError) as raised:
                        planning.create_task(payload, self.NOW)
                    self.assertIn(field, str(raised.exception))
                self.run_with(run)

    def test_unknown_fields_rejected(self):
        def run(client):
            with self.assertRaises(PlanningError):
                planning.create_task(
                    {"content": "x", "task_type": "daily", "estimated_minutes": 30,
                     "priority": 5},
                    self.NOW,
                )
        self.run_with(run)

    def test_explicit_times_override_duration(self):
        def run(client):
            task = self.create_task(
                client, time_mode="duration", estimated_minutes=120,
                est_start_tod="20:00", est_end_tod="22:00",
            )
            self.assertEqual(task["time_mode"], "explicit")
            self.assertEqual(task["est_start_tod"], "20:00")
        self.run_with(run)

    def test_timer_shorthand_parsing(self):
        self.assertEqual(planning.parse_duration_shorthand("1h30m", "t"), 90)
        self.assertEqual(planning.parse_duration_shorthand("30m", "t"), 30)
        self.assertEqual(planning.parse_duration_shorthand("1m30s", "t"), 2)
        self.assertEqual(planning.parse_duration_shorthand("30s", "t"), 1)
        self.assertEqual(planning.parse_duration_shorthand("1h", "t"), 60)
        with self.assertRaises(PlanningError):
            planning.parse_duration_shorthand("abc", "t")

    def test_deadline_range_requires_start(self):
        def run(client):
            with self.assertRaises(PlanningError):
                planning.create_task(
                    {"content": "x", "task_type": "once", "target_date": "2026-09-25",
                     "estimated_minutes": 30, "deadline_end_tod": "22:00"},
                    self.NOW,
                )
        self.run_with(run)

    def test_hollow_requires_phase_fields(self):
        def run(client):
            with self.assertRaises(PlanningError):
                planning.create_task(
                    {"content": "煮饭", "task_type": "daily", "estimated_minutes": 10,
                     "is_hollow": True},
                    self.NOW,
                )
        self.run_with(run)


class GenerationTests(_Base):
    def test_daily_generates_once_and_is_idempotent(self):
        def run(client):
            # 创建即生成（BUG-4）：当天实例立刻存在
            self.create_task(client)
            occurrences = client.rows["planning_occurrence"]
            self.assertEqual([occ["for_date"] for occ in occurrences], ["2026-09-20"])
            self.assertEqual(
                next(row for row in client.rows["planning_task"])["cursor_date"],
                "2026-09-20",
            )
            # 再跑一次不重复；游标被竞态拨回也会被唯一索引挡住
            again = planning.generate_due(self.NOW)
            self.assertEqual(again["created"], 0)
            stored = client.rows["planning_task"][0]
            stored["cursor_date"] = "2026-09-19"
            self.assertEqual(planning.generate_due(self.NOW)["created"], 0)
            self.assertEqual(len(client.rows["planning_occurrence"]), 1)
        self.run_with(run)

    def test_regression_deleted_discarded_occurrences_never_regenerate(self):
        """72 小时清理安全前提：删除出现记录不会重新生成或漏生成。"""
        def run(client):
            self.create_task(client, cursor_date="2026-09-19")
            planning.generate_due(self.NOW)
            # 模拟清理：删除全部出现记录（含废弃记录被清理后的状态）
            client.rows["planning_occurrence"].clear()
            result = planning.generate_due(self.NOW)
            self.assertEqual(result["created"], 0)
            self.assertEqual(client.rows["planning_occurrence"], [])
        self.run_with(run)

    def test_missed_days_backfill_without_duplicates(self):
        def run(client):
            self.create_task(client, cursor_date="2026-09-16")
            result = planning.generate_due(self.NOW)
            dates = sorted(occ["for_date"] for occ in client.rows["planning_occurrence"])
            # 当天实例已由「创建即生成」产出，这里只补漏掉的 17-19 日
            self.assertEqual(dates, ["2026-09-17", "2026-09-18", "2026-09-19", "2026-09-20"])
            self.assertEqual(result["created"], 3)
        self.run_with(run)

    def test_weekly_only_matching_weekdays(self):
        def run(client):
            # 2026-09-14 是周一，16/18 是周三、周五
            self.create_task(
                client, task_type="weekly", weekdays=[0, 2, 4],
                cursor_date="2026-09-13", estimated_minutes=60,
            )
            planning.generate_due(self.NOW)
            dates = [occ["for_date"] for occ in client.rows["planning_occurrence"]]
            self.assertEqual(dates, ["2026-09-14", "2026-09-16", "2026-09-18"])
        self.run_with(run)

    def test_monthly_skips_missing_dates(self):
        def run(client):
            self.create_task(
                client, task_type="monthly", month_days=[31],
                cursor_date="2026-01-31", estimated_minutes=30,
            )
            planning.generate_due(self.NOW)
            dates = [occ["for_date"] for occ in client.rows["planning_occurrence"]]
            # 2 月没有 31 日：跳过；3/5/7/8 月正常（当月无此日期则跳过）
            self.assertEqual(dates, ["2026-03-31", "2026-05-31", "2026-07-31", "2026-08-31"])
        self.run_with(run)

    def test_once_task_with_past_target_date_generates(self):
        def run(client):
            self.create_task(
                client, task_type="once", target_date="2026-09-18",
                estimated_minutes=30,
            )
            planning.generate_due(self.NOW)
            dates = [occ["for_date"] for occ in client.rows["planning_occurrence"]]
            self.assertEqual(dates, ["2026-09-18"])
        self.run_with(run)

    def test_interval_due_generates_then_completion_sets_next_due(self):
        def run(client):
            task = self.create_task(
                client, task_type="interval", interval_days=3,
                next_due=planning._iso(_cst(2026, 9, 20, 9, 0)),
            )
            planning.generate_due(self.NOW)
            self.assertEqual(
                [occ["for_date"] for occ in client.rows["planning_occurrence"]],
                ["2026-09-20"],
            )
            stored = next(row for row in client.rows["planning_task"] if row["id"] == task["id"])
            self.assertIsNone(stored["next_due"])
            # 完成后以完成时刻 + 间隔重算
            occ = client.rows["planning_occurrence"][0]
            planning.set_occurrence_status(occ["id"], {"status": "completed"}, self.NOW)
            self.assertEqual(stored["next_due"], planning._iso(self.NOW + timedelta(days=3)))
        self.run_with(run)

    def test_interval_regression_deleted_records_do_not_miss_or_duplicate(self):
        def run(client):
            task = self.create_task(
                client, task_type="interval", interval_days=3,
                next_due=planning._iso(_cst(2026, 9, 20, 9, 0)),
            )
            planning.generate_due(self.NOW)
            occ = client.rows["planning_occurrence"][0]
            planning.set_occurrence_status(occ["id"], {"status": "discarded_this"}, self.NOW)
            # 72 小时清理删除此次废弃记录
            client.rows["planning_occurrence"].clear()
            stored = next(row for row in client.rows["planning_task"] if row["id"] == task["id"])
            # next_due 仍由完成时刻驱动，删除记录不会漏生成
            self.assertEqual(stored["next_due"], planning._iso(self.NOW + timedelta(days=3)))
            self.assertEqual(planning.generate_due(self.NOW)["created"], 0)
        self.run_with(run)

    def test_hollow_task_generates_two_linked_phases(self):
        def run(client):
            self.create_task(
                client, content="煮饭", estimated_minutes=10,
                is_hollow=True, hollow_start_content="准备食材",
                hollow_start_minutes=10, hollow_wait_minutes=30,
                hollow_wait_note="烹煮", hollow_end_content="处理完成",
                hollow_end_minutes=5, cursor_date="2026-09-19",
            )
            planning.generate_due(self.NOW)
            phases = sorted(occ["phase"] for occ in client.rows["planning_occurrence"])
            self.assertEqual(phases, ["end", "start"])
            self.assertEqual(len(client.rows["planning_occurrence"]), 2)
        self.run_with(run)

    def test_duplicate_slot_is_skipped_not_duplicated(self):
        def run(client):
            self.create_task(client, cursor_date=None)
            planning.generate_due(self.NOW)
            stored = client.rows["planning_task"][0]
            self.assertEqual(stored["cursor_date"], "2026-09-20")
            # 模拟竞态重放：游标被拨回后再次生成，唯一索引命中并跳过
            stored["cursor_date"] = "2026-09-19"
            planning.generate_due(self.NOW)
            self.assertEqual(len(client.rows["planning_occurrence"]), 1)
        self.run_with(run)


class RecomputeTests(_Base):
    def test_schedulable_items_flow_from_now_in_order(self):
        def run(client):
            self._seed_tasks(client, [
                {"id": 1, "estimated_minutes": 30},
                {"id": 2, "estimated_minutes": 20},
                {"id": 3, "estimated_minutes": 60},
            ])
            self._seed(client, [
                {"task_id": 1, "sort_order": 10},
                {"task_id": 2, "sort_order": 20},
                {"task_id": 3, "sort_order": 30},
            ])
            result = planning.recompute_today(self.NOW)
            self.assertEqual(result["updated"], 3)
            rows = {row["id"]: row for row in client.rows["planning_occurrence"]}
            self.assertEqual(rows[1]["est_start"], planning._iso(self.NOW))
            self.assertEqual(rows[2]["est_start"], planning._iso(self.NOW + timedelta(minutes=30)))
            self.assertEqual(rows[3]["est_start"], planning._iso(self.NOW + timedelta(minutes=50)))
            self.assertEqual(rows[3]["est_end"], planning._iso(self.NOW + timedelta(minutes=110)))
        self.run_with(run)

    def test_fixed_slot_is_kept_and_schedulable_items_avoid_it(self):
        def run(client):
            fixed_start = _cst(2026, 9, 20, 20, 0)
            fixed_end = _cst(2026, 9, 20, 22, 0)
            self._seed_tasks(client, [
                {"id": 1, "estimated_minutes": 90},
                {"id": 2, "time_mode": "explicit", "est_start_tod": "20:00",
                 "est_end_tod": "22:00", "is_fixed": True},
                {"id": 3, "estimated_minutes": 30},
            ])
            self._seed(client, [
                {"task_id": 1, "sort_order": 10},
                {"task_id": 2, "sort_order": 20,
                 "est_start": planning._iso(fixed_start), "est_end": planning._iso(fixed_end),
                 "is_fixed": True},
                {"task_id": 3, "sort_order": 30},
            ])
            planning.recompute_today(self.NOW)
            rows = {row["id"]: row for row in client.rows["planning_occurrence"]}
            # 任务 1：14:07 + 90m = 15:37，不撞 20:00 的固定槽
            self.assertEqual(rows[1]["est_end"], planning._iso(_cst(2026, 9, 20, 15, 37)))
            # 固定槽不动
            self.assertEqual(rows[2]["est_start"], planning._iso(fixed_start))
            # 任务 3 被推到固定槽之后
            self.assertEqual(rows[3]["est_start"], planning._iso(_cst(2026, 9, 20, 22, 0)))
        self.run_with(run)

    def test_item_after_fixed_slot_in_order_schedules_after_it(self):
        def run(client):
            self._seed_tasks(client, [
                {"id": 1, "estimated_minutes": 60},
                {"id": 2, "time_mode": "explicit", "est_start_tod": "20:00",
                 "est_end_tod": "22:00", "is_fixed": True},
            ])
            self._seed(client, [
                {"task_id": 1, "sort_order": 30},
                {"task_id": 2, "sort_order": 10,
                 "est_start": planning._iso(_cst(2026, 9, 20, 20, 0)),
                 "est_end": planning._iso(_cst(2026, 9, 20, 22, 0)), "is_fixed": True},
            ])
            planning.recompute_today(self.NOW)
            rows = {row["id"]: row for row in client.rows["planning_occurrence"]}
            # 固定槽保留 20:00-22:00；列表顺序在其后的可排程项顺延到槽后
            self.assertEqual(rows[2]["est_start"], planning._iso(_cst(2026, 9, 20, 20, 0)))
            self.assertEqual(rows[1]["est_start"], planning._iso(_cst(2026, 9, 20, 22, 0)))
            self.assertEqual(rows[1]["est_end"], planning._iso(_cst(2026, 9, 20, 23, 0)))
        self.run_with(run)

    def test_schedulable_item_pushed_past_overlapping_later_slot(self):
        def run(client):
            self._seed_tasks(client, [
                {"id": 1, "estimated_minutes": 420},
                {"id": 2, "time_mode": "explicit", "est_start_tod": "20:00",
                 "est_end_tod": "22:00", "is_fixed": True},
            ])
            self._seed(client, [
                {"task_id": 1, "sort_order": 10},
                {"task_id": 2, "sort_order": 20,
                 "est_start": planning._iso(_cst(2026, 9, 20, 20, 0)),
                 "est_end": planning._iso(_cst(2026, 9, 20, 22, 0)), "is_fixed": True},
            ])
            planning.recompute_today(self.NOW)
            rows = {row["id"]: row for row in client.rows["planning_occurrence"]}
            # 14:07 起排 7h 会撞 20:00 的固定槽 → 顺延到槽结束之后
            self.assertEqual(rows[1]["est_start"], planning._iso(_cst(2026, 9, 20, 22, 0)))
            self.assertEqual(rows[1]["est_end"], planning._iso(_cst(2026, 9, 21, 5, 0)))
        self.run_with(run)

    def test_hollow_end_phase_waits_for_start_plus_gap(self):
        def run(client):
            self._seed_tasks(client, [{
                "id": 1, "estimated_minutes": 10, "is_hollow": True,
                "hollow_wait_minutes": 30, "hollow_end_minutes": 5,
            }])
            self._seed(client, [
                {"task_id": 1, "phase": "start", "sort_order": 10},
                {"task_id": 1, "phase": "end", "sort_order": 11},
            ])
            planning.recompute_today(self.NOW)
            rows = {row["id"]: row for row in client.rows["planning_occurrence"] if row["phase"]}
            start_row = next(row for row in rows.values() if row["phase"] == "start")
            end_row = next(row for row in rows.values() if row["phase"] == "end")
            self.assertEqual(start_row["est_end"], planning._iso(self.NOW + timedelta(minutes=10)))
            self.assertEqual(end_row["est_start"], planning._iso(self.NOW + timedelta(minutes=40)))
            self.assertEqual(end_row["est_end"], planning._iso(self.NOW + timedelta(minutes=45)))
        self.run_with(run)

    def test_manual_recompute_clears_wait_mark(self):
        def run(client):
            planning.request_recompute("reorder", self.NOW)
            self.assertTrue(planning.get_recompute_state(self.NOW)["pending"])
            planning.trigger_recompute(self.NOW)
            self.assertFalse(planning.get_recompute_state(self.NOW)["pending"])
        self.run_with(run)


class StatusTransitionTests(_Base):
    def _seed_task_and_occ(self, client, **task_overrides):
        before = {row["id"] for row in client.rows["planning_occurrence"]}
        task = self.create_task(client, cursor_date="2026-09-19", **task_overrides)
        planning.generate_due(self.NOW)
        occ = next(row for row in client.rows["planning_occurrence"] if row["id"] not in before)
        return task, occ

    def test_start_and_finish_record_actual_times(self):
        def run(client):
            task, occ = self._seed_task_and_occ(client)
            planning.start_occurrence(occ["id"], self.NOW)
            started = client.rows["planning_occurrence"][0]
            self.assertEqual(started["status"], "in_progress")
            self.assertEqual(started["actual_start"], planning._iso(self.NOW))
            later = self.NOW + timedelta(minutes=35)
            planning.finish_occurrence(occ["id"], later)
            finished = client.rows["planning_occurrence"][0]
            self.assertEqual(finished["status"], "completed")
            self.assertEqual(finished["actual_minutes"], 35)
            self.assertIsNotNone(finished["closed_at"])
        self.run_with(run)

    def test_timeout_cannot_be_completed(self):
        def run(client):
            task, occ = self._seed_task_and_occ(client)
            client.rows["planning_occurrence"][0]["status"] = "timeout"
            with self.assertRaises(PlanningError) as raised:
                planning.set_occurrence_status(occ["id"], {"status": "completed"}, self.NOW)
            self.assertEqual(raised.exception.status_code, 422)
        self.run_with(run)

    def test_timeout_requires_new_time_to_reschedule(self):
        def run(client):
            task, occ = self._seed_task_and_occ(client)
            client.rows["planning_occurrence"][0]["status"] = "timeout"
            with self.assertRaises(PlanningError):
                planning.set_occurrence_status(occ["id"], {"status": "pending"}, self.NOW)
            new_time = _cst(2026, 9, 21, 9, 0)
            planning.set_occurrence_status(
                occ["id"], {"status": "pending", "est_start": planning._iso(new_time)}, self.NOW,
            )
            rescheduled = client.rows["planning_occurrence"][0]
            self.assertEqual(rescheduled["status"], "pending")
            self.assertEqual(rescheduled["for_date"], "2026-09-21")
            self.assertEqual(rescheduled["est_start"], planning._iso(new_time))
        self.run_with(run)

    def test_discarded_this_only_for_repeating_tasks(self):
        def run(client):
            task, occ = self._seed_task_and_occ(client)
            planning.set_occurrence_status(occ["id"], {"status": "discarded_this"}, self.NOW)
            self.assertEqual(client.rows["planning_occurrence"][0]["status"], "discarded_this")

            task, occ = self._seed_task_and_occ(
                client, task_type="once", target_date="2026-09-20",
            )
            with self.assertRaises(PlanningError) as raised:
                planning.set_occurrence_status(occ["id"], {"status": "discarded_this"}, self.NOW)
            self.assertEqual(raised.exception.status_code, 422)
        self.run_with(run)

    def test_defer_requires_new_time_and_moves_date(self):
        def run(client):
            task, occ = self._seed_task_and_occ(client)
            with self.assertRaises(PlanningError):
                planning.set_occurrence_status(occ["id"], {"status": "deferred"}, self.NOW)
            new_time = _cst(2026, 9, 22, 10, 0)
            planning.set_occurrence_status(
                occ["id"], {"status": "deferred", "est_start": planning._iso(new_time)}, self.NOW,
            )
            deferred = client.rows["planning_occurrence"][0]
            self.assertEqual(deferred["status"], "deferred")
            self.assertEqual(deferred["for_date"], "2026-09-22")
            self.assertEqual(deferred["nominal_start"], planning._iso(new_time))
        self.run_with(run)

    def test_partial_requires_note_and_closed_records_can_return_to_progress(self):
        def run(client):
            task, occ = self._seed_task_and_occ(client)
            with self.assertRaises(PlanningError):
                planning.set_occurrence_status(occ["id"], {"status": "partial"}, self.NOW)
            planning.set_occurrence_status(
                occ["id"], {"status": "partial", "partial_note": "完成了一半"}, self.NOW,
            )
            self.assertEqual(client.rows["planning_occurrence"][0]["status"], "partial")
            # 误操作改回：回到进度中
            planning.set_occurrence_status(occ["id"], {"status": "pending"}, self.NOW)
            restored = client.rows["planning_occurrence"][0]
            self.assertEqual(restored["status"], "pending")
            self.assertIsNone(restored["closed_at"])
        self.run_with(run)

    def test_actual_times_can_be_backfilled(self):
        def run(client):
            task, occ = self._seed_task_and_occ(client)
            planning.set_occurrence_status(
                occ["id"], {
                    "status": "completed",
                    "actual_start": planning._iso(_cst(2026, 9, 20, 10, 0)),
                    "actual_end": planning._iso(_cst(2026, 9, 20, 10, 45)),
                }, self.NOW,
            )
            row = client.rows["planning_occurrence"][0]
            self.assertEqual(row["actual_minutes"], 45)
        self.run_with(run)

    def test_manual_est_edit_fixes_the_slot(self):
        def run(client):
            task, occ = self._seed_task_and_occ(client)
            planning.patch_occurrence(
                occ["id"], {"est_start": planning._iso(_cst(2026, 9, 20, 18, 0))}, self.NOW,
            )
            row = client.rows["planning_occurrence"][0]
            self.assertTrue(row["is_fixed"])
            self.assertEqual(row["for_date"], "2026-09-20")
            self.assertEqual(row["est_end"], planning._iso(_cst(2026, 9, 20, 18, 30)))
        self.run_with(run)

    def test_split_creates_once_tasks_and_closes_source(self):
        def run(client):
            task, occ = self._seed_task_and_occ(client)
            result = planning.split_occurrence(
                occ["id"],
                {"parts": [
                    {"content": "上半部分", "estimated_minutes": 15},
                    {"content": "下半部分", "estimated_minutes": 15},
                ]},
                self.NOW,
            )
            self.assertEqual(len(result["created_task_ids"]), 2)
            new_tasks = client.rows["planning_task"][-2:]
            self.assertTrue(all(row["task_type"] == "once" for row in new_tasks))
            self.assertEqual(client.rows["planning_occurrence"][0]["status"], "discarded")
        self.run_with(run)

    def test_split_supports_duration_shorthand(self):
        def run(client):
            task, occ = self._seed_task_and_occ(client)
            planning.split_occurrence(
                occ["id"],
                {"parts": [
                    {"content": "前半", "estimated_minutes": "1h30m"},
                    {"content": "后半", "estimated_minutes": "45"},
                ]},
                self.NOW,
            )
            new_tasks = client.rows["planning_task"][-2:]
            self.assertEqual(new_tasks[0]["estimated_minutes"], 90)
            self.assertEqual(new_tasks[1]["estimated_minutes"], 45)
        self.run_with(run)

    def test_discarded_repeating_instance_stops_future_refresh(self):
        """BUG-1：重复型条目「废弃」= 整个待办不再执行，次日不再生成。"""
        def run(client):
            self.create_task(client, cursor_date="2026-09-19")
            planning.generate_due(self.NOW)
            occ = client.rows["planning_occurrence"][0]
            planning.set_occurrence_status(occ["id"], {"status": "discarded"}, self.NOW)
            # 任务被停用，其余开放实例一并关闭
            self.assertFalse(client.rows["planning_task"][0]["is_active"])
            self.assertTrue(all(
                row["status"] not in planning.OPEN_STATUSES
                for row in client.rows["planning_occurrence"]
            ))
            # 次日不再生成
            next_day = self.NOW + timedelta(days=1)
            self.assertEqual(planning.generate_due(next_day)["created"], 0)
            self.assertFalse(
                any(row["for_date"] == "2026-09-21" for row in client.rows["planning_occurrence"]),
            )
        self.run_with(run)

    def test_discarded_once_instance_keeps_single_occurrence_semantics(self):
        def run(client):
            task, occ = self._seed_task_and_occ(
                client, task_type="once", target_date="2026-09-20",
            )
            planning.set_occurrence_status(occ["id"], {"status": "discarded"}, self.NOW)
            # 单次待办没有后续刷新概念，任务定义保持启用状态不变
            self.assertTrue(client.rows["planning_task"][0]["is_active"])
        self.run_with(run)

    def test_hollow_start_phase_scheduled_with_its_own_duration(self):
        """BUG-2：开始阶段按 hollow_start_minutes 排程，不吞整段预估耗时。"""
        def run(client):
            self._seed_tasks(client, [{
                "id": 1, "estimated_minutes": 45, "is_hollow": True,
                "hollow_start_minutes": 10, "hollow_wait_minutes": 30,
                "hollow_end_minutes": 5,
            }])
            self._seed(client, [
                {"task_id": 1, "phase": "start", "sort_order": 10},
                {"task_id": 1, "phase": "end", "sort_order": 11},
            ])
            planning.recompute_today(self.NOW)
            rows = {row["phase"]: row for row in client.rows["planning_occurrence"]}
            self.assertEqual(rows["start"]["est_start"], planning._iso(self.NOW))
            # 开始阶段 10m（不是 45m）
            self.assertEqual(rows["start"]["est_end"], planning._iso(self.NOW + timedelta(minutes=10)))
            # 结束锚点 = 开始预计结束 + 30m 中间等待
            self.assertEqual(rows["end"]["est_start"], planning._iso(self.NOW + timedelta(minutes=40)))
            self.assertEqual(rows["end"]["est_end"], planning._iso(self.NOW + timedelta(minutes=45)))
        self.run_with(run)

    def test_deferring_hollow_start_shifts_end_phase_along(self):
        """BUG-3：延后开始阶段时，结束阶段同日跟移，两阶段不拆散。"""
        def run(client):
            self.create_task(
                client, content="煮饭", estimated_minutes=45,
                is_hollow=True, hollow_start_content="准备食材",
                hollow_start_minutes=10, hollow_wait_minutes=30,
                hollow_wait_note="烹煮", hollow_end_content="处理完成",
                hollow_end_minutes=5, cursor_date="2026-09-19",
            )
            planning.generate_due(self.NOW)
            rows = {row["phase"]: row for row in client.rows["planning_occurrence"]}
            start_id = rows["start"]["id"]
            new_time = _cst(2026, 9, 21, 17, 0)
            planning.set_occurrence_status(
                start_id, {"status": "deferred", "est_start": planning._iso(new_time)}, self.NOW,
            )
            stored = {row["phase"]: row for row in client.rows["planning_occurrence"]}
            self.assertEqual(stored["start"]["for_date"], "2026-09-21")
            # 结束阶段跟移到同一天（原实例尚未重算、无预估起止，只平移日期）
            self.assertEqual(stored["end"]["for_date"], "2026-09-21")
        self.run_with(run)

    def test_manual_est_edit_on_hollow_start_shifts_end_phase_along(self):
        def run(client):
            self.create_task(
                client, content="煮饭", estimated_minutes=45,
                is_hollow=True, hollow_start_content="准备食材",
                hollow_start_minutes=10, hollow_wait_minutes=30,
                hollow_end_content="处理完成", hollow_end_minutes=5,
                cursor_date="2026-09-19",
            )
            planning.generate_due(self.NOW)
            planning.recompute_today(self.NOW)
            rows = {row["phase"]: row for row in client.rows["planning_occurrence"]}
            self.assertEqual(rows["end"]["est_start"], planning._iso(self.NOW + timedelta(minutes=40)))
            # 开始阶段手动改到 18:00（delta = 18:00 - 14:07），结束阶段同差平移
            planning.patch_occurrence(
                rows["start"]["id"],
                {"est_start": planning._iso(_cst(2026, 9, 20, 18, 0))},
                self.NOW,
            )
            stored = {row["phase"]: row for row in client.rows["planning_occurrence"]}
            self.assertEqual(stored["start"]["est_start"], planning._iso(_cst(2026, 9, 20, 18, 0)))
            delta = _cst(2026, 9, 20, 18, 0) - self.NOW
            self.assertEqual(
                stored["end"]["est_start"],
                planning._iso(self.NOW + timedelta(minutes=40) + delta),
            )
            self.assertEqual(stored["end"]["for_date"], "2026-09-20")
        self.run_with(run)

    def test_created_task_appears_in_today_board_immediately(self):
        """BUG-4：创建后不做任何等待，today_board 即含该实例（且带预估起止）。"""
        def run(client):
            planning.create_task(
                {"content": "背单词", "task_type": "daily", "estimated_minutes": 30},
                self.NOW,
            )
            board = planning.today_board(self.NOW)
            self.assertEqual(len(board["progress"]), 1)
            self.assertEqual(board["progress"][0]["content"], "背单词")
            self.assertEqual(board["progress"][0]["est_start"], planning._iso(self.NOW))
        self.run_with(run)

    def test_split_parts_appear_in_today_board_immediately(self):
        def run(client):
            task, occ = self._seed_task_and_occ(client)
            planning.split_occurrence(
                occ["id"],
                {"parts": [{"content": "前半", "estimated_minutes": 15},
                            {"content": "后半", "estimated_minutes": 15}]},
                self.NOW,
            )
            board = planning.today_board(self.NOW)
            contents = {item["content"] for item in board["progress"]}
            self.assertIn("前半", contents)
            self.assertIn("后半", contents)
        self.run_with(run)

    def test_type_filter_pushes_down_and_keeps_earlier_records(self):
        """BUG-5：类型筛选在 limit 之前下推，小 limit 仍能命中更早记录。"""
        def run(client):
            daily = self.create_task(client, cursor_date="2026-09-19")
            once = self.create_task(
                client, task_type="once", target_date="2026-09-17",
                content="旧单次",
            )
            planning.generate_due(self.NOW)
            planning.generate_due(self.NOW + timedelta(days=1))
            # 9 月 17 日的 once 记录比 9 月 20/21 日的 daily 记录更早
            result = planning.list_occurrences(
                task_type="once", limit=2, now=self.NOW + timedelta(days=1),
            )
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["task_type"], "once")
            self.assertEqual(result[0]["content"], "旧单次")
            # 类型没有任务时返回空列表
            self.assertEqual(planning.list_occurrences(task_type="monthly"), [])
        self.run_with(run)


class CleanupAndMaintenanceTests(_Base):
    def test_cleanup_deletes_only_expired_discards(self):
        def run(client):
            task = self.create_task(client, cursor_date="2026-09-19")
            planning.generate_due(self.NOW)
            occ = client.rows["planning_occurrence"][0]
            planning.set_occurrence_status(occ["id"], {"status": "discarded_this"}, self.NOW)
            old_discard = dict(client.rows["planning_occurrence"][0])
            old_discard.update({
                "id": 99, "status": "discarded",
                "closed_at": planning._iso(self.NOW - timedelta(hours=73)),
            })
            client.rows["planning_occurrence"].append(old_discard)
            # 已完成 / 部分完成记录永久保留
            done = dict(old_discard)
            done.update({"id": 100, "status": "completed", "closed_at": planning._iso(self.NOW - timedelta(hours=100))})
            client.rows["planning_occurrence"].append(done)
            result = planning.cleanup_discarded(self.NOW)
            self.assertEqual(result["deleted"], 1)
            statuses = {row["id"]: row["status"] for row in client.rows["planning_occurrence"]}
            self.assertEqual(statuses, {occ["id"]: "discarded_this", 100: "completed"})
        self.run_with(run)

    def test_maintenance_runs_auto_recompute_only_after_wait_window(self):
        def run(client):
            self.create_task(client, cursor_date="2026-09-19")
            planning.generate_due(self.NOW)
            planning.request_recompute("reorder", self.NOW)
            result = planning.run_maintenance(self.NOW + timedelta(minutes=10))
            self.assertTrue(result["auto_recompute"].get("skipped"))
            self.assertTrue(planning.get_recompute_state(self.NOW + timedelta(minutes=10))["pending"])
            result = planning.run_maintenance(self.NOW + timedelta(minutes=16))
            self.assertIn("updated", result["auto_recompute"])
            self.assertFalse(planning.get_recompute_state(self.NOW + timedelta(minutes=16))["pending"])
        self.run_with(run)

    def test_maintenance_generation_and_timeout_sweep(self):
        def run(client):
            self.create_task(
                client, cursor_date="2026-09-19",
                deadline_tod="12:00",
            )
            planning.generate_due(self.NOW)
            # 截止 12:00 已过 → 超时打标
            result = planning.run_maintenance(self.NOW)
            self.assertEqual(result["timeouts"]["timed_out"], 1)
            self.assertEqual(client.rows["planning_occurrence"][0]["status"], "timeout")
        self.run_with(run)

    def test_maintenance_gives_fresh_occurrences_est_times(self):
        def run(client):
            self.create_task(client, cursor_date="2026-09-19")
            planning.run_maintenance(self.NOW)
            occ = client.rows["planning_occurrence"][0]
            # 生成后立即重算：当天新实例直接拿到 14:07 起的预估起止
            self.assertEqual(occ["est_start"], planning._iso(self.NOW))
            self.assertEqual(occ["est_end"], planning._iso(self.NOW + timedelta(minutes=30)))
        self.run_with(run)

    def test_editing_deadline_syncs_limited_flag_to_open_occurrences(self):
        def run(client):
            task = self.create_task(client, cursor_date="2026-09-19")
            planning.generate_due(self.NOW)
            occ = client.rows["planning_occurrence"][0]
            self.assertFalse(occ["is_limited"])
            planning.update_task(
                task["id"], {"deadline_tod": "12:00"}, self.NOW,
            )
            self.assertTrue(client.rows["planning_occurrence"][0]["is_limited"])
            # 同步后超时判定立即生效
            result = planning.sweep_timeouts(self.NOW)
            self.assertEqual(result["timed_out"], 1)
        self.run_with(run)

    def test_schedule_edit_replaces_stale_pending_occurrences(self):
        def run(client):
            task = self.create_task(client, cursor_date="2026-09-19")
            planning.generate_due(self.NOW)
            old_id = client.rows["planning_occurrence"][0]["id"]
            planning.update_task(task["id"], {"estimated_minutes": 60}, self.NOW)
            # 未固定的 pending 实例被删除后即时按新规则重建（BUG-4）
            rows = client.rows["planning_occurrence"]
            self.assertEqual(len(rows), 1)
            self.assertNotEqual(rows[0]["id"], old_id)
            # 再次生成不重复
            planning.generate_due(self.NOW + timedelta(minutes=1))
            self.assertEqual(len(client.rows["planning_occurrence"]), 1)
        self.run_with(run)

    def test_schedule_edit_keeps_fixed_occurrence_without_duplicates(self):
        def run(client):
            task = self.create_task(client, cursor_date="2026-09-19")
            planning.generate_due(self.NOW)
            occ = client.rows["planning_occurrence"][0]
            planning.patch_occurrence(
                occ["id"], {"est_start": planning._iso(_cst(2026, 9, 20, 18, 0))}, self.NOW,
            )
            planning.update_task(task["id"], {"estimated_minutes": 60}, self.NOW)
            # 手动固定的实例保留；当天槽位被其占据，不会重复生成
            planning.generate_due(self.NOW + timedelta(minutes=1))
            rows = client.rows["planning_occurrence"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], occ["id"])
            self.assertTrue(rows[0]["is_fixed"])
            self.assertEqual(rows[0]["est_start"], planning._iso(_cst(2026, 9, 20, 18, 0)))
        self.run_with(run)


class ScheduleLabelTests(_Base):
    """BUG-12：排列状态标签展示串用「落后」，避开与「延后」完成状态撞名。"""

    def _serialize(self, occ_row, task_id):
        task = next(row for row in self.client.rows["planning_task"] if row["id"] == task_id)
        return planning.serialize_occurrence(occ_row, task, self.NOW)

    def test_deferred_and_overdue_pending_label_as_behind(self):
        def run(client):
            self._seed_tasks(client, [{"id": 1}])
            self._seed(client, [
                {"task_id": 1, "id": 1, "status": "deferred",
                 "est_start": planning._iso(_cst(2026, 9, 20, 9, 0))},
                {"task_id": 1, "id": 2, "status": "pending",
                 "est_start": planning._iso(_cst(2026, 9, 20, 9, 0))},
                {"task_id": 1, "id": 3, "status": "pending",
                 "est_start": planning._iso(_cst(2026, 9, 20, 15, 0))},
            ])
            rows = client.rows["planning_occurrence"]
            self.assertEqual(self._serialize(rows[0], 1)["schedule_label"], "落后")
            self.assertEqual(self._serialize(rows[1], 1)["schedule_label"], "落后")
            # 未来时间 + 未延后 → 正常
            self.assertEqual(self._serialize(rows[2], 1)["schedule_label"], "正常")
            # 完成状态展示名同步（前端 STATUS_META 一致）
            self.assertNotIn("延后", planning.schedule_label(
                rows[0],
                next(row for row in client.rows["planning_task"] if row["id"] == 1),
                self.NOW,
            ))
        self.run_with(run)

    def test_timeout_label_unchanged(self):
        def run(client):
            self._seed_tasks(client, [{"id": 1}])
            self._seed(client, [{"task_id": 1, "status": "timeout"}])
            self.assertEqual(
                self._serialize(client.rows["planning_occurrence"][0], 1)["schedule_label"],
                "超时",
            )
        self.run_with(run)


if __name__ == "__main__":
    unittest.main()
