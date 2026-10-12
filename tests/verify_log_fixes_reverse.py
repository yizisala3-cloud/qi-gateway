"""修复后反向验收：逐场景核对复审探针的缺陷取值已反转。

与复审 reproduce_backend.py 同场景、断言相反（缺陷不存在）。仅本地
验证用，不属于 pytest 套件；正式回归以 tests/test_gateway_log_*.py 为准。
"""
import io
import json
import logging
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(ROOT))

from gateway import logging_events as le
from gateway.config import cfg
from postgrest.exceptions import APIError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient


@contextmanager
def capture(platform=True):
    le.uninstall_handler()
    le._queue.drain(10000)
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    stream = io.StringIO()
    root.handlers = [logging.StreamHandler(stream)] if platform else []
    root.setLevel(logging.INFO)
    with mock.patch.object(cfg, "GATEWAY_LOG_ENABLED", True):
        le.install_handler()
        try:
            yield stream
        finally:
            le.uninstall_handler()
            root.handlers = handlers
            root.setLevel(level)
            le._queue.drain(10000)


result = {}

# BUG-01：短配置密钥脱敏
with capture() as stream:
    with mock.patch.object(cfg, "GATEWAY_TOKEN", "qT9!x"):
        logging.getLogger("gateway.chat").error("rejected credential qT9!x")
        event = le._queue.drain(100)[0]
        result["short_configured_secret"] = {
            "raw_contains_secret": "qT9!x" in event["message_raw"],
            "platform_contains_secret": "qT9!x" in stream.getvalue(),
        }
assert not result["short_configured_secret"]["raw_contains_secret"]
assert not result["short_configured_secret"]["platform_contains_secret"]

# BUG-02：APIError message 回显 + 多行 row 回显
marker = "REREVIEW_PRIVATE_CHAT_BODY_123456"
with capture() as stream:
    try:
        raise APIError({"code": "22P02", "message":
                        'invalid input syntax for type uuid: "' + marker + '"',
                        "details": None, "hint": None})
    except APIError:
        logging.getLogger("gateway.memo").exception("memo RPC infrastructure failure")
    events = le._queue.drain(100)
    result["body_in_apierror_message"] = {
        "stack_contains_body": marker in events[0]["stack_trace"],
        "platform_contains_body": marker in stream.getvalue(),
    }
    with tempfile.TemporaryDirectory(prefix="reverse-body-") as directory:
        spool = le._SpoolStore(directory, "probe", 10, 100000)
        spool.write(events)
        persisted = "".join(p.read_text(encoding="utf-8") for p in Path(directory).glob("*.jsonl"))
        result["body_in_apierror_message"]["spool_contains_body"] = marker in persisted
assert not result["body_in_apierror_message"]["stack_contains_body"]
assert not result["body_in_apierror_message"]["platform_contains_body"]
assert not result["body_in_apierror_message"]["spool_contains_body"]

with capture() as stream:
    exc = RuntimeError("Failing row contains (42, first line\n" + marker + ")")
    logging.getLogger("gateway.memo").warning("memo RPC rejected: %s", exc)
    events = le._queue.drain(100)
    result["multiline_row_echo"] = {
        "raw_contains_body": marker in events[0]["message_raw"],
        "platform_contains_body": marker in stream.getvalue(),
    }
assert not result["multiline_row_echo"]["raw_contains_body"]
assert not result["multiline_row_echo"]["platform_contains_body"]

# BUG-20：平台脱敏不替换异常身份（log.exception → re-raise 只计 1 次）
def logged_reraise(secret):
    async def endpoint(request):
        try:
            raise RuntimeError("synthetic dependency failed " + secret)
        except RuntimeError:
            logging.getLogger("gateway.planning").exception("operation failed")
            raise

    with capture():
        with mock.patch.object(cfg, "GATEWAY_TOKEN", "REREVIEW_SECRET_123456"):
            app = Starlette(routes=[Route("/probe", endpoint)],
                            middleware=[Middleware(le.GatewayLogMiddleware)])
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/probe")
            events = le._queue.drain(100)
    return {"status": response.status_code, "count": len(events),
            "codes": [event["error_code"] for event in events]}


