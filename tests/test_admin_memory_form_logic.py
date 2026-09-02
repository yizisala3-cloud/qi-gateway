"""Executable frontend logic tests for the memory edit patch builder.

The patch diff (independent time/precision comparison, unknown-precision
normalization for cleared times) runs as a real ES module under Node. The
repo's JS is served as browser ES modules without a package.json, so the
runner copies the module and test script into a temp dir as ``.mjs`` files
(Node then parses them as ESM) and rewrites the relative import. Skipped
when no Node runtime is available; the nodejs-bin package provides one in
the dev environment.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_SCRIPT = ROOT / "tests" / "admin_memory_form_logic.test.mjs"
PATCH_MODULE = ROOT / "admin" / "js" / "pages" / "_memory_patch.js"


def _node_runner():
    """Return a callable(list[str]) -> CompletedProcess, or None."""
    import subprocess

    node = shutil.which("node")
    if not node:
        try:
            from nodejs import node as nodejs_module

            node = nodejs_module.path  # nodejs-bin bundles the real binary
        except (ImportError, AttributeError):
            return None

    def run(args):
        return subprocess.run([node, *args], capture_output=True, text=True)

    return run


class MemoryFormPatchLogicTests(unittest.TestCase):
    def test_patch_builder_rules(self):
        run = _node_runner()
        if run is None:
            self.skipTest("no Node runtime available")
        with tempfile.TemporaryDirectory(prefix="qigate-form-logic-") as tmp:
            tmp = Path(tmp)
            shutil.copy(PATCH_MODULE, tmp / "_memory_patch.mjs")
            script = TEST_SCRIPT.read_text(encoding="utf-8").replace(
                "../admin/js/pages/_memory_patch.js", "./_memory_patch.mjs"
            )
            (tmp / "form_logic.test.mjs").write_text(script, encoding="utf-8")
            proc = run([str(tmp / "form_logic.test.mjs")])
        output = (proc.stdout or "") + (proc.stderr or "")
        self.assertEqual(proc.returncode, 0, f"form logic tests failed:\n{output}")
        self.assertNotIn("FAIL", output)
        # 十个用例必须全部执行，防止脚本被静默截断。
        self.assertGreaterEqual(output.count("PASS "), 10, output)


if __name__ == "__main__":
    unittest.main()
