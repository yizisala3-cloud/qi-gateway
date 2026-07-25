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
    # 情绪分析：独立的模型配置（走硅基流动，便宜快速无 thinking）
    ANALYSIS_BASE_URL: str = os.getenv("ANALYSIS_BASE_URL", "https://api.siliconflow.cn/v1")
    ANALYSIS_API_KEY: str = os.getenv("ANALYSIS_API_KEY", "")
    ANALYSIS_MODEL: str = os.getenv("ANALYSIS_MODEL", "Qwen/Qwen2.5-7B-Instruct")


cfg = Config()
