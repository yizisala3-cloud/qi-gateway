"""Read-only shadow preview for continuity-oriented memory extraction.

This module deliberately has no dependency on the production digest cursor,
claim, heartbeat, embedding, commit, or audit-run paths. ``chat_messages`` is
used as an immutable source and shadow results exist only in the HTTP response.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .config import cfg
from .db import get_client

log = logging.getLogger("gateway.memory_continuity_shadow")

CST = timezone(timedelta(hours=8))
DEFAULT_MAX_MESSAGES = 80
DEFAULT_MAX_CHARS = 16000
MAX_CANDIDATES = 6

CONTINUITY_TYPES = frozenset({
    "moment", "thread", "episode", "inside_joke", "relationship", "profile",
})
SUBJECTS = frozenset({"yezi", "qi", "shared", "project", "other"})
SOURCE_TYPES = frozenset({
    "natural_chat", "persona_prompt", "code", "document", "quote",
    "roleplay", "tool_result", "system_meta", "unknown",
})
THREAD_STATES = frozenset({"open", "paused", "resolved", "abandoned", "unknown"})
TIME_PRECISIONS = frozenset({"minute", "day", "approximate", "unknown"})
RETENTION_CLASSES = frozenset({"normal", "core"})
PARTICIPANTS = frozenset({"yezi", "qi", "other"})

EMBEDDED_TIMESTAMP_PATTERN = re.compile(
    r"(?m)^\s*(?P<year>\d{2}|\d{4})[.\-/](?P<month>\d{1,2})[.\-/](?P<day>\d{1,2})"
    r"\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?\s*$"
)

# Intentionally small and explainable. These patterns target obvious secrets,
# not arbitrary high-entropy text. Prompt-side exclusion remains the first line
# of defence.
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{12,}\b", re.IGNORECASE),
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{16,}\b", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"\b(?:api[_ -]?key|token|service[_ -]?role|password|passwd|密码|私钥)\b"
        r"\s*[:=：]\s*[\"']?\S{8,}",
        re.IGNORECASE,
    ),
)

SHADOW_SYSTEM_PROMPT = """你是“连续感记忆 Shadow Preview”提取器。结果只供人工观察，不会进入正式记忆。

阅读带 message id、conversation id、北京时间和 role 的聊天，提取最多 6 条能帮助“栖”在下一个聊天窗口自然承接“叶子”的连续感候选。优先少量完整的 episode 和尚未结束的 thread，不要把一句对话拆成许多事实碎片；没有合格候选时返回 {"candidates":[]}。

continuity_type：
- moment：近期共同片段，短期内有承接价值。
- thread：未结束的话题、约定、计划、承诺或悬念。
- episode：相对完整的共同经历，应合并成有上下文的故事片段。
- inside_joke：叶子和栖之间的内部梗、特殊称呼、暗号或反复引用的小事。
- relationship：反复出现的互动方式、共同约定、边界和理解。
- profile：叶子的稳定资料或长期偏好。

规则：
1. content 使用“叶子”和“栖”，不要写“用户”和“助手”。
2. 短期、亲密、暧昧或一般敏感内容，只要有连续感价值，可以抽象保留。不要仅因敏感而排除。
3. 严禁提取或复述 API Key、Token、service_role、密码、私钥、支付凭据或认证秘密。
4. evidence_message_ids 必须全部来自输入中真实存在且直接支持候选的 id，不得编造。
5. 栖单方面提出的建议不能成为叶子的事实。只有叶子明确接受、双方形成约定或已经实际执行，才可成为 shared thread/relationship。
6. 叶子粘贴的人设 Prompt、system prompt、代码、文档、引用、角色扮演或工具结果中的第一人称，不等于叶子的真实自述。
7. 代码、文档和工具结果可以形成 subject=project 的当前工作 thread，但其中的示例人物、示例偏好或第一人称不能成为叶子的 profile。
8. source_type 描述内容来源，不等同于数据库 role。只能是 natural_chat、persona_prompt、code、document、quote、roleplay、tool_result、system_meta、unknown。
9. subject 只能是 yezi、qi、shared、project、other。共同经历、约定和关系通常是 shared。
10. thread_state 仅在 continuity_type=thread 时使用 open、paused、resolved、abandoned、unknown；其他类型必须为 null。没有明确结束证据时不要臆断 resolved。
11. importance 表示内容本身的重要程度，1-10；continuity_value 表示对下一个窗口自然承接的直接价值，1-10。两者分别判断。
12. source_time 一律输出 null，由程序根据 evidence 计算。memory_time 仅在原文可靠支持实际发生或生效时间时填写，否则为 null。time_precision 只能是 minute、day、approximate、unknown。
13. profile 是 continuity_type；core 只通过 retention_class=core 表示实验性保留层级，不代表最终数据库结构。不要因为内容亲密、强烈或感人就自动标 core。
14. 不确定时降低 confidence、保守表达或不提取，不得补全精确事实。

