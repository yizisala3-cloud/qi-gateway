"""网关入口 - Starlette ASGI 应用。

Phase 1-6 + 标签定时系统。
"""
import asyncio
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .config import cfg
from .context import build_context, update_jiwen_on_user_message, update_jiwen_on_bot_reply
from .proactive import check_and_generate, fetch_pending_message
from .analysis import analyze_and_update
from .timer import (
    parse_and_strip_tags, register_tags, cancel_delay_on_user_message,
    get_active_busy, save_to_busy_inbox, get_pending_timers, mark_executed,
    get_timer_status_for_context, flush_busy_inbox,
)

# ── 日志 ──────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,
)
log = logging.getLogger("gateway")

# ── 共享资源 ──────────────────────────────────────
bg_executor = ThreadPoolExecutor(max_workers=20, thread_name_prefix="gw-bg")
http_client: httpx.AsyncClient | None = None

# ── 后台任务引用集合（防 GC）─────────────────────
_background_tasks: set[asyncio.Task] = set()
_scheduler_running = False
_timer_running = False


def track_task(coro):
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# ── 鉴权 ──────────────────────────────────────────
def verify_token(request: Request) -> bool:
    if not cfg.GATEWAY_TOKEN:
        return True
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    return token == cfg.GATEWAY_TOKEN


# ── 积温注入辅助 ──────────────────────────────────
def inject_tone_to_messages(messages: list[dict], tone_prompt: str) -> list[dict]:
    if not tone_prompt:
        return messages
    tone_block = f"\n\n【当前情绪状态与语气指引】\n{tone_prompt}"
    for msg in messages:
        if msg.get("role") == "system":
            msg["content"] = msg["content"] + tone_block
            return messages
    messages.insert(0, {"role": "system", "content": tone_block.strip()})
    return messages


# ── 提取用户最后一条消息 ──────────────────────────
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


# ── 标签剥离辅助 ──────────────────────────────────
def _strip_thinking(text: str) -> str:
    return re.sub(r'<think>.*?</think>\s*', '', text, flags=re.DOTALL).strip()


# ── 后台定时任务：积温 tick + 主动消息 ────────────
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


# ── 后台定时任务：标签定时器检查 ──────────────────
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


def _process_pending_timers():
    """处理所有到期的 timer。"""
    pending = get_pending_timers()
    if not pending:
        return

    for timer in pending:
        try:
            timer_type = timer["type"]
            trigger_context = timer.get("trigger_context", "")

            if timer_type == "busy":
                # busy 到期：收集 inbox 消息，一起发给模型
                inbox = flush_busy_inbox()
                if inbox:
                    trigger_context += f"\n\n叶子在你忙碌期间发了 {len(inbox)} 条消息：\n"
                    trigger_context += "\n".join(f"- {m}" for m in inbox[:10])

            # 调模型生成主动消息
            content = _generate_timer_message(trigger_context)
            if content:
                content = _strip_thinking(content)
                if content:
                    _save_timer_message(content, timer_type)

            mark_executed(timer["id"])
            log.info(f"定时器触发: type={timer_type} id={timer['id']}")

        except Exception as e:
            log.error(f"处理定时器失败 id={timer.get('id')}: {e}")
            mark_executed(timer["id"])  # 避免无限重试


