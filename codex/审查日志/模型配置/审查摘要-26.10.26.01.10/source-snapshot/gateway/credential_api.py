"""配置页密钥元数据与按项查看 API（§5.3 / §8.3 / §9.4，TASK-01）。

* 白名单固定（服务端定义，客户端不能提交任意环境变量名）：模型 API Key
  与既有插件 / MCP / 待办 Token。Supabase secret / service_role / 过渡
  Key、数据库密码、GATEWAY_KEY_REF_SECRET 标识秘密保持仅服务端使用，
  不进入查看白名单；``GATEWAY_TOKEN`` 是既有登录 / 调用凭证，不另建
  重复展示入口。
* 元数据接口只返回用途、是否配置、实际生效来源、安全上游、完整
  ``key_ref``、显示标签与就绪状态——绝无真实值、可恢复编码或携带真实
  值的 HTML / data 属性。
* 真实值只在用户点击某一项「显示」后由专用 reveal 接口按项返回；两类
  响应均 ``Cache-Control: no-store``。查看接口沿用管理后台
  ``GATEWAY_TOKEN`` Bearer 鉴权 + 恒时比较，token 缺失 / 错误 / 未配置
  一律拒绝（fail-closed）。当前聊天与管理共用同一 token 的单用户前提
  见执行文档 §5.3；存在其他普通调用者时须先增加独立管理凭证。
* 反刍实际生效 Key 逐项回退 CONTINUITY_*：来源与标识按实际生效值展示，
  与日志事件归属（TASK-02）一致。
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import cfg
from . import model_config
from .logging_events import compute_key_ref, key_ref_label, key_ref_ready, provider_label

log = logging.getLogger("gateway.credential_api")

_NO_STORE = {"Cache-Control": "no-store"}
_MODEL_PURPOSE = {"upstream": "chat", "analysis": "analysis", "continuity": "continuity",
                  "rumination": "rumination", "qq": "qq", "sky": "sky"}


def _authorized(request: Request) -> bool:
    if not cfg.GATEWAY_TOKEN:
        return False
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    return bool(token) and hmac.compare_digest(token, cfg.GATEWAY_TOKEN)


def _error(message: str, status: int, code: str) -> JSONResponse:
    return JSONResponse(
        {"error": message, "error_code": code}, status_code=status, headers=_NO_STORE
    )


# ── 白名单（固定；客户端不能读任意配置属性） ─────────────────────
# key_envs 按顺序回退：第一个有值的生效，source 标明实际来源。
_CREDENTIAL_SPECS: tuple[dict[str, Any], ...] = (
    {
        "id": "upstream",
        "role": "聊天转发模型",
        "key_envs": ("UPSTREAM_API_KEY",),
        "provider_env": "UPSTREAM_BASE_URL",
    },
    {
        "id": "analysis",
        "role": "记忆提取与分析",
        "key_envs": ("ANALYSIS_API_KEY",),
        "provider_env": "ANALYSIS_BASE_URL",
    },
    {
        "id": "continuity",
        "role": "连续感总结",
        "key_envs": ("CONTINUITY_API_KEY",),
        "provider_env": "CONTINUITY_BASE_URL",
    },
    {
        "id": "rumination",
        "role": "反刍连续感",
        # 实际生效 Key 逐项回退 CONTINUITY_*（与 memory_rumination._model_config
        # 一致）：来源标明回退，标识按实际生效值计算。
        "key_envs": ("RUMINATION_API_KEY", "CONTINUITY_API_KEY"),
        "provider_env": "RUMINATION_BASE_URL",
        "provider_fallback_env": "CONTINUITY_BASE_URL",
    },
    {
        "id": "memory_plugin",
        "role": "记忆插件（request_memory）",
        "key_envs": ("MEMORY_PLUGIN_TOKEN",),
        "provider_env": None,
    },
    {
        "id": "mcp_memory",
        "role": "MCP 服务",
        "key_envs": ("MCP_MEMORY_TOKEN",),
        "provider_env": None,
    },
    {
        "id": "todo_plugin",
        "role": "待办插件",
        "key_envs": ("TODO_PLUGIN_TOKEN",),
        "provider_env": None,
    },
)


def _resolve_credential(spec: dict[str, Any]) -> dict[str, Any]:
    """按白名单解析实际生效值 / 来源 / 上游 / 标识（不返回真实值）。"""
    value = ""
    source = ""
    for env_name in spec["key_envs"]:
        candidate = getattr(cfg, env_name, "")
        if isinstance(candidate, str) and candidate.strip():
            value = candidate.strip()
            source = env_name
            break
    if not source:
        # 未配置：来源显示回退链首选项。
        source = spec["key_envs"][0]
    provider = ""
    provider_env = spec.get("provider_env")
    if provider_env:
        base_url = getattr(cfg, provider_env, "") or ""
        if not base_url.strip() and spec.get("provider_fallback_env"):
            base_url = getattr(cfg, spec["provider_fallback_env"], "") or ""
        provider = provider_label(base_url)
    purpose = _MODEL_PURPOSE.get(spec["id"])
    if purpose:
        route = model_config.resolve(purpose)
        if route.source == "runtime":
            value, source, provider = route.api_key, "runtime", provider_label(route.base_url)
    key_ref = compute_key_ref(value) if value else ""
    return {
        "credential_id": spec["id"],
        "role": spec["role"],
        "configured": bool(value),
        "source": source,
        "fallback": bool(value) and source != "runtime" and source != spec["key_envs"][0],
        "provider": provider,
        "key_ref": key_ref,
        "label": key_ref_label(key_ref),
        "revealable": True,
    }


def _spec_for(credential_id: str) -> dict[str, Any] | None:
    for spec in _CREDENTIAL_SPECS:
        if spec["id"] == credential_id:
            return spec
    return None


async def credentials_metadata(request: Request) -> JSONResponse:
    """白名单元数据：用途 / 配置状态 / 实际来源 / 安全上游 / 标识。

    绝不返回真实值；初始加载页面只调用本接口（不取回任何明文）。
    """
    if not _authorized(request):
        return _error("未登录或令牌无效", 401, "unauthorized")
    model_error = None
    try:
        with model_config.snapshot():
            credentials = [_resolve_credential(spec) for spec in _CREDENTIAL_SPECS]
    except model_config.ConfigError as exc:
        # Model storage does not own plugin credentials. Preserve their independent view.
        credentials = [_resolve_credential(spec) for spec in _CREDENTIAL_SPECS if spec["id"] not in _MODEL_PURPOSE]
        model_error = str(exc)
    return JSONResponse(
        {
            "credentials": credentials,
            # 标识秘密未就绪时明确提示（不伪造可用状态）。
            "key_ref_ready": key_ref_ready(),
            "request_id": _request_id(request),
            "model_config_error": model_error,
        },
        headers=_NO_STORE,
    )


async def credential_reveal(request: Request) -> JSONResponse:
    """用户点击某一项「显示」后按项返回真实值（只读，不修改配置）。

    路径 / query / 错误体与访问日志不携带真实值；未配置与未授权真实
    报错。响应只包含这一项的真实值及其当前有效来源 / 标识。
    """
    if not _authorized(request):
        return _error("未登录或令牌无效", 401, "unauthorized")
    credential_id = request.path_params.get("credential_id", "")
    spec = _spec_for(str(credential_id))
    if spec is None:
        return _error("未知的密钥项", 404, "unknown_credential")
    try:
        purpose = _MODEL_PURPOSE.get(spec["id"])
        if purpose:
            with model_config.snapshot():
                resolved = _resolve_credential(spec)
                selected_value = model_config.resolve(purpose).api_key
        else:
            resolved = _resolve_credential(spec)
            selected_value = None
    except model_config.ConfigError as exc:
        return _error(str(exc), exc.status, "model_config_unavailable")
    if not resolved["configured"]:
        return _error("该密钥项当前未配置", 409, "credential_not_configured")
    value = ""
    for env_name in spec["key_envs"]:
        candidate = getattr(cfg, env_name, "")
        if isinstance(candidate, str) and candidate.strip():
            value = candidate.strip()
            break
    if selected_value is not None:
        value = selected_value
    return JSONResponse(
        {
            "credential_id": spec["id"],
            "role": spec["role"],
            "source": resolved["source"],
            "fallback": resolved["fallback"],
            "provider": resolved["provider"],
            "key_ref": resolved["key_ref"],
            "label": resolved["label"],
            "value": value,
        },
        headers=_NO_STORE,
    )


def _request_id(request: Request) -> str:
    # 复用中间件生成的服务端请求 ID（客户端提供的同名头不信任）。
    return request.headers.get("x-request-id", "")


credential_api_routes = [
    Route("/admin/api/config/credentials", credentials_metadata, methods=["GET"]),
    Route(
        "/admin/api/config/credentials/{credential_id}/reveal",
        credential_reveal,
        methods=["POST"],
    ),
]
