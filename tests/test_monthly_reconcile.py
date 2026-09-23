"""每月结算：两套账、一张卡。

这个任务每月只跑一次，出了错要等一个月才有下一次机会，所以这里盯的是那些
「跑完看起来一切正常、其实少报了钱」的情况：

1. **两项合计**。少看一个数就少开一张发票 —— 2026-08 交易佣金 19,294.51、
   ECAS 65,000.00，只看前者会漏掉四分之三。
2. **失败不能和「这个月就是零」长成一样**。
3. **同一个月跑第二次不许重复写**，也不许因此报成失败。
4. **ECAS 数据的截止日期**。它不是每天自动导的，少导一个月在金额上看不出来。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

from crm_basebot.domain import ecas, schema

from .conftest import TBL_COMMISSION, TBL_ECAS, TBL_ECAS_COMMISSION, TBL_SALES


def _load_module():
    """scripts/ 不是包，按路径加载。"""
    path = Path(__file__).resolve().parent.parent / "scripts" / "monthly_reconcile.py"
    spec = importlib.util.spec_from_file_location("monthly_reconcile", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


job = _load_module()


def _book(label="交易佣金", table="Commission Summary", **kwargs):
    return job.Book(label=label, table_name=table, **kwargs)


def _card_text(*books) -> str:
    return job.build_card("2026-08", list(books))["body"]["elements"][0]["content"]


def _template(*books) -> str:
    return job.build_card("2026-08", list(books))["header"]["template"]


# ---------- 结算哪个月 ----------


def test_默认结算上个月():
    assert job.previous_period(date(2026, 9, 3)) == "2026-08"


def test_一月三号结算的是去年十二月():
    assert job.previous_period(date(2026, 1, 3)) == "2025-12"


# ---------- 卡片：两项合计 ----------


def test_两套账都结出来时给两项合计():
    """这行是整张卡存在的主要理由。"""
    text = _card_text(
        _book(count=4, total=19294.51),
        _book("ECAS 开户返佣", "ECAS Commission Summary", count=2, total=65000.0),
    )
    assert "19,294.51" in text
    assert "65,000.00" in text
    assert "两项合计  84,294.51 USD" in text


def test_只有一套账时不给合计():
    """一个数后面再写一遍「合计」，只会让人以为漏了什么。"""
    assert "两项合计" not in _card_text(_book(count=4, total=19294.51))


def test_失败的那套不计入合计并且明说():
    """「少算了」和「这个月就是这么少」不能长成一样。"""
    text = _card_text(
        _book(count=4, total=19294.51),
        _book("ECAS 开户返佣", "ECAS Commission Summary", failed=True),
    )
    assert "这次没有算出来" in text
    assert "两项合计" not in text  # 只剩一套结出来了
    assert "不含" in text


def test_失败时卡片换颜色():
    """绿色卡片会被当成「都好了」扫过去。"""
    assert _template(_book(total=1.0), _book("ECAS", "T", failed=True)) == "orange"
    assert _template(_book(total=1.0)) == "green"
    assert _template(_book(total=1.0, already=True)) == "blue"


def test_已结算过的说明是本来就有不是这次写的():
    text = _card_text(_book(count=4, total=19294.51, already=True))
    assert "之前已经结算过" in text
    assert "19,294.51" in text  # 数字照样要给 —— 人要的是金额，不是「没事做」


def test_备注跟在对应那套账下面():
    text = _card_text(
        _book(count=4, total=1.0, note="另有 29 个客户没登记归属渠道"),
        _book("ECAS 开户返佣", "ECAS Commission Summary", count=2, total=2.0, note="⚠️ 只到 08-12"),
    )
    assert text.index("29 个客户") < text.index("ECAS 开户返佣")
    assert text.index("ECAS 开户返佣") < text.index("只到 08-12")


# ---------- 从 Base 读回来 ----------


def test_只读回本月的行(fake_bitable):
    table = fake_bitable.tables[TBL_COMMISSION]
    table.add_existing({schema.COMM_PERIOD: "2026-08", schema.COMM_PAYABLE: 100.0})
    table.add_existing({schema.COMM_PERIOD: "2026-08", schema.COMM_PAYABLE: 50.5})
    table.add_existing({schema.COMM_PERIOD: "2026-07", schema.COMM_PAYABLE: 999.0})

    assert job.read_summary(fake_bitable, TBL_COMMISSION, "2026-08") == (2, 150.5)


def test_ECAS汇总单独一个读法(fake_bitable):
    """两张表的列名恰好一样是巧合，不是约定。"""
    table = fake_bitable.tables[TBL_ECAS_COMMISSION]
    table.add_existing({ecas.ECOMM_PERIOD: "2026-08", ecas.ECOMM_PAYABLE: 65000.0})
    table.add_existing({ecas.ECOMM_PERIOD: "2026-07", ecas.ECOMM_PAYABLE: 1.0})

    assert job.read_ecas_summary(fake_bitable, TBL_ECAS_COMMISSION, "2026-08") == (1, 65000.0)


# ---------- ECAS 数据的截止日期 ----------


def _application(fake_bitable, when_ms: int) -> None:
    fake_bitable.tables[TBL_ECAS].add_existing({ecas.ECAS_APPLIED_AT: when_ms})


def _ms(day: date) -> int:
    from zoneinfo import ZoneInfo

    from crm_basebot.domain.dates import date_to_ms

    return date_to_ms(day, tz=ZoneInfo("Asia/Singapore"))


def test_申请表还没导到结算月末时提醒(fake_bitable):
    """「这个月只结出 5,000」和「这个月的申请还没导进来」在金额上一模一样。"""
    _application(fake_bitable, _ms(date(2026, 8, 12)))
    note = job.ecas_freshness(fake_bitable, TBL_ECAS, "2026-08", timezone_name="Asia/Singapore")
    assert "2026-08-12" in note
    assert "import_ecas" in note


def test_申请表已经导到结算月之后就不提醒(fake_bitable):
    _application(fake_bitable, _ms(date(2026, 9, 20)))
    assert (
        job.ecas_freshness(fake_bitable, TBL_ECAS, "2026-08", timezone_name="Asia/Singapore") == ""
    )


def test_申请表是空的直接说(fake_bitable):
    note = job.ecas_freshness(fake_bitable, TBL_ECAS, "2026-08", timezone_name="Asia/Singapore")
    assert "是空的" in note


def test_读截止日期失败不拖垮整个月结(fake_bitable, monkeypatch):
    """提醒取不到无所谓；为了它让月结失败是本末倒置。"""

    def boom(*_args, **_kwargs):
        raise RuntimeError("读不了")

    monkeypatch.setattr(job, "latest_applied_date", boom)
    assert (
        job.ecas_freshness(fake_bitable, TBL_ECAS, "2026-08", timezone_name="Asia/Singapore") == ""
    )


# ---------- settle：驱动一套账 ----------


class _FakeModule:
    """冒充 jobs.reconcile / jobs.ecas_reconcile。两者的 main() 签名刻意一致。"""

    def __init__(self, code: int = 0, output: str = "", on_write=None) -> None:
        self.code = code
        self.output = output
        self.calls: list[list[str]] = []
        self._on_write = on_write

    def main(self, argv):
        self.calls.append(list(argv))
        print(self.output, end="")
        if "--write" in argv and self._on_write:
            self._on_write()
        return self.code


def test_预演时不传write(fake_bitable):
    module = _FakeModule()
    job.settle(
        "交易佣金",
        "Commission Summary",
        module=module,
        bitable=fake_bitable,
        table_id=TBL_COMMISSION,
        period="2026-08",
        apply=False,
        read_back=job.read_summary,
    )
    assert module.calls == [["--period", "2026-08"]]


def test_写入后的数字是从Base读回来的不是模块报的(fake_bitable):
    """写接口返回成功不等于值进去了。"""

    def write():
        fake_bitable.tables[TBL_COMMISSION].add_existing(
            {schema.COMM_PERIOD: "2026-08", schema.COMM_PAYABLE: 19294.51}
        )

    book, _ = job.settle(
        "交易佣金",
        "Commission Summary",
        module=_FakeModule(on_write=write),
        bitable=fake_bitable,
        table_id=TBL_COMMISSION,
        period="2026-08",
        apply=True,
        read_back=job.read_summary,
    )
    assert (book.count, book.total) == (1, 19294.51)
    assert not book.failed


def test_这个月本来就有汇总时算已结算不算失败(fake_bitable):
    """reconcile 拒绝重复写会返回非 0 —— 那是幂等，不是故障。
    当成失败的话，launchd 每个月报一次假告警，久了就没人看了。"""
    fake_bitable.tables[TBL_COMMISSION].add_existing(
        {schema.COMM_PERIOD: "2026-08", schema.COMM_PAYABLE: 19294.51}
    )

    book, _ = job.settle(
        "交易佣金",
        "Commission Summary",
        module=_FakeModule(code=1),
        bitable=fake_bitable,
        table_id=TBL_COMMISSION,
        period="2026-08",
        apply=True,
        read_back=job.read_summary,
    )
    assert book.already
    assert not book.failed
    assert book.total == 19294.51


def test_本月没有旧汇总而模块又失败才算失败(fake_bitable):
    book, _ = job.settle(
        "交易佣金",
        "Commission Summary",
        module=_FakeModule(code=1),
        bitable=fake_bitable,
        table_id=TBL_COMMISSION,
        period="2026-08",
        apply=True,
        read_back=job.read_summary,
    )
    assert book.failed


# ---------- 整条路 ----------


class _Settings:
    base_app_token = "app"
    table_commission = TBL_COMMISSION
    table_sales = TBL_SALES
    table_ecas = TBL_ECAS
    table_ecas_commission = TBL_ECAS_COMMISSION
    business_timezone = "Asia/Singapore"


def _wire(monkeypatch, fake_bitable, *, ecas_code=0, settings=None):
    settings = settings or _Settings()
    monkeypatch.setattr(job, "load_settings", lambda: settings)
    monkeypatch.setattr(job, "require_settings", lambda *a, **k: None)
    monkeypatch.setattr(job, "BitableClient", lambda _token: fake_bitable)
    monkeypatch.setattr(job, "reconcile", _FakeModule())
    monkeypatch.setattr(job, "ecas_reconcile", _FakeModule(code=ecas_code))
    sent: list[dict] = []
    monkeypatch.setattr(job, "get_client", lambda: object())
    monkeypatch.setattr(job, "send_card", lambda _c, _o, card: sent.append(card) or True)
    return sent


def test_预演不写不发(monkeypatch, fake_bitable, capsys):
    sent = _wire(monkeypatch, fake_bitable)
    assert job.main(["--period", "2026-08"]) == 0
    assert sent == []
    assert "预演" in capsys.readouterr().out


def test_没配ECAS表时整节跳过(monkeypatch, fake_bitable, capsys):
    class NoEcas(_Settings):
        table_ecas = ""
        table_ecas_commission = ""

    _wire(monkeypatch, fake_bitable, settings=NoEcas())
    job.main(["--period", "2026-08"])
    out = capsys.readouterr().out
    assert "没配，跳过" in out


def test_skip_ecas只结交易佣金(monkeypatch, fake_bitable, capsys):
    _wire(monkeypatch, fake_bitable)
    job.main(["--period", "2026-08", "--skip-ecas"])
    assert "--skip-ecas，跳过" in capsys.readouterr().out


def test_ECAS失败时交易佣金那半照发并且退出码非零(monkeypatch, fake_bitable):
    """那个数是真的，照发；但退出码要如实说有一套没结成。"""
    sent = _wire(monkeypatch, fake_bitable, ecas_code=1)
    fake_bitable.tables[TBL_SALES].add_existing(
        {
            schema.SALES_OPEN_ID: "ou_admin",
            schema.SALES_NAME: "Admin",
            schema.SALES_ROLE: schema.ROLE_ADMIN,
            schema.SALES_STATUS: schema.SALES_STATUS_ACTIVE,
        }
    )

    assert job.main(["--period", "2026-08", "--apply"]) == 1
    (card,) = sent
    text = card["body"]["elements"][0]["content"]
    assert "交易佣金" in text
    assert "这次没有算出来" in text
    assert card["header"]["template"] == "orange"


def test_交易佣金失败时不发通知(monkeypatch, fake_bitable):
    """它是这个任务的主要产出，它没了这张卡没什么好报的。"""
    sent = _wire(monkeypatch, fake_bitable)
    monkeypatch.setattr(job, "reconcile", _FakeModule(code=1))
    assert job.main(["--period", "2026-08", "--apply"]) == 1
    assert sent == []


def test_名册里没有管理员时说清楚为什么没发(monkeypatch, fake_bitable, capsys):
    sent = _wire(monkeypatch, fake_bitable)
    assert job.main(["--period", "2026-08", "--apply"]) == 0
    assert sent == []
    assert "Sales Directory" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--no-notify"])
def test_no_notify时不发但照样写(monkeypatch, fake_bitable, flag):
    sent = _wire(monkeypatch, fake_bitable)
    assert job.main(["--period", "2026-08", "--apply", flag]) == 0
    assert sent == []
