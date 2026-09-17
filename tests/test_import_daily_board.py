"""日读看板 xlsx 导入的核心约束。

看板 xlsx 是佣金的唯一数据来源。这里的约束一旦破了，钱在进 Base 之前就算错了，
下游没有机会修复：

1. **表头逐字以真实导出为准**。2026-09-17 那份「OTC组销售明细」的 18 列表头就是
   合同，缺一列直接拒绝，而不是拿着空值往下算。
2. **用户ID 遇到浮点数必须报错**。18-19 位的用户ID 一过 float，低位就没了。
3. **用户ID、交易日期、总收入缺一不可**。缺了的行跳过；总收入是 0 不算缺。
4. **一次写一批**。一行一个请求的话，1.2 万行会吃光免费版的月度调用额度。
"""

from __future__ import annotations

import importlib.util
import logging
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

# 2026-09-17 内部系统导出的「OTC组销售明细」xlsx 表头，逐字照抄，顺序也照抄。
# 故意写成字面量而不是从 schema 读：schema 被改错了，这里要能红。
REAL_HEADERS = [
    "站点",
    "用户ID",
    "交易日期",
    "销售",
    "客户名称",
    "KYC日期",
    "销售分组",
    "用户类型",
    "现货手续费_剔除做市商",
    "现货交易额_剔除做市商",
    "合约手续费_剔除做市商",
    "合约交易额_剔除做市商",
    "opt手续费",
    "opt_pnl",
    "opt收入",
    "opt交易额",
    "总收入(opt+现货+合约)",
    "总交易额(opt+现货+合约)",
]

# 一行和真实导出同形态的数据：日期是文本，没发生的金额是字符串 "0"。值是编的。
BASE_ROW = {
    "站点": "新加坡站",
    "用户ID": "577809207768677761",
    "交易日期": "2026-09-10",
    "销售": "测试销售",
    "客户名称": "PLUTO STUDIO LIMITED",
    "KYC日期": "2025-03-02",
    "销售分组": "SG组",
    "用户类型": "平台介绍客户",
    "现货手续费_剔除做市商": 372.17,
    "现货交易额_剔除做市商": 465212.5,
    "合约手续费_剔除做市商": "0",
    "合约交易额_剔除做市商": "0",
    "opt手续费": "0",
    "opt_pnl": 868.38,
    "opt收入": 868.38,
    "opt交易额": "0",
    "总收入(opt+现货+合约)": 1240.55,
    "总交易额(opt+现货+合约)": 465212.5,
}

SGT = ZoneInfo("Asia/Singapore")
UID_X = "577809207768677761"


def _row(headers=REAL_HEADERS, overrides=None) -> list:
    values = {**BASE_ROW, **(overrides or {})}
    return [values.get(h) for h in headers]


def _make_xlsx(tmp_path, rows, headers=REAL_HEADERS) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.append(list(headers))
    for row in rows:
        ws.append(row)
    path = tmp_path / "board.xlsx"
    wb.save(path)
    return path


# ---------- 表头 ----------


def test_看板字段和真实导出的表头逐字一致():
    """Base 里的列就是 Excel 的列，名字和顺序都一样，拿着 Excel 能在 Base 里找到同一列。"""
    assert list(schema.DAILY_BOARD_FIELDS) == REAL_HEADERS


def test_导入脚本认的表头就是看板的列():
    assert list(importer.EXPECTED_HEADERS) == REAL_HEADERS


def test_缺任何一列都拒绝导入并点名缺的列(tmp_path):
    headers = [h for h in REAL_HEADERS if h != "用户类型"]
    path = _make_xlsx(tmp_path, [_row(headers)], headers=headers)

    with pytest.raises(importer.BoardImportError, match="用户类型"):
        importer.parse_workbook(path)


def test_全角括号和首尾空格的表头也认(tmp_path):
    headers = list(REAL_HEADERS)
    headers[headers.index("总收入(opt+现货+合约)")] = "总收入（opt+现货+合约）"
    headers[headers.index("用户ID")] = " 用户ID "
    path = _make_xlsx(tmp_path, [_row()], headers=headers)

    (row,) = importer.parse_workbook(path)
    assert row.fields[schema.BOARD_TOTAL_REVENUE] == 1240.55
    assert row.fields[schema.BOARD_CLIENT_UID] == UID_X


