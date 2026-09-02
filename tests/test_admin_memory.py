"""Service-level tests for gateway.admin_memory.

A fake Supabase client records every RPC payload so the tests can assert
what the gateway actually sends to the database: server-owned fields never
appear, recall vectors are generated before the transaction, and business
error codes map to stable, Chinese, user-facing messages.
"""

import hashlib
import unittest
from types import SimpleNamespace
from unittest import mock

from gateway import admin_memory
from gateway.admin_memory import AdminMemoryError
from gateway.memory_continuity_schema import validate_continuity_data
from gateway.memory_requests import MemoryRequestError

ASSISTANT = "assistant-test"


def sha256(text: str) -> str:
    return hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()


class FakeBuilder:
    def __init__(self, outcome):
        self.outcome = outcome

    def execute(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return SimpleNamespace(data=self.outcome)


class FakeClient:
    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}
        self.calls = []

    def rpc(self, name, payload):
        self.calls.append((name, payload))
        outcome = self.outcomes.get(name)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeBuilder(outcome)


def _rpc_error(message: str) -> Exception:
    return RuntimeError(message)


CREATE_OK = {
    "memory_id": 501,
    "continuity_id": "cu-1",
    "source": "manual",
    "verified": "verified",
    "is_active": True,
    "heat": 50.0,
}

