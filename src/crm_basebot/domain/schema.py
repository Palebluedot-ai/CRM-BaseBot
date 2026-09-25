"""表名和字段名的唯一出处。

所有业务代码引用这里的常量，不要在别处写字符串字面量。同事改了交易明细表的
列名时，只要改这一个文件，而且 ``assert_fields_present`` 会在算钱前先炸出来。

交易明细表的字段名来自实际截图，其余几张表是我们自己建的。
"""

from __future__ import annotations

from ..lark.bitable import (
    FIELD_TYPE_DATETIME,
    FIELD_TYPE_FORMULA,
    FIELD_TYPE_NUMBER,
    FIELD_TYPE_SINGLE_LINK,
    FIELD_TYPE_SINGLE_SELECT,
    FIELD_TYPE_TEXT,
    FIELD_TYPE_USER,
)

# ---------- 表 1：渠道登记 ----------
#
# 列对齐 2026-09-17 给的模板「Referral Registration」：Referral Code、Name、Email、
# Start Date、Commission Rate、Payout Frequency、Submitted On、Sales In Charge。
# 模板里没有的地址、收款信息两列留在 Base（历史行有值，删列是破坏性的），但机器人的
# 登记表单不再收这两项，改成按模板收开始日期和结算频率（2026-09-18 定的）。

TABLE_REFERRAL_NAME = "Referral Information"

REFERRAL_NO = "渠道编号"  # Referral Code
REFERRAL_NAME = "渠道名称"  # Name
REFERRAL_EMAIL = "邮箱"  # Email
REFERRAL_ADDRESS = "地址"
REFERRAL_PAYMENT = "收款信息"
REFERRAL_START_DATE = "开始日期"  # Start Date
REFERRAL_RATE = "分佣比例"  # Commission Rate，百分数，20 表示 20%
REFERRAL_PAYOUT = "结算频率"  # Payout Frequency：Monthly / Quarterly
REFERRAL_SUBMITTED_ON = "提交日期"  # Submitted On
REFERRAL_SALES_NAME = "负责销售"  # Sales In Charge，姓名。OpenID 补上之前先靠它对人
REFERRAL_OWNER = "归属销售"
REFERRAL_OWNER_OPEN_ID = "登记人OpenID"
REFERRAL_STATUS = "状态"

# 只有这两种。没有「待审核」：销售登记完渠道直接生效，不设管理员过目这一步
# （2026-09-04 定的）。佣金计算也不看状态，停掉的渠道按业务约定根本不在数据里。
STATUS_ACTIVE = "生效"
STATUS_DISABLED = "停用"

# 结算频率只有这两种，取值就是模板 Payout Frequency 里的原文。不翻译成中文：
# 导入的历史行存的是 Monthly / Quarterly，翻译会让同一列里出现中英两套值，
# 单选字段会多出两个永远没人选的选项。
PAYOUT_MONTHLY = "Monthly"
PAYOUT_QUARTERLY = "Quarterly"
PAYOUT_OPTIONS: tuple[str, ...] = (PAYOUT_MONTHLY, PAYOUT_QUARTERLY)

REFERRAL_FIELDS: dict[str, int] = {
    # 文本，不是自动编号：现成的 R001-R101 是从模板导进来的，自动编号列写不进去。
    # 机器人新登记时读最大号 +1，在写锁的临界区里串行，不会撞号（2026-09-17 定的）。
    REFERRAL_NO: FIELD_TYPE_TEXT,
    REFERRAL_NAME: FIELD_TYPE_TEXT,
    REFERRAL_EMAIL: FIELD_TYPE_TEXT,
    # 机器人的登记表单不再写这两列，留给人手工补；列本身不删，见文件开头的说明。
    REFERRAL_ADDRESS: FIELD_TYPE_TEXT,
    REFERRAL_PAYMENT: FIELD_TYPE_TEXT,
    REFERRAL_START_DATE: FIELD_TYPE_DATETIME,
    REFERRAL_RATE: FIELD_TYPE_NUMBER,
    REFERRAL_PAYOUT: FIELD_TYPE_SINGLE_SELECT,
    REFERRAL_SUBMITTED_ON: FIELD_TYPE_DATETIME,
    REFERRAL_SALES_NAME: FIELD_TYPE_TEXT,
    REFERRAL_OWNER: FIELD_TYPE_USER,
    REFERRAL_OWNER_OPEN_ID: FIELD_TYPE_TEXT,
    REFERRAL_STATUS: FIELD_TYPE_SINGLE_SELECT,
}

