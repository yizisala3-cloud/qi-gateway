"""Remote MCP surface for continuity memory tools.

The official MCP SDK owns protocol negotiation, JSON-RPC framing and
Streamable HTTP behavior. This module only supplies authentication and the two
small business tools.
"""
from __future__ import annotations

import asyncio
import hmac
import json
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from .config import cfg
from .memory_requests import MemoryRequestError, create_memory_request
from .memory_review import list_reviewable_memory_requests, review_ai_memory_request


memory_mcp = MCPServer(
    name="qi-gateway-memory",
    title="Qi Gateway Memory",
    description="Write and review six-class continuity memories.",
    version="1.0.0",
)


def _assistant_id() -> str:
    assistant_id = cfg.MEMORY_ASSISTANT_ID.strip()
    if not assistant_id:
        raise ToolError("memory assistant is not configured")
    return assistant_id


def _tool_error(exc: MemoryRequestError) -> ToolError:
    return ToolError(f"{exc.code}: {exc}")


@memory_mcp.tool(
    name="request_memory",
    description=(
        "Write a validated continuity memory. moment, thread, and inside_joke "
        "are stored directly; episode, profile, and interaction_rule remain "
        "pending for the user. assistant_id and review policy are server controlled."
    ),
)
async def request_memory(
    content: str,
    reason: str,
    continuity_type: Literal[
        "moment", "thread", "episode", "inside_joke", "profile", "interaction_rule"
    ],
    continuity_data: dict[str, Any],
    thread_state: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    importance: int = 5,
    continuity_value: int | None = None,
    subject: str = "shared",
    source_type: str = "natural_chat",
    retention_class: str = "normal",
    participants: list[str] | None = None,
    update_mode: str = "append",
    memory_key: str | None = None,
    conversation_id: str | None = None,
    source_message_id: int | None = None,
) -> dict[str, Any]:
    payload = {
        "content": content,
        "reason": reason,
        "continuity_type": continuity_type,
        "continuity_data": continuity_data,
        "thread_state": thread_state,
        "title": title,
        "tags": tags or [],
        "importance": importance,
        "continuity_value": importance if continuity_value is None else continuity_value,
        "subject": subject,
        "source_type": source_type,
        "retention_class": retention_class,
        "participants": participants or ["yezi", "qi"],
        "update_mode": update_mode,
        "memory_key": memory_key,
        "conversation_id": conversation_id,
        "source_message_id": source_message_id,
    }
    try:
        return await asyncio.to_thread(
            create_memory_request,
            payload,
            "",
            source="mcp_memory",
            assistant_id=_assistant_id(),
        )
    except MemoryRequestError as exc:
        raise _tool_error(exc) from exc


@memory_mcp.tool(
    name="review_memory_requests",
    description=(
        "List or review only pending moment, thread, and inside_joke requests. "
        "episode, profile, and interaction_rule are always reserved for the user."
    ),
)
async def review_memory_requests(
    action: Literal["list", "approve", "reject", "merge", "duplicate", "conflict"],
    request_id: int | None = None,
    content: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    importance: int | None = None,
    review_note: str | None = None,
    update_mode: str | None = None,
    memory_key: str | None = None,
    related_memory_id: int | None = None,
) -> dict[str, Any]:
    assistant_id = _assistant_id()
    try:
        if action == "list":
            return {
                "requests": await asyncio.to_thread(
                    list_reviewable_memory_requests, assistant_id, 50
                )
            }
        if request_id is None:
            raise MemoryRequestError("invalid_review", "request_id is required")
        payload = {
            key: value
            for key, value in {
                "action": action,
                "content": content,
                "title": title,
                "tags": tags,
                "importance": importance,
                "review_note": review_note,
                "update_mode": update_mode,
                "memory_key": memory_key,
                "related_memory_id": related_memory_id,
            }.items()
            if value is not None
        }
        return await asyncio.to_thread(
            review_ai_memory_request, assistant_id, request_id, payload
        )
    except MemoryRequestError as exc:
        raise _tool_error(exc) from exc


class MCPBearerAuth:
    """Minimal ASGI guard that never reads or logs the MCP request body."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        configured = cfg.MCP_MEMORY_TOKEN.strip()
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        auth = headers.get("authorization", "")
        supplied = auth.removeprefix("Bearer ").strip()
        if not configured:
            await self._reject(send, 503, "mcp_not_configured")
            return
        if not supplied or not hmac.compare_digest(supplied, configured):
            await self._reject(send, 401, "unauthorized")
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send: Any, status: int, code: str) -> None:
        body = json.dumps({"error": code}, separators=(",", ":")).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})


memory_mcp_http_app = MCPBearerAuth(
    memory_mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        max_request_body_size=65_536,
        # This is a public, bearer-protected gateway mounted behind the same
        # reverse proxy as /v1. Pinning the SDK to localhost Host values would
        # reject the user's real gateway domain.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
)
