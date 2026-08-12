import json
import importlib.util
import sys
import types
import unittest
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Keep the unit suite runnable in a bare Python environment. Production still
# installs these packages from requirements.txt; the tests replace all network
# behavior with mocks and only need import-time placeholders.
if importlib.util.find_spec("dotenv") is None:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv

if importlib.util.find_spec("httpx") is None:
    httpx = types.ModuleType("httpx")
    httpx.Client = object
    sys.modules["httpx"] = httpx

from gateway.config import cfg
from gateway.memory_extract import (
    DigestPipelineError,
    _clean_message_content,
    _conversation_context,
    _extract_memories,
    _get_embedding_sync,
    _parse_embedded_timestamp,
    _parse_model_output,
    _resolve_message_time,
    run_memory_digest,
    run_scheduled_digest_if_due,
)


MODULE = "gateway.memory_extract"


def _http_client_returning(response):
    client = MagicMock()
    client.__enter__.return_value.post.return_value = response
    return client


class ModelBoundaryTests(unittest.TestCase):
    def test_scheduled_digest_does_nothing_when_analysis_is_not_configured(self):
        with (
            patch.object(cfg, "ANALYSIS_API_KEY", ""),
            patch(f"{MODULE}._mark_stale_runs") as mark_stale,
            patch(f"{MODULE}.get_digest_status") as get_status,
            patch(f"{MODULE}.run_memory_digest") as run_digest,
        ):
            self.assertIsNone(run_scheduled_digest_if_due())

        mark_stale.assert_not_called()
        get_status.assert_not_called()
        run_digest.assert_not_called()

    def test_manual_digest_fails_before_database_access_when_not_configured(self):
        with (
            patch.object(cfg, "ANALYSIS_API_KEY", ""),
            patch(f"{MODULE}._mark_stale_runs") as mark_stale,
            patch(f"{MODULE}._create_run") as create_run,
        ):
            with self.assertRaises(DigestPipelineError) as raised:
                run_memory_digest("manual_preview", "preview")

        self.assertEqual(raised.exception.code, "analysis_not_configured")
        mark_stale.assert_not_called()
        create_run.assert_not_called()

    def test_extraction_http_error_keeps_safe_response_excerpt(self):
        response = MagicMock(status_code=429, text="rate limited")
        with (
            patch.object(cfg, "ANALYSIS_API_KEY", "configured"),
            patch(f"{MODULE}.httpx.Client", return_value=_http_client_returning(response)),
        ):
            with self.assertRaises(DigestPipelineError) as raised:
                _extract_memories("user: remember this")

        self.assertEqual(raised.exception.code, "model_http_error")
        self.assertEqual(raised.exception.model_output, "rate limited")

    def test_extraction_rejects_invalid_http_json(self):
        response = MagicMock(status_code=200, text="not-json")
        response.json.side_effect = ValueError("invalid json")
        with (
            patch.object(cfg, "ANALYSIS_API_KEY", "configured"),
            patch(f"{MODULE}.httpx.Client", return_value=_http_client_returning(response)),
        ):
            with self.assertRaises(DigestPipelineError) as raised:
                _extract_memories("user: remember this")

        self.assertEqual(raised.exception.code, "model_response_error")

    def test_extraction_rejects_invalid_model_content_json(self):
        response = MagicMock(status_code=200, text="provider response")
        response.json.return_value = {
            "choices": [{"message": {"content": "not-json"}}],
        }
        with (
            patch.object(cfg, "ANALYSIS_API_KEY", "configured"),
            patch(f"{MODULE}.httpx.Client", return_value=_http_client_returning(response)),
        ):
            with self.assertRaises(DigestPipelineError) as raised:
                _extract_memories("user: remember this")

        self.assertEqual(raised.exception.code, "model_parse_error")

    def test_embedding_http_error_is_not_silently_ignored(self):
        response = MagicMock(status_code=503, text="embedding unavailable")
        with (
            patch.object(cfg, "ANALYSIS_API_KEY", "configured"),
            patch(f"{MODULE}.httpx.Client", return_value=_http_client_returning(response)),
        ):
            with self.assertRaises(DigestPipelineError) as raised:
                _get_embedding_sync("durable memory")

        self.assertEqual(raised.exception.code, "embedding_http_error")
        self.assertEqual(raised.exception.model_output, "embedding unavailable")

    def test_embedding_request_uses_supported_model_and_database_dimension(self):
        response = MagicMock(status_code=200, text="provider response")
        response.json.return_value = {"data": [{"embedding": [0.1, 0.2]}]}
        client = _http_client_returning(response)
        with (
            patch.object(cfg, "ANALYSIS_API_KEY", "configured"),
            patch(f"{MODULE}.httpx.Client", return_value=client),
        ):
            self.assertEqual(_get_embedding_sync("durable memory"), [0.1, 0.2])

        request = client.__enter__.return_value.post.call_args.kwargs
        self.assertEqual(request["json"]["model"], "Qwen/Qwen3-Embedding-0.6B")
        self.assertEqual(request["json"]["dimensions"], 1024)

    def test_content_hash_is_stable_and_case_insensitive_for_deduplication(self):
        payload = {
            "memories": [
                {"content": "User prefers quiet mornings", "title": "Morning preference"},
                {"content": "user prefers quiet mornings", "title": "Duplicate"},
            ],
        }

        memories = _parse_model_output(json.dumps(payload))

        self.assertEqual(len(memories), 1)
        self.assertEqual(len(memories[0]["content_hash"]), 64)
        self.assertEqual(memories[0]["memory_type"], "other")
        self.assertEqual(memories[0]["update_mode"], "append")
        self.assertIsNone(memories[0]["memory_key"])
        self.assertEqual(memories[0]["tags"], ["长期记忆"])
        self.assertNotIn("reason", memories[0])

    def test_parser_keeps_semantic_type_separate_from_replace_strategy(self):
        payload = {
            "memories": [{
                "content": "User and friend A are currently on tense terms",
                "title": "Relationship with A is tense",
                "memory_type": "RELATIONSHIP",
                "update_mode": "REPLACE",
                "memory_key": " Relationship.A.State ",
            }],
        }

        memory = _parse_model_output(json.dumps(payload))[0]

        self.assertEqual(memory["memory_type"], "relationship")
        self.assertEqual(memory["update_mode"], "replace")
        self.assertEqual(memory["memory_key"], "relationship.a.state")
        self.assertEqual(memory["tags"], ["人物关系"])

    def test_invalid_replace_key_is_safely_downgraded_to_append(self):
        payload = {
            "memories": [{
                "content": "User's current project progress is sixty percent",
                "memory_type": "goal",
                "update_mode": "replace",
                "memory_key": "项目进度",
            }],
        }

        memory = _parse_model_output(json.dumps(payload))[0]

        self.assertEqual(memory["memory_type"], "goal")
        self.assertEqual(memory["update_mode"], "append")
        self.assertIsNone(memory["memory_key"])

    def test_evidence_ids_are_checked_and_times_are_normalized(self):
        payload = {
            "memories": [{
                "content": "User argued with friend A yesterday",
                "memory_type": "event",
                "update_mode": "append",
                "evidence_message_ids": [11, 11, 999],
                "memory_time": "2026-08-03",
                "time_precision": "day",
            }],
        }

        memory = _parse_model_output(
            json.dumps(payload),
            {11: "2026-08-04T21:35+08:00", 12: "2026-08-04T21:36+08:00"},
        )[0]

        self.assertEqual(memory["evidence_message_ids"], [11])
        self.assertEqual(memory["source_time"], "2026-08-04T21:35+08:00")
        self.assertEqual(memory["memory_time"], "2026-08-03")
        self.assertEqual(memory["time_precision"], "day")

    def test_candidate_without_valid_evidence_is_dropped_when_context_is_known(self):
        payload = {
            "memories": [{
                "content": "Unsupported model invention",
                "evidence_message_ids": [999],
            }],
        }

        self.assertEqual(
            _parse_model_output(
                json.dumps(payload),
                {11: "2026-08-04T21:35+08:00"},
            ),
            [],
        )

    def test_embedded_timestamp_is_extracted_before_being_removed_from_text(self):
        content = "26.08.04 21:35\n这是实际回复内容"

        parsed = _parse_embedded_timestamp(content)

        self.assertEqual(parsed.isoformat(timespec="minutes"), "2026-08-04T21:35+08:00")
        self.assertEqual(_resolve_message_time(None, content), "2026-08-04T21:35+08:00")
        self.assertEqual(_clean_message_content("assistant", content), "这是实际回复内容")

    def test_database_timestamp_is_normalized_to_beijing_time(self):
        self.assertEqual(
            _resolve_message_time("2026-08-04T13:35:00Z", "没有内嵌时间"),
            "2026-08-04T21:35+08:00",
        )
        self.assertEqual(
            _resolve_message_time("2026-08-04 21:35:00", "没有内嵌时间"),
            "2026-08-04T21:35+08:00",
        )

    def test_user_turn_without_time_does_not_borrow_following_assistant_timestamp(self):
        conversation, source_times = _conversation_context([
            {
                "id": 11,
                "role": "user",
                "content": "昨天和 A 吵架了",
                "_cleaned_content": "昨天和 A 吵架了",
            },
            {
                "id": 12,
                "role": "assistant",
                "content": "26.08.04 21:35\n我知道了",
                "_cleaned_content": "我知道了",
            },
        ])

        self.assertIsNone(source_times[11])
        self.assertEqual(source_times[12], "2026-08-04T21:35+08:00")
        self.assertIn("[id=11 t=unknown role=user]", conversation)
        self.assertNotIn("26.08.04", conversation)

    def test_consecutive_assistants_are_merged_only_within_same_conversation(self):
        conversation, source_times = _conversation_context([
            {
                "id": 10,
                "role": "assistant",
                "content": "first retry",
                "_cleaned_content": "first retry",
                "conversation_id": "conv-a",
            },
            {
                "id": 11,
                "role": "assistant",
                "content": "final in conv-a",
                "_cleaned_content": "final in conv-a",
                "conversation_id": "conv-a",
            },
            {
                "id": 12,
                "role": "assistant",
                "content": "different conv",
                "_cleaned_content": "different conv",
                "conversation_id": "conv-b",
            },
        ])

        self.assertNotIn("[id=10", conversation)
        self.assertIn("[id=11 t=unknown role=assistant] final in conv-a", conversation)
        self.assertIn("[id=12 t=unknown role=assistant] different conv", conversation)
    def test_append_candidates_never_keep_a_model_supplied_memory_key(self):
        payload = {
            "memories": [{
                "content": "User argued with friend A today",
                "memory_type": "event",
                "update_mode": "append",
                "memory_key": "relationship.a.state",
            }],
        }

        memory = _parse_model_output(json.dumps(payload))[0]

        self.assertEqual(memory["memory_type"], "event")
        self.assertEqual(memory["update_mode"], "append")
        self.assertIsNone(memory["memory_key"])