# R + 3 位自增的自动编号规则。渠道编号已经改成文本列，这条规则不再用于建表，
# 留着是给 sync_base 的建字段函数和它的测试用。
REFERRAL_NO_AUTO_SERIAL = {
    "type": "custom",
    "options": [
        {"type": "fixed_text", "value": "R"},
        {"type": "system_number", "value": "3"},
    ],
}

# ---------- 表 2：渠道介绍的客户 ----------
#
# 列对齐模板「Referred Clients」：Referral Code、Client Name、UID、Sales In Charge。
# 模板里的 Name of Referral 不单独存，关联字段会显示渠道表的主字段「R001 名称」。

TABLE_CLIENT_NAME = "Referred Client"

CLIENT_UID = "客户UID"  # UID
CLIENT_NAME = "客户名称"  # Client Name
CLIENT_REFERRAL_LINK = "所属渠道"  # Referral Code，存成指向渠道表的关联
CLIENT_SALES_NAME = "负责销售"  # Sales In Charge，姓名
CLIENT_OWNER = "归属销售"
CLIENT_OWNER_OPEN_ID = "登记人OpenID"
# 客户是不是 AI（Accredited / Professional Investor，这里两者通用）。交易佣金只付给
# AI 客户带来的交易，规则见 domain/ai_status.py。2026-09-25 加的，之前登记的客户这两列
# 是空的 —— 空的照旧算，不受新规则影响。
CLIENT_AI_STATUS = "AI状态"
CLIENT_AI_DATE = "升级AI日期"

AI_STATUS_ALREADY = "开户即AI"
AI_STATUS_UPGRADED = "升级为AI"
AI_STATUS_NOT = "非AI"
AI_STATUS_OPTIONS = (AI_STATUS_ALREADY, AI_STATUS_UPGRADED, AI_STATUS_NOT)

CLIENT_FIELDS: dict[str, int] = {
    # 必须是文本。18-19 位 UID 存成数字会在服务端就被 float64 抹平精度。
    CLIENT_UID: FIELD_TYPE_TEXT,
    CLIENT_NAME: FIELD_TYPE_TEXT,
    CLIENT_REFERRAL_LINK: FIELD_TYPE_SINGLE_LINK,
    CLIENT_SALES_NAME: FIELD_TYPE_TEXT,
    CLIENT_OWNER: FIELD_TYPE_USER,
    CLIENT_OWNER_OPEN_ID: FIELD_TYPE_TEXT,
    CLIENT_AI_STATUS: FIELD_TYPE_SINGLE_SELECT,
    CLIENT_AI_DATE: FIELD_TYPE_DATETIME,
}

# 单选列建的时候带上哪些选项。只管新建的列 —— 已经存在的列 sync 不去动它。
SINGLE_SELECT_OPTIONS: dict[str, tuple[str, ...]] = {
    CLIENT_AI_STATUS: AI_STATUS_OPTIONS,
}

# ---------- 表 3：日读看板（每日交易明细，脚本从 xlsx 导入） ----------
#
# 列名和列顺序**逐字照抄**内部系统导出的交易明细 xlsx 表头，
# 以 2026-09-17 的「OTC组销售明细」为准。导入时表头原样对应 Base 的列，不做改名：
# 看板长什么样，Base 就长什么样，拿着 Excel 能在 Base 里找到同一列。
# 导出多一列不影响，少一列导入直接拒绝。
#
# 粒度是「一个用户在一个交易日」，但同一用户同一天可能有多行（那份导出里有 402 组），
# 没有行主键，所以导入按交易日期整批替换，见 scripts/import_daily_board.py。
#
# 佣金基数是「总收入(opt+现货+合约)」。那份导出里它恒等于
# opt收入 + 现货手续费_剔除做市商 + 合约手续费_剔除做市商，合约两列目前全是 0。

TABLE_DAILY_BOARD_NAME = "Daily Revenue Board"

BOARD_STATION = "站点"
BOARD_CLIENT_UID = "用户ID"  # 和客户表的「客户UID」join
BOARD_ORDER_DATE = "交易日期"
BOARD_SALES_NAME = "销售"
BOARD_CLIENT_NAME = "客户名称"
BOARD_KYC_DATE = "KYC日期"
BOARD_SALES_GROUP = "销售分组"
BOARD_USER_TYPE = "用户类型"
BOARD_SPOT_FEE_EX_MM = "现货手续费_剔除做市商"
BOARD_SPOT_VOLUME_EX_MM = "现货交易额_剔除做市商"
BOARD_CONTRACT_FEE_EX_MM = "合约手续费_剔除做市商"
BOARD_CONTRACT_VOLUME_EX_MM = "合约交易额_剔除做市商"
BOARD_OPT_FEE = "opt手续费"
BOARD_OPT_PNL = "opt_pnl"
BOARD_OPT_REVENUE = "opt收入"
BOARD_OPT_VOLUME = "opt交易额"
BOARD_TOTAL_REVENUE = "总收入(opt+现货+合约)"  # 佣金基数
BOARD_TOTAL_VOLUME = "总交易额(opt+现货+合约)"

