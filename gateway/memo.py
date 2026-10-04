"""备忘录服务层（一期，2026-10-02 确认需求规范.md §1–§8）。

数据与事务语义全部落在 ``memo_*`` 数据库 RPC（见
``supabase/migrations/20261002060000_memo_phase1.sql``）；本模块负责：
读取组装（首页板块 / 标签详情 / 搜索 / 归档与回收站列表）、展示字段
派生（display_title / body_excerpt）、RPC 错误码到 HTTP 语义的映射。

RPC 错误码（5 字符 SQLSTATE）：
    ME001 not_found → 404
    ME002 version_conflict → 409（陈旧自动保存 / 陈旧生命周期操作）
    ME003 lifecycle_conflict → 409（状态机不允许的迁移，含已归档/已删除记录
        被迟到自动保存命中的场景——不是「恢复为正常记录」的路径）
    ME004 concurrent_modified → 409（重排成员集合已变化，需基于最新数据重排）
    ME005 invalid_payload → 400
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import get_client

log = logging.getLogger("gateway.memo")

# 时间基准与网关其他模块一致（Asia/Shanghai）
_CST = timezone(timedelta(hours=8))


def _now() -> datetime:
    return datetime.now(_CST)


def _iso(dt: datetime) -> str:
    return dt.isoformat()

KINDS = ("pinned", "note")
KIND_LABELS = {"pinned": "常驻备忘", "note": "随笔"}
STATUSES = ("active", "archived", "deleted")

# 与迁移同源的数据上限（§2.2/§2.3：用途不是字数限制；这里是防滥用边界）
MAX_TITLE_LENGTH = 200
MAX_CONTENT_LENGTH = 50000
MAX_TAG_NAME_LENGTH = 30

# 展示摘要截断（首行折叠空白后保留的字符数）
EXCERPT_MAX_LENGTH = 120
# 搜索命中片段：命中点前后保留的字符数
SEARCH_CONTEXT_CHARS = 48

# RPC errcode → (HTTP 状态码, error_code)
_RPC_ERROR_MAP = {
    "ME001": (404, "not_found"),
    "ME002": (409, "version_conflict"),
    "ME003": (409, "lifecycle_conflict"),
    "ME004": (409, "concurrent_modified"),
    "ME005": (400, "invalid_payload"),
}


def purge_expired_trash(client) -> int:
    """回收站到期清扫（M19）：硬删除删除满 72 小时的记录，返回条数。

    保留期与到期语义由数据库函数 ``memo_purge_expired_trash`` 承担
    （迁移 20261004010000_memo_trash_retention_72h.sql）；本层在读取与
    生命周期入口惰性调用。清扫失败不阻塞主流程：恢复路径的「到期无法
    恢复」由生命周期 RPC 的保留期门独立兜底，未清扫的过期条目只是
    暂留列表，待下一次成功清扫移除。
    """
    try:
        result = _call_rpc(client, "memo_purge_expired_trash", {"p_now": _iso(_now())})
    except MemoError:
        log.warning("memo 回收站到期清扫失败（不阻塞读取）", exc_info=True)
        return 0
    return int(result or 0)


class MemoError(Exception):
    """业务错误：message 为用户可读中文，code 为机器可读错误码。"""

    def __init__(self, message: str, status_code: int = 400,
                 code: str = "invalid_payload", details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.details = details


def _require_client():
    client = get_client()
    if not client:
        raise MemoError("数据库暂不可用，请稍后重试", 503, "database_unavailable")
    return client


def _rpc_error(exc: Exception) -> MemoError:
    """把数据库 RPC 抛出的 errcode 映射为 MemoError；未识别的一律 503。

    数据库消息是工程文本，不透给前端；用户文案按错误码取固定中文，
    原文只进日志。
    """
    rpc_code = getattr(exc, "code", None)
    if rpc_code in _RPC_ERROR_MAP:
        status, code = _RPC_ERROR_MAP[rpc_code]
        log.warning("memo RPC 拒绝（%s）: %s", rpc_code, exc)
        generic = {
            "ME001": "内容不存在或已被删除",
            "ME002": "内容已在其他设备更新，保存被拒绝",
            "ME003": "该记录已归档或已删除，不能继续编辑",
            "ME004": "列表已发生变化，请刷新后重试",
            "ME005": "请求内容不合法",
        }
        return MemoError(generic.get(rpc_code, "操作失败"), status, code)
    log.exception("memo RPC 基础设施失败")
    return MemoError("数据库操作暂时失败，请稍后重试", 503, "database_unavailable")


def _call_rpc(client, fn: str, params: dict) -> Any:
    try:
        response = client.rpc(fn, params).execute()
    except MemoError:
        raise
    except Exception as exc:
        raise _rpc_error(exc) from exc
    return response.data



# 分页读取的稳定排序键（BUG-01/R02）：必须是各表真实存在的唯一键。
# memo_entry_tag / memo_position 是复合主键表（(entry_id, tag_id) /
# (group_id, entry_id)），没有 id 列——按 id 排序会被 PostgREST/PostgreSQL
# 以 42703 拒绝；其余表按 identity 主键 id 排序。
_TABLE_ORDER_KEYS: dict[str, tuple[str, ...]] = {
    "memo_entry": ("id",),
    "memo_tag": ("id",),
    "memo_group": ("id",),
    "memo_entry_tag": ("entry_id", "tag_id"),
    "memo_position": ("group_id", "entry_id"),
}


def _is_range_not_satisfiable(exc: Exception) -> bool:
    """识别 PostgREST 的 416 范围错误（PGRST103）。

    真实服务端在请求范围起点越过匹配总数时返回 416，body 带
    ``{"code": "PGRST103", ...}``（postgrest SDK 解析为 APIError.code）；
    旧版本无 code 字段时 SDK 以数字状态码 416 兜底。PostgreSQL 的
    SQLSTATE 形如 "23505"，不会与这两个字面量相撞。
    """
    code = getattr(exc, "code", None)
    return code is not None and str(code) in ("PGRST103", "416")


def _rows(client, table: str, query_fn=None, *, paginate: bool = True,
          order_by: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """表读取。

    Supabase/PostgREST 单请求默认最多返回 1000 行（服务端 Max Rows，
    项目侧还可能配得更小），看板、搜索、列表、重排集合都必须拿到完整
    集合，因此统一按稳定排序分页取全（F07），不依赖项目把上限调大。
    单行读取（limit(1)）不需要分页。

    页长 1000；offset 每页按实际返回行数推进，服务端把每页截小也不会
    跳行。终止条件：空页，累计行数达到最新 count（每页都刷新，不沿用
    首页旧值），或后续页返回 416/PGRST103——并发归档/删除使匹配集合
    缩减后，下一请求的 offset 可能已越过最新总数，真实服务端以 416
    拒绝该范围；按已声明的分页读取一致性边界，这只说明「剩余不足一页」，
    已取行即全量，正常终止（BUG-01）。其他数据库失败原样上抛，不吞成
    成功。并发下集合变化时读到的是「每页发出时刻」的前缀一致快照：新增
    行可能在后续页出现、移除行已取到的仍保留；以空页、最新 count 或 416
    收敛，不保证跨页事务级一致性快照（个人规模全量组装的既有边界）。
    """
    if order_by is None:
        order_by = _TABLE_ORDER_KEYS.get(table)
        if order_by is None:
            raise MemoError(f"表 {table} 缺少稳定排序键定义", 500, "internal_error")
    if not paginate:
        query = client.table(table).select("*")
        if query_fn:
            query = query_fn(query)
        response = query.execute()
        return response.data or []

    page_size = 1000
    rows: list[dict[str, Any]] = []
    offset = 0
    total: int | None = None
    while True:
        query = client.table(table).select("*", count="exact")
        if query_fn:
            query = query_fn(query)
        for field in order_by:
            query = query.order(field)
        query = query.range(offset, offset + page_size - 1)
        try:
            response = query.execute()
        except Exception as exc:
            if _is_range_not_satisfiable(exc):
                break   # 集合缩减导致范围越界：已取行即全量，有限终止
            raise
        page = response.data or []
        rows.extend(page)
        count = getattr(response, "count", None)
        if isinstance(count, int) and count >= 0:
            total = count
        if not page:
            break
        if total is not None and len(rows) >= total:
            break
        offset += len(page)
    return rows


# ── 展示字段派生（纯函数，供看板 / 列表 / 搜索共用） ──────────────

def _first_line(content: str) -> str:
    for line in (content or "").splitlines():
        collapsed = " ".join(line.split())
        if collapsed:
            return collapsed
    return ""


def _second_line(content: str) -> str:
    seen_first = False
    for line in (content or "").splitlines():
        collapsed = " ".join(line.split())
        if not collapsed:
            continue
        if not seen_first:
            seen_first = True
            continue
        return collapsed
    return ""


def _truncate(text: str, limit: int = EXCERPT_MAX_LENGTH) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def display_fields(title: str | None, content: str) -> dict[str, str]:
    """条目展示字段（§2.3/M07）。

    display_title：标题为空时以正文首行兜底；
    body_excerpt：一行正文预览——有标题取首行，无标题取次行（首行已作
    标题兜底，避免同一行重复显示）。
    """
    first = _first_line(content)
    if title:
        display_title = _truncate(" ".join(title.split()))
        body = first
    else:
        display_title = _truncate(first)
        body = _second_line(content)
    return {
        "display_title": display_title,
        "body_excerpt": _truncate(body),
    }


def _entry_tags_map(entry_tags: list[dict]) -> dict[int, list[int]]:
    result: dict[int, list[int]] = {}
    for row in entry_tags:
        result.setdefault(row["entry_id"], []).append(row["tag_id"])
    return result


def _positions_map(positions: list[dict]) -> dict[int, dict[int, int]]:
    result: dict[int, dict[int, int]] = {}
    for row in positions:
        result.setdefault(row["group_id"], {})[row["entry_id"]] = row["position"]
    return result


def build_board(tags: list[dict], groups: list[dict], entries: list[dict],
                entry_tags: list[dict], positions: list[dict]) -> dict:
    """首页板块组装（纯函数；§3.2/§3.3/§4）。

    - 板块顺序 = memo_tag.position 升序；「未分类」固定排在最后（§4.1 只
      约定标签板块手排与新增标签置末，未分类不是标签、不参与拖动）。
    - 每个板块 preview ≤ 5 条（常驻 + 随笔合计）：先按常驻手动顺序取，
      剩余位置补当前随笔模式下的随笔；常驻满 5 条不再补随笔。
    - sections 附带完整 pinned_order / note_order id 列表（预览是展示限制，
      不是排序集合；重排必须保留第 6 条及之后的记录，§8.5/§8.7）。
    - 无活跃内容的标签不出板块（板块是内容展示单元；标签本身仍出现在
      筛选下拉与标签详情）。
    """
    mode_by_group = {g["tag_id"]: g.get("note_sort_mode", "latest") for g in groups}
    group_id_by_tag = {g["tag_id"]: g["id"] for g in groups}
    tags_map = _entry_tags_map(entry_tags)
    pos_map = _positions_map(positions)

    by_tag: dict[int | None, list[dict]] = {}
    for entry in entries:
        if entry.get("status") != "active":
            continue
        entry_tag_ids = tags_map.get(entry["id"], [])
        if entry_tag_ids:
            for tag_id in entry_tag_ids:
                by_tag.setdefault(tag_id, []).append(entry)
        else:
            by_tag.setdefault(None, []).append(entry)

    sections: list[dict] = []
    for tag in sorted(tags, key=lambda t: (t.get("position", 0), t["id"])):
        section_entries = by_tag.get(tag["id"], [])
        if not section_entries:
            continue
        sections.append(_section_payload(
            tag_payload={"id": tag["id"], "name": tag["name"], "position": tag.get("position", 0)},
            group_id=group_id_by_tag.get(tag["id"]),
            section_entries=section_entries,
            note_mode=mode_by_group.get(tag["id"], "latest"),
            positions=pos_map,
            tags_map=tags_map,
        ))

    untagged_entries = by_tag.get(None, [])
    if untagged_entries:
        sections.append(_section_payload(
            tag_payload=None,
            group_id=group_id_by_tag.get(None),
            section_entries=untagged_entries,
            note_mode=mode_by_group.get(None, "latest"),
            positions=pos_map,
            tags_map=tags_map,
        ))

    return {"sections": sections}


def _section_payload(*, tag_payload: dict | None, group_id: int | None,
                     section_entries: list[dict], note_mode: str,
                     positions: dict[int, dict[int, int]],
                     tags_map: dict[int, list[int]]) -> dict:
    group_positions = positions.get(group_id, {}) if group_id is not None else {}
    pinned, notes = _ordered_sections(section_entries, note_mode, group_positions)
    ordered = pinned + notes
    return {
        "tag": tag_payload,
        "note_sort_mode": note_mode,
        # preview = 5 条预览（§3.2）；items = 板块完整有序条目（§3.3 标签
        # 详情 / 重排提交的数据源，不受预览限制）
        "preview": [_preview_item(entry, tags_map) for entry in ordered[:5]],
        "items": [_preview_item(entry, tags_map) for entry in ordered],
        "pinned_order": [entry["id"] for entry in pinned],
        "note_order": [entry["id"] for entry in notes],
        "pinned_count": len(pinned),
        "note_count": len(notes),
    }


def _ordered_sections(section_entries: list[dict], note_mode: str,
                      group_positions: dict[int, int]) -> tuple[list[dict], list[dict]]:
    def pinned_key(entry):
        pos = group_positions.get(entry["id"])
        if pos is None:
            return (1, 0, entry["created_at"], entry["id"])
        return (0, pos, entry["created_at"], entry["id"])

    pinned = sorted((e for e in section_entries if e["kind"] == "pinned"), key=pinned_key)
    note_entries = [e for e in section_entries if e["kind"] == "note"]
    if note_mode == "manual":
        def note_manual_key(entry):
            pos = group_positions.get(entry["id"])
            if pos is None:
                # 未定位成员（手动模式期间新增）按创建先后补末尾（§4.3）
                return (1, 0, entry["created_at"], entry["id"])
            return (0, pos, entry["created_at"], entry["id"])
        notes = sorted(note_entries, key=note_manual_key)
    else:
        # latest：创建时间从新到旧；编辑旧随笔不把它推到最前（§4.3）
        notes = sorted(note_entries, key=lambda e: (_sort_ts(e), e["id"]), reverse=True)
    return pinned, notes


def _sort_ts(entry: dict) -> str:
    return entry.get("created_at") or ""


def _preview_item(entry: dict, tags_map: dict[int, list[int]]) -> dict:
    fields = display_fields(entry.get("title"), entry.get("content") or "")
    return {
        "id": entry["id"],
        "kind": entry["kind"],
        "title": entry.get("title"),
        **fields,
        "tag_ids": tags_map.get(entry["id"], []),
        "updated_at": entry.get("updated_at"),
    }


def search_entries(entries: list[dict], tags_map: dict[int, list[int]],
                   tag_by_id: dict[int, dict], query: str) -> list[dict]:
    """文字搜索（§5）：匹配标题和正文；标题命中优先、正文命中其次；
    一条记录只出现一次并展示所属标签；不改写任何持久顺序。"""
    needle = query.casefold().strip()
    if not needle:
        return []
    title_hits, content_hits = [], []
    for entry in entries:
        if entry.get("status") != "active":
            continue
        title = (entry.get("title") or "").casefold()
        content = entry.get("content") or ""
        if needle in title:
            title_hits.append(_search_item(entry, tags_map, tag_by_id, content, needle))
        elif needle in content.casefold():
            content_hits.append(_search_item(entry, tags_map, tag_by_id, content, needle))
    # 同级内按更新时间倒序；标题命中整体优先（§5）。
    key = lambda item: item["updated_at"] or ""
    return sorted(title_hits, key=key, reverse=True) + sorted(content_hits, key=key, reverse=True)


def _search_item(entry: dict, tags_map: dict[int, list[int]],
                 tag_by_id: dict[int, dict], content: str, needle: str) -> dict:
    fields = display_fields(entry.get("title"), content)
    return {
        "id": entry["id"],
        "kind": entry["kind"],
        "title": entry.get("title"),
        **fields,
        "match_fragment": _match_fragment(content, needle),
        "tags": [
            {"id": tag_id, "name": tag_by_id[tag_id]["name"]}
            for tag_id in tags_map.get(entry["id"], [])
            if tag_id in tag_by_id
        ],
        "updated_at": entry.get("updated_at"),
    }


def _match_fragment(content: str, needle: str) -> str:
    """命中正文片段（§5）：围绕首个命中点取窗口，换行折叠为空格。

    casefold 会改变字符数量（如 ß → ss），折叠文本里的命中下标不能直接
    用来切原文（F19）：长度一致时直接映射，否则建立「折叠下标 → 原文
    下标」映射，窗口按原文实际命中位置取。
    """
    lowered = content.casefold()
    index = lowered.find(needle)
    if index < 0:
        return _truncate(_first_line(content))
    if len(lowered) == len(content):
        match_start = index
        match_end = index + len(needle)
    else:
        mapping: list[int] = []
        for i, ch in enumerate(content):
            mapping.extend([i] * len(ch.casefold()))
        # 个别字符折叠后为空时映射会比折叠文本短，命中点按可用范围收敛
        match_start = mapping[index] if index < len(mapping) else len(content)
        end_fold = index + len(needle) - 1
        match_end = (mapping[end_fold] + 1) if end_fold < len(mapping) else len(content)
    start = max(0, match_start - SEARCH_CONTEXT_CHARS)
    end = min(len(content), match_end + SEARCH_CONTEXT_CHARS)
    fragment = " ".join(content[start:end].split())
    if start > 0:
        fragment = "…" + fragment
    if end < len(content):
        fragment = fragment + "…"
    return fragment


# ── 读取端点 ──────────────────────────────────────────────────────

def get_board(purge: bool = True) -> dict:
    client = _require_client()
    if purge:
        purge_expired_trash(client)   # 到期回收站清扫（M19，惰性）
    tags = _rows(client, "memo_tag", order_by=("position", "id"))
    groups = _rows(client, "memo_group")
    entries = _rows(client, "memo_entry", lambda q: q.eq("status", "active"))
    entry_tags = _rows(client, "memo_entry_tag")
    positions = _rows(client, "memo_position")
    return build_board(tags, groups, entries, entry_tags, positions)


def get_entry(entry_id: int, purge: bool = True) -> dict:
    client = _require_client()
    if purge:
        # 到期回收站清扫（M19，惰性）：过期条目按不存在处理
        purge_expired_trash(client)
    rows = _rows(client, "memo_entry", lambda q: q.eq("id", entry_id).limit(1),
                 paginate=False)
    if not rows:
        raise MemoError("内容不存在或已被删除", 404, "not_found")
    entry = rows[0]
    tag_ids = [
        row["tag_id"]
        for row in _rows(client, "memo_entry_tag", lambda q: q.eq("entry_id", entry_id))
    ]
    tags = _rows(client, "memo_tag", lambda q: q.in_("id", sorted(tag_ids))) if tag_ids else []
    tags.sort(key=lambda t: t["id"])
    payload = _serialize_entry(entry)
    payload["tags"] = [{"id": t["id"], "name": t["name"]} for t in tags]
    return payload


def list_entries(status: str = "active", query: str | None = None,
                 purge: bool = True) -> list[dict]:
    """列表视图：归档 / 回收站（按状态时间倒序）与文字搜索（§5/§6.2）。"""
    if status not in STATUSES:
        raise MemoError(f"未知状态：{status}", 400, "invalid_payload")
    client = _require_client()
    if purge:
        purge_expired_trash(client)   # 回收站到期清扫（M19）：先清后读
    entries = _rows(client, "memo_entry", lambda q: q.eq("status", status))
    entry_tags = _rows(client, "memo_entry_tag")
    tags = _rows(client, "memo_tag")
    tags_map = _entry_tags_map(entry_tags)
    tag_by_id = {t["id"]: t for t in tags}

    if status == "deleted":
        time_key = "deleted_at"
    elif status == "archived":
        time_key = "archived_at"
    else:
        time_key = "updated_at"

    if query is not None and query.strip():
        if status != "active":
            raise MemoError("搜索只在正常内容中进行", 400, "invalid_payload")
        return search_entries(entries, tags_map, tag_by_id, query)

    result = []
    for entry in entries:
        fields = display_fields(entry.get("title"), entry.get("content") or "")
        item = {
            "id": entry["id"],
            "kind": entry["kind"],
            "status": entry["status"],
            "title": entry.get("title"),
            **fields,
            "tags": [
                {"id": tag_id, "name": tag_by_id[tag_id]["name"]}
                for tag_id in tags_map.get(entry["id"], [])
                if tag_id in tag_by_id
            ],
            "created_at": entry.get("created_at"),
            "updated_at": entry.get("updated_at"),
            "archived_at": entry.get("archived_at"),
            "deleted_at": entry.get("deleted_at"),
        }
        result.append(item)
    result.sort(key=lambda item: (item.get(time_key) or "", item["id"]), reverse=True)
    return result


def list_tags() -> list[dict]:
    client = _require_client()
    tags = _rows(client, "memo_tag", order_by=("position", "id"))
    return [{"id": t["id"], "name": t["name"], "position": t.get("position", 0)}
            for t in tags]


def _serialize_entry(entry: dict) -> dict:
    return {
        "id": entry["id"],
        "title": entry.get("title"),
        "content": entry.get("content"),
        "kind": entry["kind"],
        "status": entry["status"],
        "content_version": entry["content_version"],
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
        "archived_at": entry.get("archived_at"),
        "deleted_at": entry.get("deleted_at"),
        "tags": entry.get("tags") or [],
    }


# ── 写入端点（全部经原子 RPC） ────────────────────────────────────

def create_entry(payload: dict) -> dict:
    kind = payload.get("kind")
    if kind not in KINDS:
        raise MemoError("用途必须是常驻备忘或随笔", 400, "invalid_payload")
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise MemoError("正文必填", 400, "invalid_payload")
    title = payload.get("title")
    if title is not None and (not isinstance(title, str) or len(title.strip()) > MAX_TITLE_LENGTH):
        raise MemoError(f"标题不能超过 {MAX_TITLE_LENGTH} 个字符", 400, "invalid_payload")
    if isinstance(title, str):
        title = title.strip() or None   # 空标题归一为 NULL（§2.3 未填标题）
    tag_ids = payload.get("tag_ids", [])
    if not isinstance(tag_ids, list) or not all(isinstance(t, int) for t in tag_ids):
        raise MemoError("tag_ids 必须是整数数组", 400, "invalid_payload")
    client_request_id = payload.get("client_request_id")
    if client_request_id is not None and (
        not isinstance(client_request_id, str) or not client_request_id.strip()
    ):
        raise MemoError("client_request_id 不合法", 400, "invalid_payload")

    client = _require_client()
    data = _call_rpc(client, "memo_create_entry", {
        "p_payload": {
            "kind": kind,
            "title": title,
            "content": content,
            "tag_ids": tag_ids,
            "client_request_id": client_request_id,
        },
        "p_now": _iso(_now()),
    })
    return _serialize_entry(data or {})


def update_entry(entry_id: int, payload: dict) -> dict:
    expected_version = payload.get("expected_version")
    if not isinstance(expected_version, int) or expected_version < 1:
        raise MemoError("expected_version 必须是正整数", 400, "invalid_payload")
    patch: dict[str, Any] = {}
    if "title" in payload:
        title = payload["title"]
        if title is not None and not isinstance(title, str):
            raise MemoError("标题必须是字符串或空", 400, "invalid_payload")
        patch["title"] = title
    if "content" in payload:
        content = payload["content"]
        if not isinstance(content, str) or not content.strip():
            raise MemoError("正文必填，保存失败不清空已有内容", 400, "invalid_payload")
        patch["content"] = content
    if "kind" in payload:
        if payload["kind"] not in KINDS:
            raise MemoError("用途必须是常驻备忘或随笔", 400, "invalid_payload")
        patch["kind"] = payload["kind"]
    if "tag_ids" in payload:
        tag_ids = payload["tag_ids"]
        if not isinstance(tag_ids, list) or not all(isinstance(t, int) for t in tag_ids):
            raise MemoError("tag_ids 必须是整数数组", 400, "invalid_payload")
        patch["tag_ids"] = tag_ids
    if not patch:
        raise MemoError("没有需要保存的修改", 400, "invalid_payload")

    client = _require_client()
    data = _call_rpc(client, "memo_update_entry", {
        "p_entry_id": entry_id,
        "p_expected_version": expected_version,
        "p_patch": patch,
        "p_now": _iso(_now()),
    })
    return _serialize_entry(data or {})


def set_entry_lifecycle(entry_id: int, action: str, expected_version) -> dict:
    if action not in ("archive", "delete", "restore"):
        raise MemoError("未知操作", 400, "invalid_payload")
    if not isinstance(expected_version, int) or expected_version < 1:
        raise MemoError("expected_version 必须是正整数", 400, "invalid_payload")
    client = _require_client()
    purge_expired_trash(client)   # 恢复前先清扫（M19）：过期条目由 RPC 保留期门兜底拒绝
    data = _call_rpc(client, "memo_set_entry_lifecycle", {
        "p_entry_id": entry_id,
        "p_action": action,
        "p_expected_version": expected_version,
        "p_now": _iso(_now()),
    })
    return _serialize_entry(data or {})


def reorder(payload: dict) -> dict:
    scope_kind = payload.get("scope")
    order = payload.get("order")
    if not isinstance(order, list) or not all(isinstance(i, int) for i in order):
        raise MemoError("order 必须是整数数组", 400, "invalid_payload")
    client = _require_client()
    now = _iso(_now())
    if scope_kind == "tags":
        return _call_rpc(client, "memo_reorder_tags", {
            "p_ordered_tag_ids": order, "p_now": now,
        })
    if scope_kind == "group":
        tag_id = payload.get("tag_id")
        section = payload.get("section")
        if section not in ("pinned", "note"):
            raise MemoError("section 必须是 pinned 或 note", 400, "invalid_payload")
        scope: dict[str, Any]
        if tag_id is None:
            scope = {"untagged": True}
        elif isinstance(tag_id, int):
            scope = {"tag_id": tag_id}
        else:
            raise MemoError("tag_id 不合法", 400, "invalid_payload")
        return _call_rpc(client, "memo_reorder_group", {
            "p_scope": scope, "p_section": section,
            "p_ordered_entry_ids": order, "p_now": now,
        })
    raise MemoError("scope 必须是 tags 或 group", 400, "invalid_payload")


def set_note_mode(payload: dict) -> dict:
    mode = payload.get("mode")
    if mode not in ("latest", "manual"):
        raise MemoError("mode 必须是 latest 或 manual", 400, "invalid_payload")
    tag_id = payload.get("tag_id")
    scope: dict[str, Any]
    if tag_id is None:
        scope = {"untagged": True}
    elif isinstance(tag_id, int):
        scope = {"tag_id": tag_id}
    else:
        raise MemoError("tag_id 不合法", 400, "invalid_payload")
    client = _require_client()
    return _call_rpc(client, "memo_set_note_mode", {
        "p_scope": scope, "p_mode": mode, "p_now": _iso(_now()),
    })


def create_tag(payload: dict) -> dict:
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise MemoError("标签名称不能为空", 400, "invalid_payload")
    if len(name.strip()) > MAX_TAG_NAME_LENGTH:
        raise MemoError(f"标签名称不能超过 {MAX_TAG_NAME_LENGTH} 个字符", 400, "invalid_payload")
    client = _require_client()
    return _call_rpc(client, "memo_create_tag", {
        "p_name": name, "p_now": _iso(_now()),
    })


def delete_tag(tag_id: int) -> dict:
    client = _require_client()
    return _call_rpc(client, "memo_delete_tag", {
        "p_tag_id": tag_id, "p_now": _iso(_now()),
    })
