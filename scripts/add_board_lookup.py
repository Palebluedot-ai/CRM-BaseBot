#!/usr/bin/env python
"""在 Daily Revenue Board 加一列「渠道（自动查找）」—— 由 Base 自己按 UID 查渠道。

    uv run python scripts/add_board_lookup.py            # 预演：只说会做什么
    uv run python scripts/add_board_lookup.py --apply    # 真加，加完自己验证

## 为什么（2026-09-29 超哥要的）

看板上原来的「渠道编号」「渠道名称」「本笔佣金」靠「客户」关联，关联由代码挂（机器人
登记当下、每天导入之后）。代码停了（mac mini 关机、机器人挂了），新登记客户的行就一直
空着。这一列不靠关联：公式在 ``Referred Client`` 里找「客户UID = 这一行的用户ID」的
客户，把他的「渠道」（``R094 HongKong Dimi …``）带过来。**Base 自己算**，在 Base 里
手动加的客户也是马上显示。

它**只是显示**：算钱（本笔佣金、月结、invoice）照旧走关联，这一列谁都不读。每天的
导入只写自己那 18 列，也不碰它。

## 为什么要自己验证

多维表格的公式建的时候不校验 —— 写法不对照样建得出来，只是永远是空的（2026-09-18
实测 VLOOKUP / LOOKUP 就是这样）。跨表 FILTER 的写法飞书没有给接口侧的保证，所以这里
几种写法挨个试：建好等它算完，拿**已经挂上关联的行**比对（查出来的渠道要和关联反查的
渠道编号一致）。第一种全对的留下；全都不行，就把这次加的列删掉，恢复原样。

客户表那一列「渠道」由 ``sync_base.py`` 维护（``schema.CLIENT_CHANNEL_TEXT``）；
这里缺的话也顺手建上。
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lark_oapi.api.bitable.v1 import DeleteAppTableFieldRequest  # noqa: E402

from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.lark.bitable import FIELD_TYPE_FORMULA, BitableClient  # noqa: E402
from crm_basebot.lark.client import get_client  # noqa: E402
from crm_basebot.lark.values import extract_text  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402
from crm_basebot.structure import StructureError, create_field, update_formula  # noqa: E402

LOOKUP_FIELD = "渠道（自动查找）"

_CLIENT = schema.TABLE_CLIENT_NAME
_UID = schema.CLIENT_UID
_CHANNEL = schema.CLIENT_CHANNEL_TEXT
_BOARD_UID = schema.BOARD_CLIENT_UID

# 挨个试的写法。第一种是飞书文档里「跨表引用」的链式写法，后两种是它的变体。
CANDIDATES: tuple[str, ...] = (
    f"[{_CLIENT}].FILTER(CurrentValue.[{_UID}]=[{_BOARD_UID}]).[{_CHANNEL}]",
    f"FILTER([{_CLIENT}].[{_CHANNEL}], [{_CLIENT}].[{_UID}]=[{_BOARD_UID}])",
    f"[{_CLIENT}].FILTER(CurrentValue.[{_UID}]=[{_BOARD_UID}]).[{_CHANNEL}].LISTCOMBINE()",
)

SAMPLE_ROWS = 3000
WAIT_SECONDS = 90
POLL_SECONDS = 10


@dataclass
class Check:
    linked: int = 0  # 挂了关联、有渠道编号的行
    ok: int = 0  # 其中查找列里有同一个渠道编号
    wrong: int = 0  # 查找列有值但编号不一样
    empty: int = 0  # 查找列是空的
    extra: int = 0  # 没挂关联、但查找列已经查到渠道的行（这一列的价值所在）

    @property
    def passed(self) -> bool:
        return self.linked > 0 and self.ok == self.linked


def evaluate(rows: list[tuple[str, str]]) -> Check:
    """rows = [(关联反查的渠道编号, 查找列的文字)]。纯函数，方便测。"""
    check = Check()
    for code, found in rows:
        if not code:
            if found.strip():
                check.extra += 1
            continue
        check.linked += 1
        if not found.strip():
            check.empty += 1
        elif code in {token.strip(",，;；") for token in found.split()}:
            check.ok += 1
        else:
            check.wrong += 1
    return check


def _read(bitable: BitableClient, table_id: str) -> list[tuple[str, str]]:
    records = bitable.iter_records(table_id, field_names=[schema.BOARD_REFERRAL_NO, LOOKUP_FIELD])
    return [
        (
            extract_text(r.fields.get(schema.BOARD_REFERRAL_NO)),
            extract_text(r.fields.get(LOOKUP_FIELD)),
        )
        for r in islice(records, SAMPLE_ROWS)
    ]


def _wait_and_check(bitable: BitableClient, table_id: str) -> Check:
    """公式是异步算的：隔一会儿读一次，全对就提前返回。"""
    deadline = time.monotonic() + WAIT_SECONDS
    check = Check()
    while True:
        check = evaluate(_read(bitable, table_id))
        if check.passed or time.monotonic() >= deadline:
            return check
        print(f"    算到 {check.ok}/{check.linked} 行，等 {POLL_SECONDS} 秒再看……")
        time.sleep(POLL_SECONDS)


def _field_id(bitable: BitableClient, table_id: str, name: str) -> str | None:
    return next((f.field_id for f in bitable.list_fields(table_id) if f.name == name), None)


def _delete_field(client, app_token: str, table_id: str, field_id: str) -> None:
    request = (
        DeleteAppTableFieldRequest.builder()
        .app_token(app_token)
        .table_id(table_id)
        .field_id(field_id)
        .build()
    )
    response = client.bitable.v1.app_table_field.delete(request)
    if not response.success():
        print(
            f"  ! 删不掉刚加的列：{response.code} {response.msg}。"
            f"可以在 Base 里手动删「{LOOKUP_FIELD}」。"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="看板加一列 Base 自己算的渠道查找")
    parser.add_argument("--apply", action="store_true", help="真加；不加则只预演")
    args = parser.parse_args(argv)

    settings = load_settings()
    require_settings(settings, "LARK_BASE_APP_TOKEN", "TABLE_CLIENT", "TABLE_DAILY_BOARD")
    bitable = BitableClient(settings.base_app_token)
    token = settings.base_app_token
    board, clients = settings.table_daily_board, settings.table_client

    need_client_col = _field_id(bitable, clients, _CHANNEL) is None
    existing = _field_id(bitable, board, LOOKUP_FIELD)

    if not args.apply:
        if need_client_col:
            print(f"会在「{_CLIENT}」加一列公式「{_CHANNEL}」（渠道编号 + 名称）。")
        if existing:
            print(f"「{LOOKUP_FIELD}」已经在了，会重新验证一次。")
        else:
            print(
                f"会在「{schema.TABLE_DAILY_BOARD_NAME}」加一列「{LOOKUP_FIELD}」，加完自己验证；"
            )
            print("验证不过就删掉，恢复原样。")
        print("（预演，没写。确认后加 --apply）")
        return 0

    client = get_client()
    if need_client_col:
        print(f"· 在「{_CLIENT}」加「{_CHANNEL}」……")
        create_field(
            client,
            token,
            clients,
            _CHANNEL,
            FIELD_TYPE_FORMULA,
            formula=schema.CLIENT_DERIVED_FORMULAS[_CHANNEL],
        )
        time.sleep(POLL_SECONDS)  # 让客户表那一列先算出来

    created_now = existing is None
    field_id = existing
    for index, expression in enumerate(CANDIDATES, start=1):
        print(f"· 试第 {index} 种写法……")
        try:
            if field_id is None:
                create_field(
                    client,
                    token,
                    board,
                    LOOKUP_FIELD,
                    FIELD_TYPE_FORMULA,
                    formula=(expression, schema.FORMULA_DATA_TYPE_TEXT),
                )
                field_id = _field_id(bitable, board, LOOKUP_FIELD)
            else:
                update_formula(
                    client,
                    token,
                    board,
                    field_id,
                    LOOKUP_FIELD,
                    (expression, schema.FORMULA_DATA_TYPE_TEXT),
                )
        except StructureError as exc:
            print(f"    平台不收这种写法：{exc}")
            continue

        check = _wait_and_check(bitable, board)
        print(
            f"    挂了关联的 {check.linked} 行：对 {check.ok}，空 {check.empty}，"
            f"不一致 {check.wrong}；还没挂关联但已查到渠道 {check.extra} 行"
        )
        if check.passed:
            print(f"\n✅ 好了。「{schema.TABLE_DAILY_BOARD_NAME}」最右边多了「{LOOKUP_FIELD}」。")
            print("   它由 Base 自己按 UID 查，机器人和每天导入停了也照样显示。")
            return 0

    print("\n✗ 几种写法都不行：飞书的公式接口做不了这种跨表查找。")
    if created_now and field_id:
        _delete_field(client, token, board, field_id)
        print(f"  已经把这次加的「{LOOKUP_FIELD}」删掉，看板恢复原样。")
    print("  什么都没坏。退路是在 Base 界面里手动加一个「查找引用」字段（截图发给 Claude）。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
