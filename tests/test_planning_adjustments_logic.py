"""boundary 多项冲突调整合并逻辑的前端真执行测试（批次 9 UI #2 修复）。

``tests/planning_adjustments.test.mjs`` 在 Node 下作为真实 ES 模块运行
``admin/js/lib/planning_adjustments.js``（纯逻辑、无浏览器依赖）。无 Node
运行时时跳过。
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_SCRIPT = ROOT / "tests" / "planning_adjustments.test.mjs"
ADJUST_MODULE = ROOT / "admin" / "js" / "lib" / "planning_adjustments.js"


class PlanningAdjustmentsLogicTests(unittest.TestCase):
    def test_merge_rules_hold_in_real_es_module(self):
        node = shutil.which("node")
        if node is None:
            try:
                from nodejs import node as nodejs_module

                node = nodejs_module.path  # nodejs-bin bundles the real binary
            except (ImportError, AttributeError):
                node = None
        if node is None:
            self.skipTest("no Node runtime available")
        with tempfile.TemporaryDirectory(prefix="qigate-planning-adj-") as tmp_name:
            tmp = Path(tmp_name)
            shutil.copy(ADJUST_MODULE, tmp / "planning_adjustments.mjs")
            script = TEST_SCRIPT.read_text(encoding="utf-8").replace(
                "./planning_adjustments.mjs", "./planning_adjustments.mjs")
            target = tmp / "planning_adjustments.test.mjs"
            target.write_text(script, encoding="utf-8")
            proc = subprocess.run(
                [node, str(target)], capture_output=True, text=True)
        self.assertEqual(
            proc.returncode, 0,
            f"node exit={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        self.assertIn("PASS", proc.stdout)
        self.assertNotIn("FAIL", proc.stdout)


if __name__ == "__main__":
    unittest.main()
