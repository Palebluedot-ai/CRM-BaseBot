"""每日增量导入。

日常这条路上只有三件事可能出错，测试就盯这三件：

1. **算错「哪些天是新的」** —— 少算 = 数据缺失没人发现；多算 = 把几个月的行重写一遍。
2. **导了不该导的站点** —— 看板只要新加坡站，香港站/中东站的行混进来会直接改变佣金口径。
3. **什么都不该导的时候动了 Base** —— 库里没变化却删了旧记录，是最难发现的一类破坏。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest  # noqa: F401  （fake_bitable fixture 由 conftest 提供，pytest 负责发现它）
from openpyxl import Workbook

from crm_basebot.domain import schema
from crm_basebot.domain.dates import date_to_ms

from .conftest import TBL_BOARD, TBL_CLIENT

SGT_TIMEZONE = "Asia/Singapore"


def _load_module(name: str):
    """scripts/ 不是包，按路径加载。"""
    path = Path(__file__).resolve().parent.parent / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


incremental = _load_module("import_daily_incremental")

# 和真实导出同样的 18 列（顺序也一致）。写成字面量：schema 被改坏了这里要能红。
HEADERS = [
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


def _row(*, station: str = "新加坡站", uid: str = "577809207768677761", when: str) -> list:
    values = {
        "站点": station,
        "用户ID": uid,
        "交易日期": when,
        "销售": "测试销售",
        "客户名称": "PLUTO STUDIO LIMITED",
        "KYC日期": "2025-03-02",
        "销售分组": "SG组",
        "用户类型": "平台介绍客户",
        "现货手续费_剔除做市商": 10.0,
        "现货交易额_剔除做市商": 1000.0,
        "合约手续费_剔除做市商": "0",
        "合约交易额_剔除做市商": "0",
        "opt手续费": "0",
        "opt_pnl": "0",
        "opt收入": "0",
        "opt交易额": "0",
        "总收入(opt+现货+合约)": 10.0,
        "总交易额(opt+现货+合约)": 1000.0,
    }
    return [values.get(header) for header in HEADERS]


def _make_xlsx(
    directory: Path, rows: list[list], name: str = "OTC组销售明细_2026-09-18.xlsx"
) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(HEADERS)
    for row in rows:
        sheet.append(row)
    path = directory / name
    workbook.save(path)
    return path


def _settings(**overrides) -> SimpleNamespace:
    values = {
        "business_timezone": SGT_TIMEZONE,
        "table_daily_board": TBL_BOARD,
        "table_client": TBL_CLIENT,
        "daily_export_dir": "attachments",
        "ms_tenant_id": "",
        "ms_client_id": "",
        "ms_client_secret": "",
        "ms_user_id": "",
        "graph_sender": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _args(*extra: str):
    """走真实的命令行解析，顺带把 CLI 的接线也测了。"""
    return incremental.build_parser().parse_args(list(extra))


# ---------- 算「哪些天是新的」 ----------


def test_新增日期就是导出里有而看板里没有的那些():
    source = {date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17)}
    existing = {date(2026, 9, 15), date(2026, 9, 16)}
    assert incremental.compute_new_dates(source, existing) == [date(2026, 9, 17)]


def test_看板为空时全部都是新的():
    source = {date(2026, 9, 16), date(2026, 9, 17)}
    assert incremental.compute_new_dates(source, set()) == [
        date(2026, 9, 16),
        date(2026, 9, 17),
    ]


def test_refresh点名的日期会被重导():
    source = {date(2026, 9, 16), date(2026, 9, 17)}
    existing = source
    assert incremental.compute_new_dates(source, existing, refresh={date(2026, 9, 16)}) == [
        date(2026, 9, 16)
    ]


def test_refresh点名导出里没有的日期不会凭空删数据():
    """点名一个导出里没有的日子，不该在 Base 上把那天已有的历史行删掉。"""
    assert (
        incremental.compute_new_dates(
            {date(2026, 9, 17)},  # 导出里只有 9-17
            {date(2026, 9, 1), date(2026, 9, 17)},  # 看板里 9-1 和 9-17 都有
            refresh={date(2026, 9, 1)},  # 点名重导 9-1，但导出里没有这天
        )
        == []
    )


def test_since只留这一天之后的():
    source = {date(2026, 9, 1), date(2026, 9, 16), date(2026, 9, 17)}
    assert incremental.compute_new_dates(source, set(), since=date(2026, 9, 16)) == [
        date(2026, 9, 16),
        date(2026, 9, 17),
    ]


# ---------- 挑文件 ----------


def test_按文件名里的日期挑最新一份而不是mtime(tmp_path):
    old = _make_xlsx(tmp_path, [_row(when="2026-09-16")], name="OTC组销售明细_2026-09-16.xlsx")
    new = _make_xlsx(tmp_path, [_row(when="2026-09-17")], name="OTC组销售明细_2026-09-17.xlsx")
    # 把「更旧」的那份改成最近才被下载过：mtime 说谎时仍然要选对
    old.touch()
    assert incremental.latest_export(tmp_path) == new


def test_目录里没有导出时返回空(tmp_path):
    assert incremental.latest_export(tmp_path) is None


# ---------- 走完整条路 ----------


def _seed_board(fake_bitable, day: date, uid: str = "111") -> None:
    fake_bitable.table(TBL_BOARD).add_existing(
        {
            schema.BOARD_ORDER_DATE: date_to_ms(day, tz=ZoneInfo(SGT_TIMEZONE)),
            schema.BOARD_CLIENT_UID: uid,
        }
    )


def test_只导新增那一天的记录(fake_bitable, tmp_path, capsys):
    _seed_board(fake_bitable, date(2026, 9, 16))
    xlsx = _make_xlsx(
        tmp_path,
        [
            _row(when="2026-09-16", uid="111"),  # 已有 → 不导
            _row(when="2026-09-17", uid="222"),  # 新增 → 导入
            _row(when="2026-09-17", uid="333"),
        ],
    )
    args = _args("--file", str(xlsx))

    assert incremental.run(args, _settings(), fake_bitable) == 0

    written_uids = [fields[schema.BOARD_CLIENT_UID] for _, fields in fake_bitable.writes]
    assert written_uids == ["222", "333"]
    # 表里原有那条 9-16 的记录没被删掉
    assert fake_bitable.deleted == []


def test_别的站点的行不会被导入(fake_bitable, tmp_path, capsys):
    xlsx = _make_xlsx(
        tmp_path,
        [
            _row(when="2026-09-17", uid="222"),
            _row(when="2026-09-17", uid="999", station="香港站"),
            _row(when="2026-09-17", uid="888", station="中东站"),
        ],
    )

    assert incremental.run(_args("--file", str(xlsx)), _settings(), fake_bitable) == 0

    written_uids = [fields[schema.BOARD_CLIENT_UID] for _, fields in fake_bitable.writes]
    assert written_uids == ["222"]


def test_没有新增时一个写请求都不发(fake_bitable, tmp_path):
    _seed_board(fake_bitable, date(2026, 9, 17))
    xlsx = _make_xlsx(tmp_path, [_row(when="2026-09-17", uid="111")])
    before = fake_bitable.write_count

    assert incremental.run(_args("--file", str(xlsx)), _settings(), fake_bitable) == 0

    assert fake_bitable.write_count == before
    assert fake_bitable.deleted == []


def test_新增日期超过上限时停下来(fake_bitable, tmp_path, capsys):
    """一次多出很多天，通常是看板被清空了或指错了文件 —— 该停下让人看一眼。"""
    days = [date(2026, 9, day) for day in range(1, 9)]
    xlsx = _make_xlsx(tmp_path, [_row(when=day.isoformat()) for day in days])

    assert incremental.run(_args("--file", str(xlsx)), _settings(), fake_bitable) == 1

    assert fake_bitable.write_count == 0
    assert "max-days" in capsys.readouterr().err


def test_显式放行时超过上限也导(fake_bitable, tmp_path):
    days = [date(2026, 9, day) for day in range(1, 9)]
    xlsx = _make_xlsx(tmp_path, [_row(when=day.isoformat()) for day in days])

    args = _args("--file", str(xlsx), "--allow-many-days")

    assert incremental.run(args, _settings(), fake_bitable) == 0
    assert len(fake_bitable.writes) == len(days)


def test_dry_run不碰Base(fake_bitable, tmp_path):
    _seed_board(fake_bitable, date(2026, 9, 16))
    xlsx = _make_xlsx(tmp_path, [_row(when="2026-09-17", uid="222")])
    before = fake_bitable.write_count

    assert incremental.run(_args("--file", str(xlsx), "--dry-run"), _settings(), fake_bitable) == 0

    assert fake_bitable.write_count == before
    assert fake_bitable.deleted == []


def test_导出里一行新加坡站都没有时拒绝导入(fake_bitable, tmp_path):
    xlsx = _make_xlsx(tmp_path, [_row(when="2026-09-17", station="香港站")])

    assert incremental.run(_args("--file", str(xlsx)), _settings(), fake_bitable) == 1

    assert fake_bitable.write_count == 0


def test_指定目录里找不到导出时给出下一步(tmp_path, capsys):
    args = _args("--export-dir", str(tmp_path))
    assert incremental.run(args, _settings(), None) == 1
    err = capsys.readouterr().err
    assert "--file" in err
    assert "--from-mail" in err


def test_文件不存在时报出路径(tmp_path, capsys):
    missing = tmp_path / "nope.xlsx"
    assert incremental.run(_args("--file", str(missing)), _settings(), None) == 1
    assert str(missing) in capsys.readouterr().err
