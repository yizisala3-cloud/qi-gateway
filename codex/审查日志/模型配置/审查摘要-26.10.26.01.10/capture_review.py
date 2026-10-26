"""Capture reviewed source without touching business files or private config."""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[4]
out = Path(__file__).resolve().parent
implementation = json.loads((root / "codex/临时验证/新增配置切换-20261026/implementation-manifest.json").read_text("utf-8"))
files = implementation["files"] + [{"path": "gateway/logging_events.py"}]
records = []
for item in files:
    source = root / item["path"]
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    target = out / "source-snapshot" / item["path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    records.append({"path": item["path"], "sha256": digest,
                    "matches_implementation_manifest": digest == item["sha256"] if "sha256" in item else None})
(out / "reviewed-source-manifest.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), "utf-8")
for name, command in (("head.txt", ["git", "log", "-1", "--format=%H%n%s"]),
                      ("tracked-tree.txt", ["git", "-c", "core.quotepath=false", "ls-files"]),
                      ("reviewed-tracked-diff.patch", ["git", "diff", "--", *[i["path"] for i in files]])):
    result = subprocess.run(command, cwd=root, capture_output=True, check=True)
    (out / name).write_bytes(result.stdout)
assert all(r["matches_implementation_manifest"] is not False for r in records), "Source changed since implementation manifest"
print(json.dumps({"snapshots": len(records), "implementation_files_matching": len(implementation["files"])}, ensure_ascii=False))
