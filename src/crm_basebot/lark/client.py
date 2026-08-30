"""构造飞书 SDK 客户端。

token 不用我们管：lark-oapi 内部会自己申请并缓存 tenant_access_token，
过期自动续期。所以这个模块只负责把凭证喂进去，并把客户端做成单例，
避免每次调用都重新走一遍鉴权。
"""

from __future__ import annotations

from functools import lru_cache

import lark_oapi as lark

from ..config import get_settings

_LOG_LEVELS = {
    "DEBUG": lark.LogLevel.DEBUG,
    "INFO": lark.LogLevel.INFO,
    "WARN": lark.LogLevel.WARNING,
    "WARNING": lark.LogLevel.WARNING,
    "ERROR": lark.LogLevel.ERROR,
}


def _log_level() -> lark.LogLevel:
    return _LOG_LEVELS.get(get_settings().log_level.upper(), lark.LogLevel.INFO)


@lru_cache
def get_client() -> lark.Client:
    settings = get_settings()
    return (
        lark.Client.builder()
        .app_id(settings.app_id)
        .app_secret(settings.app_secret)
        .domain(settings.domain)
        .log_level(_log_level())
        .build()
    )
