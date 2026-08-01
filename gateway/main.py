"""网关入口 - Starlette ASGI 应用。

Phase 4.5：可靠、可观测的记忆总结管线（主动消息生成暂停）。
"""
import asyncio
import json
import logging
import os
import re
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
from .context import build_context, update_jiwen_on_user_message, update_jiwen_on_bot_reply
from .proactive import check_and_generate, fetch_pending_message
from .analysis import analyze_and_update
from .memory_extract import run_scheduled_digest_if_due
from .memory_heat import run_heat_decay
from .admin_api import admin_api_routes
from .memory_digest_api import memory_digest_routes
from .memory_request_api import memory_request_routes
from .memory_review_api import memory_review_routes
from .model_routing import normalize_upstream_model
from . import db
from .timer import (
    parse_and_strip_tags, register_tags, cancel_delay_on_user_message,
    get_active_busy, save_to_busy_inbox, get_pending_timers, mark_executed,
    get_timer_status_for_context,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,
)
log = logging.getLogger("gateway")

bg_executor = ThreadPoolExecutor(max_workers=20, thread_name_prefix="gw-bg")
http_client: httpx.AsyncClient | None = None

_background_tasks: set[asyncio.Task] = set()
_scheduler_running = False
_timer_running = False
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


def inject_context_to_messages(messages: list[dict], context: str) -> list[dict]:
    if not context:
        return messages
    for msg in messages:
        if msg.get("role") == "system":
            msg["content"] = msg["content"] + "\n\n" + context
            return messages
    messages.insert(0, {"role": "system", "content": context})
    return messages


def _extract_last_user_text(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        return part.get("text", "")
            return ""
    return ""


def _strip_thinking(text: str) -> str:
    return re.sub(r'<think>.*?</think>\s*', '', text, flags=re.DOTALL).strip()


async def scheduler_loop():
    global _scheduler_running
    _scheduler_running = True
    log.info("积温调度器启动（5min 间隔）")
    while _scheduler_running:
        try:
            await asyncio.sleep(300)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(bg_executor, check_and_generate)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"积温调度器异常: {e}")
            await asyncio.sleep(60)


async def timer_check_loop():
    global _timer_running
    _timer_running = True
    log.info("标签定时器启动（30s 间隔）")
    while _timer_running:
        try:
            await asyncio.sleep(30)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(bg_executor, _process_pending_timers)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"定时器检查异常: {e}")
            await asyncio.sleep(30)


async def daily_task_loop():
    """检查持久化记忆任务；失败不会推进游标，成功提交具有幂等保护。"""
    global _daily_running, _last_digest_run, _last_heat_decay_date
    _daily_running = True
    log.info("记忆任务调度器启动（30min 间隔）")
    while _daily_running:
        try:
            await asyncio.sleep(1800)
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(bg_executor, run_scheduled_digest_if_due)
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
            if 3 <= now_cst.hour < 4 and _last_heat_decay_date != today:
                await loop.run_in_executor(bg_executor, run_heat_decay)
                _last_heat_decay_date = today
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.exception("记忆任务调度器异常: %s", e)
            await asyncio.sleep(300)


def _process_pending_timers():
    pending = get_pending_timers()
    if not pending:
        return
    for timer in pending:
        try:
            mark_executed(timer["id"])
            log.info(f"定时器到期标记: type={timer['type']} id={timer['id']}")
        except Exception as e:
            log.error(f"处理定时器失败 id={timer.get('id')}: {e}")
            mark_executed(timer["id"])


