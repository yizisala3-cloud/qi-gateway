"""10.16 browser fixture: actual APIs and disposable PostgreSQL, port 8026."""
import json
import logging
import os
from datetime import timedelta
from pathlib import Path
from unittest import mock

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from gateway import db, planning_runtime as runtime, planning_tasks as tasks
from gateway.config import cfg
from gateway.planning_api import planning_api_routes
from tests.test_planning_goals_pgserver import GoalPostgresTests
from tests.support.planning_goal_sql import GoalSqlClient

ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger('preview.planning_optimization')


def main():
    import uvicorn
    os.environ['QIGATEWAY_PG_PLANNING_TEST'] = '1'
    GoalPostgresTests.setUpClass()
    fixture = GoalPostgresTests()
    fixture.client = GoalSqlClient(fixture.conn)
    # A controlled morning clock makes same-round window checks reproducible
    # even when the harness is run near the daily boundary.
    fixture_now = runtime._now().replace(hour=7,minute=0,second=0,microsecond=0)
    patches = [mock.patch.object(runtime,'get_client',return_value=fixture.client),
               mock.patch.object(db,'get_client',return_value=fixture.client),
               mock.patch.object(cfg,'GATEWAY_TOKEN','local-optimization-preview'),
               mock.patch.object(runtime,'_now',return_value=fixture_now),
               mock.patch.object(tasks,'_generate_created_task_quietly'),
               mock.patch.object(tasks,'_generate_due_quietly')]
    try:
        for patch in patches: patch.start()
        fixture.conn.execute("set timezone='Asia/Shanghai'")
        day = runtime._now().date().isoformat()
        ids = {}
        for key, label in (('ordinary','阅读并整理笔记'),('second','散步二十分钟'),('started','正在练习书法'),('hollow','洗衣和晾衣')):
            task = fixture.task(key == 'hollow')
            occurrence_ids = fixture.occurrence(task,day,hollow=key == 'hollow')
            fixture.conn.execute('update planning_task set content=%s where id=%s',(label,task))
            fixture.conn.execute('update planning_occurrence set content_snapshot=%s,display_content=%s,planned_minutes=30 where task_id=%s',(label,label,task))
            ids[key]={'task':task,'occurrences':occurrence_ids}
        started = ids['started']['occurrences'][0]
        fixture.conn.execute("update planning_occurrence set status='in_progress',actual_start=%s where id=%s",(fixture_now-timedelta(minutes=5),started))
        goal=fixture.create(content='坚持阅读',planned_count=30)
        fixture.link(goal,ids['ordinary']['task'])

        async def health(request): return JSONResponse({'status':'isolated local fixture'})
        async def admin(request):
            html=(ROOT/'admin/index.html').read_text(encoding='utf-8')
            return HTMLResponse(html.replace('<head>',"<head><script>localStorage.setItem('qi-token','local-optimization-preview');</script>"),headers={'Cache-Control':'no-store'})
        async def empty(request): return JSONResponse([])
        async def stop(request):
            if request.headers.get('authorization')!='Bearer local-optimization-preview': return JSONResponse({},status_code=401)
            server.should_exit=True
            return JSONResponse({'stopping':True})
        app=Starlette(routes=[Route('/status',health),Route('/health',health),Route('/admin/',admin),
            *planning_api_routes,Route('/admin/api/memo/tags',empty),Route('/__local/stop',stop,methods=['POST']),
            Mount('/admin',StaticFiles(directory=ROOT/'admin',html=True))])
        (ROOT/'.venv/optimization-preview-fixture.json').write_text(json.dumps({'ids':ids,'goal':goal['id'],
            'token':'local-optimization-preview','url':'http://127.0.0.1:8026/admin/#/planning'}),encoding='utf-8')
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=8026,log_level='warning',timeout_graceful_shutdown=3))
        log.info('isolated_planning_optimization_ready',extra={'port':8026})
        server.run()
    finally:
        try:
            log.info('isolated_planning_optimization_stopping')
            if GoalPostgresTests.conn is not None:
                GoalPostgresTests.conn.close()
                GoalPostgresTests.conn=None
            if GoalPostgresTests.server is not None:
                # PostgreSQL smart shutdown can wait indefinitely for HTTP
                # worker connections after an intentionally lost response.
                # This cluster is disposable; fast shutdown cancels them.
                from pgserver._commands import pg_ctl
                pg_ctl(['-w','-m','fast','-t','10','stop'],pgdata=GoalPostgresTests.pgdata,
                       user=GoalPostgresTests.server.system_user,timeout=15)
                # We have positively stopped this cluster. Release pgserver's
                # handle without attempting a second stop against a recycled PID;
                # the test fixture below removes its disposable data directory.
                GoalPostgresTests.server.cleanup_mode = None
            GoalPostgresTests.tearDownClass()
            log.info('isolated_planning_optimization_stopped')
        finally:
            for patch in reversed(patches): patch.stop()


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    logging.getLogger('pgserver').setLevel(logging.WARNING)
    try: main()
    except Exception:
        log.exception('isolated_planning_optimization_failed')
        raise
