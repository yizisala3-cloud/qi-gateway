"""运行时配置，从环境变量读取。"""
import os
from dotenv import load_dotenv

from .model_routing import DEFAULT_UPSTREAM_MODEL

load_dotenv()

DEFAULT_UPSTREAM_BASE_URL = "https://api.deepseek.com/v1"


def _clamped_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read an integer env var; unparsable or missing values use the default."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


class Config:
    GATEWAY_TOKEN: str = os.getenv("GATEWAY_TOKEN", "")
    UPSTREAM_BASE_URL: str = os.getenv("UPSTREAM_BASE_URL", DEFAULT_UPSTREAM_BASE_URL)
    UPSTREAM_API_KEY: str = os.getenv("UPSTREAM_API_KEY", "")
    UPSTREAM_MODEL: str = os.getenv("UPSTREAM_MODEL", DEFAULT_UPSTREAM_MODEL)
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    # Preferred backend-only key. Modern sb_secret_* keys and legacy service_role
    # keys bypass RLS and must never be exposed to the browser or source control.
    SUPABASE_SECRET_KEY: str = os.getenv("SUPABASE_SECRET_KEY", "")
    SUPABASE_SERVICE_ROLE_KEY: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    # Transitional fallback for existing deployments. This may be a publishable key.
    SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")
    PORT: int = int(os.getenv("PORT", "8000"))
    UPSTREAM_READ_TIMEOUT: float = float(os.getenv("UPSTREAM_READ_TIMEOUT", "180"))
    # 记忆检索与旧版自动总结（硅基流动）：普通记忆提取与 embedding。
    ANALYSIS_BASE_URL: str = os.getenv("ANALYSIS_BASE_URL", "https://api.siliconflow.cn/v1")
    ANALYSIS_API_KEY: str = os.getenv("ANALYSIS_API_KEY", "")
    ANALYSIS_MODEL: str = os.getenv("ANALYSIS_MODEL", "Qwen/Qwen2.5-7B-Instruct")
    # 连续感总结与 Shadow Preview 的独立文本提取模型。默认留空表示未配置，
    # 绝不回退复用 ANALYSIS_*；必须提供 OpenAI-compatible /chat/completions。
    CONTINUITY_BASE_URL: str = os.getenv("CONTINUITY_BASE_URL", "")
    CONTINUITY_API_KEY: str = os.getenv("CONTINUITY_API_KEY", "")
    CONTINUITY_MODEL: str = os.getenv("CONTINUITY_MODEL", "")
    # 连续感文本提取最大输出 token 数。推理模型需要足够预算完成 reasoning 和
    # JSON 输出；允许 1024-120000，越界按边界处理。默认 8192。只作用于连续感
    # 提取，不影响旧版自动总结、普通聊天上游和 embedding。
    CONTINUITY_MAX_TOKENS: int = _clamped_env_int("CONTINUITY_MAX_TOKENS", 8192, 1024, 120000)
    # 反刍连续感路径的独立提取模型。留空时回退复用 CONTINUITY_*；两者都为空
    # 表示反刍未配置。提示词、游标与运行记录始终独立于连续感快速路径。
    RUMINATION_BASE_URL: str = os.getenv("RUMINATION_BASE_URL", "")
    RUMINATION_API_KEY: str = os.getenv("RUMINATION_API_KEY", "")
    RUMINATION_MODEL: str = os.getenv("RUMINATION_MODEL", "")
    # 反刍文本提取最大输出 token 数。允许 1024-120000，越界按边界处理；默认 8192。
    RUMINATION_MAX_TOKENS: int = _clamped_env_int("RUMINATION_MAX_TOKENS", 8192, 1024, 120000)
    # 反刍每日调度小时（Asia/Shanghai，0-23），默认 6 点。
    RUMINATION_DAILY_HOUR: int = max(0, min(23, int(os.getenv("RUMINATION_DAILY_HOUR", "6"))))
    # 记忆总结。assistant_id 留空时从 chat_messages 最新有效记录自动发现。
    MEMORY_ASSISTANT_ID: str = os.getenv("MEMORY_ASSISTANT_ID", "")
    MEMORY_DIGEST_MAX_MESSAGES: int = int(os.getenv("MEMORY_DIGEST_MAX_MESSAGES", "60"))
    MEMORY_DIGEST_MAX_CHARS: int = int(os.getenv("MEMORY_DIGEST_MAX_CHARS", "12000"))
    MEMORY_DIGEST_DAILY_HOUR: int = max(0, min(23, int(os.getenv("MEMORY_DIGEST_DAILY_HOUR", "3"))))
    MEMORY_DIGEST_IDLE_HOURS: float = float(os.getenv("MEMORY_DIGEST_IDLE_HOURS", "6"))
    # OrangeChat request_memory tool. Keep this token separate from gateway and
    # Supabase credentials so plugin access can be revoked independently.
    MEMORY_PLUGIN_TOKEN: str = os.getenv("MEMORY_PLUGIN_TOKEN", "")
    # Remote MCP uses a separate bearer token and never shares the legacy
    # OrangeChat compatibility credential.
    MCP_MEMORY_TOKEN: str = os.getenv("MCP_MEMORY_TOKEN", "")
    MEMORY_REQUEST_RATE_LIMIT: int = int(os.getenv("MEMORY_REQUEST_RATE_LIMIT", "6"))
    # OrangeChat todo tools. This token is independent from every other token.
    TODO_PLUGIN_TOKEN: str = os.getenv("TODO_PLUGIN_TOKEN", "")
    TODO_REQUEST_RATE_LIMIT: int = int(os.getenv("TODO_REQUEST_RATE_LIMIT", "60"))

    @property
    def supabase_server_key(self) -> str:
        return (
            self.SUPABASE_SECRET_KEY
            or self.SUPABASE_SERVICE_ROLE_KEY
            or self.SUPABASE_KEY
        )

    @property
    def supabase_elevated_key_configured(self) -> bool:
        return bool(self.SUPABASE_SECRET_KEY or self.SUPABASE_SERVICE_ROLE_KEY)


cfg = Config()
