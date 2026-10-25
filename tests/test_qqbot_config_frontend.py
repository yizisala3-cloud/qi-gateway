"""Execute unmodified ES modules with deferred fetches and a test DOM."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class QQConfigFrontendTests(unittest.TestCase):
    def run_group(self, group):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node runtime unavailable')
        with tempfile.TemporaryDirectory(prefix='qigate-qq-config-') as folder:
            target = Path(folder)
            shutil.copytree(ROOT / 'admin' / 'js', target / 'admin' / 'js')
            (target / 'package.json').write_text(json.dumps({'type':'module'}),encoding='utf-8')
            result = subprocess.run([node,'--test-reporter=tap',str(ROOT/'tests/qqbot_config_mount.test.mjs'),str(target),group],
                capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
        self.assertIn('# fail 0',result.stdout)
        self.assertNotIn('# tests 0',result.stdout)

    def test_stale_reads_before_during_and_after_saves(self):
        self.run_group('stale-reads')

    def test_stale_fatal_and_optional_failures(self):
        self.run_group('failures')

    def test_discarded_mounts_and_root_ownership(self):
        self.run_group('discarded')

    def test_late_save_response_is_still_isolated(self):
        self.run_group('saves')

    def test_current_load_errors_and_normal_clicks(self):
        self.run_group('normal')

    def test_daily_limit_validation_save_and_late_responses(self):
        self.run_group('daily-limit')

    def test_proactive_recovery_and_late_responses(self):
        self.run_group('evaluation-recovery')

    def test_active_window_validation_persistence_and_late_responses(self):
        self.run_group('active-window')
