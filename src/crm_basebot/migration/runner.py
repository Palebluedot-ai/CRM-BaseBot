"""一站式迁移的编排：结构 → 渠道 → 客户 → 看板 → 名册 → 自检 → 报告。

顺序不能变，因为关联要用**目标端**的 record_id 重建：先有渠道，客户才挂得上；先有客户，
看板的「客户」关联才挂得上。

两个飞书应用意味着两个客户端：源端用 ``get_client()``（单例），目标端用
``build_client(target_settings)`` 现造一个，并且**显式传给 BitableClient** —— 不传的话
它会退回源端的客户端，于是拿着源应用的身份去动目标 Base，报错还是 403 这种看不出因果的。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings
from ..domain import schema
from ..lark.bitable import BitableClient
from ..lark.client import build_client
from ..lark.values import extract_text
from ..startup import MissingConfigError, load_settings
from ..structure import StructureResult, ensure_structure
from .copy import (
    CopySpec,
    channel_index,
    copy_records,
    rebuild_link,
    uid_by_record_id,
)

logger = logging.getLogger(__name__)


class MigrationError(RuntimeError):
    """迁移没法继续。消息是给人看的，可以直接打印。"""


@dataclass
class TableCopyResult:
    """一张表的搬运结果 + 两边的行数（自检用）。"""

    label: str
    source_rows: int
    target_rows: int
    dropped_fields: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.source_rows == self.target_rows

    def summary(self) -> str:
        mark = "✅" if self.ok else "⚠️"
        line = f"{mark} {self.label}：源 {self.source_rows} 条 → 目标 {self.target_rows} 条"
        if self.dropped_fields:
            line += f"\n     未搬的列：{'、'.join(self.dropped_fields)}"
        return line


@dataclass
class MigrationResult:
    structure: StructureResult
    tables: list[TableCopyResult] = field(default_factory=list)
    applied: bool = False

    @property
    def ok(self) -> bool:
        return all(table.ok for table in self.tables)

    @property
    def notes(self) -> list[str]:
        """搬完还需要人做的（open_id 是按应用签发的，机器代劳不了）。"""
        return [
            "目标端的 OpenID 还空着 —— open_id 是飞书按应用签发的，源端的值到了目标端无效。",
            "  让目标账号的每位销售各给机器人发一条消息，日志里会记下他的 open_id，然后：",
            "    uv run python scripts/set_sales_open_id.py --env .env.target "
            '--name "某人" --open-id ou_xxx --apply',
            "名册填好之后再回填渠道/客户的归属（在那之前机器人里「我的渠道」是空的）：",
            "    uv run python scripts/backfill_owners.py --env .env.target --apply",
        ]


def _app_credentials_message(env_path: Path) -> str:
    return (
        f"{env_path} 里还没填目标账号的应用凭证（LARK_APP_ID / LARK_APP_SECRET）。\n"
        "  去哪拿：https://open.feishu.cn/app → 在**目标账号**里创建企业自建应用\n"
        "          → 左侧「凭证与基础信息」。\n"
        "  别忘了两件：① 权限管理里开通 bitable:app  ② 创建版本并发布（缺了会 403）。"
    )


def load_target_settings(env_path: Path, *, require_token: bool = True) -> Settings:
    """从目标环境文件读凭证。缺键时报出**缺哪个**，不回显任何值。

    走 ``startup.load_settings(env_file=...)`` 而不是自己构造 Settings：缺键时的文案
    只有那一处（会告诉人缺哪个键、去哪儿填），迁移这条路上同样该看到它。

    ``require_token=False`` 用于「边建 Base 边迁移」：那一步开始时 token 还不存在
    （Base 是刚建出来的），建完写回文件再继续。
    """
    if not env_path.is_file():
        raise MigrationError(
            f"找不到目标环境文件：{env_path}\n"
            "  复制 .env.target.example 改成 .env.target，填**目标账号**那个飞书应用的\n"
            "  LARK_APP_ID / LARK_APP_SECRET / LARK_BASE_APP_TOKEN（表 id 可以留空，\n"
            "  迁移会按表名找到它们）。"
        )
    try:
        settings = load_settings(env_file=env_path)
    except MissingConfigError as exc:
        message = str(exc)
        # startup 的缺键文案是围绕 `.env` 写的（会告诉人去项目根目录找）。迁移读的是
        # .env.target，那份文案会把人指到错的文件上，所以这两项换成本地版本。
        if "LARK_APP_ID" in message or "LARK_APP_SECRET" in message:
            raise MigrationError(_app_credentials_message(env_path)) from None
        raise MigrationError(message) from None
    if not settings.app_id or not settings.app_secret:
        raise MigrationError(_app_credentials_message(env_path))
    if require_token and not settings.base_app_token:
        raise MigrationError(f"{env_path} 里没填 LARK_BASE_APP_TOKEN（目标 Base 的 token）")
    return settings


def set_env_value(env_path: Path, key: str, value: str) -> None:
    """把 ``KEY=value`` 写进环境文件（已存在就替换那一行，不动的行原样保留）。

    建完 Base 拿到 token 后要落盘 —— 不然下一次跑又得重新建一个。注释和空行都留着，
    文件还是那个「人看的一页配置」，不是被程序重排过的产物。
    """
    lines = env_path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[index] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def create_target_base(client, name: str) -> str:
    """在目标账号里新建一个多维表格（Base），返回它的 app_token。

    为什么要有这条：Base 由**应用自己**创建时，应用就是它的所有者，不需要任何「把应用
    加为协作者」的界面操作 —— 那一步漏了会得到 403，而报错完全看不出是这个原因
    （迁移路上最容易卡住的一个坑）。
    """
    from lark_oapi.api.bitable.v1 import CreateAppRequest, ReqApp

    request = CreateAppRequest.builder().request_body(ReqApp.builder().name(name).build()).build()
    response = client.bitable.v1.app.create(request)
    if not response.success():
        raise MigrationError(
            f"新建 Base 失败：{response.code} {response.msg}\n"
            "  检查应用是否开通了「多维表格」权限，以及是否创建并发版了应用版本。"
        )
    app = getattr(response.data, "app", None)
    app_token = getattr(app, "app_token", None) if app else None
    if not app_token:
        raise MigrationError("新建 Base 返回成功但没给 app_token，请到飞书里确认 Base 是否已建出来")
    return app_token


def _table_id(structure: StructureResult, name: str) -> str:
    table_id = structure.table_ids.get(name)
    if not table_id:
        raise MigrationError(
            f"目标端没有「{name}」这张表。看起来结构没建起来 —— 先看一遍 --dry-run 的输出。"
        )
    return table_id


def run_migration(
    *,
    target_env_path: Path,
    apply: bool = False,
    include_audit: bool = False,
    include_commission: bool = False,
    source_settings: Settings | None = None,
    client_factory: Callable[[Any], Any] = build_client,
    create_base: str | None = None,
) -> MigrationResult:
    """一条命令的入口：``apply=False`` 只预演。

    ``create_base="名字"`` 时，若目标环境里还没有 Base token，就先**用一个应用建出这个
    Base** 再往下走（token 会写回环境文件）。这样同事那边只需要出一个应用凭证 ——
    不用手工建 Base、也不用在界面上把应用加成协作者。
    """
    source_settings = source_settings or load_settings()
    target_settings = load_target_settings(target_env_path, require_token=create_base is None)

    # 预演：目标 Base 还不存在的话，就把「要建 Base」写进计划里，别的都只读不动。
    if not apply:
        result = MigrationResult(structure=StructureResult(), applied=False)
        if create_base and not target_settings.base_app_token:
            result.structure.plan.append(f"新建 Base「{create_base}」（预演，没有真的建）")
        else:
            target_client = client_factory(target_settings)
            result.structure = ensure_structure(
                settings=target_settings,
                bitable=BitableClient(target_settings.base_app_token, target_client),
                client=target_client,
                apply=False,
            )
        return result

    target_client = client_factory(target_settings)
    if create_base and not target_settings.base_app_token:
        app_token = create_target_base(target_client, create_base)
        set_env_value(target_env_path, "LARK_BASE_APP_TOKEN", app_token)
        target_settings = load_target_settings(target_env_path)
        logger.info("已新建 Base「%s」，token 写回 %s", create_base, target_env_path)

    source_bitable = BitableClient(source_settings.base_app_token)
    # 显式把目标客户端传进去：不然 BitableClient 会用源应用的身份去动目标 Base。
    target_bitable = BitableClient(target_settings.base_app_token, target_client)

    structure = ensure_structure(
        settings=target_settings,
        bitable=target_bitable,
        client=target_client,
        apply=True,
    )
    result = MigrationResult(structure=structure, applied=True)

    source = _Source(source_bitable, source_settings)
    specs = _build_specs(
        source=source,
        target_bitable=target_bitable,
        target_settings=target_settings,
        structure=structure,
        include_audit=include_audit,
        include_commission=include_commission,
    )

    for spec in specs:
        report = copy_records(source=source_bitable, target=target_bitable, spec=spec)
        target_rows = sum(1 for _ in target_bitable.iter_records(spec.target_table_id))
        result.tables.append(
            TableCopyResult(
                label=spec.label,
                source_rows=report.read,
                target_rows=target_rows,
                dropped_fields=sorted(report.dropped_fields),
            )
        )
    return result


@dataclass
class _Source:
    """源端的几个「业务键 -> record_id」索引，建一次给多张表用。"""

    bitable: BitableClient
    settings: Settings


def _build_specs(
    *,
    source: _Source,
    target_bitable: BitableClient,
    target_settings: Settings,
    structure: StructureResult,
    include_audit: bool,
    include_commission: bool,
) -> list[CopySpec]:
    src = source.bitable
    spec_list: list[CopySpec] = []

    referral_id = _table_id(structure, schema.TABLE_REFERRAL_NAME)
    client_id = _table_id(structure, schema.TABLE_CLIENT_NAME)
    board_id = _table_id(structure, schema.TABLE_DAILY_BOARD_NAME)
    sales_id = _table_id(structure, schema.TABLE_SALES_NAME)

    # ① 渠道。没有关联字段，直接搬。
    spec_list.append(
        CopySpec(
            label="渠道 Referral Information",
            source_table_id=source.settings.table_referral,
            target_table_id=referral_id,
        )
    )

    # ② 客户：所属渠道按「渠道编号」重建（源 record_id ≠ 目标 record_id）
    channel_no_by_src_id = {
        record.record_id: extract_text(record.fields.get(schema.REFERRAL_NO))
        for record in src.iter_records(source.settings.table_referral)
    }
    channel_target_by_no = channel_index(
        target_bitable, referral_id, id_field="", key_field=schema.REFERRAL_NO
    )
    link_field = schema.CLIENT_REFERRAL_LINK

    def rebuild_channel(fields: dict[str, Any]) -> dict[str, Any]:
        rebuild_link(
            fields=fields,
            link_field=link_field,
            source_id_index={},
            source_key_by_id=channel_no_by_src_id,
            target_id_index=channel_target_by_no,
        )
        return fields

    spec_list.append(
        CopySpec(
            label="客户 Referred Client",
            source_table_id=source.settings.table_client,
            target_table_id=client_id,
            transform=rebuild_channel,
        )
    )

    # ③ 看板：客户按「客户UID」重建
    client_uid_by_src_id = uid_by_record_id(src, source.settings.table_client, schema.CLIENT_UID)
    client_target_by_uid = channel_index(
        target_bitable, client_id, id_field="", key_field=schema.CLIENT_UID
    )
    board_link = schema.BOARD_CLIENT_LINK

    def rebuild_client(fields: dict[str, Any]) -> dict[str, Any]:
        rebuild_link(
            fields=fields,
            link_field=board_link,
            source_id_index={},
            source_key_by_id=client_uid_by_src_id,
            target_id_index=client_target_by_uid,
        )
        return fields

    spec_list.append(
        CopySpec(
            label="看板 Daily Revenue Board",
            source_table_id=source.settings.table_daily_board,
            target_table_id=board_id,
            transform=rebuild_client,
        )
    )

    # ④ 名册：OpenID 是源应用签发的，不能搬（目标端要重新认领）
    spec_list.append(
        CopySpec(
            label="销售名册 Sales Directory",
            source_table_id=source.settings.table_sales,
            target_table_id=sales_id,
            extra_skip=frozenset({schema.SALES_OPEN_ID}),
        )
    )

    if include_commission:
        spec_list.append(
            CopySpec(
                label="月度汇总 Commission Summary",
                source_table_id=source.settings.table_commission,
                target_table_id=_table_id(structure, schema.TABLE_COMMISSION_NAME),
            )
        )
    if include_audit:
        spec_list.append(
            CopySpec(
                label="审计日志 Audit Log",
                source_table_id=source.settings.table_audit,
                target_table_id=_table_id(structure, schema.TABLE_AUDIT_NAME),
            )
        )

    return spec_list


def print_report(result: MigrationResult, *, target_env_path: Path) -> None:
    """迁移报告。人看的就是这一段，所以措辞都在这里。"""
    if not result.applied:
        print("\n=== 预演：目标端将要发生什么 ===")
        if not result.structure.changed:
            print("结构已经对齐，没有要建的表/列。")
        else:
            for item in result.structure.plan:
                print(f"  · {item}")
        print(
            "\n预演到此为止 —— 没有建表、没有搬数据。"
            "\n确认上面这些动作没问题后，加 --apply 真跑一次。"
        )
        return

    print("\n=== 结构 ===")
    if result.structure.changed:
        print(
            f"建了 {result.structure.built_tables} 张表、{result.structure.added_fields} 个字段。"
        )
    else:
        print("结构本来就已经对齐。")
    for item in result.structure.warnings:
        print(f"  ! {item}")

    print("\n=== 数据 ===")
    for table in result.tables:
        print(f"  {table.summary()}")

    print("\n=== 整体 ===")
    print("两边行数一致 ✅" if result.ok else "⚠️ 有表两边行数不一致，看上面带 ⚠️ 的行")
    print("\n=== 还需要人做的两件 ===")
    for note in result.notes:
        print(f"  {note}")
    print(f"\n（目标环境文件：{target_env_path}）")
