"""Separate core and identity Supabase fakes, preserving their existing semantics."""

from types import SimpleNamespace

from .planning_rpc import (
    emulate_planning_round_patch,
    emulate_planning_once_task_guarded,
    emulate_planning_insert_once_occurrence,
    emulate_planning_insert_round_occurrence,
    emulate_planning_discard_task,
    emulate_planning_split_occurrence,
    emulate_planning_recompute_batch,
    emulate_planning_request_recompute,
    emulate_planning_clear_recompute_mark,
    emulate_planning_update_cycle_boundary,
)


UNIQUE_VIOLATION = (
    'duplicate key value violates unique constraint '
    '"planning_occurrence_round_phase_uq"'
)

# 创建请求幂等（清单 #9）：planning_task 部分唯一索引
# planning_task_creation_key_uq（creation_request_key 非空行唯一）的
# fake 仿真——真实索引语义由迁移 20261004020000 定义。
CREATION_KEY_VIOLATION = (
    'duplicate key value violates unique constraint '
    '"planning_task_creation_key_uq"'
)


def _assert_creation_key_unique(rows, item) -> None:
    key = item.get("creation_request_key")
    if key is None:
        return
    if any(existing.get("creation_request_key") == key for existing in rows):
        raise RuntimeError(CREATION_KEY_VIOLATION)


def _mark_completion_fact(rows: dict, row: dict) -> None:
    """planning_occurrence_completion_fact_guard 触发器仿真（20261007010000）：

    completed 状态或 partial_at 写入的同一语句内登记任务级完成事实门槛
    （幂等，task_id 主键）；独立小表不触碰任务行，与删除 RPC 的任务行锁
    不形成反向等待。"""
    if row.get("status") != "completed" and row.get("partial_at") is None:
        return
    task_id = row.get("task_id")
    if task_id is None:
        return
    facts = rows.setdefault("planning_task_completion_fact", [])
    if not any(f.get("task_id") == task_id for f in facts):
        facts.append({"task_id": task_id, "first_fact_at": row.get("updated_at")})


class CoreQuery:
    def __init__(self, client, table):
        self.client = client
        self.table = table
        self.filters = []
        self.orders = []
        self.row_limit = None
        self.op = "select"
        self.payload = None
        self.ignore_duplicates = False

    def select(self, fields="*"):
        self.op = "select"
        return self

    def insert(self, data):
        self.op = "insert"
        self.payload = [dict(item) for item in data] if isinstance(data, list) else dict(data)
        return self

    def update(self, data):
        self.op = "update"
        self.payload = dict(data)
        return self

    def delete(self):
        self.op = "delete"
        return self

    def upsert(self, data, ignore_duplicates=False, on_conflict=None):
        self.op = "upsert"
        # 批次 6 一轮 Review BLOCKER 3：多行 upsert 模拟单条
        # INSERT .. ON CONFLICT (id) DO UPDATE 的语句级原子语义。
        self.payload = [dict(item) for item in data] if isinstance(data, list) else dict(data)
        self.ignore_duplicates = ignore_duplicates
        return self

    def eq(self, field, value):
        self.filters.append(("eq", field, value))
        return self

    def in_(self, field, values):
        self.filters.append(("in", field, list(values)))
        return self

    def is_(self, field, value):
        self.filters.append(("is", field, value))
        return self

    def gte(self, field, value):
        self.filters.append(("gte", field, value))
        return self

    def lte(self, field, value):
        self.filters.append(("lte", field, value))
        return self

    def lt(self, field, value):
        self.filters.append(("lt", field, value))
        return self

    def order(self, field, desc=False):
        self.orders.append((field, desc))
        return self

    def limit(self, value):
        self.row_limit = value
        return self

    def _compare(self, row_value, kind, value):
        if row_value is None:
            return False
        if kind == "gte":
            return row_value >= value
        if kind == "lte":
            return row_value <= value
        if kind == "lt":
            return row_value < value
        return False

    def _matches(self, row):
        for kind, field, value in self.filters:
            row_value = row.get(field)
            if kind == "eq" and row_value != value:
                return False
            if kind == "in" and row_value not in value:
                return False
            if kind == "is" and (row_value is None) != (value is None):
                return False
            if kind in ("gte", "lte", "lt") and not self._compare(row_value, kind, value):
                return False
        return True

    def execute(self):
        rows = self.client.rows.setdefault(self.table, [])
        if self.op == "insert":
            payloads = self.payload if isinstance(self.payload, list) else [self.payload]
            if self.table == "planning_occurrence":
                for item in payloads:
                    if item.get("round_key") is None:
                        continue
                    for existing in rows:
                        if (
                            existing.get("task_id") == item.get("task_id")
                            and existing.get("round_key") == item.get("round_key")
                            and existing.get("phase") == item.get("phase")
                        ):
                            raise RuntimeError(UNIQUE_VIOLATION)
            if self.table == "planning_task":
                for item in payloads:
                    _assert_creation_key_unique(rows, item)
            inserted = []
            for item in payloads:
                row = dict(item)
                row["id"] = self.client.next_id(self.table)
                rows.append(row)
                if self.table == "planning_occurrence":
                    _mark_completion_fact(self.client.rows, row)
                inserted.append(dict(row))
            return SimpleNamespace(data=inserted)
        if self.op == "upsert":
            if isinstance(self.payload, list):
                updated = []
                by_id = {row.get("id"): row for row in rows}
                for item in self.payload:
                    target = by_id.get(item.get("id"))
                    if target is not None:
                        target.update(item)
                        updated.append(dict(target))
                    elif not self.ignore_duplicates:
                        new_row = dict(item)
                        new_row.setdefault("id", self.client.next_id(self.table))
                        rows.append(new_row)
                        updated.append(dict(new_row))
                return SimpleNamespace(data=updated)
            key = self.payload.get("id")
            matched = [row for row in rows if row.get("id") == key]
            if matched and not self.ignore_duplicates:
                for row in matched:
                    row.update(self.payload)
                return SimpleNamespace(data=[dict(row) for row in matched])
            if matched and self.ignore_duplicates:
                return SimpleNamespace(data=[dict(row) for row in matched])
            row = dict(self.payload)
            row.setdefault("id", self.client.next_id(self.table))
            rows.append(row)
            return SimpleNamespace(data=[dict(row)])
        matched = [row for row in rows if self._matches(row)]
        for field, desc in self.orders:
            matched.sort(key=lambda row: row.get(field) or "", reverse=desc)
        if self.row_limit is not None:
            matched = matched[: self.row_limit]
        if self.op == "update":
            for row in matched:
                row.update(self.payload)
                if self.table == "planning_occurrence":
                    _mark_completion_fact(self.client.rows, row)
        if self.op == "delete":
            for row in matched:
                rows.remove(row)
        return SimpleNamespace(data=[dict(row) for row in matched])


