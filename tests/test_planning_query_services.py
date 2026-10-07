"""GET service boundaries preserve errors, read order and display-time sampling."""

from copy import deepcopy
from datetime import timedelta
from unittest import mock

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway import planning, planning_runtime as runtime
from gateway.config import cfg
from gateway.planning_api import planning_api_routes
from tests.support.planning_context import at, setup_core
from tests.support.planning_fixtures import seed_occ, seed_task


NOW = at(24, 10)
AUTH = {"Authorization": "Bearer query-service-test"}


@pytest.fixture
def query_context(monkeypatch):
    client, patch_context = setup_core()
    monkeypatch.setattr(cfg, "GATEWAY_TOKEN", "query-service-test")
    with patch_context():
        seed_task(client, 1, content="读取前的任务")
        seed_occ(client, 2, 1, now=NOW, est_start=NOW.isoformat(),
                 est_end=(NOW + timedelta(minutes=30)).isoformat())
        with TestClient(Starlette(routes=planning_api_routes)) as http:
            yield client, http


@pytest.mark.parametrize("kind,row_id", [("tasks", 1), ("occurrences", 2)])
def test_get_returns_serialized_row_without_writes(query_context, kind, row_id):
    client, http = query_context
    original = deepcopy(client.rows)
    with mock.patch.object(runtime, "_now", return_value=NOW) as clock:
        response = http.get(f"/admin/api/planning/{kind}/{row_id}", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["id"] == row_id
    assert response.json()["content"] == "读取前的任务"
    if kind == "occurrences":
        assert response.json()["schedule_label"] == "正常"
        assert response.json()["estimated_minutes"] == 30
    assert client.rows == original
    clock.assert_called_once_with()


@pytest.mark.parametrize("kind,row_id", [("tasks", 99), ("occurrences", 99)])
def test_missing_get_returns_404_without_sampling_clock(query_context, kind, row_id):
    _, http = query_context
    with mock.patch.object(runtime, "_now") as clock:
        response = http.get(f"/admin/api/planning/{kind}/{row_id}", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error_code"] == "not_found"
    clock.assert_not_called()


def test_missing_occurrence_does_not_fetch_task(query_context):
    with mock.patch.object(runtime, "_fetch_task") as fetch_task:
        with pytest.raises(planning.PlanningError) as caught:
            planning.get_occurrence(99)
    assert caught.value.status_code == 404
    fetch_task.assert_not_called()


def test_orphan_occurrence_returns_task_404_without_sampling_clock(query_context):
    client, http = query_context
    client.rows["planning_task"].clear()
    with mock.patch.object(runtime, "_now") as clock:
        response = http.get("/admin/api/planning/occurrences/2", headers=AUTH)
    assert response.status_code == 404
    assert response.json() == {
        "error": "待办任务不存在", "error_code": "not_found",
    }
    clock.assert_not_called()


@pytest.mark.parametrize("kind,row_id", [("tasks", 1), ("occurrences", 2)])
def test_unavailable_database_returns_503_before_clock(query_context, kind, row_id):
    _, http = query_context
    with mock.patch.object(runtime, "get_client", return_value=None), \
         mock.patch.object(runtime, "_now") as clock:
        response = http.get(f"/admin/api/planning/{kind}/{row_id}", headers=AUTH)
    assert response.status_code == 503
    assert response.json()["error_code"] == "database_unavailable"
    clock.assert_not_called()


@pytest.mark.parametrize("kind,row_id", [("tasks", 1), ("occurrences", 2)])
def test_unauthorized_get_never_accesses_database(query_context, kind, row_id):
    _, http = query_context
    with mock.patch.object(runtime, "get_client") as get_client:
        response = http.get(f"/admin/api/planning/{kind}/{row_id}")
    assert response.status_code == 401
    assert response.json()["error_code"] == "unauthorized"
    get_client.assert_not_called()


@pytest.mark.parametrize("service,row_id", [
    (planning.get_task, 1), (planning.get_occurrence, 2),
])
def test_explicit_display_time_does_not_read_default_clock(query_context, service, row_id):
    with mock.patch.object(runtime, "_now") as clock:
        result = service(row_id, NOW + timedelta(minutes=1))
    assert result["id"] == row_id
    if service is planning.get_occurrence:
        assert result["schedule_label"] == "落后"
    clock.assert_not_called()


@pytest.mark.parametrize("service,row_id", [
    (planning.get_task, 1), (planning.get_occurrence, 2),
])
def test_default_clock_is_sampled_after_rows_are_read(query_context, service, row_id):
    client, _ = query_context

    def sample_after_read():
        # The response should retain the content fetched before this clock read.
        client.rows["planning_task"][0]["content"] = "读取时钟后改变的任务"
        return NOW + timedelta(minutes=1)

    with mock.patch.object(runtime, "_now", side_effect=sample_after_read) as clock:
        result = service(row_id)
    assert result["content"] == "读取前的任务"
    if service is planning.get_occurrence:
        assert result["schedule_label"] == "落后"
    clock.assert_called_once_with()
