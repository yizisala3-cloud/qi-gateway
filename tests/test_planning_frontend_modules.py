"""Run the planning module graph and behaviour through native Node ES imports.

Production JS is copied unchanged into a temporary type=module package. Imports,
including version queries and exported names, are resolved by Node itself.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "planning_frontend_modules.test.mjs"


class PlanningFrontendModulesTests(unittest.TestCase):
    def _run_group(self, group):
        node = shutil.which("node")
        if node is None:
            try:
                from nodejs import node as bundled_node

                node = bundled_node.path
            except (ImportError, AttributeError):
                node = None
        if node is None:
            self.skipTest("no Node runtime available")
        with tempfile.TemporaryDirectory(prefix="qigate-planning-modules-") as tmp_name:
            tmp = Path(tmp_name)
            shutil.copytree(ROOT / "admin" / "js", tmp / "admin" / "js")
            (tmp / "package.json").write_text(
                json.dumps({"type": "module"}), encoding="utf-8")
            env = dict(os.environ, TZ="Asia/Shanghai")
            result = subprocess.run(
                [node, str(SCRIPT), str(tmp), group],
                capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual(
            result.returncode, 0,
            f"{group}: node exit={result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        self.assertIn(f"PASS {group}", result.stdout)

    def test_native_es_import_graph_and_versions(self):
        self._run_group("graph")

    def test_display_business_outputs(self):
        self._run_group("B2-display")

    def test_form_api_failure_retry_and_committed_state(self):
        self._run_group("B3-form")

    def test_dialog_retry_identity_and_clear_payload(self):
        self._run_group("B4-dialogs")

    def test_sort_guards_conflict_recovery_and_pointer_cleanup(self):
        self._run_group("B5-sort")

    def test_page_mount_remount_and_reminder_lifecycle(self):
        self._run_group("B6-lifecycle")

    def test_save_refreshes_visible_lists_and_invalidates_hidden_tabs(self):
        self._run_group("C1-visible-refresh")

    def test_poll_and_filter_reads_merge_and_ignore_older_responses(self):
        self._run_group("C2-stale-responses")

    def test_committed_form_preserves_lists_and_retries_refresh_without_creating(self):
        self._run_group("C3-committed-refresh-retry")

    def test_inflight_reads_cannot_render_or_restart_poll_after_unmount(self):
        self._run_group("C4-inflight-unmount")

    def test_closing_pending_form_and_remounting_cannot_start_second_create(self):
        self._run_group("C5-closed-pending-form")
