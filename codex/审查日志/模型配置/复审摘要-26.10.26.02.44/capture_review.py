import difflib
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[4]
out = Path(__file__).resolve().parent
previous = root / "codex/审查日志/模型配置/复审摘要-26.10.26.02.02"
old = json.loads((previous / "reviewed-source-manifest.json").read_text("utf-8"))
latest = json.loads((root / "codex/临时验证/模型配置登录与缓存补修-20261026/source-manifest.json").read_text("utf-8"))
expected = {item["path"]: item["sha256"] for item in old} | latest
reviewed_style_update = {"admin/js/lib/model_settings.js", "admin/js/pages/config.js",
                         "admin/js/app.js", "admin/index.html", "admin/css/style.css"}
records, changes = [], []
for name, expected_hash in expected.items():
    file = root / name
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    assert digest == expected_hash or name in reviewed_style_update, name
    target = out / "source-snapshot" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(file, target)
    records.append({"path": name, "sha256": digest,
                    "matches_prior_or_login_manifest": digest == expected_hash})
    before = previous / "source-snapshot" / name
    if before.exists():
        original = before.read_text("utf-8").splitlines(True)
        before_name = "previous-review/" + name
    else:
        git_file = subprocess.run(["git", "show", "HEAD:" + name], cwd=root, capture_output=True)
        original = git_file.stdout.decode("utf-8").splitlines(True) if git_file.returncode == 0 else []
        before_name = "HEAD/" + name if git_file.returncode == 0 else "/dev/null"
    changes.extend(difflib.unified_diff(original, file.read_text("utf-8").splitlines(True),
                                      fromfile=before_name, tofile=name))
(out / "reviewed-source-manifest.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), "utf-8")
(out / "login-fix-delta.patch").write_text("".join(changes), "utf-8")
# Replay the prior independent browser probes with this review's own endpoint
# and output directory, without editing the historical script.
script = (previous / "browser_independent.cjs").read_text("utf-8")
assert "http://127.0.0.1:8842" in script
(out / "browser_prior_independent.cjs").write_text(script.replace("http://127.0.0.1:8842", "http://127.0.0.1:8877"), "utf-8")
matching = sum(hashlib.sha256((root / name).read_bytes()).hexdigest() == digest for name, digest in latest.items())
print(json.dumps({"reviewed_files": len(records), "login_manifest_files_matching": matching,
                  "reviewed_style_updates": sum(not item["matches_prior_or_login_manifest"] for item in records)}, ensure_ascii=False))
