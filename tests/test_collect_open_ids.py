"""从机器人日志里捞未登记的 open_id。

「收 open_id」这一步是整套流程里唯一只能靠人做的环节，而人做的那部分只该是**认人**
（日志只说「有人发过消息」，不说他是谁）。所以脚本要保证的是：抄得准、不重复、不猜。
测试就盯这三条。
"""

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


collect = _load("collect_open_ids")

SAMPLE = """\
[2026-09-19 23:55:05] [WARNING] crm_basebot.bot.auth: 未登记的 open_id 尝试操作: ou_aaaa1111
2026-09-19 23:55:06 INFO  Lark: 无关的行
[2026-09-20 00:01:02] [WARNING] crm_basebot.bot.auth: 未登记的 open_id 尝试操作: ou_bbbb2222
[2026-09-20 00:01:03] [WARNING] crm_basebot.bot.auth: 未登记的 open_id 尝试操作: ou_bbbb2222
"""


def test_只捞那一行的open_id():
    assert collect.extract_open_ids(SAMPLE) == Counter({"ou_bbbb2222": 2, "ou_aaaa1111": 1})


def test_不认识的写法不会误捞():
    """别把聊天内容里的 ou_ 当身份 —— 只认 auth.require() 打的那一行。"""
    text = "销售说：我的 open_id 是 ou_cccc3333\n有人贴了 ou_dddd4444 到群里\n"
    assert collect.extract_open_ids(text) == Counter()


def test_中文冒号也认():
    text = "未登记的 open_id 尝试操作：ou_eeee5555\n"
    assert collect.extract_open_ids(text) == Counter({"ou_eeee5555": 1})


def test_按出现次数排序():
    """发得多的排在前面：通常就是最急的那个（他一直在试）。"""
    candidates = Counter({"ou_a": 1, "ou_b": 5, "ou_c": 3})
    already, fresh = collect.partition(candidates, known=set())
    assert fresh == ["ou_b", "ou_c", "ou_a"]


def test_已经在名册里的不算新面孔():
    candidates = Counter({"ou_a": 2, "ou_b": 1})
    already, fresh = collect.partition(candidates, known={"ou_a"})
    assert already == ["ou_a"]
    assert fresh == ["ou_b"]


def test_名册全都不认识时全是新面孔():
    candidates = Counter({"ou_a": 1, "ou_b": 1})
    already, fresh = collect.partition(candidates, known={"ou_zzz"})
    assert already == []
    assert set(fresh) == {"ou_a", "ou_b"}


def test_默认日志路径和命令行开关():
    args = collect.build_parser().parse_args([])
    assert args.log == "logs/bot.log"
    assert args.apply is False
    assert args.name is None