class CoreClient:
    """最小 supabase 查询构造器替身：内存行 + 乐观 id + 轮次唯一约束。"""

    def __init__(self):
        self.rows = {
            "planning_task": [],
            "planning_occurrence": [],
            "planning_recompute_state": [
                {"id": 1, "requested_at": None, "reason": None, "request_token": None}],
            "app_settings": [],
            # 20261007010000：完成事实门槛与已删除创建操作登记。
            "planning_task_completion_fact": [],
            "planning_creation_request": [],
        }
        self._counters = {}

    def next_id(self, table):
        self._counters[table] = self._counters.get(table, 0) + 1
        return self._counters[table]

    def rpc(self, fn, params=None):
        # 批次 6 二轮：round 原子补丁 RPC 的 fake 仿真（复用 phase1a 的
        # 共享实现；真库语义由 pgserver 套件验证）。
        from tests.support.planning_rpc import (emulate_planning_round_patch,
            emulate_planning_once_task_guarded, emulate_planning_insert_once_occurrence,
            emulate_planning_discard_task)
        if fn == "planning_patch_occurrence_round":
            emulate_planning_round_patch(self.rows["planning_occurrence"], dict(params or {}))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=[]))
        if fn == "planning_update_once_task_guarded":
            emulate_planning_once_task_guarded(
                self.rows["planning_task"], self.rows["planning_occurrence"],
                dict(params or {}))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=[]))
        if fn == "planning_insert_once_occurrence":
            emulate_planning_insert_once_occurrence(self, dict(params or {}))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=[]))
        if fn == "planning_insert_round_occurrence":
            from tests.support.planning_rpc import emulate_planning_insert_round_occurrence
            emulate_planning_insert_round_occurrence(self, dict(params or {}))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=[]))
        if fn == "planning_discard_task":
            from tests.support.planning_rpc import emulate_planning_discard_task
            data = emulate_planning_discard_task(
                self.rows["planning_task"], self.rows["planning_occurrence"],
                dict(params or {}),
                fact_rows=self.rows.get("planning_task_completion_fact"),
                tombstone_rows=self.rows.get("planning_creation_request"))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))
        if fn == "planning_split_occurrence":
            from tests.support.planning_rpc import emulate_planning_split_occurrence
            data = emulate_planning_split_occurrence(self, dict(params or {}))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))
        if fn == "planning_apply_recompute_batch":
            from tests.support.planning_rpc import emulate_planning_recompute_batch
            data = emulate_planning_recompute_batch(self, dict(params or {}))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))
        if fn == "planning_request_recompute":
            from tests.support.planning_rpc import emulate_planning_request_recompute
            emulate_planning_request_recompute(
                self.rows["planning_recompute_state"], dict(params or {}))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=[]))
        if fn == "planning_clear_recompute_mark":
            from tests.support.planning_rpc import emulate_planning_clear_recompute_mark
            emulate_planning_clear_recompute_mark(
                self.rows["planning_recompute_state"], dict(params or {}))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=[]))
        if fn == "planning_update_cycle_boundary":
            from tests.support.planning_rpc import emulate_planning_update_cycle_boundary
            data = emulate_planning_update_cycle_boundary(self, dict(params or {}))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))
        raise AssertionError(f"unknown rpc: {fn}")

    def table(self, name):
        if name not in self.rows:
            raise AssertionError(f"unexpected table access: {name}")
        return CoreQuery(self, name)



