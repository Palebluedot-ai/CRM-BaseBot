"""每日管线的编排层（`crm_basebot.pipeline`）。

这个文件存在的意义，一半是测行为，一半是证明**架构真的解耦了**：整条路（取数 → 解析 →
筛站点 → 算增量 → 写 Base）在测试里跑得通，而且不出网、不碰真 Base —— 取数是一个注入
进去的函数，写库是内存假件。做得到这件事，说明编排层只认识「给我一个文件路径」和
「把这批行写进去」两个动作。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from crm_basebot.domain import schema
from crm_basebot.domain.dates import date_to_ms
from crm_basebot.pipeline import compute_new_dates, run_daily
from crm_basebot.pipeline.daily import DailyError

from .conftest import TBL_BOARD, TBL_CLIENT
from .test_import_daily_incremental import _make_xlsx, _row

SGT = ZoneInfo("Asia/Singapore")


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        business_timezone="Asia/Singapore",
        table_daily_board=TBL_BOARD,
        table_client=TBL_CLIENT,
        daily_export_dir="attachments",
        ms_tenant_id="",
        ms_client_id="",
        ms_client_secret="",
        ms_user_id="",
        graph_sender="",
    )


def _seed_board(fake_bitable, day: date, uid: str = "111") -> str:
    return fake_bitable.table(TBL_BOARD).add_existing(
        {schema.BOARD_ORDER_DATE: date_to_ms(day, tz=SGT), schema.BOARD_CLIENT_UID: uid}
    )


def _fake_fetch(path: Path):
    """冒充 source.fetch_from_mail：编排层只要求「返回一个文件路径」。"""

    def fetch(_settings, _out_dir):  # noqa: ANN001 - 形状对齐即可
        return path

    return fetch


# ---------- 注入式取数（不出网） ----------


def test_取数可以注入_整条路不出网(fake_bitable, tmp_path):
    xlsx = _make_xlsx(tmp_path, [_row(when="2026-09-17", uid="222")])

    result = run_daily(
        settings=_settings(),
        bitable=fake_bitable,
        from_mail=True,
        fetch=_fake_fetch(xlsx),
    )

    assert result.written == 1
    assert result.plan.new_dates == (date(2026, 9, 17),)
    assert [fields[schema.BOARD_CLIENT_UID] for _, fields in fake_bitable.writes] == ["222"]


def test_取数失败时报出人话(tmp_path):
    from crm_basebot.pipeline.source import SourceError

    def boom(_settings, _out_dir):  # noqa: ANN001
        raise SourceError("从邮箱取附件失败：读邮件需要的 .env 键缺失：MICROSOFT_GRAPH_USER_ID")

    try:
        run_daily(
            settings=_settings(),
            bitable=None,
            from_mail=True,
            fetch=boom,
        )
    except SourceError as exc:
        assert "MICROSOFT_GRAPH_USER_ID" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应该抛出 SourceError")


# ---------- 只导新加坡站 ----------


def test_别的站点的行不写进Base(fake_bitable, tmp_path):
    xlsx = _make_xlsx(
        tmp_path,
        [
            _row(when="2026-09-17", uid="222"),
            _row(when="2026-09-17", uid="999", station="香港站"),
            _row(when="2026-09-17", uid="888", station="中东站"),
        ],
    )

    result = run_daily(settings=_settings(), bitable=fake_bitable, file=xlsx)

    assert result.written == 1
    assert result.plan.station_counts["香港站"] == 1
    assert result.plan.station_counts["中东站"] == 1


def test_某天改正后只剩别的站点时清掉旧记录(fake_bitable, tmp_path):
    """导出是它覆盖的每一天的权威：这天新加坡站没记录了，看板里那天的旧账也要清掉。

    这种日子**不是「新增」**（看板里有），所以普通增量永远碰不到它 —— 旧记录会一直
    留在看板上继续参与佣金计算，而且没人会发现。判据见 delta.stale_board_dates。
    """
    stale = _seed_board(fake_bitable, date(2026, 9, 17))
    xlsx = _make_xlsx(
        tmp_path,
        [
            _row(when="2026-09-16", uid="222"),  # 新加坡站，新增
            _row(when="2026-09-17", uid="999", station="香港站"),  # 这天只剩香港站
        ],
    )

    result = run_daily(settings=_settings(), bitable=fake_bitable, file=xlsx)

    assert result.plan.stale_dates == (date(2026, 9, 17),)
    assert result.plan.new_dates == (date(2026, 9, 16), date(2026, 9, 17))
    assert result.plan.rows_to_import == 1  # 只有 9-16 那条新加坡站的行
    assert result.deleted == 1  # 9-17 的旧记录被清掉
    assert stale not in fake_bitable.table(TBL_BOARD).records


def test_导出没覆盖的日期不会被清掉(fake_bitable, tmp_path):
    """导出窗口之外的老数据不归它管 —— 不能因为「导出里没有」就把历史删了。"""
    old = _seed_board(fake_bitable, date(2025, 1, 5))
    xlsx = _make_xlsx(tmp_path, [_row(when="2026-09-17", uid="222")])

    result = run_daily(settings=_settings(), bitable=fake_bitable, file=xlsx)

    assert result.plan.stale_dates == ()
    assert old in fake_bitable.table(TBL_BOARD).records


def test_导出里一行新加坡站都没有就停下(fake_bitable, tmp_path):
    xlsx = _make_xlsx(tmp_path, [_row(when="2026-09-17", station="香港站")])

    try:
        run_daily(settings=_settings(), bitable=fake_bitable, file=xlsx)
    except DailyError as exc:
        assert "香港站" in str(exc)
        assert "新加坡站" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应该拒绝导入")

    assert fake_bitable.write_count == 0


# ---------- 什么都不该做的时候 ----------


def test_没有新增交易日时一个写请求都不发(fake_bitable, tmp_path):
    _seed_board(fake_bitable, date(2026, 9, 17))
    xlsx = _make_xlsx(tmp_path, [_row(when="2026-09-17", uid="111")])
    before = fake_bitable.write_count

    result = run_daily(settings=_settings(), bitable=fake_bitable, file=xlsx)

    assert result.plan.has_work is False
    assert (result.deleted, result.written) == (0, 0)
    assert fake_bitable.write_count == before


def test_dry_run不碰Base(fake_bitable, tmp_path):
    xlsx = _make_xlsx(tmp_path, [_row(when="2026-09-17", uid="222")])
    before = fake_bitable.write_count

    result = run_daily(settings=_settings(), bitable=fake_bitable, file=xlsx, dry_run=True)

    assert result.plan.has_work is True
    assert (result.deleted, result.written) == (0, 0)
    assert fake_bitable.write_count == before


def test_新增太多天时停下来(fake_bitable, tmp_path):
    days = [date(2026, 9, day) for day in range(1, 9)]
    xlsx = _make_xlsx(tmp_path, [_row(when=day.isoformat()) for day in days])

    try:
        run_daily(settings=_settings(), bitable=fake_bitable, file=xlsx)
    except DailyError as exc:
        assert "--max-days" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应该拒绝导入")

    assert fake_bitable.write_count == 0


def test_显式放行时超过上限也导(fake_bitable, tmp_path):
    days = [date(2026, 9, day) for day in range(1, 9)]
    xlsx = _make_xlsx(tmp_path, [_row(when=day.isoformat()) for day in days])

    result = run_daily(
        settings=_settings(),
        bitable=fake_bitable,
        file=xlsx,
        allow_many_days=True,
    )

    assert result.written == len(days)


# ---------- 算增量（纯函数） ----------


def test_增量只认导出里有而看板没有的日期():
    assert compute_new_dates({date(2026, 9, 16), date(2026, 9, 17)}, {date(2026, 9, 16)}) == [
        date(2026, 9, 17)
    ]


def test_清账只认看板有而导出该站点没记录的日期():
    """三个集合的差集；导出没覆盖的日期不碰。"""
    from crm_basebot.pipeline import stale_board_dates

    existing = {date(2026, 9, 16), date(2026, 9, 17), date(2025, 1, 5)}
    covered = {date(2026, 9, 16), date(2026, 9, 17)}
    kept = {date(2026, 9, 16)}

    assert stale_board_dates(existing, covered, kept) == {date(2026, 9, 17)}


def test_看板已经有值时不重导(fake_bitable, tmp_path):
    """幂等：同一个文件跑两遍，第二遍什么都不写。"""
    xlsx = _make_xlsx(tmp_path, [_row(when="2026-09-17", uid="222")])

    first = run_daily(settings=_settings(), bitable=fake_bitable, file=xlsx)
    second = run_daily(settings=_settings(), bitable=fake_bitable, file=xlsx)

    assert first.written == 1
    assert second.written == 0
    assert second.deleted == 0


def test_模块的公开面就是那几个名字():
    """pipeline 是对外契约：脚本只能从这些名字进，别的地方不该被 import。"""
    import crm_basebot.pipeline as pipeline

    assert set(pipeline.__all__) == {
        "EXPECTED_HEADERS",
        "BoardImportError",
        "BoardRow",
        "DailyPlan",
        "DailyResult",
        "apply_import",
        "client_links",
        "compute_new_dates",
        "describe_stations",
        "existing_dates",
        "parse_workbook",
        "pick_latest_export",
        "run_daily",
        "split_by_station",
        "stale_board_dates",
    }
    assert sys.modules["crm_basebot.pipeline"].__doc__ is not None