result["reraise_plain_control"] = logged_reraise("plain")
result["reraise_redacted_exception"] = logged_reraise("REREVIEW_SECRET_123456")
assert result["reraise_plain_control"]["count"] == 1
assert result["reraise_redacted_exception"]["count"] == 1

# BUG-21：脱敏副本保留异常链
with capture():
    try:
        try:
            raise ValueError("synthetic original cause Bearer abc123def456ghi789")
        except ValueError as cause:
            raise APIError({"code": "23502", "message": "constraint failure",
                            "details": "row metadata", "hint": None}) from cause
    except APIError:
        logging.getLogger("gateway.memo").exception("operation failed")
    event = le._queue.drain(100)[0]
    result["exception_chain_lost"] = {
        "contains_cause_type": "ValueError" in event["stack_trace"],
        "contains_cause_frames": "raise ValueError" in event["stack_trace"],
        "contains_cause_secret": "abc123def456ghi789" in event["stack_trace"],
    }
assert result["exception_chain_lost"]["contains_cause_type"]
assert result["exception_chain_lost"]["contains_cause_frames"]
assert not result["exception_chain_lost"]["contains_cause_secret"]

# BUG-18：context 捕获点保留异常类型与堆栈
from gateway import context as context_module
with capture():
    client = mock.MagicMock()
    (client.table.return_value.select.return_value.order.return_value
     .limit.return_value.execute.side_effect) = RuntimeError("synthetic recent source failure")
    with mock.patch.object(context_module.db, "get_client", return_value=client):
        returned = context_module.build_recent_chat_context()
    event = le._queue.drain(100)[0]
    result["caught_context_exception_lost"] = {
        "returned": returned, "outcome": event["outcome"],
        "exception_type": event["exception_type"],
        "has_stack": bool(event["stack_trace"]),
    }
assert result["caught_context_exception_lost"]["exception_type"] == "RuntimeError"
assert result["caught_context_exception_lost"]["has_stack"]

# BUG-10：quiet 双失败都标已提交后失败
from datetime import datetime, time, timezone
from gateway import planning_common as common
from gateway import planning_cycles as cycles
from gateway import planning_generation as generation
from gateway import planning_runtime as runtime
now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
planning_context = cycles.PlanningRequestContext(now)
planning_context._boundary = (time(3, 0), None, frozenset())
planning_context._values = {common.PLANNING_BOUNDARY_STATE_KEY: None,
                            common.PLANNING_DAILY_REFRESH_KEY: True}
task = {"id": 7, "is_active": True, "task_type": "interval",
        "refresh_mode": "fixed_interval", "time_mode": "rule"}
with capture():
    with mock.patch.object(runtime, "_require_client", return_value=object()), \
            mock.patch.object(runtime, "_fetch_task", return_value=task), \
            mock.patch.object(generation, "_fixed_rounds", return_value=[(now.date(), now)]), \
            mock.patch.object(generation, "_following_fixed_event", return_value=now), \
            mock.patch.object(generation, "_expire_fixed_rounds", side_effect=[0, RuntimeError("synthetic cleanup failure")]), \
            mock.patch.object(generation, "_create_occurrences", side_effect=ValueError("synthetic generation failure")):
        with generation.quiet_generation_scope():
            generated = generation.generate_task(7, now, context=planning_context)
    events = le._queue.drain(100)
    result["quiet_cleanup_outcome"] = {
        "errors": generated.get("errors"),
        "events": [{k: e[k] for k in ("operation", "outcome", "error_code", "exception_type")} for e in events],
    }
assert [e["outcome"] for e in result["quiet_cleanup_outcome"]["events"]] == \
    ["committed_side_effect_failed", "committed_side_effect_failed"]