class IdentityQuery:
    def __init__(self, db, name):
        self.db, self.name, self.filters = db, name, []
        self.action, self.payload, self.max_rows = "select", None, None

    def select(self, *_):
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def in_(self, key, values):
        self.filters.append((key, set(values)))
        return self

    def is_(self, key, value):
        self.filters.append((key, ("is", value)))
        return self

    def gte(self, key, value):
        self.filters.append((key, ("gte", value)))
        return self

    def lt(self, key, value):
        self.filters.append((key, ("lt", value)))
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, value):
        self.max_rows = value
        return self

    def insert(self, value):
        self.action, self.payload = "insert", value
        return self

    def update(self, value):
        self.action, self.payload = "update", value
        return self

    def delete(self):
        self.action = "delete"
        return self

    def upsert(self, value, ignore_duplicates=False, on_conflict=None):
        self.action = "upsert"
        # 批次 6 一轮 Review BLOCKER 3：多行 upsert 模拟单条
        # INSERT .. ON CONFLICT (id) DO UPDATE 的语句级原子语义——
        # 要么全部行应用、要么（异常注入时）全部不应用。
        self.payload = [dict(item) for item in value] if isinstance(value, list) else dict(value)
        self.ignore_duplicates = ignore_duplicates
        self.on_conflict = on_conflict
        return self

    def execute(self):
        rows = self.db.rows[self.name]
        if self.action == "upsert":
            if isinstance(self.payload, list):
                self.db.writes.append((self.name, {"__upsert_rows__": len(self.payload)}))
                updated = []
                by_id = {row.get("id"): row for row in rows}
                for item in self.payload:
                    target = by_id.get(item.get("id"))
                    if target is not None:
                        target.update(item)
                        updated.append(dict(target))
                    elif self.ignore_duplicates:
                        continue
                    else:
                        new_row = dict(item)
                        new_row.setdefault("id", self.db.next_id(self.name))
                        rows.append(new_row)
                        updated.append(dict(new_row))
                return SimpleNamespace(data=updated)
            key = self.payload.get("id")
            matched = [row for row in rows if row.get("id") == key]
            if matched:
                for row in matched:
                    row.update(self.payload)
                return SimpleNamespace(data=[dict(row) for row in matched])
            row = dict(self.payload)
            row.setdefault("id", self.db.next_id(self.name))
            rows.append(row)
            return SimpleNamespace(data=[dict(row)])
        if self.action == "insert":
            inserted = []
            for item in self.payload if isinstance(self.payload, list) else [self.payload]:
                if self.name == "planning_occurrence" and any(
                    old.get("task_id") == item.get("task_id")
                    and old.get("round_key") == item.get("round_key")
                    and old.get("phase") == item.get("phase") for old in rows
                ):
                    raise RuntimeError("planning_occurrence_round_phase_uq")
                if self.name == "planning_task":
                    _assert_creation_key_unique(rows, item)
                row = {**item, "id": self.db.next_id(self.name)}
                rows.append(row)
                if self.name == "planning_occurrence":
                    _mark_completion_fact(self.db.rows, row)
                inserted.append(dict(row))
            return SimpleNamespace(data=inserted)
        def matches(row):
            for key, value in self.filters:
                actual = row.get(key)
                if isinstance(value, set):
                    match = actual in value
                elif isinstance(value, tuple) and value[0] == "gte":
                    match = actual is not None and actual >= value[1]
                elif isinstance(value, tuple) and value[0] == "lt":
                    match = actual is not None and actual < value[1]
                elif isinstance(value, tuple) and value[0] == "is":
                    match = (actual is None) == (value[1] is None)
                else:
                    match = actual == value
                if not match:
                    return False
            return True

        matched = [row for row in rows if matches(row)]
        if self.max_rows is not None:
            matched = matched[:self.max_rows]
        if self.action == "update":
            self.db.writes.append((self.name, dict(self.payload)))
            for row in matched:
                row.update(self.payload)
                if self.name == "planning_occurrence":
                    _mark_completion_fact(self.db.rows, row)
        if self.action == "delete":
            for row in matched:
                rows.remove(row)
        return SimpleNamespace(data=[dict(row) for row in matched])



