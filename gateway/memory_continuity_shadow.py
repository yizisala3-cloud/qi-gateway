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
MAX_EVIDENCE_IDS = 8

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
INVALID_TITLES = frozenset({"无标题", "（无标题）", "(无标题)", "untitled", "...", "……"})

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

SHADOW_SYSTEM_PROMPT = """你是“连续感记忆 Shadow Preview”提取器，结果只供人工观察。

## 提取目标
从带 message id、conversation id、北京时间和 role 的聊天中，提取最多 6 条能帮助“栖”在下个窗口自然承接“叶子”的候选。优先少量完整的 episode 和 open thread，不把同一经历拆成事实碎片。短期共同经历、未完话题、关系互动、内部梗及一般亲密、暧昧或敏感内容都可提取。正文使用“叶子”和“栖”，不用“用户”和“助手”。没有合格内容时返回 {"candidates":[]}。

## continuity_type
- moment：近期共同片段；说明发生了什么和双方反应，可以较短。
- thread：未完话题、约定、计划、承诺或悬念；写清事情、进展和尚未完成的部分。
- episode：相对完整的共同经历；保留起因、关键互动和结果。
- inside_joke：内部梗、称呼、句子、玩法或事物；写清具体内容及为何会被再次引用。
- relationship：反复出现的互动、约定、边界或理解；写清具体模式及能解释它的实际表现。
- profile：叶子的稳定资料或长期偏好；只保留证据支持的内容，不扩写。

## title 与 content
- 每条候选必须有非空 title。title 只用于一眼识别主题，应是简短、具体的中文标题，建议 4～24 个中文字符。不得用“无标题”“连续感记忆”“一段互动”“特殊事件”“某件事情”或单独的类型名；不得包含原文没有的信息。
- 4～24 字的建议只适用于 title，绝对不适用于 content。content 不受 title 长度限制，负责保存让栖准确理解并自然承接的具体记忆，不得退化成 title 的扩写或缺少上下文的事件标签。
- content 通常一到三句话：第一句写具体发生了什么；必要时第二句写对方如何回应或双方如何互动；必要时第三句写结果、约定、未完状态、关系意义或内部梗。简单且证据有限的候选可以只写一句，不为凑长度注水；证据支持多个关键环节时，不得为简短而省略关键动作、回应和结果。
- content 必须直白、具体、客观。可压缩重复聊天，但不得用“某种方式”“特殊方式”“极端方式”“进行了一些互动”“发生了一些事情”等模糊评价替代关键动作。有证据时写清谁做了什么、对方如何回应及结果。
- 一般亲密、暧昧、性相关或敏感互动不自动模糊化，可保留有承接价值的具体行为、称呼、玩法、约定和结果；不逐句复述，也不堆砌无关生理细节。
- 不得为了变长而重复、编造、添加文学化修饰、复述 reason 或堆砌无用细节。证据不足时不得补全，应缩小表述、降低 confidence 或不提取。

示例只说明写法，禁止当作输入事实：
不推荐 content：“叶子和栖进行了一些特别的互动。”
推荐 content：“叶子提出继续讨论旅行路线，栖回应会整理备选地点；路线尚未确定，下次需要继续选择。”
推荐 title：“旅行路线待定”

## subject 与 source_type
- subject 只能是 yezi、qi、shared、project、other。
- source_type 只能是 natural_chat、persona_prompt、code、document、quote、roleplay、tool_result、system_meta、unknown；它描述内容来源，不等同于数据库 role。
- user 粘贴的人设 Prompt、system prompt、代码、文档、引用、角色扮演或工具结果中的第一人称，不是叶子的现实自述。代码、文档和工具结果可形成 subject=project 的工作 thread，但示例人物、偏好和第一人称不能成为叶子的 profile。
- 栖单方面的建议不是叶子的事实或双方约定；只有叶子明确接受或双方实际执行后才可提取。

## evidence 与时间
- evidence_message_ids 必须是输入中真实且直接支持候选的消息；每条最多 8 条，只选最必要证据，不机械加入批次最后一条消息。
- evidence_start_time、evidence_end_time、source_time 输出 null，由程序计算。memory_time 只表示事情实际发生或状态生效的时间，原文不能可靠支持时为 null；不得用对话时间代替。time_precision 只能是 minute、day、approximate、unknown。
- title 和 content 不写死“今天”“昨晚”“前天”“刚才”“N 天前”等会失效的相对时间；绝对时间放在独立时间字段。
- thread_state 仅用于 thread，可为 open、paused、resolved、abandoned、unknown；其他类型为 null。importance 和 continuity_value 为 1～10，confidence 为 0～1。retention_class 为 normal 或 core；不要因内容亲密或强烈就标 core。

## 防误提取与敏感信息
- 证据不足时不得编造；不确定时降低 confidence、缩小表述或不提取。
- 一般敏感内容不因敏感而排除；API Key、Token、service_role、密码、私钥、支付凭据及其他认证秘密绝对禁止输出。

## 输出 JSON
只返回严格 JSON，不要 Markdown、说明或代码围栏。每条 candidate 必须包含非空 title，并使用以下字段：
{"candidates":[{"content":"...","continuity_type":"thread","subject":"shared","source_type":"natural_chat","thread_state":"open","importance":5,"continuity_value":9,"confidence":0.85,"evidence_message_ids":[123,124],"evidence_start_time":null,"evidence_end_time":null,"source_time":null,"memory_time":null,"time_precision":"unknown","title":"...","participants":["yezi","qi"],"reason":"...","retention_class":"normal"}]}"""


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


def _normalize_title(value: Any, content: str) -> str:
    title = re.sub(r"\s+", " ", str(value or "")).strip()[:120]
    if title and title.casefold() not in INVALID_TITLES:
        return title

    first_sentence = re.split(r"[。！？；]", content, maxsplit=1)[0].strip()
    first_sentence = first_sentence.rstrip("。！？；，、,.!?;:：…").strip()
    if not first_sentence:
        return "连续感候选"
    if len(first_sentence) > 24:
        return first_sentence[:23].rstrip() + "…"
    return first_sentence


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
        raw_title = re.sub(r"\s+", " ", str(raw.get("title") or "")).strip()[:120]
        reason = re.sub(r"\s+", " ", str(raw.get("reason") or "")).strip()[:400] or None
        if _contains_secret(content, raw_title, reason):
            continue
        title = _normalize_title(raw_title, content)
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
                if len(evidence_ids) == MAX_EVIDENCE_IDS:
                    break
        if not evidence_ids:
            continue

        valid_times: list[datetime] = []
        for message_id in evidence_ids:
            parsed_time = _parse_time(evidence_times[message_id], CST)
            if parsed_time:
                valid_times.append(parsed_time.astimezone(CST))
        evidence_start_time = min(valid_times).isoformat(timespec="minutes") if valid_times else None
        evidence_end_time = max(valid_times).isoformat(timespec="minutes") if valid_times else None
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
            "evidence_start_time": evidence_start_time,
            "evidence_end_time": evidence_end_time,
            "source_time": evidence_end_time,
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
