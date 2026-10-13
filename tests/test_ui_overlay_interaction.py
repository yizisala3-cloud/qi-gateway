"""Exercise production modal/surface behavior with native ES-module imports."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "ui_overlay_interaction.test.mjs"


class UiOverlayInteractionTests(unittest.TestCase):
    def test_focus_nested_escape_async_close_and_cancellation(self):
        node = shutil.which("node")
        if node is None:
            try:
                from nodejs import node as bundled_node

                node = bundled_node.path
            except (ImportError, AttributeError):
                self.skipTest("no Node runtime available")
        with tempfile.TemporaryDirectory(prefix="qigate-ui-overlay-") as name:
            workspace = Path(name)
            shutil.copytree(ROOT / "admin" / "js", workspace / "admin" / "js")
            (workspace / "package.json").write_text(
                json.dumps({"type": "module"}), encoding="utf-8")
            result = subprocess.run(
                [node, str(SCRIPT), str(workspace)],
                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("regressions passed", result.stdout)
