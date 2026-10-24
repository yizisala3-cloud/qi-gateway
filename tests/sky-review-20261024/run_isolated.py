"""Run this review without deployment dotenv or external service credentials."""
import os
from pathlib import Path
import sys

import dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
dotenv.load_dotenv = lambda *args, **kwargs: False
for key in list(os.environ):
    if key.startswith(("SUPABASE_", "UPSTREAM_", "ANALYSIS_", "CONTINUITY_",
                       "RUMINATION_", "QQBOT_", "GATEWAY_")) or key in {
        "MCP_MEMORY_TOKEN", "MEMORY_PLUGIN_TOKEN", "TODO_PLUGIN_TOKEN", "MEMORY_ASSISTANT_ID",
    }:
        os.environ.pop(key, None)
os.environ.update(QQBOT_ENABLED="false", GATEWAY_LOG_ENABLED="false")

# Import before the bridge fixtures temporarily replace time.sleep.
from gateway import main  # noqa: E402,F401
import pytest  # noqa: E402

raise SystemExit(pytest.main(sys.argv[1:]))
