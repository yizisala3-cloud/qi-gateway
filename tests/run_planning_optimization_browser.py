"""Start a fresh disposable API/PG fixture, run Edge acceptance, then stop it."""
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
OUTPUT=Path(os.environ.get('PLANNING_BROWSER_OUTPUT', ROOT/'tests/planning-optimization-20261016'))
log=logging.getLogger('test.planning_optimization_browser')


def run():
    OUTPUT.mkdir(exist_ok=True)
    (OUTPUT/'tmp').mkdir(exist_ok=True)
    env=dict(os.environ,PYTHONUTF8='1',TEMP=str(OUTPUT/'tmp'),TMP=str(OUTPUT/'tmp'))
    with (OUTPUT/'preview.log').open('w',encoding='utf-8') as stream:
        process=subprocess.Popen([sys.executable,'-m','tools.preview_planning_optimization'],cwd=ROOT,env=env,stdout=stream,stderr=stream)
        try:
            deadline=time.monotonic()+55
            while True:
                if process.poll() is not None: raise RuntimeError(f'Fixture exited: {process.returncode}; see {OUTPUT / "preview.log"}')
                try:
                    with urllib.request.urlopen('http://127.0.0.1:8026/health',timeout=1) as response:
                        if response.status==200: break
                except (urllib.error.URLError,TimeoutError,ConnectionError):
                    if time.monotonic()>=deadline: raise RuntimeError('Fixture did not become ready within 55 seconds')
                    time.sleep(.2)
            script=sys.argv[1] if len(sys.argv)>1 else 'tests/planning_optimization.browser.cjs'
            result=subprocess.run(['node',script],cwd=ROOT,env=env,timeout=180)
            return result.returncode
        finally:
            if process.poll() is None:
                try:
                    req=urllib.request.Request('http://127.0.0.1:8026/__local/stop',method='POST',headers={'Authorization':'Bearer local-optimization-preview'})
                    with urllib.request.urlopen(req,timeout=5) as response: response.read()
                    process.wait(timeout=30)
                    if process.returncode:
                        raise RuntimeError(f'Disposable fixture exited with {process.returncode}; see preview.log')
                except (urllib.error.URLError,TimeoutError,ConnectionError,subprocess.TimeoutExpired):
                    log.exception('isolated_browser_fixture_shutdown_failed',extra={'pid':process.pid})
                    process.terminate()
                    process.wait(timeout=10)
                    raise RuntimeError('Browser acceptance finished, but disposable fixture cleanup failed')
            elif process.returncode:
                raise RuntimeError(f'Disposable fixture exited with {process.returncode}; see preview.log')


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    sys.exit(run())
