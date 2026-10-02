"""客户是不是 AI —— 交易佣金只付给 AI 客户带来的交易。

AI = Accredited Investor / Professional Investor，这里两者通用。规则（2026-09-26 JY 定的，
取代 9-25 那版「按月」的说法）：

  · **按天算**：客户升级 AI **当天起**的交易就算佣金。9 月 10 日升级，9 月 10 日当天和之后的
    交易算；9 月 9 日及之前的不算。（2026-10-02 改：原先是第二天起，业务确认升级当天就算。）
  · 「开户即AI」的客户，所有交易都算。
  · 「非AI」不算；「升级为AI」但日期还没补的，也先不算（补上日期后重算就回来了）。
  · **只管 2026-08-01 起的交易**（``RULE_START_DAY``；2026-10-02 从 9-01 提前到 8-01，业务定 8 月
    也按 AI 规则）。7 月及以前已经结算付款，任何时候
    重算都按老规矩，不能因为这条新规则改掉付出去的钱。
  · 2026-09-25 之前登记的客户这两列是空的：**空的照旧算**（他们是当初按资格登记进来的）。

客户表上两列：

  ``AI状态``    开户即AI / 升级为AI / 非AI
  ``升级AI日期``  升级为AI 时必填；开户即AI 可填可不填（填了也不看）

判定顺序：开户即AI 全算 → 有日期看日期（当天及之后才算）→ 没日期时「非AI」「升级为AI」不算
→ 其余（老客户）照算。

**只管交易佣金。** ECAS 返佣开了户就返，不看 AI（见 domain/ecas.py）。

同一条规则在三个地方用：

  · Python：月结（commission.py）、佣金查询（commission_query.py）、渠道详情卡
    （referral_history.py）都走 ``commission.trade_counts`` -> ``AiEligibility.counts``。
  · Base：客户表的「AI佣金起算」公式 + 看板「本笔佣金」公式（schema.py）。那两条公式
    是这里的翻版，``gate_number`` 是给自检核对用的同一个数。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, tzinfo
from typing import Any

from ..lark.values import extract_text
from . import schema

# 这条规则从哪一天的交易开始生效。
RULE_START_DAY = date(2026, 8, 1)

# 客户表「AI佣金起算」公式的两个特殊值：0 = 什么时候的交易都算，99999999 = 都不算。
GATE_ALWAYS = schema.AI_GATE_ALWAYS
GATE_NEVER = schema.AI_GATE_NEVER


def day_number(day: date) -> int:
    """2026-09-10 -> 20260910。Base 公式里比日期用的就是这种数，这里保持一致。"""
    return day.year * 10000 + day.month * 100 + day.day


@dataclass(frozen=True)
class AiEligibility:
    status: str = ""
    upgraded_on: date | None = None
    """升级 AI 的那一天（这一天本身就算）。None 表示没填日期。"""

    def gate_number(self) -> int:
        """交易日（写成 20260910 这种数）**大于等于**它才算佣金。和 Base 公式一一对应。"""
        if self.status == schema.AI_STATUS_ALREADY:
            return GATE_ALWAYS
        if self.upgraded_on is not None:
            return day_number(self.upgraded_on)
        if self.status in (schema.AI_STATUS_NOT, schema.AI_STATUS_UPGRADED):
            return GATE_NEVER
        return GATE_ALWAYS

    def counts(self, day: date) -> bool:
        """这个客户在 ``day`` 这一天的交易算不算佣金。"""
        if day < RULE_START_DAY:
            return True
        return day_number(day) >= self.gate_number()


def eligibility_of(fields: dict[str, Any], *, tz: tzinfo) -> AiEligibility:
    """客户表的一行 -> 这个客户的 AI 资格。两列都不存在（老表没跑 sync）时照旧算。"""
    from .commission import day_of  # 避免循环导入：commission 也用这个模块

    return AiEligibility(
        status=extract_text(fields.get(schema.CLIENT_AI_STATUS)).strip(),
        upgraded_on=day_of(fields.get(schema.CLIENT_AI_DATE), tz=tz),
    )
