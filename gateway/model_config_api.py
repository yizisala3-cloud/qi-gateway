"""Authenticated metadata-only model management; no plaintext key readback."""
import asyncio
from starlette.responses import JSONResponse
from starlette.routing import Route
from .credential_api import _authorized
from . import model_config

HEADERS = {"Cache-Control": "no-store"}


async def configuration(request):
    if not _authorized(request):
        return JSONResponse({"error": "未登录或令牌无效"}, 401, headers=HEADERS)
    try:
        if request.method == "GET":
            result = await asyncio.to_thread(model_config.public)
        else:
            if len(await request.body()) > 300000:
                raise model_config.ConfigError("配置请求过大。", 413)
            try:
                body = await request.json()
            except (ValueError, UnicodeDecodeError) as exc:
                raise model_config.ConfigError("请求须为合法 JSON。", 400) from exc
            if not isinstance(body, dict) or set(body) != {"revision", "value"}:
                raise model_config.ConfigError("请提供配置版本和修改内容。", 400)
            operation = request.path_params.get("purpose", "provider")
            if operation == "provider" and not isinstance(body["value"], dict):
                raise model_config.ConfigError("供应商配置无效。", 400)
            result = await asyncio.to_thread(model_config.mutate, body["revision"], operation, body["value"])
        return JSONResponse(result, headers=HEADERS)
    except model_config.ConfigError as exc:
        return JSONResponse({"error": str(exc)}, exc.status, headers=HEADERS)


model_config_routes = [
    Route("/admin/api/model-config", configuration, methods=["GET"]),
    Route("/admin/api/model-config/providers", configuration, methods=["PUT"]),
    Route("/admin/api/model-config/routes/{purpose}", configuration, methods=["PUT"]),
]
