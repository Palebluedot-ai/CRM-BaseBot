#!/usr/bin/env python
"""在开发租户的 Base 里造一套种子数据。

## 为什么需要这个脚本

飞书自建应用「只能在同一企业内发布和使用」。你在自建的 `CRM-Dev` 团队里建的应用，
没有任何办法直接读到公司租户里那张真实的销售收入日读看板 —— 阶段 A 得自己造数据。

那手工导出 CSV 再导进来行不行？不行，而且是这个项目里最贵的一个坑：Excel 只保留
15 位有效数字，18-19 位的用户ID 一过 Excel 就被抹掉低位，
``577809207768677761`` 变成 ``577809207768678000``。抹完之后它看起来仍然是个合法的
长数字，join 时静默匹配到别的客户。测试数据从第一天起就是坏的，而且坏得看不出来。

所以种子数据只能走 API 直接写：Python 的 int 和 str 都是任意精度，全程不经过任何
浮点数环节。生产环境用 scripts/import_daily_board.py 从真实 xlsx 导入，那条路径也
显式挡了浮点 UID。

## 怎么用

    # 只预演，不写任何东西（默认）
    uv run python scripts/seed_dev_data.py --open-id ou_xxxxxxxx

    # 真的写
    uv run python scripts/seed_dev_data.py --open-id ou_xxxxxxxx --yes-this-is-a-dev-base

    # 换一套种子数据重来（先删旧的，需要交互式敲一遍 app_token 确认）
    uv run python scripts/seed_dev_data.py --open-id ou_xxxxxxxx --yes-this-is-a-dev-base --reset

另有一个一次性的自检开关 ``--with-damaged-uid``，专门用来让 UID 损伤检测响一次，
见文件中段 ``DAMAGED_UIDS`` 上面的说明。默认不开，用完记得 ``--reset``。

open_id 从 ``scripts/ws_smoke.py`` 的日志里拿：给机器人发条消息，它会把发件人的
open_id 打出来。拿不到就先加 ``--no-open-id``，但归属会挂在占位账号上，机器人查不到
这些渠道，之后得 ``--reset`` 重来一次。

## 两道安全闸门，为什么两道都要

**闸门一：必须显式传 ``--yes-this-is-a-dev-base``。** 默认只预演。这个 flag 表达的是
「我知道我在往一个可以随便造假数据的 Base 里写」。

**闸门二：扫一遍目标 Base，发现不是本脚本造的记录就拒绝执行。** 判据不是「数据多」而是
「有别人的数据」—— 一个刚建好的生产 Base 也是空的，靠行数根本区分不出来。

两道缺一不可，因为它们防的不是同一件事。flag 防的是「不知道这个脚本会写数据」，
但防不住最常见的那种失误：从文档里复制了完整正确的命令，而 ``.env`` 里的
``LARK_BASE_APP_TOKEN`` 指着生产 Base —— 这时候 flag 照样传了，人的意图也没错，
错的是目标。只有真去读一眼目标 Base 里有什么，才拦得住。

反过来只有扫描也不够：扫描对空 Base 无话可说，而 flag 至少保证了这一次执行是有意的。

## 幂等：默认跳过，另给 --reset

默认按自然键跳过已存在的行（渠道按名称、客户按 UID、交易按「日期+UID+Pnl」、
销售按 OpenID）。重复跑只补缺的，不会堆出重复数据，误跑一次的代价是零。

但只有跳过不够用：种子数据的形状会变（比如以后再加一对相邻 UID），这时表里留着上一版
的残留，对账结果就说不清是哪一版算出来的。所以另给 ``--reset``。

``--reset`` 的删除面收得很窄：只删标记字段以 ``SEED-`` 开头的记录。就算闸门二被绕过、
脚本真的指向了生产 Base，它也删不掉任何一行真实数据。在此之上再加一道交互式确认 ——
要求你手敲一遍 app_token，因为可以整行复制粘贴的确认等于没有确认。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crm_basebot.domain import schema  # noqa: E402
from crm_basebot.domain.audit import (  # noqa: E402
    ACTION_CREATE_CLIENT,
    ACTION_CREATE_REFERRAL,
    AuditLog,
)
from crm_basebot.domain.dates import date_to_ms  # noqa: E402
from crm_basebot.lark.bitable import BitableClient, Record  # noqa: E402
from crm_basebot.lark.values import extract_text, to_number, to_uid  # noqa: E402
from crm_basebot.startup import load_settings, require_settings  # noqa: E402

# 种子数据的标记。业务代码不认识它，它只服务两件事：
#   1. 在 Base 里一眼看出哪些行是脚本造的假数据，不会被误当成真实业务数据
#   2. --reset 只删标记命中的行，把删除面死死收在种子数据里
SEED_PREFIX = "SEED-"

# 每张表用哪个字段承载标记。挑的都是自由文本字段，不影响任何计算 ——
# 对账只读交易日期、用户ID、总收入 三个字段。
SEED_MARKER_FIELD: dict[str, str] = {
    schema.TABLE_REFERRAL_NAME: schema.REFERRAL_NAME,
    schema.TABLE_CLIENT_NAME: schema.CLIENT_NAME,
    schema.TABLE_DAILY_BOARD_NAME: schema.BOARD_CLIENT_NAME,
    schema.TABLE_COMMISSION_NAME: schema.COMM_REFERRAL_NAME,
    schema.TABLE_AUDIT_NAME: schema.AUDIT_ACTOR_NAME,
    schema.TABLE_SALES_NAME: schema.SALES_NAME,
}

# 我们自己维护、由 sync_base.py 建好的表。所有 6 张都在这里 —— 日读看板不再是
# 外部只读表，而是我们导入维护的。
OWNED_TABLES = (
    schema.TABLE_REFERRAL_NAME,
    schema.TABLE_CLIENT_NAME,
    schema.TABLE_DAILY_BOARD_NAME,
    schema.TABLE_COMMISSION_NAME,
    schema.TABLE_AUDIT_NAME,
    schema.TABLE_SALES_NAME,
)

# 没传 --open-id 时填进「登记人OpenID」的占位值。
# 刻意不长得像真的 open_id（真的以 ou_ 开头），免得有人以为它能登录。
PLACEHOLDER_OPEN_ID = "seed-placeholder-open-id"


# ---------- 种子数据定义（纯数据，不碰 API，可被单测覆盖） ----------


@dataclass(frozen=True)
class SeedReferral:
    name: str
    email: str
    address: str
    payment: str
    rate_percent: float
    status: str


@dataclass(frozen=True)
class SeedClient:
    uid: str
    name: str
    referral_name: str


@dataclass(frozen=True)
class SeedBoardRow:
    """一行日读看板种子数据，列和真实导出的 xlsx 表头一一对应，见 board_payload。"""

    order_date: str
    uid: str
    client_name: str
    revenue: float  # 总收入(opt+现货+合约)，佣金基数
    station: str
    sales_name: str
    kyc_date: str
    sales_group: str
    user_type: str
    spot_fee: float
    spot_volume: float
    contract_fee: float
    contract_volume: float
    opt_fee: float
    opt_pnl: float | None
    opt_revenue: float
    opt_volume: float
    total_volume: float

    @property
    def period(self) -> str:
        return self.order_date[:7]


@dataclass(frozen=True)
class SeedSales:
    open_id: str
    name: str
    role: str
    status: str


def build_referrals() -> list[SeedReferral]:
    """四个渠道，分佣比例刻意各不相同。

    12.5 是故意放的：整数比例掩盖不了的舍入问题，只有带小数的比例才暴露得出来
    （比如 Pnl 合计 × 12.5% 落在半分上，看 CommissionRow.payable 的 ROUND_HALF_UP
    是不是真的按预期进位）。

    状态**全部是「生效」**。渠道没有「待审核」这个状态（登记即生效，2026-09-04 定的），
    停掉的渠道又根本不在数据里 —— 使用方确认过：参与计算的都是生效的。而佣金计算
    不看状态，所以只要种子里放一个「停用」的渠道，它就会照样算出佣金，每次对账都
    让人怀疑是 bug。那个疑惑完全是种子数据自己造出来的，现实中不会发生。种子数据
    不该造出现实里不存在的状态。

    （销售名册里那个「停用」的假账号是另一回事，保留 —— 它验证的是 auth 拒绝停用
    账号这条真实路径，见 build_sales。）
    """
    return [
        SeedReferral(
            name=f"{SEED_PREFIX}北极星资本",
            email="ops@polaris-cap.example",
            address="Hong Kong, Central, Des Voeux Road 100",
            payment="HSBC 004-123-456789",
            rate_percent=20,
            status=schema.STATUS_ACTIVE,
        ),
        SeedReferral(
            name=f"{SEED_PREFIX}鲸落数字",
            email="finance@whalefall.example",
            address="Singapore, Raffles Place 8",
            payment="DBS 072-901234-5",
            rate_percent=12.5,
            status=schema.STATUS_ACTIVE,
        ),
        SeedReferral(
            name=f"{SEED_PREFIX}恒星资本",
            email="bd@stellar-cap.example",
            address="Hong Kong, Wan Chai, Gloucester Road 28",
            payment="USDT-TRC20 TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE",
            rate_percent=15,
            status=schema.STATUS_ACTIVE,
        ),
        SeedReferral(
            name=f"{SEED_PREFIX}灰岩科技",
            email="contact@greyrock.example",
            address="Dubai, DIFC, Gate Village 4",
            payment="Emirates NBD 1012345678901",
            rate_percent=8,
            status=schema.STATUS_ACTIVE,
        ),
    ]


def build_clients() -> list[SeedClient]:
    """七个客户，UID 全部是 18-19 位的真实形态。

    最要紧的是两对**只差最后一位**、且分属不同渠道的 UID：

        577809207768677761  ->  北极星资本
        577809207768677762  ->  鲸落数字
        2141293991366272768 ->  恒星资本
        2141293991366272769 ->  北极星资本

    这是给未来的自己埋的探针。只要哪天有人在链路上任何一处对 UID 做了 int()/float()，
    或者数据过了一手 Excel，这两对就会塌成同一个值，佣金立刻算到隔壁渠道头上 ——
    而且是**看得见**的错：对账结果里两个渠道的金额会明显对不上。
    如果 UID 都长得八竿子打不着，精度丢了也只是匹配不上，很容易被当成「数据还没导全」。

    另一件刻意的事：没有任何一个 UID 以连续 0 结尾。
    lark/values.py 的 looks_excel_truncated 会把「18 位且末尾 3 个 0」判为疑似 Excel
    截断，种子数据要是撞上这个形态，inspect_base 和 reconcile 每次都会报一次假警。
    """
    rows = (
        ("577809207768677761", "普罗米修斯资本", "北极星资本"),
        ("2141293991366272769", "青柠数科", "北极星资本"),
        ("577809207768677762", "普罗米修斯投资", "鲸落数字"),
        ("577809207768681473", "长夜资本", "鲸落数字"),
        ("2141293991366272768", "青柠科技", "恒星资本"),
        ("2141293991366298113", "潮汐资本", "恒星资本"),
        ("577809207768692231", "磐石家族办公室", "灰岩科技"),
    )
    return [
        SeedClient(uid, f"{SEED_PREFIX}{name}", f"{SEED_PREFIX}{referral}")
        for uid, name, referral in rows
    ]


# 只在看板里出现、没登记归属渠道的客户。
# 真实场景每天都在发生：看板里冒出一个新客户，销售还没来得及登记。
# 这几行是用来验证 reconcile 的 unmapped 告警真的会响的。
UNMAPPED_CLIENTS: dict[str, str] = {
    "577809207768703914": f"{SEED_PREFIX}未登记的星辰投资",
    "2141293991366311457": f"{SEED_PREFIX}未登记的沧海资管",
    "577809207768715026": f"{SEED_PREFIX}未登记的南山家办",
}

# (交易日期, 用户ID, 总收入)
#
# 三件事是刻意设计出来的，不是随手编的：
#
# 1. 跨 2026-01 / 02 / 03 三个月，用来验证按月汇总没有把月份串了。
# 2. 有负值行（负收入），而且分两种情形 —— 这在看板里对应退款/冲销/校准：
#    - 北极星资本 2026-02 有一笔 -875.40，但当月合计仍然为正
#    - 恒星资本 2026-02 合计为 -2935.10，**整月为负**
#    第二种是关键：它落在业务规则「整月合计为负佣金保底 0，不倒扣不结转」上，
#    对账输出里那个渠道当月应付是 0、收入合计仍是 -2935.10，并且会被单独标注出来。
#    这条规则由 CommissionRow.payable 落实，种子数据保证它每次对账都被走到一遍。
#    （毛收入不太可能整月为负，但规则条款仍要被覆盖到 —— 这就是这里的意义。）
# 3. 收入量级是几百到几千 USD 且都带小数，跟真实盘口一致；整数金额会掩盖掉
#    Decimal 累加和 float 累加的差别。
_BOARD_ROWS: tuple[tuple[str, str, float], ...] = (
    # ---- 2026-01 ----
    ("2026-01-06", "577809207768677761", 1240.55),
    ("2026-01-14", "577809207768677761", 862.30),
    ("2026-01-21", "2141293991366272769", 2105.75),
    ("2026-01-09", "577809207768677762", 640.20),
    ("2026-01-23", "577809207768681473", 1580.65),
    ("2026-01-12", "2141293991366272768", 3120.45),
    ("2026-01-27", "2141293991366298113", -415.60),
    ("2026-01-18", "577809207768692231", 980.15),
    ("2026-01-29", "577809207768703914", 1450.35),
    # ---- 2026-02 ----
    ("2026-02-03", "577809207768677761", -875.40),
    ("2026-02-11", "577809207768677761", 430.85),
    ("2026-02-19", "2141293991366272769", 1290.60),
    ("2026-02-05", "577809207768677762", 2240.90),
    ("2026-02-17", "577809207768681473", -320.75),
    ("2026-02-08", "2141293991366272768", -2680.30),
    ("2026-02-22", "2141293991366272768", -1145.20),
    ("2026-02-26", "2141293991366298113", 890.40),
    ("2026-02-14", "577809207768692231", 1560.05),
    ("2026-02-21", "2141293991366311457", 2310.55),
    # ---- 2026-03 ----
    ("2026-03-04", "577809207768677761", 1975.25),
    ("2026-03-16", "2141293991366272769", 3410.80),
    ("2026-03-25", "2141293991366272769", -560.15),
    ("2026-03-06", "577809207768677762", 1120.35),
    ("2026-03-13", "577809207768681473", 2050.70),
    ("2026-03-20", "577809207768681473", 745.90),
    ("2026-03-09", "2141293991366272768", 4230.15),
    ("2026-03-28", "2141293991366298113", 1680.55),
    ("2026-03-11", "577809207768692231", 2140.25),
    ("2026-03-18", "577809207768715026", 995.45),
    ("2026-03-30", "577809207768703914", 1875.80),
)

# ---------- 可选：被 Excel 改坏的 UID（--with-damaged-uid） ----------
#
# 默认不灌。种子数据平时刻意避开「末尾连续 0」这个形态，否则 inspect_base 和
# reconcile 每次都要报一次假警，真出事的时候反而没人信。
#
# 但副作用是：第一次跑只会看到一句「没有发现 Excel 截断特征」，没法确认那个检测到底
# 在干活还是压根没跑起来。这个开关就是为了让它响一次，看完用 --reset 换回干净数据。
#
# 下面三个值不是随手编的，是种子里三个真实客户UID 被 Excel 抹到 15 位有效数字之后的
# 样子。其中两个还顺带演示了那对「只差最后一位」的 UID 会塌成同一个值：
#
#   577809207768677761  ┐
#   577809207768677762  ┴─► 577809207768678000
#   2141293991366272768 ┐
#   2141293991366272769 ┴─► 2141293991366270000
#
# 刻意**只灌进交易明细，不写客户表**。真实的损伤来源就是交易明细那条导入链路
# （同事从内部系统导出、过一手 Excel、再导进 Base），客户表是机器人走 API 写的，
# 不会经过 Excel。所以这几个 UID 会 join 不上客户表、落进 unmapped —— 那正是真实
# 的失败形态。把它们也写进客户表反而会凭空多出一个拿佣金的幽灵渠道，把对账搅浑。
DAMAGED_UIDS: dict[str, str] = {
    "577809207768678000": f"{SEED_PREFIX}Excel损伤-普罗米修斯",
    "2141293991366270000": f"{SEED_PREFIX}Excel损伤-青柠",
    "577809207768681000": f"{SEED_PREFIX}Excel损伤-长夜",
}

# 每个损伤 UID 两笔，一共 6 行。够触发 assess_uid_health 的聚合判定：
# 它要求命中数至少 2 个、且超过「纯属巧合」期望值的 3 倍，而这批 UID 的巧合期望
# 加起来不到 0.05 个。数量再多没有额外信息，只是让表更脏。
_DAMAGED_BOARD_ROWS: tuple[tuple[str, str, float], ...] = (
    ("2026-03-05", "577809207768678000", 1820.40),
    ("2026-03-19", "577809207768678000", 640.75),
    ("2026-03-12", "2141293991366270000", 2450.85),
    ("2026-03-26", "2141293991366270000", -380.20),
    ("2026-02-13", "577809207768681000", 1150.30),
    ("2026-01-22", "577809207768681000", 905.60),
)

# 佣金只看总收入，但看板的其他列也得填上：空着的话，哪天有人写了个依赖它们的报表，
# 会以为线上数据也长这样。分类列用真实导出里出现过的取值；销售是假人，带种子标记。
_STATIONS = ("新加坡站", "香港站", "中东站")
_SALES_GROUPS = ("SG组", "HK组", "支付组")
_USER_TYPES = ("平台介绍客户", "自主开发客户")
_SALES_NAMES = (f"{SEED_PREFIX}王小明", f"{SEED_PREFIX}李小华")

_CENT = Decimal("0.01")


def _kyc_date_for(uid: str) -> str:
    """同一个客户每一行的 KYC日期都一样：按 UID 尾号在 2025 年里挑一天。"""
    return (date(2025, 1, 10) + timedelta(days=int(uid[-3:]) % 300)).isoformat()


def build_board_rows(*, with_damaged_uid: bool = False) -> list[SeedBoardRow]:
    """按 _BOARD_ROWS 里的总收入，拆出看板其余各列。

    拆法照 2026-09-17 真实导出里一行不差的几条关系：

        总收入 = opt收入 + 现货手续费_剔除做市商 + 合约手续费_剔除做市商
        总交易额 = opt交易额 + 现货交易额_剔除做市商 + 合约交易额_剔除做市商
        opt收入 = opt_pnl；opt_pnl 为空时 opt收入 = opt手续费

    合约两列在真实导出里全是 0，这里也是 0。总收入为负的行把负数放在现货手续费上，
    真实导出里的负数也出在那一列。金额用 Decimal 拆，加回去分毫不差。
    """
    names = {c.uid: c.name for c in build_clients()} | UNMAPPED_CLIENTS | DAMAGED_UIDS

    source = _BOARD_ROWS + (_DAMAGED_BOARD_ROWS if with_damaged_uid else ())

    rows: list[SeedBoardRow] = []
    for index, (order_date, uid, revenue) in enumerate(source):
        total = Decimal(str(revenue))
        if total >= 0:
            spot_fee = (total * Decimal("0.3")).quantize(_CENT, rounding=ROUND_HALF_UP)
            opt_revenue = total - spot_fee
        else:
            spot_fee = total
            opt_revenue = Decimal("0")

        if opt_revenue > 0 and index % 4 == 0:
            opt_pnl = None
            opt_fee = opt_revenue
        else:
            opt_pnl = opt_revenue
            opt_fee = (opt_revenue * Decimal("0.02")).quantize(_CENT, rounding=ROUND_HALF_UP)

        spot_volume = (abs(spot_fee) * 1250).quantize(_CENT)
        opt_volume = (opt_revenue * 800).quantize(_CENT)
        rows.append(
            SeedBoardRow(
                order_date=order_date,
                uid=uid,
                client_name=names[uid],
                revenue=revenue,
                station=_STATIONS[index % len(_STATIONS)],
                sales_name=_SALES_NAMES[index % len(_SALES_NAMES)],
                kyc_date=_kyc_date_for(uid),
                sales_group=_SALES_GROUPS[index % len(_SALES_GROUPS)],
                user_type=_USER_TYPES[index % len(_USER_TYPES)],
                spot_fee=float(spot_fee),
                spot_volume=float(spot_volume),
                contract_fee=0.0,
                contract_volume=0.0,
                opt_fee=float(opt_fee),
                opt_pnl=None if opt_pnl is None else float(opt_pnl),
                opt_revenue=float(opt_revenue),
                opt_volume=float(opt_volume),
                total_volume=float(spot_volume + opt_volume),
            )
        )
    return rows


def build_sales(open_id: str | None, admin_name: str) -> list[SeedSales]:
    """销售名册。

    第一行是你自己，角色给管理员 —— 管理员不受归属限制，能看全部数据，方便调试。
    后面两行是假账号，其中一个「停用」，用来验证名册里有人但被停用时会被拒。
    假账号的 open_id 不长得像真的，谁也登不进来。
    """
    rows: list[SeedSales] = []

    if open_id:
        rows.append(
            SeedSales(
                open_id=open_id,
                name=f"{SEED_PREFIX}{admin_name}",
                role=schema.ROLE_ADMIN,
                status=schema.SALES_STATUS_ACTIVE,
            )
        )

    rows.extend(
        [
            SeedSales(
                open_id="seed-fake-sales-a",
                name=f"{SEED_PREFIX}林晓",
                role=schema.ROLE_SALES,
                status=schema.SALES_STATUS_ACTIVE,
            ),
            SeedSales(
                open_id="seed-fake-sales-b",
                name=f"{SEED_PREFIX}周然",
                role=schema.ROLE_SALES,
                status=schema.SALES_STATUS_DISABLED,
            ),
        ]
    )
    return rows


def to_timestamp_ms(order_date: str, *, tz: tzinfo) -> int:
    """'2026-01-06' -> Bitable 日期字段要的毫秒时间戳。

    取业务时区那天的零点，和 scripts/import_daily_board.py 同一约定，界面里看到的就是
    那一天 0:00。所有日期都避开了月初月末，换任何时区归月都不会挪到隔壁月份。
    """
    return date_to_ms(datetime.strptime(order_date, "%Y-%m-%d").date(), tz=tz)


def is_seed_value(value: Any) -> bool:
    """这条记录的标记字段是不是本脚本写的。"""
    return extract_text(value).startswith(SEED_PREFIX)


def board_row_key(order_ms: int, uid: str, revenue: float) -> tuple[int, str, float]:
    """看板行的自然键。同一天同一个客户可能有多行（历史修正），所以带上收入区分。"""
    return (order_ms, uid, round(revenue, 4))


def board_payload(row: SeedBoardRow, *, tz: tzinfo) -> dict[str, Any]:
    """一行种子数据写进 Base 的字段。列名全部来自 schema，和导入脚本写的是同一套列。

    日期列换成业务时区那天零点的毫秒时间戳；opt_pnl 为空就不写这一列，和导入真实
    数据时一样。
    """
    fields: dict[str, Any] = {
        schema.BOARD_STATION: row.station,
        schema.BOARD_CLIENT_UID: row.uid,
        schema.BOARD_ORDER_DATE: to_timestamp_ms(row.order_date, tz=tz),
        schema.BOARD_SALES_NAME: row.sales_name,
        schema.BOARD_CLIENT_NAME: row.client_name,
        schema.BOARD_KYC_DATE: to_timestamp_ms(row.kyc_date, tz=tz),
        schema.BOARD_SALES_GROUP: row.sales_group,
        schema.BOARD_USER_TYPE: row.user_type,
        schema.BOARD_SPOT_FEE_EX_MM: row.spot_fee,
        schema.BOARD_SPOT_VOLUME_EX_MM: row.spot_volume,
        schema.BOARD_CONTRACT_FEE_EX_MM: row.contract_fee,
        schema.BOARD_CONTRACT_VOLUME_EX_MM: row.contract_volume,
        schema.BOARD_OPT_FEE: row.opt_fee,
        schema.BOARD_OPT_REVENUE: row.opt_revenue,
        schema.BOARD_OPT_VOLUME: row.opt_volume,
        schema.BOARD_TOTAL_REVENUE: row.revenue,
        schema.BOARD_TOTAL_VOLUME: row.total_volume,
    }
    if row.opt_pnl is not None:
        fields[schema.BOARD_OPT_PNL] = row.opt_pnl
    return fields


# ---------- 以下开始碰真实 API ----------


# 发现几条外来记录就够下结论了。看板可能是几万行的全量表，
# 没必要为了确认「这里不能碰」把它整张拉下来。
FOREIGN_SAMPLE_LIMIT = 5


@dataclass
class TableScan:
    table_id: str
    seed_records: list[Record]
    foreign_samples: list[str]


def _scan(bitable: BitableClient, table_id: str, marker_field: str) -> TableScan:
    """一次遍历同时干两件事：找外来数据（安全检查）、收集种子记录（幂等和 --reset）。

    一旦攒够 FOREIGN_SAMPLE_LIMIT 条外来记录就提前停 —— 这时候执行必然被拒，
    seed_records 收不全也无所谓。
    """
    seed_records: list[Record] = []
    foreign_samples: list[str] = []

    for record in bitable.iter_records(table_id):
        value = record.fields.get(marker_field)
        if is_seed_value(value):
            seed_records.append(record)
            continue

        foreign_samples.append(extract_text(value) or "(该字段为空)")
        if len(foreign_samples) >= FOREIGN_SAMPLE_LIMIT:
            break

    return TableScan(table_id=table_id, seed_records=seed_records, foreign_samples=foreign_samples)


def _describe_foreign(scans: dict[str, TableScan]) -> list[str]:
    problems: list[str] = []
    for table_name, scan in scans.items():
        if scan.foreign_samples:
            marker_field = SEED_MARKER_FIELD[table_name]
            count = len(scan.foreign_samples)
            # 攒够上限就提前停了，所以这时候只知道「至少这么多」
            count_text = f"至少 {count}" if count >= FOREIGN_SAMPLE_LIMIT else str(count)
            problems.append(
                f"「{table_name}」有 {count_text} 条不是本脚本造的记录，"
                f"例如 {marker_field}={scan.foreign_samples[0]}"
            )
    return problems


def _confirm_reset(app_token: str) -> bool:
    """--reset 的第二道确认：手敲一遍 app_token。

    刻意做成交互式而不是再加一个 flag。整行命令是可以复制粘贴的，多加一个 flag 只是
    让那一行更长；而 app_token 要从 .env 里翻出来对着敲，敲的过程本身就是一次核对
    「我到底在删哪个 Base」。
    """
    if not sys.stdin.isatty():
        print("--reset 只能在交互式终端里跑，需要你手敲一遍 app_token 确认。", file=sys.stderr)
        return False

    print(f"\n--reset 会删除这个 Base 里所有 {SEED_PREFIX} 开头的记录：{app_token}")
    print("（只删种子数据，不碰任何其他行。）")
    typed = input("确认请完整输入上面的 app_token：").strip()

    if typed != app_token:
        print("输入不匹配，已取消。", file=sys.stderr)
        return False
    return True


def _resolve_tables(bitable: BitableClient) -> tuple[dict[str, str], list[str]]:
    """按表名解析 table_id。

    刻意按名字找而不是读 .env 里的 TABLE_*：种子数据是紧跟在 sync_base.py --apply
    之后跑的，那时候 .env 里的 table_id 多半还没回填。表名是 sync_base 建表时用的，
    是这个阶段唯一可靠的锚点。跑完会把 table_id 打出来给你回填。
    """
    existing = {t.name: t.table_id for t in bitable.list_tables()}
    found = {name: existing[name] for name in OWNED_TABLES if name in existing}
    missing = [name for name in OWNED_TABLES if name not in existing]
    return found, missing


def _owner_fields(open_id: str | None, user_field: str, text_field: str) -> dict[str, Any]:
    """归属字段。

    人员字段只在拿到真 open_id 时才填 —— 写一个不存在的 open_id 进去，API 会直接
    报错，整个播种就断在这里。占位的情况下只写文本那一份，Base 里照样看得出归属，
    只是机器人查不到（open_id 对不上），这也正是应该被看见的后果。
    """
    fields: dict[str, Any] = {text_field: open_id or PLACEHOLDER_OPEN_ID}
    if open_id:
        fields[user_field] = [{"id": open_id}]
    return fields


def _seed_referrals(
    bitable: BitableClient,
    scan: TableScan,
    open_id: str | None,
    *,
    apply: bool,
) -> tuple[int, int]:
    existing = {extract_text(r.fields.get(schema.REFERRAL_NAME)) for r in scan.seed_records}
    created = skipped = 0

    for referral in build_referrals():
        if referral.name in existing:
            skipped += 1
            continue

        created += 1
        if not apply:
            continue

        fields: dict[str, Any] = {
            schema.REFERRAL_NAME: referral.name,
            schema.REFERRAL_EMAIL: referral.email,
            schema.REFERRAL_ADDRESS: referral.address,
            schema.REFERRAL_PAYMENT: referral.payment,
            schema.REFERRAL_RATE: referral.rate_percent,
            schema.REFERRAL_STATUS: referral.status,
        }
        # 渠道编号是自动编号字段，服务端生成，写进去会被拒
        fields.update(_owner_fields(open_id, schema.REFERRAL_OWNER, schema.REFERRAL_OWNER_OPEN_ID))
        bitable.create_record(scan.table_id, fields)

    return created, skipped


def _referral_record_ids(bitable: BitableClient, table_id: str) -> dict[str, str]:
    return {
        extract_text(r.fields.get(schema.REFERRAL_NAME)): r.record_id
        for r in bitable.iter_records(table_id)
    }


def _seed_clients(
    bitable: BitableClient,
    scan: TableScan,
    referral_ids: dict[str, str],
    open_id: str | None,
    *,
    apply: bool,
) -> tuple[int, int]:
    existing = {to_uid(r.fields.get(schema.CLIENT_UID)) for r in scan.seed_records}
    created = skipped = 0

    for client in build_clients():
        if client.uid in existing:
            skipped += 1
            continue

        created += 1
        if not apply:
            continue

        referral_record_id = referral_ids.get(client.referral_name)
        if referral_record_id is None:
            raise SystemExit(
                f"客户 {client.uid} 要挂到渠道「{client.referral_name}」，但渠道表里找不到它。"
            )

        fields: dict[str, Any] = {
            # 字符串，不是 int。这是整套种子数据存在的理由。
            schema.CLIENT_UID: client.uid,
            schema.CLIENT_NAME: client.name,
            schema.CLIENT_REFERRAL_LINK: [referral_record_id],
        }
        fields.update(_owner_fields(open_id, schema.CLIENT_OWNER, schema.CLIENT_OWNER_OPEN_ID))
        bitable.create_record(scan.table_id, fields)

    return created, skipped


def _seed_board_rows(
    bitable: BitableClient,
    scan: TableScan,
    *,
    apply: bool,
    tz: tzinfo,
    with_damaged_uid: bool = False,
) -> tuple[int, int]:
    existing = {
        board_row_key(
            int(to_number(r.fields.get(schema.BOARD_ORDER_DATE)) or 0),
            to_uid(r.fields.get(schema.BOARD_CLIENT_UID)),
            to_number(r.fields.get(schema.BOARD_TOTAL_REVENUE)) or 0.0,
        )
        for r in scan.seed_records
    }
    created = skipped = 0

    for row in build_board_rows(with_damaged_uid=with_damaged_uid):
        order_ms = to_timestamp_ms(row.order_date, tz=tz)
        if board_row_key(order_ms, row.uid, row.revenue) in existing:
            skipped += 1
            continue

        created += 1
        if not apply:
            continue

        bitable.create_record(scan.table_id, board_payload(row, tz=tz), reread=False)

    return created, skipped


def _seed_sales(
    bitable: BitableClient,
    scan: TableScan,
    open_id: str | None,
    admin_name: str,
    *,
    apply: bool,
) -> tuple[int, int]:
    existing = {extract_text(r.fields.get(schema.SALES_OPEN_ID)) for r in scan.seed_records}
    created = skipped = 0

    for sales in build_sales(open_id, admin_name):
        if sales.open_id in existing:
            skipped += 1
            continue

        created += 1
        if not apply:
            continue

        bitable.create_record(
            scan.table_id,
            {
                schema.SALES_OPEN_ID: sales.open_id,
                schema.SALES_NAME: sales.name,
                schema.SALES_ROLE: sales.role,
                schema.SALES_STATUS: sales.status,
            },
        )

    return created, skipped


def _seed_audit(
    bitable: BitableClient,
    scan: TableScan,
    open_id: str | None,
    *,
    apply: bool,
) -> tuple[int, int]:
    """补几条审计记录，让审计表不是空的。

    审计表按设计只增不改，没有自然键可比。所以幂等策略退化成「已经有种子记录就整体
    跳过」—— 审计行本来就该长成一条一条的流水，这里不追求逐条比对。
    """
    planned = len(build_referrals()) + len(build_clients())

    if scan.seed_records:
        return 0, planned

    if not apply:
        return planned, 0

    audit = AuditLog(bitable, scan.table_id)
    actor_open_id = open_id or PLACEHOLDER_OPEN_ID
    actor_name = f"{SEED_PREFIX}播种脚本"

    for referral in build_referrals():
        audit.record(
            actor_open_id=actor_open_id,
            actor_name=actor_name,
            action=ACTION_CREATE_REFERRAL,
            target_table=schema.TABLE_REFERRAL_NAME,
            detail={"渠道名称": referral.name, "分佣比例": referral.rate_percent},
        )

    for client in build_clients():
        audit.record(
            actor_open_id=actor_open_id,
            actor_name=actor_name,
            action=ACTION_CREATE_CLIENT,
            target_table=schema.TABLE_CLIENT_NAME,
            detail={"客户UID": client.uid, "所属渠道": client.referral_name},
        )

    return planned, 0


def _reset(bitable: BitableClient, scans: dict[str, TableScan]) -> int:
    deleted = 0
    for table_name, scan in scans.items():
        if not scan.seed_records:
            continue
        print(f"  删除「{table_name}」里的 {len(scan.seed_records)} 条种子记录…")
        for record in scan.seed_records:
            bitable.delete_record(scan.table_id, record.record_id)
            deleted += 1
    return deleted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="在开发租户的 Base 里造种子数据（默认只预演）",
    )
    parser.add_argument(
        "--open-id",
        help="你自己的 open_id（ou_ 开头），从 scripts/ws_smoke.py 的日志里拿。"
        "渠道和客户会挂在这个人名下，机器人才查得到",
    )
    parser.add_argument(
        "--no-open-id",
        action="store_true",
        help="暂时没有 open_id 也要播种。归属会挂在占位账号上，机器人查不到这些数据，"
        "拿到 open_id 后需要 --reset 重来",
    )
    parser.add_argument(
        "--admin-name",
        default="开发管理员",
        help="你在销售名册里显示的姓名，默认「开发管理员」",
    )
    parser.add_argument(
        "--yes-this-is-a-dev-base",
        action="store_true",
        dest="confirmed",
        help="真的写入。不加这个就只预演",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="先删掉所有 SEED- 开头的记录再重新播种（会再要一道交互式确认）",
    )
    parser.add_argument(
        "--with-damaged-uid",
        action="store_true",
        help="额外灌 6 笔客户UID 被 Excel 抹掉低位的交易，用来看一眼 UID 损伤检测确实会"
        "报警。不是常规步骤：确认完就用 --reset 换回干净数据",
    )
    args = parser.parse_args(argv)

    if not args.open_id and not args.no_open_id:
        print(
            "需要 --open-id。给机器人发条消息，scripts/ws_smoke.py 会把你的 open_id 打到日志里。\n"
            "确实拿不到就加 --no-open-id：数据照样造，但归属挂在占位账号上，"
            "机器人查不到这些渠道，之后得 --reset 重来一次。",
            file=sys.stderr,
        )
        return 1

    if args.open_id and not args.open_id.startswith("ou_"):
        print(
            f"open_id 应该以 ou_ 开头，你给的是「{args.open_id}」。"
            "别把 user_id 或 union_id 填进来了。",
            file=sys.stderr,
        )
        return 1

    settings = load_settings()
    require_settings(settings, "LARK_BASE_APP_TOKEN")

    bitable = BitableClient(settings.base_app_token)
    apply = args.confirmed
    tz = ZoneInfo(settings.business_timezone)

    print(f"目标 Base: {settings.base_app_token}")
    print("模式：真正写入" if apply else "模式：预演（不写任何东西）")
    if args.with_damaged_uid:
        print(
            f"--with-damaged-uid：额外灌 {len(_DAMAGED_BOARD_ROWS)} 行"
            f"被 Excel 抹掉低位的 UID（{len(DAMAGED_UIDS)} 个客户），只进日读看板。\n"
            "                    这是一次性的检测自检，确认告警会响之后请用 --reset 换回干净数据。"
        )
    print()

    tables, missing = _resolve_tables(bitable)
    if missing:
        print(
            "这些表还不存在：" + "、".join(missing) + "\n"
            "先跑 `uv run python scripts/sync_base.py --apply` 把结构建好。",
            file=sys.stderr,
        )
        return 1

    # ---------- 闸门二：目标 Base 里有没有别人的数据 ----------
    scans = {
        name: _scan(bitable, table_id, SEED_MARKER_FIELD[name]) for name, table_id in tables.items()
    }

    problems = _describe_foreign(scans)
    if problems:
        print("拒绝执行 —— 这个 Base 里有不是本脚本造的数据：", file=sys.stderr)
        for item in problems:
            print(f"  ! {item}", file=sys.stderr)
        print(
            "\n这个脚本只能在开发租户里空的（或只有种子数据的）Base 上跑。\n"
            "如果你确认这是开发租户、上面这些是你手动造的，先自己删掉它们，"
            "或者换一个干净的 Base。\n"
            "没有跳过这道检查的开关 —— 有开关就一定会有人在生产上用它。",
            file=sys.stderr,
        )
        return 1

    if args.reset:
        if not apply:
            total = sum(len(s.seed_records) for s in scans.values())
            print(f"--reset 将删除 {total} 条种子记录（加 --yes-this-is-a-dev-base 才会真删）")
        else:
            if not _confirm_reset(settings.base_app_token):
                return 1
            deleted = _reset(bitable, scans)
            print(f"已删除 {deleted} 条种子记录。\n")

        # 预演时也要把种子记录当成已删除，否则下面会报「跳过已存在 N 条」，
        # 和 --reset 之后的真实结果对不上，预演就失去意义了。
        scans = {
            name: TableScan(table_id=scan.table_id, seed_records=[], foreign_samples=[])
            for name, scan in scans.items()
        }

    open_id = args.open_id
    results: list[tuple[str, int, int]] = []

    created, skipped = _seed_referrals(
        bitable, scans[schema.TABLE_REFERRAL_NAME], open_id, apply=apply
    )
    results.append((schema.TABLE_REFERRAL_NAME, created, skipped))

    referral_ids = (
        _referral_record_ids(bitable, tables[schema.TABLE_REFERRAL_NAME]) if apply else {}
    )
    created, skipped = _seed_clients(
        bitable, scans[schema.TABLE_CLIENT_NAME], referral_ids, open_id, apply=apply
    )
    results.append((schema.TABLE_CLIENT_NAME, created, skipped))

    created, skipped = _seed_board_rows(
        bitable,
        scans[schema.TABLE_DAILY_BOARD_NAME],
        apply=apply,
        tz=tz,
        with_damaged_uid=args.with_damaged_uid,
    )
    results.append((schema.TABLE_DAILY_BOARD_NAME, created, skipped))

    created, skipped = _seed_sales(
        bitable, scans[schema.TABLE_SALES_NAME], open_id, args.admin_name, apply=apply
    )
    results.append((schema.TABLE_SALES_NAME, created, skipped))

    created, skipped = _seed_audit(bitable, scans[schema.TABLE_AUDIT_NAME], open_id, apply=apply)
    results.append((schema.TABLE_AUDIT_NAME, created, skipped))

    verb = "已写入" if apply else "将写入"
    print(f"\n{'=' * 60}")
    for table_name, created, skipped in results:
        print(f"  {table_name:<22} {verb} {created:>3} 条，跳过已存在 {skipped:>3} 条")
    print(f"  {schema.TABLE_COMMISSION_NAME:<22} 刻意留空 —— 由 reconcile --write 填")
    print("=" * 60)

    if not apply:
        print("\n确认无误后加 --yes-this-is-a-dev-base 真正执行。")
        return 0

    print("\n各表的 table_id，回填到 .env：")
    env_keys = {
        schema.TABLE_REFERRAL_NAME: "TABLE_REFERRAL",
        schema.TABLE_CLIENT_NAME: "TABLE_CLIENT",
        schema.TABLE_DAILY_BOARD_NAME: "TABLE_DAILY_BOARD",
        schema.TABLE_COMMISSION_NAME: "TABLE_COMMISSION",
        schema.TABLE_AUDIT_NAME: "TABLE_AUDIT",
        schema.TABLE_SALES_NAME: "TABLE_SALES",
    }
    for table_name, env_key in env_keys.items():
        table_id = tables.get(table_name, "")
        print(f"  {env_key}={table_id}")

    if not open_id:
        print(
            f"\n注意：没给 --open-id，渠道和客户的归属挂在占位值 {PLACEHOLDER_OPEN_ID} 上。\n"
            "机器人按 open_id 过滤归属，所以你现在用机器人查「我的渠道」会是空的。\n"
            "拿到 open_id 后重跑一次：--open-id ou_xxx --yes-this-is-a-dev-base --reset"
        )

    board_rows = build_board_rows(with_damaged_uid=args.with_damaged_uid)
    periods = sorted({r.period for r in board_rows})
    print("\n下一步，验证对账（只算不写）：")
    for period in periods:
        print(f"  uv run python -m crm_basebot.jobs.reconcile --period {period}")
    print(f"  uv run python -m crm_basebot.jobs.reconcile   # 不传月份就是最新的 {periods[-1]}")

    unmapped_count = len(UNMAPPED_CLIENTS) + (len(DAMAGED_UIDS) if args.with_damaged_uid else 0)
    print(
        f"\n预期能看到：{unmapped_count} 个未登记归属的客户告警；"
        "以及 2026-02 有一个渠道整月合计为负 —— 按业务规则它当月应付佣金是 0"
        "（保底，不倒扣不结转），汇总里会单独标注一行。"
    )

    if args.with_damaged_uid:
        print(
            f"\n另外 inspect_base.py 和 reconcile 都会对「{schema.TABLE_DAILY_BOARD_NAME}"
            f".{schema.BOARD_CLIENT_UID}」报 UID 损伤告警（判定 likely_damaged），"
            f"点名那 {len(DAMAGED_UIDS)} 个以连续 0 结尾的值。看到告警就说明检测在工作。\n"
            "确认完请换回干净数据，别顶着这条告警继续跑：\n"
            "  uv run python scripts/seed_dev_data.py --open-id ou_xxx "
            "--yes-this-is-a-dev-base --reset"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
