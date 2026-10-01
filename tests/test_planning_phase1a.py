"""Phase 1A invariants, independent of legacy generation and scheduler tests."""

import unittest
import contextlib
import re
import sqlite3
from importlib.util import find_spec
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway import planning
from gateway.config import cfg
from gateway.planning_api import planning_api_routes
from gateway.planning_domain import (
    BoundaryTransition,
    EstimatedTimeOwnership,
    OccurrenceIdentity,
    PlanningCycle,
    RefreshDefinition,
    calendar_round_key,
    cycle_start_boundary,
    fixed_round_key,
    once_round_key,
    parse_refresh_boundary,
    planning_cycle_at,
    round_phase_group,
    timed_round_key,
    validate_round_key,
    validate_task_refresh_mode,
)


UTC = timezone.utc
BEIJING = timezone(timedelta(hours=8))
MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase/migrations/20260924010000_planning_phase1a_domain_identity.sql"
)


class PlanningDomainTests(unittest.TestCase):
    def test_boundary_is_beijing_cycle_not_midnight_or_host_timezone(self):
        before = PlanningCycle.at(datetime(2026, 9, 23, 21, 59, tzinfo=UTC))
        at = PlanningCycle.at(datetime(2026, 9, 23, 22, 0, tzinfo=UTC))
        self.assertEqual(before.key, date(2026, 9, 23))
        self.assertEqual(at.key, date(2026, 9, 24))
        self.assertEqual(at.end - at.start, timedelta(days=1))
        shifted = PlanningCycle.at(
            datetime(2026, 9, 23, 22, 0, tzinfo=UTC), time(9, 30)
        )
        self.assertEqual(shifted.key, date(2026, 9, 23))

    def test_boundary_requires_exact_local_minute_and_aware_instant(self):
        for value in ("6:00", "24:00", "12:60", "06:00:00", "bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_refresh_boundary(value)
        with self.assertRaises(ValueError):
            PlanningCycle.at(datetime(2026, 9, 24, 6))

    def test_round_display_and_estimate_are_independent(self):
        original = OccurrenceIdentity(
            task_id=7,
            round_key=calendar_round_key(date(2026, 9, 24)),
            schedule_date=date(2026, 9, 24),
            display_cycle_date=date(2026, 9, 24),
        )
        carried = original.carried_to(date(2026, 9, 25))
        manual = original.carried_to(date(2026, 9, 25), manual=True)
        self.assertEqual(carried.round_key, original.round_key)
        self.assertEqual(carried.schedule_date, original.schedule_date)
        self.assertEqual(carried.display_reason, "carryover")
        self.assertEqual(manual.display_reason, "manual_defer")
        self.assertEqual(once_round_key(), "once")
        self.assertNotEqual(timed_round_key("handled", date(2026, 9, 24), "one"),
                            timed_round_key("handled", date(2026, 9, 24), "two"))
        with self.assertRaises(ValueError):
            timed_round_key("handled", date(2026, 9, 24), "")
        pair = round_phase_group(7, "cycle:2026-09-24")
        start = OccurrenceIdentity(7, "cycle:2026-09-24", date(2026, 9, 24),
                                   date(2026, 9, 24), phase="start", phase_group=pair)
        end = OccurrenceIdentity(7, "cycle:2026-09-24", date(2026, 9, 24),
                                 date(2026, 9, 24), phase="end", phase_group=pair)
        self.assertEqual(start.phase_group, end.phase_group)
        with self.assertRaises(ValueError):
            OccurrenceIdentity(7, "cycle:2026-09-24", date(2026, 9, 24),
                               date(2026, 9, 24), phase="start")
        with self.assertRaises(ValueError):
            OccurrenceIdentity(7, "cycle:2026-09-25", date(2026, 9, 24),
                               date(2026, 9, 24))
        with self.assertRaises(ValueError):
            OccurrenceIdentity(7, "cycle:2026-09-24", date(2026, 9, 24),
                               date(2026, 9, 25), display_reason="initial")
        with self.assertRaises(ValueError):
            OccurrenceIdentity(7, "cycle:2026-09-24", date(2026, 9, 24),
                               date(2026, 9, 24), phase="end",
                               phase_group=round_phase_group(8, "cycle:2026-09-24"))

    def test_display_can_never_precede_schedule_date(self):
        # 已生成实例冻结：展示周期绝不早于原始轮次周期，任何 boundary 变更
        # 都不再提供「提前展示」的合法路径。
        with self.assertRaises(ValueError):
            OccurrenceIdentity(7, "cycle:2026-09-24", date(2026, 9, 24),
                               date(2026, 9, 23), "carryover")
        with self.assertRaises(ValueError):
            OccurrenceIdentity(7, "cycle:2026-09-24", date(2026, 9, 24),
                               date(2026, 9, 23), "manual_defer")

    def test_fixed_round_key_is_due_derived_and_boundary_independent(self):
        due = datetime(2026, 9, 28, 7, tzinfo=BEIJING)
        key = fixed_round_key(due)
        self.assertEqual(key, fixed_round_key(datetime(2026, 9, 28, 7, tzinfo=BEIJING)))
        self.assertTrue(key.startswith("fixed:2026-09-28:"))
        self.assertNotEqual(key, fixed_round_key(due + timedelta(days=3)))
        # fixed 轮次身份由到期事件派生：不要求日期段等于创建周期。
        validate_round_key(key, date(2026, 9, 27))
        validate_round_key(key, date(2026, 9, 28))
        # handled/early 轮次仍必须与其原始周期一致。
        validate_round_key(timed_round_key("handled", date(2026, 9, 28), "x"), date(2026, 9, 28))
        with self.assertRaises(ValueError):
            validate_round_key(timed_round_key("handled", date(2026, 9, 28), "x"), date(2026, 9, 27))
        with self.assertRaises(ValueError):
            validate_round_key(timed_round_key("early", date(2026, 9, 28), "x"), date(2026, 9, 27))

    def test_fixed_round_key_canonicalizes_timezone_representation(self):
        # LOW #11：同一到期瞬间在不同时区表示 / 精度拼写下得到同一身份。
        beijing = datetime(2026, 9, 28, 7, 30, tzinfo=BEIJING)
        utc = datetime(2026, 9, 27, 23, 30, tzinfo=timezone.utc)
        canonical = fixed_round_key(beijing)
        self.assertEqual(canonical, fixed_round_key(utc))
        self.assertEqual(canonical, fixed_round_key(
            datetime(2026, 9, 28, 7, 30, 0, 0, tzinfo=BEIJING)))
        self.assertEqual(canonical, fixed_round_key(
            datetime(2026, 9, 27, 23, 30, 0, 999999, tzinfo=UTC)))
        # 日期段是北京日历日（UTC 表示仍是 9/27，身份段必须归一为 9/28）。
        self.assertTrue(canonical.startswith("fixed:2026-09-28:"))
        with self.assertRaises(ValueError):
            fixed_round_key(datetime(2026, 9, 28, 7, 30))  # naive 不接受

    def test_boundary_transition_keeps_current_cycle_until_next_boundary(self):
        # 06:00 → 08:00，9/25 07:00 修改：当前周期（9/25）继续按 06:00 走完，
        # 直到 9/26 08:00 新边界第一次出现；周期键不会同日碰撞。
        transition = BoundaryTransition.plan_first(
            time(6), datetime(2026, 9, 25, 7, tzinfo=BEIJING), time(8),
        )
        self.assertEqual(transition.spanning_key, date(2026, 9, 25))
        self.assertEqual(transition.effective_at, datetime(2026, 9, 26, 8, tzinfo=BEIJING))
        in_window = planning_cycle_at(datetime(2026, 9, 25, 20, tzinfo=BEIJING), time(8), transition)
        late_window = planning_cycle_at(datetime(2026, 9, 26, 7, tzinfo=BEIJING), time(8), transition)
        after = planning_cycle_at(datetime(2026, 9, 26, 8, 30, tzinfo=BEIJING), time(8), transition)
        self.assertEqual((in_window.key, in_window.end), (date(2026, 9, 25), transition.effective_at))
        self.assertEqual(late_window.key, date(2026, 9, 25))
        self.assertEqual(after.key, date(2026, 9, 26))

    def test_boundary_transition_reverse_direction_and_absorbed_cycle(self):
        # 06:00 → 04:00，9/25 07:00 修改：生效点 9/26 04:00。
        forward = BoundaryTransition.plan_first(
            time(6), datetime(2026, 9, 25, 7, tzinfo=BEIJING), time(4),
        )
        self.assertEqual(forward.effective_at, datetime(2026, 9, 26, 4, tzinfo=BEIJING))
        self.assertEqual(planning_cycle_at(
            datetime(2026, 9, 26, 3, tzinfo=BEIJING), time(4), forward).key, date(2026, 9, 25))
        self.assertEqual(planning_cycle_at(
            datetime(2026, 9, 26, 5, tzinfo=BEIJING), time(4), forward).key, date(2026, 9, 26))
        # 08:00 → 06:00，9/25 07:00 修改：当前周期是 9/24（07:00 早于 08:00），
        # 9/25 的 06:00 已过，生效点顺延到 9/26 06:00，9/25 不再作为周期键存在。
        backward = BoundaryTransition.plan_first(
            time(8), datetime(2026, 9, 25, 7, tzinfo=BEIJING), time(6),
        )
        self.assertEqual(backward.spanning_key, date(2026, 9, 24))
        self.assertEqual(backward.effective_at, datetime(2026, 9, 26, 6, tzinfo=BEIJING))
        self.assertEqual(planning_cycle_at(
            datetime(2026, 9, 25, 12, tzinfo=BEIJING), time(6), backward).key, date(2026, 9, 24))
        # 9/25 已被 9/24 的跨越周期吸收，不再是周期键；枚举时必须跳过它。
        self.assertTrue(
            backward.spanning_key < date(2026, 9, 25) < backward.effective_at.date())
        self.assertEqual(backward.absorbed_cycle_keys(), [date(2026, 9, 25)])
        self.assertEqual(cycle_start_boundary(date(2026, 9, 26), time(6), backward), time(6))

    def test_boundary_transition_second_change_keeps_spanning_cycle_frozen(self):
        # HIGH #2：过渡等待期间再次修改边界，当前已成立的周期身份保持不变。
        first = BoundaryTransition.plan_first(
            time(6), datetime(2026, 9, 25, 7, tzinfo=BEIJING), time(8),
        )
        # B → C（9/26 07:00，仍在过渡窗口内）：跨越周期仍是 9/25，而不是
        # 用普通周期算术算出来的 9/26。
        second = BoundaryTransition.plan(
            first.spanning_key, first.spanning_boundary,
            datetime(2026, 9, 26, 7, tzinfo=BEIJING), time(5),
        )
        self.assertEqual(second.spanning_key, date(2026, 9, 25))
        self.assertEqual(second.spanning_boundary, time(6))
        self.assertEqual(second.effective_at, datetime(2026, 9, 27, 5, tzinfo=BEIJING))
        self.assertEqual(planning_cycle_at(
            datetime(2026, 9, 26, 7, 30, tzinfo=BEIJING), time(5), second).key, date(2026, 9, 25))
        self.assertEqual(planning_cycle_at(
            datetime(2026, 9, 27, 5, 30, tzinfo=BEIJING), time(5), second).key, date(2026, 9, 27))
        # A → B → A：当前周期只延长到它的下一个自然边界点（9/26 06:00）。
        back = BoundaryTransition.plan(
            first.spanning_key, first.spanning_boundary,
            datetime(2026, 9, 25, 12, tzinfo=BEIJING), time(6),
        )
        self.assertEqual(back.effective_at, datetime(2026, 9, 26, 6, tzinfo=BEIJING))
        self.assertEqual(planning_cycle_at(
            datetime(2026, 9, 25, 20, tzinfo=BEIJING), time(6), back).key, date(2026, 9, 25))
        self.assertEqual(planning_cycle_at(
            datetime(2026, 9, 26, 6, 30, tzinfo=BEIJING), time(6), back).key, date(2026, 9, 26))
        # 连续三次修改，跨越周期始终是同一个。
        third = BoundaryTransition.plan(
            second.spanning_key, second.spanning_boundary,
            datetime(2026, 9, 26, 20, tzinfo=BEIJING), time(9),
        )
        self.assertEqual(third.spanning_key, date(2026, 9, 25))
        self.assertEqual(third.effective_at, datetime(2026, 9, 27, 9, tzinfo=BEIJING))
        # 吸收周期登记随每次修改累积（此处 9/26 被 9/25 的延长周期吸收）。
        self.assertIn(date(2026, 9, 26), third.absorbed_cycle_keys())

    def test_automatic_persisted_time_remains_system_owned(self):
        anchor = datetime(2026, 9, 24, 7, tzinfo=UTC)
        self.assertEqual(
            EstimatedTimeOwnership("automatic", estimated_start=anchor,
                                   estimated_end=anchor + timedelta(hours=1)).fixed_source, None
        )
        EstimatedTimeOwnership("rule", "rule", True, anchor, anchor + timedelta(hours=1))
        EstimatedTimeOwnership("manual", "manual", False, anchor, anchor + timedelta(hours=1))
        with self.assertRaises(ValueError):
            EstimatedTimeOwnership("automatic", "manual", True, anchor)
        with self.assertRaises(ValueError):
            EstimatedTimeOwnership("manual", None, True)
        with self.assertRaises(ValueError):
            EstimatedTimeOwnership("manual", "manual", False)
        with self.assertRaises(ValueError):
            EstimatedTimeOwnership("unassigned", estimated_start=anchor,
                                   estimated_end=anchor + timedelta(hours=1))
        with self.assertRaises(ValueError):
            EstimatedTimeOwnership("automatic", estimated_start=anchor)
        with self.assertRaises(ValueError):
            EstimatedTimeOwnership("rule", "rule", True)

    def test_refresh_baseline_is_not_execution_time(self):
        anchor = datetime(2026, 9, 1, 6, tzinfo=UTC)
        handled = datetime(2026, 9, 24, 7, tzinfo=UTC)
        fixed = RefreshDefinition("fixed_interval", anchor_at=anchor)
        after = RefreshDefinition("after_completion", last_handled_at=handled)
        self.assertEqual(fixed.anchor_at, anchor)
        self.assertIsNone(fixed.last_handled_at)
        self.assertEqual(after.last_handled_at, handled)
        with self.assertRaises(ValueError):
            RefreshDefinition("fixed_interval")
        with self.assertRaises(ValueError):
            RefreshDefinition("after_completion", anchor_at=anchor)
        with self.assertRaises(ValueError):
            RefreshDefinition("fixed_interval", anchor_at=anchor, last_handled_at=handled)
        validate_task_refresh_mode("interval", "after_completion")
        with self.assertRaises(ValueError):
            validate_task_refresh_mode("once", "daily")


class PlanningBoundaryApiTests(unittest.TestCase):
    """批次 7 起 boundary 保存需要读取启用中任务做全量校验：本类统一
    patch 一个空任务库的 fake client（任务级校验语义由批次 7 定向测试
    test_planning_cycle_boundary.py 覆盖）。"""

    def _settings(self, stored):
        """返回 (contextmanager, load, save)：app_settings 存储与空任务库。"""

        def load(key):
            return stored.get(key)

        def save(key, value):
            stored[key] = value
            return True

        @contextlib.contextmanager
        def ctx():
            tokens = [
                mock.patch.object(planning, "get_client", return_value=_Database()),
                mock.patch.object(planning.db, "load_app_setting", load),
                mock.patch.object(planning.db, "save_app_setting", save),
            ]
            for token in tokens:
                token.start()
            try:
                yield
            finally:
                for token in reversed(tokens):
                    token.stop()

        return ctx(), load, save

    def test_boundary_endpoint_requires_token_and_persists_changed_cycle(self):
        stored = {}
        settings, _load, _save = self._settings(stored)
        with mock.patch.object(cfg, "GATEWAY_TOKEN", "phase1a-test"), settings:
            client = TestClient(Starlette(routes=list(planning_api_routes)))
            self.assertEqual(client.get("/admin/api/planning/cycle").status_code, 401)
            headers = {"Authorization": "Bearer phase1a-test"}
            response = client.patch(
                "/admin/api/planning/cycle",
                json={"refresh_boundary_time": "09:30"},
                headers=headers,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["refresh_boundary_time"], "09:30")
            self.assertIn("pending_boundary", response.json())
            # 单 key 原子状态：配置、过渡记录、吸收周期登记同写一行。
            state = stored["planning.refresh_boundary_state"]
            self.assertEqual(state["boundary"], "09:30")
            self.assertEqual(state["transition"]["spanning_boundary"], "06:00")
            self.assertIsInstance(state["absorbed"], list)

    def test_read_and_write_configured_boundary_keeps_current_cycle(self):
        # 北京时间 9/24 06:00 整把 06:00 改为 09:30：当前周期（9/24）保持原
        # 边界继续，过渡记录等待 9/25 09:30 生效，而不是立即重排当天。
        instant = datetime(2026, 9, 23, 22, 0, tzinfo=UTC)
        stored = {}
        settings, _load, _save = self._settings(stored)
        with settings:
            self.assertEqual(planning.get_cycle_settings(instant)["cycle_key"], "2026-09-24")
            changed = planning.set_cycle_settings({"refresh_boundary_time": "09:30"}, instant)
            self.assertEqual(changed["cycle_key"], "2026-09-24")
            self.assertEqual(changed["pending_boundary"]["previous_time"], "06:00")
            self.assertEqual(changed["pending_boundary"]["effective_at"],
                             datetime(2026, 9, 25, 9, 30, tzinfo=BEIJING).isoformat())
            self.assertEqual(stored["planning.refresh_boundary_state"]["boundary"], "09:30")
        with self.assertRaises(planning.PlanningError):
            planning.set_cycle_settings({"refresh_boundary_time": "25:00"}, instant)

    def test_boundary_change_back_to_same_boundary_extends_current_cycle(self):
        # A → B → A（HIGH #2）：当前周期冻结，只延长到它的下一个自然边界点；
        # 不再出现「改回即清除过渡 → 当前周期被立即重排」的路径。
        change_at = datetime(2026, 9, 25, 7, tzinfo=BEIJING)
        stored = {
            planning.PLANNING_BOUNDARY_STATE_KEY: {
                "boundary": "08:00",
                "transition": {
                    "spanning_key": "2026-09-25",
                    "spanning_boundary": "06:00",
                    "change_at": change_at.isoformat(),
                },
                "absorbed": [],
            },
        }

        def load(key):
            return stored.get(key)

        def save(key, value):
            stored[key] = value
            return True

        instant = datetime(2026, 9, 25, 12, tzinfo=BEIJING)
        settings, _load, _save = self._settings(stored)
        with settings:
            pending = planning.get_cycle_settings(instant)["pending_boundary"]
            self.assertEqual(pending["previous_time"], "06:00")
            changed = planning.set_cycle_settings({"refresh_boundary_time": "06:00"}, instant)
            # 当前周期 9/25 冻结，直到 9/26 06:00（A 的下一个自然边界点）。
            self.assertEqual(changed["cycle_key"], "2026-09-25")
            self.assertEqual(changed["pending_boundary"]["effective_at"],
                             datetime(2026, 9, 26, 6, tzinfo=BEIJING).isoformat())
            state = stored[planning.PLANNING_BOUNDARY_STATE_KEY]
            self.assertEqual(state["boundary"], "06:00")
            self.assertEqual(state["transition"]["spanning_key"], "2026-09-25")
            self.assertEqual(state["transition"]["spanning_boundary"], "06:00")
            # 被吸收的 9/25 之后、生效前的周期键进入登记（此处无中间日）。
            self.assertEqual(state["absorbed"], [])

    def test_boundary_state_write_failure_leaves_no_partial_commit(self):
        # HIGH #3：边界修改是单事务原子保存（批次 7 经 RPC）；状态写入失败
        # 时整体保持修改前状态（含关联任务窗口模板零更新）。
        original = {
            planning.PLANNING_BOUNDARY_STATE_KEY: {
                "boundary": "06:00", "transition": None, "absorbed": [],
            },
        }
        settings, _load, _save = self._settings(original)
        # 真实 save_app_setting 失败时返回 falsy，不抛异常
        failing_save = mock.patch.object(
            planning.db, "save_app_setting", lambda key, value: False)
        instant = datetime(2026, 9, 25, 7, tzinfo=BEIJING)
        with settings, failing_save:
            with self.assertRaises(planning.PlanningError) as error:
                planning.set_cycle_settings({"refresh_boundary_time": "08:00"}, instant)
            self.assertEqual(error.exception.status_code, 503)
        # 唯一的状态键未被触碰：不存在「新配置已写入、过渡记录丢失」的半更新。
        self.assertEqual(original[planning.PLANNING_BOUNDARY_STATE_KEY]["boundary"], "06:00")
        self.assertIsNone(original[planning.PLANNING_BOUNDARY_STATE_KEY]["transition"])

    def test_transition_absorbed_cycles_are_registered_and_persist(self):
        # 08:00 → 06:00 吸收 9/25；过渡结束后登记仍然生效，不得补生成该周期。
        stored = {}
        settings, _load, _save = self._settings(stored)
        with settings:
            planning.set_cycle_settings(
                {"refresh_boundary_time": "06:00"},
                datetime(2026, 9, 23, 7, tzinfo=BEIJING),
            )
            planning.set_cycle_settings(
                {"refresh_boundary_time": "08:00"},
                datetime(2026, 9, 23, 9, tzinfo=BEIJING),
            )
            state = stored[planning.PLANNING_BOUNDARY_STATE_KEY]
            self.assertIsInstance(state["absorbed"], list)
            # 后续再修改边界，吸收登记只增不减。
            planning.set_cycle_settings(
                {"refresh_boundary_time": "05:00"},
                datetime(2026, 9, 23, 12, tzinfo=BEIJING),
            )
            later = stored[planning.PLANNING_BOUNDARY_STATE_KEY]
            self.assertTrue(set(state["absorbed"]) <= set(later["absorbed"]))

    def test_auto_recompute_settings_round_trip(self):
        stored = {}

        def load(key):
            return stored.get(key)

        def save(key, value):
            stored[key] = value
            return True

        instant = datetime(2026, 9, 24, 7, tzinfo=BEIJING)
        with mock.patch.object(planning.db, "load_app_setting", load), \
             mock.patch.object(planning.db, "save_app_setting", save):
            settings = planning.get_cycle_settings(instant)
            self.assertTrue(settings["auto_recompute_enabled"])
            self.assertEqual(settings["auto_recompute_wait_minutes"], 30)
            changed = planning.set_cycle_settings({"auto_recompute_wait_minutes": 45}, instant)
            self.assertEqual(changed["auto_recompute_wait_minutes"], 45)
            changed = planning.set_cycle_settings({"auto_recompute_enabled": False}, instant)
            self.assertFalse(changed["auto_recompute_enabled"])
        with self.assertRaises(planning.PlanningError):
            planning.set_cycle_settings({"auto_recompute_wait_minutes": 0}, instant)

    def test_legacy_rows_are_not_given_guessed_identity(self):
        row = {"id": 1, "task_id": 2, "for_date": "2026-09-24", "phase": None,
               "status": "pending", "est_start": "2026-09-25T15:00:00+08:00"}
        task = {"id": 2, "content": "旧任务", "task_type": "once",
                "time_mode": "duration", "is_active": True}
        result = planning.serialize_occurrence(
            row, task, datetime(2026, 9, 24, 8, tzinfo=UTC)
        )
        self.assertIsNone(result["round_key"])
        self.assertIsNone(result["display_cycle_date"])
        self.assertIsNone(result["estimated_time_source"])


class PlanningDomainMigrationTests(unittest.TestCase):
    def test_additive_migration_requires_new_round_identity_and_null_phase_uniqueness(self):
        sql = MIGRATION.read_text(encoding="utf-8").lower()
        self.assertIn("round_key is null or", sql)
        self.assertIn("coalesce(phase, '')", sql)
        self.assertIn("where round_key is not null", sql)
        self.assertIn("drop index if exists public.planning_occurrence_schedule_slot_uq", sql)
        self.assertIn("create constraint trigger planning_occurrence_identity_guard", sql)
        self.assertIn("hollow phases disagree on round or display identity", sql)
        self.assertIn("and for_date = schedule_date", sql)
        self.assertIn(") is true);", sql)
        self.assertIn("planning.refresh_boundary_state", sql)
        self.assertNotIn("planning.refresh_boundary_time", sql)
        self.assertIn("and display_cycle_date >= schedule_date", sql)
        self.assertIn("closed occurrences cannot re-enter the open lifecycle", sql)
        self.assertIn("confirmed handling instant is immutable once written", sql)
        self.assertIn("(closed_at is not null))", sql)
        self.assertIn("partial_at", sql)
        self.assertNotIn("update public.planning_occurrence", sql)
        self.assertNotIn("delete from public.planning_occurrence", sql)

    def test_migration_no_longer_carries_boundary_evidence_machinery(self):
        # 需求精简：反向展示与历史证据机制整体退出，当前规则不得重验已生成实例。
        sql = MIGRATION.read_text(encoding="utf-8")
        lowered = sql.lower()
        for absent in (
            "boundary_shift_at", "boundary_shift_from_time", "boundary_shift_to_time",
            "boundary_shift_refresh_mode", "boundary_shift", "boundaryshiftevidence",
            "planning_cycle_key_at", "refresh_round_boundary_time",
            "earlier display", "valid_early_display",
        ):
            self.assertNotIn(absent, lowered)
        self.assertIn("fixed|handled|early", sql)
        self.assertIn("round_key like 'fixed:%'", sql)

    @unittest.skipUnless(find_spec("pglast"), "PostgreSQL parser is optional locally")
    def test_identity_check_rejects_sql_unknown_instead_of_accepting_it(self):
        from pglast import ast, parse_sql

        statements = parse_sql(MIGRATION.read_text(encoding="utf-8"))
        identity = next(node.stmt for node in statements
                        if "planning_occurrence_phase1a_identity_check" in str(node.stmt))
        self.assertIsInstance(identity.cmds[0].def_.raw_expr, ast.BooleanTest)
        self.assertEqual(identity.cmds[0].def_.raw_expr.booltesttype.name, "IS_TRUE")

    def test_python_and_sql_round_key_predicates_agree_on_same_cases(self):
        # 简化后的共享不变量：round_key 与 schedule_date 的绑定规则在 CHECK 与
        # Python 校验中必须给出相同判定（含 SQL UNKNOWN 不得绕过）。
        sql = MIGRATION.read_text(encoding="utf-8")
        start = sql.index("length(btrim(round_key)) > 0")
        end = sql.index("and schedule_date is not null", start)
        predicate = sql[start:end].rstrip()
        predicate = predicate.replace("schedule_date::text", "sched").replace("round_key", "rk")
        predicate = predicate.replace(" ~ ", " REGEXP ")

        con = sqlite3.connect(":memory:")
        con.create_function("REGEXP", 2, lambda pattern, value: bool(value and re.search(pattern, value)))
        con.create_function("btrim", 1, lambda value: value.strip() if isinstance(value, str) else value)
        con.create_function("split_part", 3, lambda value, sep, index: (
            value.split(sep)[index - 1] if isinstance(value, str) and 1 <= index <= len(value.split(sep)) else ""))
        query = f"select ({predicate}) from (select ? as rk, ? as sched)"

        def sql_check(round_key, schedule):
            return bool(con.execute(query, (round_key, schedule.isoformat())).fetchone()[0])

        def python_check(round_key, schedule):
            try:
                validate_round_key(round_key, schedule)
            except ValueError:
                return False
            return True

        digest = "a" * 32
        cases = (
            ("once", date(2026, 9, 24), True),
            ("cycle:2026-09-24", date(2026, 9, 24), True),
            ("cycle:2026-09-25", date(2026, 9, 24), False),
            ("", date(2026, 9, 24), False),
            ("cycle:2026-09-2a", date(2026, 9, 24), False),
            (f"fixed:2026-09-28:{digest}", date(2026, 9, 27), True),
            (f"fixed:2026-09-28:{digest}", date(2026, 9, 28), True),
            (f"handled:2026-09-24:{digest}", date(2026, 9, 24), True),
            (f"handled:2026-09-25:{digest}", date(2026, 9, 24), False),
            (f"early:2026-09-24:{digest}", date(2026, 9, 24), True),
            (f"early:2026-09-25:{digest}", date(2026, 9, 24), False),
            (f"handled:2026-09-24:{digest.upper()}", date(2026, 9, 24), False),
            (f"early:2026-09-24:{'g' * 32}", date(2026, 9, 24), False),
            (f"early:2026-9-24:{digest}", date(2026, 9, 24), False),
        )
        for round_key, schedule, expected in cases:
            with self.subTest(round_key=round_key, schedule=schedule.isoformat()):
                self.assertEqual(python_check(round_key, schedule), expected)
                self.assertEqual(sql_check(round_key, schedule), expected)

class _Query:
    def __init__(self, db, name):
        self.db, self.name, self.filters = db, name, []
        self.action, self.payload, self.max_rows = "select", None, None

    def select(self, *_):
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def in_(self, key, values):
        self.filters.append((key, set(values)))
        return self

    def is_(self, key, value):
        self.filters.append((key, ("is", value)))
        return self

    def gte(self, key, value):
        self.filters.append((key, ("gte", value)))
        return self

    def lt(self, key, value):
        self.filters.append((key, ("lt", value)))
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, value):
        self.max_rows = value
        return self

    def insert(self, value):
        self.action, self.payload = "insert", value
        return self

    def update(self, value):
        self.action, self.payload = "update", value
        return self

    def delete(self):
        self.action = "delete"
        return self

    def upsert(self, value, ignore_duplicates=False, on_conflict=None):
        self.action = "upsert"
        # 批次 6 一轮 Review BLOCKER 3：多行 upsert 模拟单条
        # INSERT .. ON CONFLICT (id) DO UPDATE 的语句级原子语义——
        # 要么全部行应用、要么（异常注入时）全部不应用。
        self.payload = [dict(item) for item in value] if isinstance(value, list) else dict(value)
        self.ignore_duplicates = ignore_duplicates
        self.on_conflict = on_conflict
        return self

    def execute(self):
        rows = self.db.rows[self.name]
        if self.action == "upsert":
            if isinstance(self.payload, list):
                self.db.writes.append((self.name, {"__upsert_rows__": len(self.payload)}))
                updated = []
                by_id = {row.get("id"): row for row in rows}
                for item in self.payload:
                    target = by_id.get(item.get("id"))
                    if target is not None:
                        target.update(item)
                        updated.append(dict(target))
                    elif self.ignore_duplicates:
                        continue
                    else:
                        new_row = dict(item)
                        new_row.setdefault("id", self.db.next_id(self.name))
                        rows.append(new_row)
                        updated.append(dict(new_row))
                return SimpleNamespace(data=updated)
            key = self.payload.get("id")
            matched = [row for row in rows if row.get("id") == key]
            if matched:
                for row in matched:
                    row.update(self.payload)
                return SimpleNamespace(data=[dict(row) for row in matched])
            row = dict(self.payload)
            row.setdefault("id", self.db.next_id(self.name))
            rows.append(row)
            return SimpleNamespace(data=[dict(row)])
        if self.action == "insert":
            inserted = []
            for item in self.payload if isinstance(self.payload, list) else [self.payload]:
                if self.name == "planning_occurrence" and any(
                    old.get("task_id") == item.get("task_id")
                    and old.get("round_key") == item.get("round_key")
                    and old.get("phase") == item.get("phase") for old in rows
                ):
                    raise RuntimeError("planning_occurrence_round_phase_uq")
                row = {**item, "id": self.db.next_id(self.name)}
                rows.append(row)
                inserted.append(dict(row))
            return SimpleNamespace(data=inserted)
        def matches(row):
            for key, value in self.filters:
                actual = row.get(key)
                if isinstance(value, set):
                    match = actual in value
                elif isinstance(value, tuple) and value[0] == "gte":
                    match = actual is not None and actual >= value[1]
                elif isinstance(value, tuple) and value[0] == "lt":
                    match = actual is not None and actual < value[1]
                elif isinstance(value, tuple) and value[0] == "is":
                    match = (actual is None) == (value[1] is None)
                else:
                    match = actual == value
                if not match:
                    return False
            return True

        matched = [row for row in rows if matches(row)]
        if self.max_rows is not None:
            matched = matched[:self.max_rows]
        if self.action == "update":
            self.db.writes.append((self.name, dict(self.payload)))
            for row in matched:
                row.update(self.payload)
        if self.action == "delete":
            for row in matched:
                rows.remove(row)
        return SimpleNamespace(data=[dict(row) for row in matched])


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
    "actual_start", "actual_end", "actual_minutes", "updated_at"})
