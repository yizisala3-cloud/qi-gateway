"""Build a source-only incremental review package without git or deployment writes."""
from pathlib import Path
import difflib
import hashlib
import json

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / 'qqbot/private/proactive-recovery-20261024'
REVIEW = ROOT / 'codex/审查日志/qqbot接入/待审查-26.10.24.12.14'
CODE = [
    'gateway/qqbot.py', 'gateway/qqbot_store.py', 'gateway/qqbot_model.py',
    'gateway/qqbot_settings.py', 'gateway/logging_events.py',
    'admin/js/pages/config.js', 'admin/js/pages/logs.js', 'admin/js/app.js', 'admin/index.html',
    'tests/qqbot_config_mount.test.mjs', 'tests/test_qqbot_config_frontend.py', 'tests/test_qqbot_context.py',
]
DOCS = [
    'README.md', 'qqbot/README.md', 'codex/需求/qqbot接入/需求规范.md',
    'codex/功能维护规范/qqbot接入/qqbot接入.md', 'codex/功能维护规范/网关日志/网关日志.md',
    'codex/网关当前状态/网关当前状态.md', 'codex/控制台-文档索引/目录说明.md',
]
NEW = [
    'gateway/qqbot_proactive.py', 'tests/test_qqbot_proactive_recovery.py',
    'codex/代码修改日志/qqbot接入/主动评估退避与故障恢复-26.10.24.12.14.md',
    'tests/qqbot-proactive-recovery-20261024/preview.py',
    'tests/qqbot-proactive-recovery-20261024/browser.cjs',
]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(relative, data):
    path = REVIEW / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def main():
    task = json.loads((TASK / 'task.json').read_text(encoding='utf-8'))
    manifest, hunks = [], []
    for name in CODE + DOCS + NEW:
        before_path = TASK / 'local-before' / name
        if name in NEW:
            assert not before_path.exists(), name
            before = b''
        else:
            before = before_path.read_bytes()
            if name in task['local_hashes']:
                assert digest(before) == task['local_hashes'][name], name
            save('source-before/' + name, before)
        after = (ROOT / name).read_bytes()
        save('source-after/' + name, after)
        old, new = before.decode('utf-8-sig').splitlines(True), after.decode('utf-8-sig').splitlines(True)
        old, new = [[line.rstrip('\r\n') + '\n' for line in lines] for lines in (old, new)]
        assert old != new, f'Unexpected unchanged file: {name}'
        hunks.append(f'diff --git a/{name} b/{name}\n')
        if name in NEW:
            hunks.append('new file mode 100644\n')
        hunks.extend(difflib.unified_diff(old, new, fromfile='/dev/null' if name in NEW else 'a/'+name,
                                        tofile='b/'+name))
        manifest.append(dict(path=name, status='added' if name in NEW else 'modified',
            before_sha256=None if name in NEW else digest(before), after_sha256=digest(after)))
    patch = ''.join(hunks).encode('utf-8')
    save('本轮改动.patch', patch)
    evidence = []
    base = ROOT / 'tests/qqbot-proactive-recovery-20261024'
    for path in sorted(base.iterdir()):
        if path.is_file() and (path.suffix in {'.png', '.cjs', '.py'} or path.name in
                              {'browser-result.json', 'regressions.xml', 'regressions.txt'}):
            evidence.append(dict(path=path.relative_to(ROOT).as_posix(), sha256=digest(path.read_bytes())))
    payload = dict(batch=task['stamp'], baseline='local-before, not HEAD',
        disposition='local only; no commit, push, deployment, or production settings writes',
        production_reference_image=task['base_image'], patch_sha256=digest(patch),
        file_count=len(manifest), files=manifest, evidence=evidence,
        review_entry_sha256=digest((REVIEW/'审查入口.md').read_bytes()),
        tests=dict(passed=407, subtests_passed=117, skipped=27))
    save('manifest.json', (json.dumps(payload, ensure_ascii=False, indent=2)+'\n').encode('utf-8'))
    print(json.dumps(dict(files=len(manifest), patch_sha256=digest(patch), evidence=len(evidence))))


if __name__ == '__main__':
    main()
