"""名册填 OpenID 的脚本。

这条路的代价很容易被低估：机器人「谁都不认」时，最省事的做法是去放宽鉴权（比如名册里
没有也放行）。这些测试盯的是相反的方向 —— 只写一个明确的人、形状不对不写、重名不猜，
以及写完要回读确认。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from crm_basebot.domain import schema

from .conftest import TBL_SALES


def _load_module():
    """scripts/ 不是包，按路径加载。"""
    path = Path(__file__).resolve().parent.parent / "scripts" / "set_sales_open_id.py"
    spec = importlib.util.spec_from_file_location("set_sales_open_id", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _load_module()

JAMES = "ou_0000000000000000000000000000ja"


def _settings() -> SimpleNamespace:
    return SimpleNamespace(table_sales=TBL_SALES)


def _args(*extra: str):
    return script.build_parser().parse_args(list(extra))


def _seed(fake_bitable, name: str, open_id: str = "", uid: str = "rec1") -> str:
    return fake_bitable.table(TBL_SALES).add_existing(
        {schema.SALES_NAME: name, schema.SALES_OPEN_ID: open_id}
    )


# ---------- 姓名归一 ----------


def test_姓名归一忽略空白和大小写():
    assert script.normalize("  James   YANG ") == script.normalize("james yang")


def test_姓名归一认全角空格():
    assert script.normalize("Kevin\u3000Yu") == script.normalize("Kevin Yu")


def test_按姓名找人不看大小写和多余空格(fake_bitable):
    _seed(fake_bitable, "James YANG")
    roster = script.load_roster(fake_bitable, TBL_SALES)
    assert len(script.find_by_name(roster, "james  yang")) == 1


def test_找不到就是空列表(fake_bitable):
    _seed(fake_bitable, "James YANG")
    roster = script.load_roster(fake_bitable, TBL_SALES)
    assert script.find_by_name(roster, "Nobody") == []


# ---------- 写入 ----------


def test_apply时才真写并回读确认(fake_bitable, capsys):
    record_id = _seed(fake_bitable, "James YANG")
    args = _args("--name", "James YANG", "--open-id", JAMES, "--apply")

    assert script.run(args, _settings(), fake_bitable) == 0

    assert fake_bitable.table(TBL_SALES).records[record_id][schema.SALES_OPEN_ID] == JAMES
    assert "已写入并回读确认" in capsys.readouterr().out


def test_不加apply一个写都不发(fake_bitable):
    _seed(fake_bitable, "James YANG")
    args = _args("--name", "James YANG", "--open-id", JAMES)

    assert script.run(args, _settings(), fake_bitable) == 0

    assert fake_bitable.updates == []


def test_list只读不写(fake_bitable):
    _seed(fake_bitable, "James YANG")

    assert script.run(_args("--list"), _settings(), fake_bitable) == 0

    assert fake_bitable.updates == []


def test_open_id形状不对时拒绝写入(fake_bitable, capsys):
    """把 user_id（u_）或 union_id（on_）填进来，机器人永远认不出这个人。"""
    _seed(fake_bitable, "James YANG")
    args = _args("--name", "James YANG", "--open-id", "u_not_an_open_id", "--apply")

    assert script.run(args, _settings(), fake_bitable) == 1

    assert fake_bitable.updates == []
    assert "形状不对" in capsys.readouterr().err


def test_重名时拒绝猜(fake_bitable, capsys):
    _seed(fake_bitable, "James YANG", uid="rec1")
    _seed(fake_bitable, "james yang", uid="rec2")
    args = _args("--name", "James YANG", "--open-id", JAMES, "--apply")

    assert script.run(args, _settings(), fake_bitable) == 1

    assert fake_bitable.updates == []
    assert "不敢猜" in capsys.readouterr().err


def test_名册里没这个人时拒绝写入(fake_bitable, capsys):
    _seed(fake_bitable, "Prance Wang")
    args = _args("--name", "James YANG", "--open-id", JAMES, "--apply")

    assert script.run(args, _settings(), fake_bitable) == 1

    assert fake_bitable.updates == []
    assert "没有叫" in capsys.readouterr().err


def test_已经是这个值时重复跑不写(fake_bitable, capsys):
    _seed(fake_bitable, "James YANG", open_id=JAMES)
    args = _args("--name", "James YANG", "--open-id", JAMES, "--apply")

    assert script.run(args, _settings(), fake_bitable) == 0

    assert fake_bitable.updates == []
    assert "已经是这个值" in capsys.readouterr().out