class _FailingRpc:
    def execute(self):
        raise RuntimeError("atomic commit failed")


class _SuccessfulRpc:
    def __init__(self, inserted_count):
        self.inserted_count = inserted_count

    def execute(self):
        return SimpleNamespace(data=self.inserted_count)


class _UpdateQuery:
    def __init__(self, client, payload):
        self.client = client
        self.payload = payload

    def eq(self, *_args):
        return self

    def execute(self):
        self.client.updates.append(self.payload)
        return SimpleNamespace(data=[{**self.client.run, **self.payload}])


class _DigestClient:
    def __init__(self, run, commit_succeeds=False):
        self.run = run
        self.commit_succeeds = commit_succeeds
        self.rpc_calls = []
        self.updates = []

    def rpc(self, name, payload):
        self.rpc_calls.append((name, payload))
        if self.commit_succeeds:
            self.run = {
                **self.run,
                "status": "succeeded",
                "extracted_count": len(payload["p_memories"]),
                "inserted_count": len(payload["p_memories"]),
            }
            return _SuccessfulRpc(len(payload["p_memories"]))
        return _FailingRpc()

    def table(self, name):
        if name != "memory_digest_runs":
            raise AssertionError(f"unexpected table write: {name}")
        return self

    def update(self, payload):
        return _UpdateQuery(self, payload)

    def select(self, *_args):
        return self

    def eq(self, *_args):
        return self

    def limit(self, *_args):
        return self

    def execute(self):
        return SimpleNamespace(data=[self.run])


