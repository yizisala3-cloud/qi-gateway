"""Create latency regressions through the public service and real planning rules.

The fake records executed database requests, including settings reads and RPCs.
Timing below measures the same in-memory business path, not network or production
latency; it accompanies the structural assertion that generation no longer grows
with the number of unrelated daily tasks.
"""

from copy import deepcopy
from datetime import timedelta
from time import perf_counter
from types import SimpleNamespace
from unittest import mock

import pytest

from gateway import db, planning, planning_generation, planning_runtime
from gateway import planning_schedule
from tests.support.planning_context import at
from tests.support.planning_db import CoreClient, CoreQuery
from tests.support.planning_fixtures import HOLLOW, seed_occ, seed_task


BOUNDARY_KEY = planning.PLANNING_BOUNDARY_STATE_KEY
DAILY_KEY = planning.PLANNING_DAILY_REFRESH_KEY
CREATE_KEYS = {BOUNDARY_KEY, DAILY_KEY}


class CountingQuery(CoreQuery):
    def execute(self):
        event = {
            "kind": "query", "table": self.table, "op": self.op,
            "filters": deepcopy(self.filters), "payload": deepcopy(self.payload),
        }
        self.client.calls.append(event)
        if self.client.before_query is not None:
            self.client.before_query(event)
        return super().execute()


