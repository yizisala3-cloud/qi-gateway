"""Isolated SQL comparison, not a production/chat latency benchmark.

Uses 1024-dimensional synthetic vectors, no ANN index, warm runs and one client.
Creates/stops its own temporary PostgreSQL; never connects to production DB.
"""
import json
import os
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_memory_recall_pgserver import RecallPostgresTests
from gateway import memory_recall


def run():
    fixture = RecallPostgresTests
    fixture.setUpClass()
    try:
        conn = fixture.conn
        source = (ROOT / "supabase/migrations/20260905010000_rumination_continuity_path.sql").read_text(encoding="utf-8")
        a = source.index("drop function if exists public.match_memories(", source.index("-- 7. Recall:"))
        b = source.index("-- 8. Fast-path write gating", a)
        old = source[a:b].replace("public.match_memories", "public.match_memories_before_recall").replace(
            "public.search_memories_by_keywords", "public.search_memories_by_keywords_before_recall")
        conn.execute(old)
        conn.execute("alter table memories alter column recall_embedding set storage external")
        vector = "[1," + ",".join(["0"] * 1023) + "]"
        result = {
            "scope": "SQL server execution only; no production, HTTP, embedding or LLM latency",
            "server": conn.execute("select version()").fetchone()[0],
            "conditions": "1024 dimensions; no ANN index; 0% and 20% recent recalls; 5 warm samples per query; alternating order",
            "cases": [],
        }
        def measure(sql, args):
            return conn.execute("explain (analyze, timing off, format json) " + sql, args).fetchone()[0][0]["Execution Time"]
        for count, fraction in ((n, f) for n in (1000, 10000, 100000) for f in (0.0, 0.2)):
            conn.execute("truncate memories restart identity")
            conn.execute("""insert into memories(content,title,recall_scene,recall_embedding,last_recalled_at)
                select repeat('聊天记忆正文 ',20), '聊天', '聊天', %s::extensions.vector,
                    case when %s and i %% 5 = 0 then now() else null end
                from generate_series(1,%s) i""", (vector, fraction > 0, count))
            conn.execute("analyze memories")
            case = {"memories": count, "cooling_fraction": fraction}
            for channel, before, after, args in (
                ("keyword", "select * from search_memories_by_keywords_before_recall(array['聊天'],40)",
                 "select * from search_memories_by_keywords(array['聊天'],40,3600)", ()),
                ("vector", "select * from match_memories_before_recall(%s::extensions.vector,0.5,40)",
                 "select * from match_memories(%s::extensions.vector,0.5,40,3600)", (vector,)),
            ):
                measure(before, args)
                measure(after, args)
                pre, post = [], []
                for n in range(5):
                    if n % 2:
                        post.append(measure(after, args)); pre.append(measure(before, args))
                    else:
                        pre.append(measure(before, args)); post.append(measure(after, args))
                case[channel] = {"before_ms": pre, "after_ms": post,
                    "median_before_ms": statistics.median(pre), "median_after_ms": statistics.median(post),
                    "median_delta_ms": statistics.median(post) - statistics.median(pre)}
            case["combined_median_delta_ms"] = sum(case[c]["median_delta_ms"] for c in ("keyword", "vector"))
            result["cases"].append(case)
        # Local snapshot + executor submission only. Worker is released immediately;
        # database and JSON encoding are deliberately absent from the caller.
        class ImmediateExecutor:
            def submit(self, fn, *args):
                memory_recall._slots.release()
        memories = [{"id": n, "title": "标题", "continuity_type": "moment", "injection_text": "字" * 280} for n in range(1, 9)]
        samples = []
        with patch.object(memory_recall, "_executor", ImmediateExecutor()):
            for _ in range(1000):
                started = time.perf_counter_ns()
                memory_recall.record_memories_in_background(memories)
                samples.append((time.perf_counter_ns() - started) / 1e6)
        result["local_snapshot_prepare_ms"] = {"median": statistics.median(samples), "max": max(samples),
                                               "note": "1000 samples; fake submit, no real thread scheduling/DB"}
        real_samples = []
        with ThreadPoolExecutor(max_workers=1) as executor:
            with patch.object(memory_recall, "_executor", executor), patch.object(memory_recall, "_slots", BoundedSemaphore(32)), patch.object(memory_recall, "_persist", return_value=None):
                executor.submit(lambda: None).result()
                for _ in range(32):
                    for _ in range(32):
                        started = time.perf_counter_ns()
                        memory_recall.record_memories_in_background(memories)
                        real_samples.append((time.perf_counter_ns() - started) / 1e6)
                    executor.submit(lambda: None).result()
        result["snapshot_real_submit_ms"] = {"median": statistics.median(real_samples),
            "p95": sorted(real_samples)[int(len(real_samples) * 0.95)], "max": max(real_samples),
            "note": "1024 samples; actual thread submission; DB writer stubbed; no backlog"}
        output = ROOT / "codex/需求/10.20新增召回冷却-召回记录/隔离SQL性能结果.json"
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
    finally:
        fixture.tearDownClass()


if __name__ == "__main__":
    run()
