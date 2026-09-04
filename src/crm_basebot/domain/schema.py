"""表名和字段名的唯一出处。

所有业务代码引用这里的常量，不要在别处写字符串字面量。同事改了交易明细表的
列名时，只要改这一个文件，而且 ``assert_fields_present`` 会在算钱前先炸出来。

交易明细表的字段名来自实际截图，其余几张表是我们自己建的。
"""

from __future__ import annotations

from ..lark.bitable import (
    FIELD_TYPE_AUTO_NUMBER,
    FIELD_TYPE_DATETIME,
    FIELD_TYPE_NUMBER,
    FIELD_TYPE_SINGLE_LINK,
    FIELD_TYPE_SINGLE_SELECT,
    FIELD_TYPE_TEXT,
    FIELD_TYPE_USER,
)

# ---------- 表 1：渠道登记 ----------

TABLE_REFERRAL_NAME = "Referral Information"

REFERRAL_NO = "渠道编号"
REFERRAL_NAME = "渠道名称"
REFERRAL_EMAIL = "邮箱"
REFERRAL_ADDRESS = "地址"
REFERRAL_PAYMENT = "收款信息"
REFERRAL_RATE = "分佣比例"
REFERRAL_OWNER = "归属销售"
REFERRAL_OWNER_OPEN_ID = "登记人OpenID"
REFERRAL_STATUS = "状态"

# 只有这两种。没有「待审核」：销售登记完渠道直接生效，不设管理员过目这一步
# （2026-09-04 定的）。佣金计算也不看状态，停掉的渠道按业务约定根本不在数据里。
STATUS_ACTIVE = "生效"
STATUS_DISABLED = "停用"

REFERRAL_FIELDS: dict[str, int] = {
    REFERRAL_NO: FIELD_TYPE_AUTO_NUMBER,
    REFERRAL_NAME: FIELD_TYPE_TEXT,
    REFERRAL_EMAIL: FIELD_TYPE_TEXT,
    REFERRAL_ADDRESS: FIELD_TYPE_TEXT,
    REFERRAL_PAYMENT: FIELD_TYPE_TEXT,
    REFERRAL_RATE: FIELD_TYPE_NUMBER,
    REFERRAL_OWNER: FIELD_TYPE_USER,
    REFERRAL_OWNER_OPEN_ID: FIELD_TYPE_TEXT,
    REFERRAL_STATUS: FIELD_TYPE_SINGLE_SELECT,
}

# R + 3 位自增数字。递增由飞书系统保证，多个销售同时提交也不会撞号。
REFERRAL_NO_AUTO_SERIAL = {
    "type": "custom",
    "options": [
        {"type": "fixed_text", "value": "R"},
        {"type": "system_number", "value": "3"},
    ],
}

# ---------- 表 2：渠道介绍的客户 ----------

TABLE_CLIENT_NAME = "Referred Client"

CLIENT_UID = "客户UID"
CLIENT_NAME = "客户名称"
CLIENT_REFERRAL_LINK = "所属渠道"
CLIENT_OWNER = "归属销售"
CLIENT_OWNER_OPEN_ID = "登记人OpenID"

CLIENT_FIELDS: dict[str, int] = {
    # 必须是文本。18-19 位 UID 存成数字会在服务端就被 float64 抹平精度。
    CLIENT_UID: FIELD_TYPE_TEXT,
    CLIENT_NAME: FIELD_TYPE_TEXT,
    CLIENT_REFERRAL_LINK: FIELD_TYPE_SINGLE_LINK,
    CLIENT_OWNER: FIELD_TYPE_USER,
    CLIENT_OWNER_OPEN_ID: FIELD_TYPE_TEXT,
}

# ---------- 表 3：交易明细（同事维护，我们只读） ----------

TABLE_TRANSACTION_NAME = "Transaction Details"