class CountingClient(CoreClient):
    """Count actual execute calls while retaining the shared fake's RPC rules."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.before_query = None
        self.before_rpc = None

    def table(self, name):
        assert name in self.rows, f"unexpected table access: {name}"
        return CountingQuery(self, name)

    def rpc(self, name, params=None):
        def execute():
            event = {"kind": "rpc", "name": name, "params": deepcopy(params or {})}
            self.calls.append(event)
            if self.before_rpc is not None:
                self.before_rpc(event)
            return super(CountingClient, self).rpc(name, params).execute()

        return SimpleNamespace(execute=execute)

    def set_setting(self, key, value):
        rows = self.rows["app_settings"]
        existing = next((row for row in rows if row["key"] == key), None)
        if existing is None:
            rows.append({"key": key, "value": value})
        else:
            existing["value"] = value

    def settings_reads(self):
        return [call for call in self.calls
                if call.get("table") == "app_settings" and call["op"] == "select"]

    def rpc_calls(self, name):
        return [call for call in self.calls if call.get("name") == name]


@pytest.fixture
def client():
    fake = CountingClient()
    # Do not replace settings helpers: their query construction and failure
    # sentinel are part of the behaviour under test.
    with mock.patch.object(planning_runtime, "get_client", return_value=fake), \
            mock.patch.object(db, "get_client", return_value=fake):
        yield fake


def create(now=None, **fields):
    return planning.create_task({
        "content": "新建每日待办", "task_type": "daily",
        "estimated_minutes": 30, **fields,
    }, now or at(24, 7))


def seed_daily(client, count, now=None, *, with_occurrences=True):
    now = now or at(24, 7)
    for task_id in range(1, count + 1):
        seed_task(client, task_id)
        client.rows["planning_task"][-1].update({
            "created_at": at(23, 7).isoformat(),
            "updated_at": at(23, 7).isoformat(),
        })
        if with_occurrences:
            seed_occ(client, task_id, task_id, now=now, sort_order=task_id * 10)
    client._counters["planning_task"] = count
    client._counters["planning_occurrence"] = count if with_occurrences else 0


def assert_one_create_settings_read(client):
    reads = client.settings_reads()
    assert len(reads) == 1, reads
    assert reads[0]["filters"] == [("in", "key", mock.ANY)]
    assert set(reads[0]["filters"][0][2]) == CREATE_KEYS


def test_create_roundtrips_do_not_grow_with_existing_daily_tasks(client, record_property):
    counts = {}
    for existing_count in (0, 10, 50):
        client.rows["planning_task"].clear()
        client.rows["planning_occurrence"].clear()
        seed_daily(client, existing_count)
        client.calls.clear()
        with mock.patch.object(planning_generation, "_reconcile_task_rounds",
                               wraps=planning_generation._reconcile_task_rounds) as reconcile, \
                mock.patch.object(planning_schedule, "compute_schedule",
                                  wraps=planning_schedule.compute_schedule) as schedule:
            started = perf_counter()
            task = create()
            elapsed_ms = (perf_counter() - started) * 1000

        assert [call.args[1]["id"] for call in reconcile.call_args_list] == [task["id"]]
        assert_one_create_settings_read(client)
        assert schedule.call_count == 1
        scheduled_rows, task_map = schedule.call_args.args[:2]
        assert {row["task_id"] for row in scheduled_rows} == set(range(1, existing_count + 2))
        assert set(task_map) == set(range(1, existing_count + 2))
        assert len(client.rpc_calls("planning_insert_round_occurrence")) == 1
        batches = client.rpc_calls("planning_apply_recompute_batch")
        assert len(batches) == 1
        assert {row["id"] for row in batches[0]["params"]["p_expected"]} == {
            row["id"] for row in scheduled_rows
        }
        assert all(row["estimated_time_source"] == "automatic"
                   for row in client.rows["planning_occurrence"])
        assert task["first_round_skipped"] is False
        assert task["schedule_conflict"] is False
        counts[existing_count] = len(client.calls)
        record_property(f"create_{existing_count}_database_requests", len(client.calls))
        record_property(f"create_{existing_count}_in_memory_ms", round(elapsed_ms, 3))

    assert len(set(counts.values())) == 1, counts


def test_create_leaves_unrelated_missing_daily_rounds_to_full_maintenance(client):
    seed_daily(client, 2, with_occurrences=False)
    task = create()
    assert [row["task_id"] for row in client.rows["planning_occurrence"]] == [task["id"]]
    with mock.patch.object(planning_generation, "_reconcile_task_rounds",
                           wraps=planning_generation._reconcile_task_rounds) as reconcile:
        result = planning.generate_due(at(24, 8))
    assert result["created"] == 2
    assert {call.args[1]["id"] for call in reconcile.call_args_list} == {1, 2, task["id"]}
    assert {row["task_id"] for row in client.rows["planning_occurrence"]} == {1, 2, task["id"]}


@pytest.mark.parametrize("fields,settings", [
    ({}, {DAILY_KEY: False}),
    ({"refresh_enabled": False}, {}),
])
def test_create_respects_daily_switch_and_task_pause(client, fields, settings):
    for key, value in settings.items():
        client.set_setting(key, value)
    task = create(**fields)
    assert len(client.rows["planning_task"]) == 1
    assert client.rows["planning_occurrence"] == []
    assert client.rpc_calls("planning_apply_recompute_batch") == []
    assert task["is_active"] is True
    assert_one_create_settings_read(client)


@pytest.mark.parametrize("fields,prefix", [
    ({}, "cycle:2026-09-24"),
    ({"task_type": "once"}, "once"),
    ({"task_type": "idle"}, "once"),
    ({"task_type": "weekly", "weekdays": [3]}, "cycle:2026-09-24"),
    ({"task_type": "monthly", "month_days": [24]}, "cycle:2026-09-24"),
    ({"task_type": "interval", "interval_days": 3,
      "refresh_mode": "fixed_interval"}, "fixed:2026-09-24:"),
    ({"task_type": "interval", "interval_days": 3,
      "refresh_mode": "after_completion"}, "handled:2026-09-24:"),
])
def test_create_keeps_each_refresh_models_round_identity(client, fields, prefix):
    task = create(**fields)
    rows = client.rows["planning_occurrence"]
    assert len(rows) == 1
    assert rows[0]["task_id"] == task["id"]
    assert rows[0]["round_key"].startswith(prefix)
    assert rows[0]["schedule_date"] == rows[0]["display_cycle_date"] == "2026-09-24"
    assert rows[0]["planned_minutes"] == 30
    assert task["first_round_skipped"] is False
    assert_one_create_settings_read(client)


@pytest.mark.parametrize("fields", [
    {"task_type": "weekly", "weekdays": [4]},
    {"task_type": "monthly", "month_days": [25]},
    {"task_type": "interval", "interval_days": 3,
     "refresh_mode": "fixed_interval", "refresh_anchor_at": at(27, 7).isoformat()},
    {"task_type": "once", "target_date": "2026-09-25"},
])
def test_create_does_not_force_a_future_rule_round(client, fields):
    task = create(**fields)
    assert client.rows["planning_occurrence"] == []
    assert task["first_round_skipped"] is False
    assert task["schedule_conflict"] is False


@pytest.mark.parametrize("daily_value", [None, "false", 0, {}, []])
def test_missing_or_malformed_daily_switch_keeps_enabled_default(client, daily_value):
    if daily_value is not None:
        client.set_setting(DAILY_KEY, daily_value)
    create()
    assert len(client.rows["planning_occurrence"]) == 1


@pytest.mark.parametrize("boundary_value", [None, "04:00", 0, []])
def test_missing_or_non_object_boundary_keeps_six_oclock_default(client, boundary_value):
    if boundary_value is not None:
        client.set_setting(BOUNDARY_KEY, boundary_value)
    create(at(24, 5))
    assert client.rows["planning_occurrence"][0]["round_key"] == "cycle:2026-09-23"


def test_invalid_boundary_rejects_bounded_creation_before_task_write(client):
    client.set_setting(BOUNDARY_KEY, {"boundary": "broken"})
    with pytest.raises(planning.PlanningError) as error:
        create(window_start_tod="08:00", window_end_tod="09:00")
    assert error.value.code == "invalid_setting"
    assert error.value.status_code == 500
    assert client.rows["planning_task"] == []


@pytest.mark.parametrize("bounded", [False, True])
def test_settings_query_failure_preserves_pre_and_post_write_failure_boundaries(client, bounded, caplog):
    def fail_settings(event):
        if event["table"] == "app_settings":
            raise RuntimeError("simulated settings connection failure")

    client.before_query = fail_settings
    fields = {"window_start_tod": "08:00", "window_end_tod": "09:00"} if bounded else {}
    if bounded:
        with pytest.raises(planning.PlanningError) as error:
            create(**fields)
        assert error.value.code == "database_unavailable"
        assert error.value.status_code == 503
        assert client.rows["planning_task"] == []
    else:
        task = create(**fields)
        assert client.rows["planning_task"][0]["id"] == task["id"]
        assert "同步生成失败" in caplog.text
    assert client.rows["planning_occurrence"] == []


def test_daily_query_failure_is_interpreted_after_successful_window_validation(client, caplog):
    # A successful boundary read must retain its pre-insert semantics even when
    # a different setting in the same batch has a failure sentinel.
    with mock.patch.object(db, "load_app_settings", return_value={
        BOUNDARY_KEY: {"boundary": "06:00", "transition": None, "absorbed": []},
        DAILY_KEY: db.APP_SETTING_QUERY_FAILED,
    }):
        task = create(window_start_tod="08:00", window_end_tod="09:00")
    assert client.rows["planning_task"][0]["id"] == task["id"]
    assert client.rows["planning_occurrence"] == []
    assert "同步生成失败" in caplog.text


def test_request_snapshot_reused_for_generation_and_schedule_then_refreshed_next_request(client):
    client.set_setting(BOUNDARY_KEY, {"boundary": "04:00", "transition": None, "absorbed": []})

    def change_boundary_after_validation(event):
        if event["table"] == "planning_task" and event["op"] == "insert":
            client.set_setting(BOUNDARY_KEY, {"boundary": "06:00", "transition": None, "absorbed": []})

    client.before_query = change_boundary_after_validation
    # 06:00–08:00 is valid under both boundaries, so the database's boundary
    # guard would also permit this task. The snapshot controls the request's
    # cycle attribution while database guards remain authoritative.
    task = create(at(24, 5), window_start_tod="06:00", window_end_tod="08:00")
    row = client.rows["planning_occurrence"][0]
    assert task["first_round_skipped"] is False
    assert row["round_key"] == "cycle:2026-09-24"
    assert row["schedule_date"] == row["display_cycle_date"] == "2026-09-24"
    assert row["window_end_at"] == at(24, 8).isoformat()
    assert row["est_start"] == at(24, 6).isoformat()
    assert_one_create_settings_read(client)

    client.before_query = None
    client.calls.clear()
    second = create(at(24, 5))
    new_row = next(row for row in client.rows["planning_occurrence"] if row["task_id"] == second["id"])
    assert new_row["round_key"] == "cycle:2026-09-23"
    assert_one_create_settings_read(client)


@pytest.mark.parametrize("hour,cycle_key", [(3, "2026-09-24"), (5, "2026-09-25")])
def test_creation_during_boundary_transition_uses_frozen_current_cycle(client, hour, cycle_key):
    client.set_setting(BOUNDARY_KEY, {
        "boundary": "04:00", "absorbed": [],
        "transition": {"spanning_key": "2026-09-24", "spanning_boundary": "06:00",
                       "change_at": at(24, 15).isoformat()},
    })
    create(at(25, hour))
    row = client.rows["planning_occurrence"][0]
    assert row["round_key"] == f"cycle:{cycle_key}"
    assert row["schedule_date"] == row["display_cycle_date"] == cycle_key
    assert row["estimated_time_source"] == "automatic"
    assert_one_create_settings_read(client)


def test_first_round_deadline_skip_stays_settled_until_next_cycle(client):
    task = create(at(24, 10), window_start_tod="08:00", window_end_tod="09:00")
    assert task["first_round_skipped"] is True
    assert task["schedule_conflict"] is False
    assert client.rows["planning_occurrence"] == []
    assert client.rows["planning_task"][0]["refresh_generated_through"] == "2026-09-24"
    assert_one_create_settings_read(client)
    assert planning.generate_due(at(24, 11))["created"] == 0
    assert planning.generate_due(at(25, 7))["created"] == 1
    assert client.rows["planning_occurrence"][0]["round_key"] == "cycle:2026-09-25"


def test_hollow_create_generates_one_atomic_pair_and_preserves_wait(client):
    task = create(**HOLLOW)
    rows = client.rows["planning_occurrence"]
    assert len(rows) == 2
    assert {row["phase"] for row in rows} == {"start", "end"}
    assert {row["round_key"] for row in rows} == {"cycle:2026-09-24"}
    assert len({row["phase_group"] for row in rows}) == 1
    assert {row["task_id"] for row in rows} == {task["id"]}
    assert len(client.rpc_calls("planning_insert_round_occurrence")) == 1
    batch = client.rpc_calls("planning_apply_recompute_batch")[0]["params"]
    assert len(batch["p_rounds"]) == 1
    assert batch["p_singles"] == []
    start, end = sorted(rows, key=lambda row: row["phase"] == "end")
    assert planning._parse_dt(end["est_start"], "est_start") - \
        planning._parse_dt(start["est_end"], "est_end") == timedelta(minutes=60)


@pytest.mark.parametrize("hollow", [False, True])
def test_generation_failure_keeps_saved_task_and_maintenance_can_recover(client, hollow, caplog):
    def fail_insert(event):
        if event["name"] == "planning_insert_round_occurrence":
            raise RuntimeError("simulated round insertion failure")

    client.before_rpc = fail_insert
    task = create(at(24, 8, 30), window_start_tod="08:00", window_end_tod="09:00",
                  **(HOLLOW if hollow else {}))
    assert len(client.rows["planning_task"]) == 1
    assert client.rows["planning_task"][0]["id"] == task["id"]
    assert task["first_round_skipped"] is False
    assert client.rows["planning_occurrence"] == []
    assert "生成失败" in caplog.text
    client.before_rpc = None
    result = planning.generate_due(at(24, 10))
    assert result["created"] == (2 if hollow else 1)
    assert len(client.rows["planning_task"]) == 1
    assert planning.generate_due(at(24, 11))["created"] == 0


def test_recompute_failure_keeps_created_round_without_claiming_an_estimate(client, caplog):
    def fail_recompute(event):
        if event["name"] == "planning_apply_recompute_batch":
            raise RuntimeError("simulated schedule connection failure")

    client.before_rpc = fail_recompute
    task = create()
    row = client.rows["planning_occurrence"][0]
    assert row["task_id"] == task["id"]
    assert row["est_start"] is row["est_end"] is None
    assert row["estimated_time_source"] == "unassigned"
    assert "同步重算失败" in caplog.text
    client.before_rpc = None
    assert planning.recompute_today(at(24, 8))["updated"] == 1
    assert row["estimated_time_source"] == "automatic"
    assert len(client.rows["planning_task"]) == 1


def test_partial_generation_failure_still_schedules_committed_occurrence(client, caplog):
    def fail_cursor_after_insert(event):
        if (event["table"] == "planning_task" and event["op"] == "update"
                and "refresh_generated_through" in event["payload"]):
            raise RuntimeError("simulated cursor write failure after round commit")

    client.before_query = fail_cursor_after_insert
    task = create(task_type="interval", interval_days=3, refresh_mode="fixed_interval")
    row = client.rows["planning_occurrence"][0]
    assert row["task_id"] == task["id"]
    assert row["round_key"].startswith("fixed:2026-09-24:")
    assert row["estimated_time_source"] == "automatic"
    assert len(client.rpc_calls("planning_apply_recompute_batch")) == 1
    assert "新任务生成失败" in caplog.text
    assert client.rows["planning_task"][0].get("refresh_generated_through") is None
    client.before_query = None
    assert planning.generate_due(at(24, 8))["created"] == 0
    assert len(client.rows["planning_occurrence"]) == 1
    assert client.rows["planning_task"][0]["refresh_generated_through"] == "2026-09-24"


def test_create_schedule_preserves_fixed_slot_and_includes_it_in_batch_guards(client):
    seed_daily(client, 1)
    existing = client.rows["planning_occurrence"][0]
    existing.update({
        "est_start": at(24, 7).isoformat(), "est_end": at(24, 7, 30).isoformat(),
        "is_fixed": True, "fixed_source": "manual", "estimated_time_source": "manual",
    })
    before = dict(existing)
    task = create()
    assert existing == before
    new_row = next(row for row in client.rows["planning_occurrence"] if row["task_id"] == task["id"])
    assert new_row["est_start"] == at(24, 7, 30).isoformat()
    batch = client.rpc_calls("planning_apply_recompute_batch")[0]["params"]
    assert {row["id"] for row in batch["p_expected"]} == {existing["id"], new_row["id"]}
    assert [row["id"] for row in batch["p_singles"]] == [new_row["id"]]


def test_create_schedule_preserves_lifecycle_facts_and_rejects_drift(client):
    seed_daily(client, 1)
    existing = client.rows["planning_occurrence"][0]
    existing.update({"est_start": at(24, 8).isoformat(), "est_end": at(24, 8, 30).isoformat()})
    old_estimate = (existing["est_start"], existing["est_end"])

    def start_existing_during_commit(event):
        if event["name"] == "planning_apply_recompute_batch":
            existing.update({"status": "in_progress", "actual_start": at(24, 7).isoformat()})

    client.before_rpc = start_existing_during_commit
    task = create()
    assert existing["status"] == "in_progress"
    assert existing["actual_start"] == at(24, 7).isoformat()
    assert (existing["est_start"], existing["est_end"]) == old_estimate
    new_row = next(row for row in client.rows["planning_occurrence"] if row["task_id"] == task["id"])
    assert new_row["est_start"] is None
    assert len(client.rows["planning_task"]) == 2


@pytest.mark.parametrize("edit", [{"is_active": False}, {"content": "锁等待期间已修改"}])
def test_created_task_is_reread_after_taking_the_shared_maintenance_lock(client, edit):
    original_lock = planning_runtime._maintenance_lock

    class EditingLock:
        def __enter__(self):
            original_lock.__enter__()
            client.rows["planning_task"][-1].update(edit)
            return self

        def __exit__(self, *args):
            return original_lock.__exit__(*args)

    with mock.patch.object(planning_runtime, "_maintenance_lock", EditingLock()):
        create()
    if edit.get("is_active") is False:
        assert client.rows["planning_occurrence"] == []
    else:
        assert client.rows["planning_occurrence"][0]["content_snapshot"] == edit["content"]


def test_update_and_split_keep_their_existing_full_generation_side_effect(client):
    task = create()
    seed_task(client, 2)
    client.rows["planning_task"][-1].update({"created_at": at(23, 7).isoformat(),
                                           "updated_at": at(23, 7).isoformat()})
    client._counters["planning_task"] = 2
    with mock.patch.object(planning_generation, "_reconcile_task_rounds",
                           wraps=planning_generation._reconcile_task_rounds) as reconcile:
        planning.update_task(task["id"], {"estimated_minutes": 31}, at(24, 8))
    assert {call.args[1]["id"] for call in reconcile.call_args_list} == {1, 2}
    assert {row["task_id"] for row in client.rows["planning_occurrence"]} == {1, 2}
    occurrence = next(row for row in client.rows["planning_occurrence"] if row["task_id"] == 1)
    result = planning.split_occurrence(occurrence["id"], {
        "parts": [{"content": "拆出单次", "estimated_minutes": 30}],
    }, at(24, 9))
    child_id = result["created_task_ids"][0]
    assert occurrence["status"] == "discarded_this"
    assert len([row for row in client.rows["planning_occurrence"] if row["task_id"] == child_id]) == 1
    assert next(row for row in client.rows["planning_task"] if row["id"] == child_id)["task_type"] == "once"
