"""运行时配置，从环境变量读取。"""
import os
from dotenv import load_dotenv

from .model_routing import normalize_upstream_model

load_dotenv()


class Config:
    GATEWAY_TOKEN: str = os.getenv("GATEWAY_TOKEN", "")
    UPSTREAM_BASE_URL: str = os.getenv("UPSTREAM_BASE_URL", "")
    UPSTREAM_API_KEY: str = os.getenv("UPSTREAM_API_KEY", "")
    UPSTREAM_MODEL: str = normalize_upstream_model(os.getenv("UPSTREAM_MODEL", ""))
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    # Preferred backend-only key. Modern sb_secret_* keys and legacy service_role
    # keys bypass RLS and must never be exposed to the browser or source control.
    SUPABASE_SECRET_KEY: str = os.getenv("SUPABASE_SECRET_KEY", "")
    SUPABASE_SERVICE_ROLE_KEY: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    # Transitional fallback for existing deployments. This may be a publishable key.
    SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")
    PORT: int = int(os.getenv("PORT", "8000"))
    UPSTREAM_READ_TIMEOUT: float = float(os.getenv("UPSTREAM_READ_TIMEOUT", "180"))
    # 情绪分析 + 记忆提取（硅基流动）
    ANALYSIS_BASE_URL: str = os.getenv("ANALYSIS_BASE_URL", "https://api.siliconflow.cn/v1")
    ANALYSIS_API_KEY: str = os.getenv("ANALYSIS_API_KEY", "")
    ANALYSIS_MODEL: str = os.getenv("ANALYSIS_MODEL", "Qwen/Qwen2.5-7B-Instruct")
    # 记忆总结。assistant_id 留空时从 chat_messages 最新有效记录自动发现。
    MEMORY_ASSISTANT_ID: str = os.getenv("MEMORY_ASSISTANT_ID", "")
    MEMORY_DIGEST_MAX_MESSAGES: int = int(os.getenv("MEMORY_DIGEST_MAX_MESSAGES", "60"))
    MEMORY_DIGEST_MAX_CHARS: int = int(os.getenv("MEMORY_DIGEST_MAX_CHARS", "12000"))
    MEMORY_DIGEST_IDLE_HOURS: float = float(os.getenv("MEMORY_DIGEST_IDLE_HOURS", "6"))
    # OrangeChat request_memory tool. Keep this token separate from gateway and
    # Supabase credentials so plugin access can be revoked independently.
    MEMORY_PLUGIN_TOKEN: str = os.getenv("MEMORY_PLUGIN_TOKEN", "")
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

