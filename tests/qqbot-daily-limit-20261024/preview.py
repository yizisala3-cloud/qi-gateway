"""Isolated browser preview: real settings API, fake QQ transport, no remote DB."""
import json
from contextlib import asynccontextmanager
from pathlib import Path
import socket
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse,JSONResponse
from starlette.routing import Mount,Route
from starlette.staticfiles import StaticFiles
from gateway.config import cfg
from gateway.qqbot import QQRuntime
from gateway.qqbot_settings import QQSettings,qqbot_settings_routes
from tests.test_qqbot import Model,Transport

BASE=Path(__file__).resolve().parent
CANDIDATE=ROOT/'qqbot/private/daily-limit-20261024/candidate'
cfg.GATEWAY_TOKEN='daily-limit-preview-only'
temporary=tempfile.TemporaryDirectory(prefix='qqbot-daily-limit-')
folder=Path(temporary.name)
env={'QQBOT_ENABLED':'true','QQBOT_TOKEN':'x'*32,'QQBOT_ACCOUNT_ID':'12345','QQBOT_OWNER_ID':'67890',
     'QQBOT_ASSISTANT_ID':'role','QQBOT_STORE_PATH':str(folder/'ledger.sqlite3')}

@asynccontextmanager
async def lifespan(app):
    manager=QQSettings(app,None,env=env,path=folder/'settings.json',runtime_factory=lambda config,client:
        QQRuntime(config,model=Model(),transport=Transport(),exporter=lambda *args:None))
    app.state.qqbot_settings=manager;await manager.startup()
    try:yield
    finally:await manager.shutdown();temporary.cleanup()

async def health(request):return JSONResponse({'status':'ok','uptime':1})
async def status(request):return JSONResponse({'upstream_model':'isolated-preview','supabase':{}})
async def optional(request):return JSONResponse({})
def artifact(path):
    async def serve(request):return FileResponse(CANDIDATE/path)
    return serve

app=Starlette(lifespan=lifespan,routes=[*qqbot_settings_routes,Route('/health',health),Route('/status',status),
    Route('/admin/api/{path:path}',optional),Route('/admin/',artifact('admin/index.html')),
    Route('/admin/js/app.js',artifact('admin/js/app.js')),Route('/admin/js/pages/config.js',artifact('admin/js/pages/config.js')),
    Mount('/admin',StaticFiles(directory=ROOT/'admin',html=True))])
if __name__=='__main__':
    listener=socket.socket();listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    (BASE/'preview-server.json').write_text(json.dumps({'port':port}),encoding='utf8')
    uvicorn.Server(uvicorn.Config(app,log_level='warning',access_log=False)).run(sockets=[listener])
