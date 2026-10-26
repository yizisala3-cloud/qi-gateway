"""Local model-config UI fixture. All credentials/data are synthetic; no upstream I/O."""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
import uvicorn

from gateway.config import cfg
from gateway.model_config_api import model_config_routes
from gateway.credential_api import credential_api_routes


async def fixture(request):
    path = request.url.path
    if path == "/health":
        return JSONResponse({"status": "ok", "uptime": 3600, "phase": "local-preview"})
    if path == "/status":
        from gateway.model_config import resolve
        route = resolve("chat")
        return JSONResponse({"upstream_base_url": route.base_url, "upstream_model": route.model,
                             "phase": "本地合成数据", "supabase": {}, "bg_tasks": 0})
    if path == "/admin/api/memory-continuity/status":
        from gateway.model_config import resolve
        route = resolve("continuity")
        return JSONResponse({"analysis_configured": route.configured, "analysis_model": route.model,
                             "status": "ready", "recent_runs": []})
    if path == "/admin/api/memory-rumination/status":
        from gateway.model_config import resolve
        route = resolve("rumination")
        return JSONResponse({"configured": route.configured, "model": route.model, "recent_runs": []})
    return JSONResponse({})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8797)
    parser.add_argument("--store", required=True)
    args = parser.parse_args()
    os.environ["MODEL_CONFIG_PATH"] = str(Path(args.store).resolve())
    for name in vars(type(cfg)):
        if name.endswith(("_API_KEY", "_TOKEN", "_SECRET_KEY", "_SERVICE_ROLE_KEY", "_SECRET")):
            setattr(cfg, name, "")
    cfg.SUPABASE_URL = ""
    cfg.GATEWAY_TOKEN = "local-model-preview"
    cfg.MEMORY_PLUGIN_TOKEN = "preview-synthetic-plugin-token"
    cfg.GATEWAY_KEY_REF_SECRET = "preview-key-reference-secret-0000000000000000"
    for prefix in ("UPSTREAM", "ANALYSIS", "CONTINUITY", "RUMINATION"):
        setattr(cfg, prefix + "_BASE_URL", "https://example.invalid/v1")
        setattr(cfg, prefix + "_API_KEY", "preview-synthetic-key-" + prefix.lower())
        setattr(cfg, prefix + "_MODEL", "preview-" + prefix.lower())
    app = Starlette(routes=[*model_config_routes, *credential_api_routes,
                           Mount("/admin", StaticFiles(directory=ROOT / "admin", html=True)),
                           Route("/{path:path}", fixture)])
    # API routes above precede the static mount. Other admin/API fixture routes
    # must also precede it so the UI never falls through to an external service.
    app.router.routes.insert(len(model_config_routes) + len(credential_api_routes),
                             Route("/admin/api/{path:path}", fixture))
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
