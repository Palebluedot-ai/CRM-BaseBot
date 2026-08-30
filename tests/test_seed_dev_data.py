"""种子数据的形状约束。

这些测试不碰任何 API，只盯纯逻辑：造出来的数据必须满足几条「一旦破了，用它跑出来的
对账结果就没有意义」的性质。最要紧的是那两对只差最后一位的客户UID —— 它们是整套种子
数据存在的理由，被谁顺手改成「好看一点」的值，探针就失效了。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from crm_basebot.domain import schema
from crm_basebot.domain.commission import period_of


def _load_seed_module():
    """scripts/ 不是包，按路径加载。

    必须先塞进 sys.modules 再 exec —— @dataclass 会回头查 sys.modules[cls.__module__]，
    查不到就在装饰阶段炸掉。
    """
    path = Path(__file__).resolve().parent.parent / "scripts" / "seed_dev_data.py"
    spec = importlib.util.spec_from_file_location("seed_dev_data", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


seed = _load_seed_module()


# ---------- 客户UID 的形态 ----------


def test_uids_are_strings_of_realistic_length():
    for client in seed.build_clients():
        assert isinstance(client.uid, str)
        assert client.uid.isdigit()
        assert 18 <= len(client.uid) <= 19, f"{client.uid} 不是真实形态的长度"


def test_has_uid_pairs_differing_only_in_last_digit_across_referrals():
    """探针：至少一对只差最后一位、且分属不同渠道的 UID。

    UID 一旦被数值化，这一对会塌成同一个值，佣金立刻算到隔壁渠道头上 —— 而且是在
    对账输出里看得见的错，不是静默的「匹配不上」。
    """
    by_uid = {c.uid: c for c in seed.build_clients()}

    pairs = [
        (a, b)
        for a in by_uid
        for b in by_uid
        if a < b and a[:-1] == b[:-1] and by_uid[a].referral_name != by_uid[b].referral_name
    ]

    assert pairs, "没有找到只差最后一位、且跨渠道的 UID 对"


def test_uids_cover_both_18_and_19_digits():
    lengths = {len(c.uid) for c in seed.build_clients()}
    assert {18, 19} <= lengths


def test_no_uid_looks_excel_truncated():
    """种子 UID 不能长得像被 Excel 抹过低位的样子。

    lark/values.py 把「长度超过 15 位、且尾部有 (长度-15) 个连续 0」判为疑似截断。
    种子数据要是撞上这个形态，inspect_base 和 reconcile 每次都要报一次假警，
    真出事的时候反而没人信了。
    """
    all_uids = [c.uid for c in seed.build_clients()] + list(seed.UNMAPPED_CLIENTS)

    for uid in all_uids:
        excess = len(uid) - 15
        assert not uid.endswith("0" * excess), f"{uid} 看起来像被 Excel 截断过"


def test_unmapped_clients_are_not_registered():
    """未登记归属的客户：出现在交易里，但不在客户表里。用来验证 reconcile 的告警。"""
    registered = {c.uid for c in seed.build_clients()}
    traded = {t.uid for t in seed.build_transactions()}

    assert seed.UNMAPPED_CLIENTS, "至少要有一个未登记归属的客户"
    for uid in seed.UNMAPPED_CLIENTS:
        assert uid not in registered
        assert uid in traded


# ---------- 渠道 ----------


def test_referral_rates_differ_and_include_a_fractional_one():
    rates = [r.rate_percent for r in seed.build_referrals()]

    assert len(set(rates)) >= 3, "分佣比例至少要有三个不同的值"
    assert any(rate != int(rate) for rate in rates), "至少要有一个带小数的比例"


def test_every_client_points_at_an_existing_referral():
    referral_names = {r.name for r in seed.build_referrals()}
    for client in seed.build_clients():
        assert client.referral_name in referral_names


# ---------- 交易明细 ----------


def test_transactions_span_at_least_three_months():
    periods = {t.period for t in seed.build_transactions()}
    assert len(periods) >= 3, f"只跨了 {sorted(periods)}，验证不了按月汇总"


def test_order_time_survives_the_timestamp_round_trip():
    """日期转成毫秒时间戳后，reconcile 归的月份必须还是原来那个月。"""
    for txn in seed.build_transactions():
        assert period_of(seed.to_timestamp_ms(txn.order_date)) == txn.period


def test_pnl_is_realistic_and_includes_losses():
    pnls = [t.pnl for t in seed.build_transactions()]

    assert any(p < 0 for p in pnls), "没有亏损单，验证不了负 Pnl 的处理"
    assert all(100 <= abs(p) <= 10000 for p in pnls), "Pnl 量级不像真实盘口"
    assert all(round(p, 2) != round(p) for p in pnls), "Pnl 应该带小数，整数会掩盖累加误差"


def test_one_referral_month_totals_negative():
    """必须有「某个渠道某个月合计为负」的情形。

    这是负 Pnl 里真正含糊的那一档：单笔亏损被同月的盈利盖过去了不算数，整月为负才会
    让 CommissionRow.payable 算出一个负的应付佣金，逼业务规则被明确下来。
    """
    referral_of = {c.uid: c.referral_name for c in seed.build_clients()}

    totals: dict[tuple[str, str], float] = {}
    for txn in seed.build_transactions():
        referral_name = referral_of.get(txn.uid)
        if referral_name is None:
            continue
        key = (txn.period, referral_name)
        totals[key] = round(totals.get(key, 0.0) + txn.pnl, 2)

    assert any(total < 0 for total in totals.values()), (
        f"没有整月为负的渠道，负 Pnl 的处理不会被暴露出来：{totals}"
    )


def test_transaction_client_names_match_the_client_table():
    known = {c.uid: c.name for c in seed.build_clients()} | dict(seed.UNMAPPED_CLIENTS)
    for txn in seed.build_transactions():
        assert txn.client_name == known[txn.uid]


def test_transaction_natural_key_separates_same_day_same_client_rows():
    """幂等靠这个键。同一天同一个客户可能有多笔，键必须分得开。"""
    keys = [
        seed.transaction_key(seed.to_timestamp_ms(t.order_date), t.uid, t.pnl)
        for t in seed.build_transactions()
    ]
    assert len(keys) == len(set(keys)), "交易的自然键有重复，重复跑会漏写或写重"


# ---------- 销售名册 ----------


def test_sales_includes_the_operator_as_admin():
    rows = seed.build_sales("ou_abc123", "陈超")
    admin = rows[0]

    assert admin.open_id == "ou_abc123"
    assert admin.role == schema.ROLE_ADMIN
    assert admin.status == schema.SALES_STATUS_ACTIVE
    assert "陈超" in admin.name


def test_sales_without_open_id_has_no_real_account():
    rows = seed.build_sales(None, "开发管理员")
    assert all(not r.open_id.startswith("ou_") for r in rows), "占位账号不该长得像真 open_id"


def test_sales_covers_a_disabled_person():
    rows = seed.build_sales("ou_abc123", "陈超")
    assert any(r.status == schema.SALES_STATUS_DISABLED for r in rows)


# ---------- 种子标记（--reset 的删除面全靠它） ----------


def test_every_seeded_row_carries_the_marker():
    marked = (
        [r.name for r in seed.build_referrals()]
        + [c.name for c in seed.build_clients()]
        + list(seed.UNMAPPED_CLIENTS.values())
        + [t.client_name for t in seed.build_transactions()]
        + [s.name for s in seed.build_sales("ou_abc123", "陈超")]
    )
    for value in marked:
        assert seed.is_seed_value(value), f"{value} 没有 {seed.SEED_PREFIX} 标记，--reset 删不掉它"


def test_is_seed_value_rejects_real_looking_data():
    assert not seed.is_seed_value("PLUTO STUDIO LIMITED")
    assert not seed.is_seed_value(None)
    assert not seed.is_seed_value("")
    assert seed.is_seed_value([{"type": "text", "text": f"{seed.SEED_PREFIX}北极星资本"}])


def test_marker_fields_are_not_used_by_the_commission_calculation():
    """标记只能落在对账不读的字段上，否则等于往计算里掺假。"""
    used_by_reconcile = set(schema.TXN_REQUIRED_FIELDS)
    assert seed.SEED_MARKER_FIELD[schema.TABLE_TRANSACTION_NAME] not in used_by_reconcile


# ---------- 模拟交易明细表的字段 ----------


def test_mock_transaction_table_has_every_field_of_the_real_one():
    expected = {
        schema.TXN_ORDER_TIME,
        schema.TXN_ENTITY,
        schema.TXN_CLIENT_NAME,
        schema.TXN_CLIENT_UID,
        schema.TXN_QUANTITY,
        schema.TXN_PRICE,
        schema.TXN_FEE,
        schema.TXN_FEE_CURRENCY,
        schema.TXN_PNL,
    }
    assert set(seed.MOCK_TRANSACTION_FIELDS) == expected


def test_mock_uid_field_is_text_not_number():
    from crm_basebot.lark.bitable import FIELD_TYPE_NUMBER, FIELD_TYPE_TEXT

    assert seed.MOCK_TRANSACTION_FIELDS[schema.TXN_CLIENT_UID] == FIELD_TYPE_TEXT
    assert seed.MOCK_TRANSACTION_FIELDS[schema.TXN_CLIENT_UID] != FIELD_TYPE_NUMBER


def test_mock_table_satisfies_the_reconcile_schema_check():
    """assert_fields_present 会在算钱前校验这三个字段，模拟表必须过得了。"""
    from crm_basebot.lark.bitable import FieldInfo, assert_fields_present

    fields = [
        FieldInfo(
            field_id=f"fld{index}",
            name=name,
            type=type_code,
            ui_type="",
            is_primary=False,
        )
        for index, (name, type_code) in enumerate(seed.MOCK_TRANSACTION_FIELDS.items())
    ]
    assert_fields_present(fields, schema.TXN_REQUIRED_FIELDS, table_label="模拟交易明细表")


# ---------- CLI 的失败路径 ----------


def test_help_exits_cleanly():
    with pytest.raises(SystemExit) as excinfo:
        seed.main(["--help"])
    assert excinfo.value.code == 0


def test_missing_open_id_is_rejected_with_a_hint(capsys):
    assert seed.main([]) == 1
    assert "open_id" in capsys.readouterr().err


def test_non_open_id_identifier_is_rejected(capsys):
    assert seed.main(["--open-id", "on_union_id_looking_thing"]) == 1
    assert "ou_" in capsys.readouterr().err


def test_missing_credentials_reports_plainly_instead_of_a_traceback(monkeypatch, capsys):
    def boom():
        raise RuntimeError("LARK_APP_ID field required")

    monkeypatch.setattr(seed, "get_settings", boom)

    assert seed.main(["--open-id", "ou_abc123"]) == 1
    assert "docs/LARK_APP_SETUP.md" in capsys.readouterr().err
