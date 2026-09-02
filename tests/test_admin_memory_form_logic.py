"""Executable frontend logic tests for the edit patch builder and time helpers.

The patch diff and the Asia/Shanghai time semantics run as a real ES module
under Node via ``tests/admin_memory_form_logic.test.mjs``. Every result must
be independent of the system timezone, so the suite is executed twice with
``TZ=UTC`` and ``TZ=Asia/Shanghai`` and both runs must pass identically.
Skipped when no Node runtime is available; the nodejs-bin package provides
one in the dev environment.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_SCRIPT = ROOT / "tests" / "admin_memory_form_logic.test.mjs"
PATCH_MODULE = ROOT / "admin" / "js" / "pages" / "_memory_patch.js"


def _node_binary():
    """Return a node executable path, or None."""
    node = shutil.which("node")
    if node:
        return node
    try:
        from nodejs import node as nodejs_module

        return nodejs_module.path  # nodejs-bin bundles the real binary
    except (ImportError, AttributeError):
        return None


def _prepare_script(tmp: Path) -> Path:
    """Copy the pure module + test script as .mjs (repo JS has no package.json)."""
    shutil.copy(PATCH_MODULE, tmp / "_memory_patch.mjs")
    script = TEST_SCRIPT.read_text(encoding="utf-8").replace(
        "../admin/js/pages/_memory_patch.js", "./_memory_patch.mjs"
    )
    target = tmp / "form_logic.test.mjs"
    target.write_text(script, encoding="utf-8")
    return target


class MemoryFormPatchLogicTests(unittest.TestCase):
    def _run_under_tz(self, node: str, tz: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory(prefix="qigate-form-logic-") as tmp_name:
            tmp = Path(tmp_name)
            target = _prepare_script(tmp)
            env = {**os.environ, "TZ": tz}
            return subprocess.run(
                [node, str(target)], capture_output=True, text=True, env=env,
            )

    def test_patch_and_time_rules_are_timezone_independent(self):
        node = _node_binary()
        if node is None:
            self.skipTest("no Node runtime available")
        for tz in ("UTC", "Asia/Shanghai"):
            with self.subTest(timezone=tz):
                proc = self._run_under_tz(node, tz)
                output = (proc.stdout or "") + (proc.stderr or "")
                self.assertEqual(proc.returncode, 0, f"TZ={tz} failed:\n{output}")
                self.assertNotIn("FAIL", output)
                # 21 个用例必须全部执行，防止脚本被静默截断。
                self.assertGreaterEqual(output.count("PASS "), 21, output)


if __name__ == "__main__":
    unittest.main()
