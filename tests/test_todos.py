import importlib.util
import sys
import types
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch


if "dotenv" not in sys.modules and importlib.util.find_spec("dotenv") is None:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv

from gateway.todos import (
    TodoError,
    cancel_todo,
    complete_todo,
    create_todo,
    get_proactive_todo_context,
    list_todos,
    snooze_todo,
    validate_create_todo,
)


MODULE = "gateway.todos"
TODO_ID = "11111111-1111-4111-8111-111111111111"


class _Query:
    def __init__(self, client, operation="select", data=None):
        self.client = client
        self.operation = operation
        self.data = data
        self.filters = []
        self.row_limit = None

    def select(self, fields):
        self.client.selected_fields.append(fields)
        return self

    def insert(self, data):
        self.operation = "insert"
        self.data = dict(data)
        return self

    def update(self, data):
        self.operation = "update"
        self.data = dict(data)
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def limit(self, value):
        self.row_limit = value
        return self

    def _matches(self, row):
        return all(row.get(field) == value for field, value in self.filters)

    def execute(self):
        self.client.executed_filters.append(list(self.filters))
        if self.operation == "insert":
            row = {
                "id": TODO_ID,
                "created_at": "2026-08-02T00:00:00Z",
                "updated_at": "2026-08-02T00:00:00Z",
                "sort_order": 0,
                "completed_at": None,
                "parent_id": None,
                **self.data,
            }
            self.client.rows.append(row)
            self.client.inserted = dict(self.data)
            return SimpleNamespace(data=[row])

        matched = [row for row in self.client.rows if self._matches(row)]
        if self.row_limit is not None:
            matched = matched[: self.row_limit]
        if self.operation == "update":
            for row in matched:
                row.update(self.data)
            self.client.updated = dict(self.data)
        return SimpleNamespace(data=matched)


class _Client:
    def __init__(self, rows=None, *, rpc_rows=None, rpc_error=None):
        self.rows = list(rows or [])
        self.table_names = []
        self.selected_fields = []
        self.inserted = None
        self.updated = None
        self.executed_filters = []
        self.rpc_rows = rpc_rows
        self.rpc_error = rpc_error
        self.rpc_calls = []

    def table(self, name):
        self.table_names.append(name)
        if name != "todos":
            raise AssertionError(f"unexpected table access: {name}")
        return _Query(self)

    def rpc(self, name, payload):
        self.rpc_calls.append((name, dict(payload)))
        if self.rpc_error:
            raise self.rpc_error
        if self.rpc_rows is None:
            raise RuntimeError("rpc unavailable")
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=self.rpc_rows))


def _row(**overrides):
    row = {
        "id": TODO_ID,
        "user_name": "叶子",
        "ai_name": "苏02",
        "content": "整理今天的代码进度",
        "todo_type": "user",
        "status": "regular",
        "estimated_time": None,
        "scheduled_start": "2026-08-02T09:00:00+08:00",
        "scheduled_end": None,
        "sort_order": 0,
        "is_completed": False,
        "completed_at": None,
        "is_private": False,
        "is_hidden": False,
        "parent_id": None,
        "is_start_marker": False,
        "is_end_marker": False,
        "note": None,
        "created_at": "2026-08-01T12:00:00Z",
        "updated_at": "2026-08-01T12:00:00Z",
    }
    row.update(overrides)
    return row


def _create_payload(**overrides):
    payload = {
        "user_name": "叶子",
        "ai_name": "苏02",
        "content": "整理今天的代码进度",
        "todo_type": "user",
        "scheduled_start": "2026-08-02T09:00:00+08:00",
    }
    payload.update(overrides)
    return payload


class ValidationTests(unittest.TestCase):
    def test_normalizes_time_and_defaults(self):
        result = validate_create_todo(_create_payload())
        self.assertEqual(result["scheduled_start"], "2026-08-02T01:00:00Z")
        self.assertEqual(result["status"], "regular")
        self.assertFalse(result["is_private"])

    def test_requires_timezone_and_rejects_unknown_fields(self):
        with self.assertRaises(TodoError) as timezone_error:
            validate_create_todo(_create_payload(scheduled_start="2026-08-02T09:00:00"))
        self.assertEqual(timezone_error.exception.code, "invalid_payload")

        with self.assertRaises(TodoError) as unknown:
            validate_create_todo(_create_payload(service_role_key="secret"))
        self.assertEqual(unknown.exception.code, "invalid_payload")

    def test_end_must_not_precede_start(self):
        with self.assertRaises(TodoError):
            validate_create_todo(_create_payload(scheduled_end="2026-08-02T08:00:00+08:00"))


