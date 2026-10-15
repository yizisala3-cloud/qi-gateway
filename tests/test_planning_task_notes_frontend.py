"""Native module execution of note editor validation, isolation and retries."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TaskNoteFrontendTests(unittest.TestCase):
    def test_note_editor_behaviors(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is required for frontend behavior tests')
        with tempfile.TemporaryDirectory(prefix='qigate-task-note-js-') as name:
            workspace = Path(name)
            shutil.copytree(ROOT / 'admin/js', workspace / 'admin/js')
            (workspace / 'package.json').write_text(json.dumps({'type': 'module'}), encoding='utf-8')
            result = subprocess.run([node, str(ROOT / 'tests/planning_task_notes.test.mjs'), str(workspace)],
                                    capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('task note behavior passed', result.stdout)
