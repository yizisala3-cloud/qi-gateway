"""网关入口 - Starlette ASGI 应用。

Phase 1: 透传请求到上游 LLM，真流式转发。
后续 Phase 逐步加入积温、Eventide、记忆注入。
"""
import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .config import cfg

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


# ── 核心：/v1/chat/completions ────────────────────
async def chat_completions(request: Request):
    """真流式透传到上游 LLM。"""
    if not verify_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    # ── Phase 1: 直接透传，不改动消息 ──
    # 后续 Phase 会在这里注入积温/Eventide/记忆到 system prompt

    upstream_url = f"{cfg.UPSTREAM_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg.UPSTREAM_API_KEY}",
        "Content-Type": "application/json",
    }

    # 如果客户端没指定 model，用配置里的默认值
    if not body.get("model") and cfg.UPSTREAM_MODEL:
        body["model"] = cfg.UPSTREAM_MODEL

    is_stream = body.get("stream", False)

    if not is_stream:
        # ── 非流式：等完整回复再返回 ──
        try:
            resp = await http_client.post(
                upstream_url,
                headers=headers,
                json=body,
                timeout=httpx.Timeout(cfg.UPSTREAM_READ_TIMEOUT, connect=10.0),
            )
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type="application/json",
            )
        except httpx.TimeoutException:
            return JSONResponse(
                {"error": "upstream timeout"}, status_code=504
            )
        except Exception as e:
            log.error(f"upstream error: {e}")
            return JSONResponse(
                {"error": "upstream error"}, status_code=502
            )

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

                    # 攒完整回复用于后续分析
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
            # Phase 2+: 这里会触发对话后分析（情绪 delta 提取）
            complete_text = "".join(full_content)
            if complete_text:
                log.info(f"回复长度: {len(complete_text)} 字")

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
    """健康检查。"""
    return JSONResponse({
        "status": "ok",
        "phase": 1,
        "uptime": time.time() - _start_time,
    })


async def status(request: Request):
    """网关状态（需鉴权）。"""
    if not verify_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({
        "phase": 1,
        "upstream_base_url": cfg.UPSTREAM_BASE_URL,
        "upstream_model": cfg.UPSTREAM_MODEL,
        "bg_tasks": len(_background_tasks),
        "executor_threads": bg_executor._max_workers,
    })


# ── OpenAI 兼容: /v1/models ──────────────────────
async def list_models(request: Request):
    """返回模型列表，让橘瓣能发现可用模型。"""
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
    log.info(f"网关启动 | upstream={cfg.UPSTREAM_BASE_URL} model={cfg.UPSTREAM_MODEL}")

    # Phase 2+: 这里启动后台定时任务（积温 tick、Eventide advance 等）

    yield

    # 关闭
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
