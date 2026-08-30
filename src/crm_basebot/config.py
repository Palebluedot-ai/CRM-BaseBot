"""集中读取环境变量。"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_id: str = Field(alias="LARK_APP_ID")
    app_secret: str = Field(alias="LARK_APP_SECRET")
    base_app_token: str = Field(default="", alias="LARK_BASE_APP_TOKEN")

    # 探查 Base 之前这些是空的，所以都给默认值
    table_referral: str = Field(default="", alias="TABLE_REFERRAL")
    table_client: str = Field(default="", alias="TABLE_CLIENT")
    table_transaction: str = Field(default="", alias="TABLE_TRANSACTION")
    table_commission: str = Field(default="", alias="TABLE_COMMISSION")
    table_audit: str = Field(default="", alias="TABLE_AUDIT")
    table_sales: str = Field(default="", alias="TABLE_SALES")

    domain: str = Field(default="https://open.feishu.cn", alias="LARK_DOMAIN")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
