"""月结存档：客户明细加起来一分不差等于结算、存过的不再写。"""

from __future__ import annotations

from decimal import Decimal as D
from types import SimpleNamespace

from crm_basebot.jobs import archive as arc
from crm_basebot.lark.bitable import Record, TableInfo


def _settled(no="R076", amount="5627.79", kind=arc.KIND_TRADE, base="18759.29"):
    return arc.Settled(kind, no, "DAI CANGWEI", 2, D(base), "30.00%", D(amount))


def test_现算正好等于结算就照用():
    parts = [
        arc.Contribution("A", "1", D("100"), D("30.00")),
        arc.Contribution("B", "2", D("50"), D("15.00")),
    ]
    rows = arc.split(_settled(amount="45.00", base="150"), parts)
    assert [(r.client, r.amount) for r in rows] == [("A", D("30.00")), ("B", D("15.00"))]


def test_对不上就按收入从结算数分_加起来一分不差():
    # 结算时按 30%，现在比例改成 50%：现算是 50/25，结算只有 30.01
    parts = [
        arc.Contribution("A", "1", D("100"), D("50.00")),
        arc.Contribution("B", "2", D("50"), D("25.00")),
        arc.Contribution("C", "3", D("0"), D("0")),  # 这个月没收入的客户不列
    ]
    rows = arc.split(_settled(amount="30.01", base="150"), parts)
    assert [r.client for r in rows] == ["A", "B"]
    assert sum(r.amount for r in rows) == D("30.01")
    assert all(r.rate == "30.00%" for r in rows)  # 比例写结算时的


def test_一个客户都找不到就写一行总额():
    (row,) = arc.split(_settled(), [])
    assert row.client == arc.NO_CLIENT and row.amount == D("5627.79")


def test_结算是0的渠道不列明细_ECAS没编号的按名字对():
    summary = [
        _settled(no="R001", amount="0"),
        arc.Settled(arc.KIND_ECAS, "", "Unknown Co", 1, D("5000"), "50%", D("2500.00")),
    ]
    contributions = {
        (arc.KIND_ECAS, "?Unknown Co"): [arc.Contribution("X", "", D("5000"), D("2500.00"))]
    }
    (row,) = arc.build_details(summary, contributions)
    assert (row.kind, row.client, row.amount) == (arc.KIND_ECAS, "X", D("2500.00"))


class _Resp:
    def __init__(self, table_id):
        self.data = SimpleNamespace(table_id=table_id)
        self.code, self.msg = 0, "ok"

    def success(self):
        return True


class _Client:
    """只接 app_table.create。建一张就在假 Base 里挂一张空表。"""

    def __init__(self, base):
        self.base = base
        self.bitable = self.v1 = self.app_table = self
        self.created: list[tuple[str, list[str]]] = []

    def create(self, request):
        table = request.request_body.table
        table_id = f"tbl{len(self.base.tables_by_name) + 1}"
        self.base.tables_by_name[table.name] = table_id
        self.base.rows[table_id] = []
        self.created.append((table.name, [f.field_name for f in table.fields]))
        return _Resp(table_id)


class _Base:
    def __init__(self):
        self.tables_by_name: dict[str, str] = {}
        self.rows: dict[str, list[dict]] = {}

    def list_tables(self):
        return [TableInfo(table_id=i, name=n) for n, i in self.tables_by_name.items()]

    def iter_records(self, table_id, **_):
        for index, fields in enumerate(self.rows[table_id]):
            yield Record(record_id=f"r{index}", fields=fields)

    def batch_create_records(self, table_id, records):
        self.rows[table_id].extend(records)
        return len(records)


def _snapshot(period="2026-09"):
    summary = [_settled(amount="45.00", base="150")]
    details = arc.split(summary[0], [arc.Contribution("A", "1", D("150"), D("45.00"))])
    return arc.Snapshot(period, summary, details)


def test_建两张月表加一张总表_第一列是结算月份():
    base = _Base()
    client = _Client(base)
    result = arc.write_archive(base, client, "app", _snapshot())

    assert result.created == ["2026-09 结算明细", "2026-09 结算汇总"]
    assert result.appended == 1
    names = [name for name, _ in client.created]
    assert names == ["2026-09 结算明细", "2026-09 结算汇总", arc.CUMULATIVE_TABLE]
    assert all(columns[0] == arc.F_PERIOD for _, columns in client.created)
    (detail,) = base.rows[base.tables_by_name["2026-09 结算明细"]]
    assert detail[arc.F_AMOUNT] == 45.0 and detail[arc.F_CLIENT] == "A"
    assert "拖进 Archive" in result.line


def test_存过的不再写_下个月只追加总表():
    base = _Base()
    client = _Client(base)
    arc.write_archive(base, client, "app", _snapshot())
    again = arc.write_archive(base, client, "app", _snapshot())
    assert again.created == [] and again.appended == 0
    assert "之前已经存过了" in again.line

    arc.write_archive(base, client, "app", _snapshot("2026-10"))
    cumulative = base.rows[base.tables_by_name[arc.CUMULATIVE_TABLE]]
    assert [r[arc.F_PERIOD] for r in cumulative] == ["2026-09", "2026-10"]