# 看板只放这个站点的记录（2026-09-17 定的）。导出里还有香港站、中东站，导入时一律不进 Base。
# 筛的是「站点」这一列，不是「销售分组」：新加坡站的记录里也有 HK组、支付组的销售。
BOARD_STATION_IN_SCOPE = "新加坡站"

# 顺序就是 xlsx 的列顺序：sync_base 按这个顺序建列，导入脚本按这份清单核对表头。
DAILY_BOARD_FIELDS: dict[str, int] = {
    BOARD_STATION: FIELD_TYPE_TEXT,
    # 必须是文本。用户ID 大多是 18-19 位数字，存成数字字段会在服务端就被 float64
    # 抹平精度，join 客户表时静默错配。见 lark/values.py。
    BOARD_CLIENT_UID: FIELD_TYPE_TEXT,
    BOARD_ORDER_DATE: FIELD_TYPE_DATETIME,
    BOARD_SALES_NAME: FIELD_TYPE_TEXT,
    BOARD_CLIENT_NAME: FIELD_TYPE_TEXT,
    BOARD_KYC_DATE: FIELD_TYPE_DATETIME,
    BOARD_SALES_GROUP: FIELD_TYPE_TEXT,
    BOARD_USER_TYPE: FIELD_TYPE_TEXT,
    BOARD_SPOT_FEE_EX_MM: FIELD_TYPE_NUMBER,
    BOARD_SPOT_VOLUME_EX_MM: FIELD_TYPE_NUMBER,
    BOARD_CONTRACT_FEE_EX_MM: FIELD_TYPE_NUMBER,
    BOARD_CONTRACT_VOLUME_EX_MM: FIELD_TYPE_NUMBER,
    BOARD_OPT_FEE: FIELD_TYPE_NUMBER,
    BOARD_OPT_PNL: FIELD_TYPE_NUMBER,
    BOARD_OPT_REVENUE: FIELD_TYPE_NUMBER,
    BOARD_OPT_VOLUME: FIELD_TYPE_NUMBER,
    BOARD_TOTAL_REVENUE: FIELD_TYPE_NUMBER,
    BOARD_TOTAL_VOLUME: FIELD_TYPE_NUMBER,
}

# 算佣金真正依赖的三个字段。类型给 None 表示只要求存在 —— 用户ID 可能是文本，
# 也可能是查找引用，两种都能安全取值，但绝不能是数字。
DAILY_BOARD_REQUIRED_FIELDS: dict[str, int | None] = {
    BOARD_ORDER_DATE: None,
    BOARD_CLIENT_UID: None,
    BOARD_TOTAL_REVENUE: FIELD_TYPE_NUMBER,
}

# ---------- 看板上的渠道反查列（2026-09-18 定的） ----------
#
# 需求：每一行交易都要看得出它归哪个渠道、比例多少、这一笔该分多少钱，而且要在 Base 里
# 算 —— 改渠道比例时佣金列立刻跟着变，不靠脚本重跑。
#
# **匹配这一步 Base 自己做不到。** 多维表格的公式里没有 VLOOKUP / LOOKUP（实测：这类表达式
# 建得出来但永远返回空值），跨表取值只有「关联字段 + 公式引用」一条路，而关联必须由写入方
# 建立。所以分工是：
#
#   · 「客户」是单向关联列，导入脚本按「用户ID = 客户UID」写进去 —— 只做匹配，不算钱
#   · 其余四列是公式，取值和乘法全在 Base 里算
#
# 两跳引用 `[客户].[所属渠道].[分佣比例]` 实测可用（2026-09-18 在 CRM-Dev 租户建临时表验证
# 过整条链路），所以不用在客户表加中间列。
BOARD_CLIENT_LINK = "客户"  # 单向关联 -> Referred Client
BOARD_REFERRAL_NO = "渠道编号"  # 公式：渠道的 Referral Code
BOARD_REFERRAL_NAME = "渠道名称"  # 公式：渠道的 Name
BOARD_CLIENT_RATE = "分佣比例"  # 公式：渠道的 Commission Rate，百分数
# 以前还有一列「本笔佣金」（收入 × 比例）。2026-09-25 删了：AI 规则上线后（升级为 AI 之前
# 的月份不算），这一列不知道那条规则，Base 里看到的会和机器人、月结对不上。结算从来不读它。
BOARD_MONTH = "月份"  # 公式：交易日期所属月份，形如 2026-07

