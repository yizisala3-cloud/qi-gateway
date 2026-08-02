import importlib.util
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch


if "dotenv" not in sys.modules and importlib.util.find_spec("dotenv") is None:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv

from gateway.memory_heat import run_heat_decay


MODULE = "gateway.memory_heat"


class _Client:
    def __init__(self, data=None, error=None):
        self.data = data
        self.error = error
        self.rpc_name = None
        self.rpc_payload = None

    def rpc(self, name, payload):
        self.rpc_name = name
        self.rpc_payload = payload
        return self

    def execute(self):
        if self.error:
            raise self.error
        return SimpleNamespace(data=self.data)


class HeatDecayTests(unittest.TestCase):
    def test_unavailable_database_skips_without_writes(self):
        with patch(f"{MODULE}.get_client", return_value=None):
            result = run_heat_decay()

        self.assertEqual(result, {
            "status": "skipped",
            "reason": "supabase_unavailable",
        })

    def test_decay_is_delegated_to_single_atomic_rpc(self):
        client = _Client({
            "status": "succeeded",
            "run_date": "2026-08-03",
            "elapsed_days": 1,
            "updated_count": 2,
            "archived_count": 0,
        })
        with patch(f"{MODULE}.get_client", return_value=client):
            result = run_heat_decay()

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(client.rpc_name, "run_memory_heat_decay")
        self.assertEqual(client.rpc_payload, {})

    def test_same_day_idempotency_is_a_successful_terminal_result(self):
        client = _Client([{
            "status": "already_ran",
            "run_date": "2026-08-03",
            "elapsed_days": 0,
            "updated_count": 0,
            "archived_count": 0,
        }])
        with patch(f"{MODULE}.get_client", return_value=client):
            result = run_heat_decay()

        self.assertEqual(result["status"], "already_ran")

    def test_rpc_failure_stays_retryable(self):
        client = _Client(error=RuntimeError("database unavailable"))
        with patch(f"{MODULE}.get_client", return_value=client):
            result = run_heat_decay()

        self.assertEqual(result, {"status": "failed", "reason": "rpc_failed"})

    def test_malformed_response_stays_retryable(self):
        client = _Client([])
        with patch(f"{MODULE}.get_client", return_value=client):
            result = run_heat_decay()

        self.assertEqual(result, {
            "status": "failed",
            "reason": "invalid_rpc_response",
        })


if __name__ == "__main__":
    unittest.main()

