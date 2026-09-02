"""网关入口 - Starlette ASGI 应用。

保留聊天代理、客户端主动请求上下文、Eventide、待办和记忆任务。
"""
import asyncio
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles

from .config import cfg
from .context import build_context
from .memory_continuity import run_continuity_digest_if_due
from .memory_extract import resolve_assistant_id
from .memory_heat import run_heat_decay
from .admin_api import admin_api_routes
from .admin_memory_api import admin_memory_routes
from .memory_digest_api import memory_digest_routes
from .memory_request_api import memory_request_routes
from .memory_review_api import memory_review_routes
from .memory_mcp import memory_mcp, memory_mcp_http_app
from .todo_api import todo_routes
from .todos import get_proactive_todo_context
from .model_routing import select_upstream_model
from .request_context import (
    append_gateway_context,
    build_todo_feedback_guidance,
    extract_last_user_text,
    extract_recent_turns,
    is_orangechat_proactive_request,
    message_text,
    annotate_proactive_control_signal,
)
from . import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,
)
log = logging.getLogger("gateway")

bg_executor = ThreadPoolExecutor(max_workers=20, thread_name_prefix="gw-bg")
http_client: httpx.AsyncClient | None = None

_background_tasks: set[asyncio.Task] = set()
_daily_running = False
_last_digest_run: dict | None = None
_last_heat_decay_date = ""


def track_task(coro):
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def verify_token(request: Request) -> bool:
    if not cfg.GATEWAY_TOKEN:
        return True
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    return token == cfg.GATEWAY_TOKEN


# ── 聊天原文保存（旁路） ──────────────────────────────────────────
# 仅保存本次请求新产生的消息：普通请求取最后一条真实 user 消息与上游回复；
# 主动请求的合成控制信号一律不作为 user 保存。历史消息不在这里重复落库，
# 由客户端的常规请求流程维护。user 记录与 assistant 记录成对保存——只有
# 上游成功产出有效 assistant 文本时才写两条；上游失败时不保留用户输入，
# 这是有意的取舍，保证 chat_messages 里不出现没有回复的孤儿 user 行。

def extract_assistant_reply_text(status_code: int, payload: bytes) -> str:
    """从 OpenAI-compatible 响应中提取 assistant 文本；结构异常返回空串。"""
    if status_code < 200 or status_code >= 300:
        return ""
    try:
        data = json.loads(payload)
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    if not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    return message_text(message).strip()


def stream_delta_text(line: str) -> str:
    """从单条 SSE 行提取 choices[0].delta.content 片段；其余返回空串。"""
    stripped = line.strip()
    if not stripped.startswith("data:"):
        return ""
    data = stripped[len("data:"):].strip()
    if not data or data == "[DONE]":
        return ""
    try:
        chunk = json.loads(data)
    except Exception:
        return ""
    if not isinstance(chunk, dict):
        return ""
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    if not isinstance(choices[0], dict):
        return ""
    delta = choices[0].get("delta")
    if not isinstance(delta, dict):
        return ""
    content = delta.get("content")
    return content if isinstance(content, str) else ""


async def persist_chat_records(user_text: str, assistant_text: str):
    """在独立任务中保存本次请求的 user/assistant 原文。

    该协程经 track_task 调度，不挂在会被客户端下一条消息取消的当前请求
    任务上；Supabase 写入失败只记日志，绝不影响聊天响应。
    """

    def _job():
        try:
            author = resolve_assistant_id()
        except Exception as exc:
            # assistant_id 取自现有协议（MEMORY_ASSISTANT_ID 或从 chat_messages
            # 自动发现）。取不到时明确跳过并记日志，不伪造身份。
            log.warning(
                "聊天原文保存跳过: persist_chat_records | 无法确定 assistant_id | error=%s",
                type(exc).__name__,
            )
            return
        if user_text.strip():
            db.save_chat_message("user", user_text, author)
        if assistant_text:
            db.save_chat_message("assistant", assistant_text, author)

    try:
        await asyncio.to_thread(_job)
    except Exception as exc:
        log.error("聊天原文保存异常: persist_chat_records | error=%s", type(exc).__name__)


