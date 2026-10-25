import threading
from types import SimpleNamespace
from unittest import mock

from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway import admin_api, db, planning, planning_api, planning_commands, planning_runtime
from gateway.config import cfg
from tests.support.planning_db import CoreClient
from tests.test_planning_latency_pgserver import NOW


def test_board_independent_reads_reach_barrier_together():
    barrier = threading.Barrier(3)
    original = planning_runtime._rows
    names = []

    def read(client, table, query_fn=None):
        names.append(table)
        if table in {'planning_occurrence', 'planning_recompute_state'}:
            barrier.wait(timeout=3)
        return original(client, table, query_fn)

    client = CoreClient()
    with mock.patch.object(planning_runtime, 'get_client', return_value=client), \
            mock.patch.object(db, 'get_client', return_value=client), \
            mock.patch.object(planning_runtime, '_rows', side_effect=read):
        board = planning.today_board(NOW)
    assert board['progress'] == []
    assert names.count('planning_occurrence') == 2
    assert names.count('planning_recompute_state') == 1


def test_memory_summary_and_exact_count_share_one_query_without_truncating_details():
    builder = mock.MagicMock()
    builder.select.return_value = builder
    builder.eq.return_value = builder
    builder.order.return_value = builder
    builder.range.return_value = builder
    builder.execute.return_value = SimpleNamespace(data=[{'id': 20, 'content': '长正文' * 200}], count=51)
    client = mock.MagicMock()
    client.table.return_value = builder
    with mock.patch.object(db, 'get_client', return_value=client):
        result = admin_api._execute_list('memories', admin_api._TABLES['memories'],
            select='id,content', eq={'is_active': True}, order='created_at', ascending=False,
            offset=20, limit=20, search='', count_only=False, with_count=True, summary=True)
    builder.select.assert_called_once_with('id,content', count='exact')
    builder.range.assert_called_once_with(20, 39)
    builder.execute.assert_called_once()
    assert result['count'] == 51
    assert len(result['data'][0]['content']) == 160
    assert len(builder.execute.return_value.data[0]['content']) == 600


def test_command_endpoint_auth_json_validation_and_dispatch():
    app = Starlette(routes=planning_api.planning_api_routes)
    with mock.patch.object(cfg, 'GATEWAY_TOKEN', 'latency-test-token'), TestClient(app) as http:
        path = '/admin/api/planning/occurrences/1/command'
        with mock.patch.object(planning_commands, 'occurrence_command', return_value={'id': 1, 'status': 'completed'}) as command:
            assert http.post(path, json={}).status_code == 401
            command.assert_not_called()
            headers = {'Authorization': 'Bearer latency-test-token'}
            assert http.post(path, content='invalid json', headers=headers).status_code == 400
            command.assert_not_called()
            payload = {'status': 'completed', 'request_key': '849cdaf9-e19b-4fb2-880c-b6b19950928c'}
            saved = http.post(path, json=payload, headers=headers)
            assert saved.status_code == 200
            assert saved.json()['status'] == 'completed'
            command.assert_called_once_with(1, payload)