# BUG-15：损坏清理后累计损失仍在状态里
with tempfile.TemporaryDirectory(prefix="reverse-purge-") as directory:
    spool = le._SpoolStore(directory, "probe", 1, 100000)
    spool._seq = 100
    Path(directory, "probe-000000000001.jsonl").write_text("not json\n", encoding="utf-8")
    spool.replay(lambda rows: None)
    spool.write([{"event_id": "synthetic-event"}])
    result["purged_corrupt_status"] = spool.snapshot()
assert result["purged_corrupt_status"]["corrupt_purged_total"] == 1
assert result["purged_corrupt_status"]["corrupt_files"] == 0
assert result["purged_corrupt_status"]["corrupt_total"] == 1  # 历史损失仍可见

# BUG-22：删除失败不虚增丢弃；写入前预留额度——实际文件数有界
with tempfile.TemporaryDirectory(prefix="reverse-delete-") as directory:
    spool = le._SpoolStore(directory, "probe", 1, 100000)
    spool._seq = 100
    spool.write([{"event_id": "synthetic-old"}])
    with mock.patch.object(le.os, "remove", side_effect=PermissionError("synthetic locked file")):
        first = spool.write([{"event_id": "synthetic-new-1"}])
        second = spool.write([{"event_id": "synthetic-new-2"}])
    result["spool_delete_failure"] = spool.snapshot()
    result["spool_delete_failure"]["actual_files"] = len(list(Path(directory).glob("*.jsonl")))
    result["spool_delete_failure"]["writes_rejected"] = [first, second]
assert result["spool_delete_failure"]["dropped_total"] == 0  # 仍可回放不计丢弃
assert result["spool_delete_failure"]["actual_files"] == 1   # 容量有界（BUG-22 验收）
assert first is False and second is False                    # 新批次明确拒绝

# BUG-02 残留：嵌套括号 / 内嵌引号整段移除
nested_marker = "REVERSE_NESTED_PRIVATE_BODY_123456"
with capture() as stream:
    logging.getLogger("gateway.memo").error(
        "Failing row contains (null, ordinary (aside) %s).", nested_marker)
    logging.getLogger("gateway.memo").error(
        'invalid input syntax for type uuid: "prefix" %s"', nested_marker)
    events = le._queue.drain(100)
    result["nested_delimiter_echo"] = {
        "raw_contains_body": any(nested_marker in e["message_raw"] for e in events),
        "platform_contains_body": nested_marker in stream.getvalue(),
    }
assert not result["nested_delimiter_echo"]["raw_contains_body"]
assert not result["nested_delimiter_echo"]["platform_contains_body"]

# BUG-21 残留：深度链与 ExceptionGroup 子异常不泄漏
deep_marker = "sk-reverse-deep-secret-1234567890"
with capture() as stream, mock.patch.object(cfg, "UPSTREAM_API_KEY", deep_marker):
    deep = RuntimeError("level0")
    for index in range(1, 11):
        wrapper = RuntimeError(f"wrap{index} {deep_marker}")
        wrapper.__cause__ = deep
        deep = wrapper
    try:
        raise deep
    except RuntimeError:
        logging.getLogger("gateway.memory").exception("deep chain failed")
    group = ExceptionGroup("group", [RuntimeError(f"child {deep_marker}")])
    try:
        raise group
    except ExceptionGroup:
        logging.getLogger("gateway.memory").exception("group failed")
    events = le._queue.drain(100)
    result["deep_and_group_leak"] = {
        "stack_contains_secret": any(deep_marker in e["stack_trace"] for e in events),
        "platform_contains_secret": deep_marker in stream.getvalue(),
        "types_kept": sorted({e["exception_type"] for e in events}),
    }
assert not result["deep_and_group_leak"]["stack_contains_secret"]
assert not result["deep_and_group_leak"]["platform_contains_secret"]
assert "ExceptionGroup" in result["deep_and_group_leak"]["types_kept"]

