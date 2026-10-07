# -*- coding: utf-8 -*-
"""规划管理旧已删除数据 dry-run 排查工具（2026-10-07 执行文档 §7）。

只读工具：对目标数据库生成旧数据清理候选报告，不做任何写入、删除或
备份。执行清理前必须先跑本工具并人工核对报告；实际删除走与在线删除
一致的 planning_discard_task 事务（按 §25 事实门槛分流），且需要用户
对目标环境的明确执行授权与事先备份。

分类口径（§25 / §31.1）：
* keep      —— 有完成 / 部分完成 / 中空阶段完成 / 提前完成事实（持久
               事实门槛或当前行证据）：保留全部历史。
* delete    —— 已删除（deleted_at 非空）且从无上述事实：拟物理删除
               （任务与关联业务实例）。
* unknown   —— is_active=false 但无删除标记（迁移前旧停用行）：来源
               不明，须人工确认停用来源后归类，不能凭停用状态直接删。

用法：
    PYTHONUTF8=1 python tools/planning_old_data_dryrun.py \
        [--json 输出.json] [--limit 500]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway import db  # noqa: E402
from gateway.planning_common import _CST  # noqa: E402

OPEN_STATUSES = ("pending", "in_progress", "deferred", "partial")

# R15（2026-10-07 复审 #15）：PostgREST 单次 select 受服务端行数上限约束
# （Supabase 默认 1000）。分页读取按各表主键稳定升序键集游标（gt + order +
# limit），直到取不满一页——单页截断不再被当作全量（旧实现第 1001 行之后
# 的完成事实读不到，目标任务被误判 delete）。
PAGE_SIZE = 1000

# 各表分页游标列（主键；planning_creation_request 为 text 主键，同样可排序）。
PAGE_CURSOR_COLUMNS = {
    "planning_task": "id",
    "planning_occurrence": "id",
    "planning_task_completion_fact": "task_id",
    "planning_creation_request": "request_key",
}


def _rows(client, table, query_fn=None):
    """稳定分页读取（只读）：按主键升序键集游标翻页，覆盖服务端单页上限
    之外的全部行；读取期间新提交的更大主键行同样进入结果。"""
    cursor_column = PAGE_CURSOR_COLUMNS.get(table)
    rows: list[dict] = []
    cursor = None
    while True:
        query = client.table(table).select("*")
        if query_fn:
            query = query_fn(query)
        if cursor is not None and cursor_column:
            query = query.gt(cursor_column, cursor)
        if cursor_column:
            query = query.order(cursor_column)
        page = query.limit(PAGE_SIZE).execute().data or []
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        if cursor_column is None:
            return rows
        cursor = page[-1].get(cursor_column)
        if cursor is None:
            return rows


def _collect(client, limit: int) -> list[dict]:
    """收集全部 inactive 任务及其事实证据（只读）。"""
    tasks = _rows(client, "planning_task", lambda q: q.eq("is_active", False))
    facts = _rows(client, "planning_task_completion_fact")
    fact_task_ids = {row.get("task_id") for row in facts}
    occurrences = _rows(client, "planning_occurrence")
    tombstones = _rows(client, "planning_creation_request")

    by_task: dict[int, list[dict]] = {}
    for occ in occurrences:
        by_task.setdefault(occ.get("task_id"), []).append(occ)
    # 其他任务对某实例的重排引用（request_source_occurrence_id）
    source_refs: dict[int, list[int]] = {}
    for task in _rows(client, "planning_task"):
        ref = task.get("request_source_occurrence_id")
        if ref is not None:
            source_refs.setdefault(ref, []).append(task["id"])
    tomb_keys = {row.get("request_key") for row in tombstones}

    report = []
    truncated = len(tasks) > limit
    for task in tasks[:limit]:
        task_id = task["id"]
        occs = by_task.get(task_id, [])
        completed = [o for o in occs if o.get("status") == "completed"]
        partial = [o for o in occs if o.get("partial_at") is not None]
        early = [o for o in occs if o.get("source") == "early"]
        hollow_start_done = [o for o in occs
                             if o.get("phase") == "start"
                             and o.get("status") == "completed"]
        has_fact = (
            task_id in fact_task_ids
            or bool(completed) or bool(partial) or bool(hollow_start_done))
        refs = sorted({
            ref_id for occ in occs
            for ref_id in source_refs.get(occ.get("id"), [])
        })
        if has_fact:
            verdict = "keep"
            reason = "存在完成/部分完成/中空阶段完成事实（§25.1）"
        elif task.get("deleted_at"):
            verdict = "delete"
            reason = "已删除且从无完成事实（§25.2 拟物理删除）"
        else:
            verdict = "unknown"
            reason = "is_active=false 且无删除标记：迁移前旧停用行，须人工确认来源"
        report.append({
            "task_id": task_id,
            "content": task.get("content"),
            "task_type": task.get("task_type"),
            "verdict": verdict,
            "reason": reason,
            "deleted_at": task.get("deleted_at"),
            "refresh_enabled": task.get("refresh_enabled"),
            "request_state": task.get("request_state"),
            "creation_request_key": task.get("creation_request_key"),
            "creation_key_tombstoned": (
                task.get("creation_request_key") in tomb_keys
                if task.get("creation_request_key") else None),
            "occurrence_count": len(occs),
            "occurrence_status": dict(Counter(
                o.get("status") for o in occs)),
            "open_occurrences": sum(
                1 for o in occs if o.get("status") in OPEN_STATUSES),
            "completed_rows": len(completed),
            "partial_rows": len(partial),
            "early_rows": len(early),
            "hollow_start_completed": len(hollow_start_done),
            "referenced_by_tasks": refs,
        })
    # 孤儿实例（任务行缺失——正常不应出现，出现即数据异常，单独列出）
    task_ids = {t["id"] for t in tasks}
    all_task_ids = task_ids | {
        t["id"] for t in _rows(client, "planning_task")}
    orphans = [
        {"occurrence_id": o.get("id"), "task_id": o.get("task_id"),
         "status": o.get("status")}
        for o in occurrences if o.get("task_id") not in all_task_ids
    ]
    return report, orphans, {
        "total_tasks": len(tasks),
        "total_occurrences": len(occurrences),
        "fact_rows": len(facts),
        "tombstones": len(tombstones),
        "truncated_by_limit": truncated,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", dest="json_path", default=None,
                        help="同时输出 JSON 报告到该路径")
    parser.add_argument("--limit", type=int, default=500,
                        help="最多处理的 inactive 任务数（默认 500）")
    args = parser.parse_args()

    client = db.get_client()
    if not client:
        print("错误：Supabase 未配置（检查环境变量），无法连接目标数据库。")
        return 2

    report, orphans, totals = _collect(client, args.limit)
    counts = Counter(item["verdict"] for item in report)
    now = datetime.now(_CST).isoformat()

    print("=" * 72)
    print("规划管理旧已删除数据 dry-run 报告（只读，未做任何修改）")
    print(f"生成时间：{now}（Asia/Shanghai）")
    print(f"数据库任务总数（inactive 候选 {totals['total_tasks']}、"
          f"实例 {totals['total_occurrences']}、事实行 {totals['fact_rows']}、"
          f"已删除创建登记 {totals['tombstones']}）")
    if totals.get("truncated_by_limit"):
        print(f"注意：inactive 候选超过 --limit（{args.limit}），本报告只覆盖"
              f"前 {args.limit} 个任务；其余候选未判定，不得据此执行清理。")
    print("=" * 72)
    print(f"拟物理删除（delete）：{counts.get('delete', 0)}")
    print(f"应保留（keep）：{counts.get('keep', 0)}")
    print(f"来源不明（unknown）：{counts.get('unknown', 0)}")
    if orphans:
        print(f"异常孤儿实例（任务行缺失）：{len(orphans)}")
    print("-" * 72)
    for item in report:
        print(f"[{item['verdict'].upper():7}] task={item['task_id']}"
              f" {item['content']!r}（{item['task_type']}）")
        print(f"         {item['reason']}")
        print(f"         deleted_at={item['deleted_at']} "
              f"refresh_enabled={item['refresh_enabled']} "
              f"request_state={item['request_state']}")
        print(f"         实例 {item['occurrence_count']} 条"
              f"（开放 {item['open_occurrences']}）"
              f" 状态分布={item['occurrence_status']} "
              f"完成行 {item['completed_rows']} / partial {item['partial_rows']}"
              f" / early {item['early_rows']}"
              f" / 中空开始完成 {item['hollow_start_completed']}")
        if item["creation_request_key"]:
            print(f"         创建键 {item['creation_request_key']}"
                  f"（登记表已有 tombstone：{item['creation_key_tombstoned']}）")
        if item["referenced_by_tasks"]:
            print(f"         被其他任务的重排请求引用：{item['referenced_by_tasks']}")
    if orphans:
        print("-" * 72)
        print("孤儿实例（任务行缺失，属数据异常，请人工核查）：")
        for o in orphans:
            print(f"  occurrence={o['occurrence_id']} task={o['task_id']}"
                  f" status={o['status']}")
    print("=" * 72)
    print("后续执行边界：")
    print("1. delete 组须经用户确认后，先做可恢复备份，再逐任务调用")
    print("   planning_discard_task（与在线删除同一事务规则）；")
    print("2. unknown 组必须先人工确认停用来源（暂停刷新 / 被取代 / 旧废弃），")
    print("   不得凭 is_active=false 直接删除；")
    print("3. keep 组保留全部历史，不做任何处理。")

    if args.json_path:
        payload = {
            "generated_at": now,
            "totals": totals,
            "verdict_counts": dict(counts),
            "candidates": report,
            "orphan_occurrences": orphans,
        }
        Path(args.json_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"JSON 报告已写入：{args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