def test_多出来的列被忽略(tmp_path):
    headers = [*REAL_HEADERS, "备注"]
    path = _make_xlsx(tmp_path, [[*_row(), "随便写的"]], headers=headers)

    (row,) = importer.parse_workbook(path)
    assert "备注" not in row.fields


# ---------- 一行里的值 ----------


def test_真实形态的一行能完整读进来(tmp_path):
    path = _make_xlsx(tmp_path, [_row()])

    (row,) = importer.parse_workbook(path)
    fields = row.fields

    assert set(fields) == set(REAL_HEADERS)
    assert fields["用户ID"] == UID_X
    assert fields["交易日期"] == date(2026, 9, 10)
    assert fields["KYC日期"] == date(2025, 3, 2)
    assert fields["站点"] == "新加坡站"
    assert fields["用户类型"] == "平台介绍客户"
    assert fields["合约手续费_剔除做市商"] == 0.0, "文本 0 要转成数字 0"
    assert fields["现货手续费_剔除做市商"] == 372.17
    assert fields["总收入(opt+现货+合约)"] == 1240.55
    assert row.order_date == date(2026, 9, 10)


def test_总收入为0的行不会被当成缺失丢掉(tmp_path):
    """真实导出里有几百行总收入是 0。它们不产生佣金，但要算进记录笔数和客户数。"""
    path = _make_xlsx(tmp_path, [_row(overrides={"总收入(opt+现货+合约)": "0"})])

    (row,) = importer.parse_workbook(path)
    assert row.fields[schema.BOARD_TOTAL_REVENUE] == 0.0


def test_opt_pnl为空时不写这一列(tmp_path):
    path = _make_xlsx(tmp_path, [_row(overrides={"opt_pnl": None})])

    (row,) = importer.parse_workbook(path)
    assert schema.BOARD_OPT_PNL not in row.fields


def test_千分位逗号的金额也认(tmp_path):
    path = _make_xlsx(tmp_path, [_row(overrides={"总交易额(opt+现货+合约)": "1,465,212.50"})])

    (row,) = importer.parse_workbook(path)
    assert row.fields[schema.BOARD_TOTAL_VOLUME] == 1465212.5


# ---------- 用户ID 精度 ----------


def test_用户ID是浮点数直接拒绝(tmp_path):
    """浮点形态的用户ID 说明源头把这一列当数字存了，精度已损。"""
    path = _make_xlsx(tmp_path, [_row(overrides={"用户ID": 5.77809e17})])

    with pytest.raises(importer.BoardImportError, match="用户ID"):
        importer.parse_workbook(path)


def test_数字格式的短用户ID转成字符串(tmp_path):
    """五到七位的 ID 存成数字也不丢精度，转成字符串照常用。"""
    path = _make_xlsx(tmp_path, [_row(overrides={"用户ID": 1234567})])

    (row,) = importer.parse_workbook(path)
    assert row.fields[schema.BOARD_CLIENT_UID] == "1234567"


def test_数字格式的长用户ID一定会被拒绝(tmp_path):
    """xlsx 的数字单元格底层就是 float64。18-19 位的 ID 一旦存成数字，写进文件那一刻
    低位就没了，读回来是浮点数；连 openpyxl 自己写整数也是这样。所以这条路只能拒绝。"""
    path = _make_xlsx(tmp_path, [_row(overrides={"用户ID": 2141293991366272768})])

    with pytest.raises(importer.BoardImportError, match="浮点数"):
        importer.parse_workbook(path)


def test_短用户ID也照常读(tmp_path):
    """真实导出里 HK组和支付组有五到七位的用户ID，不能当成坏数据。"""
    path = _make_xlsx(tmp_path, [_row(overrides={"用户ID": "1234567"})])

    (row,) = importer.parse_workbook(path)
    assert row.fields[schema.BOARD_CLIENT_UID] == "1234567"


# ---------- 日期 ----------


def test_交易日期支持文本和datetime(tmp_path):
    path = _make_xlsx(
        tmp_path,
        [
            _row(overrides={"交易日期": datetime(2026, 9, 10, 12, 0)}),
            _row(overrides={"交易日期": "2026/09/11"}),
            _row(overrides={"交易日期": "2026-09-12"}),
        ],
    )

    assert {r.order_date for r in importer.parse_workbook(path)} == {
        date(2026, 9, 10),
        date(2026, 9, 11),
        date(2026, 9, 12),
    }