# BUG-25：默认 Uvicorn 命名 logger 的平台输出清洗
uvicorn_marker = "sk-reverse-uvicorn-secret-1234567890"
uvicorn_stream = io.StringIO()
uvicorn_logger = logging.getLogger("uvicorn.error")
uvicorn_handler = logging.StreamHandler(uvicorn_stream)
uvicorn_logger.addHandler(uvicorn_handler)
try:
    with mock.patch.object(cfg, "UPSTREAM_API_KEY", uvicorn_marker):
        with capture():
            uvicorn_logger.error("upstream failed with %s", uvicorn_marker)
finally:
    uvicorn_logger.removeHandler(uvicorn_handler)
result["uvicorn_platform_output"] = {
    "contains_secret": uvicorn_marker in uvicorn_stream.getvalue(),
    "filter_count": sum(1 for h in uvicorn_logger.handlers
                        if le._scrub_filter in h.filters) if le._scrub_filter else 0,
}
assert not result["uvicorn_platform_output"]["contains_secret"]

# BUG-26：聊天 / SSE 错误出口不回传敏感原文（真实 main.app + mock 上游）
from gateway import main as gateway_main


class FakeResponse:
    def __init__(self, status_code, content):
        self.status_code = status_code
        self.content = content


class FakeClient:
    def __init__(self, behavior):
        self._behavior = behavior

    async def post(self, *args, **kwargs):
        if isinstance(self._behavior, Exception):
            raise self._behavior
        return self._behavior

    def stream(self, *args, **kwargs):
        behavior = self._behavior
        outer = self

        class _Ctx:
            async def __aenter__(self):
                if isinstance(behavior, Exception):
                    raise behavior
                return behavior

            async def __aexit__(self, *a):
                return False
        return _Ctx()


