"""#45/#46: validate before writing, bounded remote calls and optimistic inputs."""
from copy import deepcopy
from datetime import timedelta
from unittest import mock
from uuid import uuid4

import pytest
from gateway import db, planning_recompute as service, planning_runtime
from tests.test_planning_create_latency import CountingClient, seed_daily
from tests.support.planning_context import at
from tests.support.planning_fixtures import seed_task, seed_occ


@pytest.fixture
def client():
    client = CountingClient()
    with mock.patch.object(planning_runtime, 'get_client', return_value=client), \
         mock.patch.object(db, 'get_client', return_value=client):
        yield client


@pytest.mark.parametrize('count', [2, 13, 50, 100])
def test_confirm_remote_writes_are_constant(client, count):
    seed_daily(client, count, at(24))
    client.calls.clear()
    result = service.save_order(list(range(count, 0, -1)), at(24), request_key=str(uuid4()))
    assert result['saved'] == count
    assert len(client.calls) == 5
    assert len(client.rpc_calls('planning_save_order_with_schedule')) == 1
    assert not [c for c in client.calls if c.get('op') == 'update']
    assert result['schedule_applied'] is True
    assert result['updated'] == count
    assert len(result['estimates']) == count
    assert client.rows['planning_recompute_state'][0]['requested_at'] is None


def test_conflicting_order_does_not_write_and_has_chinese_reason(client):
    now = at(24)
    seed_daily(client, 2, now)
    client.rows['planning_occurrence'][0].update(window_start_at=now.isoformat(),
        window_end_at=(now + timedelta(minutes=30)).isoformat())
    before = deepcopy(client.rows)
    with pytest.raises(service.common.PlanningError) as error:
        service.save_order([2, 1], now)
    assert error.value.code == 'schedule_conflict'
    assert error.value.details['conflicts'][0]['constraint'] == 'window_end'
    assert '时段' in error.value.details['conflicts'][0]['reason']
    assert client.rows == before
    assert not client.rpc_calls('planning_save_order_with_schedule')


def test_concurrent_order_since_entering_mode_is_rejected(client):
    now = at(24)
    seed_daily(client, 2, now)
    expected = [{'id': r['id'], 'sort_order': r['sort_order'], 'status': r['status']}
                for r in client.rows['planning_occurrence']]
    client.rows['planning_occurrence'][0]['sort_order'] = 999
    with pytest.raises(service.common.PlanningError) as error:
        service.save_order([2, 1], now, expected_order=expected)
    assert error.value.code == 'concurrent_modified'
    assert not client.rpc_calls('planning_save_order_with_schedule')


@pytest.mark.parametrize('change', ['member', 'state', 'duration', 'task'])
def test_input_change_before_rpc_rejects_entire_order(client, change):
    now = at(24)
    seed_daily(client, 2, now)
    def interleave(event):
        if event['name'] != 'planning_save_order_with_schedule':
            return
        if change == 'member':
            seed_task(client, 3)
            seed_occ(client, 3, 3, now=now)
        elif change == 'state':
            client.rows['planning_occurrence'][0]['status'] = 'in_progress'
        elif change == 'duration':
            client.rows['planning_occurrence'][0]['planned_minutes'] = 120
        else:
            client.rows['planning_task'][0]['estimated_minutes'] = 120
    client.before_rpc = interleave
    before = [r['sort_order'] for r in client.rows['planning_occurrence']]
    with pytest.raises(service.common.PlanningError) as error:
        service.save_order([2, 1], now)
    assert error.value.code == 'concurrent_modified'
    assert [r['sort_order'] for r in client.rows['planning_occurrence'][:2]] == before
    assert not client.rows['planning_recompute_state'][0]['requested_at']


def test_same_identity_replays_after_other_inputs_change(client):
    now = at(24)
    seed_daily(client, 2, now)
    key = str(uuid4())
    saved = service.save_order([2, 1], now, request_key=key)
    token = client.rows['planning_recompute_state'][0]['request_token']
    client.rows['planning_occurrence'][0]['window_end_at'] = now.isoformat()
    assert service.save_order([2, 1], now, request_key=key) == {**saved, 'replayed': True}
    assert client.rows['planning_recompute_state'][0]['request_token'] == token


def test_disabled_recompute_still_checks_conflicts_without_wait_mark(client):
    now = at(24)
    seed_daily(client, 2, now)
    client.set_setting('planning.auto_recompute_enabled', False)
    saved = service.save_order([2, 1], now)
    assert saved['recompute_enabled'] is False
    assert saved['schedule_applied'] is False
    assert saved['updated'] == 0
    assert all(row['est_start'] is None for row in client.rows['planning_occurrence'])
    assert client.rows['planning_recompute_state'][0]['requested_at'] is None
    client.rows['planning_occurrence'][0]['window_end_at'] = now.isoformat()
    with pytest.raises(service.common.PlanningError):
        service.save_order([1, 2], now)


