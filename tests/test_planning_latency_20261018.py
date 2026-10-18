from copy import deepcopy
from threading import Barrier
from unittest import mock

import pytest

from gateway import db, memo, planning, planning_generation, planning_runtime
from tests.support.planning_context import at
from tests.test_memo_api_contract import FakeClient as MemoClient
from tests.test_planning_create_latency import CountingClient, seed_daily


@pytest.fixture
def client():
    fake = CountingClient()
    with mock.patch.object(planning_runtime, "get_client", return_value=fake), \
            mock.patch.object(db, "get_client", return_value=fake):
        yield fake


@pytest.mark.parametrize("operation", ["resume", "duration"])
def test_saved_task_generation_does_not_scale_with_other_tasks(client, operation):
    counts = []
    for count in (10, 50):
        client.rows["planning_task"].clear()
        client.rows["planning_occurrence"].clear()
        seed_daily(client, count)
        if operation == "resume":
            client.rows["planning_task"][0]["refresh_enabled"] = False
        unaffected = deepcopy(client.rows["planning_occurrence"][1:])
        client.calls.clear()
        with mock.patch.object(planning_generation, "generate_task", wraps=planning_generation.generate_task) as generate:
            planning.update_task(1, {"refresh_enabled": True} if operation == "resume"
                                 else {"estimated_minutes": 31}, at(24, 8))
        assert [call.args[0] for call in generate.call_args_list] == [1]
        assert client.rows["planning_occurrence"][1:] == unaffected
        counts.append(len(client.calls))
    assert counts[0] == counts[1]
    assert counts[1] <= 10


def test_settled_daily_maintenance_is_bounded_and_read_only(client):
    seed_daily(client, 50)
    before = deepcopy(client.rows)
    client.calls.clear()
    result = planning.generate_due(at(24, 8))
    assert result["created"] == 0 and result["timed_out"] == 0
    assert client.rows == before
    assert len(client.calls) <= 5
    assert not any(call["kind"] == "rpc" or call.get("op") != "select" for call in client.calls)


def test_missing_current_round_still_generates_without_touching_settled_tasks(client):
    seed_daily(client, 50)
    client.rows["planning_occurrence"] = client.rows["planning_occurrence"][:-1]
    before = deepcopy(client.rows["planning_occurrence"])
    with mock.patch.object(planning_generation, "_reconcile_task_rounds",
                           wraps=planning_generation._reconcile_task_rounds) as reconcile:
        result = planning.generate_due(at(24, 8))
    assert result["created"] == 1
    assert [call.args[1]["id"] for call in reconcile.call_args_list] == [50]
    assert client.rows["planning_occurrence"][:49] == before
    assert len(client.rows["planning_occurrence"]) == 50


def test_open_old_round_is_not_skipped_by_current_round_fast_path(client):
    seed_daily(client, 50)
    old = deepcopy(client.rows["planning_occurrence"][0])
    old.update(id=51, schedule_date="2026-09-23", for_date="2026-09-23",
               round_key="cycle:2026-09-23", display_cycle_date="2026-09-23")
    client.rows["planning_occurrence"].append(old)
    unaffected = deepcopy(client.rows["planning_occurrence"][1:50])
    result = planning.generate_due(at(24, 8))
    assert result["timed_out"] == 1
    assert old["status"] == "timeout" and old["display_cycle_date"] == "2026-09-24"
    assert client.rows["planning_occurrence"][1:50] == unaffected


def test_incomplete_hollow_round_cannot_be_treated_as_settled(client):
    seed_daily(client, 1)
    task = client.rows["planning_task"][0]
    task.update(is_hollow=True, hollow_start_minutes=10, hollow_wait_minutes=20,
                hollow_end_minutes=10)
    context = planning.cycles.PlanningRequestContext(at(24, 8))
    settled = planning_generation._settled_daily_tasks(client, [task], context.cycle, True)
    assert settled == set()


def test_today_configuration_is_read_once_per_request(client):
    seed_daily(client, 50)
    client.calls.clear()
    board = planning.today_board(at(24, 8))
    assert len(board["progress"]) == 50
    assert len(client.settings_reads()) == 1
    assert len(client.calls) == 5


@pytest.mark.parametrize("kind,read_count", [("board", 5), ("search", 3)])
def test_memo_independent_reads_start_together_and_keep_150_rows(kind, read_count):
    client = MemoClient()
    base = client.rows["memo_entry"][0]
    client.rows["memo_entry"] = [{**base, "id": index, "title": f"测试 {index}",
                                  "content": "需要匹配的正文"} for index in range(1, 151)]
    client.rows["memo_entry_tag"] = [{"entry_id": index, "tag_id": 1} for index in range(1, 151)]
    barrier = Barrier(read_count, timeout=3)
    original = memo._rows

    def read_together(*args, **kwargs):
        barrier.wait()
        return original(*args, **kwargs)

    with mock.patch.object(memo, "_require_client", return_value=client), \
            mock.patch.object(memo, "_rows", side_effect=read_together):
        result = memo.get_board() if kind == "board" else memo.list_entries("active", "匹配")
    assert len(result["sections"][0]["items"] if kind == "board" else result) == 150


def test_memo_parallel_read_failure_is_not_returned_as_an_empty_success():
    client = MemoClient()
    original = memo._rows

    def fail_positions(client, table, *args, **kwargs):
        if table == "memo_position":
            raise RuntimeError("synthetic positions failure")
        return original(client, table, *args, **kwargs)

    with mock.patch.object(memo, "_require_client", return_value=client), \
            mock.patch.object(memo, "_rows", side_effect=fail_positions):
        with pytest.raises(RuntimeError, match="synthetic positions failure"):
            memo.get_board()
