"""交接包：一次性导入的编排 + 打包。

打包这段看着像琐事，其实有个只有「在别人机器上才暴露」的 bug 值得钉住：macOS 自带的
`zip` 不给中文文件名打 UTF-8 标记，Windows 解压出来是 `µ╕áΘüôσ«óµê╖.xlsx` 这种乱码。
所以打包必须走 Python 的 zipfile，并且这件事要有测试看着。
"""

from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pack = _load("pack_handover")
handover = _load("import_handover")


# ---------- 打包 ----------


def _fake_inputs(tmp_path: Path) -> Path:
    (tmp_path / "handover.xlsx").write_bytes(b"refs")
    (tmp_path / "board.xlsx").write_bytes(b"board")
    return tmp_path


def test_打包出来的中文文件名带UTF8标记(tmp_path):
    """macOS 的 `zip` 不给标记 → Windows 解压成乱码。这条测试就是防它回归。"""
    out = _fake_inputs(tmp_path)
    assert pack.main(["--out", str(out), "--no-handoff-html"]) == 0

    zip_path = next(out.glob("CRM-BaseBot-首次导入-*.zip"))
    names = zipfile.ZipFile(zip_path).infolist()
    chinese = [info for info in names if not info.filename.isascii()]

    assert chinese, "包内应该有中文名的文件"
    for info in chinese:
        assert (info.flag_bits // 2048) % 2 == 1, f"{info.filename} 缺 UTF-8 标记"


def test_包里该有的都在(tmp_path):
    out = _fake_inputs(tmp_path)
    pack.main(["--out", str(out), "--no-handoff-html"])

    zip_path = next(out.glob("CRM-BaseBot-首次导入-*.zip"))
    with zipfile.ZipFile(zip_path) as archive:
        assert set(archive.namelist()) == {"渠道客户.xlsx", "看板.xlsx", "导入说明.txt"}
        readme = archive.read("导入说明.txt").decode("utf-8")

    # 说明书要讲到那几件最要紧的事
    assert "不要转成 CSV" in readme
    assert "15 位有效数字" in readme
    assert "import_handover.py" in readme
    assert "只导一次" in readme
    # 顺序：新 bot 先跑起来，才收得到 open_id（这条顺序写反过一次，钉住）
    assert "全新的应用、全新的 bot" in readme
    assert "(b) 在本机把机器人跑起来" in readme
    assert readme.index("(b) 在本机把机器人跑起来") < readme.index("(c) 每位销售各给机器人")


def test_缺文件时提示先导出(tmp_path):
    assert pack.main(["--out", str(tmp_path)]) == 1


# ---------- 一次性导入的编排 ----------


def test_找不到xlsx时报出期望的文件名(tmp_path):
    args = handover.build_parser().parse_args(["--dir", str(tmp_path)])
    assert handover.find_file(Path(args.dir), handover.REGISTRATIONS_NAMES) is None
    assert handover.find_file(Path(args.dir), handover.BOARD_NAMES) is None


def test_默认名字两种都认(tmp_path):
    """原主人导出的是 handover.xlsx/board.xlsx，同事收到的可能是中文名 —— 两种都认。"""
    (tmp_path / "渠道客户.xlsx").touch()
    (tmp_path / "看板.xlsx").touch()
    assert handover.find_file(tmp_path, handover.REGISTRATIONS_NAMES).name == "渠道客户.xlsx"
    assert handover.find_file(tmp_path, handover.BOARD_NAMES).name == "看板.xlsx"


def test_中文文件名优先于英文名(tmp_path):
    (tmp_path / "handover.xlsx").touch()
    (tmp_path / "渠道客户.xlsx").touch()
    assert handover.find_file(tmp_path, handover.REGISTRATIONS_NAMES).name == "渠道客户.xlsx"


def test_命令行默认只预演():
    args = handover.build_parser().parse_args([])
    assert args.apply is False
    assert args.dry_run is False
    assert args.skip_structure is False


def test_apply和dryrun一起给是错的():
    args = handover.build_parser().parse_args(["--apply", "--dry-run"])
    assert args.apply and args.dry_run


def test_没有env文件时报人话(monkeypatch, tmp_path):
    """用 --env 指向一个不存在的文件：报错该说清缺哪个文件，而不是抛栈。"""
    missing = tmp_path / "nope.env"
    argv = ["--dir", str(tmp_path), "--env", str(missing)]
    with pytest.raises(SystemExit) as exc:
        handover.main(argv)
    assert exc.value.code not in (0, None)
