"""Bitable 字段类型码到人话的映射。"""

from __future__ import annotations

FIELD_TYPE_NAMES: dict[int, str] = {
    1: "文本",
    2: "数字",
    3: "单选",
    4: "多选",
    5: "日期",
    7: "复选框",
    11: "人员",
    13: "电话号码",
    15: "超链接",
    17: "附件",
    18: "单向关联",
    19: "查找引用",
    20: "公式",
    21: "双向关联",
    22: "地理位置",
    23: "群组",
    24: "流程",
    1001: "创建时间",
    1002: "最后更新时间",
    1003: "创建人",
    1004: "修改人",
    1005: "自动编号",
    3001: "按钮",
}


def type_name(type_code: int) -> str:
    return FIELD_TYPE_NAMES.get(type_code, f"未知({type_code})")
