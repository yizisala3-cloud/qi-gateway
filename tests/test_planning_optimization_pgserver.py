"""10.16 acceptance: actual services, complete migrations, isolated PostgreSQL."""
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from uuid import uuid4

from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway import db, planning_detail_edit as edit, planning_generation as generation
from gateway import planning_runtime as runtime, planning_recompute as recompute, planning_schedule as schedule
from gateway import planning_tasks as tasks
from gateway.config import cfg
from gateway.planning_api import planning_api_routes
from tests import test_planning_phase1r_pgserver_integration as chain
from tests import test_planning_goals_pgserver as goalbase
from tests.support.planning_goal_sql import GoalSqlClient

NOW = datetime.fromisoformat('2026-10-16T07:00:00+08:00')


class OptimizationPostgresTests(unittest.TestCase):
    setUpClass = classmethod(chain.PlanningInvariantsOnPostgresTests.setUpClass.__func__)
    tearDownClass = classmethod(chain.PlanningInvariantsOnPostgresTests.tearDownClass.__func__)
    _query = classmethod(chain.PlanningInvariantsOnPostgresTests._query.__func__)

    def setUp(self):
        self.conn.execute("set timezone='Asia/Shanghai'")
        self.conn.execute('truncate planning_goal,planning_goal_round_fact,planning_task,planning_occurrence,planning_detail_edit_request,planning_reorder_request,planning_task_completion_fact restart identity cascade')
        self.conn.execute('update planning_recompute_state set requested_at=null,request_token=null,reason=null')
        self.conn.execute("delete from app_settings where key in ('planning.auto_recompute_enabled','planning.refresh_boundary_state')")
        self.helper = goalbase.GoalPostgresTests()
        self.helper.conn = self.conn
        self.client = GoalSqlClient(self.conn)
        self.helper.client = self.client
        for patch in (mock.patch.object(runtime, 'get_client', return_value=self.client),
                      mock.patch.object(db, 'get_client', return_value=self.client),
                      mock.patch.object(runtime, '_now', return_value=NOW),
                      mock.patch.object(tasks, '_generate_due_quietly'),
                      mock.patch.object(cfg, 'GATEWAY_TOKEN', 'optimization-local')):
            patch.start()
            self.addCleanup(patch.stop)
        self.task_id = self.helper.task()
        self.occ_id = self.helper.occurrence(self.task_id)[0]
        self.conn.execute('update planning_occurrence set planned_minutes=30 where id=%s', (self.occ_id,))

    def row(self, oid=None):
        return self.conn.execute('select to_jsonb(o) from planning_occurrence o where id=%s', (oid or self.occ_id,)).fetchone()[0]

    def task(self):
        return self.conn.execute('select to_jsonb(t) from planning_task t where id=%s', (self.task_id,)).fetchone()[0]

    def payload(self, changes=None, parked=None, oid=None):
        oid = oid or self.occ_id
        ctx = edit.get_context(self.task_id, oid, NOW)
        return {'occurrence_id': oid, 'token': ctx['token'],
                **({'parked': parked} if parked is not None else {'changes': changes})}

    def save(self, payload, key=None):
        return edit.command(self.task_id, payload, idempotency_key=key or str(uuid4()), now=NOW)

    def test_name_duration_current_future_history_and_real_schedule(self):
        old = self.helper.occurrence(self.task_id, '2026-10-15')[0]
        self.helper.status(old, 'completed')
        before = self.row(old)
        self.conn.execute("update planning_occurrence set est_start='2026-10-16T08:00:00+08',"
                          "est_end='2026-10-16T08:30:00+08',estimated_time_source='automatic' where id=%s", (self.occ_id,))
        result = self.save(self.payload({'content': '新名字', 'estimated_minutes': 60, 'note': '新备注'}))
        self.assertEqual(result['scope']['time'], 'current_and_future')
        self.assertEqual(self.task()['estimated_minutes'], 60)
        row = self.row()
        self.assertEqual((row['display_content'], row['planned_minutes']), ('新名字', 60))
        self.assertIsNone(row['est_start'])
        start, end = schedule.compute_schedule([row], {self.task_id: self.task()}, NOW).placed[self.occ_id]
        self.assertEqual(end-start, timedelta(minutes=60))
        self.assertEqual(self.row(old), before)

    def test_current_time_changes_even_when_future_value_already_matches(self):
        self.conn.execute('update planning_task set estimated_minutes=60 where id=%s', (self.task_id,))
        self.save(self.payload({'estimated_minutes': 60}))
        self.assertEqual(self.row()['planned_minutes'], 60)

    def test_general_save_reminders_and_note_share_receipt_and_rollback(self):
        before_task, before_row = self.task(), self.row()
        with self.assertRaises(edit.common.PlanningError):
            self.save(self.payload({'note': '不得部分保存', 'alarm_start': True,
                                    'timer_minutes': '无效时长'}))
        self.assertEqual(self.task(), before_task)
        self.assertEqual(self.row(), before_row)
        payload = self.payload({'note': '通用保存', 'alarm_start': True,
                                'alarm_end': True, 'timer_minutes': '1h30m'})
        key = str(uuid4())
        result = self.save(payload, key)
        self.assertEqual(result['task']['timer_minutes'], 90)
        self.assertTrue(result['task']['alarm_start'])
        self.assertTrue(result['task']['alarm_end'])
        self.assertEqual(self.row()['note'], '通用保存')
        task, row = self.task(), self.row()
        self.assertTrue(self.save(payload, key)['replayed'])
        self.assertEqual(self.task(), task)
        self.assertEqual(self.row(), row)
        self.assertEqual(self.conn.execute('select count(*) from planning_detail_edit_request').fetchone()[0], 1)

    def test_closed_once_reminder_settings_preserve_historical_facts(self):
        self.conn.execute("update planning_task set task_type='once',refresh_mode='none',target_date='2026-10-16' where id=%s", (self.task_id,))
        self.helper.status(self.occ_id, 'completed')
        before = self.row()
        self.save(self.payload({'alarm_start': True, 'timer_minutes': '10m'}))
        self.assertEqual(self.task()['timer_minutes'], 10)
        self.assertEqual(self.row(), before)
        task = self.task()
        with self.assertRaises(edit.common.PlanningError):
            self.save(self.payload({'note': '禁止改历史', 'alarm_end': True}))
        self.assertEqual(self.task(), task)
        self.assertEqual(self.row(), before)

    def test_started_and_partial_names_notes_current_but_time_only_future(self):
        for state in ('in_progress', 'partial'):
            with self.subTest(state=state):
                self.conn.execute('update planning_occurrence set status=%s,actual_start=%s where id=%s', (state,NOW,self.occ_id))
                before = self.row()
                result = self.save(self.payload({'content': state, 'note': state, 'estimated_minutes': 90,
                    'window_start_tod': '08:00', 'window_end_tod': '12:00'}))
                row = self.row()
                self.assertEqual((row['display_content'], row['note']), (state,state))
                self.assertEqual(result['scope']['time'], 'future')
                for field in ('planned_minutes','window_start_at','window_end_at','actual_start'):
                    self.assertEqual(row[field], before[field])

    def test_closed_record_all_snapshots_and_goal_facts_preserved(self):
        goal = self.helper.create()
        self.helper.link(goal, self.task_id)
        self.helper.status(self.occ_id, 'completed')
        before = self.row()
        self.assertEqual(self.helper.get(goal)['executed_count'], 1)
        result = self.save(self.payload({'content': '未来名字','note': '未来备注','estimated_minutes': 80}))
        self.assertEqual(result['scope'], {'text':'future','time':'future','name':'future'})
        self.assertEqual(self.row(), before)
        self.assertEqual(self.helper.get(goal)['executed_count'], 1)

    def test_window_and_duration_validate_before_both_writes(self):
        before_task, before_row = self.task(), self.row()
        with self.assertRaises(edit.common.PlanningError):
            self.save(self.payload({'content':'不应保存','estimated_minutes':120,
                'window_start_tod':'08:00','window_end_tod':'08:30'}))
        self.assertEqual(self.task(), before_task)
        self.assertEqual(self.row(), before_row)
        self.save(self.payload({'estimated_minutes':60,'window_start_tod':'08:00','window_end_tod':'10:00'}))
        self.assertEqual(self.row()['window_start_at'], '2026-10-16T08:00:00+08:00')
        self.assertEqual(self.row()['window_end_at'], '2026-10-16T10:00:00+08:00')

    def test_zero_slack_window_preserves_complete_owned_time_tuple(self):
        identity={k:self.row()[k] for k in ('id','task_id','round_key','schedule_date')}
        self.save(self.payload({'window_start_tod':'08:00','window_end_tod':'08:30'}))
        row=self.row()
        self.assertEqual({k:row[k] for k in identity},identity)
        self.assertEqual(row['est_start'],'2026-10-16T08:00:00+08:00')
        self.assertEqual(row['est_end'],'2026-10-16T08:30:00+08:00')
        self.assertEqual(row['nominal_start'],row['est_start'])
        self.assertTrue(row['is_fixed'])
        self.assertFalse(row['schedule_managed'])

    def test_zero_slack_hollow_window_anchors_both_stages_atomically(self):
        self.task_id=self.helper.task(True)
        self.occ_id,end=self.helper.occurrence(self.task_id,hollow=True)
        self.save(self.payload({'window_start_tod':'08:00','window_end_tod':'08:20'}))
        first,last=self.row(),self.row(end)
        self.assertEqual(first['est_end'],'2026-10-16T08:05:00+08:00')
        self.assertEqual(last['est_start'],'2026-10-16T08:15:00+08:00')
        self.assertEqual(last['est_end'],'2026-10-16T08:20:00+08:00')
        self.assertTrue(first['is_fixed'] and last['is_fixed'])
        self.assertEqual(first['window_start_at'],last['window_start_at'])
        self.assertEqual(first['window_end_at'],last['window_end_at'])

    def test_late_database_failure_rolls_back_template_round_and_receipt(self):
        before_task, before_row = self.task(), self.row()
        self.conn.execute("create function opt_fail() returns trigger language plpgsql as $$ begin raise exception 'late injected'; end $$")
        self.conn.execute(f'create trigger opt_fail before update on planning_occurrence for each row when (new.id={self.occ_id}) execute function opt_fail()')
        try:
            with self.assertRaises(chain.psycopg.Error):
                self.save(self.payload({'content':'回滚','estimated_minutes':60}))
            self.assertEqual(self.task(), before_task)
            self.assertEqual(self.row(), before_row)
        finally:
            self.conn.execute('drop trigger opt_fail on planning_occurrence; drop function opt_fail()')

    def test_context_conflict_preserves_new_execution_and_never_partially_saves(self):
        payload = self.payload({'content':'陈旧草稿','estimated_minutes':60})
        self.conn.execute("update planning_occurrence set status='in_progress',actual_start=%s where id=%s", (NOW,self.occ_id))
        with self.assertRaises(edit.common.ConcurrencyRejected): self.save(payload)
        self.assertEqual(self.task()['content'], '背单词')
        self.assertEqual(self.row()['status'], 'in_progress')

    def test_cross_connection_change_between_validation_and_commit_rejected(self):
        payload = self.payload({'content':'陈旧保存','estimated_minutes':60})
        real_rpc = self.client.rpc
        def racing_rpc(name, params):
            if name == 'planning_commit_detail_edit':
                with chain.psycopg.connect(self.server.get_uri(), autocommit=True) as conn:
                    conn.execute("update planning_occurrence set status='partial',partial_at=%s where id=%s", (NOW,self.occ_id))
            return real_rpc(name,params)
        with mock.patch.object(self.client,'rpc',side_effect=racing_rpc):
            with self.assertRaises(edit.common.ConcurrencyRejected): self.save(payload)
        self.assertEqual(self.task()['content'], '背单词')
        self.assertEqual(self.row()['status'], 'partial')

    def test_unknown_result_same_key_replays_after_later_edit(self):
        key = str(uuid4())
        payload = self.payload({'content':'第一次'})
        self.save(payload,key)
        self.save(self.payload({'content':'较新的修改'}))
        self.assertTrue(self.save(payload,key)['replayed'])
        self.assertEqual(self.row()['display_content'], '较新的修改')
        with self.assertRaises(edit.common.PlanningError): self.save({**payload,'changes':{'content':'换内容'}},key)

    def test_park_excludes_old_automatic_and_fixed_slots_conflicts_and_preserves_facts(self):
        self.conn.execute("update planning_occurrence set est_start='2026-10-16T08:00+08',est_end='2026-10-16T09:00+08',"
            "estimated_time_source='manual',fixed_source='manual',is_fixed=true,schedule_managed=false,"
            "window_end_at='2026-10-16T07:10+08' where id=%s", (self.occ_id,))
        self.save(self.payload(parked=True))
        other_task = self.helper.task()
        other_id = self.helper.occurrence(other_task)[0]
        other = self.row(other_id)
        result = schedule.compute_schedule([self.row(),other],{self.task_id:self.task(),other_task:{'estimated_minutes':30}},NOW)
        self.assertNotIn(self.occ_id, result.placed)
        self.assertEqual(result.placed[other_id][0],NOW)
        self.assertEqual(result.conflicts,[])
        self.assertEqual(self.row()['window_end_at'],'2026-10-16T07:10:00+08:00')

    def test_restore_does_not_reuse_old_automatic_slot_and_new_round_not_parked(self):
        self.conn.execute("update planning_occurrence set est_start='2026-10-16T06:00+08',est_end='2026-10-16T06:30+08',estimated_time_source='automatic' where id=%s", (self.occ_id,))
        self.save(self.payload(parked=True))
        self.assertTrue(self.row()['is_parked'])
        self.save(self.payload(parked=False))
        row = self.row()
        self.assertFalse(row['is_parked'])
        self.assertIsNone(row['est_start'])
        self.assertEqual(schedule.compute_schedule([row],{self.task_id:self.task()},NOW).placed[self.occ_id][0],NOW)
        self.save(self.payload(parked=True))
        self.conn.execute("update planning_occurrence set display_cycle_date='2026-10-17',display_reason='carryover' where id=%s",(self.occ_id,))
        self.assertTrue(self.row()['is_parked'])
        self.assertFalse(self.row(self.helper.occurrence(self.task_id,'2026-10-17')[0])['is_parked'])

    def test_parking_deadline_still_times_out_and_cannot_restore(self):
        self.conn.execute("update planning_occurrence set window_end_at='2026-10-16T08:00+08' where id=%s",(self.occ_id,))
        self.save(self.payload(parked=True))
        generation.sweep_timeouts(NOW+timedelta(hours=2))
        self.assertEqual(self.row()['status'],'timeout')
        with self.assertRaises(edit.common.PlanningError): self.save(self.payload(parked=False))

    def test_parked_daily_round_still_closes_at_original_cycle_boundary(self):
        self.save(self.payload(parked=True))
        tomorrow=NOW+timedelta(days=1)
        expired=generation._expire_daily_rounds(self.client,self.task(),tomorrow.date(),tomorrow,
            NOW.replace(hour=0,minute=0,second=0).time(),None,frozenset())
        self.assertEqual(expired,1)
        row=self.row()
        self.assertEqual(row['status'],'timeout')
        self.assertEqual(row['closed_at'],'2026-10-17T00:00:00+08:00')
        self.assertIsNone(row['handled_at'])
        self.assertIsNone(row['actual_start'])

    def test_hollow_round_edits_and_parking_are_atomic_and_fact_gated(self):
        self.task_id=self.helper.task(True)
        self.occ_id,end=self.helper.occurrence(self.task_id,hollow=True)
        self.save(self.payload({'content':'整轮新名','hollow_start_minutes':10,'hollow_end_minutes':15}))
        self.assertEqual((self.row()['planned_minutes'],self.row(end)['planned_minutes']),(10,15))
        self.save(self.payload({'hollow_start_content':'新的开始阶段'}))
        self.assertEqual(self.row()['display_content'],'新的开始阶段·开始')
        self.assertEqual(self.task()['hollow_start_content'],'新的开始阶段')
        self.save(self.payload(parked=True))
        self.assertTrue(self.row()['is_parked'] and self.row(end)['is_parked'])
        self.save(self.payload(parked=False))
        self.helper.status(self.occ_id,'completed')
        with self.assertRaises(edit.common.PlanningError): self.save(self.payload(parked=True,oid=end))

    def test_once_date_locked_but_selected_window_and_duration_editable(self):
        self.conn.execute("update planning_task set task_type='once',refresh_mode='none',target_date='2026-10-16' where id=%s",(self.task_id,))
        self.save(self.payload({'estimated_minutes':45,'window_start_tod':'08:00','window_end_tod':'10:00'}))
        self.assertEqual(self.row()['planned_minutes'],45)
        self.assertEqual(self.task()['target_date'],'2026-10-16')
        with self.assertRaises(edit.common.PlanningError): self.save(self.payload({'target_date':'2026-10-17'}))

    def test_resident_once_cannot_acquire_window(self):
        self.conn.execute("update planning_task set task_type='once',refresh_mode='none' where id=%s",(self.task_id,))
        with self.assertRaises(edit.common.PlanningError): self.save(self.payload({'window_end_tod':'12:00'}))
        self.assertIsNone(self.row()['window_end_at'])

    def test_real_order_confirmation_and_recompute_respect_parking_with_auto_on_off(self):
        second_task=self.helper.task()
        second=self.helper.occurrence(second_task)[0]
        self.save(self.payload(parked=True))
        for enabled in (True,False):
            self.conn.execute("insert into app_settings(key,value) values('planning.auto_recompute_enabled',%s::jsonb) on conflict(key) do update set value=excluded.value",(json.dumps(enabled),))
            result=recompute.save_order([second,self.occ_id],NOW,request_key=str(uuid4()))
            self.assertEqual(result['recompute_enabled'],enabled)
            self.assertEqual(result['schedule_applied'],enabled)
            self.assertIsNone(self.row()['est_start'])
            self.assertTrue(self.row()['is_parked'])
            with self.assertRaises(edit.common.PlanningError): recompute.save_order([self.occ_id,second],NOW)
        result=recompute.recompute_today(NOW)
        self.assertEqual(result['conflicts'],[])
        self.assertTrue(self.row()['is_parked'])

    def test_fixed_anchor_duration_and_deferred_validation(self):
        self.conn.execute("update planning_occurrence set status='deferred',est_start='2026-10-16T08:00+08',est_end='2026-10-16T08:30+08',"
            "estimated_time_source='manual',fixed_source='manual',is_fixed=true,schedule_managed=false,window_end_at='2026-10-16T09:00+08' where id=%s",(self.occ_id,))
        self.save(self.payload({'estimated_minutes':45}))
        self.assertEqual(self.row()['est_start'],'2026-10-16T08:00:00+08:00')
        self.assertEqual(self.row()['est_end'],'2026-10-16T08:45:00+08:00')
        before=self.row()
        with self.assertRaises(edit.common.PlanningError): self.save(self.payload({'estimated_minutes':120}))
        self.assertEqual(self.row(),before)

    def test_deferred_end_only_window_keeps_its_frozen_future_date(self):
        self.conn.execute("update planning_occurrence set status='deferred',window_end_at='2026-10-17T01:00+08' where id=%s",(self.occ_id,))
        self.save(self.payload({'window_end_tod':'02:00'}))
        self.assertEqual(self.row()['window_end_at'],'2026-10-17T02:00:00+08:00')
        self.assertEqual(self.row()['schedule_date'],'2026-10-16')

    def test_old_recompute_snapshot_cannot_overwrite_a_parked_row(self):
        row=self.row()
        expected=recompute._recompute_expected_snapshot(row)
        self.save(self.payload(parked=True))
        patch={'id':self.occ_id,**edit.common._estimate_patch(NOW,NOW+timedelta(minutes=30),source='automatic'),'updated_at':NOW.isoformat()}
        with self.assertRaises(chain.psycopg.Error) as caught:
            self.client.rpc('planning_apply_recompute_batch',{'p_expected':[expected],'p_singles':[patch],'p_rounds':[]}).execute()
        self.assertEqual(caught.exception.sqlstate,'PC001')
        self.assertIsNone(self.row()['est_start'])

    def test_once_task_context_anchors_its_sole_round_and_started_time_cannot_be_changed(self):
        self.conn.execute("update planning_task set task_type='once',refresh_mode='none' where id=%s",(self.task_id,))
        context=edit.get_context(self.task_id,now=NOW)
        self.assertEqual(context['occurrence_id'],self.occ_id)
        self.conn.execute("update planning_occurrence set status='in_progress',actual_start=%s where id=%s",(NOW,self.occ_id))
        with self.assertRaises(edit.common.PlanningError): self.save(self.payload({'estimated_minutes':60}))
        self.assertEqual(self.row()['planned_minutes'],30)

    def test_schema_replay_and_permissions(self):
        self.save(self.payload(parked=True))
        for name in ('20261017010000_planning_detail_edit_and_parking.sql','20261017020000_planning_parked_recompute_guard.sql','20261017030000_planning_detail_once_absence_guard.sql','20261017040000_planning_detail_general_save.sql'):
            self.conn.execute((chain.MIGRATIONS_DIR/name).read_text(encoding='utf-8'))
        self.assertTrue(self.row()['is_parked'])
        for role in ('anon','authenticated'):
            self.assertFalse(self.conn.execute("select has_table_privilege(%s,'planning_detail_edit_request','select')",(role,)).fetchone()[0])
            self.assertFalse(self.conn.execute("select has_function_privilege(%s,'planning_commit_detail_edit(jsonb,jsonb,jsonb,jsonb,uuid,jsonb,timestamptz)','execute')",(role,)).fetchone()[0])

    def test_http_auth_and_mandatory_type(self):
        with TestClient(Starlette(routes=list(planning_api_routes))) as http:
            path=f'/admin/api/planning/tasks/{self.task_id}/detail-edit'
            self.assertEqual(http.get(path).status_code,401)
            auth={'Authorization':'Bearer optimization-local'}
            self.assertEqual(http.get(path,headers=auth).status_code,200)
            self.assertEqual(http.post('/admin/api/planning/tasks',headers=auth,json={'content':'无类型','estimated_minutes':30}).status_code,400)
            self.assertEqual(http.post(path,headers=auth,json=self.payload({'content':'没有键'})).status_code,400)
