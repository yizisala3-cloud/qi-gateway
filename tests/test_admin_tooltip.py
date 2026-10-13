"""Execute shared paper tooltip behaviour with deterministic DOM/timer boundaries."""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "retro_tooltip.test.mjs"


class AdminTooltipTests(unittest.TestCase):
    def test_tooltip_names_keyboard_hover_viewport_and_lifecycle(self):
        node = shutil.which("node")
        if node is None:
            try:
                from nodejs import node as bundled_node
                node = bundled_node.path
            except (ImportError, AttributeError):
                self.skipTest("no Node runtime available")
        result = subprocess.run(
            [node, str(SCRIPT)], cwd=ROOT, capture_output=True,
            text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS tooltip:", result.stdout)


if __name__ == "__main__":
    unittest.main()
