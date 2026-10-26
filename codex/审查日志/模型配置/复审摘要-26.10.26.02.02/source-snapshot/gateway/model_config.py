"""Server-owned provider library and independent, manually selected text routes.

SQLite transactions serialize workers sharing MODEL_CONFIG_PATH. Reads never create
storage; absent storage preserves environment defaults. Corrupt storage fails closed.
Embedding deliberately bypasses this registry and retains its environment settings.
"""
from __future__ import annotations

import contextvars
import functools
import json
import logging
import os
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from .config import cfg
from .embedding_config import EMBEDDING_MODEL, EMBEDDING_DIM
from .logging_events import compute_key_ref, key_ref_label, provider_label, register_runtime_secrets

log = logging.getLogger("gateway.model_config")
PURPOSES = {"chat": "日常聊天", "qq": "QQ", "sky": "光遇",
            "analysis": "记忆提取与分析", "continuity": "连续感总结", "rumination": "反刍"}
_pinned = contextvars.ContextVar("model_config_snapshot", default=None)


class ConfigError(Exception):
    def __init__(self, message="模型配置暂不可用，请检查服务端配置存储。", status=503):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ModelRoute:
    base_url: str
    api_key: str = field(repr=False)
    model: str = ""
    provider_id: str = ""
    key_id: str = "environment"
    provider_name: str = ""
    key_name: str = "现有密钥"
    source: str = "environment"

    @property
    def configured(self):
        return bool(self.base_url and self.api_key and self.model)


def _path():
    explicit = os.environ.get("MODEL_CONFIG_PATH", "").strip()
    if explicit:
        return Path(explicit)
    # Reuse the deployment's existing persistent directory without enabling QQ.
    anchor = os.environ.get("QQBOT_SETTINGS_PATH", "").strip() or os.environ.get("QQBOT_STORE_PATH", "").strip()
    return (Path(anchor).parent if anchor and anchor != ":memory:" else Path("data")) / "model-config.sqlite3"


def _empty():
    return {"revision": 0, "providers": [], "routes": {}}


def _decode(row):
    if row is None:
        raise ConfigError()
    state = json.loads(row[1])
    if (not isinstance(state, dict) or set(state) != {"providers", "routes"}
            or not isinstance(state["providers"], list) or not isinstance(state["routes"], dict)):
        raise ConfigError()
    if type(row[0]) is not int or row[0] < 0 or len(state["providers"]) > 100 or set(state["routes"]) - set(PURPOSES):
        raise ConfigError()
    if any(not isinstance(binding, dict) or set(binding) != {"provider_id", "key_id", "model"}
           or any(not isinstance(value, str) for value in binding.values())
           for binding in state["routes"].values()):
        raise ConfigError()
    ids = set()
    try:
        for provider in state["providers"]:
            pid = provider["id"]
            if not isinstance(pid, str) or not pid or pid.startswith("env-") or pid in ids:
                raise ConfigError()
            ids.add(pid)
            clean_provider(provider, provider)
    except (ConfigError, KeyError, TypeError) as exc:
        raise ConfigError() from exc
    register_runtime_secrets(key["value"] for provider in state["providers"] for key in provider["keys"])
    return {**state, "revision": row[0]}


def load():
    pinned = _pinned.get()
    if pinned is not None:
        return pinned
    try:
        path = _path()
        try:
            path.stat()
        except FileNotFoundError:
            return _empty()
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
            return _decode(conn.execute("SELECT revision, value FROM model_config WHERE id=1").fetchone())
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        log.exception("model_config.read_failed location=load error_type=%s", type(exc).__name__)
        raise ConfigError() from exc


@contextmanager
def snapshot():
    token = _pinned.set(load())
    try:
        yield
    finally:
        _pinned.reset(token)


