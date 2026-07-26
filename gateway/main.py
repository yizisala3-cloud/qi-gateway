"""网关入口 - Starlette ASGI 应用。

Phase 1-7 + Phase 4 记忆系统内化。
"""
import asyncio
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from functools import partial

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .config import cfg
from .context import build_context, update_jiwen_on_user_message, update_jiwen_on_bot_reply, is_proactive_request
from .persona import load_persona
from .proactive import check_and_generate, fetch_pending_message
from .analysis import analyze_and_update
from .memory_extract import run_daily_digest
from .memory_heat import run_heat_decay
from . import db
from .timer import (
    parse_and_strip_tags, register_tags, cancel_delay_on_user_message,
    get_active_busy, save_to_busy_inbox, get_pending_timers, mark_executed,
    get_timer_status_for_context, flush_busy_inbox,
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
_last_digest_date: str = ""


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


def _patch_missing_assistant_reply(messages: list[dict]) -> list[dict]:
    """主动消息场景：如果 messages 数组最后一条非 system 的是 user，
    从 chat_messages 表补上最新的 assistant 回复，让模型知道自己已经回过了。
    """
    non_system = [m for m in messages if m.get("role") != "system"]
    if not non_system:
        return messages

    last_non_system = non_system[-1]
    if last_non_system.get("role") != "user":
        return messages

    try:
        client = db.get_client()
        if not client:
            return messages
        resp = (
            client.table("chat_messages")
            .select("role, content")
            .eq("role", "assistant")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if resp.data:
            latest_reply = resp.data[0].get("content", "")
            if latest_reply:
                last_user_idx = None
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].get("role") == "user" and messages[i] is last_non_system:
                        last_user_idx = i
                        break
                if last_user_idx is not None:
                    messages.insert(last_user_idx + 1, {
                        "role": "assistant",
                        "content": latest_reply[:500]
                    })
                    log.info("主动消息补丁：插入最新 assistant 回复")
    except Exception as e:
        log.warning(f"补丁 assistant 回复失败: {e}")

    return messages


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
    global _daily_running, _last_digest_date
    _daily_running = True
    log.info("每日任务调度器启动（30min 间隔）")
    while _daily_running:
        try:
            await asyncio.sleep(1800)
            loop = asyncio.get_event_loop()
            cst = timezone(timedelta(hours=8))
            now_cst = datetime.now(cst)
            today_str = now_cst.strftime("%Y-%m-%d")
            should_run = False
            if 3 <= now_cst.hour < 4 and today_str != _last_digest_date:
                should_run = True
                log.info("每日总结触发：凌晨定时")
            if not should_run and today_str != _last_digest_date:
                try:
                    jiwen_raw = db.load_jiwen_state()
                    if jiwen_raw and jiwen_raw.get("last_chat_at"):
                        from .jiwen_engine import _iso_to_ts
                        last_chat_ts = _iso_to_ts(jiwen_raw["last_chat_at"])
                        if last_chat_ts:
                            silence_hours = (time.time() - last_chat_ts) / 3600.0
                            if silence_hours >= 6:
                                should_run = True
                                log.info(f"每日总结触发：沉默 {silence_hours:.1f} 小时")
                except Exception:
                    pass
            if should_run:
                _last_digest_date = today_str
                await loop.run_in_executor(bg_executor, run_daily_digest)
                await loop.run_in_executor(bg_executor, run_heat_decay)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"每日任务异常: {e}")
            await asyncio.sleep(1800)


def _process_pending_timers():
    pending = get_pending_timers()
    if not pending:
        return
    for timer in pending:
        try:
            timer_type = timer["type"]
            trigger_context = timer.get("trigger_context", "")
            if timer_type == "busy":
                inbox = flush_busy_inbox()
                if inbox:
                    trigger_context += f"\n\n叶子在你忙碌期间发了 {len(inbox)} 条消息：\n"
                    trigger_context += "\n".join(f"- {m}" for m in inbox[:10])
            content = _generate_timer_message(trigger_context)
            if content:
                content = _strip_thinking(content)
                if content:
                    _save_timer_message(content, timer_type)
            mark_executed(timer["id"])
            log.info(f"定时器触发: type={timer_type} id={timer['id']}")
        except Exception as e:
            log.error(f"处理定时器失败 id={timer.get('id')}: {e}")
            mark_executed(timer["id"])