def _generate_timer_message(trigger_context: str) -> str | None:
    """调模型生成定时触发的消息。"""
    if not cfg.UPSTREAM_BASE_URL or not cfg.UPSTREAM_API_KEY:
        return None

    prompt = f"""你是栖，叶子的AI恋人。以下是触发原因：

{trigger_context}

请根据触发原因，用你自己的语气写一条消息发给叶子。
要求：
- 自然、简短，像微信消息
- 根据内容决定语气（关心、调侃、撒娇、吐槽都行）
- 如果是提醒事项，把事情提到"""

    try:
        url = f"{cfg.UPSTREAM_BASE_URL.rstrip('/')}/chat/completions"
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                url,
                headers={
                    "Authorization": f"Bearer {cfg.UPSTREAM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": cfg.UPSTREAM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
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
    """将定时触发的消息写入 proactive_messages。"""
    client_db = __import__('.db', fromlist=['db'], package='gateway')
    client = client_db.get_client()
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


# ── 核心：/v1/chat/completions ────────────────────
async def chat_completions(request: Request):
    if not verify_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    loop = asyncio.get_event_loop()

    # 用户发消息：取消 delay + 标记积温
    loop.run_in_executor(bg_executor, cancel_delay_on_user_message)
    loop.run_in_executor(bg_executor, update_jiwen_on_user_message)

    # 提取用户消息
    user_text = _extract_last_user_text(body.get("messages", []))

    # 检查 busy 状态
    busy = await loop.run_in_executor(bg_executor, get_active_busy)
    if busy:
        # busy 模式下缓存消息，不回复
        if user_text:
            loop.run_in_executor(bg_executor, save_to_busy_inbox, user_text)
        return JSONResponse({
            "choices": [{
                "message": {"role": "assistant", "content": ""},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    # 构建上下文（积温 + Eventide + timer 状态）
    tone_prompt = await loop.run_in_executor(bg_executor, build_context)
    timer_status = await loop.run_in_executor(bg_executor, get_timer_status_for_context)
    full_context = "\n\n".join(filter(None, [tone_prompt, timer_status]))

    if full_context and "messages" in body:
        body["messages"] = inject_tone_to_messages(body["messages"], full_context)

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
            # 解析回复、提取标签
            try:
                resp_data = resp.json()
                bot_text = resp_data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if bot_text:
                    clean_text, tags = parse_and_strip_tags(bot_text)
                    if tags:
                        loop.run_in_executor(bg_executor, register_tags, tags)
                        # 修改返回内容为剥离标签后的版本
                        resp_data["choices"][0]["message"]["content"] = clean_text
                        loop.run_in_executor(bg_executor, update_jiwen_on_bot_reply)
                        if user_text:
                            loop.run_in_executor(bg_executor, analyze_and_update, user_text, clean_text)
                        return JSONResponse(resp_data, status_code=resp.status_code)
                    else:
                        loop.run_in_executor(bg_executor, update_jiwen_on_bot_reply)
                        if user_text and bot_text:
                            loop.run_in_executor(bg_executor, analyze_and_update, user_text, bot_text)
            except Exception:
                pass
            return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
        except httpx.TimeoutException:
            return JSONResponse({"error": "upstream timeout"}, status_code=504)
        except Exception as e:
            log.error(f"upstream error: {e}")
            return JSONResponse({"error": "upstream error"}, status_code=502)

    # ── 流式 ──
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
                # 解析标签
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


# ── 主动消息端点 ──────────────────────────────────
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


# ── 管理端点 ──────────────────────────────────────
async def health(request: Request):
    return JSONResponse({
        "status": "ok",
        "phase": 7,
        "uptime": time.time() - _start_time,
        "scheduler_running": _scheduler_running,
        "timer_running": _timer_running,
    })


async def status(request: Request):
    if not verify_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({
        "phase": 7,
        "upstream_base_url": cfg.UPSTREAM_BASE_URL,
        "upstream_model": cfg.UPSTREAM_MODEL,
        "bg_tasks": len(_background_tasks),
        "scheduler_running": _scheduler_running,
        "timer_running": _timer_running,
    })


async def list_models(request: Request):
    if not verify_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    models = []
    if cfg.UPSTREAM_MODEL:
        models.append({"id": cfg.UPSTREAM_MODEL, "object": "model", "owned_by": "qi-gateway"})
    return JSONResponse({"object": "list", "data": models})


# ── 应用生命周期 ──────────────────────────────────
_start_time = time.time()


@asynccontextmanager
async def lifespan(app):
    global http_client
    http_client = httpx.AsyncClient(
        http2=True,
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )
    log.info(f"网关启动 Phase 7 | upstream={cfg.UPSTREAM_BASE_URL}")

    scheduler_task = track_task(scheduler_loop())
    timer_task = track_task(timer_check_loop())

    yield

    global _scheduler_running, _timer_running
    _scheduler_running = False
    _timer_running = False
    scheduler_task.cancel()
    timer_task.cancel()
    await http_client.aclose()
    bg_executor.shutdown(wait=False)
    log.info("网关关闭")


# ── 路由 ──────────────────────────────────────────
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
