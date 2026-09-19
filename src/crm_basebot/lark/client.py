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


def build_client(settings) -> lark.Client:
    """按给定凭证造一个客户端。

    单独一个函数是为了迁移：源端和目标端是**两个不同的飞书应用**，必须能拿到两个
    客户端。``get_client()`` 是单例（源端用），目标端走这里现造。
    """
    return (
        lark.Client.builder()
        .app_id(settings.app_id)
        .app_secret(settings.app_secret)
        .domain(settings.domain)
        .log_level(_LOG_LEVELS.get(settings.log_level.upper(), lark.LogLevel.INFO))
        .build()
    )


@lru_cache
def get_client() -> lark.Client:
    return build_client(get_settings())
