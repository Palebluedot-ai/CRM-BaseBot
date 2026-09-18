"""集中读取环境变量。"""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
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
    table_daily_board: str = Field(default="", alias="TABLE_DAILY_BOARD")
    table_commission: str = Field(default="", alias="TABLE_COMMISSION")
    table_audit: str = Field(default="", alias="TABLE_AUDIT")
    table_sales: str = Field(default="", alias="TABLE_SALES")

    # 日读看板 xlsx 的默认路径。import_daily_board.py 支持 --file 覆盖。
    daily_board_xlsx: str = Field(default="", alias="DAILY_BOARD_XLSX")

    # 每日增量导入在哪个目录找导出文件（scripts/import_daily_incremental.py 挑最新的一份）。
    # --from-mail 时下载的附件也落到这里。
    daily_export_dir: str = Field(default="attachments", alias="DAILY_EXPORT_DIR")

    # 邮件抓取（Microsoft Graph 的应用权限）。四个键齐了才能用 --from-mail；
    # 只在需要读邮件时才要求，所以给默认空值而不是必填 —— 手工 --file 导入不该被它拦住。
    ms_tenant_id: str = Field(default="", alias="MICROSOFT_GRAPH_TENANT_ID")
    ms_client_id: str = Field(default="", alias="MICROSOFT_GRAPH_CLIENT_ID")
    ms_client_secret: str = Field(default="", alias="MICROSOFT_GRAPH_CLIENT_SECRET")
    ms_user_id: str = Field(default="", alias="MICROSOFT_GRAPH_USER_ID")
    # 发件人留空就用 graph/mail.py 里的默认值（内部系统地址），改发件人只改 .env。
    graph_sender: str = Field(default="", alias="GRAPH_SENDER")

    domain: str = Field(default="https://open.feishu.cn", alias="LARK_DOMAIN")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # 渠道编号默认由后端串行递增：读最大号 +1，在写锁的临界区内。
    # 渠道编号是文本列，现成的 R001-R101 从模板导入（2026-09-17 定的）。只有把那一列
    # 重建成 Bitable 的自动编号字段时才改成 true，改之前用 scripts/verify_numbering.py 实测。
    referral_auto_number: bool = Field(default=False, alias="REFERRAL_AUTO_NUMBER")

    # 对账把交易归到哪个月，按这个时区算。Bitable 日期字段存的是 UTC 毫秒时间戳，
    # 直接按 UTC 取月份的话，本地每个月 1 号 0 点到 8 点的交易会掉进上个月。
    # 用新加坡是 2026-09-04 定的业务规则；结算口径变了改 .env，不用改代码。
    business_timezone: str = Field(default="Asia/Singapore", alias="BUSINESS_TIMEZONE")

    @field_validator("business_timezone")
    @classmethod
    def _timezone_must_exist(cls, value: str) -> str:
        """拼错时区名要在启动时炸，不能等对账跑到一半，更不能悄悄退回 UTC。"""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"「{value}」不是合法的 IANA 时区名，要写成 Asia/Singapore 这种形式"
            ) from exc
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