chat_marker = "sk-reverse-chat-echo-1234567890"
with mock.patch.object(cfg, "GATEWAY_TOKEN", "reverse-chat-token"), \
        mock.patch.object(cfg, "GATEWAY_LOG_ENABLED", True), \
        mock.patch.object(cfg, "GEMINI_BROWSER_TOOL_COMPAT_ENABLED", False), \
        mock.patch.object(cfg, "UPSTREAM_API_KEY", chat_marker):
    # 同一 TestClient 会话内完成两个探针（lifespan 只跑一次）
    with TestClient(gateway_main.app, raise_server_exceptions=False) as client:
        with mock.patch.object(gateway_main, "http_client", FakeClient(FakeResponse(
                401, json.dumps({"error": f"Incorrect API key: {chat_marker}"}).encode()))):
            response = client.post(
                "/v1/chat/completions",
                json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": "Bearer reverse-chat-token"})
        result["chat_error_body"] = {
            "status": response.status_code,
            "contains_secret": chat_marker in response.text,
        }
        with mock.patch.object(gateway_main, "http_client", FakeClient(
                RuntimeError(f"stream broke {chat_marker}"))):
            stream_response = client.post(
                "/v1/chat/completions",
                json={"model": "m", "stream": True,
                      "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": "Bearer reverse-chat-token"})
        result["sse_interrupt_text"] = {
            "status": stream_response.status_code,
            "contains_secret": chat_marker in stream_response.text,
        }
assert response.status_code == 401
assert not result["chat_error_body"]["contains_secret"]
assert not result["sse_interrupt_text"]["contains_secret"]

# BUG-27：记忆失败记录入库前清洗（模拟 client 记录 update 载荷）
from gateway import memory_rumination


class CaptureClient:
    def __init__(self):
        self.updates = []

    def table(self, name):
        return self

    def update(self, payload):
        self.payload = payload
        return self

    def eq(self, *a, **k):
        return self

    def execute(self):
        self.updates.append(self.payload)
        class Row:
            data = [dict(self.payload, id=1)]
        return Row()


memory_marker = "sk-reverse-memory-1234567890"
capture_client = CaptureClient()
with mock.patch.object(memory_rumination, "get_client", lambda: capture_client), \
        mock.patch.object(cfg, "UPSTREAM_API_KEY", memory_marker):
    memory_rumination._mark_failed(1, "pipeline_error", f"failure {memory_marker}")
    projected = memory_rumination._public_run(
        {"id": 1, "error_message": f"legacy {memory_marker}"})
result["memory_failure_record"] = {
    "stored_contains_secret": any(
        memory_marker in p.get("error_message", "") for p in capture_client.updates),
    "projection_contains_secret": memory_marker in projected["error_message"],
}
assert not result["memory_failure_record"]["stored_contains_secret"]
assert not result["memory_failure_record"]["projection_contains_secret"]

# TASK-02：两把密钥同故障分组不同；标识秘密不泄漏
with mock.patch.object(cfg, "GATEWAY_KEY_REF_SECRET", "r" * 40):
    ref_a = le.compute_key_ref("reverse-key-A")
    ref_b = le.compute_key_ref("reverse-key-B")
    result["key_ref_identity"] = {
        "same_value_same_ref": le.compute_key_ref("reverse-key-A") == ref_a,
        "diff_value_diff_ref": ref_a != ref_b,
        "label": le.key_ref_label(ref_a),
    }
    with capture():
        for key in ("reverse-key-A", "reverse-key-B"):
            with le.credential_scope("chat", "api.example.com", key):
                logging.getLogger("gateway.chat").error("chat failed")
        events = le._queue.drain(100)
    result["key_ref_identity"]["group_count"] = len({e["fingerprint"] for e in events})
    result["key_ref_identity"]["refs_on_events"] = sorted({e["key_ref"] for e in events})
assert result["key_ref_identity"]["same_value_same_ref"]
assert result["key_ref_identity"]["diff_value_diff_ref"]
assert result["key_ref_identity"]["group_count"] == 2
assert set(result["key_ref_identity"]["refs_on_events"]) == {ref_a, ref_b}

# BUG-23：重试上限 0 严格执行
class DownStore:
    def insert_events(self, rows):
        raise RuntimeError("synthetic db down")


with mock.patch.object(cfg, "GATEWAY_LOG_RETRY_PENDING_MAX", 0), \
        mock.patch.object(cfg, "GATEWAY_LOG_SPOOL_DIR", ""):
    queue = le._EventQueue(100)
    writer = le._LogWriter(DownStore(), queue)
    writer._write_batch([{"event_id": "synthetic-" + str(i)} for i in range(50)])
    result["retry_limit_zero"] = {"configured": cfg.GATEWAY_LOG_RETRY_PENDING_MAX,
                                  "retained": writer._retry_pending_events,
                                  "dropped": queue.dropped_total}
assert result["retry_limit_zero"]["retained"] == 0
assert result["retry_limit_zero"]["dropped"] == 50

# BUG-24：spool 过期事件不回放
with tempfile.TemporaryDirectory(prefix="reverse-retention-") as directory:
    from datetime import timedelta
    old_event = {"event_id": "synthetic-expired",
                 "occurred_at": (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()}
    spool = le._SpoolStore(directory, "probe", 10, 100000)
    spool.write([old_event])
    spool._enforce_bounds()
    retained = spool.snapshot()["files"]
    replayed = []
    with mock.patch.object(cfg, "GATEWAY_LOG_RETENTION_DAYS", 30):
        spool.replay(lambda rows: replayed.extend(rows))
    result["spool_retention"] = {"retention_days": 30, "event_age_days": 40,
                                 "retained_files": retained,
                                 "expired_events_replayed": len(replayed),
                                 "expired_total": spool.snapshot()["expired_total"]}
assert result["spool_retention"]["expired_events_replayed"] == 0
assert result["spool_retention"]["expired_total"] == 1

out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).with_name("reverse-results.json")
out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(json.dumps(result, indent=2, ensure_ascii=False))
