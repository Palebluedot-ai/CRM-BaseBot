"""ECAS 导入：读表头、判读比例、以及「判不出来就整次拒绝」这条。

最要紧的一条是**不许部分导入**。ECAS 这张表的全部价值在于合计对得上来源表
（2026-08 是 65,000.00，和财务一分不差）。跳过一行算不出来的，合计就悄悄变小了，
而变小之后没有任何地方会报警。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from openpyxl import Workbook

from crm_basebot.domain import ecas
from crm_basebot.lark.bitable import FIELD_TYPE_FORMULA


def _load_module():
    """scripts/ 不是包，按路径加载。"""
    path = Path(__file__).resolve().parent.parent / "scripts" / "import_ecas.py"
    spec = importlib.util.spec_from_file_location("import_ecas", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


importer = _load_module()
SG = ZoneInfo("Asia/Singapore")

# 来源表的表头，逐字照抄 2026-09 那份「Wallet and Trades」的 ECAS 分页，顺序也照抄。
# 故意写成字面量而不是从脚本里读：脚本被改错了，这里要能红。
REAL_HEADERS = [
    "Client Name",
    "ECAS Revenue",
    "Application Time",
    "Text 7",
    "Sales in Charge",
    "UID",
    "Referrer",
    "%",
    "Amount of Referral Fee",
    "test",
]


def _sheet(tmp_path: Path, rows: list[list], headers: list[str] | None = None) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "ECAS"
    sheet.append(headers if headers is not None else REAL_HEADERS)
    for row in rows:
        sheet.append(row)
    path = tmp_path / "ecas.xlsx"
    workbook.save(path)
    return path


def _row(
    client="XUSHENG TRADING LIMITED",
    revenue="5000",
    when=datetime(2026, 8, 10),
    sales="Prance Wang",
    uid=None,
    referrer="JIANG JUN",
    rate="0.5",
    fee="2500",
):
    # 来源表里数字都是**文本形态**，照着来
    return [client, revenue, when, None, sales, uid, referrer, rate, fee, fee]


# ---------- 表头 ----------


def test_缺一列就拒绝而不是猜列位(tmp_path):
    headers = [h for h in REAL_HEADERS if h != "Amount of Referral Fee"]
    path = _sheet(tmp_path, [_row()[:8] + [None]], headers=headers)

    with pytest.raises(importer.EcasImportError) as exc_info:
        importer.parse_sheet(path, "ECAS")
    assert "Amount of Referral Fee" in str(exc_info.value)


def test_没有数据行时拒绝(tmp_path):
    path = _sheet(tmp_path, [])
    with pytest.raises(importer.EcasImportError):
        importer.parse_sheet(path, "ECAS")


# ---------- 比例判读 ----------


def test_小数写法的比例换算成百分数(tmp_path):
    parsed = importer.parse_sheet(_sheet(tmp_path, [_row()]), "ECAS")
    (row,) = parsed.rows
    assert row.rate_percent == Decimal("50.0")
    assert row.stated_fee == Decimal("2500")


def test_比例判不出来时整次拒绝一行都不导(tmp_path):
    path = _sheet(
        tmp_path,
        [
            _row(client="好的一行"),
            _row(client="坏的一行", fee="1234"),
            _row(client="另一个好的"),
        ],
    )
    with pytest.raises(importer.EcasImportError) as exc_info:
        importer.parse_sheet(path, "ECAS")
    message = str(exc_info.value)
    assert "坏的一行" in message
    # 「跳过坏的、导好的」正是这里不做的事
    assert "整次导入没有进行" in message


def test_介绍人栏填成栏位标题时照样导入但不算返佣(tmp_path):
    # 真实数据里就有这么一行：Gong Ming 的 Referrer 栏写着「Referrer」
    path = _sheet(tmp_path, [_row(client="Gong Ming", referrer="Referrer", rate="0.1", fee="600")])
    parsed = importer.parse_sheet(path, "ECAS")
    (row,) = parsed.rows
    assert row.client_name == "Gong Ming"
    assert row.referrer == ""
    assert row.rate_percent is None
    assert "不是渠道名" in parsed.warnings[0]


def test_有介绍人但没填比例时不算返佣并且报出来(tmp_path):
    path = _sheet(tmp_path, [_row(rate=None, fee="0")])
    parsed = importer.parse_sheet(path, "ECAS")
    assert parsed.rows[0].rate_percent is None
    assert "没填比例" in parsed.warnings[0]


def test_没有介绍人的申请照样进表(tmp_path):
    # 一百多笔申请里六成没有介绍人。它们是这张表作为「开户申请全集」的一部分。
    path = _sheet(tmp_path, [_row(referrer=None, rate=None, fee="0")])
    parsed = importer.parse_sheet(path, "ECAS")
    assert len(parsed.rows) == 1
    assert parsed.rows[0].referrer == ""


# ---------- UID ----------


def test_被Excel改坏的UID留空而不是写进去(tmp_path):
    # 尾巴一串 0 = 被当成数字存过，低位已经没了。一个看不出坏掉的坏 UID
    # 比一个空格危险得多 —— 它会静默匹配到别的客户，或者永远匹配不上。
    path = _sheet(tmp_path, [_row(uid="2259494339016480000")])
    parsed = importer.parse_sheet(path, "ECAS")
    assert parsed.rows[0].uid == ""
    assert "像被 Excel 抹过低位" in parsed.warnings[0]


def test_好的UID原样保留(tmp_path):
    path = _sheet(tmp_path, [_row(uid="2103059041345425920")])
    parsed = importer.parse_sheet(path, "ECAS")
    # 这个值本身尾数是 920，不是一串 0，判定为健康
    assert parsed.rows[0].uid == "2103059041345425920"


def test_UID是浮点数时直接报错(tmp_path):
    from crm_basebot.lark.values import PrecisionLossError

    path = _sheet(tmp_path, [_row(uid=2.259494339016480e18)])
    with pytest.raises((importer.EcasImportError, PrecisionLossError, Exception)):
        importer.parse_sheet(path, "ECAS")


# ---------- 申请时间 ----------


def test_申请时间不是日期时拒绝(tmp_path):
    path = _sheet(tmp_path, [_row(when="八月十号")])
    with pytest.raises(importer.EcasImportError) as exc_info:
        importer.parse_sheet(path, "ECAS")
    assert "归不了月" in str(exc_info.value)


# ---------- 写进 Base 的字段 ----------


def test_公式列不进写入负载(tmp_path):
    # 公式字段写进去平台会拒，整批一起挂
    parsed = importer.parse_sheet(_sheet(tmp_path, [_row()]), "ECAS")
    payload = importer.build_payload(parsed.rows[0], "recRef", "文本", SG)
    formulas = {n for n, t in ecas.ECAS_FIELDS.items() if t == FIELD_TYPE_FORMULA}
    assert formulas & payload.keys() == set()


def test_主字段也写上客户名(tmp_path):
    # 不写的话界面上每行第一格都是空的，整张表看起来像「无标题记录」
    parsed = importer.parse_sheet(_sheet(tmp_path, [_row(client="A")]), "ECAS")
    payload = importer.build_payload(parsed.rows[0], "recRef", "文本", SG)
    assert payload["文本"] == "A"


def test_申请时间按业务时区存不是UTC(tmp_path):
    parsed = importer.parse_sheet(
        _sheet(tmp_path, [_row(when=datetime(2026, 9, 1, 0, 30))]), "ECAS"
    )
    payload = importer.build_payload(parsed.rows[0], "recRef", "文本", SG)
    stored = datetime.fromtimestamp(payload[ecas.ECAS_APPLIED_AT] / 1000, tz=SG)
    assert stored == datetime(2026, 9, 1, 0, 30, tzinfo=SG)


def test_没挂上渠道时不写关联字段(tmp_path):
    parsed = importer.parse_sheet(_sheet(tmp_path, [_row()]), "ECAS")
    payload = importer.build_payload(parsed.rows[0], "", "文本", SG)
    assert ecas.ECAS_REFERRAL_LINK not in payload
    # 但名字照写，结算时靠它认收款人
    assert payload[ecas.ECAS_REFERRER_NAME] == "JIANG JUN"
