"""按 UID 复算佣金的对账脚本。

这个脚本存在的唯一理由是：公式挂错客户时**不报错**，只是算出一个看着正常的数。
所以测试盯的是「差异能不能被发现」—— 每一条错法都必须被点出来，且退出码非 0。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from crm_basebot.domain import schema

from .conftest import TBL_BOARD, TBL_CLIENT, TBL_REFERRAL


def _load_module():
    """scripts/ 不是包，按路径加载。"""
    path = Path(__file__).resolve().parent.parent / "scripts" / "verify_commission.py"
    spec = importlib.util.spec_from_file_location("verify_commission", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


verifier = _load_module()

UID_A = "577809207768677761"
UID_B = "2176760078253834240"


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        table_daily_board=TBL_BOARD,
        table_client=TBL_CLIENT,
        table_referral=TBL_REFERRAL,
    )


def _args(*extra: str):
    return verifier.build_parser().parse_args(list(extra))


def _channel(fake_bitable, no: str, name: str, rate: float) -> str:
    return fake_bitable.table(TBL_REFERRAL).add_existing(
        {
            schema.REFERRAL_NO: no,
            schema.REFERRAL_NAME: name,
            schema.REFERRAL_RATE: rate,
        }
    )


def _client(fake_bitable, uid: str, name: str, channel_id: str) -> str:
    return fake_bitable.table(TBL_CLIENT).add_existing(
        {
            schema.CLIENT_UID: uid,
            schema.CLIENT_NAME: name,
            schema.CLIENT_REFERRAL_LINK: {"link_record_ids": [channel_id]},
        }
    )


def _board_row(
    fake_bitable,
    *,
    uid: str,
    revenue: float,
    rate: float | None,
    amount: float | None,
    link: str | None,
    month: str = "2026-03",
) -> str:
    fields = {
        schema.BOARD_CLIENT_UID: uid,
        schema.BOARD_TOTAL_REVENUE: revenue,
        schema.BOARD_MONTH: month,
        schema.BOARD_CLIENT_LINK: {"link_record_ids": [link] if link else None},
    }
    if rate is not None:
        fields[schema.BOARD_CLIENT_RATE] = rate
    if amount is not None:
        fields[schema.BOARD_ROW_COMMISSION] = amount
    return fake_bitable.table(TBL_BOARD).add_existing(fields)


def test_一致的链路报零差异(fake_bitable, capsys):
    channel = _channel(fake_bitable, "R001", "ABC Capital", 20.0)
    client = _client(fake_bitable, UID_A, "PLUTO STUDIO", channel)
    _board_row(fake_bitable, uid=UID_A, revenue=1000.0, rate=20.0, amount=200.0, link=client)

    assert verifier.run(_args(), _settings(), fake_bitable) == 0
    assert "逐行一致" in capsys.readouterr().out


def test_挂错客户会被发现(fake_bitable, capsys):
    channel = _channel(fake_bitable, "R001", "ABC Capital", 20.0)
    client = _client(fake_bitable, UID_A, "PLUTO STUDIO", channel)
    other = _client(fake_bitable, UID_B, "SOMEONE ELSE", channel)
    _board_row(fake_bitable, uid=UID_A, revenue=1000.0, rate=20.0, amount=200.0, link=other)

    assert verifier.run(_args(), _settings(), fake_bitable) == 1
    assert "挂错客户" in capsys.readouterr().out


def test_客户表里有却没挂关联会被发现(fake_bitable, capsys):
    channel = _channel(fake_bitable, "R001", "ABC Capital", 20.0)
    _client(fake_bitable, UID_A, "PLUTO STUDIO", channel)
    _board_row(fake_bitable, uid=UID_A, revenue=1000.0, rate=None, amount=None, link=None)

    assert verifier.run(_args(), _settings(), fake_bitable) == 1
    assert "漏挂" in capsys.readouterr().out


def test_客户表UID重复会被发现(fake_bitable, capsys):
    channel = _channel(fake_bitable, "R001", "ABC Capital", 20.0)
    client = _client(fake_bitable, UID_A, "PLUTO STUDIO", channel)
    _client(fake_bitable, UID_A, "PLUTO STUDIO 重复行", channel)
    _board_row(fake_bitable, uid=UID_A, revenue=1000.0, rate=20.0, amount=200.0, link=client)

    assert verifier.run(_args(), _settings(), fake_bitable) == 1
    assert "UID 重复" in capsys.readouterr().out


def test_比例不符会被发现(fake_bitable, capsys):
    channel = _channel(fake_bitable, "R001", "ABC Capital", 20.0)
    client = _client(fake_bitable, UID_A, "PLUTO STUDIO", channel)
    # 看板公式说 30%，但渠道表写的是 20%
    _board_row(fake_bitable, uid=UID_A, revenue=1000.0, rate=30.0, amount=300.0, link=client)

    assert verifier.run(_args(), _settings(), fake_bitable) == 1
    assert "比例不符" in capsys.readouterr().out


def test_金额不符会被发现(fake_bitable, capsys):
    channel = _channel(fake_bitable, "R001", "ABC Capital", 20.0)
    client = _client(fake_bitable, UID_A, "PLUTO STUDIO", channel)
    # 1000 × 20% 应该是 200，看板给的是 250
    _board_row(fake_bitable, uid=UID_A, revenue=1000.0, rate=20.0, amount=250.0, link=client)

    assert verifier.run(_args(), _settings(), fake_bitable) == 1
    assert "金额不符" in capsys.readouterr().out


def test_渠道没有比例时不算差异(fake_bitable):
    """渠道还没填比例 → 佣金列本来就该是空的，这不是「算错」。"""
    channel = _channel(fake_bitable, "R001", "ABC Capital", 0.0)
    client = _client(fake_bitable, UID_A, "PLUTO STUDIO", channel)
    _board_row(fake_bitable, uid=UID_A, revenue=1000.0, rate=0.0, amount=0.0, link=client)

    assert verifier.run(_args(), _settings(), fake_bitable) == 0


def test_month只核对指定月份(fake_bitable, capsys):
    channel = _channel(fake_bitable, "R001", "ABC Capital", 20.0)
    client = _client(fake_bitable, UID_A, "PLUTO STUDIO", channel)
    _board_row(fake_bitable, uid=UID_A, revenue=1000.0, rate=20.0, amount=200.0, link=client)
    # 这笔挂在 4 月，且金额是错的；只核对 3 月时不该被点出来
    _board_row(
        fake_bitable,
        uid=UID_A,
        revenue=1000.0,
        rate=20.0,
        amount=999.0,
        link=client,
        month="2026-04",
    )

    assert verifier.run(_args("--month", "2026-03"), _settings(), fake_bitable) == 0
    assert "核对看板 1 行" in capsys.readouterr().out