只返回严格 JSON，不要 Markdown、解释或代码围栏：
{"candidates":[{"content":"...","continuity_type":"thread","subject":"shared","source_type":"natural_chat","thread_state":"open","importance":5,"continuity_value":9,"confidence":0.85,"evidence_message_ids":[123,124],"source_time":null,"memory_time":null,"time_precision":"unknown","title":"...","participants":["yezi","qi"],"reason":"...","retention_class":"normal"}]}"""


class ShadowPreviewError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _analysis_configured() -> bool:
    return bool(
        cfg.ANALYSIS_BASE_URL.strip()
        and cfg.ANALYSIS_API_KEY.strip()
        and cfg.ANALYSIS_MODEL.strip()
    )


def _client():
    client = get_client()
    if not client:
        raise ShadowPreviewError("database_unavailable", "Supabase server client is unavailable")
    return client


def _parse_time(value: Any, assume_tz=timezone.utc) -> datetime | None:
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=assume_tz)
    return parsed


def _resolve_message_time(created_at: Any, content: Any) -> str | None:
    embedded = None
    match = EMBEDDED_TIMESTAMP_PATTERN.search(str(content or ""))
    if match:
        try:
            year = int(match.group("year"))
            embedded = datetime(
                year + 2000 if year < 100 else year,
                int(match.group("month")), int(match.group("day")),
                int(match.group("hour")), int(match.group("minute")),
                int(match.group("second") or 0), tzinfo=CST,
            )
        except ValueError:
            embedded = None

    database_time = _parse_time(created_at, CST)
    if database_time:
        database_time = database_time.astimezone(CST)
    chosen = database_time
    if embedded and database_time and abs((embedded - database_time).total_seconds()) <= 21600:
        chosen = embedded
    elif embedded and not database_time:
        chosen = embedded
    return chosen.isoformat(timespec="minutes") if chosen else None


def _clean_content(content: Any) -> str:
    text = str(content or "")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<<(?:delay|schedule|busy):.*?>>", "", text, flags=re.IGNORECASE)
    text = EMBEDDED_TIMESTAMP_PATTERN.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def resolve_shadow_assistant_id() -> str:
    configured = cfg.MEMORY_ASSISTANT_ID.strip()
    if configured:
        return configured
    try:
        response = (
            _client().table("chat_messages")
            .select("assistant_id")
            .order("id", desc=True)
            .limit(50)
            .execute()
        )
    except ShadowPreviewError:
        raise
    except Exception as exc:
        raise ShadowPreviewError("source_read_failed", "Failed to read chat message source") from exc
    for row in response.data or []:
        assistant_id = str(row.get("assistant_id") or "").strip()
        if assistant_id and assistant_id.casefold() != "test":
            return assistant_id
    # An entirely empty source is a valid preview with no candidates.
    return ""


def _normalize_messages(rows: list[dict[str, Any]], max_chars: int) -> tuple[list[dict[str, Any]], list[str]]:
    """Clean, fold assistant retries, and retain the newest messages in budget."""
    normalized: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: int(item.get("id") or 0)):
        role = str(row.get("role") or "").strip().casefold()
        if role not in {"user", "assistant"}:
            continue
        content = _clean_content(row.get("content"))
        if not content:
            continue
        item = {
            "id": int(row["id"]),
            "conversation_id": str(row.get("conversation_id") or ""),
            "role": role,
            "content": content,
            "source_time": _resolve_message_time(row.get("created_at"), row.get("content")),
        }
        if (
            role == "assistant"
            and normalized
            and normalized[-1]["role"] == "assistant"
            and normalized[-1]["conversation_id"] == item["conversation_id"]
        ):
            normalized[-1] = item
        else:
            normalized.append(item)

    selected_reversed: list[dict[str, Any]] = []
    used = 0
    for item in reversed(normalized):
        cost = len(item["content"]) + 100
        if selected_reversed and used + cost > max_chars:
            break
        copied = dict(item)
        if not selected_reversed and cost > max_chars:
            copied["content"] = copied["content"][:max(1, max_chars - 100)].rstrip()
            copied["truncated"] = True
            cost = len(copied["content"]) + 100
        selected_reversed.append(copied)
        used += cost

    selected = list(reversed(selected_reversed))
    warnings = ["oldest_messages_omitted_by_character_budget"] if len(selected) < len(normalized) else []
    if any(item.get("truncated") for item in selected):
        warnings.append("oversized_message_truncated")
    return selected, warnings


def fetch_recent_shadow_sample(
    assistant_id: str,
    max_messages: int = DEFAULT_MAX_MESSAGES,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        response = (
            _client().table("chat_messages")
            .select("id,assistant_id,conversation_id,role,content,created_at")
            .eq("assistant_id", assistant_id)
            .order("id", desc=True)
            .limit(max_messages)
            .execute()
        )
    except ShadowPreviewError:
        raise
    except Exception as exc:
        raise ShadowPreviewError("source_read_failed", "Failed to read chat message source") from exc
    return _normalize_messages(response.data or [], max_chars)


def _format_conversation(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    previous_conversation: str | None = None
    for message in messages:
        conversation_id = message["conversation_id"] or "unknown"
        if conversation_id != previous_conversation:
            lines.append(f"<conversation id={json.dumps(conversation_id, ensure_ascii=False)}>")
            previous_conversation = conversation_id
        source_time = message["source_time"] or "unknown"
        lines.append(
            f"[id={message['id']} t={source_time} role={message['role']}] {message['content']}"
        )
    return "\n".join(lines)


def _clamp(value: Any, minimum: float, maximum: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _contains_secret(*values: Any) -> bool:
    text = "\n".join(str(value or "") for value in values)
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def _normalize_memory_time(value: Any, precision: str) -> tuple[str | None, str]:
    raw = str(value or "").strip()
    clean_precision = precision if precision in TIME_PRECISIONS else "unknown"
    if not raw:
        return None, "unknown"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return None, "unknown"
        return raw, "day" if clean_precision == "unknown" else clean_precision
    parsed = _parse_time(raw, CST)
    if not parsed:
        return None, "unknown"
    return parsed.astimezone(CST).isoformat(timespec="minutes"), (
        "minute" if clean_precision == "unknown" else clean_precision
    )


def parse_shadow_output(
    text: str,
    evidence_times: dict[int, str | None],
) -> list[dict[str, Any]]:
    cleaned = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL | re.IGNORECASE).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ShadowPreviewError("model_parse_error", "Shadow model returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
        raise ShadowPreviewError("model_schema_error", "Shadow model JSON has no candidates array")

    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in payload["candidates"][:MAX_CANDIDATES]:
        if not isinstance(raw, dict):
            continue
        content = re.sub(r"\s+", " ", str(raw.get("content") or "")).strip()[:1000]
        if not content:
            continue
        title = re.sub(r"\s+", " ", str(raw.get("title") or "")).strip()[:120] or None
        reason = re.sub(r"\s+", " ", str(raw.get("reason") or "")).strip()[:400] or None
        if _contains_secret(content, title, reason):
            continue
        fingerprint = content.casefold()
        if fingerprint in seen:
            continue

        continuity_type = str(raw.get("continuity_type") or "").strip().casefold()
        subject = str(raw.get("subject") or "").strip().casefold()
        source_type = str(raw.get("source_type") or "").strip().casefold()
        if (
            continuity_type not in CONTINUITY_TYPES
            or subject not in SUBJECTS
            or source_type not in SOURCE_TYPES
        ):
            continue

        evidence_ids: list[int] = []
        for candidate in raw.get("evidence_message_ids") or []:
            if isinstance(candidate, bool):
                continue
            try:
                message_id = int(candidate)
            except (TypeError, ValueError):
                continue
            if message_id in evidence_times and message_id not in evidence_ids:
                evidence_ids.append(message_id)
        if not evidence_ids:
            continue

        valid_times = [evidence_times[item] for item in evidence_ids if evidence_times[item]]
        source_time = max(valid_times) if valid_times else None
        precision = str(raw.get("time_precision") or "unknown").strip().casefold()
        memory_time, time_precision = _normalize_memory_time(raw.get("memory_time"), precision)

        thread_state = str(raw.get("thread_state") or "unknown").strip().casefold()
        if continuity_type != "thread":
            thread_state = None
        elif thread_state not in THREAD_STATES:
            thread_state = "unknown"

        participants: list[str] = []
        for participant in raw.get("participants") or []:
            value = str(participant or "").strip().casefold()
            if value in PARTICIPANTS and value not in participants:
                participants.append(value)

        retention_class = str(raw.get("retention_class") or "normal").strip().casefold()
        if retention_class not in RETENTION_CLASSES:
            retention_class = "normal"

        item = {
            "content": content,
            "continuity_type": continuity_type,
            "subject": subject,
            "source_type": source_type,
            "thread_state": thread_state,
            "importance": int(round(_clamp(raw.get("importance"), 1, 10, 5))),
            "continuity_value": int(round(_clamp(raw.get("continuity_value"), 1, 10, 5))),
            "confidence": round(_clamp(raw.get("confidence"), 0, 1, 0.6), 3),
            "evidence_message_ids": evidence_ids,
            "source_time": source_time,
            "memory_time": memory_time,
            "time_precision": time_precision,
            "title": title,
            "participants": participants,
            "reason": reason,
            "retention_class": retention_class,
        }
        validated.append(item)
        seen.add(fingerprint)
    return validated


def _extract_shadow_candidates(
    conversation: str,
    evidence_times: dict[int, str | None],
) -> list[dict[str, Any]]:
    if not conversation.strip():
        return []
    if not _analysis_configured():
        raise ShadowPreviewError("analysis_not_configured", "The analysis model provider is not configured")

    url = f"{cfg.ANALYSIS_BASE_URL.rstrip('/')}/chat/completions"
    request_body = {
        "model": cfg.ANALYSIS_MODEL,
        "messages": [
            {"role": "system", "content": SHADOW_SYSTEM_PROMPT},
            {"role": "user", "content": f"<chat_log>\n{conversation}\n</chat_log>"},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 1800,
        "temperature": 0.1,
    }
    headers = {
        "Authorization": f"Bearer {cfg.ANALYSIS_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.post(url, headers=headers, json=request_body)
            if response.status_code == 400:
                excerpt = response.text[:500].casefold()
                if "response_format" in excerpt or "json_object" in excerpt:
                    request_body.pop("response_format", None)
                    response = client.post(url, headers=headers, json=request_body)
    except Exception as exc:
        raise ShadowPreviewError(
            "model_request_failed", f"Shadow model request failed: {type(exc).__name__}"
        ) from exc

    if response.status_code != 200:
        raise ShadowPreviewError("model_http_error", f"Shadow model returned HTTP {response.status_code}")
    try:
        output = response.json()["choices"][0]["message"]["content"]
        if not isinstance(output, str) or not output.strip():
            raise ValueError("empty model content")
    except Exception as exc:
        raise ShadowPreviewError("model_response_error", "Shadow model response shape is invalid") from exc
    return parse_shadow_output(output, evidence_times)


def run_shadow_preview(max_messages: int = DEFAULT_MAX_MESSAGES, max_chars: int = DEFAULT_MAX_CHARS) -> dict[str, Any]:
    assistant_id = resolve_shadow_assistant_id()
    messages, warnings = (
        fetch_recent_shadow_sample(assistant_id, max_messages, max_chars)
        if assistant_id else ([], [])
    )
    if not messages:
        return {
            "mode": "shadow_preview",
            "persistence": "none",
            "assistant_id": assistant_id,
            "source_first_message_id": None,
            "source_last_message_id": None,
            "message_count": 0,
            "candidates": [],
            "warnings": warnings,
        }

    if not _analysis_configured():
        raise ShadowPreviewError("analysis_not_configured", "The analysis model provider is not configured")
    evidence_times = {message["id"]: message["source_time"] for message in messages}
    candidates = _extract_shadow_candidates(_format_conversation(messages), evidence_times)
    log.info(
        "Continuity shadow preview completed: assistant_id=%s first_id=%s last_id=%s messages=%s candidates=%s",
        assistant_id, messages[0]["id"], messages[-1]["id"], len(messages), len(candidates),
    )
    return {
        "mode": "shadow_preview",
        "persistence": "none",
        "assistant_id": assistant_id,
        "source_first_message_id": messages[0]["id"],
        "source_last_message_id": messages[-1]["id"],
        "message_count": len(messages),
        "candidates": candidates,
        "warnings": warnings,
    }
