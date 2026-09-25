"""审计日志。

每一次写入业务表之前，先在这里留一条记录。审计表只增不改 —— 机器人不提供任何
修改或删除审计记录的入口。

审计先行的原因：如果业务写入成功而审计失败，就出现了没有记录的变更；反过来
（审计成功而业务失败）留下一条「尝试过」的记录，是可以接受也更安全的。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..lark.bitable import BitableClient
from . import schema

logger = logging.getLogger(__name__)

ACTION_CREATE_REFERRAL = "登记渠道"
ACTION_CREATE_CLIENT = "登记客户"
ACTION_COMPUTE_COMMISSION = "计算佣金"
# ECAS 返佣是和交易佣金分开的第二套账，审计里也分开记，别让人把两次结算看成同一件事。
ACTION_COMPUTE_ECAS = "计算ECAS返佣"
ACTION_UPDATE_CLIENT_AI = "更新客户AI状态"


class AuditLog:
    def __init__(self, bitable: BitableClient, table_id: str) -> None:
        self._bitable = bitable
        self._table_id = table_id

    def record(
        self,
        *,
        actor_open_id: str,
        actor_name: str,
        action: str,
        target_table: str,
        target_record: str = "",
        detail: dict[str, Any] | None = None,
    ) -> str:
        fields = {
            schema.AUDIT_AT: int(time.time() * 1000),
            schema.AUDIT_ACTOR_OPEN_ID: actor_open_id,
            schema.AUDIT_ACTOR_NAME: actor_name,
            schema.AUDIT_ACTION: action,
            schema.AUDIT_TARGET_TABLE: target_table,
            schema.AUDIT_TARGET_RECORD: target_record,
            schema.AUDIT_DETAIL: json.dumps(detail or {}, ensure_ascii=False),
        }
        # 审计表没有自动编号之类要读回来的字段，所以不回读 —— 这一步在卡片回调的
        # 3 秒预算里，每省一个往返都是实的。
        created = self._bitable.create_record(self._table_id, fields, reread=False)
        return created.record_id