class IdentityRpcCall:
    """镜像迁移 1B 两个原子合并函数的语义（真库行为由 pgserver 套件验证）。

    - planning_takeover_reschedule_request：CAS 接管身份，expected 键随同
      一语句原子并入 absorbed；条件不命中返回 False（PostgREST 布尔）。
    - planning_absorb_reschedule_request：幂等原子登记（已是 current 或已
      absorbed 时不写）。
    """

    def __init__(self, db, fn, params):
        self.db, self.fn, self.params = db, fn, dict(params or {})

    @staticmethod
    def _merge(absorbed, *keys):
        merged = list(absorbed or [])
        for key in keys:
            if key and key not in merged:
                merged.append(key)
        return merged

    def execute(self):
        rows = self.db.rows["planning_task"]
        task_id = self.params.get("p_task_id")
        row = next((r for r in rows if r.get("id") == task_id), None)
        now = self.params.get("p_now")
        if self.fn == "planning_takeover_reschedule_request":
            expected = self.params.get("p_expected_key")
            if not row or not expected or row.get("request_key") != expected:
                return SimpleNamespace(data=False)
            row["request_key"] = self.params["p_new_key"]
            row["request_est_start"] = self.params["p_new_est_start"]
            row["request_absorbed_keys"] = self._merge(
                row.get("request_absorbed_keys"), expected)
            row["updated_at"] = now
            return SimpleNamespace(data=True)
        if self.fn == "planning_patch_occurrence_round":
            emulate_planning_round_patch(self.db.rows["planning_occurrence"], self.params)
            return SimpleNamespace(data=[])
        if self.fn == "planning_update_once_task_guarded":
            emulate_planning_once_task_guarded(
                self.db.rows["planning_task"], self.db.rows["planning_occurrence"],
                self.params)
            return SimpleNamespace(data=[])
        if self.fn == "planning_insert_once_occurrence":
            emulate_planning_insert_once_occurrence(self.db, self.params)
            return SimpleNamespace(data=[])
        if self.fn == "planning_insert_round_occurrence":
            emulate_planning_insert_round_occurrence(self.db, self.params)
            return SimpleNamespace(data=[])
        if self.fn == "planning_discard_task":
            data = emulate_planning_discard_task(
                self.db.rows["planning_task"], self.db.rows["planning_occurrence"],
                self.params,
                fact_rows=self.db.rows.setdefault("planning_task_completion_fact", []),
                tombstone_rows=self.db.rows.setdefault("planning_creation_request", []))
            return SimpleNamespace(data=data)
        if self.fn == "planning_split_occurrence":
            data = emulate_planning_split_occurrence(self.db, self.params)
            return SimpleNamespace(data=data)
        if self.fn == "planning_apply_recompute_batch":
            data = emulate_planning_recompute_batch(self.db, self.params)
            return SimpleNamespace(data=data)
        if self.fn == "planning_request_recompute":
            emulate_planning_request_recompute(
                self.db.rows["planning_recompute_state"], self.params)
            return SimpleNamespace(data=[])
        if self.fn == "planning_clear_recompute_mark":
            emulate_planning_clear_recompute_mark(
                self.db.rows["planning_recompute_state"], self.params)
            return SimpleNamespace(data=[])
        if self.fn == "planning_update_cycle_boundary":
            data = emulate_planning_update_cycle_boundary(self.db, self.params)
            return SimpleNamespace(data=data)
        if self.fn == "planning_absorb_reschedule_request":
            key = self.params.get("p_request_key")
            if (not row or not key
                    or row.get("request_key") == key
                    or key in (row.get("request_absorbed_keys") or [])):
                return SimpleNamespace(data=False)
            row["request_absorbed_keys"] = self._merge(
                row.get("request_absorbed_keys"), key)
            row["updated_at"] = now
            return SimpleNamespace(data=True)
        raise AssertionError(f"unknown rpc: {self.fn}")


class IdentityDatabase:
    def __init__(self):
        self.rows = {"planning_task": [], "planning_occurrence": [],
                     "planning_recompute_state": [
                         {"id": 1, "requested_at": None, "reason": None,
                          "request_token": None}],
                     "planning_task_completion_fact": [],
                     "planning_creation_request": []}
        self.counters, self.writes = {}, []

    def next_id(self, name):
        self.counters[name] = self.counters.get(name, 0) + 1
        return self.counters[name]

    def table(self, name):
        return IdentityQuery(self, name)

    def rpc(self, fn, params=None):
        return IdentityRpcCall(self, fn, params)
