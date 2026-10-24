"""Real config.js behavior in the deterministic DOM, no deployment credentials."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


def test_real_configuration_switch_and_async_boundaries():
    root=Path(__file__).resolve().parents[1]
    node=shutil.which('node')
    assert node,'Node runtime is required for frontend verification'
    with tempfile.TemporaryDirectory(prefix='sky-config-') as folder:
        target=Path(folder)
        shutil.copytree(root/'admin/js',target/'admin/js')
        (target/'package.json').write_text(json.dumps({'type':'module'}),encoding='utf8')
        result=subprocess.run([node,'--test-reporter=tap',str(root/'tests/sky_config.test.mjs'),str(target)],
            capture_output=True,text=True,encoding='utf8',timeout=40)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
    assert '# tests 12' in result.stdout and '# fail 0' in result.stdout