def pinned(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with snapshot():
            return fn(*args, **kwargs)
    return wrapped


def defaults():
    def env(prefix, name):
        return ModelRoute(getattr(cfg, prefix + "_BASE_URL").strip().rstrip("/"),
                          getattr(cfg, prefix + "_API_KEY").strip(),
                          getattr(cfg, prefix + "_MODEL").strip(), "env-" + prefix.lower(),
                          provider_name=name)
    chat = env("UPSTREAM", "现有聊天服务")
    analysis = env("ANALYSIS", "现有记忆分析服务")
    continuity = env("CONTINUITY", "现有连续感服务")
    rumination = ModelRoute(
        cfg.RUMINATION_BASE_URL.strip().rstrip("/") or continuity.base_url,
        cfg.RUMINATION_API_KEY.strip() or continuity.api_key,
        cfg.RUMINATION_MODEL.strip() or continuity.model,
        "env-rumination", provider_name="现有反刍服务")
    return {"chat": chat, "qq": chat, "sky": chat, "analysis": analysis,
            "continuity": continuity, "rumination": rumination}


def _providers(state):
    providers = list(state["providers"])
    seen = set()
    for route in defaults().values():
        if route.provider_id in seen or not route.configured:
            continue
        seen.add(route.provider_id)
        providers.append({"id": route.provider_id, "name": route.provider_name,
                          "base_url": route.base_url, "models": [route.model], "readonly": True,
                          "keys": [{"id": "environment", "name": "现有密钥", "value": route.api_key}]})
    return providers


def _resolve(purpose, state):
    if purpose not in PURPOSES:
        raise ConfigError("未知的文本模型用途。", 400)
    binding = state["routes"].get(purpose)
    if binding is None:
        return defaults()[purpose]
    try:
        provider = next(p for p in _providers(state) if p["id"] == binding["provider_id"])
        key = next(k for k in provider["keys"] if k["id"] == binding["key_id"])
        if binding["model"] not in provider["models"]:
            raise ValueError("unknown model")
        return ModelRoute(provider["base_url"], key["value"], binding["model"],
                          provider["id"], key["id"], provider["name"], key["name"], "runtime")
    except (KeyError, TypeError, StopIteration, ValueError) as exc:
        raise ConfigError("已选择的模型配置无效，请重新设置对应用途。", 409) from exc


def resolve(purpose):
    return _resolve(purpose, load())


def configured(purpose):
    try:
        return resolve(purpose).configured
    except ConfigError:
        return False


def route_metadata(route):
    ref = compute_key_ref(route.api_key) if route.api_key else ""
    return {"base_url": route.base_url, "model": route.model, "provider_id": route.provider_id,
            "key_id": route.key_id, "provider_name": route.provider_name, "key_name": route.key_name,
            "source": route.source, "configured": route.configured,
            "key_ref": ref, "label": key_ref_label(ref)}


def _purpose_metadata(purpose, name, state):
    binding = state["routes"].get(purpose)
    try:
        current = {**route_metadata(_resolve(purpose, state)), "available": True, "error": None}
    except ConfigError as exc:
        # Keep the saved choice visible; never silently select its replacement.
        invalid = ModelRoute("", "", binding.get("model", ""),
                             binding.get("provider_id", ""), binding.get("key_id", ""),
                             provider_name="配置已失效", source="invalid")
        current = {**route_metadata(invalid), "available": False, "error": str(exc)}
    return {"id": purpose, "name": name, "selection": binding, "current": current}


def public(state=None):
    state = load() if state is None else state
    providers = []
    for provider in _providers(state):
        providers.append({k: provider[k] for k in ("id", "name", "base_url", "models")} | {
            "readonly": provider.get("readonly", False), "keys": [
                {"id": key["id"], "name": key["name"],
                 "label": key_ref_label(compute_key_ref(key["value"]))} for key in provider["keys"]]})
    # These values intentionally come directly from ANALYSIS_*, never a text route.
    vector = ModelRoute(cfg.ANALYSIS_BASE_URL.rstrip("/"), cfg.ANALYSIS_API_KEY,
                        EMBEDDING_MODEL, provider_name=provider_label(cfg.ANALYSIS_BASE_URL))
    return {"revision": state["revision"], "providers": providers,
            "purposes": [_purpose_metadata(purpose, name, state)
                         for purpose, name in PURPOSES.items()],
            "vector": {**route_metadata(vector), "dimensions": EMBEDDING_DIM, "readonly": True}}


def _text(value, label, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ConfigError(label + "不能为空、超长或包含控制字符。", 400)
    return value.strip()


def clean_provider(raw, previous=None):
    if not isinstance(raw, dict) or set(raw) - {"id", "name", "base_url", "models", "keys"}:
        raise ConfigError("供应商字段无效。", 400)
    name = _text(raw.get("name"), "供应商名称", 120)
    base = _text(raw.get("base_url"), "API 地址", 2000).rstrip("/")
    try:
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid base URL")
    except ValueError as exc:
        raise ConfigError("API 地址须为不含账号、查询参数和片段的 HTTP(S) 地址。", 400) from exc
    models = raw.get("models")
    if not isinstance(models, list) or not 1 <= len(models) <= 100:
        raise ConfigError("请填写1到100个模型名称。", 400)
    models = list(dict.fromkeys(_text(m, "模型名称") for m in models))
    keys = raw.get("keys")
    if not isinstance(keys, list) or not 1 <= len(keys) <= 50:
        raise ConfigError("请添加1到50把 Key。", 400)
    existing = {k["id"]: k for k in (previous or {}).get("keys", [])}
    cleaned, ids = [], set()
    for key in keys:
        if not isinstance(key, dict) or set(key) - {"id", "name", "value"}:
            raise ConfigError("Key 字段无效。", 400)
        kid = key.get("id") or str(uuid4())
        if not isinstance(kid, str) or (key.get("id") and kid not in existing) or kid in ids:
            raise ConfigError("Key 标识无效或重复。", 400)
        ids.add(kid)
        value = key.get("value")
        if not value and kid in existing:
            value = existing[kid]["value"]
        cleaned.append({"id": kid, "name": _text(key.get("name"), "Key 别名", 120),
                        "value": _text(value, "API Key", 4000)})
    register_runtime_secrets(key["value"] for key in cleaned)
    return {"id": previous["id"] if previous else str(uuid4()), "name": name,
            "base_url": base, "models": models, "keys": cleaned}


def mutate(revision, operation, payload):
    if type(revision) is not int or revision < 0:
        raise ConfigError("配置版本无效，请刷新后重试。", 400)
    path = _path()
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.stat()
            target = path.resolve().as_uri() + "?mode=rw"
        except FileNotFoundError:
            # Build a complete first state privately. Publish without replacement
            # only after commit; failed saves cannot create/reset the real store.
            fd, filename = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
            os.close(fd)
            temporary = Path(filename)
            target = temporary.resolve().as_uri() + "?mode=rw"
        with closing(sqlite3.connect(target, uri=True, timeout=5, isolation_level=None)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if temporary is not None:
                    conn.execute("CREATE TABLE model_config (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, value TEXT NOT NULL)")
                    state = _empty()
                else:
                    state = _decode(conn.execute("SELECT revision, value FROM model_config WHERE id=1").fetchone())
                if state["revision"] != revision:
                    raise ConfigError("配置已被其他页面更新，请刷新后重试。", 409)
                if operation == "provider":
                    pid = payload.get("id")
                    previous = next((p for p in state["providers"] if p["id"] == pid), None)
                    if pid and previous is None:
                        raise ConfigError("该供应商不存在或为只读环境配置。", 400)
                    provider = clean_provider(payload, previous)
                    if previous:
                        state["providers"][state["providers"].index(previous)] = provider
                    elif len(state["providers"]) < 100:
                        state["providers"].append(provider)
                    else:
                        raise ConfigError("供应商数量已达100个。", 400)
                elif operation in PURPOSES:
                    if payload is None:
                        state["routes"].pop(operation, None)
                    elif isinstance(payload, dict) and set(payload) == {"provider_id", "key_id", "model"}:
                        state["routes"][operation] = payload
                    else:
                        raise ConfigError("请选择供应商、Key 和模型。", 400)
                else:
                    raise ConfigError("该用途仅可查看，不能修改。", 400)
                # A stale environment binding must not prevent repairing another
                # purpose. Provider edits still protect every reference to it.
                affected = ([operation] if operation in PURPOSES and payload is not None else
                            [p for p, binding in state["routes"].items()
                             if operation == "provider" and binding["provider_id"] == provider["id"]])
                for purpose in affected:
                    if not _resolve(purpose, state).configured:
                        raise ConfigError("当前选择的模型服务未配置完整。", 400)
                state["revision"] += 1
                value = json.dumps({k: state[k] for k in ("providers", "routes")}, ensure_ascii=False)
                response = public(state)
                conn.execute("INSERT INTO model_config VALUES(1, ?, ?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision, value=excluded.value", (state["revision"], value))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        if temporary is not None:
            try:
                os.link(temporary, path)
            except FileExistsError:
                # A concurrent first writer published a complete state first.
                # Re-enter the existing-store transaction to enforce its revision.
                return mutate(revision, operation, payload)
        return response
    except ConfigError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        log.exception("model_config.write_failed operation=%s error_type=%s", operation, type(exc).__name__)
        raise ConfigError("模型配置保存失败，请检查服务端存储后重试。") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError as exc:
                log.warning("model_config.temporary_cleanup_failed error_type=%s", type(exc).__name__, exc_info=True)
