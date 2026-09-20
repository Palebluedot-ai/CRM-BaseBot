"""缺凭证时用户看到什么。

这些测试盯的是一件很容易退化的事：**报错必须是我们写的那段人话，不是 pydantic 的
ValidationError 栈回溯**。退化的方式也很具体 —— 某天有人在新入口里直接 `get_settings()`，
那个入口就悄悄退回裸 traceback，而所有别的测试照样绿。所以这里既测文案，也测
「没有任何入口绕过 startup」。

## 隔离

两处必须小心，不然这些测试会在装好 .env 的机器上静默变成永远通过：

1. `monkeypatch.chdir(tmp_path)` —— pydantic-settings 按**当前工作目录**找 .env，
   在项目根目录下跑测试的话，开发者自己那份真 .env 会被读进来。
2. `get_settings.cache_clear()` —— 它带 lru_cache，别的测试读成功过一次就会留在缓存里。

测试全程不读、也不打印任何真实 .env 的内容。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from crm_basebot import app, startup
from crm_basebot.config import Settings, get_settings
from crm_basebot.jobs import reconcile
from crm_basebot.startup import (
    PROJECT_ROOT,
    MissingConfigError,
    load_settings,
    require_settings,
)

# .env / 环境变量里所有属于本项目的名字。清干净才谈得上隔离。
_OUR_ENV_PREFIXES = ("LARK_", "TABLE_", "REFERRAL_", "LOG_LEVEL")


def _stripped_environ() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(_OUR_ENV_PREFIXES)}


@pytest.fixture
def bare_process(monkeypatch, tmp_path):
    """把进程摘成「刚 clone 完、什么都没配」的样子。"""
    for key in list(os.environ):
        if key.startswith(_OUR_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def pristine_project(bare_process, tmp_path, monkeypatch):
    """再造一个「只有 .env.example、还没有 .env」的项目根，并把 PROJECT_ROOT 指过去。

    文案里那几条建议（cp 还是 cd）取决于项目根下有没有 .env。跑测试的机器上多半是
    有的 —— 开发者自己那份 —— 所以不换掉 PROJECT_ROOT 的话，这些断言会随机器状态摇摆。
    """
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env.example").write_text("LARK_APP_ID=\n", encoding="utf-8")
    monkeypatch.setattr(startup, "PROJECT_ROOT", root)
    monkeypatch.chdir(root)
    return root


# ---------- 抛的是我们的错，不是 pydantic 的 ----------


def test_缺凭证抛的是我们的错而不是pydantic的ValidationError(bare_process):
    with pytest.raises(MissingConfigError) as excinfo:
        load_settings()

    assert not isinstance(excinfo.value, ValidationError)
    # SystemExit 的子类：解释器会打印这段话并以 1 退出，入口不用写任何 except
    assert isinstance(excinfo.value, SystemExit)


def test_我们的错不会被宽泛的except_Exception吞掉(bare_process):
    """MissingConfigError 继承 SystemExit，不是 Exception 的子类。

    这条不是形式主义：谁在入口外面包一层 `except Exception` 兜底日志，就会把这段
    唯一有用的话换成一句「未知错误」。
    """
    with pytest.raises(SystemExit):
        try:
            load_settings()
        except Exception:  # noqa: BLE001 - 正是要证明它捞不到
            pytest.fail("MissingConfigError 被 except Exception 捞走了")


def test_报错文案点名了缺的每一个变量(bare_process):
    with pytest.raises(MissingConfigError) as excinfo:
        load_settings()

    message = str(excinfo.value)
    assert "LARK_APP_ID" in message
    assert "LARK_APP_SECRET" in message


def test_报错文案给的是下一步动作而不只是缺了什么(pristine_project):
    """「缺哪个变量 / 怎么建 .env / 去哪拿这个值」三件事一件都不能少。"""
    with pytest.raises(MissingConfigError) as excinfo:
        load_settings()

    message = str(excinfo.value)
    assert "cp .env.example .env" in message, "没告诉人怎么建 .env"
    assert "凭证与基础信息" in message, "没告诉人去开发者后台哪个页面拿"
    assert "docs/LARK_APP_SETUP.md 第 2 步" in message, "没指向文档的具体步骤"


def test_值不合法时也给人话而不是栈回溯(bare_process, monkeypatch):
    monkeypatch.setenv("LARK_APP_ID", "cli_test")
    monkeypatch.setenv("LARK_APP_SECRET", "secret")
    monkeypatch.setenv("REFERRAL_AUTO_NUMBER", "大概吧")

    with pytest.raises(MissingConfigError) as excinfo:
        load_settings()

    message = str(excinfo.value)
    assert "REFERRAL_AUTO_NUMBER" in message
    assert "true / false" in message


# ---------- .env 建到哪去了：那个带空格的孪生目录 ----------


def test_报错文案给出env的绝对路径(bare_process):
    """只说「建个 .env」不够 —— 用户真正卡住的地方是建到了哪个目录。"""
    with pytest.raises(MissingConfigError) as excinfo:
        load_settings()

    message = str(excinfo.value)
    assert str(PROJECT_ROOT / ".env") in message, "没说 .env 该建在哪个绝对路径下"
    assert str(bare_process) in message, "还要说清现在是在哪个目录下跑的"


def test_路径是运行时解析出来的不是写死的():
    """换台机器、换个用户名、换个 clone 位置，这段话都得说对地方。"""
    source = (PROJECT_ROOT / "src" / "crm_basebot" / "startup.py").read_text(encoding="utf-8")

    assert "/Users/" not in source
    assert PROJECT_ROOT == Path(startup.__file__).resolve().parents[2]


def test_只差首尾空格的孪生目录会被找出来(tmp_path):
    root = tmp_path / "CRM-BaseBot"
    twin = tmp_path / "CRM-BaseBot "
    root.mkdir()
    twin.mkdir()
    (tmp_path / "CRM-BaseBot-old").mkdir()  # 名字不同，不算孪生

    found = startup._lookalike_dirs(root)

    assert [p.name for p in found] == ["CRM-BaseBot "]


def test_有孪生目录时报错里会点名它(bare_process, tmp_path, monkeypatch):
    """真踩过的坑：`.env` 建进了结尾带空格的那个目录，程序读不到，报错却一模一样。

    路径打在终端里看起来完全相同，不明说没人会想到是这个原因。
    """
    root = tmp_path / "proj"
    (tmp_path / "proj ").mkdir()
    root.mkdir()
    monkeypatch.setattr(startup, "PROJECT_ROOT", root)

    note = startup._where_to_put_env()

    assert "'proj '" in note
    assert str(root / ".env") in note


def test_env在项目根但人在别处跑时只叫人cd不叫人再建一份(pristine_project, monkeypatch, tmp_path):
    """这时候再劝人 `cp .env.example .env` 就会多出第二份配置，反而更难查。"""
    (pristine_project / ".env").write_text("", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    steps = startup._how_to_create_env()

    assert f"cd {pristine_project}" in steps
    assert "cp .env.example" not in steps


def test_env就在当前目录时不会再劝人建一个(bare_process):
    (bare_process / ".env").write_text("", encoding="utf-8")

    note = startup._where_to_put_env()

    assert "变量名拼错" in note
    assert "cp .env.example" not in note


# ---------- 凭证之外的必填项 ----------


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, LARK_APP_ID="cli_test", LARK_APP_SECRET="secret", **overrides)


def test_没填app_token时点名它并说去哪拿():
    with pytest.raises(MissingConfigError) as excinfo:
        require_settings(_settings(), "LARK_BASE_APP_TOKEN")

    message = str(excinfo.value)
    assert "LARK_BASE_APP_TOKEN" in message
    assert "/base/" in message, "没说 token 在多维表格地址栏的哪一段"
    assert "第 7 步" in message


def test_没填table_id时指向探查脚本():
    with pytest.raises(MissingConfigError) as excinfo:
        require_settings(_settings(), "TABLE_REFERRAL", "TABLE_CLIENT")

    message = str(excinfo.value)
    assert "TABLE_REFERRAL" in message
    assert "TABLE_CLIENT" in message
    assert "scripts/inspect_base.py" in message


def test_填了就放行():
    require_settings(_settings(LARK_BASE_APP_TOKEN="bascnABC"), "LARK_BASE_APP_TOKEN")


def test_变量名写错了立刻炸():
    """调用方拼错变量名是代码 bug —— 静默放过的话，这个入口就等于没做检查。"""
    with pytest.raises(KeyError):
        require_settings(_settings(), "TABLE_REFERAL")


# ---------- 别让下一个入口漏掉 ----------


# 纯粹本地、一个凭证都不读的脚本。上面那条规则的目的是「缺凭证时要给人话而不是 pydantic 栈
# 回溯」—— 对没有凭证可缺的脚本不适用。往里加名字之前先问一句：它真的不需要任何配置吗？
NO_CONFIG_SCRIPTS = {
    "pack_handover.py",  # 打包交接材料：只读本地文件、写 zip，不连任何服务
    "bot_connectivity_report.py",  # 读机器人日志算空窗：纯文本分析，不需要 Base 也不需要凭证
}


ENTRY_POINTS = sorted(
    [PROJECT_ROOT / "src" / "crm_basebot" / "app.py"]
    + [PROJECT_ROOT / "src" / "crm_basebot" / "jobs" / "reconcile.py"]
    + [
        path
        for path in (PROJECT_ROOT / "scripts").glob("*.py")
        if path.name not in NO_CONFIG_SCRIPTS
    ]
)


@pytest.mark.parametrize("path", ENTRY_POINTS, ids=lambda p: p.name)
def test_没有入口绕过startup直接读配置(path: Path):
    source = path.read_text(encoding="utf-8")

    assert "get_settings" not in source, (
        f"{path.name} 直接调了 get_settings()，缺凭证时它会甩一段 pydantic 栈回溯。"
        "改成 crm_basebot.startup.load_settings()。"
    )
    assert "load_settings" in source, f"{path.name} 没有从 startup 拿配置"


def test_入口声明的每个必填项都有取值说明():
    """新加一个必填变量却忘了写「去哪拿」，报错就退回「缺了什么」那个层次。"""
    declared = set(app.REQUIRED_KEYS) | set(reconcile.REQUIRED_KEYS) | set(reconcile.WRITE_KEYS)

    assert declared <= set(startup._HINTS)


def test_文案里的变量名都是真实存在的配置项():
    """变量被改名或删掉时，把这段文案一起带走，别留下一句指向不存在变量的指引。"""
    assert set(startup._HINTS) <= set(startup._FIELD_BY_ENV)


# ---------- 真跑一遍：退出码非 0、stderr 里没有裸 traceback ----------

_ENTRY_COMMANDS = [
    (["scripts/ws_smoke.py"], "ws_smoke"),
    (["scripts/inspect_base.py"], "inspect_base"),
    (["scripts/sync_base.py"], "sync_base"),
    (["scripts/verify_numbering.py"], "verify_numbering"),
    (["scripts/seed_dev_data.py", "--open-id", "ou_test"], "seed_dev_data"),
    (["-m", "crm_basebot.app"], "app"),
    (["-m", "crm_basebot.jobs.reconcile"], "reconcile"),
]


@pytest.mark.parametrize("argv,name", _ENTRY_COMMANDS, ids=[name for _, name in _ENTRY_COMMANDS])
def test_每个入口在没有凭证时都以非零退出且不吐traceback(argv, name, tmp_path):
    """真起一个进程跑，因为这里要验的恰好是进程级的行为：退出码和 stderr 长什么样。

    cwd 指到一个空目录，进程环境里所有 LARK_* / TABLE_* 都摘掉 —— 既保证读不到
    开发机上可能存在的真 .env，也保证这个测试在 CI 和本地行为一致。
    """
    env = _stripped_environ()
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")

    command = [sys.executable]
    command += [str(PROJECT_ROOT / arg) if arg.startswith("scripts/") else arg for arg in argv]

    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode != 0, f"{name} 缺凭证却成功退出了"
    assert "Traceback (most recent call last)" not in result.stderr, (
        f"{name} 吐的是裸 traceback：\n{result.stderr}"
    )
    assert "ValidationError" not in result.stderr, f"{name} 漏出了 pydantic 的原始报错"
    assert "LARK_APP_ID" in result.stderr, f"{name} 没说缺哪个变量"
    assert "docs/LARK_APP_SETUP.md" in result.stderr, f"{name} 没指向文档"
    assert str(PROJECT_ROOT / ".env") in result.stderr, f"{name} 没说 .env 该建在哪"
    # 具体给的是 cp 还是 cd，取决于跑测试这台机器上有没有真 .env —— 两条都是可执行的
    # 下一步动作，有一条就行。文案本身由上面几个用 pristine_project 的测试钉住。
    assert "cp .env.example .env" in result.stderr or f"cd {PROJECT_ROOT}" in result.stderr, (
        f"{name} 没给出下一步该敲什么"
    )
