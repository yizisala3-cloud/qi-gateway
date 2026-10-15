"""Task-note validation, HTTP boundaries, snapshots and legacy request identity."""
import hashlib
import json
import unittest
import re
from pathlib import Path
from unittest import mock

from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway import planning_common as common, planning_tasks as tasks
from gateway import planning_serialization as serialization, planning_generation as generation
from gateway import planning_runtime as runtime
from gateway.config import cfg
from gateway.planning_api import planning_api_routes
from tests.support.planning_context import cst
from tests.support.planning_db import CoreClient
from tests.support.planning_fixtures import seed_task

NOW = cst(2026, 10, 14, 12)


class TaskNoteValidationTests(unittest.TestCase):
    def test_limit_matches_frontend_and_database(self):
        root = Path(__file__).resolve().parents[1]
        frontend = (root / 'admin/js/lib/planning_notes.js').read_text(encoding='utf-8')
        sql = (root / 'supabase/migrations/20261014010000_planning_task_notes.sql').read_text(encoding='utf-8')
        self.assertEqual(int(re.search(r'TASK_NOTE_LIMIT = (\d+)', frontend).group(1)), common.MAX_TASK_NOTE_LENGTH)
        self.assertEqual([int(value) for value in re.findall(r'char_length\(note\) <= (\d+)', sql)],
                         [common.MAX_TASK_NOTE_LENGTH] * 2)

    def test_note_is_optional_and_empty_clears_it(self):
        for value in (None, '', '  \n '):
            self.assertIsNone(tasks.validate_task_payload({'note': value}, partial=True)['note'])
        self.assertNotIn('note', tasks.validate_task_payload({'content': '原内容'}, partial=True))

    def test_limit_unicode_and_line_endings(self):
        for value in ('字' * 60, '🙂' * 60):
            self.assertEqual(tasks.validate_task_payload({'note': value}, partial=True)['note'], value)
        self.assertEqual(tasks.validate_task_payload({'note': ' 第一行\r\n第二行 '}, partial=True)['note'], '第一行\n第二行')
        for value in ('字' * 61, '🙂' * 61, 123, [], {}):
            with self.subTest(value=type(value).__name__), self.assertRaises(common.PlanningError):
                tasks.validate_task_payload({'note': value}, partial=True)

    def test_new_creation_notes_participate_in_idempotency(self):
        base = tasks.validate_task_payload({'content': '阅读', 'task_type': 'daily', 'estimated_minutes': 20})
        first = tasks._creation_request_content({**base, 'note': '第一条'})
        second = tasks._creation_request_content({**base, 'note': '第二条'})
        self.assertNotEqual(tasks._creation_content_digest(first), tasks._creation_content_digest(second))

    def test_empty_notes_keep_legacy_tombstone_digest(self):
        payload = tasks.validate_task_payload({'content': '阅读', 'task_type': 'daily', 'estimated_minutes': 20})
        old = tasks._creation_request_content(payload)
        self.assertNotIn('note', old)
        expected = hashlib.sha256(json.dumps(old, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(tasks._creation_content_digest(old), expected)
        self.assertEqual(tasks._creation_request_content({**payload, 'note': None}), old)

    def test_generation_carries_note_but_history_never_reads_current_template(self):
        client = CoreClient()
        seed_task(client, 1)
        task = client.rows['planning_task'][0]
        task['note'] = '今天的备注'
        self.assertEqual(generation._generation_snapshots(task, NOW.date(), None)['note'], '今天的备注')
        for status in ('pending', 'completed', 'timeout'):
            occ = {'id': 1, 'task_id': 1, 'for_date': NOW.date().isoformat(),
                   'round_key': 'note-history', 'status': status}
            occ['note'] = None
            self.assertIsNone(serialization.serialize_occurrence(occ, task, NOW)['note'])
            occ['note'] = '当时的备注'
            self.assertEqual(serialization.serialize_occurrence(occ, task, NOW)['note'], '当时的备注')


class TaskNoteHttpTests(unittest.TestCase):
    def setUp(self):
        self.db = CoreClient()
        seed_task(self.db, 1)
        self.db.rows['planning_task'][0]['note'] = '旧备注'
        for patch in (mock.patch.object(cfg, 'GATEWAY_TOKEN', 'note-test-token'),
                      mock.patch.object(runtime, 'get_client', lambda: self.db)):
            patch.start()
            self.addCleanup(patch.stop)
        self.http = TestClient(Starlette(routes=list(planning_api_routes)))
        self.addCleanup(self.http.close)
        self.auth = {'Authorization': 'Bearer note-test-token'}

    def test_note_edit_requires_gateway_auth(self):
        response = self.http.patch('/admin/api/planning/tasks/1', json={'note': '新备注'})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.db.rows['planning_task'][0]['note'], '旧备注')

    def test_note_only_update_does_not_touch_schedule_or_call_generation(self):
        before = dict(self.db.rows['planning_task'][0])
        with mock.patch.object(tasks, '_generate_due_quietly') as generate:
            response = self.http.patch('/admin/api/planning/tasks/1', headers=self.auth, json={'note': '新备注'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['note'], '新备注')
        generate.assert_not_called()
        after = self.db.rows['planning_task'][0]
        self.assertEqual({k: v for k, v in after.items() if k not in ('note', 'updated_at')},
                         {k: v for k, v in before.items() if k not in ('note', 'updated_at')})
        self.assertEqual(self.http.get('/admin/api/planning/tasks/1', headers=self.auth).json()['note'], '新备注')

    def test_invalid_note_is_rejected_before_any_write(self):
        response = self.http.patch('/admin/api/planning/tasks/1', headers=self.auth, json={'note': '字' * 61})
        self.assertEqual(response.status_code, 400)
        self.assertIn('60', response.json()['error'])
        self.assertEqual(self.db.rows['planning_task'][0]['note'], '旧备注')

    def test_missing_task_and_clearing_note(self):
        self.assertEqual(self.http.patch('/admin/api/planning/tasks/999', headers=self.auth,
                                        json={'note': '备注'}).status_code, 404)
        response = self.http.patch('/admin/api/planning/tasks/1', headers=self.auth, json={'note': ''})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['note'])

    def test_create_with_note_persists_and_returns_it(self):
        with mock.patch('gateway.db.load_app_setting', return_value=None), \
                mock.patch.object(tasks, '_generate_created_task_quietly'):
            response = self.http.post('/admin/api/planning/tasks', headers=self.auth,
                                      json={'content': '新建待办', 'task_type': 'daily',
                                            'estimated_minutes': 20, 'note': '创建时的备注'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['note'], '创建时的备注')
        self.assertEqual(self.db.rows['planning_task'][-1]['note'], '创建时的备注')