ROUND_EXPECTED_TASK_FIELDS = frozenset({
    "task_type", "refresh_mode", "refresh_enabled", "is_active",
    "request_state", "content", "time_mode", "estimated_minutes",
    "is_hollow", "hollow_start_minutes", "hollow_wait_minutes",
    "hollow_end_minutes", "hollow_start_content", "hollow_end_content",
    "interval_days", "weekdays", "month_days", "target_date",
    "created_at", "refresh_anchor_at",
    "last_handled_at", "refresh_next_due_at", "window_start_tod",
    "window_end_tod",
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


def emulate_planning_discard_task(task_rows, occ_rows, params):
    """废弃整个任务：锁内关闭全部开放 occurrence + 停用任务（单事务语义）。

    与 20261001010000 真库 SQL 同语义（不同步替身会掩盖新分支）：
    * 可选 pending 目标行事实补齐（actual 字段白名单，原守卫原样）；
    * 执行中（in_progress 且无 closed_at / handled_at）行在批量关闭前补齐
      起止与耗时——目标行缺失的起止以补丁值补齐（已有事实让位，补入的
      开始时间一并落库），其余行以 p_now 收口；耗时一律按最终采用的起止
      重算（应用层 _minutes_between 契约），不接受 patch 携带的耗时；
      倒置区间整体拒绝。真库锁扫描覆盖全部开放行并在锁内复核最新版本
      （扫描后才开始 / 关闭的行各自归位），fake 单线程按最终状态等价仿真。
    """
    task = next((r for r in task_rows if r.get("id") == params.get("p_task_id")), None)
    if task is None:
        raise RuntimeError("planning_discard_task: task not found")
    if not task.get("is_active"):
        raise RuntimeError("planning_discard_task: task already inactive (concurrent change)")
    now = params.get("p_now")
    target_patch = params.get("p_target_patch") or {}
    if set(target_patch) - ROUND_DISCARD_TARGET_FIELDS:
        raise RuntimeError(
            "planning_discard_task: target patch contains unsupported field")
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
    for row in occ_rows:
        if (row.get("task_id") == task["id"]
                and row.get("status") in ("pending", "in_progress", "deferred", "partial")):
            row["status"] = "discarded"
            row["closed_at"] = now
            row["updated_at"] = now
    task["is_active"] = False


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
    days = params.get("p_after_completion_days")
    if days is not None and (not isinstance(days, int)
                             or isinstance(days, bool)
                             or not 1 <= days <= 365):
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
    if days is not None:
        round_all = [row for row in db.rows["planning_occurrence"]
                     if row.get("task_id") == task["id"]
                     and row.get("round_key") == params.get("p_round_key")]
        if round_all and all(row.get("handled_at") for row in round_all):
            handled = max(_discard_dt(row["handled_at"]) for row in round_all)
            if task.get("is_active"):
                task["last_handled_at"] = handled.isoformat()
                task["refresh_next_due_at"] = (
                    handled + timedelta(days=days)).isoformat()
            else:
                raise RuntimeError(
                    "planning_split_occurrence: task already inactive (concurrent change)")
    return created_ids


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


class _RpcCall:
    """镜像迁移 1B 两个原子合并函数的语义（真库行为由 pgserver 套件验证）。

    - planning_takeover_reschedule_request：CAS 接管身份，expected 键随同
      一语句原子并入 absorbed；条件不命中返回 False（PostgREST 布尔）。
    - planning_absorb_reschedule_request：幂等原子登记（已是 current 或已
      absorbed 时不写）。
    """

    def __init__(self, db, fn, params):
        self.db, self.fn, self.params = db, fn, dict(params or {})

    @staticmethod
    def _merge(absorbed, *keys):
        merged = list(absorbed or [])
        for key in keys:
            if key and key not in merged:
                merged.append(key)
        return merged

    def execute(self):
        rows = self.db.rows["planning_task"]
        task_id = self.params.get("p_task_id")
        row = next((r for r in rows if r.get("id") == task_id), None)
        now = self.params.get("p_now")
        if self.fn == "planning_takeover_reschedule_request":
            expected = self.params.get("p_expected_key")
            if not row or not expected or row.get("request_key") != expected:
                return SimpleNamespace(data=False)
            row["request_key"] = self.params["p_new_key"]
            row["request_est_start"] = self.params["p_new_est_start"]
            row["request_absorbed_keys"] = self._merge(
                row.get("request_absorbed_keys"), expected)
            row["updated_at"] = now
            return SimpleNamespace(data=True)
        if self.fn == "planning_patch_occurrence_round":
            emulate_planning_round_patch(self.db.rows["planning_occurrence"], self.params)
            return SimpleNamespace(data=[])
        if self.fn == "planning_update_once_task_guarded":
            emulate_planning_once_task_guarded(
                self.db.rows["planning_task"], self.db.rows["planning_occurrence"],
                self.params)
            return SimpleNamespace(data=[])
        if self.fn == "planning_insert_once_occurrence":
            emulate_planning_insert_once_occurrence(self.db, self.params)
            return SimpleNamespace(data=[])
        if self.fn == "planning_insert_round_occurrence":
            emulate_planning_insert_round_occurrence(self.db, self.params)
            return SimpleNamespace(data=[])
        if self.fn == "planning_discard_task":
            emulate_planning_discard_task(
                self.db.rows["planning_task"], self.db.rows["planning_occurrence"],
                self.params)
            return SimpleNamespace(data=[])
        if self.fn == "planning_split_occurrence":
            data = emulate_planning_split_occurrence(self.db, self.params)
            return SimpleNamespace(data=data)
        if self.fn == "planning_request_recompute":
            emulate_planning_request_recompute(
                self.db.rows["planning_recompute_state"], self.params)
            return SimpleNamespace(data=[])
        if self.fn == "planning_clear_recompute_mark":
            emulate_planning_clear_recompute_mark(
                self.db.rows["planning_recompute_state"], self.params)
            return SimpleNamespace(data=[])
        if self.fn == "planning_update_cycle_boundary":
            data = emulate_planning_update_cycle_boundary(self.db, self.params)
            return SimpleNamespace(data=data)
        if self.fn == "planning_absorb_reschedule_request":
            key = self.params.get("p_request_key")
            if (not row or not key
                    or row.get("request_key") == key
                    or key in (row.get("request_absorbed_keys") or [])):
                return SimpleNamespace(data=False)
            row["request_absorbed_keys"] = self._merge(
                row.get("request_absorbed_keys"), key)
            row["updated_at"] = now
            return SimpleNamespace(data=True)
        raise AssertionError(f"unknown rpc: {self.fn}")


class _Database:
    def __init__(self):
        self.rows = {"planning_task": [], "planning_occurrence": [],
                     "planning_recompute_state": [
                         {"id": 1, "requested_at": None, "reason": None,
                          "request_token": None}]}
        self.counters, self.writes = {}, []

    def next_id(self, name):
        self.counters[name] = self.counters.get(name, 0) + 1
        return self.counters[name]

    def table(self, name):
        return _Query(self, name)

    def rpc(self, fn, params=None):
        return _RpcCall(self, fn, params)


class PlanningIdentityPathTests(unittest.TestCase):
    def setUp(self):
        self.db = _Database()
        self.now = datetime(2026, 9, 24, 7, tzinfo=BEIJING)
        self.client_patch = mock.patch.object(planning, "get_client", return_value=self.db)
        self.setting_patch = mock.patch.object(planning.db, "load_app_setting", return_value=None)
        self.client_patch.start()
        self.setting_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.addCleanup(self.setting_patch.stop)

    def test_create_generate_read_round_identity_and_cross_day_display(self):
        task = planning.create_task({"content": "安排", "task_type": "daily",
                                     "estimated_minutes": 30}, self.now)
        rows = self.db.rows["planning_occurrence"]
        self.assertEqual(task["refresh_mode"], "daily")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["round_key"], "cycle:2026-09-24")
        self.assertEqual(rows[0]["schedule_date"], "2026-09-24")
        self.assertEqual(rows[0]["for_date"], rows[0]["schedule_date"])
        self.assertEqual(rows[0]["display_cycle_date"], "2026-09-24")
        self.assertEqual(rows[0]["estimated_time_source"], "automatic")
        board = planning.today_board(self.now)
        self.assertEqual(board["progress"][0]["round_key"], rows[0]["round_key"])
        self.assertEqual(planning.list_occurrences(schedule_date="2026-09-24", now=self.now)[0]["round_key"],
                         rows[0]["round_key"])
        self.assertEqual(planning.list_occurrences(for_date="2026-09-24", now=self.now)[0]["for_date"],
                         "2026-09-24")
        self.assertEqual(planning.list_occurrences(for_date="2026-09-25", now=self.now), [])
        with self.assertRaises(planning.PlanningError):
            planning.list_occurrences(for_date="2026-09-25", schedule_date="2026-09-24", now=self.now)

    def test_manual_time_and_release_write_complete_ownership_tuple(self):
        planning.create_task({"content": "安排", "task_type": "daily",
                              "estimated_minutes": 30}, self.now)
        occ = self.db.rows["planning_occurrence"][0]
        start = datetime(2026, 9, 25, 8, tzinfo=BEIJING)
        planning.patch_occurrence(occ["id"], {"est_start": start.isoformat()}, self.now)
        self.assertEqual((occ["estimated_time_source"], occ["fixed_source"], occ["schedule_managed"], occ["is_fixed"]),
                         ("manual", "manual", False, True))
        self.assertEqual((occ["round_key"], occ["schedule_date"]), ("cycle:2026-09-24", "2026-09-24"))
        self.assertEqual((occ["display_cycle_date"], occ["display_reason"]), ("2026-09-25", "manual_defer"))
        self.assertTrue({"est_start", "est_end", "estimated_time_source", "fixed_source",
                         "schedule_managed", "is_fixed"} <= self.db.writes[-1][1].keys())
        planning.patch_occurrence(occ["id"], {"is_fixed": False}, self.now)
        self.assertEqual((occ["estimated_time_source"], occ["fixed_source"], occ["schedule_managed"], occ["is_fixed"]),
                         ("unassigned", None, True, False))
        self.assertIsNone(occ["est_start"])
        self.assertIsNone(occ["est_end"])
        self.assertEqual(occ["for_date"], "2026-09-24")

    def test_zero_freedom_window_rule_fixed_cannot_be_relabelled_as_automatic(self):
        # 零自由度窗口（窗口长恰等于占用跨度）生成期预锚定为 rule 固定
        # （机制继承原 explicit 分支，§13.2）；实例 patch 不能释放规则固定。
        planning.create_task({"content": "规则固定", "task_type": "daily",
                              "estimated_minutes": 60,
                              "window_start_tod": "08:00", "window_end_tod": "09:00"}, self.now)
        occ = self.db.rows["planning_occurrence"][0]
        self.assertEqual((occ["estimated_time_source"], occ["fixed_source"], occ["is_fixed"]),
                         ("rule", "rule", True))
        self.assertEqual(occ["est_start"], datetime(2026, 9, 24, 8, tzinfo=BEIJING).isoformat())
        before = len(self.db.writes)
        with self.assertRaises(planning.PlanningError) as error:
            planning.patch_occurrence(occ["id"], {"is_fixed": False}, self.now)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(len(self.db.writes), before)
        self.assertEqual((occ["estimated_time_source"], occ["fixed_source"]), ("rule", "rule"))

    def test_task_rule_edit_keeps_windowed_instance_frozen(self):
        # 规则修改只影响未来：已生成实例的身份与实例级数据（含冻结窗口与
        # 预估时间）不因任务定义变化被改写或清空。
        planning.create_task({"content": "规则固定", "task_type": "daily",
                              "estimated_minutes": 60,
                              "window_start_tod": "08:00", "window_end_tod": "09:00"}, self.now)
        task = self.db.rows["planning_task"][0]
        occ = self.db.rows["planning_occurrence"][0]
        self.assertEqual(occ["est_start"], datetime(2026, 9, 24, 8, tzinfo=BEIJING).isoformat())
        with mock.patch.object(planning, "_generate_due_quietly") as generate:
            planning.update_task(task["id"], {"estimated_minutes": 90}, self.now)
        generate.assert_called_once()
        self.assertEqual((occ["est_start"], occ["estimated_time_source"], occ["fixed_source"],
                          occ["is_fixed"], occ["schedule_managed"]),
                         (datetime(2026, 9, 24, 8, tzinfo=BEIJING).isoformat(), "rule", "rule",
                          True, True))
        self.assertEqual(occ["window_start_at"],
                         datetime(2026, 9, 24, 8, tzinfo=BEIJING).isoformat())
        self.assertEqual(occ["window_end_at"],
                         datetime(2026, 9, 24, 9, tzinfo=BEIJING).isoformat())
        # 实例保留规则固定时间，不参与自动排程。
        self.assertFalse(planning._freely_schedulable(occ, task))

    def test_task_window_template_change_writes_new_window_only_on_future_rounds(self):
        planning.create_task({"content": "规则固定", "task_type": "daily",
                              "estimated_minutes": 60,
                              "window_start_tod": "08:00", "window_end_tod": "09:00"}, self.now)
        task = self.db.rows["planning_task"][0]
        occ = self.db.rows["planning_occurrence"][0]
        # 当前实例保持生成时的冻结窗口与预锚定（已生成实例冻结）。
        self.assertEqual((occ["est_start"], occ["est_end"]),
                         (datetime(2026, 9, 24, 8, tzinfo=BEIJING).isoformat(),
                          datetime(2026, 9, 24, 9, tzinfo=BEIJING).isoformat()))
        self.assertEqual((occ["estimated_time_source"], occ["fixed_source"], occ["is_fixed"]),
                         ("rule", "rule", True))
        # 模板窗口修改（编辑入口接线属批次 6；此处播种任务行模拟）只影响
        # 尚未生成的未来实例。
        task["window_start_tod"] = "10:00"
        task["window_end_tod"] = "11:00"
        created = planning._create_occurrences(self.db, task, date(2026, 9, 25), self.now)
        self.assertEqual(created, 1)
        future = self.db.rows["planning_occurrence"][-1]
        self.assertEqual((future["est_start"], future["est_end"]),
                         (datetime(2026, 9, 25, 10, tzinfo=BEIJING).isoformat(),
                          datetime(2026, 9, 25, 11, tzinfo=BEIJING).isoformat()))
        self.assertEqual((future["window_start_at"], future["window_end_at"]),
                         (datetime(2026, 9, 25, 10, tzinfo=BEIJING).isoformat(),
                          datetime(2026, 9, 25, 11, tzinfo=BEIJING).isoformat()))
        self.assertEqual((future["estimated_time_source"], future["fixed_source"]),
                         ("rule", "rule"))

    def test_manual_time_edit_in_same_display_cycle_preserves_carryover_reason(self):
        planning.create_task({"content": "顺延", "task_type": "daily", "estimated_minutes": 30}, self.now)
        occ = self.db.rows["planning_occurrence"][0]
        occ.update({"display_cycle_date": "2026-09-25", "display_reason": "carryover"})
        moved = datetime(2026, 9, 25, 16, tzinfo=BEIJING)
        planning.patch_occurrence(occ["id"], {"est_start": moved.isoformat()},
                                  datetime(2026, 9, 25, 7, tzinfo=BEIJING))
        self.assertEqual((occ["display_cycle_date"], occ["display_reason"]),
                         ("2026-09-25", "carryover"))
        self.assertEqual(occ["estimated_time_source"], "manual")
        self.assertEqual(occ["for_date"], "2026-09-24")

    def test_invalid_fixed_half_state_rejected_before_write(self):
        planning.create_task({"content": "安排", "task_type": "daily",
                              "estimated_minutes": 30}, self.now)
        occ = self.db.rows["planning_occurrence"][0]
        before = len(self.db.writes)
        with self.assertRaises(planning.PlanningError):
            planning.patch_occurrence(occ["id"], {"est_start": None, "is_fixed": True}, self.now)
        self.assertEqual(len(self.db.writes), before)

    def test_status_reschedule_uses_same_manual_tuple(self):
        planning.create_task({"content": "安排", "task_type": "daily",
                              "estimated_minutes": 30}, self.now)
        occ = self.db.rows["planning_occurrence"][0]
        planning.set_occurrence_status(occ["id"], {
            "status": "deferred", "est_start": datetime(2026, 9, 25, 8, tzinfo=BEIJING).isoformat(),
        }, self.now)
        self.assertEqual((occ["estimated_time_source"], occ["fixed_source"], occ["schedule_managed"], occ["is_fixed"]),
                         ("manual", "manual", False, True))
        self.assertEqual((occ["round_key"], occ["schedule_date"], occ["display_cycle_date"]),
                         ("cycle:2026-09-24", "2026-09-24", "2026-09-25"))

    def test_hollow_round_is_created_as_one_pair_and_replay_is_idempotent(self):
        task = {"id": 7, "content": "中空", "task_type": "daily", "refresh_mode": "daily",
                "time_mode": "duration", "estimated_minutes": 10, "is_hollow": True,
                "hollow_wait_minutes": 30, "hollow_end_minutes": 5}
        self.db.rows["planning_task"].append(task)
        self.assertEqual(planning._create_occurrences(self.db, task, date(2026, 9, 24), self.now), 2)
        self.assertEqual(planning._create_occurrences(self.db, task, date(2026, 9, 24), self.now), 0)
        rows = self.db.rows["planning_occurrence"]
        self.assertEqual({row["phase"] for row in rows}, {"start", "end"})
        self.assertEqual({row["round_key"] for row in rows}, {"cycle:2026-09-24"})
        self.assertEqual({row["phase_group"] for row in rows},
                         {str(round_phase_group(7, "cycle:2026-09-24"))})
        self.assertEqual({row["display_cycle_date"] for row in rows}, {"2026-09-24"})

    def test_early_rounds_have_independent_identity_and_fixed_baseline(self):
        # 提前完成现在先按正常生命周期补跑生成/到期清理；当前应有轮次被正常
        # 处理，之后的额外完成记录仍拥有独立 early 身份，且固定轴不动。
        task = {"id": 8, "content": "间歇", "task_type": "interval", "refresh_mode": "fixed_interval",
                "time_mode": "duration", "estimated_minutes": 10, "interval_days": 3,
                "is_active": True, "is_hollow": False,
                "created_at": datetime(2026, 9, 23, 6, tzinfo=BEIJING).isoformat(),
                "refresh_anchor_at": datetime(2026, 9, 23, 6, tzinfo=BEIJING).isoformat()}
        self.db.rows["planning_task"].append(task)
        # 到期轮次先生成并完成（正常处理路径）
        planning.generate_due(self.now)
        for row in list(self.db.rows["planning_occurrence"]):
            planning.set_occurrence_status(row["id"], {"status": "completed"}, self.now)
        first = planning.complete_task_early(8, self.now + timedelta(minutes=1), idempotency_key="e1")
        self.assertTrue(first["round_key"].startswith("early:2026-09-24:"))
        self.assertEqual(first["schedule_date"], "2026-09-24")
        # 同一固定刷新周期重复点击（不同请求键）返回已有记录，不重复新增
        again = planning.complete_task_early(8, self.now + timedelta(minutes=2), idempotency_key="e2")
        self.assertEqual(again["id"], first["id"])
        # 下一固定刷新周期（9/26 槽之后）：先正常完成 9/26 轮，再提前完成
        planning.generate_due(datetime(2026, 9, 26, 7, tzinfo=BEIJING))
        for row in self.db.rows["planning_occurrence"]:
            if row["schedule_date"] == "2026-09-26" and row["status"] in planning.OPEN_STATUSES:
                planning.set_occurrence_status(
                    row["id"], {"status": "completed"},
                    datetime(2026, 9, 26, 7, 15, tzinfo=BEIJING))
        second = planning.complete_task_early(
            8, datetime(2026, 9, 26, 7, 30, tzinfo=BEIJING), idempotency_key="e3")
        self.assertNotEqual(second["round_key"], first["round_key"])
        self.assertTrue(second["round_key"].startswith("early:2026-09-26:"))
        self.assertFalse(any("next_due" in patch for table, patch in self.db.writes if table == "planning_task"))

    def test_hollow_order_uses_its_own_round_not_another_round_of_same_task(self):
        group_a = str(round_phase_group(7, "cycle:2026-09-23"))
        group_b = str(round_phase_group(7, "cycle:2026-09-24"))
        self.db.rows["planning_occurrence"] = [
            {"id": 1, "task_id": 7, "round_key": "cycle:2026-09-23",
             "phase_group": group_a, "phase": "start", "status": "pending",
             "display_cycle_date": "2026-09-24"},
            {"id": 2, "task_id": 7, "round_key": "cycle:2026-09-23",
             "phase_group": group_a, "phase": "end", "status": "completed",
             "display_cycle_date": "2026-09-24"},
            {"id": 3, "task_id": 7, "round_key": "cycle:2026-09-24",
             "phase_group": group_b, "phase": "start", "status": "completed",
             "display_cycle_date": "2026-09-24"},
            {"id": 4, "task_id": 7, "round_key": "cycle:2026-09-24",
             "phase_group": group_b, "phase": "end", "status": "pending",
             "display_cycle_date": "2026-09-24"},
        ]
        with mock.patch.object(planning, "request_recompute"):
            self.assertEqual(planning.save_order([4, 1], self.now), {"saved": 2})

    def test_old_scheduler_phase_lookup_is_scoped_to_business_round(self):
        task = {"id": 7, "time_mode": "duration", "estimated_minutes": 10,
                "hollow_end_minutes": 10, "hollow_wait_minutes": 30}
        start_a = {"id": 1, "task_id": 7, "round_key": "cycle:2026-09-23",
                   "phase_group": str(round_phase_group(7, "cycle:2026-09-23")),
                   "phase": "start", "status": "pending", "sort_order": 0,
                   "is_fixed": True, "schedule_managed": True, "fixed_source": "rule",
                   "estimated_time_source": "rule",
                   "est_start": datetime(2026, 9, 24, 10, tzinfo=BEIJING).isoformat(),
                   "est_end": datetime(2026, 9, 24, 10, 10, tzinfo=BEIJING).isoformat()}
        end_b = {"id": 4, "task_id": 7, "round_key": "cycle:2026-09-24",
                 "phase_group": str(round_phase_group(7, "cycle:2026-09-24")),
                 "phase": "end", "status": "pending", "sort_order": 1,
                 "is_fixed": False, "schedule_managed": True, "fixed_source": None,
                 "estimated_time_source": "unassigned", "est_start": None, "est_end": None}
        result = planning.compute_schedule([start_a, end_b], {7: task},
                                           datetime(2026, 9, 24, 9, tzinfo=BEIJING))
        self.assertEqual(result.placed[4][0], datetime(2026, 9, 24, 10, 10, tzinfo=BEIJING))
        self.assertEqual(result.conflicts, [])

    def test_deadline_reader_uses_original_cycle_not_compatibility_date(self):
        task = {"deadline_tod": "12:00"}
        occ = {"is_limited": True, "schedule_date": "2026-09-24",
               "for_date": "2026-09-25"}
        self.assertEqual(planning._deadline_for(task, occ),
                         datetime(2026, 9, 24, 12, tzinfo=BEIJING).isoformat())
        self.assertIsNone(planning._deadline_for(task, {"is_limited": True, "for_date": "2026-09-24"}))

    def test_identity_assertion_detects_old_row_builder(self):
        task = {"id": 9, "content": "安排", "task_type": "daily", "refresh_mode": "daily",
                "time_mode": "duration", "estimated_minutes": 30}
        self.db.rows["planning_task"].append(task)
        original = planning._occurrence_row

        def broken_builder(*args, **kwargs):
            row = original(*args, **kwargs)
            row["round_key"] = None  # deliberate pre-F1 regression
            return row

        with mock.patch.object(planning, "_occurrence_row", broken_builder):
            planning._create_occurrences(self.db, task, date(2026, 9, 24), self.now)
        with self.assertRaises(AssertionError):
            self.assertEqual(self.db.rows["planning_occurrence"][0]["round_key"],
                             "cycle:2026-09-24")


if __name__ == "__main__":
    unittest.main()