def _generate_timer_message(trigger_context: str) -> str | None:
    if not cfg.UPSTREAM_BASE_URL or not cfg.UPSTREAM_API_KEY:
        return None
    persona = load_persona()
    recent_messages = []
    try:
        client = db.get_client()
        if client:
            resp = (
                client.table("chat_messages")
                .select("role, content")
                .order("created_at", desc=True)
                .limit(5)
                .execute()
            )
            if resp.data:
                for msg in reversed(resp.data):
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    if content:
                        recent_messages.append({"role": role, "content": content[:300]})
    except Exception as e:
        log.warning(f"定时器消息获取上下文失败: {e}")
    messages = []
    if persona:
        messages.append({"role": "system", "content": persona})
    messages.extend(recent_messages)
    messages.append({"role": "user", "content": f"【系统触发】{trigger_context}\n\n请根据触发原因，用你自己的语气写一条消息发给叶子。自然、简短，像微信消息。"})
    try:
        url = f"{cfg.UPSTREAM_BASE_URL.rstrip('/')}/chat/completions"
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(
                url,
                headers={
                    "Authorization": f"Bearer {cfg.UPSTREAM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": cfg.UPSTREAM_MODEL,
                    "messages": messages,
                    "max_tokens": 300,
                    "temperature": 0.8,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            else:
                log.error(f"定时器消息生成失败: {resp.status_code}")
                return None
    except Exception as e:
        log.error(f"定时器 LLM 调用失败: {e}")
        return None


def _save_timer_message(content: str, timer_type: str):
    client = db.get_client()
    if not client:
        return
    try:
        client.table("proactive_messages").insert({
            "content": content,
            "tone_level": f"timer_{timer_type}",
            "urgency": 0.8,
        }).execute()
    except Exception as e:
        log.error(f"定时器消息写入失败: {e}")


async def chat_completions(request: Request):
    if not verify_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    loop = asyncio.get_event_loop()
    messages = body.get("messages", [])

    proactive = is_proactive_request(messages)
    if proactive:
        log.info("检测到主动消息请求，走瘦身注入")
        # 补丁：确保 messages 里有 AI 最新的回复，防止重复回复用户最后一条消息
        messages = _patch_missing_assistant_reply(messages)
        body["messages"] = messages

    if not proactive:
        loop.run_in_executor(bg_executor, cancel_delay_on_user_message)
        loop.run_in_executor(bg_executor, update_jiwen_on_user_message)

    user_text = _extract_last_user_text(messages)

    if not proactive:
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

    full_context = await loop.run_in_executor(
        bg_executor,
        partial(build_context, user_text, is_proactive=proactive)
    )

    if not proactive:
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

    if not body.get("model") and cfg.UPSTREAM_MODEL:
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
                        if user_text and not proactive:
                            loop.run_in_executor(bg_executor, analyze_and_update, user_text, clean_text)
                        return JSONResponse(resp_data, status_code=resp.status_code)
                    else:
                        loop.run_in_executor(bg_executor, update_jiwen_on_bot_reply)
                        if user_text and bot_text and not proactive:
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
                if user_text and not proactive:
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
        "phase": "4-memory",
        "uptime": time.time() - _start_time,
        "scheduler_running": _scheduler_running,
        "timer_running": _timer_running,
        "daily_running": _daily_running,
    })


async def status(request: Request):
    if not verify_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({
        "phase": "4-memory",
        "upstream_base_url": cfg.UPSTREAM_BASE_URL,
        "upstream_model": cfg.UPSTREAM_MODEL,
        "bg_tasks": len(_background_tasks),
        "scheduler_running": _scheduler_running,
        "timer_running": _timer_running,
        "daily_running": _daily_running,
        "last_digest_date": _last_digest_date,
    })


async def list_models(request: Request):
    if not verify_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    models = []
    if cfg.UPSTREAM_MODEL:
        models.append({"id": cfg.UPSTREAM_MODEL, "object": "model", "owned_by": "qi-gateway"})
    return JSONResponse({"object": "list", "data": models})


_start_time = time.time()


@asynccontextmanager
async def lifespan(app):
    global http_client
    http_client = httpx.AsyncClient(
        http2=True,
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )
    log.info(f"网关启动 Phase 4 Memory | upstream={cfg.UPSTREAM_BASE_URL}")
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


app = Starlette(
    routes=[
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/v1/models", list_models, methods=["GET"]),
        Route("/v1/proactive", proactive_check, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/status", status, methods=["GET"]),
    ],
    lifespan=lifespan,
)
