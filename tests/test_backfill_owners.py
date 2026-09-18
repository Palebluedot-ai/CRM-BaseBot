"""存量渠道/客户的归属回填。

这一步是「把机器人放进生产」时最容易造出静默错误的地方：归属决定谁看得到什么，
也决定佣金算到谁头上。所以测试盯的是三条：

1. 姓名对不上名册时**不猜**（列出来给人看）；
2. 已经有归属的行不碰（重复跑不会把人工调整改掉）；
3. 没加 --apply 时一个写都不发。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from crm_basebot.domain import schema

from .conftest import TBL_CLIENT, TBL_REFERRAL, TBL_SALES

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load_module(name: str):
    """scripts/ 不是包，按路径加载。"""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


backfill = _load_module("backfill_owners")

JAMES = "ou_0000000000000000000000000000ja"
JACKIE = "ou_0000000000000000000000000000jc"


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        table_referral=TBL_REFERRAL,
        table_client=TBL_CLIENT,
        table_sales=TBL_SALES,
    )


def _args(*extra: str):
    return backfill.build_parser().parse_args(list(extra))


def _seed_sales(fake_bitable, name: str, open_id: str) -> None:
    fake_bitable.table(TBL_SALES).add_existing(
        {schema.SALES_NAME: name, schema.SALES_OPEN_ID: open_id}
    )


def _seed_channel(fake_bitable, no: str, sales_name: str, open_id: str = "") -> str:
    return fake_bitable.table(TBL_REFERRAL).add_existing(
        {
            schema.REFERRAL_NO: no,
            schema.REFERRAL_SALES_NAME: sales_name,
            schema.REFERRAL_OWNER_OPEN_ID: open_id,
        }
    )


def _seed_client(fake_bitable, uid: str, sales_name: str, open_id: str = "") -> str:
    return fake_bitable.table(TBL_CLIENT).add_existing(
        {
            schema.CLIENT_UID: uid,
            schema.CLIENT_SALES_NAME: sales_name,
            schema.CLIENT_OWNER_OPEN_ID: open_id,
        }
    )


# ---------- 计划本身（纯函数） ----------


def test_姓名大小写和空格不同也算同一个人():
    """模板里同时有 ``James Yang`` 和 ``James YANG``，指的是同一个人。"""
    rows = [("rec1", "R001", "James Yang", "")]
    plan = backfill.build_plan(rows, {"james yang": JAMES})
    assert [change.open_id for change in plan.changes] == [JAMES]


def test_对不上名册的姓名不猜(fake_bitable):
    rows = [("rec1", "R002", "Somebody Else", "")]
    plan = backfill.build_plan(rows, {"james yang": JAMES})
    assert plan.changes == []
    assert plan.unmatched["Somebody Else"] == 1


def test_负责销售为空的行不动():
    rows = [("rec1", "R003", "", "")]
    plan = backfill.build_plan(rows, {"james yang": JAMES})
    assert plan.changes == []
    assert plan.no_sales_name == 1


def test_已经有归属的行跳过():
    """重复跑不能把人工调整过的归属改回去。"""
    rows = [("rec1", "R004", "James YANG", JACKIE)]
    plan = backfill.build_plan(rows, {"james yang": JAMES})
    assert plan.changes == []
    assert plan.already_owned == 1


def test_名册里没填OpenID的人进不了映射():
    roster = [("recsales", "James YANG", ""), ("recsales2", "Jackie Cao", JACKIE)]
    mapping = backfill.open_id_map(roster)
    assert mapping == {"jackie cao": JACKIE}


# ---------- 整条路 ----------


def test_预演不写Base(fake_bitable, capsys):
    _seed_sales(fake_bitable, "James YANG", JAMES)
    _seed_channel(fake_bitable, "R001", "James YANG")

    assert backfill.run(_args(), _settings(), fake_bitable) == 0

    assert fake_bitable.updates == []
    assert "Base 没有被改动" in capsys.readouterr().out


def test_apply时两列一起写(fake_bitable):
    _seed_sales(fake_bitable, "James YANG", JAMES)
    record_id = _seed_channel(fake_bitable, "R001", "James YANG")
    _seed_client(fake_bitable, "577809207768677761", "James Yang")

    assert backfill.run(_args("--apply"), _settings(), fake_bitable) == 0

    channel_fields = next(
        fields
        for table_id, record, fields in fake_bitable.updates
        if table_id == TBL_REFERRAL and record == record_id
    )
    # 人员字段用 open_id，机器人过滤读的是文本列，两列必须一致
    assert channel_fields[schema.REFERRAL_OWNER] == [{"id": JAMES}]
    assert channel_fields[schema.REFERRAL_OWNER_OPEN_ID] == JAMES


def test_only只处理指定的人(fake_bitable):
    _seed_sales(fake_bitable, "James YANG", JAMES)
    _seed_sales(fake_bitable, "Jackie Cao", JACKIE)
    james = _seed_channel(fake_bitable, "R001", "James YANG")
    jackie = _seed_channel(fake_bitable, "R002", "Jackie Cao")

    assert backfill.run(_args("--only", "james yang", "--apply"), _settings(), fake_bitable) == 0

    touched = {record for _table, record, _fields in fake_bitable.updates}
    assert touched == {james}
    assert jackie not in touched


def test_名册空时什么都不写(fake_bitable, capsys):
    _seed_channel(fake_bitable, "R001", "James YANG")

    assert backfill.run(_args("--apply"), _settings(), fake_bitable) == 0

    assert fake_bitable.updates == []
    assert "一个都没有" in capsys.readouterr().out