class AtomicCommitTests(unittest.TestCase):
    def setUp(self):
        self.messages = [
            {
                "id": 11,
                "role": "user",
                "content": "remember this",
                "_cleaned_content": "remember this",
            },
            {
                "id": 12,
                "role": "assistant",
                "content": "understood",
                "_cleaned_content": "understood",
            },
        ]
        self.run = {
            "id": 7,
            "assistant_id": "assistant-1",
            "trigger": "manual_execute",
            "mode": "execute",
            "status": "running",
            "source_first_message_id": 11,
            "source_last_message_id": 12,
            "message_count": 2,
        }
        self.memory = _parse_model_output(json.dumps({
            "memories": [{
                "content": "User prefers quiet mornings",
                "title": "Preference",
                "memory_type": "preference",
                "update_mode": "append",
            }],
        }))[0]

    @contextmanager
    def _pipeline_patches(self, client, *, mode="execute", embedding=None):
        run = {**self.run, "mode": mode}
        extract = patch(
            f"{MODULE}._extract_memories",
            return_value=([self.memory], '{"memories":[]}'),
        )
        if embedding is None:
            embedding = patch(
                f"{MODULE}._get_embedding_sync",
                return_value=[0.1, 0.2],
            )
        patches = (
            patch.object(cfg, "ANALYSIS_API_KEY", "configured"),
            patch(f"{MODULE}._mark_stale_runs"),
            patch(f"{MODULE}.resolve_assistant_id", return_value="assistant-1"),
            patch(f"{MODULE}._fetch_batch", return_value=(10, self.messages)),
            patch(f"{MODULE}._claim_slot", return_value={"status": "claimed", "run_id": 7}),
            patch(f"{MODULE}._update_heartbeat"),
            extract,
            embedding,
            patch(f"{MODULE}._client", return_value=client),
        )
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            yield

    def test_preview_succeeds_without_embedding_commit_or_cursor_advance(self):
        client = _DigestClient({**self.run, "mode": "preview"})
        embedding = patch(f"{MODULE}._get_embedding_sync")

        with self._pipeline_patches(client, mode="preview", embedding=embedding):
            result = run_memory_digest("manual_preview", "preview")

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["cursor_before"], 10)
        self.assertEqual(result["cursor_after"], 10)
        self.assertEqual(client.rpc_calls, [])
        self.assertNotIn("content_hash", result["preview_memories"][0])
        self.assertEqual(result["preview_memories"][0]["memory_type"], "preference")
        self.assertEqual(result["preview_memories"][0]["update_mode"], "append")

    def test_embedding_failure_records_specific_error_without_committing(self):
        client = _DigestClient(self.run)
        embedding_error = DigestPipelineError(
            "embedding_http_error",
            "Embedding model returned HTTP 503",
        )
        embedding = patch(
            f"{MODULE}._get_embedding_sync",
            side_effect=embedding_error,
        )

        with self._pipeline_patches(client, embedding=embedding):
            result = run_memory_digest("manual_execute", "execute")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "embedding_http_error")
        self.assertEqual(result["cursor_before"], 10)
        self.assertEqual(result["cursor_after"], 10)
        self.assertEqual(client.rpc_calls, [])

    def test_successful_atomic_commit_advances_cursor_to_last_source_message(self):
        client = _DigestClient(self.run, commit_succeeds=True)

        with self._pipeline_patches(client):
            result = run_memory_digest("manual_execute", "execute")

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["cursor_before"], 10)
        self.assertEqual(result["cursor_after"], 12)
        self.assertEqual(result["inserted_count"], 1)
        committed_memory = client.rpc_calls[0][1]["p_memories"][0]
        self.assertEqual(committed_memory["content_hash"], self.memory["content_hash"])
        self.assertEqual(committed_memory["embedding"], [0.1, 0.2])

    def test_atomic_commit_failure_records_error_and_preserves_cursor(self):
        client = _DigestClient(self.run)

        with self._pipeline_patches(client), patch(f"{MODULE}.log.exception"):
            result = run_memory_digest("manual_execute", "execute")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "pipeline_error")
        self.assertEqual(result["cursor_before"], 10)
        self.assertEqual(result["cursor_after"], 10)
        self.assertEqual(client.rpc_calls[0][0], "commit_memory_digest_run")
        committed_memory = client.rpc_calls[0][1]["p_memories"][0]
        self.assertEqual(committed_memory["content_hash"], self.memory["content_hash"])
        self.assertEqual(committed_memory["embedding"], [0.1, 0.2])
        self.assertEqual(client.updates[-1]["status"], "failed")


    def test_extraction_retries_without_response_format_on_provider_400(self):
        first_response = MagicMock(status_code=400, text='unsupported response_format type json_object')
        second_response = MagicMock(status_code=200, text='ok')
        second_response.json.return_value = {
            "choices": [{"message": {"content": '{"memories":[]}'}}],
        }

        call_count = [0]
        def _post(*args, **kwargs):
            idx = call_count[0]
            call_count[0] += 1
            return [first_response, second_response][idx]

        mock_client = MagicMock()
        mock_client.__enter__.return_value.post = _post

        with (
            patch.object(cfg, "ANALYSIS_API_KEY", "configured"),
            patch(f"{MODULE}.httpx.Client", return_value=mock_client),
        ):
            memories, raw = _extract_memories("user: hello")

        self.assertEqual(memories, [])
        self.assertEqual(call_count[0], 2)

    def test_scheduled_digest_persists_error_on_exception(self):
        with (
            patch.object(cfg, "ANALYSIS_API_KEY", "configured"),
            patch(f"{MODULE}._mark_stale_runs"),
            patch(f"{MODULE}.resolve_assistant_id", return_value="assistant-1"),
            patch(f"{MODULE}.get_digest_status", side_effect=RuntimeError("db down")),
            patch(f"{MODULE}._create_run", return_value={
                "id": 99,
                "assistant_id": "assistant-1",
                "trigger": "scheduled_daily",
                "mode": "execute",
                "status": "failed",
                "error_code": "scheduled_check_error",
            }) as create_run,
        ):
            result = run_scheduled_digest_if_due()

        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "scheduled_check_error")
        create_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
