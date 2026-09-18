"""客户UID 的精度保护。

这些用的是截图里的真实 UID：18 位和 19 位，都超过 float64 的精确整数上限。
如果哪天有人「顺手优化」把 UID 转成数字，这里会立刻炸。
"""

import pytest

from crm_basebot.lark.values import (
    MAX_EXACT_INT,
    PrecisionLossError,
    extract_text,
    link_ids,
    to_number,
    to_uid,
)

UID_18 = "577809207768677761"
UID_19 = "2141293991366272768"


# ---------- 关联字段的返回形态 ----------
#
# 坑（2026-09-18 踩过）：空关联读回来是 {"link_record_ids": None}，它本身是真的。
# 拿它判断「这行挂上渠道了吗」，会把**每一行**都报成已挂，看着一切正常，其实一行没挂。


def test_空关联不算挂上了():
    assert link_ids({"link_record_ids": None}) == []
    assert link_ids({}) == []
    assert link_ids(None) == []
    assert link_ids([]) == []


@pytest.mark.parametrize(
    "raw",
    [
        ["rec001"],
        {"link_record_ids": ["rec001"]},
        [{"record_id": "rec001"}],
        [{"id": "rec001"}],
    ],
)
def test_几种返回形态都抽得出记录id(raw):
    """写入时给列表，读回来可能是 link_record_ids，也可能是 record_id 对象。"""
    assert link_ids(raw) == ["rec001"]


def test_多条关联一条都不漏():
    assert link_ids({"link_record_ids": ["a", "b"]}) == ["a", "b"]


def test_真实uid确实超出float精确范围():
    # 前提假设成立才谈得上后面的保护
    assert int(UID_18) > MAX_EXACT_INT
    assert int(UID_19) > MAX_EXACT_INT
    # 这一行就是我们要防的事故
    assert str(int(float(UID_18))) != UID_18


@pytest.mark.parametrize("uid", [UID_18, UID_19])
def test_字符串uid原样保留(uid):
    assert to_uid(uid) == uid


@pytest.mark.parametrize("uid", [UID_18, UID_19])
def test_大整数uid不丢精度(uid):
    # Python 的 int 是任意精度，从 JSON 整数字面量解析出来是准确的
    assert to_uid(int(uid)) == uid


@pytest.mark.parametrize("uid", [UID_18, UID_19])
def test_查找引用数组里的uid(uid):
    assert to_uid([{"type": "text", "text": uid}]) == uid


def test_uid两边空白被去掉():
    assert to_uid(f"  {UID_18}\n") == UID_18


def test_浮点uid直接报错而不是凑合():
    with pytest.raises(PrecisionLossError, match="精度"):
        to_uid(5.778092077686778e17)


def test_多值uid拒绝猜测():
    with pytest.raises(ValueError, match="多个值"):
        to_uid([{"text": UID_18}, {"text": UID_19}])


def test_空uid返回空串():
    assert to_uid(None) == ""
    assert to_uid([]) == ""


@pytest.mark.parametrize("uid", [UID_18, UID_19])
def test_uid在字典和数组之间往返不变(uid):
    """模拟一条记录写进去再读出来的完整链路。"""
    written = to_uid(uid)
    read_back = to_uid([{"type": "text", "text": written}])
    assert read_back == uid


def test_客户名称抽取():
    assert extract_text("PLUTO STUDIO LIMITED") == "PLUTO STUDIO LIMITED"
    assert extract_text([{"type": "text", "text": "HOMEX AND AI PTE. LTD."}]) == (
        "HOMEX AND AI PTE. LTD."
    )


def test_pnl金额转换():
    assert to_number(729.99) == pytest.approx(729.99)
    assert to_number("6,805.40") == pytest.approx(6805.40)
    assert to_number([{"type": "number", "value": 4231.31}]) == pytest.approx(4231.31)


def test_pnl空值和零要分得清():
    assert to_number(None) is None
    assert to_number("") is None
    assert to_number(0) == 0.0
