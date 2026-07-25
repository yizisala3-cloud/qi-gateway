"""网关入口 - Starlette ASGI 应用。

Phase 1: 透传请求到上游 LLM，真流式转发。
Phase 2: 积温语气注入到 system prompt。
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

    # 找到第一条 system message 并追加
    for msg in messages:
        if msg.get("role") == "system":
            msg["content"] = msg["content"] + tone_block
            return messages

    # 没有 system message，创建一条
    messages.insert(0, {"role": "system", "content": tone_block.strip()})
    return messages


# ── 核心：/v1/chat/completions ────────────────────
async def chat_completions(request: Request):
    """真流式透传到上游 LLM，注入积温语气。"""
    if not verify_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    # ── Phase 2: 积温注入 ──
    # 1. 标记用户发消息
    loop = asyncio.get_event_loop()
    loop.run_in_executor(bg_executor, update_jiwen_on_user_message)

    # 2. 构建积温语气上下文并注入
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
            # 回复后更新积温
            loop.run_in_executor(bg_executor, update_jiwen_on_bot_reply)
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

    # ── 流式：边收边转发 ──
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
                # 回复完成后更新积温
                loop.run_in_executor(bg_executor, update_jiwen_on_bot_reply)

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── 管理端点 ──────────────────────────────────────
async def health(request: Request):
    return JSONResponse({
        "status": "ok",
        "phase": 2,
        "uptime": time.time() - _start_time,
    })


async def status(request: Request):
    if not verify_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({
        "phase": 2,
        "upstream_base_url": cfg.UPSTREAM_BASE_URL,
        "upstream_model": cfg.UPSTREAM_MODEL,
        "bg_tasks": len(_background_tasks),
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
    log.info(f"网关启动 Phase 2 | upstream={cfg.UPSTREAM_BASE_URL}")

    # TODO Phase 5: 后台定时 tick 积温

    yield

    await http_client.aclose()
    bg_executor.shutdown(wait=False)
    log.info("网关关闭")


# ── 路由 ──────────────────────────────────────────
app = Starlette(
    routes=[
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/v1/models", list_models, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/status", status, methods=["GET"]),
    ],
    lifespan=lifespan,
)
