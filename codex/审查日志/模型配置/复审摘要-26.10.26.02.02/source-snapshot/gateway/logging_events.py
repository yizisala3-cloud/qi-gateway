"""全网关报错日志采集（§36 独立批次）。

流水线：现有 logger / 结构化 emit → 脱敏 → 有界队列 → 后台批量 writer →
Supabase ``gateway_error_events`` 表；查询走 :mod:`gateway.log_api`。

设计约束（执行文档 §6）：

* handler 的 ``emit`` 不做任何网络 I/O，只做格式化、脱敏与入队；
* 主请求绝不等待日志写入，writer 异常不覆盖业务异常；
* writer 自身诊断走独立 logger（``gateway.log_internal``），不经过采集
  handler，避免「记录日志失败 → 产生日志 → 再次记录失败」递归；
* handler 安装 / 卸载可重入，重复初始化不重复采集；
* 队列、重试、spool 均有界，丢弃计数真实可见。
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
import traceback
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from urllib.parse import urlparse

from .config import cfg

# ── 级别与模块映射 ───────────────────────────────────────────────

_LEVEL_RANK = {"WARNING": 0, "ERROR": 1, "CRITICAL": 2}
_LEVEL_NAMES = ("WARNING", "ERROR", "CRITICAL")

_MODULE_ZH = {
    "qqbot": "QQBot",
    "planning": "规划管理",
    "memo": "备忘录",
    "memory": "记忆",
    "context": "上下文",
    "chat": "聊天转发",
    "database": "数据库",
    "system": "系统",
    "todo": "待办插件",
    "eventide": "身体状态",
    "persona": "人设",
    "other": "其他组件",
}

_LEVEL_ZH = {"WARNING": "警告", "ERROR": "错误", "CRITICAL": "严重错误"}

# logger 名 → 模块。前缀匹配，最长前缀优先；未命中 → other（保留 logger 名）。
_LOGGER_MODULE_RULES: tuple[tuple[str, str], ...] = tuple(
    sorted(
        (
            ("gateway.qqbot", "qqbot"),
            ("gateway.planning", "planning"),
            ("gateway.memo", "memo"),
            ("gateway.memory", "memory"),
            ("gateway.admin_memory", "memory"),
            ("gateway.context", "context"),
            ("gateway.chat", "chat"),
            ("gateway.upstream_compat", "chat"),
            ("gateway.db", "database"),
            ("gateway.admin_api", "database"),
            ("supabase", "database"),
            ("gateway.todo", "todo"),
            ("gateway.todos", "todo"),
            ("gateway.eventide", "eventide"),
            ("gateway.persona", "persona"),
            ("gateway.app_settings", "system"),
            ("gateway", "system"),
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)

# 永不采集的 logger 前缀：writer / 采集层自身诊断（防递归）、uvicorn 与
# asyncio（ASGI 未处理异常由本模块中间件按请求关联记录，避免同一异常
# 双重计数；平台标准输出仍保留这些记录）。
_EXCLUDED_LOGGER_PREFIXES = (
    "gateway.log_store",
    "gateway.log_internal",
    "gateway.logging_events",
    "uvicorn",
    "asyncio",
)

# 模块 → emit_error 使用的 logger 名（结构化入口与既有 logger 命名一致）。
_MODULE_LOGGER = {
    "qqbot": "gateway.qqbot",
    "planning": "gateway.planning",
    "memo": "gateway.memo",
    "memory": "gateway.memory",
    "context": "gateway.context",
    "chat": "gateway.chat",
    "database": "gateway.db",
    "system": "gateway",
    "todo": "gateway.todo",
    "eventide": "gateway.eventide",
    "persona": "gateway.persona",
}

# 路由模板 → 模块（HTTP 边界兜底事件用）。
_ROUTE_MODULE_RULES: tuple[tuple[str, str], ...] = (
    ("/admin/api/qqbot", "qqbot"),
    ("/admin/api/planning", "planning"),
    ("/admin/api/memo", "memo"),
    ("/admin/api/memory", "memory"),
    ("/admin/api/data", "database"),
    ("/admin/api/context", "context"),
    ("/admin/api/eventide", "eventide"),
    ("/admin/api/todo", "todo"),
    ("/admin/api/todos", "todo"),
    ("/v1/chat", "chat"),
)

# 已知（模块, 错误码）→ 中文摘要。泛型兜底在 _fallback_message_zh。
_CODE_MESSAGE_ZH = {
    ("chat", "upstream_timeout"): "聊天转发请求超时",
    ("chat", "upstream_connect_error"): "聊天上游连接失败",
    ("chat", "upstream_status_error"): "聊天上游返回错误状态",
    ("chat", "stream_timeout"): "聊天流式读取超时",
    ("chat", "stream_interrupted"): "聊天流式输出中断",
    ("chat", "chat_persist_skipped"): "聊天回复已返回，但原文保存被跳过",
    ("chat", "chat_persist_failed"): "聊天回复已返回，但原文保存失败",
    ("database", "db_query_failed"): "数据库查询失败",
    ("database", "db_write_failed"): "数据库写入失败",
    ("database", "db_unavailable"): "数据库客户端不可用",
    ("planning", "quiet_generate_failed"): "待办已保存，但同步排程失败，将由维护流程重试",
    ("planning", "quiet_recompute_failed"): "待办已保存，但同步重算失败，将由维护流程重试",
    ("system", "background_task_failed"): "后台任务出现未处理异常",
    ("system", "unhandled_exception"): "接口发生未处理异常",
    ("system", "http_5xx_boundary"): "接口返回服务器错误（未记录到具体错误日志）",
}

# safe_context 白名单：只收非敏感诊断变量，未知键一律拒绝。
_SAFE_CONTEXT_KEYS = frozenset(
    {
        "attempts", "stage", "data_source", "duration_ms", "exists", "count",
        "status_code", "upstream_status", "http_status", "task_id", "run_id",
        "batch", "retry", "timeout_seconds", "message_count", "reason_code",
        "job", "phase", "queue_depth", "dropped",
    }
)

# ── 字段长度上限（截断是容量控制；脱敏在截断之前完成） ──────────

_LIMITS = {
    "message_raw": 4000,
    "stack_trace": 16000,
    "message_zh": 400,
    "operation": 80,
    "error_code": 60,
    "route": 200,
    "method": 10,
    "logger_name": 120,
    "source": 200,
    "request_id": 64,
    "operation_id": 128,
    "instance_id": 64,
    "release_id": 64,
    "fingerprint": 32,
    "safe_context_value": 200,
    "key_ref": 40,
    "credential_role": 40,
    "provider": 80,
}

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{8,}=*"),
        "Bearer ***",
    ),
    (re.compile(r"sb_secret_[A-Za-z0-9]{8,}"), "sb_secret_***"),
    (
        re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),
        "***JWT***",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"), "sk-***"),
    # URL userinfo 凭证（postgres://user:pass@…、https://key@…）
    (
        re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^/?#\s:@]+:)([^@/\s]+)@"),
        r"\1***@",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|secret|password|passwd|"
            r"authorization|cookie|set-cookie|access[_-]?token|"
            r"refresh[_-]?token)\b['\"]?\s*[:=]\s*['\"]?"
            r"(?![Bb]earer\b)[^\s'\"]{6,}"
        ),
        r"\1=***",
    ),
    (
        re.compile(
            r"(?i)([?&](api[_-]?key|apikey|key|token|password|secret|"
            r"sig|signature|access[_-]?token|refresh[_-]?token)=)[^&\s]+"
        ),
        r"\1***",
    ),
)

# 数据库错误正文中的行值 / 键值回显：按完整错误结构移除自由正文（BUG-02）。
# 截断只是容量控制，这些替换必须在截断之前完成。值列表可跨行（存储
# 内容含换行时 PG 会多行回显）；message 主字段的非法输入回显（22P02
# 等）同样按模板剥离输入值，只保留类型与错误语义。
#
# BUG-02 残留修复：值内含正常括号 / 引号时，最短匹配会把值内部分隔符
# 当成模板终点，其后的正文原样保留。括号形态改为配对扫描（见
# _scrub_paren_echoes），引号形态改为「到模板边界内的最后一个引号」的
# 贪婪匹配——值可跨行、可内嵌引号，但不越过 traceback 帧 / 新异常段
# 边界（调用帧必须保留，不能整体删掉）。
_FRAME_BOUNDARY = r'\n  File "|\nTraceback |\nDuring handling of|\nThe above exception was'
# 受限贪婪体：任意字符（含换行），但不越过帧行 / 新 traceback 段的
# 起点——值可跨行、可内嵌引号，调用帧必须保留。
_VALUE_BODY = r'(?:(?!' + _FRAME_BOUNDARY + r')[\s\S])*'

_BODY_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)(invalid input syntax for\s+type\s+[a-z_][a-z0-9_ ]*:\s*)\""
            + _VALUE_BODY + r"\""
        ),
        '\\1"***"',
    ),
    (
        re.compile(
            r"(?i)(invalid input syntax for\s+type\s+[a-z_][a-z0-9_ ]*:\s*)'"
            + _VALUE_BODY + r"'"
        ),
        "\\1'***'",
    ),
    (
        re.compile(r'(?i)(malformed array literal:\s*)"' + _VALUE_BODY + r'"'),
        '\\1"***"',
    ),
    (
        re.compile(r"(?i)(malformed array literal:\s*)'" + _VALUE_BODY + r"'"),
        "\\1'***'",
    ),
)

# 括号形态的模板起点：Failing row contains (…) / Key (cols)=(…)。
# 列名部分不含括号（标识符列表），避免贪婪跨组错配。
_ROW_ECHO_START = re.compile(r"(?i)\bfailing row contains\s*\(")
_KEY_ECHO_START = re.compile(r"(?i)\b(key\s+\([\w\s,.'\"]*\)\s*=\s*)\(")
_FRAME_BOUNDARY_RE = re.compile(_FRAME_BOUNDARY)


def _echo_scan_region(text: str, open_pos: int) -> int:
    """括号回显的扫描上界：帧 / 新 traceback 段边界（保护调用帧）。"""
    match = _FRAME_BOUNDARY_RE.search(text, open_pos)
    return match.start() if match else len(text)


def _echo_close_paren(text: str, open_pos: int) -> int | None:
    """按完整结构找模板值的右括号（BUG-02）。

    配对扫描嵌套括号；深度归零的右括号只有后随句子终点（`.` / 换行 /
    分号 / 文本末）才算模板终点——值内多余右括号会继续向后配对。扫描
    区域限制在帧边界内；区域内找不到任何右括号时返回 None（调用方按
    fail-closed 取到区域末尾，正文不保留）。
    """
    region_end = _echo_scan_region(text, open_pos)
    depth = 0
    best_close = -1
    last_close = -1
    index = open_pos
    while index < region_end:
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            last_close = index
            if depth <= 0 and _echo_terminated(text, index + 1, region_end):
                best_close = index
        index += 1
    if best_close >= 0:
        return best_close
    return last_close if last_close >= 0 else None


def _echo_terminated(text: str, pos: int, region_end: int) -> bool:
    """右括号后（跳过空白）是否是句子终点：`.` / 换行 / 分号 / 区域末。"""
    index = pos
    while index < region_end and text[index] in " \t":
        index += 1
    if index >= region_end:
        return True
    return text[index] in ".\n;"


def _scrub_paren_echoes(text: str, start_re: re.Pattern[str]) -> str:
    """移除 ``Failing row contains (…)`` / ``Key (…)=(…)`` 的自由值。

    模板前缀（含列名）保留，值部分整体替换为 ``(***)``；同一文本中的
    多处回显逐一处理。配对失败时按 fail-closed 取到帧边界，不依赖
    「第一个右括号」当模板终点（BUG-02）。
    """
    result: list[str] = []
    pos = 0
    while True:
        match = start_re.search(text, pos)
        if match is None:
            result.append(text[pos:])
            return "".join(result)
        # match.end() 位于开括号之后：前缀含开括号，值替换为 ***
        result.append(text[pos:match.end()])
        result.append("***)")
        close = _echo_close_paren(text, match.end() - 1)
        if close is None:
            pos = _echo_scan_region(text, match.end())
        else:
            pos = close + 1

# 服务端已配置密钥的实际值：掩码不依赖格式正则命中（BUG-01）。
# GATEWAY_KEY_REF_SECRET 只用于 key_ref 计算，同样必须被掩码（TASK-02：
# 永不进入日志、平台输出或构建产物）。
_CONFIGURED_SECRET_ATTRS = (
    "GATEWAY_TOKEN",
    "UPSTREAM_API_KEY",
    "SUPABASE_SECRET_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_KEY",
    "ANALYSIS_API_KEY",
    "CONTINUITY_API_KEY",
    "RUMINATION_API_KEY",
    "MEMORY_PLUGIN_TOKEN",
    "MCP_MEMORY_TOKEN",
    "TODO_PLUGIN_TOKEN",
    "GATEWAY_KEY_REF_SECRET",
)


_runtime_secret_lock = threading.Lock()
_runtime_secret_values: set[str] = set()


def register_runtime_secrets(values) -> None:
    """Remember accepted provider secrets for this process, including retired keys.

    Called by the configuration read/write boundary, never by the log handler.
    Keeping old values protects in-flight responses and delayed exception chains
    after a provider edit. No configuration I/O or logging occurs here.
    """
    with _runtime_secret_lock:
        _runtime_secret_values.update(value for value in values if isinstance(value, str) and value)


def _configured_secret_values() -> tuple[str, ...]:
    """收集当前配置里的密钥实际值（每次现读，兼容测试对 cfg 打补丁）。

    BUG-01：所有被系统接受的非空配置值都掩码——长度豁免会留下「鉴权接受
    却不清洗」的窗口，短凭证同样能进入事件 / spool / 平台输出。
    """
    with _runtime_secret_lock:
        values = set(_runtime_secret_values)
    for name in _CONFIGURED_SECRET_ATTRS:
        value = getattr(cfg, name, "")
        if isinstance(value, str) and value:
            values.add(value)
    return tuple(sorted(values, key=len, reverse=True))

# ANSI 转义序列先处理（ESC 本身也在控制字符范围内，先删控制字符会把
# 转义序列拆成残留的 [31m 之类文本）；再清其余控制字符（保留 \n \t）。
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _scrub_text_core(text: str) -> str:
    """脱敏（格式正则 + 配置密钥实际值 + 行值回显）+ 去控制字符；不截断。

    持久化事件、平台输出与错误响应出口共用同一清洗规则（BUG-01/02/26）。
    """
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    text = _scrub_paren_echoes(text, _ROW_ECHO_START)
    text = _scrub_paren_echoes(text, _KEY_ECHO_START)
    for pattern, replacement in _BODY_VALUE_PATTERNS:
        text = pattern.sub(replacement, text)
    for secret in _configured_secret_values():
        if secret in text:
            text = text.replace(secret, "***")
    text = _ANSI_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def scrub_text(text: Any, limit: int) -> tuple[str, bool]:
    """脱敏 + 去控制字符 + 截断；返回 (清洗后文本, 是否截断)。"""
    if text is None:
        return "", False
    cleaned = text if isinstance(text, str) else str(text)
    cleaned = _scrub_text_core(cleaned)
    if len(cleaned) > limit:
        return cleaned[:limit], True
    return cleaned, False


def scrub_error_text(text: Any, limit: int = 2000) -> str:
    """业务失败记录 / 错误响应出口共用的敏感清洗（BUG-26/27）。

    与持久化事件同一规则：先清洗后截断；上游错误正文、SDK 异常文本、
    模型输出片段在入库或返回客户端之前必须经过这里。
    """
    cleaned, _ = scrub_text(text, limit)
    return cleaned


# 异常链递归脱敏的深度上限（BUG-21）：循环链 / 超深包装链到此截断。
_CHAIN_DEPTH_LIMIT = 8


def _safe_placeholder(exc: BaseException, note: str) -> BaseException:
    """深度 / 循环 / 构造失败时的安全占位（BUG-21）。

    保留原异常类型名（文本内可见），不携带任何原文本——绝不能把未
    检查层级的原始异常对象交回 traceback 展开器。
    """
    return RuntimeError(f"<{type(exc).__name__}: {note}>")


def _clone_exception_with_text(
    exc: BaseException, text: str, seen: frozenset[int],
) -> BaseException:
    """构造保留类型名、属性与异常链、但文本已清洗的异常副本。

    ``copy.copy`` 会经 ``__init__`` 重建，自定义签名的异常（如 postgrest
    APIError 接收 dict）会失败；这里用 ``__new__`` 绕过初始化。原异常
    对象绝不修改（业务侧的 ``code`` / ``sqlstate`` 等属性不受影响）。

    BUG-21：``__cause__`` / ``__context__`` / ``__suppress_context__`` 不在
    ``__dict__`` 里，必须显式递归复制（每层各自脱敏），否则包装异常下的
    底层根因与调用帧会在副本上整体消失。构造失败 fail-closed：返回安全
    占位，不返回原对象。
    """
    try:
        clone = exc.__class__.__new__(exc.__class__)
        clone.args = (text,)
        try:
            clone.__dict__.update(getattr(exc, "__dict__", {}))
        except Exception:
            pass
        try:
            # 链上层级的调用帧：traceback 模块按各层 __traceback__ 展开源码
            # 行（帧文本随后仍经整段文本清洗兜底）。
            clone.__traceback__ = exc.__traceback__
        except Exception:
            pass
        try:
            clone.__suppress_context__ = bool(exc.__suppress_context__)
        except Exception:
            pass
        for attr in ("__cause__", "__context__"):
            linked = getattr(exc, attr, None)
            if linked is not None:
                setattr(clone, attr, _scrubbed_exception(linked, seen))
        return clone
    except Exception:
        return _safe_placeholder(exc, "exception clone failed")


def _clone_exception_group(
    exc: BaseException, text: str, children: tuple[BaseException, ...],
    seen: frozenset[int],
) -> BaseException:
    """ExceptionGroup 副本：子异常逐个脱敏后重建（BUG-21）。

    顶层 str 只含组消息、不含子异常文本——原对象直接返回会让
    traceback 展开器输出未清洗的子异常。优先保留原类（自定义子类构造
    失败时退回基类），全部失败返回安全占位。
    """
    constructors: list[type] = [type(exc)]
    if all(isinstance(child, Exception) for child in children):
        constructors.append(ExceptionGroup)
    constructors.append(BaseExceptionGroup)
    for ctor in constructors:
        try:
            clone = ctor(text, children)
        except Exception:
            continue
        try:
            clone.__traceback__ = exc.__traceback__
        except Exception:
            pass
        try:
            clone.__dict__.update(getattr(exc, "__dict__", {}))
        except Exception:
            pass
        try:
            clone.__suppress_context__ = bool(exc.__suppress_context__)
        except Exception:
            pass
        for attr in ("__cause__", "__context__"):
            linked = getattr(exc, attr, None)
            if linked is not None:
                setattr(clone, attr, _scrubbed_exception(linked, seen))
        return clone
    return _safe_placeholder(exc, "exception group clone failed")


def _scrubbed_exception(
    exc: BaseException, seen: frozenset[int] = frozenset(),
) -> BaseException:
    """返回用于格式化 / 平台输出的脱敏异常副本；原异常对象绝不修改。

    完整异常树递归脱敏（BUG-21）：``__cause__`` / ``__context__`` 链逐层
    克隆；``BaseExceptionGroup`` 的子异常逐个脱敏后重建（traceback 展开
    器会输出全部子异常）。循环链（同一异常在链上重现）与超深链（≥
    ``_CHAIN_DEPTH_LIMIT``）用安全占位截断——绝不返回原对象。结构化
    SDK 错误（APIError 等 code + message/details 形态）只保留错误码与
    消息模板行；其余异常按 scrub 规则清洗 str 文本。本层与全链都无需
    清洗时返回原对象（不制造副本）。
    """
    if id(exc) in seen:
        # 循环链：链上更高层已处理过该异常，此处重复出现必须用占位截断，
        # 返回原对象会把未清洗文本交回展开器（BUG-21）。
        return _safe_placeholder(exc, "repeated in exception chain")
    if len(seen) >= _CHAIN_DEPTH_LIMIT:
        return _safe_placeholder(exc, "chain depth limit reached")
    linked_seen = seen | {id(exc)}
    try:
        if isinstance(exc, BaseExceptionGroup):
            children = tuple(
                _scrubbed_exception(child, linked_seen)
                for child in getattr(exc, "exceptions", ())
            )
            text = _scrub_text_core(str(exc))
            return _clone_exception_group(exc, text, children, linked_seen)
        code = getattr(exc, "code", None)
        structured = (
            isinstance(code, str) and code
            and (hasattr(exc, "details") or hasattr(exc, "message"))
        )
        text = str(exc)
        cleaned = _scrub_text_core(text)
        has_chain = exc.__cause__ is not None or exc.__context__ is not None
        if not structured and cleaned == text and not has_chain:
            return exc
        if structured:
            message = getattr(exc, "message", None)
            message_text = (
                _scrub_text_core(message) if isinstance(message, str) else "")
            clone_text = f"Error {code}: {message_text}".strip()
        else:
            clone_text = cleaned
        return _clone_exception_with_text(exc, clone_text, linked_seen)
    except Exception:
        return _safe_placeholder(exc, "exception scrub failed")


def _scrub_scalar(value: Any) -> Any:
    """safe_context 值清洗：标量保留（字符串脱敏限长），其余拒绝。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, str):
        cleaned, _ = scrub_text(value, _LIMITS["safe_context_value"])
        return cleaned
    return None


