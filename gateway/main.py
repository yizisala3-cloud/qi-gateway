"""网关入口 - Starlette ASGI 应用。

Phase 1: 透传请求到上游 LLM，真流式转发。
Phase 2: 积温语气注入到 system prompt。
Phase 3: Eventide 身体状态卡注入。
Phase 5: 后台定时 tick + 主动消息端点。
Phase 6: 对话后情绪分析。
"""
import asyncio
import json
import logging
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


def track_task(coro):
    """创建后台任务并防止被 GC。"""
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
    """把积温语气提示词注入到消息列表的 system prompt 中。"""
    if not tone_prompt:
        return messages

    tone_block = f"\n\n【当前情绪状态与语气指引】\n{tone_prompt}"

    for msg in messages:
        if msg.get("role") == "system":
            msg["content"] = msg["content"] + tone_block
            return messages

    messages.insert(0, {"role": "system", "content": tone_block.strip()})
    return messages


# ── 后台定时任务 ──────────────────────────────────
async def scheduler_loop():
    """后台循环：每 5 分钟 tick 积温 + 检查主动消息。"""
    global _scheduler_running
    _scheduler_running = True
    log.info("后台调度器启动")

    while _scheduler_running:
        try:
            await asyncio.sleep(300)  # 5 分钟
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(bg_executor, check_and_generate)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"调度器异常: {e}")
            await asyncio.sleep(60)

    log.info("后台调度器停止")


# ── 提取用户最后一条消息 ──────────────────────────
def _extract_last_user_text(messages: list[dict]) -> str:
    """从消息列表中提取最后一条 user 消息文本。"""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            # 多模态消息，取第一个 text 块
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        return part.get("text", "")
            return ""
    return ""


# ── 核心：/v1/chat/completions ────────────────────
async def chat_completions(request: Request):
    """真流式透传到上游 LLM，注入积温语气 + Eventide 状态卡。"""
    if not verify_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    # 标记用户发消息
    loop = asyncio.get_event_loop()
    loop.run_in_executor(bg_executor, update_jiwen_on_user_message)

    # 提取用户消息用于后续分析
    user_text = _extract_last_user_text(body.get("messages", []))

    # 构建上下文并注入
    tone_prompt = await loop.run_in_executor(bg_executor, build_context)
    if tone_prompt and "messages" in body:
        body["messages"] = inject_tone_to_messages(body["messages"], tone_prompt)

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
                upstream_url,
                headers=headers,
                json=body,
                timeout=httpx.Timeout(cfg.UPSTREAM_READ_TIMEOUT, connect=10.0),
            )
            loop.run_in_executor(bg_executor, update_jiwen_on_bot_reply)
            # Phase 6: 非流式回复后触发情绪分析
            try:
                resp_data = resp.json()
                bot_text = resp_data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if bot_text and user_text:
                    loop.run_in_executor(bg_executor, analyze_and_update, user_text, bot_text)
            except Exception:
                pass
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type="application/json",
            )
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
                "POST",
                upstream_url,
                headers=headers,
                json=body,
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
                            delta = (
                                chunk.get("choices", [{}])[0]
                                .get("delta", {})
                                .get("content", "")
                            )
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
                loop.run_in_executor(bg_executor, update_jiwen_on_bot_reply)
                # Phase 6: 流式回复完成后触发情绪分析
                if user_text:
                    loop.run_in_executor(
                        bg_executor, analyze_and_update, user_text, complete_text
                    )

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── 主动消息端点 ──────────────────────────────────
async def proactive_check(request: Request):
    """橘瓣 workflow 轮询：有没有想说的话。"""
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
        "phase": 6,
        "uptime": time.time() - _start_time,
        "scheduler_running": _scheduler_running,
    })


async def status(request: Request):
    if not verify_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({
        "phase": 6,
        "upstream_base_url": cfg.UPSTREAM_BASE_URL,
        "upstream_model": cfg.UPSTREAM_MODEL,
        "bg_tasks": len(_background_tasks),
        "scheduler_running": _scheduler_running,
    })


async def list_models(request: Request):
    if not verify_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    models = []
    if cfg.UPSTREAM_MODEL:
        models.append({
            "id": cfg.UPSTREAM_MODEL,
            "object": "model",
            "owned_by": "qi-gateway",
        })
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
    log.info(f"网关启动 Phase 6 | upstream={cfg.UPSTREAM_BASE_URL}")

    # 启动后台调度器
    scheduler_task = track_task(scheduler_loop())

    yield

    # 关闭
    global _scheduler_running
    _scheduler_running = False
    scheduler_task.cancel()
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