def test_交易日期写错时报错并点名列(tmp_path):
    path = _make_xlsx(tmp_path, [_row(overrides={"交易日期": "昨天"})])

    with pytest.raises(importer.BoardImportError, match="交易日期"):
        importer.parse_workbook(path)


def test_KYC日期可以为空(tmp_path):
    """真实导出里有一百多行没有 KYC日期。"""
    path = _make_xlsx(tmp_path, [_row(overrides={"KYC日期": None})])

    (row,) = importer.parse_workbook(path)
    assert schema.BOARD_KYC_DATE not in row.fields


def test_KYC日期写错时报错并点名列(tmp_path):
    path = _make_xlsx(tmp_path, [_row(overrides={"KYC日期": "待补"})])

    with pytest.raises(importer.BoardImportError, match="KYC日期"):
        importer.parse_workbook(path)


# ---------- 空行和缺值 ----------


def test_全空行被跳过(tmp_path):
    path = _make_xlsx(tmp_path, [_row(), [None] * len(REAL_HEADERS)])

    assert len(importer.parse_workbook(path)) == 1


def test_缺用户ID的行被跳过且日志里不带客户信息(tmp_path, caplog):
    """跳过要留痕，但日志会被转发和截图，不该把客户名称打出来。"""
    path = _make_xlsx(tmp_path, [_row(), _row(overrides={"用户ID": None})])

    with caplog.at_level(logging.WARNING):
        rows = importer.parse_workbook(path)

    assert len(rows) == 1
    assert "用户ID" in caplog.text
    assert "PLUTO" not in caplog.text


def test_only_date只保留指定日期(tmp_path):
    path = _make_xlsx(
        tmp_path,
        [
            _row(overrides={"交易日期": "2026-09-10"}),
            _row(overrides={"交易日期": "2026-09-11", "用户ID": "577809207768677762"}),
        ],
    )

    (row,) = importer.parse_workbook(path, only_date=date(2026, 9, 11))
    assert row.fields[schema.BOARD_CLIENT_UID] == "577809207768677762"


# ---------- 写进 Base ----------


def _board_row(day: date, uid: str = UID_X) -> importer.BoardRow:
    return importer.BoardRow(
        order_date=day,
        fields={
            schema.BOARD_CLIENT_UID: uid,
            schema.BOARD_ORDER_DATE: day,
            schema.BOARD_KYC_DATE: date(2025, 3, 2),
            schema.BOARD_TOTAL_REVENUE: 100.0,
        },
    )


def _sgt_midnight_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=SGT).timestamp() * 1000)


def test_两个日期列都写成业务时区那天的零点(fake_bitable):
    importer._apply(fake_bitable, TBL_BOARD, [_board_row(date(2026, 9, 10))], tz=SGT)

    (record,) = fake_bitable.tables[TBL_BOARD].records.values()
    assert record[schema.BOARD_ORDER_DATE] == _sgt_midnight_ms(date(2026, 9, 10))
    assert record[schema.BOARD_KYC_DATE] == _sgt_midnight_ms(date(2025, 3, 2))


def test_读进来的一行写进Base时列名都是看板表的列(tmp_path, fake_bitable):
    path = _make_xlsx(tmp_path, [_row()])
    importer._apply(fake_bitable, TBL_BOARD, importer.parse_workbook(path), tz=SGT)

    ((table_id, fields),) = fake_bitable.writes
    assert table_id == TBL_BOARD
    assert set(fields) <= set(schema.DAILY_BOARD_FIELDS)
    assert fields[schema.BOARD_CLIENT_UID] == UID_X


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


def test_写入和删除都按批发送(fake_bitable):
    """一行一个请求的话，1.2 万行的导出一次就是 1.2 万次调用，免费版一个月基线才 1 万次。"""
    day = date(2026, 9, 10)
    for _ in range(600):
        fake_bitable.tables[TBL_BOARD].add_existing(
            {schema.BOARD_ORDER_DATE: _sgt_midnight_ms(day), schema.BOARD_CLIENT_UID: UID_X}
        )

    deleted, written = importer._apply(
        fake_bitable, TBL_BOARD, [_board_row(day) for _ in range(1201)], tz=SGT
    )

    assert (deleted, written) == (600, 1201)
    assert fake_bitable.batch_delete_calls == [(TBL_BOARD, 500), (TBL_BOARD, 100)]
    assert fake_bitable.batch_create_calls == [
        (TBL_BOARD, 500),
        (TBL_BOARD, 500),
        (TBL_BOARD, 201),
    ]
    assert len(fake_bitable.tables[TBL_BOARD].records) == 1201