async def chat_completions(request: Request):
    if not verify_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    loop = asyncio.get_event_loop()
    messages = body.get("messages", [])
    loop.run_in_executor(bg_executor, cancel_delay_on_user_message)
    loop.run_in_executor(bg_executor, update_jiwen_on_user_message)
    user_text = _extract_last_user_text(messages)

    busy = await loop.run_in_executor(bg_executor, get_active_busy)
    if busy:
        if user_text:
            loop.run_in_executor(bg_executor, save_to_busy_inbox, user_text)
        return JSONResponse({
            "choices": [{
                "message": {"role": "assistant", "content": ""},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    full_context = await loop.run_in_executor(bg_executor, build_context, user_text)
    timer_status = await loop.run_in_executor(bg_executor, get_timer_status_for_context)
    if timer_status:
        full_context = full_context + "\n\n" + timer_status if full_context else timer_status
    if full_context and "messages" in body:
        body["messages"] = inject_context_to_messages(body["messages"], full_context)

    upstream_url = f"{cfg.UPSTREAM_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg.UPSTREAM_API_KEY}",
        "Content-Type": "application/json",
    }
    requested_model = body.get("model")
    if isinstance(requested_model, str) and requested_model:
        body["model"] = normalize_upstream_model(requested_model)
    elif cfg.UPSTREAM_MODEL:
        body["model"] = cfg.UPSTREAM_MODEL
    is_stream = body.get("stream", False)

    if not is_stream:
        try:
            resp = await http_client.post(
                upstream_url, headers=headers, json=body,
                timeout=httpx.Timeout(cfg.UPSTREAM_READ_TIMEOUT, connect=10.0),
            )
            try:
                resp_data = resp.json()
                bot_text = resp_data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if bot_text:
                    clean_text, tags = parse_and_strip_tags(bot_text)
                    if tags:
                        loop.run_in_executor(bg_executor, register_tags, tags)
                        resp_data["choices"][0]["message"]["content"] = clean_text
                        loop.run_in_executor(bg_executor, update_jiwen_on_bot_reply)
                        if user_text:
                            loop.run_in_executor(bg_executor, analyze_and_update, user_text, clean_text)
                        return JSONResponse(resp_data, status_code=resp.status_code)
                    loop.run_in_executor(bg_executor, update_jiwen_on_bot_reply)
                    if user_text:
                        loop.run_in_executor(bg_executor, analyze_and_update, user_text, bot_text)
            except Exception:
                pass
            return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
        except httpx.TimeoutException:
            return JSONResponse({"error": "upstream timeout"}, status_code=504)
        except Exception as e:
            log.error(f"upstream error: {e}")
            return JSONResponse({"error": "upstream error"}, status_code=502)

    async def stream_generator():
        full_content = []
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
                    if line.startswith("data: ") and not line.startswith("data: [DONE]"):
                        try:
                            chunk = json.loads(line[6:])
                            delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                            if delta:
                                full_content.append(delta)
                        except (json.JSONDecodeError, IndexError, KeyError):
                            pass
        except httpx.TimeoutException:
            yield f"data: {json.dumps({'error': 'upstream read timeout'})}\n\n"
        except Exception as e:
            log.error(f"stream error: {e}")
            yield f"data: {json.dumps({'error': str(e)[:200]})}\n\n"
        finally:
            complete_text = "".join(full_content)
            if complete_text:
                log.info(f"回复长度: {len(complete_text)} 字")
                clean_text, tags = parse_and_strip_tags(complete_text)
                if tags:
                    loop.run_in_executor(bg_executor, register_tags, tags)
                loop.run_in_executor(bg_executor, update_jiwen_on_bot_reply)
                if user_text:
                    loop.run_in_executor(bg_executor, analyze_and_update, user_text, clean_text or complete_text)

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def proactive_check(request: Request):
    if not verify_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    loop = asyncio.get_event_loop()
    msg = await loop.run_in_executor(bg_executor, fetch_pending_message)
    if msg:
        return JSONResponse({
            "has_message": True,
            "content": msg["content"],
            "tone_level": msg.get("tone_level"),
            "created_at": msg.get("created_at"),
        })
    return JSONResponse({"has_message": False})


async def health(request: Request):
    return JSONResponse({
        "status": "ok",
        "phase": "4.5-memory-digest",
        "uptime": time.time() - _start_time,
        "scheduler_running": _scheduler_running,
        "timer_running": _timer_running,
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
        "scheduler_running": _scheduler_running,
        "timer_running": _timer_running,
        "daily_running": _daily_running,
        "last_digest_run": _last_digest_run,
        "last_heat_decay_date": _last_heat_decay_date,
        "memory_plugin_configured": bool(cfg.MEMORY_PLUGIN_TOKEN),
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
    scheduler_task = track_task(scheduler_loop())
    timer_task = track_task(timer_check_loop())
    daily_task = track_task(daily_task_loop())
    yield
    global _scheduler_running, _timer_running, _daily_running
    _scheduler_running = False
    _timer_running = False
    _daily_running = False
    scheduler_task.cancel()
    timer_task.cancel()
    daily_task.cancel()
    await http_client.aclose()
    bg_executor.shutdown(wait=False)
    log.info("网关关闭")


_routes = [
    Route("/v1/chat/completions", chat_completions, methods=["POST"]),
    Route("/v1/models", list_models, methods=["GET"]),
    Route("/v1/proactive", proactive_check, methods=["GET"]),
    Route("/health", health, methods=["GET"]),
    Route("/status", status, methods=["GET"]),
]
_routes.extend(admin_api_routes)
_routes.extend(memory_digest_routes)
_routes.extend(memory_request_routes)
_routes.extend(memory_review_routes)

if os.path.isdir(_admin_dir):
    _routes.append(Mount("/admin", app=StaticFiles(directory=_admin_dir, html=True), name="admin"))
    log.info(f"Admin panel mounted at /admin (dir={_admin_dir})")

app = Starlette(routes=_routes, lifespan=lifespan)

