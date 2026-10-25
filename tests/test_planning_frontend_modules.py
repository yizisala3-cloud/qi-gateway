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
    def _run_group(self, group, payload=None):
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
            if payload is not None:
                (tmp / "label-cases.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
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

    def test_committed_occurrence_receipt_does_not_wait_for_board_refresh(self):
        self._run_group("console-receipt-background")

    def test_latency_feedback_is_immediate_shared_and_restored(self):
        self._run_group("latency-actions")

    def test_latency_writes_refresh_only_visible_authoritative_lists(self):
        self._run_group("latency-visible-writes")

    def test_recompute_receipt_refreshes_current_mount_without_polling(self):
        self._run_group("latency-recompute-remount")

    def test_all_tasks_hide_deleted_definitions_and_occurrences_but_keep_paused(self):
        self._run_group("all-hide-deleted")

    def test_memo_tag_deletion_entry_confirmation_and_lifecycle(self):
        self._run_group("memo-tag-delete")

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

    def test_note_commit_after_remount_refreshes_current_page(self):
        self._run_group("C8-note-remount")

    def test_reminder_silent_warmup_dismissal_recovery_and_cleanup(self):
        self._run_group("B7-reminder-ring")

    def test_reminder_playback_identity_and_modal_recovery_entry(self):
        self._run_group("B8-ring-recovery-identity")

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

    def test_order_receipt_preserves_newer_results_facts_and_seeded_cache(self):
        self._run_group("C6-order-receipt")

    def test_field_authority_and_labels_match_backend_semantics(self):
        from datetime import datetime
        from gateway.planning_serialization import schedule_label

        now = "2026-10-12T12:00:00+08:00"
        payload = []
        starts = [None, "2026-10-12T11:00:00+08:00", "2026-10-12T12:00:00+08:00",
                  "2026-10-12T12:00:00.001+08:00", "2026-10-12T12:00:00.000001+08:00",
                  "2026-10-12T12:00:00.000999+08:00", "2026-10-12T04:30:00Z"]
        nominals = [None, "2026-10-12T12:00:59.999+08:00", "2026-10-12T12:01:00+08:00",
                    "2026-10-12T12:01:00.001+08:00", "2026-10-12T12:01:00.000001+08:00",
                    "2026-10-12T13:00:00+08:00"]
        for status in ["pending", "in_progress", "partial", "deferred", "timeout", "completed", "discarded"]:
            for start in starts:
                for nominal in nominals:
                    occ = {"status": status, "est_start": start, "nominal_start": nominal}
                    payload.append({"occ": occ, "now": now,
                                    "expected": schedule_label(occ, {}, datetime.fromisoformat(now))})
        for clock in ["2026-10-12T12:00:00.000999+08:00", "2026-10-12T12:00:00.000001+08:00"]:
            for start in starts:
                occ = {"status": "pending", "est_start": start, "nominal_start": None}
                payload.append({"occ": occ, "now": clock,
                                "expected": schedule_label(occ, {}, datetime.fromisoformat(clock))})
        self._run_group("C7-field-authority-and-labels", payload)
