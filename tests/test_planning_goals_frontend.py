"""Native module behavior: request identity, reads, drafts and recovery."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class GoalFrontendTests(unittest.TestCase):
    def _run(self, script, expected):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is required for frontend behavior tests')
        with tempfile.TemporaryDirectory(prefix='qigate-goals-js-') as folder:
            workspace = Path(folder)
            shutil.copytree(ROOT / 'admin/js', workspace / 'admin/js')
            (workspace / 'package.json').write_text(json.dumps({'type': 'module'}), encoding='utf-8')
            result = subprocess.run([node, str(ROOT / 'tests' / script), str(workspace)],
                                    capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(expected, result.stdout)

    def test_goal_frontend_behavior(self):
        self._run('planning_goals.test.mjs', 'goal frontend behavior passed')

    def test_goal_race_regressions(self):
        self._run('planning_goals_races.test.mjs', 'goal race regressions passed')
