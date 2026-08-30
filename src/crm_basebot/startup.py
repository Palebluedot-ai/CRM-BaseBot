"""进程启动时读配置：读得到就往下跑，读不到就给一段照着做就能修好的话。

所有用户可能直接执行的入口都从这里拿 Settings —— scripts/ 下那五个脚本、
``crm_basebot.app``、``crm_basebot.jobs.reconcile``。

## 为什么单开一个模块，而不是每个入口自己 try 一下

第一次跑这个项目的人看到的第一条错误信息，决定了他下一步是照着做还是去翻源码。
默认那条是 pydantic 的：

    pydantic_core._pydantic_core.ValidationError: 2 validation errors for Settings
    LARK_APP_ID
      Field required [type=missing, input_value={}, input_type=dict]

它说对了「缺什么」，但没说要建 `.env`、没说去哪拿这个值、也没说文档在哪 —— 三件
真正决定下一步动作的事一件都没讲。

这段文案里带着具体命令和文档步骤号，抄七份之后改一次文档就得记得改七处，必然漂移。
所以只留一份，每个入口一行调用。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from .config import Settings, get_settings

# src/crm_basebot/startup.py -> 项目根。用运行时解析，不写死任何绝对路径。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

ENV_FILE_NAME = ".env"
ENV_EXAMPLE_NAME = ".env.example"

SETUP_DOC = "docs/LARK_APP_SETUP.md"

# 环境变量名 -> Settings 上的属性名。从模型的 alias 反推，避免再抄一份对应关系。
_FIELD_BY_ENV: dict[str, str] = {
    str(field.alias): name for name, field in Settings.model_fields.items() if field.alias
}

# 每个必填项「去哪拿」。一项一到两行：第一行是动作，第二行是文档出处。
# 步骤号跟着 docs/LARK_APP_SETUP.md 走，改文档记得回来对一眼。
#
# table_id 那六个共用一段说明 —— 它们的来源完全一样，分开写只会让六段慢慢长得不一样。
_TABLE_HINT = (
    f"跑 `uv run python scripts/inspect_base.py`，把它列出来的 table_id 回填进 {ENV_FILE_NAME}",
    f"{SETUP_DOC} 第 8 步。表都还没建就先跑第 9 步的 `scripts/sync_base.py --apply`",
)

_HINTS: dict[str, tuple[str, ...]] = {
    "LARK_APP_ID": (
        "cli_ 开头的那一串。开发者后台 https://open.feishu.cn/app"
        " → 你的应用 → 左侧「凭证与基础信息」",
        f"{SETUP_DOC} 第 2 步。应用还没建就从第 1 步看起 —— 个人版租户里建的应用用不了",
    ),
    "LARK_APP_SECRET": (
        "和 App ID 同一个页面「凭证与基础信息」，点一下就能看到",
        f"{SETUP_DOC} 第 2 步",
    ),
    "LARK_BASE_APP_TOKEN": (
        "多维表格地址栏里 /base/ 后面那一段：https://xxx.feishu.cn/base/<就是这段>?table=...",
        f"{SETUP_DOC} 第 7 步。同一步的「添加文档应用」别漏，漏了 token 填对也读不到数据",
    ),
    "TABLE_REFERRAL": _TABLE_HINT,
    "TABLE_CLIENT": _TABLE_HINT,
    "TABLE_TRANSACTION": _TABLE_HINT,
    "TABLE_COMMISSION": _TABLE_HINT,
    "TABLE_AUDIT": _TABLE_HINT,
    "TABLE_SALES": _TABLE_HINT,
}


class MissingConfigError(SystemExit):
    """必需的配置没填。

    继承 SystemExit 而不是 Exception 是有意的：这不是上层能「处理」的异常，入口拿到
    它唯一该做的就是把话打出来、以非 0 退出。而 ``SystemExit(str)`` 的解释器默认行为
    正好是把字符串打到 stderr 并返回 1，所以入口一行 except 都不用写。

    另一个好处：它不是 Exception 的子类，``except Exception`` 捞不到，不会被哪个
    宽泛的兜底 catch 悄悄吞掉再换成一句更没用的话。
    """


def load_settings() -> Settings:
    """读配置。缺凭证时抛 MissingConfigError，而不是让 pydantic 的栈回溯打到用户脸上。"""
    try:
        return get_settings()
    except ValidationError as exc:
        missing = [str(e["loc"][0]) for e in exc.errors() if e["type"] == "missing" and e["loc"]]
        if missing:
            raise MissingConfigError(_missing_message(missing)) from None
        raise MissingConfigError(_invalid_message(exc)) from None


def require_settings(settings: Settings, *env_keys: str) -> None:
    """确认这几个变量填了值。空的就抛 MissingConfigError，带上各自的取值方式。

    凭证之外的东西（app_token、各表 table_id）在 Settings 里都有默认空串 —— 探查
    Base 之前它们本来就该是空的，不能在读配置的时候一律拦死。所以哪个入口需要哪几个，
    由入口自己在开跑前声明一次。
    """
    unknown = [key for key in env_keys if key not in _FIELD_BY_ENV]
    if unknown:
        # 调用方把变量名写错了。这是代码 bug，不是用户配置问题，要立刻炸而不是静默放过。
        raise KeyError(f"未知的配置项：{', '.join(unknown)}")

    missing = [key for key in env_keys if not getattr(settings, _FIELD_BY_ENV[key])]
    if missing:
        raise MissingConfigError(_incomplete_message(missing))


# ---------- 以下都是拼文案 ----------


def _bullets(env_keys: list[str]) -> str:
    lines = []
    for key in env_keys:
        lines.append(f"  · {key}")
        lines.extend(f"      {line}" for line in _HINTS.get(key, (f"见 {SETUP_DOC}",)))
    return "\n".join(lines)


def _lookalike_dirs(root: Path) -> list[Path]:
    """找出只在首尾空格上和项目根目录不同的兄弟目录。

    真踩过的坑：`CRM-BaseBot` 旁边还躺着一个 `CRM-BaseBot `（结尾一个空格）。
    `.env` 建到那个里面去，程序一个字都读不到，而报错和「压根没建过」完全一样 ——
    路径在终端里看起来还一模一样，肉眼分不出来。所以发现有这种孪生目录就点名说一句。
    """
    try:
        siblings = list(root.parent.iterdir())
    except OSError:
        return []
    return [
        path
        for path in siblings
        if path.name != root.name and path.name.strip() == root.name.strip() and path.is_dir()
    ]


def _active_env_file() -> Path:
    """当前这次运行实际会读到的 .env。

    pydantic-settings 按**当前工作目录**找 .env，不是按项目根目录 —— 这正是
    「明明建了却读不到」的来源，所以要如实报当前工作目录下的那个。
    """
    cwd_env = Path.cwd() / ENV_FILE_NAME
    return cwd_env if cwd_env.exists() else PROJECT_ROOT / ENV_FILE_NAME


def _where_to_put_env() -> str:
    """`.env` 到底该建在哪。给「建了却读不到」和「压根还没建」两种情况用。"""
    cwd = Path.cwd()
    root_env = PROJECT_ROOT / ENV_FILE_NAME

    if (cwd / ENV_FILE_NAME).exists():
        return (
            f"（{cwd / ENV_FILE_NAME} 是读到了的，所以文件在，只是上面这几项还空着 ——"
            "也可能是变量名拼错了，对着 .env.example 核一遍。）"
        )

    parts = [
        f"{ENV_FILE_NAME} 按**当前工作目录**找，所以它既要建在项目根目录，跑的时候也要待在那儿：",
        f"    应该在这里    {root_env}",
        f"    现在在这里跑  {cwd}",
    ]

    twins = _lookalike_dirs(PROJECT_ROOT)
    if twins:
        listed = "、".join(f"'{path.name}'" for path in twins)
        parts.append(
            f"注意 {PROJECT_ROOT.parent} 下还有只差首尾空格的同名目录（{listed}）。"
            f"真正的项目是上面那个绝对路径 —— {ENV_FILE_NAME} 建进孪生目录的话，"
            "程序读不到，报出来的还是现在这一段，很难看出问题出在路径上。"
        )

    return "\n".join(parts)


def _how_to_create_env() -> str:
    if (Path.cwd() / ENV_FILE_NAME).exists():
        return f"改完 {Path.cwd() / ENV_FILE_NAME} 重跑一次就行。"

    if (PROJECT_ROOT / ENV_FILE_NAME).exists():
        # 文件是有的，只是没在它旁边跑。这时候再劝人 cp 一份出来只会多出第二份配置。
        return "\n".join(
            [
                f"  cd {PROJECT_ROOT}",
                f"  # {ENV_FILE_NAME} 已经在这个目录下了，cd 过去重跑就行（原因见下）",
            ]
        )

    steps = [f"  cd {PROJECT_ROOT}"]
    if (PROJECT_ROOT / ENV_EXAMPLE_NAME).exists():
        steps.append(f"  cp {ENV_EXAMPLE_NAME} {ENV_FILE_NAME}")
    else:
        steps.append(f"  touch {ENV_FILE_NAME}   # 仓库里没有 {ENV_EXAMPLE_NAME}，只能手建")
    steps.append(f"  # 用编辑器打开 {ENV_FILE_NAME}，把上面几项填上，然后重跑这条命令")
    return "\n".join(steps)


def _missing_message(missing: list[str]) -> str:
    return "\n".join(
        [
            "读不到飞书应用凭证，起不来。",
            "",
            f"这几项是必填的，现在还没有（{ENV_FILE_NAME} 里的变量，或者进程环境变量）：",
            _bullets(missing),
            "",
            "照着做：",
            _how_to_create_env(),
            "",
            _where_to_put_env(),
        ]
    )


def _incomplete_message(missing: list[str]) -> str:
    return "\n".join(
        [
            "这几项配置还是空的，这一步跑不下去：",
            _bullets(missing),
            "",
            f"填进 {_active_env_file()} 再重跑。",
        ]
    )


def _invalid_message(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        key = str(error["loc"][0]) if error["loc"] else "(未知变量)"
        lines.append(f"  · {key}：{error['msg']}")
    return "\n".join(
        [
            f"{ENV_FILE_NAME} 里有配置项的值不合法：",
            *lines,
            "",
            f"对着 {ENV_EXAMPLE_NAME} 核一遍格式（布尔值写 true / false），"
            f"改 {_active_env_file()} 再重跑。",
        ]
    )
