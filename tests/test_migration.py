"""迁移：把整套 Base 从「一个飞书账号」搬到「另一个飞书账号」。

搬家最容易出的两类事故，测试就盯这两类：

1. **关联搬丢或搬错。** 关联存的是 record_id，两边 id 完全不同。直接抄过去等于把客户
   挂到不存在的渠道上 —— 佣金会算不出来，或者算到别人头上。所以必须借业务键
   （渠道编号 / 客户UID）重建，重建不了就留空，绝不瞎指。
2. **把不该搬的列搬过去。** 公式列搬过去写不进（整条记录都会被拒），人员列的
   open_id 是源应用签发的、到了目标端是无效值。
"""

from __future__ import annotations

from pathlib import Path

from crm_basebot.domain import schema

from .conftest import TBL_BOARD, TBL_CLIENT, TBL_REFERRAL, TBL_SALES


def _load_module():
    """scripts/ 不是包，按路径加载。"""
    import importlib.util
    import sys

    path = Path(__file__).resolve().parent.parent / "scripts" / "migrate_base.py"
    spec = importlib.util.spec_from_file_location("migrate_base", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cli = _load_module()

from crm_basebot.migration.copy import (  # noqa: E402
    CopySpec,
    channel_index,
    copy_records,
    rebuild_link,
    unwritable_fields,
)
from crm_basebot.migration.runner import MigrationError, load_target_settings  # noqa: E402


def _channel(fake_bitable, no: str, name: str) -> str:
    return fake_bitable.table(TBL_REFERRAL).add_existing(
        {schema.REFERRAL_NO: no, schema.REFERRAL_NAME: name}
    )


def _client(fake_bitable, uid: str, name: str, channel_record_id: str) -> str:
    return fake_bitable.table(TBL_CLIENT).add_existing(
        {
            schema.CLIENT_UID: uid,
            schema.CLIENT_NAME: name,
            schema.CLIENT_REFERRAL_LINK: {"link_record_ids": [channel_record_id]},
        }
    )


# ---------- 关联重建 ----------


def test_关联按业务键重建(fake_bitable):
    """源端 record_id 与目标端完全不同，只有业务键能对上。"""
    source_channel = _channel(fake_bitable, "R001", "ABC Capital")
    target_channel = _channel(fake_bitable, "R001", "ABC Capital")  # 目标端另一条记录

    fields = {schema.CLIENT_REFERRAL_LINK: {"link_record_ids": [source_channel]}}
    rebuild_link(
        fields=fields,
        link_field=schema.CLIENT_REFERRAL_LINK,
        source_id_index={},
        source_key_by_id={source_channel: "R001"},
        target_id_index={"R001": target_channel},
    )

    assert fields[schema.CLIENT_REFERRAL_LINK] == [target_channel]


def test_目标端找不到对应记录时留空而不是瞎指(fake_bitable):
    source_channel = _channel(fake_bitable, "R999", "Not Migrated")

    fields = {schema.CLIENT_REFERRAL_LINK: {"link_record_ids": [source_channel]}}
    rebuild_link(
        fields=fields,
        link_field=schema.CLIENT_REFERRAL_LINK,
        source_id_index={},
        source_key_by_id={source_channel: "R999"},
        target_id_index={"R001": "rec-other"},
    )

    assert schema.CLIENT_REFERRAL_LINK not in fields


def test_空关联不会变成瞎指(fake_bitable):
    fields = {schema.CLIENT_REFERRAL_LINK: {"link_record_ids": None}}
    rebuild_link(
        fields=fields,
        link_field=schema.CLIENT_REFERRAL_LINK,
        source_id_index={},
        source_key_by_id={},
        target_id_index={"R001": "rec-other"},
    )
    assert schema.CLIENT_REFERRAL_LINK not in fields


def test_业务键索引忽略空键(fake_bitable):
    _channel(fake_bitable, "", "没有编号的渠道")
    _channel(fake_bitable, "R002", "有编号的")
    index = channel_index(fake_bitable, TBL_REFERRAL, id_field="", key_field=schema.REFERRAL_NO)
    assert set(index) == {"R002"}


# ---------- 不搬的列 ----------


def test_公式列和人员列不进payload(fake_bitable):
    """公式列搬过去整条记录都会被拒；人员列的 open_id 跨应用无效。"""
    from crm_basebot.lark.bitable import FieldInfo

    fake_bitable.table(TBL_BOARD).fields = [
        FieldInfo(
            field_id="f1",
            name=schema.BOARD_ROW_COMMISSION,
            type=20,
            ui_type="Formula",
            is_primary=False,
        ),
        FieldInfo(
            field_id="f2",
            name=schema.BOARD_CLIENT_UID,
            type=1,
            ui_type="Text",
            is_primary=False,
        ),
    ]
    skip = unwritable_fields(fake_bitable, TBL_BOARD)
    assert schema.BOARD_ROW_COMMISSION in skip
    assert schema.BOARD_CLIENT_UID not in skip


def test_只读列不会出现在搬运结果里(fake_bitable):
    from crm_basebot.lark.bitable import FieldInfo

    source = fake_bitable
    target = fake_bitable
    source.table(TBL_REFERRAL).add_existing(
        {schema.REFERRAL_NO: "R001", schema.REFERRAL_NAME: "ABC", "编号": "R001"}
    )
    source.table(TBL_REFERRAL).fields = [
        FieldInfo(field_id="f0", name="编号", type=1005, ui_type="AutoNumber", is_primary=True)
    ]
    target.table(TBL_REFERRAL).fields = [
        FieldInfo(field_id="f0", name="编号", type=1005, ui_type="AutoNumber", is_primary=True)
    ]

    report = copy_records(
        source=source,
        target=target,
        spec=CopySpec(label="渠道", source_table_id=TBL_REFERRAL, target_table_id=TBL_REFERRAL),
    )

    assert report.read == 1
    assert "编号" in report.dropped_fields


def test_名册的OpenID不搬(fake_bitable):
    fake_bitable.table(TBL_SALES).add_existing(
        {schema.SALES_NAME: "James YANG", schema.SALES_OPEN_ID: "ou_source_tenant"}
    )

    report = copy_records(
        source=fake_bitable,
        target=fake_bitable,
        spec=CopySpec(
            label="名册",
            source_table_id=TBL_SALES,
            target_table_id=TBL_SALES,
            extra_skip=frozenset({schema.SALES_OPEN_ID}),
        ),
    )

    assert schema.SALES_OPEN_ID in report.dropped_fields


def test_预演时不写(fake_bitable):
    _channel(fake_bitable, "R001", "ABC Capital")
    before = fake_bitable.write_count

    report = copy_records(
        source=fake_bitable,
        target=fake_bitable,
        spec=CopySpec(label="渠道", source_table_id=TBL_REFERRAL, target_table_id=TBL_REFERRAL),
        dry_run=True,
    )

    assert report.read == 1
    assert report.written == 0
    assert fake_bitable.write_count == before


# ---------- 目标环境文件 ----------


def test_目标环境文件不存在时给出怎么做(tmp_path):
    missing = tmp_path / "nope.env"
    try:
        load_target_settings(missing)
    except MigrationError as exc:
        assert str(missing) in str(exc)
        assert ".env.target" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应该报错")


def test_目标环境文件缺Base_token时报错(tmp_path):
    env = tmp_path / ".env.target"
    env.write_text("LARK_APP_ID=cli_x\nLARK_APP_SECRET=s\n", encoding="utf-8")
    try:
        load_target_settings(env)
    except MigrationError as exc:
        assert "LARK_BASE_APP_TOKEN" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应该报错")


def test_目标环境文件缺应用凭证时告诉去哪拿(tmp_path):
    """缺应用凭证时不能把人指到 .env 上去 —— 迁移读的是 .env.target。"""
    for text in ("LARK_BASE_APP_TOKEN=bascnXYZ\n", "LARK_APP_ID=\nLARK_APP_SECRET=\n"):
        env = tmp_path / ".env.target"
        env.write_text(text, encoding="utf-8")

        try:
            load_target_settings(env)
        except MigrationError as exc:
            assert "LARK_APP_ID" in str(exc)
            assert str(env) in str(exc)  # 指的文件必须是 .env.target
            assert "open.feishu.cn/app" in str(exc)
            assert "bitable:app" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("应该报错")


def test_命令行默认只预演():
    args = cli.build_parser().parse_args(["--target-env", ".env.target"])
    assert args.apply is False
    assert args.include_audit is False
    assert args.include_commission is False
    assert args.create_base is None


def test_建Base的名字能传进去():
    args = cli.build_parser().parse_args(["--apply", "--create-base", "CRM 佣金看板"])
    assert args.create_base == "CRM 佣金看板"


# ---------- 把 token 写回环境文件 ----------


def test_写回环境文件保留注释和未改的行(tmp_path):
    from crm_basebot.migration import set_env_value

    env = tmp_path / ".env.target"
    env.write_text(
        "# 目标账号的配置\nLARK_APP_ID=cli_x\nLARK_BASE_APP_TOKEN=\n\n# 注释留着\n",
        encoding="utf-8",
    )

    set_env_value(env, "LARK_BASE_APP_TOKEN", "bascnXYZ")

    text = env.read_text(encoding="utf-8")
    assert "LARK_BASE_APP_TOKEN=bascnXYZ" in text
    assert "# 目标账号的配置" in text  # 注释不能被重排掉
    assert "# 注释留着" in text
    assert "LARK_APP_ID=cli_x" in text


def test_写回环境文件时键不存在就追加(tmp_path):
    from crm_basebot.migration import set_env_value

    env = tmp_path / ".env.target"
    env.write_text("LARK_APP_ID=cli_x\n", encoding="utf-8")

    set_env_value(env, "LARK_BASE_APP_TOKEN", "bascnXYZ")

    assert env.read_text(encoding="utf-8").splitlines() == [
        "LARK_APP_ID=cli_x",
        "LARK_BASE_APP_TOKEN=bascnXYZ",
    ]


def test_没有token且没给create_base时报人话(tmp_path):
    env = tmp_path / ".env.target"
    env.write_text("LARK_APP_ID=cli_x\nLARK_APP_SECRET=s\n", encoding="utf-8")

    try:
        load_target_settings(env)
    except MigrationError as exc:
        assert "LARK_BASE_APP_TOKEN" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应该报错")

    # 但「边建 Base 边迁移」那条路允许暂时没有 token
    assert load_target_settings(env, require_token=False).app_id == "cli_x"