async def daily_task_loop():
    """检查持久化记忆任务；失败不会推进游标，成功提交具有幂等保护。"""
    global _daily_running, _last_digest_run, _last_heat_decay_date
    _daily_running = True
    log.info("记忆任务调度器启动（30min 间隔）")
    while _daily_running:
        try:
            await asyncio.sleep(1800)
            loop = asyncio.get_event_loop()
            try:
                result = await loop.run_in_executor(bg_executor, run_continuity_digest_if_due)
            except Exception as exc:
                # Continuity failures must never prevent the independent daily
                # heat-decay check below from running.
                result = None
                log.exception("连续感自动总结检查失败: %s", type(exc).__name__)
            if result:
                _last_digest_run = {
                    key: result.get(key)
                    for key in (
                        "id", "trigger", "status", "message_count", "extracted_count",
                        "inserted_count", "error_code", "completed_at",
                    )
                }
                log.info(
                    "记忆任务完成: id=%s trigger=%s status=%s extracted=%s inserted=%s",
                    result.get("id"), result.get("trigger"), result.get("status"),
                    result.get("extracted_count"), result.get("inserted_count"),
                )

            now_cst = datetime.now(timezone(timedelta(hours=8)))
            today = now_cst.strftime("%Y-%m-%d")
            if _last_heat_decay_date != today:
                decay_result = await loop.run_in_executor(bg_executor, run_heat_decay)
                if decay_result.get("status") in {"succeeded", "already_ran"}:
                    _last_heat_decay_date = today
                else:
                    log.warning(
                        "热度衰减将在下一次调度检查时重试: status=%s reason=%s",
                        decay_result.get("status"),
                        decay_result.get("reason", ""),
                    )
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.exception("记忆任务调度器异常: %s", e)
            await asyncio.sleep(300)


async def chat_completions(request: Request):
    if not verify_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    loop = asyncio.get_event_loop()
    messages = body.get("messages", [])
    proactive_request = is_orangechat_proactive_request(messages)
    if proactive_request:
        # OrangeChat already supplies its complete persona, history, proactive
        # rules and synthetic trigger. Keep the original prompt untouched, add
        # only a neutral control-signal annotation, and do not count the trigger
        # as a new message from the human user.
        user_text = ""
        proactive_request_messages = annotate_proactive_control_signal(messages)
        try:
            todo_context = await asyncio.wait_for(
                loop.run_in_executor(bg_executor, get_proactive_todo_context),
                timeout=3.0,
            )
        except asyncio.TimeoutError:
            # The reminder is optional; a slow database must not delay or
            # suppress the proactive chat request itself.
            todo_context = ""
            log.warning("主动消息待办读取超时，已跳过")
        if todo_context:
            proactive_request_messages = append_gateway_context(
                proactive_request_messages,
                todo_context,
            )
        body["messages"] = proactive_request_messages
        log.info("OrangeChat proactive request detected; preserving client system prompt")
    else:
        user_text = extract_last_user_text(messages)
        history_turns = extract_recent_turns(messages)

    if not proactive_request:
        full_context = await loop.run_in_executor(
            bg_executor, build_context, user_text, history_turns
        )
        todo_feedback = build_todo_feedback_guidance(user_text)
        if todo_feedback:
            full_context = full_context + "\n\n" + todo_feedback if full_context else todo_feedback
        if full_context and "messages" in body:
            body["messages"] = append_gateway_context(body["messages"], full_context)

    upstream_url = f"{cfg.UPSTREAM_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg.UPSTREAM_API_KEY}",
        "Content-Type": "application/json",
    }
    requested_model = body.get("model")
    selected_model = select_upstream_model(
        cfg.UPSTREAM_MODEL,
        requested_model if isinstance(requested_model, str) else "",
    )
    if selected_model:
        body["model"] = selected_model
    is_stream = body.get("stream", False)

    if not is_stream:
        try:
            resp = await http_client.post(
                upstream_url, headers=headers, json=body,
                timeout=httpx.Timeout(cfg.UPSTREAM_READ_TIMEOUT, connect=10.0),
            )
        except httpx.TimeoutException:
            return JSONResponse({"error": "upstream timeout"}, status_code=504)
        except Exception as e:
            log.error(f"upstream error: {e}")
            return JSONResponse({"error": "upstream error"}, status_code=502)
        assistant_text = extract_assistant_reply_text(resp.status_code, resp.content)
        if assistant_text:
            track_task(persist_chat_records(user_text, assistant_text))
        return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")

    async def stream_generator():
        collected: list[str] = []
        completed = False
        try:
            async with http_client.stream(
                "POST", upstream_url, headers=headers, json=body,
                timeout=httpx.Timeout(cfg.UPSTREAM_READ_TIMEOUT, connect=10.0),
            ) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    yield f"data: {json.dumps({'error': error_body.decode()[:500]})}\n\n"
                    return
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    delta = stream_delta_text(line)
                    if delta:
                        collected.append(delta)
                    yield f"{line}\n\n"
            completed = True
        except httpx.TimeoutException:
            yield f"data: {json.dumps({'error': 'upstream read timeout'})}\n\n"
        except Exception as e:
            log.error(f"stream error: {e}")
            yield f"data: {json.dumps({'error': str(e)[:200]})}\n\n"
        if completed and collected:
            assistant_text = "".join(collected).strip()
            if assistant_text:
                track_task(persist_chat_records(user_text, assistant_text))

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def health(request: Request):
    return JSONResponse({
        "status": "ok",
        "phase": "4.5-memory-digest",
        "uptime": time.time() - _start_time,
        "daily_running": _daily_running,
    })