class PersistenceTests(unittest.TestCase):
    def test_retry_returns_existing_scoped_todo(self):
        client = _Client([_row()])
        with (
            patch(f"{MODULE}._server_access_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
        ):
            result = create_todo(_create_payload())

        self.assertFalse(result["created"])
        self.assertTrue(result["deduplicated"])
        self.assertIsNone(client.inserted)
        self.assertEqual(set(client.table_names), {"todos"})

    def test_creates_todo_without_touching_chat_history(self):
        client = _Client()
        with (
            patch(f"{MODULE}._server_access_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
        ):
            result = create_todo(_create_payload())

        self.assertTrue(result["created"])
        self.assertEqual(client.inserted["user_name"], "叶子")
        self.assertEqual(client.inserted["ai_name"], "苏02")
        self.assertFalse(client.inserted["is_hidden"])
        self.assertNotIn("chat_messages", client.table_names)

    def test_today_list_includes_overdue_and_unscheduled_but_not_future_or_markers(self):
        rows = [
            _row(),
            _row(id="22222222-2222-4222-8222-222222222222", scheduled_start=None),
            _row(id="33333333-3333-4333-8333-333333333333", scheduled_start="2026-08-03T09:00:00+08:00"),
            _row(id="44444444-4444-4444-8444-444444444444", status="hollow"),
            _row(id="55555555-5555-4555-8555-555555555555", is_start_marker=True),
            _row(id="66666666-6666-4666-8666-666666666666", ai_name="另一个角色"),
        ]
        client = _Client(rows)
        with (
            patch(f"{MODULE}._server_access_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
        ):
            result = list_todos(
                {
                    "user_name": "叶子",
                    "ai_name": "苏02",
                    "scope": "today",
                    "timezone_offset_minutes": 480,
                },
                now=datetime(2026, 8, 2, 4, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(result["count"], 2)
        self.assertEqual(
            {item["id"] for item in result["todos"]},
            {TODO_ID, "22222222-2222-4222-8222-222222222222"},
        )

    def test_mutations_are_scoped_and_cancellation_is_soft(self):
        client = _Client([_row()])
        scope = {"user_name": "叶子", "ai_name": "苏02"}
        with (
            patch(f"{MODULE}._server_access_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
        ):
            completed = complete_todo(TODO_ID, scope)
            snoozed = snooze_todo(TODO_ID, {
                **scope,
                "scheduled_start": "2026-08-03T10:00:00+08:00",
            })
            cancelled = cancel_todo(TODO_ID, scope)

        self.assertTrue(completed["is_completed"])
        self.assertEqual(snoozed["scheduled_start"], "2026-08-03T02:00:00Z")
        self.assertTrue(cancelled["is_hidden"])
        self.assertEqual(len(client.rows), 1)

    def test_elevated_server_key_is_required(self):
        with (
            patch(f"{MODULE}._server_access_allowed", return_value=False),
            patch(f"{MODULE}.get_client") as get_client,
        ):
            with self.assertRaises(TodoError) as raised:
                create_todo(_create_payload())

        self.assertEqual(raised.exception.code, "database_permissions_unavailable")
        get_client.assert_not_called()


class ProactiveContextTests(unittest.TestCase):
    def test_reads_single_owner_todos_without_identity_configuration(self):
        rows = [
            _row(content="今天九点的任务"),
            _row(
                id="22222222-2222-4222-8222-222222222222",
                user_name="不需要配置的名字",
                ai_name="不需要配置的角色",
                content="没有排期的任务",
                scheduled_start=None,
            ),
            _row(
                id="33333333-3333-4333-8333-333333333333",
                content="明天的任务",
                scheduled_start="2026-08-03T09:00:00+08:00",
            ),
        ]
        client = _Client(rows)
        with (
            patch(f"{MODULE}._server_access_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
        ):
            context = get_proactive_todo_context(
                now=datetime(2026, 8, 2, 4, 0, tzinfo=timezone.utc),
            )

        self.assertIn("今天九点的任务", context)
        self.assertIn("没有排期的任务", context)
        self.assertNotIn("明天的任务", context)
        filter_fields = {
            field
            for query_filters in client.executed_filters
            for field, _value in query_filters
        }
        self.assertNotIn("user_name", filter_fields)
        self.assertNotIn("ai_name", filter_fields)

    def test_atomic_claim_uses_three_hour_cooldown_without_count_limit(self):
        client = _Client(rpc_rows=[{
            "todo_id": TODO_ID,
            "content": "三小时后才能再次进入上下文",
            "scheduled_start": "2026-08-02T09:00:00+08:00",
        }])
        with (
            patch(f"{MODULE}._server_access_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
        ):
            context = get_proactive_todo_context(
                now=datetime(2026, 8, 2, 4, 0, tzinfo=timezone.utc),
            )

        self.assertIn("三小时后才能再次进入上下文", context)
        self.assertEqual(client.table_names, [])
        rpc_name, payload = client.rpc_calls[0]
        self.assertEqual(rpc_name, "claim_proactive_todos")
        self.assertEqual(payload["p_cooldown_minutes"], 180)
        self.assertNotIn("p_daily_limit", payload)
        self.assertNotIn("p_reminder_count", payload)

    def test_empty_successful_claim_does_not_fall_back_or_repeat_todos(self):
        client = _Client([_row()], rpc_rows=[])
        with (
            patch(f"{MODULE}._server_access_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
        ):
            context = get_proactive_todo_context(
                now=datetime(2026, 8, 2, 4, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(context, "")
        self.assertEqual(client.table_names, [])

    def test_database_failure_does_not_block_proactive_reply(self):
        with patch(f"{MODULE}._client", side_effect=RuntimeError("offline")):
            self.assertEqual(get_proactive_todo_context(), "")


if __name__ == "__main__":
    unittest.main()

