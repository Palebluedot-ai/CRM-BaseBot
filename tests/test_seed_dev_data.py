"""种子数据的形状约束。

这些测试不碰任何 API，只盯纯逻辑：造出来的数据必须满足几条「一旦破了，用它跑出来的
对账结果就没有意义」的性质。最要紧的是那两对只差最后一位的客户UID —— 它们是整套种子
数据存在的理由，被谁顺手改成「好看一点」的值，探针就失效了。
"""

from __future__ import annotations

import importlib.util
import sys
from decimal import ROUND_HALF_UP, Context
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from crm_basebot.config import Settings
from crm_basebot.domain import schema
from crm_basebot.domain.commission import period_of
from crm_basebot.lark.values import assess_uid_health, looks_excel_truncated


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

# 对账归月用的业务时区。种子的时间戳是 UTC 零点，也就是这个时区的早上 8 点，
# 而所有日期都避开了月初月末，所以归月不会被时区偏移挪到隔壁月份。
BUSINESS_TZ = ZoneInfo(Settings.model_fields["business_timezone"].default)


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
    """默认种子 UID 不能长得像被 Excel 抹过低位的样子。

    lark/values.py 把「长度超过 15 位、且尾部有 (长度-15) 个连续 0」判为疑似截断。
    种子数据要是撞上这个形态，inspect_base 和 reconcile 每次都要报一次假警，
    真出事的时候反而没人信了。

    唯一的例外是 --with-damaged-uid 那批，它们存在的意义正是触发告警。
    """
    all_uids = (
        [c.uid for c in seed.build_clients()]
        + list(seed.UNMAPPED_CLIENTS)
        + [t.uid for t in seed.build_board_rows()]
    )

    for uid in all_uids:
        assert not looks_excel_truncated(uid), f"{uid} 看起来像被 Excel 截断过"


def test_unmapped_clients_are_not_registered():
    """未登记归属的客户：出现在交易里，但不在客户表里。用来验证 reconcile 的告警。"""
    registered = {c.uid for c in seed.build_clients()}
    traded = {t.uid for t in seed.build_board_rows()}

    assert seed.UNMAPPED_CLIENTS, "至少要有一个未登记归属的客户"
    for uid in seed.UNMAPPED_CLIENTS:
        assert uid not in registered
        assert uid in traded


# ---------- 渠道 ----------


def test_referral_rates_differ_and_include_a_fractional_one():
    rates = [r.rate_percent for r in seed.build_referrals()]

    assert len(set(rates)) >= 3, "分佣比例至少要有三个不同的值"
    assert any(rate != int(rate) for rate in rates), "至少要有一个带小数的比例"


def test_all_referrals_are_active():
    """渠道只有「生效」和「停用」两种状态，而停掉的渠道根本不在数据里。

    佣金计算不看状态（没有「待审核」，登记即生效，2026-09-04 定的），所以种子里
    放一个非生效的渠道，它会照样算出佣金，每次对账都让人怀疑是 bug —— 而那个
    疑惑纯粹是种子数据自己造出来的。
    """
    statuses = {r.status for r in seed.build_referrals()}
    assert statuses == {schema.STATUS_ACTIVE}, f"渠道状态必须全是生效，实际有 {statuses}"


def test_every_client_points_at_an_existing_referral():
    referral_names = {r.name for r in seed.build_referrals()}
    for client in seed.build_clients():
        assert client.referral_name in referral_names


# ---------- 交易明细 ----------


def test_board_rows_span_at_least_three_months():
    periods = {t.period for t in seed.build_board_rows()}
    assert len(periods) >= 3, f"只跨了 {sorted(periods)}，验证不了按月汇总"


def test_order_time_survives_the_timestamp_round_trip():
    """日期转成毫秒时间戳后，reconcile 归的月份必须还是原来那个月。"""
    for txn in seed.build_board_rows():
        assert (
            period_of(seed.to_timestamp_ms(txn.order_date, tz=BUSINESS_TZ), tz=BUSINESS_TZ)
            == txn.period
        )


