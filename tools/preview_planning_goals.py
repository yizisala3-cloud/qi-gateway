"""Actual goal/task HTTP APIs + admin UI on disposable local PostgreSQL.

Run: .venv/Scripts/python.exe -m tools.preview_planning_goals
No production client is created. The full migration chain runs in a temporary
cluster; task background generation/recompute is disabled in this local fixture.
"""
import json
import logging
import os
from pathlib import Path
from unittest import mock

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from gateway import planning_runtime as runtime, planning_tasks as tasks
from gateway.config import cfg
from gateway.planning_api import planning_api_routes
from tests.test_planning_goals_pgserver import GoalPostgresTests
from tests.support.planning_goal_sql import GoalSqlClient

log = logging.getLogger('preview.planning_goals')
ROOT = Path(__file__).resolve().parents[1]


def main():
    import uvicorn
    os.environ['QIGATEWAY_PG_PLANNING_TEST'] = '1'
    GoalPostgresTests.setUpClass()
    fixture = GoalPostgresTests()
    fixture.client = GoalSqlClient(fixture.conn)
    patches = [
        mock.patch.object(runtime, 'get_client', return_value=fixture.client),
        mock.patch('gateway.db.get_client', return_value=fixture.client),
        mock.patch.object(cfg, 'GATEWAY_TOKEN', 'local-goals-preview'),
        mock.patch('gateway.db.load_app_setting', return_value=None),
        mock.patch.object(tasks, '_generate_created_task_quietly'),
        mock.patch.object(tasks, '_generate_due_quietly'),
        mock.patch('gateway.planning_occurrences.recompute.request_recompute'),
    ]
    try:
        for patch in patches: patch.start()
        task, hollow = fixture.task(), fixture.task(True)
        fixture.conn.execute('update planning_task set content=%s where id=%s', ('每日阅读，记下一句有启发的话', task))
        fixture.conn.execute('update planning_task set content=%s where id=%s', ('开始拉伸、结束记录', hollow))
        today = runtime._now().date().isoformat()
        ordinary = fixture.occurrence(task, today)[0]
        phases = fixture.occurrence(hollow, today, hollow=True)
        goal = fixture.create(content='完成阅读计划', target_value='200.5', planned_count=40)
        fixture.link(goal, task)
        fixture.create(content='慢慢积累新的习惯', unit='天', target_value='100', planned_count=30)

        async def health(request): return JSONResponse({'status': 'local fixture'})
        async def home(request): return RedirectResponse('/admin/')
        async def admin_home(request):
            html = (ROOT / 'admin/index.html').read_text(encoding='utf-8')
            return HTMLResponse(html.replace('<head>', "<head><script>localStorage.setItem('qi-token','local-goals-preview');</script>"),
                                headers={'Cache-Control': 'no-store'})
        async def memo_empty(request): return JSONResponse([])
        async def stop(request):
            if request.headers.get('authorization') != 'Bearer local-goals-preview':
                return JSONResponse({}, status_code=401)
            server.should_exit = True
            return JSONResponse({'stopping': True})
        app = Starlette(routes=[
            Route('/', home), Route('/health', health), Route('/status', health),
            Route('/admin/', admin_home),
            *planning_api_routes,
            Route('/admin/api/memo/tags', memo_empty),
            Route('/__local/stop', stop, methods=['POST']),
            Mount('/admin', StaticFiles(directory=ROOT / 'admin', html=True)),
        ])
        evidence = ROOT / '.venv' / 'goals-preview-fixture.json'
        evidence.write_text(json.dumps({'goal_id': goal['id'], 'task_id': task,
            'ordinary': ordinary, 'hollow_task': hollow, 'phases': phases,
            'token': 'local-goals-preview', 'url': 'http://127.0.0.1:8021/admin/#/planning'}), encoding='utf-8')
        log.info('isolated_goal_preview_ready', extra={'evidence': str(evidence)})
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=8021, log_level='info'))
        server.run()
    finally:
        for patch in reversed(patches): patch.stop()
        GoalPostgresTests.tearDownClass()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    try:
        main()
    except Exception:
        log.exception('isolated_goal_preview_failed')
        raise
