"""Synthetic pre-upgrade chat binding; actual UI and management backend."""
import argparse
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(root))
from gateway import model_config as registry
from starlette.responses import JSONResponse
from tools import preview_model_config as preview

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--store", required=True)
args = parser.parse_args()
os.environ["MODEL_CONFIG_PATH"] = str(Path(args.store).resolve())
if not registry._path().exists():
    registry.cfg.UPSTREAM_BASE_URL = "https://before-update.invalid/v1"
    registry.cfg.UPSTREAM_API_KEY = "synthetic-before-update-key"
    registry.cfg.UPSTREAM_MODEL = "before-update-model"
    registry.mutate(0, "chat", {"provider_id": "env-upstream", "key_id": "environment", "model": "before-update-model"})

original_fixture = preview.fixture


async def safe_fixture(request):
    try:
        return await original_fixture(request)
    except registry.ConfigError as exc:
        # Match the production /status boundary, whose 409 is independently
        # checked using the actual gateway.main.status function.
        return JSONResponse({"error": str(exc)}, status_code=exc.status)


preview.fixture = safe_fixture
preview.main()