MOMENT_DATA = {
    "scene": "聊天窗口", "event": "约定赶海", "moment_state": "standalone",
}
THREAD_OPEN_DATA = {
    "open_question": "下周三赶海是否成行", "current_state": "已约定待确认",
}
EPISODE_DATA = {
    "beginning": "约好赶海", "development": "讨论装备", "outcome": "定在下周三",
    "closure_quality": "complete",
}
JOKE_DATA = {
    "origin": "把防晒霜叫贝壳", "trigger_phrases": ["贝壳"], "shared_meaning": "防晒霜代号",
}
PROFILE_DATA = {
    "facet": "作息", "statement": "叶子习惯晚睡", "scope": "全局",
    "stability": "stable", "basis": "explicit_self_report",
}
RULE_DATA = {
    "trigger": "提到赶海", "expected_behavior": "提醒防晒", "scope": "全局",
    "priority": 5, "rule_state": "active", "explicit_instruction": "赶海话题提醒防晒",
}


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.embedding_calls = []
        patches = [
            mock.patch.object(admin_memory, "get_client", lambda: self.client),
            mock.patch.object(admin_memory, "resolve_assistant_id", lambda: ASSISTANT),
            mock.patch.object(
                admin_memory, "_server_writes_allowed", lambda: True
            ),
            mock.patch.object(
                admin_memory, "_recall_embedding",
                self._fake_recall_embedding,
            ),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def _fake_recall_embedding(self, scene):
        self.embedding_calls.append(scene)
        return [0.1, 0.2, 0.3]

    def _last_call(self, name):
        calls = [item for item in self.client.calls if item[0] == name]
        self.assertTrue(calls, f"RPC {name} was never called")
        return calls[-1][1]


class CreateAdminMemoryTests(ServiceTestCase):
    """用户手工新增：六类各一条成功案例与全部服务端固定值。"""

    def test_six_types_create_successfully(self):
        cases = [
            ("moment", MOMENT_DATA, None),
            ("thread", THREAD_OPEN_DATA, "open"),
            ("episode", EPISODE_DATA, None),
            ("inside_joke", JOKE_DATA, None),
            ("profile", PROFILE_DATA, None),
            ("interaction_rule", RULE_DATA, None),
        ]
        for index, (ctype, data, state) in enumerate(cases):
            with self.subTest(type=ctype):
                self.client.outcomes["create_admin_memory_v1"] = CREATE_OK
                result = admin_memory.create_admin_memory({
                    "content": f"这是一条手工写入的{ctype}测试记忆。",
                    "importance": 6,
                    "recall_tags": ["测试"],
                    "continuity_type": ctype,
                    "thread_state": state,
                    "continuity_data": data,
                })
                self.assertEqual(result["memory_id"], 501)
                payload = self._last_call("create_admin_memory_v1")
                self.assertEqual(payload["p_continuity_type"], ctype)
                self.assertEqual(payload["p_thread_state"], state)
                self.assertEqual(
                    payload["p_continuity_data"],
                    validate_continuity_data(ctype, state, data),
                )
                self.assertEqual(payload["p_assistant_id"], ASSISTANT)

    def test_required_fields_missing_per_type_are_rejected(self):
        cases = [
            ("moment", {}, None),
            ("thread", {}, "open"),
            ("episode", {"beginning": "开端"}, None),
            ("inside_joke", {"origin": "来历"}, None),
            ("profile", {"facet": "侧面"}, None),
            ("interaction_rule", {"trigger": "触发"}, None),
        ]
        for ctype, data, state in cases:
            with self.subTest(type=ctype):
                with self.assertRaises(AdminMemoryError) as ctx:
                    admin_memory.create_admin_memory({
                        "content": "这条记忆的结构不完整。",
                        "continuity_type": ctype,
                        "thread_state": state,
                        "continuity_data": data,
                    })
                self.assertEqual(ctx.exception.code, "admin_memory_invalid_continuity_data")
                self.assertFalse(self.client.calls)

    def test_fixed_enums_are_validated(self):
        cases = [
            {"continuity_type": "moment",
             "continuity_data": {**MOMENT_DATA, "moment_state": "merged"}},
            {"continuity_type": "episode",
             "continuity_data": {**EPISODE_DATA, "closure_quality": "fine"}},
            {"continuity_type": "profile",
             "continuity_data": {**PROFILE_DATA, "stability": "solid"}},
            {"continuity_type": "profile",
             "continuity_data": {**PROFILE_DATA, "basis": "guessing"}},
            {"continuity_type": "interaction_rule",
             "continuity_data": {**RULE_DATA, "rule_state": "expired"}},
            {"continuity_type": "interaction_rule",
             "continuity_data": {**RULE_DATA, "priority": 0}},
            {"continuity_type": "thread", "thread_state": "closed",
             "continuity_data": THREAD_OPEN_DATA},
            {"continuity_type": "moment", "thread_state": "open",
             "continuity_data": MOMENT_DATA},
        ]
        for body in cases:
            with self.subTest(type=body["continuity_type"]):
                with self.assertRaises(AdminMemoryError) as ctx:
                    admin_memory.create_admin_memory(
                        {"content": "枚举值不合法时必须拒绝。", **body}
                    )
                self.assertIn(ctx.exception.code, {
                    "admin_memory_invalid_continuity_data", "admin_memory_invalid_type",
                    "admin_memory_invalid_thread_state",
                })

    def test_closed_thread_requires_closure_fields_and_open_thread_forbids_them(self):
        closed_data = {**THREAD_OPEN_DATA, "closure_summary": "改期了", "closure_reason": "下雨"}
        with self.subTest(case="closed missing fields"):
            with self.assertRaises(AdminMemoryError):
                admin_memory.create_admin_memory({
                    "content": "关闭线索缺少结束信息。",
                    "continuity_type": "thread", "thread_state": "resolved",
                    "continuity_data": closed_data,
                })
        with self.subTest(case="open with residue"):
            with self.assertRaises(AdminMemoryError):
                admin_memory.create_admin_memory({
                    "content": "进行中的线索残留结束字段。",
                    "continuity_type": "thread", "thread_state": "open",
                    "continuity_data": {**THREAD_OPEN_DATA, "closed_at": "2026-09-01"},
                })

    def test_content_boundaries(self):
        self.client.outcomes["create_admin_memory_v1"] = CREATE_OK
        for text, ok in (("短", False), ("刚好五个字", True), ("字" * 600, True), ("字" * 601, False)):
            with self.subTest(length=len(text), ok=ok):
                if ok:
                    admin_memory.create_admin_memory({
                        "content": text, "continuity_type": "moment",
                        "continuity_data": MOMENT_DATA,
                    })
                else:
                    with self.assertRaises(AdminMemoryError):
                        admin_memory.create_admin_memory({
                            "content": text, "continuity_type": "moment",
                            "continuity_data": MOMENT_DATA,
                        })

    def test_tags_dedupe_and_length_rules(self):
        self.client.outcomes["create_admin_memory_v1"] = CREATE_OK
        admin_memory.create_admin_memory({
            "content": "标签去重与长度测试。", "tags": [" 赶海 ", "赶海", "潮汐"],
            "recall_tags": ["海边", "海边"],
            "continuity_type": "moment", "continuity_data": MOMENT_DATA,
        })
        payload = self._last_call("create_admin_memory_v1")
        self.assertEqual(payload["p_tags"], ["赶海", "潮汐"])
        self.assertEqual(payload["p_recall_tags"], ["海边"])
        with self.subTest(case="overlong tag"):
            with self.assertRaises(AdminMemoryError) as ctx:
                admin_memory.create_admin_memory({
                    "content": "超长标签必须拒绝。",
                    "tags": ["字" * 201],
                    "continuity_type": "moment", "continuity_data": MOMENT_DATA,
                })
            self.assertEqual(ctx.exception.code, "admin_memory_invalid_tags")

    def test_empty_recall_tags_are_allowed_by_server(self):
        self.client.outcomes["create_admin_memory_v1"] = CREATE_OK
        admin_memory.create_admin_memory({
            "content": "没有召回标签也允许提交。",
            "recall_tags": [],
            "continuity_type": "moment", "continuity_data": MOMENT_DATA,
        })
        payload = self._last_call("create_admin_memory_v1")
        self.assertEqual(payload["p_recall_tags"], [])

    def test_source_type_blank_saves_as_null(self):
        self.client.outcomes["create_admin_memory_v1"] = CREATE_OK
        for blank in (None, "", "  "):
            with self.subTest(source_type=repr(blank)):
                admin_memory.create_admin_memory({
                    "content": "来源类型空值保存为 NULL。",
                    "source_type": blank,
                    "continuity_type": "moment", "continuity_data": MOMENT_DATA,
                })
                payload = self._last_call("create_admin_memory_v1")
                self.assertIsNone(payload["p_source_type"])
        with self.subTest(case="invalid enum"):
            with self.assertRaises(AdminMemoryError) as ctx:
                admin_memory.create_admin_memory({
                    "content": "来源类型枚举校验。",
                    "source_type": "magic",
                    "continuity_type": "moment", "continuity_data": MOMENT_DATA,
                })
            self.assertEqual(ctx.exception.code, "admin_memory_invalid_source_type")

    def test_internal_fields_cannot_be_forged(self):
        for field, value in (
            ("assistant_id", "hacker"), ("source", "mcp_memory"),
            ("verified", "rejected"), ("is_active", False), ("heat", 99),
            ("content_hash", "0" * 64), ("continuity_id", "cu-9"),
            ("recall_embedding", [0.0]), ("id", 1), ("superseded_at", "now"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(AdminMemoryError) as ctx:
                    admin_memory.create_admin_memory({
                        "content": "内部字段不能由前端伪造。",
                        "continuity_type": "moment", "continuity_data": MOMENT_DATA,
                        field: value,
                    })
                self.assertEqual(ctx.exception.code, "admin_memory_unsupported_field")
                self.assertFalse(self.client.calls)

    def test_recall_embedding_generated_only_when_scene_present(self):
        self.client.outcomes["create_admin_memory_v1"] = CREATE_OK
        with self.subTest(case="with scene"):
            admin_memory.create_admin_memory({
                "content": "有召回场景的记忆。",
                "recall_scene": "聊到赶海时",
                "continuity_type": "moment", "continuity_data": MOMENT_DATA,
            })
            self.assertEqual(self.embedding_calls, ["聊到赶海时"])
            payload = self._last_call("create_admin_memory_v1")
            self.assertEqual(payload["p_recall_embedding"], [0.1, 0.2, 0.3])
        with self.subTest(case="sceneless"):
            self.embedding_calls.clear()
            admin_memory.create_admin_memory({
                "content": "没有召回场景的记忆。",
                "continuity_type": "moment", "continuity_data": MOMENT_DATA,
            })
            self.assertEqual(self.embedding_calls, [])
            payload = self._last_call("create_admin_memory_v1")
            self.assertIsNone(payload["p_recall_embedding"])

    def test_embedding_failure_blocks_the_write(self):
        def boom(_scene):
            raise MemoryRequestError("recall_embedding_failed", "vector provider down", 503)

        with mock.patch.object(admin_memory, "_recall_embedding", boom):
            with self.assertRaises(AdminMemoryError) as ctx:
                admin_memory.create_admin_memory({
                    "content": "向量失败时不能写入半成品。",
                    "recall_scene": "聊到赶海时",
                    "continuity_type": "moment", "continuity_data": MOMENT_DATA,
                })
        self.assertEqual(ctx.exception.code, "recall_embedding_failed")
        self.assertFalse(self.client.calls)

    def test_no_evidence_means_no_fabricated_times(self):
        self.client.outcomes["create_admin_memory_v1"] = CREATE_OK
        admin_memory.create_admin_memory({
            "content": "没有证据消息的记忆。",
            "evidence_message_ids": [],
            "continuity_type": "moment", "continuity_data": MOMENT_DATA,
        })
        payload = self._last_call("create_admin_memory_v1")
        self.assertEqual(payload["p_evidence_message_ids"], [])
        # 服务端不伪造任何证据时间字段：载荷里根本没有这些键。
        self.assertNotIn("p_evidence_start_time", payload)
        self.assertNotIn("p_evidence_end_time", payload)


class EditAdminMemoryTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.client.outcomes["edit_admin_memory_v1"] = {
            "memory": {"id": 7, "continuity_type": "moment"},
        }

    def test_unclassified_memory_edits_ordinary_fields_without_continuity(self):
        admin_memory.edit_admin_memory(7, {"content": "未分类记忆只改正文。"})
        payload = self._last_call("edit_admin_memory_v1")
        self.assertEqual(payload["p_patch"], {"content": "未分类记忆只改正文。"})
        self.assertEqual(payload["p_content_hash"], sha256("未分类记忆只改正文。"))
        self.assertNotIn("continuity_type", payload["p_patch"])

    def test_unclassified_memory_can_adopt_a_type(self):
        admin_memory.edit_admin_memory(7, {
            "continuity_type": "profile",
            "continuity_data": PROFILE_DATA,
        })
        payload = self._last_call("edit_admin_memory_v1")
        self.assertEqual(payload["p_patch"]["continuity_type"], "profile")
        self.assertEqual(
            payload["p_patch"]["continuity_data"],
            validate_continuity_data("profile", None, PROFILE_DATA),
        )

    def test_incomplete_adoption_is_rejected(self):
        with self.assertRaises(AdminMemoryError) as ctx:
            admin_memory.edit_admin_memory(7, {
                "continuity_type": "profile",
                "continuity_data": {"facet": "只有侧面"},
            })
        self.assertEqual(ctx.exception.code, "admin_memory_invalid_continuity_data")

    def test_same_type_continuity_edit_passes_validation(self):
        admin_memory.edit_admin_memory(7, {
            "continuity_type": "moment",
            "continuity_data": {**MOMENT_DATA, "outcome": "定在下周三"},
        })
        payload = self._last_call("edit_admin_memory_v1")
        self.assertEqual(payload["p_patch"]["continuity_data"]["outcome"], "定在下周三")

    def test_content_hash_recomputed_on_content_change_only(self):
        admin_memory.edit_admin_memory(7, {"title": "只改标题"})
        payload = self._last_call("edit_admin_memory_v1")
        self.assertIsNone(payload["p_content_hash"])
        admin_memory.edit_admin_memory(7, {"content": "正文变了哈希也要变。"})
        payload = self._last_call("edit_admin_memory_v1")
        self.assertEqual(payload["p_content_hash"], sha256("正文变了哈希也要变。"))

    def test_recall_scene_edit_rebuilds_embedding_and_clear_resets_to_null(self):
        with self.subTest(case="scene changed"):
            admin_memory.edit_admin_memory(7, {"recall_scene": "新的召回场景"})
            self.assertEqual(self.embedding_calls, ["新的召回场景"])
            payload = self._last_call("edit_admin_memory_v1")
            self.assertEqual(payload["p_recall_embedding"], [0.1, 0.2, 0.3])
        with self.subTest(case="scene cleared"):
            self.embedding_calls.clear()
            admin_memory.edit_admin_memory(7, {"recall_scene": None})
            self.assertEqual(self.embedding_calls, [])
            payload = self._last_call("edit_admin_memory_v1")
            self.assertIsNone(payload["p_patch"]["recall_scene"])
            self.assertIsNone(payload["p_recall_embedding"])

    def test_not_editable_error_is_mapped(self):
        self.client.outcomes["edit_admin_memory_v1"] = _rpc_error(
            "admin_memory_not_editable"
        )
        with self.assertRaises(AdminMemoryError) as ctx:
            admin_memory.edit_admin_memory(7, {"title": "归档记忆不可编辑"})
        self.assertEqual(ctx.exception.code, "admin_memory_not_editable")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_empty_patch_is_rejected(self):
        with self.assertRaises(AdminMemoryError):
            admin_memory.edit_admin_memory(7, {})

    def test_same_type_continuity_edit_carries_current_type(self):
        # 前端实际构造的同类型结构编辑请求：continuity_data + 当前
        # continuity_type 一起提交；thread 同时带完整 thread_state。
        admin_memory.edit_admin_memory(7, {
            "continuity_type": "moment",
            "continuity_data": {**MOMENT_DATA, "outcome": "定在下周三"},
        })
        payload = self._last_call("edit_admin_memory_v1")
        self.assertEqual(payload["p_patch"]["continuity_type"], "moment")
        self.assertNotIn("thread_state", payload["p_patch"])

    def test_thread_same_type_edit_with_state_change(self):
        self.client.outcomes["edit_admin_memory_v1"] = {
            "memory": {"id": 7, "continuity_type": "thread"},
        }
        admin_memory.edit_admin_memory(7, {
            "continuity_type": "thread",
            "thread_state": "paused",
            "continuity_data": THREAD_OPEN_DATA,
        })
        payload = self._last_call("edit_admin_memory_v1")
        self.assertEqual(payload["p_patch"]["continuity_type"], "thread")
        self.assertEqual(payload["p_patch"]["thread_state"], "paused")
        self.assertEqual(
            payload["p_patch"]["continuity_data"],
            validate_continuity_data("thread", "paused", THREAD_OPEN_DATA),
        )

    def test_type_switch_via_edit_reaches_rpc_and_maps_rejection(self):
        # 服务层不做类型切换裁决（由事务 RPC 强制），但稳定错误码必须映射。
        self.client.outcomes["edit_admin_memory_v1"] = _rpc_error(
            "admin_memory_type_change_forbidden"
        )
        with self.assertRaises(AdminMemoryError) as ctx:
            admin_memory.edit_admin_memory(7, {
                "continuity_type": "profile",
                "continuity_data": PROFILE_DATA,
            })
        self.assertEqual(ctx.exception.code, "admin_memory_type_change_forbidden")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_content_hash_conflict_maps_to_409(self):
        self.client.outcomes["edit_admin_memory_v1"] = _rpc_error(
            "admin_memory_content_exists"
        )
        with self.assertRaises(AdminMemoryError) as ctx:
            admin_memory.edit_admin_memory(7, {"content": "这条正文和别的记忆一样了。"})
        self.assertEqual(ctx.exception.code, "admin_memory_content_exists")
        self.assertEqual(ctx.exception.status_code, 409)


class ChangeTypeTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.client.outcomes["change_memory_type_v1"] = {
            "memory": {"id": 9, "continuity_type": "episode"},
            "previous_version_id": 7,
            "removed_version_id": 6,
        }

    def test_change_creates_new_version_payload(self):
        result = admin_memory.change_memory_type(7, {
            "content": "类型修改后的全新版本正文。",
            "importance": 7,
            "recall_tags": ["经历"],
            "continuity_type": "episode",
            "continuity_data": EPISODE_DATA,
        })
        self.assertEqual(result["memory_id"], 9)
        self.assertEqual(result["previous_version_id"], 7)
        self.assertEqual(result["removed_version_id"], 6)
        payload = self._last_call("change_memory_type_v1")
        self.assertEqual(payload["p_memory_id"], 7)
        self.assertEqual(payload["p_continuity_type"], "episode")
        self.assertEqual(payload["p_content_hash"], sha256("类型修改后的全新版本正文。"))
        self.assertNotIn("p_source", payload)

    def test_same_type_change_is_rejected_by_the_rpc(self):
        # 当前类型保存在数据库行里，同类型拒绝发生在事务 RPC 中；服务层只
        # 负责把稳定错误码映射为中文信息。
        self.client.outcomes["change_memory_type_v1"] = _rpc_error(
            "admin_memory_type_unchanged"
        )
        with self.assertRaises(AdminMemoryError) as ctx:
            admin_memory.change_memory_type(7, {
                "content": "同类型不允许走类型修改流程。",
                "continuity_type": "moment",
                "continuity_data": MOMENT_DATA,
            })
        self.assertEqual(ctx.exception.code, "admin_memory_type_unchanged")
        self.assertEqual(len(self.client.calls), 1)

    def test_rpc_failure_raises_and_sends_nothing_else(self):
        self.client.outcomes["change_memory_type_v1"] = _rpc_error(
            "admin_memory_content_exists"
        )
        with self.assertRaises(AdminMemoryError) as ctx:
            admin_memory.change_memory_type(7, {
                "content": "与现有内容重复的类型修改。",
                "continuity_type": "episode",
                "continuity_data": EPISODE_DATA,
            })
        self.assertEqual(ctx.exception.code, "admin_memory_content_exists")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(len(self.client.calls), 1)


class UndoAndRestoreTests(ServiceTestCase):
    def test_undo_calls_dedicated_rpc(self):
        self.client.outcomes["undo_memory_type_change_v1"] = {
            "restored_memory_id": 6, "undo_memory_id": 9, "undo_deleted": True,
        }
        result = admin_memory.undo_memory_type_change(9)
        self.assertEqual(result["restored_memory_id"], 6)
        self.assertTrue(result["undo_deleted"])
        payload = self._last_call("undo_memory_type_change_v1")
        self.assertEqual(payload, {"p_memory_id": 9})

    def test_undo_conflict_maps_to_clear_error(self):
        self.client.outcomes["undo_memory_type_change_v1"] = _rpc_error(
            "admin_memory_previous_conflict"
        )
        with self.assertRaises(AdminMemoryError) as ctx:
            admin_memory.undo_memory_type_change(9)
        self.assertEqual(ctx.exception.code, "admin_memory_previous_conflict")
        self.assertIn("两个版本同时生效", str(ctx.exception))

    def test_undo_without_previous_version_is_rejected(self):
        self.client.outcomes["undo_memory_type_change_v1"] = _rpc_error(
            "admin_memory_no_previous_version"
        )
        with self.assertRaises(AdminMemoryError) as ctx:
            admin_memory.undo_memory_type_change(9)
        self.assertEqual(ctx.exception.code, "admin_memory_no_previous_version")

    def test_undo_archived_fallback_is_reported_not_hidden(self):
        self.client.outcomes["undo_memory_type_change_v1"] = {
            "restored_memory_id": 6, "undo_memory_id": 9, "undo_deleted": False,
        }
        result = admin_memory.undo_memory_type_change(9)
        self.assertFalse(result["undo_deleted"])

    def test_archive_calls_dedicated_rpc_and_maps_codes(self):
        self.client.outcomes["archive_admin_memory_v1"] = {
            "memory": {"id": 8, "is_active": False, "heat": 60.0},
        }
        result = admin_memory.archive_admin_memory(8)
        self.assertFalse(result["is_active"])
        # 归档只翻 is_active，不动 heat：服务端返回什么就透传什么。
        self.assertEqual(result["memory"]["heat"], 60.0)
        payload = self._last_call("archive_admin_memory_v1")
        self.assertEqual(payload, {"p_memory_id": 8})

        for code, status in (
            ("admin_memory_superseded", 409),
            ("admin_memory_already_archived", 409),
            ("admin_memory_not_archivable", 409),
            ("admin_memory_not_found", 404),
        ):
            with self.subTest(code=code):
                self.client.outcomes["archive_admin_memory_v1"] = _rpc_error(code)
                self.client.calls.clear()
                with self.assertRaises(AdminMemoryError) as ctx:
                    admin_memory.archive_admin_memory(8)
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(ctx.exception.status_code, status)

    def test_restore_not_archived_maps_to_stable_code(self):
        self.client.outcomes["restore_archived_memory_v1"] = _rpc_error(
            "admin_memory_not_archived"
        )
        with self.assertRaises(AdminMemoryError) as ctx:
            admin_memory.restore_archived_memory(8)
        self.assertEqual(ctx.exception.code, "admin_memory_not_archived")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_restore_resets_heat_and_maps_conflicts(self):
        self.client.outcomes["restore_archived_memory_v1"] = {
            "memory": {"id": 8, "heat": 50.0, "is_active": True},
        }
        result = admin_memory.restore_archived_memory(8)
        self.assertEqual(result["heat"], 50.0)
        payload = self._last_call("restore_archived_memory_v1")
        self.assertEqual(payload, {"p_memory_id": 8})

        for code, status in (
            ("admin_memory_superseded", 409),
            ("admin_memory_continuity_conflict", 409),
            ("admin_memory_key_conflict", 409),
            ("admin_memory_chain_conflict", 409),
        ):
            with self.subTest(code=code):
                self.client.outcomes["restore_archived_memory_v1"] = _rpc_error(code)
                self.client.calls.clear()
                with self.assertRaises(AdminMemoryError) as ctx:
                    admin_memory.restore_archived_memory(8)
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(ctx.exception.status_code, status)


if __name__ == "__main__":
    unittest.main()
