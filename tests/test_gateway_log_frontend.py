"""日志页前端契约测试（Node 原生 ES 模块导入生产 logs.js）。

生产 JS 原样拷贝进临时 type=module 包，由 Node 解析 import 与导出；
DOM 用最小 shim，fetch 可编程应答。覆盖：真实渲染、筛选与分页参数、
失败 / 空态 / 401 中文提示、详情与 XSS 转义、旧响应丢弃。
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "gateway_log_frontend.test.mjs"


class GatewayLogFrontendTests(unittest.TestCase):
    def _run_group(self, group):
        node = shutil.which("node")
        if node is None:
            try:
                from nodejs import node as bundled_node

                node = bundled_node.path
            except (ImportError, AttributeError):
                node = None
        if node is None:
            self.skipTest("no Node runtime available")
        with tempfile.TemporaryDirectory(prefix="qigate-log-frontend-") as tmp_name:
            tmp = Path(tmp_name)
            shutil.copytree(ROOT / "admin" / "js", tmp / "admin" / "js")
            (tmp / "package.json").write_text(
                json.dumps({"type": "module"}), encoding="utf-8")
            env = dict(os.environ, TZ="Asia/Shanghai")
            result = subprocess.run(
                [node, str(SCRIPT), str(tmp), group],
                capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual(
            result.returncode, 0,
            f"{group}: node exit={result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        self.assertIn(f"PASS {group}", result.stdout)

    def test_graph_and_placeholder_removed(self):
        self._run_group("graph")

    def test_list_render_and_cursor_paging(self):
        self._run_group("list")

    def test_filters_and_custom_range_validation(self):
        self._run_group("filters")

    def test_empty_failure_and_auth_states(self):
        self._run_group("errors")

    def test_group_detail_and_xss_escaping(self):
        self._run_group("detail")

    def test_stale_response_cannot_overwrite_new_filter(self):
        self._run_group("stale")

    def test_remount_resets_state_and_cancels_requests(self):
        self._run_group("remount")

    def test_meta_race_cannot_overwrite_new_filter(self):
        self._run_group("metaRace")

    def test_spool_loss_status_is_visible(self):
        self._run_group("spoolLoss")

    def test_group_detail_reuses_filters_and_snapshot(self):
        self._run_group("detailFilters")

    def test_paging_session_freezes_time_range(self):
        self._run_group("frozenRange")

    def test_detail_modal_lifecycle_is_recycled_on_unmount(self):
        self._run_group("detailLifecycle")

    def test_purged_corrupt_loss_stays_visible(self):
        self._run_group("purgedCorrupt")
    def test_key_identity_filter_and_visible_snapshot(self):
        self._run_group("keyIdentityAndVisibleSnapshot")



if __name__ == "__main__":
    unittest.main()
