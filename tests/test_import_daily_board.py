"""日读看板 xlsx 导入的核心约束。

三件事一旦破了，佣金就会算错，且**在看板还没进 Base 之前**就错了 —— 下游没有
任何机会修复：

1. **user_id 遇到浮点数必须报错**。18-19 位 UID 一过 float，低位就没了。
2. **表头映射覆盖必填字段**。少一个必填列直接拒绝，而不是继续按空值算钱。
3. **交易日期 + 总收入 缺一不可**。缺任何一个的行会被跳过而不是当 0 算。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from openpyxl import Workbook

from crm_basebot.domain import schema

from .conftest import TBL_BOARD


def _load_module():
    """scripts/ 不是包，按路径加载。"""
    path = Path(__file__).resolve().parent.parent / "scripts" / "import_daily_board.py"
    spec = importlib.util.spec_from_file_location("import_daily_board", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


importer = _load_module()


def _make_xlsx(tmp_path, headers, rows) -> Path:
    """造一个 xlsx。headers 是表头行，rows 是数据行的 tuple 列表。"""
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    path = tmp_path / "board.xlsx"
    wb.save(path)
    return path


# ---------- 表头映射 ----------


def test_识别中英文混合的表头(tmp_path):
    """看板 xlsx 的表头就是 user_id / client_name 这种英中混合，导入脚本必须认。"""
    path = _make_xlsx(
        tmp_path,
        [
            "站点",
            "user_id",
            "client_name",
            "销售分组",
            "销售",
            "交易日期",
            "总收入（opt+现货）",
            "opt手续费",
            "现货手续费剔除做市商",
            "opt_pnl",
        ],
        [
            (
                "HashKey SG",
                "577809207768677761",
                "PLUTO",
                "机构组A",
                "王小明",
                date(2026, 9, 10),
                1234.56,
                0.5,
                0.3,
                400.0,
            ),
        ],
    )

    rows = importer.parse_workbook(path)

    assert len(rows) == 1
    fields = rows[0].fields
    assert fields[schema.BOARD_CLIENT_UID] == "577809207768677761"
    assert fields[schema.BOARD_CLIENT_NAME] == "PLUTO"
    assert fields[schema.BOARD_TOTAL_REVENUE] == 1234.56


def test_缺必填列直接报错(tmp_path):
    """user_id 少了下游没法算 —— 让脚本在导入前就炸。"""
    path = _make_xlsx(
        tmp_path,
        ["站点", "client_name", "交易日期", "总收入（opt+现货）"],
        [("HashKey", "X", date(2026, 9, 10), 100.0)],
    )

    with pytest.raises(importer.BoardImportError, match="必填列"):
        importer.parse_workbook(path)


def test_未识别的列被忽略而不是报错(tmp_path):
    """上游可能加辅助列。不该因为多了一列就整份拒绝 —— 但必须至少 warn 一下，
    这里只验证行为不炸，warn 由 logger 输出。"""
    path = _make_xlsx(
        tmp_path,
        ["站点", "user_id", "client_name", "交易日期", "总收入（opt+现货）", "未来某列"],
        [("HashKey", "577809207768677761", "PLUTO", date(2026, 9, 10), 100.0, "随便")],
    )

    rows = importer.parse_workbook(path)
    assert len(rows) == 1


# ---------- user_id 精度 ----------


def test_user_id_是浮点数直接拒绝(tmp_path):
    """浮点形态的 UID 说明源头把它当数字存了，精度已损。"""
    path = _make_xlsx(
        tmp_path,
        ["站点", "user_id", "client_name", "交易日期", "总收入（opt+现货）"],
        [("HashKey", 5.77809e17, "PLUTO", date(2026, 9, 10), 100.0)],
    )

    with pytest.raises(importer.BoardImportError, match="浮点数"):
        importer.parse_workbook(path)


def test_18位整数user_id保精度(tmp_path):
    """整数走的是 str(int)，Python int 是任意精度，不会掉低位。"""
    path = _make_xlsx(
        tmp_path,
        ["站点", "user_id", "client_name", "交易日期", "总收入（opt+现货）"],
        # openpyxl 会把 18 位整数存成 int 或 float 视 Excel 内部形式而定；
        # 这里用字符串写入避免 openpyxl 自作主张
        [("HashKey", "577809207768677761", "PLUTO", date(2026, 9, 10), 100.0)],
    )

    rows = importer.parse_workbook(path)
    assert rows[0].fields[schema.BOARD_CLIENT_UID] == "577809207768677761"


# ---------- 日期解析 ----------


def test_交易日期支持datetime和字符串(tmp_path):
    path = _make_xlsx(
        tmp_path,
        ["站点", "user_id", "client_name", "交易日期", "总收入（opt+现货）"],
        [
            ("HK", "577809207768677761", "A", datetime(2026, 9, 10, 12, 0), 100.0),
            ("HK", "577809207768677762", "B", "2026-09-11", 200.0),
            ("HK", "577809207768677763", "C", "2026/09/12", 300.0),
        ],
    )

    rows = importer.parse_workbook(path)
    dates = {r.order_date for r in rows}
    assert dates == {date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 12)}


def test_日期无法解析直接报错(tmp_path):
    path = _make_xlsx(
        tmp_path,
        ["站点", "user_id", "client_name", "交易日期", "总收入（opt+现货）"],
        [("HK", "577809207768677761", "A", "昨天", 100.0)],
    )

    with pytest.raises(importer.BoardImportError, match="交易日期"):
        importer.parse_workbook(path)


# ---------- 空/缺失行 ----------


def test_全空行被跳过(tmp_path):
    path = _make_xlsx(
        tmp_path,
        ["站点", "user_id", "client_name", "交易日期", "总收入（opt+现货）"],
        [
            ("HK", "577809207768677761", "A", date(2026, 9, 10), 100.0),
            (None, None, None, None, None),
        ],
    )

    assert len(importer.parse_workbook(path)) == 1


def test_only_date_只保留指定日期(tmp_path):
    path = _make_xlsx(
        tmp_path,
        ["站点", "user_id", "client_name", "交易日期", "总收入（opt+现货）"],
        [
            ("HK", "577809207768677761", "A", date(2026, 9, 10), 100.0),
            ("HK", "577809207768677762", "B", date(2026, 9, 11), 200.0),
        ],
    )

    rows = importer.parse_workbook(path, only_date=date(2026, 9, 11))
    assert len(rows) == 1
    assert rows[0].fields[schema.BOARD_CLIENT_UID] == "577809207768677762"


# ---------- 表头别名 ----------


def test_中文表头也认(tmp_path):
    """有的导出会把 user_id 中文化。两种表头都要能进。"""
    path = _make_xlsx(
        tmp_path,
        ["站点", "客户UID", "客户名称", "交易日期", "总收入"],
        [("HK", "577809207768677761", "A", date(2026, 9, 10), 100.0)],
    )

    rows = importer.parse_workbook(path)
    assert len(rows) == 1
    assert rows[0].fields[schema.BOARD_CLIENT_UID] == "577809207768677761"


# ---------- 写进 Base 的日期，以及先删后写认不认界面里填的日期 ----------

SGT = ZoneInfo("Asia/Singapore")
UID_X = "577809207768677761"


def _board_row(day: date, uid: str = UID_X) -> importer.BoardRow:
    return importer.BoardRow(
        order_date=day,
        fields={
            schema.BOARD_CLIENT_UID: uid,
            schema.BOARD_ORDER_DATE: day,
            schema.BOARD_TOTAL_REVENUE: 100.0,
        },
    )


def _sgt_midnight_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=SGT).timestamp() * 1000)


def test_交易日期写成业务时区那天的零点(fake_bitable):
    importer._apply(fake_bitable, TBL_BOARD, [_board_row(date(2026, 9, 10))], tz=SGT)

    (record,) = fake_bitable.tables[TBL_BOARD].records.values()
    assert record[schema.BOARD_ORDER_DATE] == _sgt_midnight_ms(date(2026, 9, 10))


def test_先删后写认得界面里手工填的日期(fake_bitable):
    """Base 界面里填「9 月 10 日」存的是新加坡零点。按 UTC 取日期它是 9 月 9 日，
    重导 9 月 10 日时就删不掉它，表里会留下两份。"""
    stale = fake_bitable.tables[TBL_BOARD].add_existing(
        {
            schema.BOARD_ORDER_DATE: _sgt_midnight_ms(date(2026, 9, 10)),
            schema.BOARD_CLIENT_UID: UID_X,
        }
    )

    deleted, written = importer._apply(
        fake_bitable, TBL_BOARD, [_board_row(date(2026, 9, 10))], tz=SGT
    )

    assert (deleted, written) == (1, 1)
    assert stale not in fake_bitable.tables[TBL_BOARD].records


def test_先删后写只碰涉及的日期(fake_bitable):
    kept = fake_bitable.tables[TBL_BOARD].add_existing(
        {
            schema.BOARD_ORDER_DATE: _sgt_midnight_ms(date(2026, 9, 9)),
            schema.BOARD_CLIENT_UID: UID_X,
        }
    )

    importer._apply(fake_bitable, TBL_BOARD, [_board_row(date(2026, 9, 10))], tz=SGT)

    assert kept in fake_bitable.tables[TBL_BOARD].records