async def status(request: Request):
    if not verify_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    supabase_status = await asyncio.to_thread(db.get_client_status)
    return JSONResponse({
        "phase": "4.5-memory-digest",
        "upstream_base_url": cfg.UPSTREAM_BASE_URL,
        "upstream_model": cfg.UPSTREAM_MODEL,
        "supabase": supabase_status,
        "rls_ready": supabase_status["elevated_active"],
        "bg_tasks": len(_background_tasks),
        "daily_running": _daily_running,
        "last_digest_run": _last_digest_run,
        "last_heat_decay_date": _last_heat_decay_date,
        "memory_plugin_configured": bool(cfg.MEMORY_PLUGIN_TOKEN),
        "memory_mcp_configured": bool(cfg.MCP_MEMORY_TOKEN),
        "todo_plugin_configured": bool(cfg.TODO_PLUGIN_TOKEN),
    })


async def list_models(request: Request):
    if not verify_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    models = []
    if cfg.UPSTREAM_MODEL:
        models.append({"id": cfg.UPSTREAM_MODEL, "object": "model", "owned_by": "qi-gateway"})
    return JSONResponse({"object": "list", "data": models})


_start_time = time.time()
_admin_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin")


@asynccontextmanager
async def lifespan(app):
    global http_client
    http_client = httpx.AsyncClient(
        http2=True,
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )
    log.info(f"网关启动 Phase 4.5 Memory Digest | upstream={cfg.UPSTREAM_BASE_URL}")
    daily_task = track_task(daily_task_loop())
    try:
        async with memory_mcp.session_manager.run():
            yield
    finally:
        global _daily_running
        _daily_running = False
        daily_task.cancel()
        await http_client.aclose()
        bg_executor.shutdown(wait=False)
        log.info("网关关闭")


_routes = [
    Route("/v1/chat/completions", chat_completions, methods=["POST"]),
    Route("/v1/models", list_models, methods=["GET"]),
    Route("/health", health, methods=["GET"]),
    Route("/status", status, methods=["GET"]),
]
_routes.extend(admin_api_routes)
_routes.extend(admin_memory_routes)
_routes.extend(memory_digest_routes)
_routes.extend(memory_request_routes)
_routes.extend(memory_review_routes)
_routes.extend(todo_routes)

if os.path.isdir(_admin_dir):
    _routes.append(Mount("/admin", app=StaticFiles(directory=_admin_dir, html=True), name="admin"))
    log.info(f"Admin panel mounted at /admin (dir={_admin_dir})")

# The SDK owns the exact /mcp Streamable HTTP route. This catch-all mount is
# deliberately last so it cannot shadow gateway, admin, memory, or todo paths.
_routes.append(Mount("/", app=memory_mcp_http_app, name="memory-mcp"))

app = Starlette(routes=_routes, lifespan=lifespan)