def test_seed_dates_are_business_timezone_midnight():
    """和导入脚本同一约定：交易日期写成业务时区那天的零点，界面里看到的就是那一天 0:00。"""
    from datetime import datetime

    ms = seed.to_timestamp_ms("2026-01-06", tz=BUSINESS_TZ)
    local = datetime.fromtimestamp(ms / 1000, tz=BUSINESS_TZ)
    assert (local.year, local.month, local.day, local.hour, local.minute) == (2026, 1, 6, 0, 0)


def test_revenue_is_realistic_and_includes_negatives():
    """种子里必须有负收入（模拟退款/冲销）—— 佣金保底 0 的规则得能被触发到。"""
    revenues = [t.revenue for t in seed.build_board_rows()]

    assert any(p < 0 for p in revenues), "没有负值行，验证不了 max(0, ...) 保底的处理"
    assert all(100 <= abs(p) <= 10000 for p in revenues), "收入量级不像真实盘口"
    assert all(round(p, 2) != round(p) for p in revenues), "收入应该带小数，整数会掩盖累加误差"


def test_one_referral_month_totals_negative():
    """必须有「某个渠道某个月合计为负」的情形。

    这是负 Pnl 里真正含糊的那一档：单笔亏损被同月的盈利盖过去了不算数，整月为负才会
    让 CommissionRow.payable 算出一个负的应付佣金，逼业务规则被明确下来。
    """
    referral_of = {c.uid: c.referral_name for c in seed.build_clients()}

    totals: dict[tuple[str, str], float] = {}
    for txn in seed.build_board_rows():
        referral_name = referral_of.get(txn.uid)
        if referral_name is None:
            continue
        key = (txn.period, referral_name)
        totals[key] = round(totals.get(key, 0.0) + txn.revenue, 2)

    assert any(total < 0 for total in totals.values()), (
        f"没有整月为负的渠道，负 Pnl 的处理不会被暴露出来：{totals}"
    )


def test_board_row_client_names_match_the_client_table():
    known = {c.uid: c.name for c in seed.build_clients()} | dict(seed.UNMAPPED_CLIENTS)
    for row in seed.build_board_rows():
        assert row.client_name == known[row.uid]


def test_board_rows_follow_the_real_export_arithmetic():
    """列之间的关系照真实导出造：2026-09-17 那份 1.2 万行里，这几条恒等式没有一行例外。

    总收入 = opt收入 + 现货手续费_剔除做市商 + 合约手续费_剔除做市商
    总交易额 = opt交易额 + 现货交易额_剔除做市商 + 合约交易额_剔除做市商
    opt收入 = opt_pnl；opt_pnl 为空时 opt收入 = opt手续费
    """
    from decimal import Decimal

    def d(value: float) -> Decimal:
        return Decimal(str(value))

    for row in seed.build_board_rows(with_damaged_uid=True):
        assert d(row.revenue) == d(row.opt_revenue) + d(row.spot_fee) + d(row.contract_fee)
        assert d(row.total_volume) == (
            d(row.opt_volume) + d(row.spot_volume) + d(row.contract_volume)
        )
        if row.opt_pnl is None:
            assert d(row.opt_revenue) == d(row.opt_fee)
        else:
            assert d(row.opt_revenue) == d(row.opt_pnl)
        assert row.contract_fee == 0 and row.contract_volume == 0, "真实导出里合约两列全是 0"


def test_board_rows_cover_both_filled_and_empty_opt_pnl():
    rows = seed.build_board_rows()
    assert any(r.opt_pnl is None for r in rows), "真实导出里 opt_pnl 有空值，种子也要有"
    assert any(r.opt_pnl is not None for r in rows)


def test_board_rows_use_the_real_category_values():
    rows = seed.build_board_rows()
    assert {r.station for r in rows} <= {"中东站", "新加坡站", "香港站"}
    assert {r.sales_group for r in rows} <= {"HK组", "SG组", "支付组"}
    assert {r.user_type for r in rows} == {"平台介绍客户", "自主开发客户"}