def _sanitize_safe_context(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or key not in _SAFE_CONTEXT_KEYS:
            continue  # 未知键默认拒绝（§5.1 白名单 context）
        cleaned = _scrub_scalar(value)
        if cleaned is not None:
            result[key] = cleaned
    return result


def _module_for_logger(name: str) -> str:
    # 规则已按前缀长度降序：先命中最长前缀（gateway.planning_api 归
    # planning 而不是落到 gateway 根前缀的 system）。
    for prefix, module in _LOGGER_MODULE_RULES:
        if name.startswith(prefix):
            return module
    return "other"


def _module_for_route(route: str) -> str:
    for prefix, module in sorted(_ROUTE_MODULE_RULES, key=lambda r: len(r[0]), reverse=True):
        if route.startswith(prefix):
            return module
    return "system"


def _is_excluded_logger(name: str) -> bool:
    for prefix in _EXCLUDED_LOGGER_PREFIXES:
        if name.startswith(prefix):
            return True
    return False


def _fallback_message_zh(module: str, level: str, exception_type: str, logger_name: str) -> str:
    module_zh = _MODULE_ZH.get(module, module or "未知模块")
    level_zh = _LEVEL_ZH.get(level, level)
    if module == "other":
        base = f"其他组件出现{level_zh}（logger: {logger_name}）"
    else:
        base = f"{module_zh}出现{level_zh}"
    if exception_type:
        base = f"{base}（{exception_type}）"
    cleaned, _ = scrub_text(base, _LIMITS["message_zh"])
    return cleaned


def _fingerprint(
    module: str, operation: str, error_code: str,
    exception_type: str, logger_name: str, source: str,
    key_ref: str = "",
) -> str:
    # 归一化来源：文件名 + 函数名（不含行号，避免代码版本间碎片化）。
    normalized_source = source.rsplit(":", 1)[0] if source.count(":") >= 2 else source
    raw = "|".join((module, operation, error_code, exception_type, logger_name, normalized_source))
    # TASK-02：完整密钥身份参与分组——同功能不同密钥必须分组。仅在有
    # 标识时追加：无凭证 / 历史未知事件保持与旧公式相同的指纹，不与
    # 已识别密钥混组，也不把旧记录拆成新组。
    if key_ref:
        raw = f"{raw}|{key_ref}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _instance_id() -> str:
    configured = cfg.GATEWAY_INSTANCE_ID
    if configured:
        cleaned, _ = scrub_text(configured, _LIMITS["instance_id"])
        return cleaned
    try:
        host = socket.gethostname()
    except Exception:
        host = "unknown"
    cleaned, _ = scrub_text(host, _LIMITS["instance_id"])
    return cleaned or "unknown"


def _release_id() -> str:
    cleaned, _ = scrub_text(cfg.GATEWAY_RELEASE_ID, _LIMITS["release_id"])
    return cleaned


# ── 稳定密钥身份（TASK-02，§4.3） ─────────────────────────────────
# 独立服务端秘密 GATEWAY_KEY_REF_SECRET（建议 ≥32 字节安全随机值）只用于
# HMAC-SHA-256；不复用 GATEWAY_TOKEN、上游密钥或公开盐。秘密缺失 / 无效
# 时标识明确不可用——禁止用真实密钥、普通哈希或每次启动随机值兜底；
# 业务与安全日志继续运行。

_KEY_REF_PREFIX = "kh1-"
_KEY_REF_SECRET_MIN_BYTES = 32


def key_ref_ready() -> bool:
    """标识秘密是否有效就绪（配置页与采集状态共用）。"""
    return len(cfg.GATEWAY_KEY_REF_SECRET.strip()) >= _KEY_REF_SECRET_MIN_BYTES


def compute_key_ref(value: str) -> str:
    """实际密钥值的完整不可逆稳定标识（算法版本前缀 + HMAC-SHA-256）。

    同一有效值在同一稳定秘密下得到同一 ``key_ref``；用途与上游另列字段
    （credential_role / provider），不混入密钥身份——同一把密钥跨功能
    仍是同一身份。秘密缺失 / 无效返回空串（明确不可用，不兜底）。
    """
    secret = cfg.GATEWAY_KEY_REF_SECRET.strip()
    if not value or len(secret) < _KEY_REF_SECRET_MIN_BYTES:
        return ""
    digest = hmac.new(
        secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{_KEY_REF_PREFIX}{digest[:32]}"


def key_ref_label(ref: str) -> str:
    """页面短标识：``K-`` + 完整标识前 8 位；空标识返回空串。"""
    if ref.startswith(_KEY_REF_PREFIX) and len(ref) >= len(_KEY_REF_PREFIX) + 8:
        return f"K-{ref[len(_KEY_REF_PREFIX):len(_KEY_REF_PREFIX) + 8]}"
    return ""


def provider_label(base_url: str) -> str:
    """安全上游标识：URL 主机名（不含 userinfo / 路径 / 查询参数）。"""
    try:
        host = urlparse(base_url or "").hostname
    except ValueError:
        host = ""
    cleaned, _ = scrub_text(host or "", _LIMITS["provider"])
    return cleaned


class _CredentialContext:
    __slots__ = ("key_ref", "role", "provider")

    def __init__(self, key_ref: str, role: str, provider: str) -> None:
        self.key_ref = key_ref
        self.role = role
        self.provider = provider


_credential_ctx: contextvars.ContextVar[_CredentialContext | None] = contextvars.ContextVar(
    "gateway_credential_ctx", default=None
)


def _bind_credential(exc: BaseException, ctx: _CredentialContext) -> None:
    """把凭证身份标注到穿过调用边界的异常上（尽力而为）。

    异常捕获点（API 层 / 调度器）常在 ``to_thread`` / ``run_in_executor``
    的另一侧，contextvar 不可见；异常对象上的标注让这些日志事件仍能
    归属实际调用的密钥。只新增诊断属性，不影响异常身份（``id(exc)``
    去重契约）；带 ``__slots__`` 的异常设置失败时静默跳过。
    """
    try:
        if getattr(exc, "gateway_credential", None) is None:
            exc.gateway_credential = (ctx.key_ref, ctx.role, ctx.provider)  # type: ignore[attr-defined]
    except Exception:
        pass


@contextmanager
def credential_scope(role: str, provider: str, api_key: str) -> Iterator[None]:
    """在实际 HTTP/SDK 调用边界绑定凭证身份（TASK-02）。

    作用域内产生的事件携带 ``key_ref`` / ``credential_role`` / ``provider``，
    fingerprint 加入完整密钥身份（同功能不同密钥分组）；穿过边界的异常
    被标注，供边界外的捕获点归属。密钥原文绝不进入事件或日志。
    """
    ctx = _CredentialContext(compute_key_ref(api_key), role, provider)
    token = _credential_ctx.set(ctx)
    try:
        yield
    except BaseException as exc:
        _bind_credential(exc, ctx)
        raise
    finally:
        _credential_ctx.reset(token)


def _event_credential(record: logging.LogRecord) -> _CredentialContext | None:
    """事件归属：优先异常标注（跨线程传播），其次当前调用边界作用域。"""
    if record.exc_info and record.exc_info[1] is not None:
        bound = getattr(record.exc_info[1], "gateway_credential", None)
        if isinstance(bound, tuple) and len(bound) == 3:
            return _CredentialContext(str(bound[0]), str(bound[1]), str(bound[2]))
    return _credential_ctx.get()


# ── 请求关联上下文 ───────────────────────────────────────────────
# ContextVar 持有可变对象：asyncio.to_thread / create_task 复制上下文时，
# 线程内对同一对象的计数修改对中间件可见（ContextVar 本身的 set 不回传）。


class RequestLogContext:
    __slots__ = (
        "request_id", "method", "route", "http_status", "events_recorded",
        "error_events", "recorded_exc_ids", "boundary_suppressed",
    )

    def __init__(self, request_id: str, method: str, route: str) -> None:
        self.request_id = request_id
        self.method = method
        self.route = route
        self.http_status: int | None = None
        self.events_recorded = 0
        # BUG-03 仲裁：WARNING 不再抑制后续未处理异常 / 5xx 兜底——只有
        # ERROR+ 级别事件（或已按异常身份记录过）才算「本请求的终结性错误
        # 已被主记录者记录」。
        self.error_events = 0
        self.recorded_exc_ids: set[int] = set()
        # BUG-12：日志查询链自身失败时置位，本请求 5xx 不再触发边界兜底。
        self.boundary_suppressed = False


_request_ctx: contextvars.ContextVar[RequestLogContext | None] = contextvars.ContextVar(
    "gateway_request_log_ctx", default=None
)


def current_request_id() -> str:
    ctx = _request_ctx.get()
    return ctx.request_id if ctx else ""


def current_request_context() -> RequestLogContext | None:
    return _request_ctx.get()


def suppress_boundary_for_current_request() -> None:
    """日志查询链自身失败时调用：本请求 5xx 不再触发边界兜底（BUG-12）。

    查询失败已由限频的内部事件解释，边界兜底再记一条只会随查询次数放大。
    """
    ctx = _request_ctx.get()
    if ctx is not None:
        ctx.boundary_suppressed = True


# ── 有界队列（满时优先保留高等级） ───────────────────────────────


class _EventQueue:
    def __init__(self, maxlen: int) -> None:
        self._lock = threading.Lock()
        self._items: deque[dict[str, Any]] = deque()
        self._maxlen = max(1, maxlen)
        self.dropped_total = 0
        self.dropped_by_level: dict[str, int] = {"WARNING": 0, "ERROR": 0, "CRITICAL": 0}
        self.last_drop_at: str = ""
        self.last_drop_reason: str = ""

    def _count_drop(self, event: dict[str, Any], reason: str) -> None:
        self.dropped_total += 1
        level = event.get("level", "WARNING")
        self.dropped_by_level[level] = self.dropped_by_level.get(level, 0) + 1
        self.last_drop_at = _utc_now_iso()
        self.last_drop_reason = reason

    def _count_drop_batch(self, events: list[dict[str, Any]], reason: str) -> None:
        for event in events:
            self._count_drop(event, reason)

    def put(self, event: dict[str, Any]) -> bool:
        with self._lock:
            if len(self._items) < self._maxlen:
                self._items.append(event)
                return True
            incoming_rank = _LEVEL_RANK.get(event.get("level", "WARNING"), 0)
            for index, existing in enumerate(self._items):
                if _LEVEL_RANK.get(existing.get("level", "WARNING"), 0) < incoming_rank:
                    del self._items[index]
                    self._count_drop(existing, "queue_full_replaced")
                    self._items.append(event)
                    return True
            self._count_drop(event, "queue_full")
            return False

    def stamp_request_status(self, request_id: str, status: int) -> int:
        """响应状态回填：日志先于响应产生时，补记本请求事件的网关状态。

        尽力而为——已被 writer 取走的事件不再回填；未记录任何事件的
        5xx 由中间件边界兜底事件精确记录。
        """
        stamped = 0
        with self._lock:
            for event in self._items:
                if event.get("request_id") == request_id and event.get("http_status") is None:
                    event["http_status"] = status
                    stamped += 1
        return stamped

    def drain(self, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            batch = []
            while self._items and len(batch) < limit:
                batch.append(self._items.popleft())
            return batch

    def depth(self) -> int:
        with self._lock:
            return len(self._items)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "queue_depth": len(self._items),
                "queue_max": self._maxlen,
                "dropped_total": self.dropped_total,
                "dropped_by_level": dict(self.dropped_by_level),
                "dropped_last_at": self.last_drop_at,
                "dropped_last_reason": self.last_drop_reason,
            }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 事件构建 ─────────────────────────────────────────────────────


def _build_event_from_record(record: logging.LogRecord) -> dict[str, Any] | None:
    evt = record.__dict__.get("evt")
    evt = evt if isinstance(evt, dict) else {}

    level = record.levelname if record.levelname in _LEVEL_RANK else "WARNING"
    module_raw = evt.get("module")
    module = module_raw if module_raw in _MODULE_ZH else _module_for_logger(record.name)

    operation_raw = evt.get("operation") or record.funcName or "unknown"
    operation, _ = scrub_text(operation_raw, _LIMITS["operation"])

    error_code_raw = evt.get("error_code") or "unlogged_error"
    error_code, _ = scrub_text(error_code_raw, _LIMITS["error_code"])

    exception_type = ""
    stack_trace = ""
    if record.exc_info and record.exc_info[0] is not None:
        exception_type = record.exc_info[0].__name__[:_LIMITS["error_code"]]
        # 堆栈末行含 str(exc)：用脱敏副本格式化，行值回显不落库（BUG-02）。
        formatted_exc = _scrubbed_exception(record.exc_info[1])
        try:
            stack_trace = "".join(
                traceback.format_exception(
                    record.exc_info[0], formatted_exc, record.exc_info[2]
                )
            )
        except Exception:
            stack_trace = ""

    try:
        message = record.getMessage()
    except Exception:
        message = "<unformattable log message>"
    message_raw, raw_truncated = scrub_text(message, _LIMITS["message_raw"])
    stack_trace, stack_truncated = scrub_text(stack_trace, _LIMITS["stack_trace"])

    message_zh_raw = evt.get("message_zh") or _CODE_MESSAGE_ZH.get((module, error_code))
    if message_zh_raw:
        message_zh, _ = scrub_text(message_zh_raw, _LIMITS["message_zh"])
    else:
        message_zh = _fallback_message_zh(module, level, exception_type, record.name)

    logger_name, _ = scrub_text(record.name, _LIMITS["logger_name"])
    source_raw = f"{record.pathname}:{record.funcName}:{record.lineno}"
    source, _ = scrub_text(source_raw, _LIMITS["source"])

    ctx = _request_ctx.get()
    request_id = ""
    route = ""
    method = ""
    if ctx is not None:
        request_id = ctx.request_id[:_LIMITS["request_id"]]
        route = ctx.route[:_LIMITS["route"]]
        method = ctx.method[:_LIMITS["method"]]
        ctx.events_recorded += 1
        if level in ("ERROR", "CRITICAL"):
            ctx.error_events += 1
        if record.exc_info and record.exc_info[1] is not None:
            ctx.recorded_exc_ids.add(id(record.exc_info[1]))

    operation_id, _ = scrub_text(evt.get("operation_id") or "", _LIMITS["operation_id"])

    # TASK-02：实际调用密钥的稳定标识 / 用途 / 安全上游（无归属为空，
    # 历史事件同样为空——不按当前配置补猜）。
    credential = _event_credential(record)
    key_ref = ""
    credential_role = ""
    provider = ""
    if credential is not None:
        key_ref, _ = scrub_text(credential.key_ref, _LIMITS["key_ref"])
        credential_role, _ = scrub_text(credential.role, _LIMITS["credential_role"])
        provider, _ = scrub_text(credential.provider, _LIMITS["provider"])

    upstream_status = evt.get("upstream_status")
    http_status = evt.get("http_status")
    if not isinstance(upstream_status, int):
        upstream_status = None
    if not isinstance(http_status, int):
        http_status = ctx.http_status if ctx is not None else None

    outcome_raw = evt.get("outcome")
    outcome = outcome_raw if outcome_raw in ("failed", "degraded", "committed_side_effect_failed") else "failed"

    truncated: list[str] = []
    if raw_truncated:
        truncated.append("message_raw")
    if stack_truncated:
        truncated.append("stack_trace")

    return {
        "event_id": str(uuid.uuid4()),
        "occurred_at": _utc_now_iso(),
        "level": level,
        "module": module,
        "operation": operation,
        "error_code": error_code,
        "message_zh": message_zh,
        "exception_type": exception_type,
        "message_raw": message_raw,
        "stack_trace": stack_trace,
        "logger_name": logger_name,
        "source": source,
        "request_id": request_id,
        "operation_id": operation_id,
        "route": route,
        "method": method,
        "http_status": http_status,
        "upstream_status": upstream_status,
        "outcome": outcome,
        "instance_id": _instance_id(),
        "release_id": _release_id(),
        "safe_context": _sanitize_safe_context(evt.get("safe_context")),
        "key_ref": key_ref,
        "credential_role": credential_role,
        "provider": provider,
        "fingerprint": _fingerprint(
            module, operation, error_code, exception_type, logger_name, source, key_ref
        ),
        "truncated_fields": truncated,
    }


# ── 采集 handler ─────────────────────────────────────────────────

_capture_active = False
_handler: "GatewayLogHandler | None" = None
_scrub_filter: "_PlatformScrubFilter | None" = None

# BUG-11：writer 线程内抑制采集——写入 / 重试 / 清理链调用公共 db 时，
# gateway.db 的 warning/error 不再被重新采集（线程本地，业务线程不受影响）。
_capture_suppressed = threading.local()


@contextmanager
def suppress_capture() -> Iterator[None]:
    previous = getattr(_capture_suppressed, "active", False)
    _capture_suppressed.active = True
    try:
        yield
    finally:
        _capture_suppressed.active = previous


class _PlatformScrubFilter(logging.Filter):
    """平台输出与持久化使用同一安全格式（BUG-01/02）。

    挂在 root 既有 handler 上，在格式化前改写共享 record：掩码已知密钥
    实际值与行值回显、预格式化并清洗整段 traceback 缓存到 ``exc_text``。
    不丢弃任何记录。

    BUG-20：绝不替换 ``record.exc_info`` 里的异常对象——共享 LogRecord 的
    异常身份是采集 handler 与中间件按 ``id(exc)`` 去重的契约，替换成脱敏
    副本会让端点 ``log.exception`` 后 re-raise 的同一异常被记成两次。平台
    清洗只改 ``exc_text`` 缓存与消息文本：标准 Formatter 优先使用
    ``exc_text``，输出仍是脱敏后的内容；``exc_text`` 每次重算覆盖，避免
    先前无过滤 handler 缓存的原始帧文本漏出。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            cleaned = _scrub_text_core(message)
            if cleaned != message:
                record.msg = cleaned
                record.args = None
            exc_info = record.exc_info
            if exc_info and exc_info[1] is not None:
                scrubbed = _scrubbed_exception(exc_info[1])
                formatted = "".join(traceback.format_exception(
                    exc_info[0], scrubbed, exc_info[2]))
                record.exc_text = _scrub_text_core(formatted)
        except Exception:
            pass
        return True


class GatewayLogHandler(logging.Handler):
    """捕获 WARNING+ 日志记录为脱敏事件并入队；emit 内绝不做网络 I/O。"""

    def __init__(self, level: int = logging.WARNING) -> None:
        super().__init__(level=level)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not _capture_active:
                return
            if record.levelno < self.level:
                return
            if _is_excluded_logger(record.name):
                return
            if getattr(_capture_suppressed, "active", False):
                return
            event = _build_event_from_record(record)
            if event is not None:
                _queue.put(event)
        except Exception:
            # 采集自身失败绝不影响业务日志调用，也绝不在这里再打日志（递归）。
            pass


# 平台输出脱敏需要覆盖的命名 logger（BUG-25）：部署入口
# ``python -m uvicorn`` 的默认 LOGGING_CONFIG 给 uvicorn / uvicorn.access
# 配了独立 handler 且 ``propagate=False``，不经过 root——只挂 root 会漏
# 掉 ASGI 未处理异常的平台输出。采集排除 uvicorn（防双计）与平台清洗
# 互不排斥：过滤器只改写消息与 ``exc_text``，不丢弃记录。
_PLATFORM_LOGGER_NAMES = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _platform_scrub_targets() -> list[logging.Handler]:
    """需要挂平台脱敏过滤器的全部 handler：root 既有 + 命名平台 logger。"""
    handlers: list[logging.Handler] = []
    seen: set[int] = set()
    for existing in logging.getLogger().handlers:
        if isinstance(existing, GatewayLogHandler) or id(existing) in seen:
            continue
        handlers.append(existing)
        seen.add(id(existing))
    for name in _PLATFORM_LOGGER_NAMES:
        for existing in logging.getLogger(name).handlers:
            if id(existing) in seen:
                continue
            handlers.append(existing)
            seen.add(id(existing))
    return handlers


def install_handler() -> bool:
    """幂等安装采集 handler；返回是否为本次新装。"""
    global _handler, _capture_active, _scrub_filter
    if not cfg.GATEWAY_LOG_ENABLED:
        return False
    root = logging.getLogger()
    already_installed = False
    for existing in root.handlers:
        if isinstance(existing, GatewayLogHandler):
            _handler = existing
            already_installed = True
            break
    # 平台输出脱敏（幂等）：挂在所有非采集 handler 上（含 uvicorn 独立
    # handler，BUG-25）；卸载时移除，回滚后平台输出恢复原样。
    if _scrub_filter is None:
        _scrub_filter = _PlatformScrubFilter()
    for existing in _platform_scrub_targets():
        if _scrub_filter not in existing.filters:
            existing.addFilter(_scrub_filter)
    if already_installed:
        _capture_active = True
        return False
    min_level_name = cfg.GATEWAY_LOG_MIN_LEVEL if cfg.GATEWAY_LOG_MIN_LEVEL in _LEVEL_RANK else "WARNING"
    handler = GatewayLogHandler(level=_LEVEL_RANK.get(min_level_name, 0) * 10 + 30)
    # logging.WARNING=30, ERROR=40, CRITICAL=50 —— 由级别名换算标准数值。
    root.addHandler(handler)
    _handler = handler
    _capture_active = True
    return True


def uninstall_handler() -> None:
    """停止接收新事件并卸载 handler（正常关闭 / 回滚开关）。"""
    global _handler, _capture_active, _scrub_filter
    _capture_active = False
    handler, _handler = _handler, None
    scrub_filter, _scrub_filter = _scrub_filter, None
    if scrub_filter is not None:
        for existing in _platform_scrub_targets():
            try:
                existing.removeFilter(scrub_filter)
            except Exception:
                pass
    if handler is not None:
        logging.getLogger().removeHandler(handler)


def handler_installed() -> bool:
    return _handler is not None and _capture_active


# ── 结构化 emit 入口（补齐缺口的业务代码用） ─────────────────────


def emit_error(
    module: str,
    operation: str,
    error_code: str,
    message_zh: str,
    *,
    level: str = "ERROR",
    exc: BaseException | None = None,
    outcome: str = "failed",
    operation_id: str = "",
    upstream_status: int | None = None,
    http_status: int | None = None,
    safe_context: dict[str, Any] | None = None,
) -> None:
    """写一条会被采集 handler 捕获的结构化日志（同时保留平台输出）。

    业务代码补齐「返回错误却无日志」的路径时使用；既有普通 log 调用
    不需要改写，handler 会按 logger 名自动归类。
    """
    logger_name = _MODULE_LOGGER.get(module, "gateway")
    evt: dict[str, Any] = {
        "module": module,
        "operation": operation,
        "error_code": error_code,
        "message_zh": message_zh,
        "outcome": outcome,
    }
    if operation_id:
        evt["operation_id"] = operation_id
    if upstream_status is not None:
        evt["upstream_status"] = upstream_status
    if http_status is not None:
        evt["http_status"] = http_status
    if safe_context:
        evt["safe_context"] = safe_context
    logger = logging.getLogger(logger_name)
    level_name = level if level in _LEVEL_RANK else "ERROR"
    if exc is not None:
        logger.log(_LEVEL_RANK[level_name] * 10 + 30, message_zh, exc_info=exc, extra={"evt": evt})
    else:
        logger.log(_LEVEL_RANK[level_name] * 10 + 30, message_zh, extra={"evt": evt})


# ── HTTP 边界兜底（中间件调用） ──────────────────────────────────


def record_http_5xx_boundary(ctx: RequestLogContext) -> None:
    module = _module_for_route(ctx.route)
    emit_error(
        module, "http_boundary", "http_5xx_boundary",
        "接口返回服务器错误（未记录到具体错误日志）",
        level="ERROR", outcome="failed", http_status=ctx.http_status,
    )


def record_unhandled_exception(ctx: RequestLogContext, exc: BaseException) -> None:
    module = _module_for_route(ctx.route)
    emit_error(
        module, "http_unhandled", "unhandled_exception",
        "接口发生未处理异常",
        exc=exc, outcome="failed", http_status=500,
    )


def record_task_exception(task_name: str, exc: BaseException) -> None:
    emit_error(
        "system", "background_task", "background_task_failed",
        "后台任务出现未处理异常",
        exc=exc, outcome="failed",
        safe_context={"job": task_name[:80]},
    )


# ── 中间件 ───────────────────────────────────────────────────────


def _route_template(scope: dict[str, Any]) -> str:
    """路由模板（不含路径参数）；未匹配时回退原始路径（已脱敏限长）。"""
    route = scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path[:_LIMITS["route"]]
    raw_path = scope.get("path", "") or "/"
    template = _match_route_template(scope.get("app"), raw_path)
    cleaned, _ = scrub_text(template or raw_path, _LIMITS["route"])
    return cleaned or "/"


def _match_route_template(app: Any, path: str) -> str | None:
    """在应用的 Route 表中按正则匹配路径，返回模板（§4：不默认保留路径参数）。

    Starlette 0.49 不写 scope["route"]，需自行匹配；Mount 的子应用
    （静态文件 / MCP）没有路由表，回退原始路径。
    """
    try:
        from starlette.routing import Mount, Route

        routes = getattr(app, "routes", None)
        if not routes:
            return None
        for route in routes:
            if isinstance(route, Route):
                regex = getattr(route, "path_regex", None)
                if regex is not None and regex.match(path):
                    return route.path
            elif isinstance(route, Mount):
                prefix = route.path or ""
                if prefix and (path == prefix or path.startswith(prefix + "/")):
                    matched = _match_route_template(
                        route.app, path[len(prefix):] or "/")
                    if matched:
                        return prefix + matched
        return None
    except Exception:
        return None


class GatewayLogMiddleware:
    """请求关联 ID 生成、X-Request-ID 响应头与 HTTP 边界兜底记录。

    纯 ASGI 实现（不经 BaseHTTPMiddleware），对 SSE 流式响应零干预：
    流内错误由业务侧 emit_error 记录，HTTP 200 不会被误当完全成功——
    事件里 outcome / error_code 可辨别。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        request_id = secrets.token_hex(8)
        method = scope.get("method", "") or ""
        ctx = RequestLogContext(request_id, method, _route_template(scope))
        token = _request_ctx.set(ctx)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                ctx.http_status = message.get("status")
                headers = message.setdefault("headers", [])
                headers.append((b"x-request-id", request_id.encode("ascii")))
                status = message.get("status") or 0
                if status >= 400 and ctx.events_recorded:
                    # 日志先于响应产生：回填网关状态（尽力而为）
                    _queue.stamp_request_status(ctx.request_id, status)
                if (
                    status >= 500
                    and ctx.error_events == 0
                    and not ctx.boundary_suppressed
                    and _capture_active
                ):
                    # 主记录者仲裁（BUG-03）：WARNING 不抑制兜底——只有本请求
                    # 已记录过 ERROR+ 级别事件（或查询链显式置位）才不补边界
                    # 事件，同一故障只计一次。回填与兜底互不排斥。
                    record_http_5xx_boundary(ctx)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            # 未处理异常按异常身份去重（BUG-03）：端点已 log.exception 后重新
            # raise 的同一异常不再补记；warning 从不抑制根因记录。
            if _capture_active and id(exc) not in ctx.recorded_exc_ids:
                record_unhandled_exception(ctx, exc)
            raise
        finally:
            _request_ctx.reset(token)


# ── 后台 writer ──────────────────────────────────────────────────

_diag_logger = logging.getLogger("gateway.log_internal")
_diag_lock = threading.Lock()
_last_diag_at = 0.0


def _diag(message: str, *args) -> None:
    """writer / 采集层自身诊断：限频（60s 一次），不进采集链。"""
    global _last_diag_at
    with _diag_lock:
        now = time.monotonic()
        if now - _last_diag_at < 60.0:
            return
        _last_diag_at = now
    try:
        _diag_logger.warning(message, *args)
    except Exception:
        pass


class _SpoolStore:
    """本地 JSONL spool：先脱敏的事件按行落盘，恢复后按 event_id 幂等补写。

    仅在显式配置目录时启用（容器临时盘不承诺跨重建保留，状态接口如实
    报告 enabled 与计数）。文件名含实例 ID 与序号，多实例互不抢读。

    容量上限（文件数 / 总字节）覆盖 ``.jsonl`` 与隔离的 ``.jsonl.corrupt``
    两种文件（BUG-14）；损坏文件计数与事件丢弃计数分开报告，损坏文件数
    不冒充事件数（BUG-15）。本地保留期与数据库 cleanup 共用同一配置，
    过期清理与容量损失分别计数（BUG-24）。
    """

    def __init__(self, directory: str, instance: str, max_files: int, max_bytes: int) -> None:
        self.dir = directory
        self.instance = re.sub(r"[^A-Za-z0-9_.-]", "_", instance)[:48] or "inst"
        self.max_files = max(1, max_files)
        self.max_bytes = max(1, max_bytes)
        self.replayed_total = 0
        self.corrupt_total = 0
        self.corrupt_purged_total = 0
        self.dropped_total = 0
        self.dropped_last_at = ""
        self.dropped_last_reason = ""
        self.expired_total = 0
        self.expired_last_at = ""
        self._seq = int(time.time() * 1000) % 1_000_000_000
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.dir)

    def _path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def _files(self) -> list[str]:
        """待回放的有效 spool 文件（仅 .jsonl）。"""
        try:
            names = os.listdir(self.dir)
        except OSError:
            return []
        return sorted(
            name for name in names
            if name.startswith(self.instance + "-") and name.endswith(".jsonl")
        )

    def _all_files(self) -> list[str]:
        """容量核算范围：有效文件 + 损坏隔离文件（BUG-14）。"""
        try:
            names = os.listdir(self.dir)
        except OSError:
            return []
        return sorted(
            name for name in names
            if name.startswith(self.instance + "-")
            and (name.endswith(".jsonl") or name.endswith(".jsonl.corrupt"))
        )

    def _enforce_bounds(self) -> bool:
        """容量轮转（不新增文件时使用）；返回清理后是否仍在限额内。

        BUG-22：损失计数与额度释放只在删除**成功**后发生——删除失败
        （锁定 / 权限 / 挂载异常）时立即停止本轮腾挪，按实物重新核算，
        不把仍可回放的文件虚报成丢弃，也不绕过容量继续写新文件。
        """
        return self._rotate_for_capacity(0, require_new_file=False)

    def _rotate_for_capacity(
        self, payload_bytes: int, *, require_new_file: bool,
    ) -> bool:
        """腾出「文件数 + 字节」额度（BUG-22）。

        ``payload_bytes`` 是即将写入的新批字节；``require_new_file``
        表示调用方还要新增一个文件。删除失败立即返回 False（无法腾出
        额度，调用方拒绝新批次）；删除成功才计入损失并释放额度。
        """
        files = self._all_files()
        sizes: dict[str, int] = {}
        total_bytes = 0
        for name in files:
            try:
                size = os.path.getsize(self._path(name))
            except OSError:
                size = 0
            sizes[name] = size
            total_bytes += size
        index = 0
        while index < len(files) and (
            len(files) - index + (1 if require_new_file else 0) > self.max_files
            or total_bytes + payload_bytes > self.max_bytes
        ):
            name = files[index]
            if name.endswith(".corrupt"):
                # 损坏文件里的事件早已丢失：按文件计数，不冒充事件丢弃数。
                dropped = 0
            else:
                dropped = self._count_events(name)
            try:
                os.remove(self._path(name))
            except OSError:
                return False  # 删除失败：额度无法释放，拒绝新批次
            if name.endswith(".corrupt"):
                self.corrupt_purged_total += 1
            else:
                self.dropped_total += dropped
                if dropped:
                    self.dropped_last_at = _utc_now_iso()
                    self.dropped_last_reason = "spool_capacity_rotation"
            total_bytes -= sizes.get(name, 0)
            index += 1
        remaining = len(files) - index + (1 if require_new_file else 0)
        return remaining <= self.max_files and total_bytes + payload_bytes <= self.max_bytes

    def _count_events(self, name: str) -> int:
        try:
            with open(self._path(name), "r", encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        except OSError:
            return 0

    def _retention_cutoff(self) -> datetime:
        days = max(1, int(cfg.GATEWAY_LOG_RETENTION_DAYS))
        return datetime.now(timezone.utc) - timedelta(days=days)

    @staticmethod
    def _event_expired(parsed: dict[str, Any], cutoff: datetime) -> bool:
        """按真实 occurred_at 判定过期；解析不了的时间不当作过期。"""
        raw = parsed.get("occurred_at")
        if not isinstance(raw, str) or not raw:
            return False
        try:
            occurred = datetime.fromisoformat(raw)
        except ValueError:
            return False
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        return occurred < cutoff

    def _enforce_retention(self) -> None:
        """本地保留期清理（BUG-24）：与数据库 cleanup 同一配置，不依赖
        数据库成功——长期故障期间过期事件也在本地到期移除。

        有效文件按行内 ``occurred_at`` 过滤并重写（重写失败保留原文件，
        下轮重试，计数不虚增）；损坏隔离文件按 mtime TTL 清理。
        """
        if not self.enabled:
            return
        cutoff = self._retention_cutoff()
        for name in self._files():
            path = self._path(name)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    lines = handle.readlines()
            except OSError:
                continue
            kept_lines: list[str] = []
            expired = 0
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    parsed = json.loads(stripped)
                except ValueError:
                    kept_lines.append(line)  # 解析失败留给 replay 隔离
                    continue
                if isinstance(parsed, dict) and self._event_expired(parsed, cutoff):
                    expired += 1
                    continue
                kept_lines.append(line)
            if not expired:
                continue
            tmp_path = path + ".tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as handle:
                    handle.writelines(kept_lines)
                os.replace(tmp_path, path)
            except OSError:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                continue  # 重写失败：原文件保留，下轮重试
            self.expired_total += expired
            self.expired_last_at = _utc_now_iso()
            if not any(line.strip() for line in kept_lines):
                try:
                    os.remove(path)
                except OSError:
                    pass
        # 损坏隔离文件 TTL：按 mtime 判定（事件不可读，无法按 occurred_at）。
        corrupt_cutoff = time.time() - max(1, int(cfg.GATEWAY_LOG_RETENTION_DAYS)) * 86400
        for name in self._all_files():
            if not name.endswith(".corrupt"):
                continue
            path = self._path(name)
            try:
                if os.path.getmtime(path) >= corrupt_cutoff:
                    continue
                os.remove(path)
            except OSError:
                continue
            self.corrupt_purged_total += 1

    def write(self, events: list[dict[str, Any]]) -> bool:
        """写入前先预留容量额度（BUG-22）；腾不出额度则拒绝本批。

        返回 False 时事件由调用方按 ``spool_write_failed`` 准确计损——
        只计真正丢失的新事件；既有超额状态不继续扩张，可回放的旧文件
        保留。绝不先创建超额文件再依赖「事后总能删除」来保证上限。
        """
        if not self.enabled or not events:
            return False
        with self._lock:
            payload = "".join(_json_line(event) for event in events)
            payload_bytes = len(payload.encode("utf-8"))
            if payload_bytes > self.max_bytes:
                return False  # 单批超过字节上限：拒绝，不落盘
            if not self._rotate_for_capacity(payload_bytes, require_new_file=True):
                return False
            self._seq += 1
            name = f"{self.instance}-{self._seq:012d}.jsonl"
            try:
                os.makedirs(self.dir, exist_ok=True)
                with open(self._path(name), "w", encoding="utf-8") as handle:
                    handle.write(payload)
            except OSError:
                return False
            return True

    def replay(self, insert) -> int:
        """逐文件补写；``insert(events)`` 失败时抛出，文件保留等下轮。

        返回本轮成功补写的事件数（BUG-13：失败必须向 writer 传播，由其
        记录失败状态并进入退避）。损坏文件改名 .corrupt 并计数；改名失败
        时文件保留（容量仍受 _all_files 约束），下轮重试。恢复前先按
        ``occurred_at`` 过滤过期事件（BUG-24）：过期行不回放，仅在文件
        实际移除后计入 expired 计数（重试不重复计数）。
        """
        written = 0
        if not self.enabled:
            return 0
        cutoff = self._retention_cutoff()
        for name in self._files():
            path = self._path(name)
            events: list[dict[str, Any]] = []
            expired = 0
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        parsed = json.loads(line)
                        if isinstance(parsed, dict):
                            if self._event_expired(parsed, cutoff):
                                expired += 1
                            else:
                                events.append(parsed)
            except (OSError, ValueError):
                try:
                    os.replace(path, path + ".corrupt")
                except OSError:
                    continue  # 隔离失败：保留原文件，容量仍计入
                self.corrupt_total += 1
                continue
            if not events:
                try:
                    os.remove(path)
                except OSError:
                    pass
                else:
                    if expired:
                        self.expired_total += expired
                        self.expired_last_at = _utc_now_iso()
                continue
            insert(events)  # 数据库仍不可用：异常向上传播（BUG-13）
            try:
                os.remove(path)
            except OSError:
                pass  # 删除失败：event_id 幂等保证下轮补写不重复计数
            else:
                if expired:
                    self.expired_total += expired
                    self.expired_last_at = _utc_now_iso()
            self.replayed_total += len(events)
            written += len(events)
        return written

    def snapshot(self) -> dict[str, Any]:
        files = self._files() if self.enabled else []
        all_files = self._all_files() if self.enabled else []
        corrupt_files = sum(1 for name in all_files if name.endswith(".corrupt"))
        total_bytes = 0
        for name in all_files:
            try:
                total_bytes += os.path.getsize(self._path(name))
            except OSError:
                pass
        return {
            "enabled": self.enabled,
            "files": len(files),
            "corrupt_files": corrupt_files,
            "bytes": total_bytes,
            "replayed_total": self.replayed_total,
            "corrupt_total": self.corrupt_total,
            "corrupt_purged_total": self.corrupt_purged_total,
            "dropped_total": self.dropped_total,
            "dropped_last_at": self.dropped_last_at,
            "dropped_last_reason": self.dropped_last_reason,
            "expired_total": self.expired_total,
            "expired_last_at": self.expired_last_at,
        }


def _json_line(event: dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"


class _LogWriter:
    """后台批量写入线程：与业务数据库事务完全隔离，失败有界重试。

    所有数据库尝试（新批次 / 内存重试 / spool 回放）共享同一退避时钟
    （BUG-13）：退避期内不碰数据库，重试批次转存 spool 后新批次也不得
    绕过退避。写入 / 重试 / 清理链在 ``suppress_capture`` 内运行，公共
    db 初始化的 warning/error 不会被重新采集（BUG-11）。
    """

    def __init__(self, store, queue: _EventQueue) -> None:
        self._store = store
        self._queue = queue
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._retry_pending: list[list[dict[str, Any]]] = []
        self._retry_pending_events = 0
        self._retry_not_before = 0.0
        self._consecutive_failures = 0
        self._last_write_error = ""
        self.written_total = 0
        self.last_write_at = ""
        self._spool = _SpoolStore(
            cfg.GATEWAY_LOG_SPOOL_DIR,
            _instance_id(),
            cfg.GATEWAY_LOG_SPOOL_MAX_FILES,
            cfg.GATEWAY_LOG_SPOOL_MAX_BYTES,
        )
        self._next_cleanup_at = time.monotonic() + 60.0
        self._cleanup_interval = 3600.0
        # spool 本地保留期清理（BUG-24）：首个周期立即执行一次，之后与
        # 数据库 cleanup 共用小时级间隔；不依赖数据库成功。
        self._next_spool_retention_at = 0.0
        self.last_cleanup_at = ""
        self.last_cleanup_status = ""
        self.last_cleanup_deleted = 0

    # ── 生命周期 ──

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping = False
        self._thread = threading.Thread(
            target=self._run, name="gw-log-writer", daemon=True
        )
        self._thread.start()

    def stop(self, flush_timeout: float) -> None:
        self._stopping = True
        with self._cond:
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=flush_timeout + 5.0)

    def _wait(self, timeout: float = 2.0) -> None:
        with self._cond:
            self._cond.wait(timeout=timeout)

    def _wake(self) -> None:
        with self._cond:
            self._cond.notify_all()

    # ── 主循环 ──

    def _run(self) -> None:
        while not self._stopping:
            try:
                with suppress_capture():
                    self._process_once()
            except Exception as exc:
                _diag("日志 writer 周期异常: %s", type(exc).__name__)
            if self._stopping:
                break
            self._wait()
        with suppress_capture():
            self._final_flush()

    def _process_once(self) -> None:
        # 0. spool 本地保留期（BUG-24）：纯本地磁盘操作，退避期内照常
        #    执行——数据库长时间不可用正是 spool 积压过期的场景。
        if (
            self._spool.enabled
            and time.monotonic() >= self._next_spool_retention_at
        ):
            self._next_spool_retention_at = time.monotonic() + self._cleanup_interval
            self._spool._enforce_retention()
        now = time.monotonic()
        # 1. 共享退避（BUG-13）：退避期内不碰数据库——新批次、内存重试、
        #    spool 回放都不得绕过。
        if now < self._retry_not_before:
            return
        # 2. 内存重试积压优先（退避到期才尝试，避免连接风暴）
        if self._retry_pending:
            self._flush_retry_pending()
            if self._retry_pending or time.monotonic() < self._retry_not_before:
                return
        else:
            # 3. 队列里没有积压重试时才接收新事件
            batch = self._queue.drain(cfg.GATEWAY_LOG_BATCH_MAX)
            if batch:
                self._write_batch(batch)
                if time.monotonic() < self._retry_not_before:
                    return
        # 4. spool 补写：失败进入同一退避状态机（BUG-13）
        if self._spool.enabled:
            self._replay_spool()
        # 5. 到期清理（单调度者语义由 RPC 内 advisory xact 锁保证）
        if time.monotonic() >= self._next_cleanup_at:
            self._maybe_cleanup()

    def _register_write_failure(self, exc: Exception, diag_message: str) -> None:
        self._consecutive_failures += 1
        self._last_write_error = type(exc).__name__
        backoff = min(2.0 ** min(self._consecutive_failures, 6), 60.0)
        self._retry_not_before = time.monotonic() + backoff
        _diag("%s: %s", diag_message, type(exc).__name__)

    def _write_batch(self, batch: list[dict[str, Any]]) -> None:
        try:
            self._store.insert_events(batch)
            self.written_total += len(batch)
            self.last_write_at = _utc_now_iso()
            self._consecutive_failures = 0
            self._last_write_error = ""
        except Exception as exc:
            self._register_write_failure(exc, "日志批量写入失败（退避重试）")
            # 连续 3 次失败且 spool 可用：转存磁盘，内存腾空
            if self._spool.enabled and self._consecutive_failures >= 3:
                if not self._spool.write(batch):
                    self._queue._count_drop_batch(batch, "spool_write_failed")
                return
            self._append_retry_pending(batch)

    def _append_retry_pending(self, batch: list[dict[str, Any]]) -> None:
        """失败批次进入内存重试；按事件数严格执行上限（BUG-23）。

        上限 0 同样生效（全部丢弃）；单批超过上限时按「级别优先、同级取
        新」裁剪到上限——被裁事件计入丢弃，保留事件沿用原 event_id（重试
        不重复计数）。批次边界不参与上限语义，重试队列按事件数核算。
        """
        max_events = cfg.GATEWAY_LOG_RETRY_PENDING_MAX
        self._retry_pending.append(list(batch))
        self._retry_pending_events += len(batch)
        if self._retry_pending_events <= max_events:
            return
        merged: list[dict[str, Any]] = []
        for item in self._retry_pending:
            merged.extend(item)
        if max_events <= 0:
            keep_ids: set[int] = set()
        else:
            order = sorted(
                range(len(merged)),
                key=lambda index: (
                    _LEVEL_RANK.get(merged[index].get("level"), 0), index),
            )
            keep_ids = {id(merged[index]) for index in order[len(merged) - max_events:]}
        kept: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        for event in merged:
            (kept if id(event) in keep_ids else dropped).append(event)
        self._retry_pending = [kept] if kept else []
        self._retry_pending_events = len(kept)
        if dropped:
            self._queue._count_drop_batch(dropped, "retry_pending_overflow")

    def _flush_retry_pending(self) -> None:
        flushed_any = False
        while self._retry_pending:
            batch = self._retry_pending[0]
            try:
                self._store.insert_events(batch)
            except Exception as exc:
                self._register_write_failure(exc, "日志重试写入失败")
                if self._spool.enabled and self._consecutive_failures >= 3:
                    pending = self._retry_pending
                    self._retry_pending = []
                    self._retry_pending_events = 0
                    for item in pending:
                        if not self._spool.write(item):
                            self._queue._count_drop_batch(item, "spool_write_failed")
                return
            self._retry_pending.pop(0)
            self._retry_pending_events -= len(batch)
            self.written_total += len(batch)
            self.last_write_at = _utc_now_iso()
            self._consecutive_failures = 0
            self._last_write_error = ""
            flushed_any = True
            if self._stopping:
                break
        if flushed_any:
            self._wake()

    def _replay_spool(self) -> None:
        """磁盘回放与写入共用失败 / 退避状态机（BUG-13）。

        回放成功同步 written / last_write 指标；失败按部分成功计数后进入
        退避——重启后只剩磁盘积压时，状态也能准确显示不可用。
        """
        before = self._spool.replayed_total
        try:
            written = self._spool.replay(self._store.insert_events)
        except Exception as exc:
            replayed = self._spool.replayed_total - before
            if replayed:
                self.written_total += replayed
                self.last_write_at = _utc_now_iso()
            self._register_write_failure(exc, "日志 spool 补写失败（退避重试）")
            return
        if written:
            self.written_total += written
            self.last_write_at = _utc_now_iso()
            self._consecutive_failures = 0
            self._last_write_error = ""

    def _maybe_cleanup(self) -> None:
        self._next_cleanup_at = time.monotonic() + self._cleanup_interval
        try:
            result = self._store.cleanup(cfg.GATEWAY_LOG_RETENTION_DAYS)
            self.last_cleanup_at = _utc_now_iso()
            self.last_cleanup_status = str(result.get("status", ""))
            self.last_cleanup_deleted = int(result.get("deleted", 0) or 0)
        except Exception as exc:
            self.last_cleanup_at = _utc_now_iso()
            self.last_cleanup_status = f"failed:{type(exc).__name__}"
            _diag("日志清理失败: %s", type(exc).__name__)

    def _final_flush(self) -> None:
        """正常关闭：先停止接收（handler 已卸载），再做有时间上限的 flush。"""
        deadline = time.monotonic() + cfg.GATEWAY_LOG_FLUSH_TIMEOUT
        batches: list[list[dict[str, Any]]] = list(self._retry_pending)
        self._retry_pending = []
        self._retry_pending_events = 0
        batch = self._queue.drain(cfg.GATEWAY_LOG_BATCH_MAX)
        while batch:
            batches.append(batch)
            batch = self._queue.drain(cfg.GATEWAY_LOG_BATCH_MAX)
        remaining: list[dict[str, Any]] = []
        for item in batches:
            remaining.extend(item)
        while remaining and time.monotonic() < deadline:
            chunk = remaining[: cfg.GATEWAY_LOG_BATCH_MAX]
            try:
                self._store.insert_events(chunk)
                self.written_total += len(chunk)
                self.last_write_at = _utc_now_iso()
                remaining = remaining[len(chunk):]
            except Exception as exc:
                self._last_write_error = type(exc).__name__
                _diag("关闭 flush 写入失败: %s", type(exc).__name__)
                break
        if remaining:
            if self._spool.enabled:
                if not self._spool.write(remaining):
                    self._queue._count_drop_batch(remaining, "shutdown_spool_failed")
            else:
                self._queue._count_drop_batch(remaining, "shutdown_flush_timeout")

    # ── 状态 ──

    def snapshot(self) -> dict[str, Any]:
        return {
            "written_total": self.written_total,
            "last_write_at": self.last_write_at,
            "last_write_error": self._last_write_error,
            "consecutive_failures": self._consecutive_failures,
            "spool": self._spool.snapshot(),
            "retention_days": cfg.GATEWAY_LOG_RETENTION_DAYS,
            "last_cleanup_at": self.last_cleanup_at,
            "last_cleanup_status": self.last_cleanup_status,
            "last_cleanup_deleted": self.last_cleanup_deleted,
            "instance_id": _instance_id(),
            "release_id": _release_id(),
        }


_queue = _EventQueue(cfg.GATEWAY_LOG_QUEUE_MAX)
_writer: _LogWriter | None = None
_writer_lock = threading.Lock()


def get_writer() -> _LogWriter | None:
    return _writer


def startup(store=None) -> None:
    """安装采集 handler 并启动后台 writer（幂等，可重复调用）。"""
    global _writer
    if not cfg.GATEWAY_LOG_ENABLED:
        return
    install_handler()
    with _writer_lock:
        if _writer is not None and _writer._thread is not None and _writer._thread.is_alive():
            return
        if store is None:
            from . import log_store

            store = log_store
        _writer = _LogWriter(store, _queue)
        _writer.start()


def shutdown() -> None:
    """先停止接收新事件，再做有时间上限的 flush（幂等）。"""
    global _writer
    uninstall_handler()
    with _writer_lock:
        writer, _writer = _writer, None
    if writer is not None:
        writer.stop(cfg.GATEWAY_LOG_FLUSH_TIMEOUT)


def collection_status() -> dict[str, Any]:
    """采集状态快照（只含非敏感信息，供 meta 接口与页面显示）。"""
    base = {
        "enabled": cfg.GATEWAY_LOG_ENABLED,
        "min_level": cfg.GATEWAY_LOG_MIN_LEVEL if cfg.GATEWAY_LOG_MIN_LEVEL in _LEVEL_RANK else "WARNING",
        "handler_installed": handler_installed(),
        # TASK-02：标识秘密未就绪时明确显示（不伪造可用状态）。
        "key_ref_ready": key_ref_ready(),
    }
    base.update(_queue.snapshot())
    writer = _writer
    if writer is not None:
        base.update(writer.snapshot())
    else:
        base.update(
            {
                "written_total": 0,
                "last_write_at": "",
                "last_write_error": "",
                "consecutive_failures": 0,
                "spool": {
                    "enabled": bool(cfg.GATEWAY_LOG_SPOOL_DIR),
                    "files": 0,
                    "corrupt_files": 0,
                    "bytes": 0,
                    "replayed_total": 0,
                    "corrupt_total": 0,
                    "corrupt_purged_total": 0,
                    "dropped_total": 0,
                    "dropped_last_at": "",
                    "dropped_last_reason": "",
                    "expired_total": 0,
                    "expired_last_at": "",
                },
                "retention_days": cfg.GATEWAY_LOG_RETENTION_DAYS,
                "last_cleanup_at": "",
                "last_cleanup_status": "",
                "last_cleanup_deleted": 0,
                "instance_id": _instance_id(),
                "release_id": _release_id(),
            }
        )
    return base
