"""Executable tests for admin/js/lib/absorb_display.js via quickjs.

The pure JS module is loaded and EXECUTED in a real JS engine (no browser
needed), so the failure-branch scoping regression (undefined `id` when a
target row fails to load) is verified by behaviour, not by string matching.
"""

import json
import unittest
from pathlib import Path

try:
    import quickjs  # 测试依赖（requirements-test.txt），生产网关不安装。
    _QUICKJS_AVAILABLE = True
except ImportError:  # pragma: no cover - 环境相关
    _QUICKJS_AVAILABLE = False

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "admin" / "js" / "lib" / "absorb_display.js"


def _load_module():
    source = LIB.read_text(encoding="utf-8")
    # quickjs' python binding evaluates plain scripts: replace the single
    # export statement and expose the functions on globalThis.
    script = source.replace(
        "export { absorbImpactViews, absorbImpactSummary };",
        "var __absorb = { absorbImpactViews, absorbImpactSummary };",
    )
    assert "__absorb" in script
    ctx = quickjs.Context()
    ctx.eval(script)
    return ctx


def _call(ctx, name, *args):
    payload = json.dumps(args)
    return json.loads(ctx.eval(f"JSON.stringify(__absorb.{name}(...JSON.parse({payload!r})))"))


def _row(memory_id=1, content="正文", content_hash="a" * 64, **overrides):
    row = {
        "id": memory_id,
        "content": content,
        "content_hash": content_hash,
        "title": "标题",
        "continuity_id": "21111111-1111-1111-1111-1111111111a1",
        "continuity_type": "moment",
        "memory_key": None,
        "thread_state": None,
        "evidence_message_ids": [3],
        "producer_path": "fast_path",
        "verified": "verified",
        "is_active": True,
    }
    row.update(overrides)
    return row


def _snapshot(memory_id=1, content_hash="a" * 64, **overrides):
    snapshot = {
        "memory_id": memory_id,
        "content_hash": content_hash,
        "continuity_id": "21111111-1111-1111-1111-1111111111a1",
        "continuity_type": "moment",
        "memory_key": None,
        "thread_state": None,
        "evidence_message_ids": [3],
        "producer_path": "fast_path",
        "verified": "verified",
        "is_active": True,
    }
    snapshot.update(overrides)
    return snapshot


@unittest.skipUnless(
    _QUICKJS_AVAILABLE,
    "quickjs 未安装（pip install -r requirements-test.txt）：跳过前端纯函数执行测试",
)
@unittest.skipUnless(
    _QUICKJS_AVAILABLE,
    "quickjs 未安装（pip install -r requirements-test.txt）：跳过前端纯函数执行测试",
)
class AbsorbImpactViewTests(unittest.TestCase):
    """审核表单影响范围：快照逐项比较 + 缺失目标的独立降级。"""

    @classmethod
    def setUpClass(cls):
        cls.ctx = _load_module()

    def test_unchanged_target_is_not_flagged(self):
        views = _call(
            self.ctx, "absorbImpactViews", [4],
            [_row(4)],
            [_snapshot(4)],
        )
        self.assertTrue(views[0]["ok"])
        self.assertFalse(views[0]["changed"])

    def test_each_snapshot_field_change_is_flagged(self):
        cases = {
            "content_hash": _row(4, content_hash="b" * 64),
            "continuity_id": _row(4, continuity_id="21111111-1111-1111-1111-1111111111a2"),
            "continuity_type": _row(4, continuity_type="episode"),
            "memory_key": _row(4, memory_key="topic.changed"),
            "thread_state": _row(4, thread_state="resolved"),
            "evidence_message_ids": _row(4, evidence_message_ids=[999]),
            "is_active": _row(4, is_active=False),
        }
        for field, current in cases.items():
            with self.subTest(field=field):
                views = _call(
                    self.ctx, "absorbImpactViews", [4],
                    [current], [_snapshot(4)],
                )
                self.assertTrue(views[0]["changed"], field)

    def test_producer_path_and_verified_changes_are_flagged(self):
        for field, override in (
            ("producer_path", {"producer_path": "rumination"}),
            ("verified", {"verified": "unverified"}),
        ):
            with self.subTest(field=field):
                views = _call(
                    self.ctx, "absorbImpactViews", [4],
                    [_row(4)], [_snapshot(4, **override)],
                )
                self.assertTrue(views[0]["changed"], field)

    def test_failed_row_uses_the_original_target_id(self):
        views = _call(
            self.ctx, "absorbImpactViews", [8], [None],
            [_snapshot(8)],
        )
        self.assertFalse(views[0]["ok"])
        self.assertEqual(views[0]["id"], 8)
        self.assertIn("无法读取", views[0]["label"])

    def test_snapshot_missing_blocks_even_when_row_active(self):
        # 有目标 ID、当前行存在、快照缺失：必须阻塞（数据库会拒绝）。
        views = _call(
            self.ctx, "absorbImpactViews", [4], [_row(4)], [],
        )
        self.assertTrue(views[0]["snapshotMissing"])
        self.assertFalse(views[0]["changed"])
        self.assertFalse(views[0]["ok"])
        self.assertIn("申请快照缺失", views[0]["label"])
        self.assertTrue(_call(self.ctx, "absorbImpactSummary", views)["blocked"])

    def test_one_missing_snapshot_blocks_but_others_render(self):
        views = _call(
            self.ctx, "absorbImpactViews", [4, 5],
            [_row(4), _row(5)],
            [_snapshot(4)],
        )
        self.assertTrue(views[0]["ok"])
        self.assertFalse(views[0]["snapshotMissing"])
        self.assertTrue(views[1]["snapshotMissing"])
        self.assertTrue(_call(self.ctx, "absorbImpactSummary", views)["blocked"])

    def test_empty_ids_and_snapshots_are_not_flagged(self):
        views = _call(self.ctx, "absorbImpactViews", [], [], [])
        self.assertEqual(views, [])
        summary = _call(self.ctx, "absorbImpactSummary", views)
        self.assertFalse(summary["blocked"])
        self.assertFalse(summary["snapshotMissing"])

    def test_id_without_matching_snapshot_blocks(self):
        # ID 与 snapshot.memory_id 不匹配 → 快照缺失 → blocked。
        views = _call(
            self.ctx, "absorbImpactViews", [4], [_row(4)],
            [_snapshot(99)],
        )
        self.assertTrue(views[0]["snapshotMissing"])
        self.assertTrue(_call(self.ctx, "absorbImpactSummary", views)["blocked"])

    def test_summary_blocks_on_missing_or_changed(self):
        views = _call(self.ctx, "absorbImpactViews", [1], [None], [])
        self.assertTrue(_call(self.ctx, "absorbImpactSummary", views)["blocked"])
        views = _call(
            self.ctx, "absorbImpactViews", [1],
            [_row(1, content_hash="b" * 64)],
            [_snapshot(1)],
        )
        self.assertTrue(_call(self.ctx, "absorbImpactSummary", views)["blocked"])
        views = _call(
            self.ctx, "absorbImpactViews", [1],
            [_row(1)], [_snapshot(1)],
        )
        self.assertFalse(_call(self.ctx, "absorbImpactSummary", views)["blocked"])


if __name__ == "__main__":
    unittest.main()
