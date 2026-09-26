"""网关入口 - Starlette ASGI 应用。

保留聊天代理、Eventide、待办和记忆任务。
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
from .memory_rumination import _rumination_analysis_configured as _rumination_configured
from .memory_rumination import run_rumination_digest_if_due
from .admin_api import admin_api_routes
from .admin_memory_api import admin_memory_routes
from .context_admin_api import context_admin_routes
from .eventide_admin_api import eventide_admin_routes
from .memory_digest_api import memory_digest_routes
from .memory_request_api import memory_request_routes
from .memory_review_api import memory_review_routes
from .memory_mcp import memory_mcp, memory_mcp_http_app
from .planning import run_maintenance as run_planning_maintenance
from .planning_api import planning_api_routes
from .todo_api import todo_routes
from .model_routing import select_upstream_model
from .upstream_compat import normalize_gemini_browser_tool_history
from .request_context import (
    append_gateway_context,
    build_todo_feedback_guidance,
    extract_last_user_text,
    extract_recent_turns,
    message_text,
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
_planning_running = False
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
# 仅保存本次请求新产生的消息：普通请求取最后一条真实 user 消息与上游回复。
# 历史消息不在这里重复落库，由客户端的常规请求流程维护。user 记录与
# assistant 记录成对保存——只有上游成功产出有效 assistant 文本时才写两条；
# 上游失败时不保留用户输入，这是有意的取舍，保证 chat_messages 里不出现
# 没有回复的孤儿 user 行。

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

            # 反刍路径独立调度：每天到达配置小时后运行一次，独立游标与
            # 运行记录；失败只记日志，绝不影响连续感快速路径与热度衰减。
            try:
                rumination_result = await loop.run_in_executor(
                    bg_executor, run_rumination_digest_if_due,
                )
            except Exception as exc:
                rumination_result = None
                log.exception("反刍连续感调度检查失败: %s", type(exc).__name__)
            if rumination_result:
                log.info(
                    "反刍运行完成: trigger=%s status=%s batches=%s op_counts=%s",
                    rumination_result.get("trigger"),
                    rumination_result.get("status"),
                    rumination_result.get("batch_count"),
                    rumination_result.get("op_counts"),
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


async def planning_loop():
    """规划管理后台循环（约 1 分钟粒度）。

    职责：按任务刷新模式与轮次身份补生成出现实例（不使用旧游标）、
    限时超时打标、等待期满的自动重算（等待时长可配置、可关闭）、
    旧版无轮次键遗留记录的 72 小时清理。
    失败只记日志，绝不影响记忆任务循环。
    """
    global _planning_running
    _planning_running = True
    log.info("规划管理调度器启动（1min 间隔）")
    while _planning_running:
        try:
            await asyncio.sleep(60)
            loop = asyncio.get_event_loop()
            try:
                result = await loop.run_in_executor(bg_executor, run_planning_maintenance)
                if isinstance(result, dict) and result.get("status") == "skipped_busy":
                    continue
                log.debug("规划维护完成: %s", result)
            except Exception:
                log.exception("规划维护循环异常")
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.exception("规划管理调度器异常: %s", e)
            await asyncio.sleep(30)


async def chat_completions(request: Request):
    if not verify_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    loop = asyncio.get_event_loop()
    messages = body.get("messages", [])
    user_text = extract_last_user_text(messages)
    history_turns = extract_recent_turns(messages)

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
    if "messages" in body:
        body["messages"] = normalize_gemini_browser_tool_history(
            body["messages"],
            enabled=cfg.GEMINI_BROWSER_TOOL_COMPAT_ENABLED,
            selected_model=selected_model,
        )
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
        "planning_running": _planning_running,
        "last_digest_run": _last_digest_run,
        "rumination_configured": _rumination_configured(),
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
    planning_task = track_task(planning_loop())
    try:
        async with memory_mcp.session_manager.run():
            yield
    finally:
        global _daily_running, _planning_running
        _daily_running = False
        _planning_running = False
        daily_task.cancel()
        planning_task.cancel()
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
_routes.extend(context_admin_routes)
_routes.extend(eventide_admin_routes)
_routes.extend(memory_digest_routes)
_routes.extend(memory_request_routes)
_routes.extend(memory_review_routes)
_routes.extend(todo_routes)
_routes.extend(planning_api_routes)


class NoCacheStaticFiles(StaticFiles):
    # Same no-cache stance as the SSE route: every /admin load revalidates, so a
    # forgotten ?v= bump costs one hard refresh instead of a stale page forever.
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


if os.path.isdir(_admin_dir):
    _routes.append(Mount("/admin", app=NoCacheStaticFiles(directory=_admin_dir, html=True), name="admin"))
    log.info(f"Admin panel mounted at /admin (dir={_admin_dir})")

# The SDK owns the exact /mcp Streamable HTTP route. This catch-all mount is
# deliberately last so it cannot shadow gateway, admin, memory, or todo paths.
_routes.append(Mount("/", app=memory_mcp_http_app, name="memory-mcp"))

app = Starlette(routes=_routes, lifespan=lifespan)

