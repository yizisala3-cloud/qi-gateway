"""Local review preview: real QQ/log APIs, fake provider/QQ and memory log storage."""
import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import logging
from pathlib import Path
import socket
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount,Route
from starlette.staticfiles import StaticFiles
from gateway import log_api,log_store,logging_events
from gateway.config import cfg
from gateway.qqbot import QQRuntime
from gateway.qqbot_settings import QQSettings,qqbot_settings_routes
from tests.test_qqbot import Clock,Model,Transport,event

BASE=Path(__file__).resolve().parent
cfg.GATEWAY_TOKEN='qqbot-recovery-preview-only'
cfg.GATEWAY_LOG_ENABLED=True
logging_events._queue=logging_events._EventQueue(100)
logging_events._capture_active=True
capture=logging_events.GatewayLogHandler();logging.getLogger('gateway.qqbot').addHandler(capture)
events=[];clock=Clock();seed_number=0
temporary=tempfile.TemporaryDirectory(prefix='qqbot-recovery-preview-');folder=Path(temporary.name)

class PreviewRuntime(QQRuntime):
    def start(self):self.activate()

def refresh_events():
    events.extend(logging_events._queue.drain(100))
    return events

def groups():
    result={}
    for row in refresh_events():
        key=row['fingerprint']
        if key not in result:
            result[key]={**row,'event_count':0,'first_seen_at':row['occurred_at']}
        result[key].update(last_seen_at=row['occurred_at'],sample_event_id=row['event_id'],message_zh=row['message_zh'])
        result[key]['event_count']+=1
    return list(result.values())

def query_groups(**filters):
    data=groups()
    for field in ['module','level']:
        if filters.get(field):data=[row for row in data if row[field]==filters[field]]
    return {'groups':data,'has_more':False,'visible_snapshot':'100:110:'}

def facets(**kwargs):
    data=refresh_events()
    return {'modules':[{'module':key,'count':value} for key,value in Counter(row['module'] for row in data).items()],
        'levels':[{'level':key,'count':value} for key,value in Counter(row['level'] for row in data).items()],
        'total_events':len(data),'keys':[]}

log_store.query_groups=query_groups
log_store.facets=facets
log_store.get_event=lambda event_id:next((row for row in refresh_events() if row['event_id']==event_id),None)
log_store.get_group=lambda fingerprint,**kwargs:{
    'group':next((row for row in groups() if row['fingerprint']==fingerprint),None),
    'samples':[row for row in refresh_events() if row['fingerprint']==fingerprint]}

async def seed_fault(app):
    global seed_number
    runtime=app.state.qqbot;seed_number+=1
    if runtime.evaluation_status()['fault_stopped']:
        runtime.resume_evaluation(runtime.evaluation_status()['fault_version'])
    original=runtime.config
    runtime.config=replace(original,proactive_enabled=True,quiet_seconds=1,cooldown_seconds=60,
        active_start='00:00',active_end='00:00')
    runtime.model=Model();runtime.transport.ready=True
    await runtime.ingest(event(seed_number,'isolated preview message'));clock.advance(4)
    await runtime.reply_once();await runtime.send_once()
    clock.advance(400)
    async def failed(*args,**kwargs):raise httpx.ReadTimeout('isolated provider timeout')
    runtime.model.generate=failed
    for i in range(5):
        await runtime.proactive_once()
        if i<4:clock.advance(runtime.store.evaluation_state()['next_check_at']-clock())
    runtime.config=original;runtime.model=Model();refresh_events()

@asynccontextmanager
async def lifespan(app):
    env={'QQBOT_ENABLED':'true','QQBOT_TOKEN':'x'*32,'QQBOT_ACCOUNT_ID':'12345','QQBOT_OWNER_ID':'67890',
        'QQBOT_ASSISTANT_ID':'role','QQBOT_STORE_PATH':str(folder/'ledger.sqlite3'),'QQBOT_DAILY_LIMIT':'30',
        'QQBOT_TIMEZONE':'Asia/Shanghai','QQBOT_ACTIVE_START':'00:00','QQBOT_ACTIVE_END':'00:00'}
    manager=QQSettings(app,None,env=env,path=folder/'settings.json',runtime_factory=lambda config,client:
        PreviewRuntime(config,model=Model(),transport=Transport(),now=clock,exporter=lambda *args:None))
    app.state.qqbot_settings=manager;await manager.startup()
    clock.on_advance=lambda:app.state.qqbot.store.acquire(app.state.qqbot.owner,clock())
    await seed_fault(app)
    try:yield
    finally:
        await manager.shutdown();logging.getLogger('gateway.qqbot').removeHandler(capture);temporary.cleanup()

async def health(request):return JSONResponse({'status':'ok','uptime':1})
async def status(request):return JSONResponse({'upstream_model':'isolated-preview','supabase':{}})
async def optional(request):return JSONResponse({})
async def log_settings(request):return JSONResponse({'enabled':True,'writable':True})
async def inject(request):
    await seed_fault(request.app)
    return JSONResponse(request.app.state.qqbot_settings.snapshot())

app=Starlette(lifespan=lifespan,routes=[*qqbot_settings_routes,*log_api.log_api_routes,
    Route('/health',health),Route('/status',status),Route('/__test/fault',inject,methods=['POST']),
    Route('/admin/api/logs/settings',log_settings),Route('/admin/api/{path:path}',optional),
    Mount('/admin',StaticFiles(directory=ROOT/'admin',html=True))])
if __name__=='__main__':
    listener=socket.socket();listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    (BASE/'preview-server.json').write_text(json.dumps({'port':port}),encoding='utf8')
    uvicorn.Server(uvicorn.Config(app,log_level='warning',access_log=False)).run(sockets=[listener])
