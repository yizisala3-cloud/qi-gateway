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
from .memory_heat import run_heat_decay
from .admin_api import admin_api_routes
from .memory_digest_api import memory_digest_routes
from .memory_request_api import memory_request_routes
from .memory_review_api import memory_review_routes
from .todo_api import todo_routes
from .todos import get_proactive_todo_context
from .model_routing import select_upstream_model
from .request_context import (
    append_gateway_context,
    build_todo_feedback_guidance,
    extract_last_user_text,
    is_orangechat_proactive_request,
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

    if not proactive_request:
        full_context = await loop.run_in_executor(bg_executor, build_context, user_text)
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
            return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
        except httpx.TimeoutException:
            return JSONResponse({"error": "upstream timeout"}, status_code=504)
        except Exception as e:
            log.error(f"upstream error: {e}")
            return JSONResponse({"error": "upstream error"}, status_code=502)

    async def stream_generator():
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
                    yield f"{line}\n\n"
        except httpx.TimeoutException:
            yield f"data: {json.dumps({'error': 'upstream read timeout'})}\n\n"
        except Exception as e:
            log.error(f"stream error: {e}")
            yield f"data: {json.dumps({'error': str(e)[:200]})}\n\n"

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
    yield
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
_routes.extend(memory_digest_routes)
_routes.extend(memory_request_routes)
_routes.extend(memory_review_routes)
_routes.extend(todo_routes)

if os.path.isdir(_admin_dir):
    _routes.append(Mount("/admin", app=StaticFiles(directory=_admin_dir, html=True), name="admin"))
    log.info(f"Admin panel mounted at /admin (dir={_admin_dir})")

app = Starlette(routes=_routes, lifespan=lifespan)

