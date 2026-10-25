"""Source-only handoff for R13-R15; does not stage, commit or deploy."""
import difflib
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
BASE = Path(__file__).resolve().parent
REVIEW = ROOT/'codex/审查日志/qqbot接入/修复待复审-26.10.24.23.18'
FILES = [
    'gateway/qqbot.py', 'gateway/qqbot_store.py', 'admin/js/pages/config.js',
    'admin/js/app.js', 'admin/index.html', 'tests/qqbot_config_mount.test.mjs',
    'tests/test_qqbot_recovery_boundaries.py',
    'codex/需求/qqbot接入/后续待修复.md', 'codex/功能维护规范/qqbot接入/qqbot接入.md',
    'codex/网关当前状态/网关当前状态.md', 'codex/控制台-文档索引/目录说明.md',
    'codex/代码修改日志/qqbot接入/主动评估三项审查问题修复-26.10.24.23.18.md',
]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(relative, data):
    path = REVIEW/relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def main():
    manifest, patch = [], []
    links = lambda text: set(re.findall(r'\]\(([^)]+)\)', text))
    for name in FILES:
        before_path = BASE/'source-before'/name
        before = before_path.read_bytes() if before_path.exists() else b''
        after = (ROOT/name).read_bytes()
        if before_path.exists():
            save('source-before/'+name, before)
        save('source-after/'+name, after)
        old, new = before.decode('utf-8-sig'), after.decode('utf-8-sig')
        if name.endswith('.md'):
            for target in links(new)-links(old):
                target = target.strip('<>').split('#', 1)[0]
                if target and '://' not in target:
                    assert ((ROOT/name).parent/target).resolve().exists(), (name, target)
        old_lines, new_lines = [text.splitlines(keepends=True) for text in (old, new)]
        old_lines, new_lines = [[line.rstrip('\r\n')+'\n' for line in lines]
                                for lines in (old_lines, new_lines)]
        assert old_lines != new_lines, name
        patch.append(f'diff --git a/{name} b/{name}\n')
        if not before_path.exists():
            patch.append('new file mode 100644\n')
        patch.extend(difflib.unified_diff(old_lines, new_lines,
            fromfile='a/'+name if before_path.exists() else '/dev/null', tofile='b/'+name))
        manifest.append(dict(path=name, before_sha256=sha(before) if before_path.exists() else None,
                             after_sha256=sha(after)))
    patch = ''.join(patch).encode('utf-8')
    save('修复改动.patch', patch)
    entry = REVIEW/'复审入口.md'
    regression = (BASE/'regressions.txt').read_text(encoding='utf-8-sig')
    assert '419 passed, 27 skipped, 128 subtests passed' in regression
    probes = ROOT/'codex/审查日志/qqbot接入/审查摘要-26.10.24.23.03/test_independent_recovery.py'
    evidence = [dict(path=path.relative_to(ROOT).as_posix(), sha256=sha(path.read_bytes()))
        for path in [*sorted(BASE.glob('*.txt')), *sorted(BASE.glob('*.xml')), Path(__file__), probes]]
    result = dict(batch='26.10.24.23.18', scope='R13, R14, R15 only; local before-fix baseline, not HEAD',
        status='locally fixed; awaiting independent review; no commit, push, deployment, or production writes',
        file_count=len(manifest), files=manifest, evidence=evidence,
        patch_sha256=sha(patch), review_entry_sha256=sha(entry.read_bytes()),
        tests=dict(passed=419, subtests_passed=128, skipped=27, independent_probes_passed=3))
    save('manifest.json', (json.dumps(result, ensure_ascii=False, indent=2)+'\n').encode('utf-8'))
    for target in links(entry.read_text(encoding='utf-8')):
        target = target.strip('<>').split('#', 1)[0]
        if target and '://' not in target:
            assert (entry.parent/target).resolve().exists(), target
    for args in [
        ['git', 'apply', '--reverse', '--check', '--ignore-space-change', str(REVIEW/'修复改动.patch')],
        ['git', '-c', 'core.autocrlf=false', '-c', 'core.whitespace=blank-at-eol,blank-at-eof,space-before-tab,cr-at-eol',
         'diff', '--no-index', '--check', '--', str(REVIEW/'source-before'), str(REVIEW/'source-after')],
    ]:
        checked = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
        assert checked.returncode in (0, 1) and not checked.stdout and not checked.stderr, checked
    print(json.dumps(dict(files=len(manifest), evidence=len(evidence), patch_matches=True,
                         whitespace_clean=True, new_document_links_valid=True)))


if __name__ == '__main__':
    main()
