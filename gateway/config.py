"""运行时配置，从环境变量读取。"""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    GATEWAY_TOKEN: str = os.getenv("GATEWAY_TOKEN", "")
    UPSTREAM_BASE_URL: str = os.getenv("UPSTREAM_BASE_URL", "")
    UPSTREAM_API_KEY: str = os.getenv("UPSTREAM_API_KEY", "")
    UPSTREAM_MODEL: str = os.getenv("UPSTREAM_MODEL", "")
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")
    PORT: int = int(os.getenv("PORT", "8000"))
    UPSTREAM_READ_TIMEOUT: float = float(os.getenv("UPSTREAM_READ_TIMEOUT", "180"))
    # 情绪分析用的模型（不带 thinking，更快更便宜）
    ANALYSIS_MODEL: str = os.getenv("ANALYSIS_MODEL", "[kiro量高缓]claude-opus-4-6")


cfg = Config()
