"""Cleanup scheduling is independent of interactive memo requests."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest import mock

from gateway import main, memo


class MemoMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_waits_runs_serially_recovers_and_can_be_cancelled(self):
        sleeps = []
        cleanup = mock.Mock(side_effect=[RuntimeError("transient cleanup failure"), 0])

        async def sleep(seconds):
            sleeps.append(seconds)
            self.assertEqual(cleanup.call_count, len(sleeps) - 1)
            if len(sleeps) == 3:
                raise asyncio.CancelledError()

        with ThreadPoolExecutor(max_workers=1) as executor, \
                mock.patch.object(main, "bg_executor", executor), \
                mock.patch.object(main, "run_trash_maintenance", cleanup), \
                mock.patch.object(main.asyncio, "sleep", sleep), \
                self.assertLogs("gateway.memo", level="ERROR") as logged:
            with self.assertRaises(asyncio.CancelledError):
                await main.memo_maintenance_loop()
        self.assertEqual(sleeps, [60, 60, 60])
        self.assertEqual(cleanup.call_count, 2)
        self.assertIn("transient cleanup failure", "\n".join(logged.output))

    async def test_cleanup_without_database_does_not_issue_rpc(self):
        with mock.patch.object(memo, "get_client", return_value=None), \
                mock.patch.object(memo, "_call_rpc") as rpc:
            self.assertEqual(memo.run_trash_maintenance(), 0)
        rpc.assert_not_called()
