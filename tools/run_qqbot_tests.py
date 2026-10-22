"""Run local tests with dotenv and all deployment credentials disabled.

This runner never starts the application against a deployment. Optional --db
uses the repository's disposable pgserver fixture, not Supabase.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main():
    env = dict(os.environ)
    for key in list(env):
        if (key.startswith(("SUPABASE_","UPSTREAM_","ANALYSIS_","CONTINUITY_","RUMINATION_","QQBOT_"))
                or key in {"GATEWAY_TOKEN","MCP_MEMORY_TOKEN","MEMORY_PLUGIN_TOKEN","TODO_PLUGIN_TOKEN","MEMORY_ASSISTANT_ID"}):
            env.pop(key,None)
    env.update(PYTHON_DOTENV_DISABLED="1",PYTHONUTF8="1",QQBOT_ENABLED="false",GATEWAY_LOG_ENABLED="false",
               GATEWAY_TOKEN="local-qqbot-tests",PGTZ="Asia/Shanghai")
    args = sys.argv[1:] or ["tests/test_qqbot.py","tests/test_qqbot_transport.py","tests/test_qqbot_context.py","tests/test_qqbot_asgi.py"]
    return subprocess.call([sys.executable,"-m","pytest",*args],cwd=Path(__file__).resolve().parents[1],env=env)


if __name__=="__main__":
    raise SystemExit(main())
