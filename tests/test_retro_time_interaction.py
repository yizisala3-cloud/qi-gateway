"""Run shared calendar/time and planning-dialog behavior with native ES imports."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "retro_time_interaction.test.mjs"


class RetroTimeInteractionTests(unittest.TestCase):
    def test_calendar_time_keyboard_values_and_dialog_integration(self):
        node = shutil.which("node")
        if node is None:
            try:
                from nodejs import node as bundled_node
                node = bundled_node.path
            except (ImportError, AttributeError):
                self.skipTest("no Node runtime available")
        with tempfile.TemporaryDirectory(prefix="qigate-retro-time-") as name:
            workspace = Path(name)
            shutil.copytree(ROOT / "admin" / "js", workspace / "admin" / "js")
            (workspace / "package.json").write_text(json.dumps({"type": "module"}), encoding="utf-8")
            for timezone in ("UTC", "Asia/Shanghai", "America/Los_Angeles"):
                with self.subTest(timezone=timezone):
                    result = subprocess.run(
                        [node, str(SCRIPT), str(workspace)],
                        capture_output=True, text=True, encoding="utf-8", timeout=30,
                        env=dict(os.environ, TZ=timezone),
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("regressions passed", result.stdout)
