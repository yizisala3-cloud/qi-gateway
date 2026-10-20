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
    GEMINI_BROWSER_TOOL_COMPAT_ENABLED: bool = (
        os.getenv("GEMINI_BROWSER_TOOL_COMPAT_ENABLED", "false").strip().lower()
        in ("1", "true", "yes", "on")
    )
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    # Preferred backend-only key. Modern sb_secret_* keys and legacy service_role
    # keys bypass RLS and must never be exposed to the browser or source control.
    SUPABASE_SECRET_KEY: str = os.getenv("SUPABASE_SECRET_KEY", "")
    SUPABASE_SERVICE_ROLE_KEY: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    # Transitional fallback for existing deployments. This may be a publishable key.
    SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")
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
    # 聊天检索冷却：分钟，0 关闭；用户资料 / 互动规则始终豁免。
    # 启动时读取，聊天不为此新增数据库查询。
    MEMORY_RECALL_COOLDOWN_MINUTES: int = _clamped_env_int(
        "MEMORY_RECALL_COOLDOWN_MINUTES", 60, 0, 10080
    )
    MEMORY_DIGEST_MAX_MESSAGES: int = int(os.getenv("MEMORY_DIGEST_MAX_MESSAGES", "60"))
    MEMORY_DIGEST_MAX_CHARS: int = int(os.getenv("MEMORY_DIGEST_MAX_CHARS", "12000"))
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
    # ── 全网关报错日志（§36 独立批次） ─────────────────────────────
    # 未保存页面设置时的采集默认值：默认关闭。管理页设置持久化到
    # app_settings，运行时由 log_settings 同步；关闭后仍可查询历史日志。
    GATEWAY_LOG_ENABLED: bool = (
        os.getenv("GATEWAY_LOG_ENABLED", "false").strip().lower()
        in ("1", "true", "yes", "on")
    )
    # 采集最低级别：WARNING / ERROR / CRITICAL。不默认采集 INFO/DEBUG。
    GATEWAY_LOG_MIN_LEVEL: str = (os.getenv("GATEWAY_LOG_MIN_LEVEL", "WARNING") or "WARNING").strip().upper()
    # 有界内存队列与批量写入参数。
    GATEWAY_LOG_QUEUE_MAX: int = _clamped_env_int("GATEWAY_LOG_QUEUE_MAX", 2000, 100, 50000)
    GATEWAY_LOG_BATCH_MAX: int = _clamped_env_int("GATEWAY_LOG_BATCH_MAX", 50, 1, 200)
    # 写入失败时的内存重试上限（事件数）；超过后按 spool 配置降级。
    GATEWAY_LOG_RETRY_PENDING_MAX: int = _clamped_env_int("GATEWAY_LOG_RETRY_PENDING_MAX", 500, 0, 5000)
    # 建议默认 30 天保留（用户未逐项确认的工程默认值，可调整）。
    GATEWAY_LOG_RETENTION_DAYS: int = _clamped_env_int("GATEWAY_LOG_RETENTION_DAYS", 30, 1, 365)
    # 本地 spool 目录：留空 = 不启用（容器临时盘不承诺跨重建保留）。
    # 配置后按「先脱敏、限文件数、限总字节」落盘，恢复后按 event_id 幂等补写。
    GATEWAY_LOG_SPOOL_DIR: str = os.getenv("GATEWAY_LOG_SPOOL_DIR", "").strip()
    GATEWAY_LOG_SPOOL_MAX_FILES: int = _clamped_env_int("GATEWAY_LOG_SPOOL_MAX_FILES", 200, 1, 10000)
    GATEWAY_LOG_SPOOL_MAX_BYTES: int = _clamped_env_int("GATEWAY_LOG_SPOOL_MAX_BYTES", 52428800, 1048576, 1073741824)
    # 正常关闭时的 flush 时间上限（秒）：到时未写完的事件按 spool 配置
    # 降级或计入丢弃，绝不无限阻塞网关关闭。
    GATEWAY_LOG_FLUSH_TIMEOUT: float = max(0.5, min(60.0, float(os.getenv("GATEWAY_LOG_FLUSH_TIMEOUT", "5") or 5)))
    # 实例 / 部署版本的非敏感标识（多实例排查用；默认取容器主机名）。
    GATEWAY_INSTANCE_ID: str = os.getenv("GATEWAY_INSTANCE_ID", "").strip()
    GATEWAY_RELEASE_ID: str = os.getenv("GATEWAY_RELEASE_ID", "").strip()
    # TASK-02：密钥标识生成秘密。独立服务端值（建议 ≥32 字节安全随机值），
    # 只用于 HMAC-SHA-256 计算 key_ref；不复用 GATEWAY_TOKEN / 上游密钥 /
    # 公开盐，永不进入浏览器、日志、平台输出或构建上下文。缺失或短于
    # 32 字节时密钥标识明确不可用（不兜底），业务与安全日志照常运行。
    GATEWAY_KEY_REF_SECRET: str = os.getenv("GATEWAY_KEY_REF_SECRET", "").strip()

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
