import difflib
import hashlib
import json
import shutil
from pathlib import Path

root = Path(__file__).resolve().parents[4]
out = Path(__file__).resolve().parent
old = root / "codex/审查日志/模型配置/审查摘要-26.10.26.01.10"
fix = json.loads((root / "codex/临时验证/模型配置五项补修-20261026/fix-manifest.json").read_text("utf-8"))
records, delta = [], []
for item in fix["files"]:
    source = root / item["path"]
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert digest == item["sha256"], item["path"]
    target = out / "source-snapshot" / item["path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    records.append({"path": item["path"], "sha256": digest})
    before = old / "source-snapshot" / item["path"]
    delta.extend(difflib.unified_diff(before.read_text("utf-8").splitlines(True) if before.exists() else [],
                                    source.read_text("utf-8").splitlines(True),
                                    fromfile="previous-review/" + item["path"], tofile=item["path"]))
for name, digest in fix["original_review_files"].items():
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name
(out / "reviewed-source-manifest.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), "utf-8")
(out / "fix-delta.patch").write_text("".join(delta), "utf-8")
print(json.dumps({"fix_source_files_matching": len(records), "original_review_files_unchanged": len(fix["original_review_files"])}, ensure_ascii=False))