# ---------- 只导新加坡站（2026-09-17 定的） ----------


class RunSettings:
    business_timezone = "Asia/Singapore"
    table_daily_board = TBL_BOARD
    daily_board_xlsx = ""


def _run(path: Path, bitable, *extra: str) -> int:
    args = importer.build_parser().parse_args(["--file", str(path), *extra])
    return importer.run(args, RunSettings(), bitable)


def _sg_record(day: date) -> dict:
    return {schema.BOARD_ORDER_DATE: _sgt_midnight_ms(day), schema.BOARD_STATION: "新加坡站"}


def test_只有新加坡站的行写进Base(tmp_path, fake_bitable):
    """导出里还有香港站和中东站，看板只要新加坡站。"""
    path = _make_xlsx(
        tmp_path,
        [
            _row(overrides={"站点": "新加坡站", "用户ID": "577809207768677761"}),
            _row(overrides={"站点": "香港站", "用户ID": "577809207768677762"}),
            _row(overrides={"站点": "中东站", "用户ID": "577809207768677763"}),
        ],
    )

    assert _run(path, fake_bitable) == 0

    records = list(fake_bitable.tables[TBL_BOARD].records.values())
    assert [r[schema.BOARD_CLIENT_UID] for r in records] == ["577809207768677761"]
    assert {r[schema.BOARD_STATION] for r in records} == {"新加坡站"}


def test_站点两边带空格也算新加坡站(tmp_path, fake_bitable):
    path = _make_xlsx(tmp_path, [_row(overrides={"站点": " 新加坡站 "})])

    assert _run(path, fake_bitable) == 0
    assert len(fake_bitable.tables[TBL_BOARD].records) == 1


def test_导出覆盖到的日期整天替换_只有别的站点的日子也清掉旧记录(tmp_path, fake_bitable):
    """导出是它覆盖的每一天的权威。某天导出里只剩香港站，说明这天新加坡站没有记录，
    Base 里这天的旧记录就得删掉，不然更正过的数据会留着旧账。导出没覆盖的日子不动。"""
    table = fake_bitable.tables[TBL_BOARD]
    old_0910 = table.add_existing(_sg_record(date(2026, 9, 10)))
    old_0911 = table.add_existing(_sg_record(date(2026, 9, 11)))
    untouched_0909 = table.add_existing(_sg_record(date(2026, 9, 9)))
    path = _make_xlsx(
        tmp_path,
        [
            _row(overrides={"站点": "香港站", "交易日期": "2026-09-10"}),
            _row(overrides={"站点": "新加坡站", "交易日期": "2026-09-11"}),
        ],
    )

    assert _run(path, fake_bitable) == 0

    assert old_0910 not in table.records
    assert old_0911 not in table.records
    assert untouched_0909 in table.records
    assert len(table.records) == 2, "留下 9 月 9 日的旧记录和 9 月 11 日新写的一条"


def test_一行新加坡站都没有就拒绝导入且不碰Base(tmp_path, fake_bitable, capsys):
    """站点改了写法、或者导错了文件，筛完一行不剩。这时照常先删后写，会把 Base 里
    这些日期的记录删光，所以宁可停下。"""
    table = fake_bitable.tables[TBL_BOARD]
    old = table.add_existing(_sg_record(date(2026, 9, 10)))
    path = _make_xlsx(tmp_path, [_row(overrides={"站点": "香港站", "交易日期": "2026-09-10"})])

    assert _run(path, fake_bitable) == 1

    assert old in table.records
    assert fake_bitable.deleted == []
    assert fake_bitable.write_count == 0
    err = capsys.readouterr().err
    assert "新加坡站" in err
    assert "香港站" in err


def test_预演列出各站点的行数且不碰Base(tmp_path, fake_bitable, capsys):
    path = _make_xlsx(
        tmp_path,
        [
            _row(overrides={"站点": "新加坡站"}),
            _row(overrides={"站点": "香港站"}),
            _row(overrides={"站点": "香港站"}),
        ],
    )

    assert _run(path, fake_bitable, "--dry-run") == 0

    out = capsys.readouterr().out
    assert "新加坡站 1 行" in out
    assert "香港站 2 行" in out
    assert fake_bitable.tables[TBL_BOARD].scan_count == 0
    assert fake_bitable.write_count == 0
