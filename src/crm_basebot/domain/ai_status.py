"""客户是不是 AI —— 交易佣金只付给 AI 客户带来的交易（2026-09-25 定的）。

AI = Accredited Investor / Professional Investor，这里两者通用。规则：

  · 客户**成为 AI 的那个月起**，这个月整月和之后每个月的交易都算佣金。9 月 10 日才升级，
    9 月 1–9 日的交易照样算 —— 按月，不按天。
  · 成为 AI 之前的月份不算。
  · **只管 2026-09 起的结算**（``RULE_START``）。8 月及以前已经结算付款，任何时候重算都
    按老规矩，不能因为这条新规则改掉付出去的钱。
  · 2026-09-25 之前登记的客户这两列是空的：**空的照旧算**（他们是当初按资格登记进来的）。

客户表上两列：

  ``AI状态``    开户即AI / 升级为AI / 非AI
  ``升级AI日期``  升级为AI 时必填；开户即AI 可填可不填

判定顺序：有日期看日期；没日期时「非AI」「升级为AI」不算（后者是日期还没补），其余照算。

**只管交易佣金。** ECAS 返佣开了户就返，不看 AI（见 domain/ecas.py）。

月结（commission.py）、佣金查询（commission_query.py）、渠道详情卡（referral_history.py）
三处都走这里的 ``AiEligibility.counts``，同一个客户同一个月不会一处算一处不算。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import tzinfo
from typing import Any

from ..lark.values import extract_text
from . import schema

# 这条规则从哪个月的结算开始生效。
RULE_START = "2026-09"


@dataclass(frozen=True)
class AiEligibility:
    status: str = ""
    since: str = ""
    """成为 AI 的月份（YYYY-MM）。空表示没填日期。"""

    def counts(self, period: str) -> bool:
        """这个客户在 ``period`` 这个月的交易算不算佣金。"""
        if period < RULE_START:
            return True
        if self.since:
            return period >= self.since
        if self.status in (schema.AI_STATUS_NOT, schema.AI_STATUS_UPGRADED):
            return False
        return True


def eligibility_of(fields: dict[str, Any], *, tz: tzinfo) -> AiEligibility:
    """客户表的一行 -> 这个客户的 AI 资格。两列都不存在（老表没跑 sync）时照旧算。"""
    from .commission import period_of  # 避免循环导入：commission 也用这个模块

    return AiEligibility(
        status=extract_text(fields.get(schema.CLIENT_AI_STATUS)).strip(),
        since=period_of(fields.get(schema.CLIENT_AI_DATE), tz=tz),
    )