def test_board_payload_fills_every_board_column():
    rows = seed.build_board_rows()
    full = next(r for r in rows if r.opt_pnl is not None)
    no_pnl = next(r for r in rows if r.opt_pnl is None)

    payload = seed.board_payload(full, tz=BUSINESS_TZ)
    assert set(payload) == set(schema.DAILY_BOARD_FIELDS)
    assert set(seed.board_payload(no_pnl, tz=BUSINESS_TZ)) == set(schema.DAILY_BOARD_FIELDS) - {
        schema.BOARD_OPT_PNL
    }
    assert isinstance(payload[schema.BOARD_ORDER_DATE], int)
    assert isinstance(payload[schema.BOARD_KYC_DATE], int)
    assert payload[schema.BOARD_CLIENT_UID] == full.uid


# ---------- --with-damaged-uid：让 Excel 损伤检测响一次 ----------


def test_damaged_uids_are_off_by_default():
    default_uids = {t.uid for t in seed.build_board_rows()}
    assert not (default_uids & set(seed.DAMAGED_UIDS)), "默认不该灌损伤 UID"


def test_damaged_uids_are_added_only_to_board_rows():
    """损伤只发生在看板那条导入链路上。客户表是机器人走 API 写的，不过 Excel。

    也因此这几个 UID 必然 join 不上客户表 —— 那正是真实的失败形态。写进客户表反而会
    凭空多出一个拿佣金的幽灵渠道。
    """
    registered = {c.uid for c in seed.build_clients()}
    for uid in seed.DAMAGED_UIDS:
        assert uid not in registered

    damaged_rows = [
        t for t in seed.build_board_rows(with_damaged_uid=True) if t.uid in seed.DAMAGED_UIDS
    ]
    assert len(damaged_rows) == len(seed._DAMAGED_BOARD_ROWS)


def test_every_damaged_uid_actually_trips_the_detector():
    for uid in seed.DAMAGED_UIDS:
        assert looks_excel_truncated(uid), f"{uid} 不会被检测到，这批数据就白灌了"


def _as_excel_would_store_it(uid: str) -> str:
    """Excel 只保留 15 位有效数字，多出来的低位按四舍五入抹成 0。

    注意是四舍五入而不是直接截断：577809207768677761 的第 16 位是 7，
    所以第 15 位会被进位，结果是 ...678000 而不是 ...677000。
    """
    return str(int(Context(prec=15, rounding=ROUND_HALF_UP).create_decimal(uid)))


def test_damaged_uids_are_the_excel_form_of_real_seed_uids():
    """损伤值必须真的是种子里某个 UID 过一遍 Excel 之后的样子，不是随手编的。

    顺带锁住那对「只差最后一位」的 UID 会塌成同一个损伤值 —— 这是整套种子数据里
    最有说服力的一个例子。
    """
    excel_forms = {_as_excel_would_store_it(c.uid) for c in seed.build_clients()}

    for damaged in seed.DAMAGED_UIDS:
        assert damaged in excel_forms, f"{damaged} 不是任何种子 UID 的 Excel 形态"

    assert _as_excel_would_store_it("577809207768677761") == "577809207768678000"
    assert _as_excel_would_store_it("577809207768677762") == "577809207768678000"
    assert "577809207768678000" in seed.DAMAGED_UIDS


def test_damaged_batch_is_enough_to_reach_a_verdict():
    """数量必须够触发聚合判定：命中至少 2 个，且超过巧合期望的 3 倍。

    低于这个门槛的话，检测只会说「在巧合范围内，暂不能判定」，自检就失败了。
    """
    uids = (
        [c.uid for c in seed.build_clients()]
        + list(seed.UNMAPPED_CLIENTS)
        + list(seed.DAMAGED_UIDS)
    )

    report = assess_uid_health(uids)

    assert report.verdict == "likely_damaged"
    assert len(report.truncated) == len(seed.DAMAGED_UIDS)


