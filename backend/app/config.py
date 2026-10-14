from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # DB / Redis
    database_url: str = "postgresql+psycopg2://stock:stock@db:5432/stock"
    redis_url: str = "redis://redis:6379/0"

    # Auth
    secret_key: str = "change_me"
    access_token_expire_minutes: int = 1440

    # LLM (OpenAI 兼容)
    llm_enabled: bool = False
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_api_key: str = ""
    llm_timeout: int = 20
    llm_max_tokens: int = 300

    # 通知通道
    feishu_webhook: str = ""
    server_chan_sendkey: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: str = ""

    # 数据源
    tushare_token: str = ""

    # CORS
    cors_origins: str = "http://localhost"


@lru_cache
def get_settings() -> Settings:
    return Settings()