@pytest.mark.parametrize('constraint', ['only_latest', 'fixed_slot', 'hollow_envelope', 'hollow_order'])
def test_confirmation_reuses_full_scheduler_and_never_writes_conflicts(client, constraint):
    now = at(24)
    seed_task(client, 1)
    seed_task(client, 2)
    first = seed_occ(client, 1, 1, now=now, sort_order=0,
                     window_end_at=(now + timedelta(minutes=30)).isoformat())
    second = seed_occ(client, 2, 2, now=now, sort_order=1)
    order = [2, 1]
    if constraint == 'fixed_slot':
        second.update(is_fixed=True, fixed_source='manual', estimated_time_source='manual',
                      est_start=(now + timedelta(minutes=10)).isoformat(),
                      est_end=(now + timedelta(minutes=40)).isoformat())
    if constraint.startswith('hollow'):
        first.update(phase='start', phase_group='round', planned_wait_minutes=60,
                     window_end_at=(now + timedelta(minutes=120)).isoformat())
        client.rows['planning_task'][0].update(is_hollow=True, hollow_start_minutes=30,
            hollow_wait_minutes=60, hollow_end_minutes=30)
        seed_occ(client, 3, 1, now=now, phase='end', phase_group='round', sort_order=2,
                 planned_wait_minutes=60, window_end_at=first['window_end_at'])
        order = [2, 1, 3] if constraint == 'hollow_envelope' else [3, 1, 2]
    before = deepcopy(client.rows)
    with pytest.raises(service.common.PlanningError) as error:
        service.save_order(order, now)
    assert error.value.code == 'schedule_conflict'
    assert client.rows == before
    assert not client.rpc_calls('planning_save_order_with_schedule')


@pytest.mark.parametrize('status', ['pending', 'in_progress', 'partial'])
def test_earliest_only_and_started_facts_keep_existing_semantics(client, status):
    now = at(24)
    seed_task(client, 1)
    seed_task(client, 2)
    row = seed_occ(client, 1, 1, now=now, status=status, sort_order=0,
        window_start_at=(now + timedelta(minutes=60)).isoformat())
    seed_occ(client, 2, 2, now=now, sort_order=1)
    if status != 'pending':
        row.update(actual_start=now.isoformat(), est_start=now.isoformat(),
                   est_end=(now + timedelta(minutes=30)).isoformat(),
                   window_end_at=(now - timedelta(minutes=1)).isoformat())
    frozen = {key: row.get(key) for key in ('status', 'actual_start', 'est_start', 'est_end',
        'window_start_at', 'window_end_at')}
    assert service.save_order([2, 1], now)['saved'] == 2
    if status != 'pending':
        assert {key: row.get(key) for key in frozen} == frozen
    else:
        assert row['est_start'] == (now + timedelta(minutes=60)).isoformat()
        assert row['window_start_at'] == frozen['window_start_at']


def test_confirm_computes_once_and_consumes_prior_wait_without_new_wait(client):
    now = at(24)
    seed_daily(client, 2, now)
    service.request_recompute('edit', now)
    with mock.patch.object(service.scheduler, 'compute_schedule', wraps=service.scheduler.compute_schedule) as calculate:
        result = service.save_order([2, 1], now)
    assert calculate.call_count == 1
    assert result['updated'] == 2
    assert [row['id'] for row in result['estimates']] == [2, 1]
    assert client.rows['planning_occurrence'][1]['est_start'] == now.isoformat()
    assert client.rows['planning_occurrence'][0]['est_start'] == (now + timedelta(minutes=30)).isoformat()
    assert not service.get_recompute_state(now)['pending']
    # A subsequent edit still follows its configured delay and cannot be swallowed.
    service.request_recompute('edit', now)
    state = service.get_recompute_state(now)
    assert state['pending'] and state['wait_minutes'] == 30


def test_replay_does_not_reapply_estimates_after_a_later_order(client):
    now = at(24)
    seed_daily(client, 2, now)
    key = str(uuid4())
    first = service.save_order([2, 1], now, request_key=key)
    service.save_order([1, 2], now, request_key=str(uuid4()))
    before = deepcopy(client.rows)
    replay = service.save_order([2, 1], now, request_key=key)
    assert replay == {**first, 'replayed': True}
    assert client.rows == before


def test_unchanged_schedule_counts_zero_and_preserves_nominal_start(client):
    now = at(24)
    seed_daily(client, 2, now)
    service.save_order([2, 1], now)
    nominal = [row['nominal_start'] for row in client.rows['planning_occurrence']]
    result = service.save_order([2, 1], now)
    assert result['updated'] == 0 and result['schedule_applied'] is True
    assert [row['nominal_start'] for row in client.rows['planning_occurrence']] == nominal


def test_hollow_order_uses_same_round_estimate_payload(client):
    now = at(24)
    seed_task(client, 1, is_hollow=True, hollow_start_minutes=30,
              hollow_wait_minutes=60, hollow_end_minutes=30)
    for occ_id, phase in [(1, 'start'), (2, 'end')]:
        seed_occ(client, occ_id, 1, now=now, phase=phase, phase_group='pair',
                 planned_wait_minutes=60, sort_order=occ_id)
    result = service.save_order([1, 2], now)
    payload = client.rpc_calls('planning_save_order_with_schedule')[0]['params']
    assert len(payload['p_rounds']) == 1 and not payload['p_singles']
    assert result['updated'] == 2
    assert client.rows['planning_occurrence'][1]['est_start'] == (now + timedelta(minutes=90)).isoformat()
