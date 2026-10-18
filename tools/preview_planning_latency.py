"""Disposable 50-task/150-memo API preview with explicit artificial latency."""
import asyncio
import logging
import os
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from time import monotonic
from unittest import mock

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from gateway import db, memo, planning_runtime
from gateway.config import cfg
from gateway.memo_api import memo_api_routes
from gateway.planning_api import planning_api_routes
from tests import test_planning_goals_pgserver as goalbase
from tests.test_planning_latency_pgserver import CountingClient

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "local-latency-fixture"
NOW = datetime.fromisoformat("2026-10-18T08:00:00+08:00")
log = logging.getLogger("preview.planning_latency")


def main():
    import uvicorn
    os.environ["QIGATEWAY_PG_PLANNING_TEST"] = "1"
    goalbase.GoalPostgresTests.setUpClass()
    fixture = goalbase.GoalPostgresTests()
    fixture.client = CountingClient(fixture.conn)
    requests = []
    try:
        fixture.conn.execute("truncate planning_task,planning_occurrence,memo_entry,memo_tag restart identity cascade")
        fixture.conn.execute("set timezone='Asia/Shanghai'")
        with ExitStack() as patches:
            for target in (planning_runtime, db, memo):
                patches.enter_context(mock.patch.object(target, "get_client", return_value=fixture.client))
            patches.enter_context(mock.patch.object(planning_runtime, "_now", return_value=NOW))
            patches.enter_context(mock.patch.object(memo, "_now", return_value=NOW))
            patches.enter_context(mock.patch.object(cfg, "GATEWAY_TOKEN", TOKEN))
            for index in range(1, 51):
                task_id = fixture.task()
                fixture.conn.execute("update planning_task set content=%s,estimated_minutes=10 where id=%s", (f"延迟测试待办 {index:02d}", task_id))
                fixture.occurrence(task_id, "2026-10-18")
                fixture.conn.execute("update planning_occurrence set content_snapshot=%s,display_content=%s,planned_minutes=10 where task_id=%s", (f"延迟测试待办 {index:02d}", f"延迟测试待办 {index:02d}", task_id))
            fixture.create(content="延迟测试长期目标", planned_count=30)
            for index in range(1, 151):
                entry = memo.create_entry({"title": f"延迟测试备忘录 {index:03d}", "content": f"测试数据 {index}：规划管理延迟，正文 Straße。", "kind": "note"})
                if index > 130:
                    memo.set_entry_lifecycle(entry["id"], "archive" if index <= 140 else "delete", entry["content_version"])

            class Delay(BaseHTTPMiddleware):
                async def dispatch(self, request, call_next):
                    path = request.url.path
                    record = {"path": path, "method": request.method, "started": monotonic()}
                    if path.startswith("/admin/api/"):
                        requests.append(record)
                    if path.startswith("/admin/api/") and (request.method != "GET" or request.query_params.get("q") or ("/memo/entries/" in path and path.rsplit("/", 1)[-1].isdigit())):
                        await asyncio.sleep(1.5)
                    response = await call_next(request)
                    record.update(status=response.status_code, elapsed=monotonic() - record["started"])
                    return response

            async def health(request):
                return JSONResponse({"status": "disposable local fixture", "tasks": 50, "memos": 150, "artificial_delay_seconds": 1.5})

            async def admin(request):
                html = (ROOT / "admin/index.html").read_text(encoding="utf-8")
                return HTMLResponse(html.replace("<head>", f"<head><script>localStorage.setItem('qi-token','{TOKEN}');</script>"), headers={"Cache-Control": "no-store"})

            async def evidence(request):
                return JSONResponse(requests)

            async def stop(request):
                if request.headers.get("authorization") != f"Bearer {TOKEN}":
                    return JSONResponse({}, status_code=401)
                server.should_exit = True
                return JSONResponse({"stopping": True})

            app = Starlette(middleware=[Middleware(Delay)], routes=[
                Route("/health", health), Route("/status", health), Route("/admin/", admin),
                *planning_api_routes, *memo_api_routes,
                Route("/__local/evidence", evidence), Route("/__local/stop", stop, methods=["POST"]),
                Mount("/admin", StaticFiles(directory=ROOT / "admin", html=True)),
            ])
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8028, log_level="warning", timeout_graceful_shutdown=3))
            log.info("planning_latency_fixture_ready", extra={"tasks": 50, "memos": 150, "port": 8028})
            server.run()
    finally:
        goalbase.GoalPostgresTests.tearDownClass()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        main()
    except Exception:
        log.exception("planning_latency_fixture_failed")
        raise