def test_clean_seed_data_gets_a_clean_verdict():
    """反过来也要成立：不开开关时检测必须是 clean，否则平时全是假警。"""
    uids = [t.uid for t in seed.build_board_rows()] + [c.uid for c in seed.build_clients()]

    assert assess_uid_health(uids).verdict == "clean"


def test_damaged_rows_carry_the_seed_marker_so_reset_can_remove_them():
    for name in seed.DAMAGED_UIDS.values():
        assert seed.is_seed_value(name), "损伤行也必须能被 --reset 清掉"


def test_damaged_rows_do_not_change_the_period_range():
    """损伤数据只是加噪，不该把结算月份范围也改了。"""
    clean = {t.period for t in seed.build_board_rows()}
    damaged = {t.period for t in seed.build_board_rows(with_damaged_uid=True)}

    assert clean == damaged


def test_board_row_natural_key_separates_same_day_same_client_rows():
    """幂等靠这个键。同一天同一个客户可能有多行（历史修正），键必须分得开。"""
    keys = [
        seed.board_row_key(seed.to_timestamp_ms(t.order_date, tz=BUSINESS_TZ), t.uid, t.revenue)
        for t in seed.build_board_rows()
    ]
    assert len(keys) == len(set(keys)), "看板的自然键有重复，重复跑会漏写或写重"


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
        + [t.client_name for t in seed.build_board_rows()]
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
    used_by_reconcile = set(schema.DAILY_BOARD_REQUIRED_FIELDS)
    assert seed.SEED_MARKER_FIELD[schema.TABLE_DAILY_BOARD_NAME] not in used_by_reconcile


# ---------- 日读看板表的字段（sync_base.py 建，seed 只往里写） ----------


def test_import_script_expects_exactly_the_board_columns():
    """导入脚本认的表头和 sync_base 建的列是同一份清单，seed 往里写的也是这一份。"""
    path = Path(__file__).resolve().parent.parent / "scripts" / "import_daily_board.py"
    spec = importlib.util.spec_from_file_location("import_daily_board", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    assert tuple(module.EXPECTED_HEADERS) == tuple(schema.DAILY_BOARD_FIELDS)


def test_board_uid_field_is_text_not_number():
    from crm_basebot.lark.bitable import FIELD_TYPE_NUMBER, FIELD_TYPE_TEXT

    assert schema.DAILY_BOARD_FIELDS[schema.BOARD_CLIENT_UID] == FIELD_TYPE_TEXT
    assert schema.DAILY_BOARD_FIELDS[schema.BOARD_CLIENT_UID] != FIELD_TYPE_NUMBER


def test_board_table_satisfies_the_reconcile_schema_check():
    """assert_fields_present 会在算钱前校验这三个字段，看板表必须过得了。"""
    from crm_basebot.lark.bitable import FieldInfo, assert_fields_present

    fields = [
        FieldInfo(
            field_id=f"fld{index}",
            name=name,
            type=type_code,
            ui_type="",
            is_primary=False,
        )
        for index, (name, type_code) in enumerate(schema.DAILY_BOARD_FIELDS.items())
    ]
    assert_fields_present(fields, schema.DAILY_BOARD_REQUIRED_FIELDS, table_label="日读看板")


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


def test_missing_credentials_is_not_swallowed(monkeypatch):
    """缺凭证的那段人话必须原样浮到进程外面，不能被这里改写成一句更含糊的话。

    文案本身在 tests/test_startup.py 里测；这里只钉住 seed 没有把它 catch 掉。
    """
    from crm_basebot.startup import MissingConfigError

    def boom():
        raise MissingConfigError("缺 LARK_APP_ID，见 docs/LARK_APP_SETUP.md 第 2 步")

    monkeypatch.setattr(seed, "load_settings", boom)

    with pytest.raises(SystemExit) as excinfo:
        seed.main(["--open-id", "ou_abc123"])

    assert excinfo.value.code != 0
    assert "docs/LARK_APP_SETUP.md" in str(excinfo.value)