# 公式返回值的类型码。formula_type=2 的多维表格建公式字段时必须带上它，不带接口报错。
# 只实测过这两个值。
FORMULA_DATA_TYPE_TEXT = 1
FORMULA_DATA_TYPE_NUMBER = 2

# 反查列：列名 -> 字段类型。sync_base 按这个建列，顺序也是这个。
DAILY_BOARD_DERIVED_FIELDS: dict[str, int] = {
    BOARD_CLIENT_LINK: FIELD_TYPE_SINGLE_LINK,
    BOARD_REFERRAL_NO: FIELD_TYPE_FORMULA,
    BOARD_REFERRAL_NAME: FIELD_TYPE_FORMULA,
    BOARD_CLIENT_RATE: FIELD_TYPE_FORMULA,
    BOARD_MONTH: FIELD_TYPE_FORMULA,
}

# 公式：列名 -> (表达式, 返回类型)。
#
# **平台不校验表达式**：写错的公式照样建得出来，只是永远返回空值（实测）。所以建完必须拿
# 真实记录读回核对，见 scripts/sync_base.py 的公式自检。
DAILY_BOARD_DERIVED_FORMULAS: dict[str, tuple[str, int]] = {
    BOARD_REFERRAL_NO: (
        f"[{BOARD_CLIENT_LINK}].[{CLIENT_REFERRAL_LINK}].[{REFERRAL_NO}]",
        FORMULA_DATA_TYPE_TEXT,
    ),
    BOARD_REFERRAL_NAME: (
        f"[{BOARD_CLIENT_LINK}].[{CLIENT_REFERRAL_LINK}].[{REFERRAL_NAME}]",
        FORMULA_DATA_TYPE_TEXT,
    ),
    BOARD_CLIENT_RATE: (
        f"[{BOARD_CLIENT_LINK}].[{CLIENT_REFERRAL_LINK}].[{REFERRAL_RATE}]",
        FORMULA_DATA_TYPE_NUMBER,
    ),
    # 交易日期所属月份，形如 2026-07。视图和仪表盘都没法「按派生维度分组」，得先有一个
    # 月份列，才能做「7 月 → 每个渠道分多少钱、哪几个客户带来的」。
    #
    # **时区**：TEXT() 按平台时区算，实测是 UTC+8，和业务时区 Asia/Singapore 同一偏移，
    # 所以月初那天（存的是业务时区零点 = 前一天 16:00 UTC）算出来仍是本月：实测
    # 2026-08-01 得到 "2026-08" 而不是 "2026-07"。
    # **业务时区若换成 UTC+8 以外的时区，这一列会错月** —— sync_base 的自检会拿业务时区
    # 逐行比对并报警，别只改 .env 就完事。
    BOARD_MONTH: (
        f'TEXT([{BOARD_ORDER_DATE}], "yyyy-MM")',
        FORMULA_DATA_TYPE_TEXT,
    ),
}

# ---------- 表 4：佣金汇总（按月，后端写入） ----------

TABLE_COMMISSION_NAME = "Commission Summary"

COMM_PERIOD = "结算月份"
COMM_REFERRAL_NO = "渠道编号"
COMM_REFERRAL_NAME = "渠道名称"
COMM_CLIENT_COUNT = "客户数"
COMM_TXN_COUNT = "记录笔数"
COMM_REVENUE_TOTAL = "总收入合计"  # 曾叫 Pnl 合计。切到日读看板后佣金基数是毛收入，不是 Pnl
COMM_RATE = "分佣比例"
COMM_PAYABLE = "应付佣金"
COMM_COMPUTED_AT = "计算时间"

COMMISSION_FIELDS: dict[str, int] = {
    COMM_PERIOD: FIELD_TYPE_TEXT,
    COMM_REFERRAL_NO: FIELD_TYPE_TEXT,
    COMM_REFERRAL_NAME: FIELD_TYPE_TEXT,
    COMM_CLIENT_COUNT: FIELD_TYPE_NUMBER,
    COMM_TXN_COUNT: FIELD_TYPE_NUMBER,
    COMM_REVENUE_TOTAL: FIELD_TYPE_NUMBER,
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
