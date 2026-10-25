"""Build and verify the R15 known-outcome source-only review handoff."""
import difflib
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
BASE = Path(__file__).resolve().parent
REVIEW = ROOT/'codex/审查日志/qqbot接入/修复待复审-26.10.25.00.02'
FILES = [
    'gateway/qqbot.py', 'gateway/qqbot_store.py', 'tests/test_qqbot_recovery_boundaries.py',
    'tests/test_qqbot_lease_outcomes.py', 'codex/需求/qqbot接入/后续待修复.md',
    'codex/功能维护规范/qqbot接入/qqbot接入.md', 'codex/网关当前状态/网关当前状态.md',
    'codex/控制台-文档索引/目录说明.md',
    'codex/代码修改日志/qqbot接入/租约恢复保留已知评估结果-26.10.25.00.02.md',
]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(name, data):
    target = REVIEW/name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def check_links(path, current, previous=''):
    links = lambda text: set(re.findall(r'\]\(([^)]+)\)', text))
    for target in links(current)-links(previous):
        target = target.strip('<>').split('#', 1)[0]
        if target and '://' not in target:
            assert (path.parent/target).resolve().exists(), (path, target)


def main():
    files, patch = [], []
    for name in FILES:
        source = ROOT/name
        old_path = BASE/'source-before'/name
        before = old_path.read_bytes() if old_path.exists() else b''
        after = source.read_bytes()
        old, new = before.decode('utf-8-sig'), after.decode('utf-8-sig')
        if old_path.exists():
            save('source-before/'+name, before)
        save('source-after/'+name, after)
        if name.endswith('.md'):
            check_links(source, new, old)
        old_lines, new_lines = [text.splitlines(keepends=True) for text in (old, new)]
        old_lines, new_lines = [[line.rstrip('\r\n')+'\n' for line in lines]
                               for lines in (old_lines, new_lines)]
        assert old_lines != new_lines, name
        patch.append(f'diff --git a/{name} b/{name}\n')
        if not old_path.exists():
            patch.append('new file mode 100644\n')
        patch.extend(difflib.unified_diff(old_lines, new_lines,
            fromfile='a/'+name if old_path.exists() else '/dev/null', tofile='b/'+name))
        files.append(dict(path=name, before_sha256=sha(before) if old_path.exists() else None,
                          after_sha256=sha(after)))
    patch = ''.join(patch).encode('utf-8')
    save('修复改动.patch', patch)
    assert '437 passed, 27 skipped, 134 subtests passed' in (BASE/'regressions.txt').read_text(encoding='utf-8-sig')
    probes = [ROOT/f'codex/审查日志/qqbot接入/审查摘要-{stamp}/{name}' for stamp, name in [
        ('26.10.24.23.03', 'test_independent_recovery.py'),
        ('26.10.24.23.54', 'test_independent_lease_outcomes.py')]]
    evidence = [dict(path=path.relative_to(ROOT).as_posix(), sha256=sha(path.read_bytes()))
        for path in [*sorted(BASE.glob('*.txt')), *sorted(BASE.glob('*.xml')), Path(__file__), *probes]]
    entry = REVIEW/'复审入口.md'
    manifest = dict(batch='26.10.25.00.02', baseline='local before this fix, not HEAD',
        status='local only; awaiting independent review; no commit, push, deployment or production writes',
        file_count=len(files), files=files, evidence=evidence, patch_sha256=sha(patch),
        review_entry_sha256=sha(entry.read_bytes()),
        tests=dict(passed=437, subtests_passed=134, skipped=27, new_tests_passed=15, new_subtests_passed=6))
    save('manifest.json', (json.dumps(manifest, ensure_ascii=False, indent=2)+'\n').encode('utf-8'))
    check_links(entry, entry.read_text(encoding='utf-8'))
    commands = [
        (['git', 'apply', '--reverse', '--check', '--ignore-space-change', str(REVIEW/'修复改动.patch')], (0,)),
        (['git', '-c', 'core.autocrlf=false', '-c', 'core.whitespace=blank-at-eol,blank-at-eof,space-before-tab,cr-at-eol',
          'diff', '--no-index', '--check', '--', str(REVIEW/'source-before'), str(REVIEW/'source-after')], (0, 1)),
    ]
    for command, expected in commands:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
        assert result.returncode in expected and not result.stdout and not result.stderr, result
    print(json.dumps(dict(files=len(files), evidence=len(evidence), reverse_patch_check=True,
                         whitespace_clean=True, new_document_links_valid=True)))


if __name__ == '__main__':
    main()
