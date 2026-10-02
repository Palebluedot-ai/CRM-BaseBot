"""结算表的一行是「进行中」还是「已结算」。两张结算表（交易、ECAS）共用。

每天导入后，当月（和月初还没结的上个月）的数写成「进行中」，每次覆盖；每月 1 号 16:30
月结把上个月写成「已结算」，之后谁都不再改它（2026-10-02 起，以前是 3 号才结、中间不写）。
状态那一列空着的行是那之前写的，都是已结算。

**读结算表算钱的地方（invoice、存档、对账）只认已结算的行**：进行中的数还会变。
"""

from __future__ import annotations

from typing import Any

from ..lark.values import extract_text
from . import schema


def is_live(fields: dict[str, Any]) -> bool:
    return extract_text(fields.get(schema.COMM_STATUS)).strip() == schema.SETTLE_LIVE