TXN_ORDER_TIME = "订单时间"
TXN_ENTITY = "名称"
TXN_CLIENT_NAME = "客户名称"
TXN_CLIENT_UID = "客户UID"
TXN_QUANTITY = "HTS 获得数量"
TXN_PRICE = "价格"
TXN_FEE = "手续费"
TXN_FEE_CURRENCY = "手续费币种"
TXN_PNL = "Pnl(USD)"

# 算佣金真正依赖的三个字段。类型给 None 表示只要求存在 —— 客户UID 可能是文本，
# 也可能是查找引用，两种都能安全取值，但绝不能是数字。
TXN_REQUIRED_FIELDS: dict[str, int | None] = {
    TXN_ORDER_TIME: None,
    TXN_CLIENT_UID: None,
    TXN_PNL: FIELD_TYPE_NUMBER,
}

# ---------- 表 4：佣金汇总（按月，后端写入） ----------

TABLE_COMMISSION_NAME = "Commission Summary"

COMM_PERIOD = "结算月份"
COMM_REFERRAL_NO = "渠道编号"
COMM_REFERRAL_NAME = "渠道名称"
COMM_CLIENT_COUNT = "客户数"
COMM_TXN_COUNT = "交易笔数"
COMM_PNL_TOTAL = "Pnl合计"
COMM_RATE = "分佣比例"
COMM_PAYABLE = "应付佣金"
COMM_COMPUTED_AT = "计算时间"

COMMISSION_FIELDS: dict[str, int] = {
    COMM_PERIOD: FIELD_TYPE_TEXT,
    COMM_REFERRAL_NO: FIELD_TYPE_TEXT,
    COMM_REFERRAL_NAME: FIELD_TYPE_TEXT,
    COMM_CLIENT_COUNT: FIELD_TYPE_NUMBER,
    COMM_TXN_COUNT: FIELD_TYPE_NUMBER,
    COMM_PNL_TOTAL: FIELD_TYPE_NUMBER,
    COMM_RATE: FIELD_TYPE_NUMBER,
    COMM_PAYABLE: FIELD_TYPE_NUMBER,
    COMM_COMPUTED_AT: FIELD_TYPE_DATETIME,
}

# ---------- 表 5：审计日志（只增不改） ----------

TABLE_AUDIT_NAME = "Audit Log"

AUDIT_AT = "时间"
AUDIT_ACTOR_OPEN_ID = "操作人OpenID"
AUDIT_ACTOR_NAME = "操作人"
AUDIT_ACTION = "动作"
AUDIT_TARGET_TABLE = "目标表"
AUDIT_TARGET_RECORD = "目标记录"
AUDIT_DETAIL = "详情"

AUDIT_FIELDS: dict[str, int] = {
    AUDIT_AT: FIELD_TYPE_DATETIME,
    AUDIT_ACTOR_OPEN_ID: FIELD_TYPE_TEXT,
    AUDIT_ACTOR_NAME: FIELD_TYPE_TEXT,
    AUDIT_ACTION: FIELD_TYPE_TEXT,
    AUDIT_TARGET_TABLE: FIELD_TYPE_TEXT,
    AUDIT_TARGET_RECORD: FIELD_TYPE_TEXT,
    AUDIT_DETAIL: FIELD_TYPE_TEXT,
}

# ---------- 表 6：销售名册（open_id 到身份的映射） ----------

TABLE_SALES_NAME = "Sales Directory"

SALES_OPEN_ID = "OpenID"
SALES_NAME = "姓名"
SALES_ROLE = "角色"
SALES_STATUS = "状态"

ROLE_SALES = "销售"
ROLE_ADMIN = "管理员"

SALES_STATUS_ACTIVE = "在职"
SALES_STATUS_DISABLED = "停用"

SALES_FIELDS: dict[str, int] = {
    SALES_OPEN_ID: FIELD_TYPE_TEXT,
    SALES_NAME: FIELD_TYPE_TEXT,
    SALES_ROLE: FIELD_TYPE_SINGLE_SELECT,
    SALES_STATUS: FIELD_TYPE_SINGLE_SELECT,
}
