import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def test_console_loading_receipts_and_retry_identity():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='qigate-console-latency-') as directory:
        workspace = Path(directory)
        shutil.copytree(root / 'admin/js', workspace / 'admin/js')
        (workspace / 'package.json').write_text(json.dumps({'type': 'module'}), encoding='utf-8')
        result = subprocess.run(['node', '--test-reporter=tap', str(root / 'tests/console_latency.test.mjs'), str(workspace)],
                                capture_output=True, encoding='utf-8', timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '# tests 8' in result.stdout
    assert '# fail 0' in result.stdout
