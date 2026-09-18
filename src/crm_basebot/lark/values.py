"""把 Bitable 返回的字段值规整成 Python 值。

单独成模块是因为这里藏着这个项目最贵的一个 bug：客户UID 是 18–19 位数字，
超过 float64 能精确表示的整数上限（2^53 ≈ 9.0e15，16 位）。一旦它在某个环节
变成浮点数，尾数就被抹平了 —— 而抹平后的值看起来仍然是个合法的长数字，
join 时静默匹配到别的客户，佣金算到别人头上，没有任何报错。

所以 UID 全链路必须是字符串，且遇到浮点数要炸而不是凑合。

但只做字符串比较挡不住所有情况：如果 UID 在**进入 Base 之前**就被改坏了，
我们拿到的字符串本身已经是错的。最现实的来源是 Excel —— 交易明细是同事从内部
系统导出再导入的，而 Excel 只保留 15 位有效数字。文件末尾的
``looks_excel_truncated`` 就是用来事后诊断这种损伤的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# float64 能精确表示的最大整数
MAX_EXACT_INT = 2**53

# Excel 的有效数字上限。超过这个长度的整数进 Excel 后，多出来的低位会被抹成 0。
# 这不是拍的数：Excel 明确只保留 15 位有效数字（底层是 float64，约 15.95 位）。
EXCEL_SIGNIFICANT_DIGITS = 15


class PrecisionLossError(ValueError):
    """值以浮点数形式到达，无法保证精度。"""


def extract_text(value: Any) -> str:
    """把 Bitable 各种花样的字段值抽成纯文本。

    同一个逻辑上的「文本」，Bitable 可能给你：
      - 文本字段： "PLUTO STUDIO LIMITED"
      - 富文本/查找引用： [{"type": "text", "text": "577809207768677761"}]
      - 公式/单值查找引用： {"type": "text", "text": "..."}
      - 数字字段： 729.99
    """
    if value is None:
        return ""

    if isinstance(value, str):
        return value.strip()

    if isinstance(value, bool):
        return str(value)

    if isinstance(value, int):
        return str(value)

    if isinstance(value, float):
        # 整数值的 float 交给调用方决定是否可接受，这里如实转换
        return repr(value) if value != int(value) else str(int(value))

    if isinstance(value, dict):
        for key in ("text", "name", "value"):
            if key in value:
                return extract_text(value[key])
        return ""

    if isinstance(value, list):
        parts = [extract_text(item) for item in value]
        return ", ".join(p for p in parts if p)

    return str(value).strip()


def to_uid(value: Any) -> str:
    """把客户UID 规整成字符串，精度存疑就直接报错。

    浮点数一律拒绝：Bitable 若把 UID 存成「数字」字段，服务端就已经按 float64
    存了，精度在到达我们之前就丢了，客户端补救不了。这种情况必须让人看见，
    把字段类型改成文本，而不是让它悄悄算错账。
    """
    if value is None:
        return ""

    if isinstance(value, float):
        raise PrecisionLossError(
            f"客户UID 以浮点数 {value!r} 到达，精度已不可信。"
            "请把 Bitable 里该字段的类型从「数字」改成「文本」。"
        )

    if isinstance(value, int) and not isinstance(value, bool):
        if value >= MAX_EXACT_INT:
            # Python 的 int 是任意精度，这里是安全的；留个记录说明为何不拦
            return str(value)
        return str(value)

    if isinstance(value, list):
        texts = [to_uid(item) for item in value]
        non_empty = [t for t in texts if t]
        if len(non_empty) > 1:
            raise ValueError(f"客户UID 字段返回了多个值，无法确定用哪个：{non_empty!r}")
        return non_empty[0] if non_empty else ""

    if isinstance(value, dict):
        for key in ("text", "value", "name"):
            if key in value:
                return to_uid(value[key])
        return ""

    text = extract_text(value)
    return text


def to_number(value: Any) -> float | None:
    """把金额类字段转成 float。空值返回 None，区别于 0。

    金额用 float 是可以的：Pnl(USD) 这类数量级远在 2^53 以内，
    而且 Bitable 本来就是按 float64 存的。
    """
    if value is None or value == "":
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, int | float):
        return float(value)

    if isinstance(value, list):
        for item in value:
            result = to_number(item)
            if result is not None:
                return result
        return None

    if isinstance(value, dict):
        for key in ("value", "text", "number"):
            if key in value:
                return to_number(value[key])
        return None

    text = extract_text(value).replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def link_ids(value: Any) -> list[str]:
    """关联字段返回的 record_id 列表。

    同一个关联字段有三种返回形态：写入时给 ``["recxxx"]``，读回来可能是
    ``{"link_record_ids": [...]}``，也可能是 ``[{"record_id": "..."}]``。

    **空关联不是 None，而是 ``{"link_record_ids": None}``** —— 直接判断真假会把
    「没挂关联」当成「挂了关联」。这个坑在「抽查有多少行挂上了渠道」的自检里踩过一次
    （2026-09-18）：全部行都报成已挂，看着一切正常，其实一行都没挂。
    """
    if isinstance(value, dict):
        value = value.get("link_record_ids") or value.get("record_ids") or []
    if not isinstance(value, list):
        value = [value] if value else []

    ids: list[str] = []
    for item in value:
        if isinstance(item, str):
            if item:
                ids.append(item)
        elif isinstance(item, dict):
            found = item.get("record_id") or item.get("id")
            if found:
                ids.append(str(found))
    return ids


# ---------- Excel 损伤诊断 ----------
#
# 为什么需要这个：交易明细是同事每天从内部系统导出再导入 Base 的。只要中间过了
# 一手 Excel，18 位的 UID 就会被抹成 15 位有效数字 —— 而这发生在数据进 Base
# 之前，我们下游做多严格的字符串比较都救不回来，因为字符串本身已经是错的。
#
# 这是启发式，不是硬判定，所以调用方只应该告警，不应该据此中止对账。


def looks_scientific_notation(uid: str) -> bool:
    """5.77809E+17 这类形态 —— Excel 导出 CSV 时最典型的产物。

    UID 是纯整数，出现小数点或指数符号只能是被当成数字处理过。这个信号几乎不会
    误报，所以单个命中就足以下结论。
    """
    text = uid.strip()
    if not text:
        return False
    return "e" in text.lower() or "." in text


def looks_excel_truncated(uid: str) -> bool:
    """UID 是否呈现「被 Excel 抹掉低位」的特征。

    判定规则：长度超过 15 位，且尾部至少有 (长度 - 15) 个连续的 0。
    Excel 把超长整数舍入到 15 位有效数字，多出来的低位正好变成这么多个 0。

    误报权衡 —— 这条规则对单个值的可靠性完全取决于长度：

        18 位（真实数据的长度）：随机 UID 恰好以 3 个 0 结尾的概率 1/1000
        19 位：以 4 个 0 结尾，1/10000
        17 位：1/100
        16 位：1/10   ← 十个合法 UID 就有一个会被误报

    所以 16 位这一档单看一个值几乎没有判别力。这也是为什么不要拿单个命中就下
    结论，而要用 ``assess_uid_health`` 把观测比例和「纯属巧合时的期望比例」
    对比：真被 Excel 改过的话，命中率会是巧合水平的几十倍甚至 100%，
    这个差距在聚合层面非常清楚。

    阈值选 15 而不是 16（float64 的 2^53 是 16 位）的原因：我们诊断的是 Excel
    这个具体的损伤来源，而 Excel 的行为是 15 位。用 16 会漏掉 16 位 UID 被
    抹掉最后 1 位的情况。
    """
    digits = uid.strip()
    if not digits.isdigit():
        return False

    excess = len(digits) - EXCEL_SIGNIFICANT_DIGITS
    if excess <= 0:
        return False

    return digits.endswith("0" * excess)


def chance_of_false_positive(uid: str) -> float:
    """这个 UID 纯属巧合就命中 ``looks_excel_truncated`` 的概率。

    长度 L 的随机数字串以 (L-15) 个 0 结尾的概率是 10^-(L-15)。
    """
    digits = uid.strip()
    if not digits.isdigit():
        return 0.0

    excess = len(digits) - EXCEL_SIGNIFICANT_DIGITS
    if excess <= 0:
        return 0.0

    return 10.0**-excess


@dataclass(frozen=True)
class UidHealthReport:
    total: int
    long_count: int
    truncated: list[str]
    scientific: list[str]
    expected_by_chance: float
    """纯属巧合时，truncated 里预期会有多少个（不是比例，是个数）。"""

    @property
    def suspicious_count(self) -> int:
        return len(self.truncated) + len(self.scientific)

    @property
    def truncated_ratio(self) -> float:
        return len(self.truncated) / self.long_count if self.long_count else 0.0

    @property
    def verdict(self) -> str:
        """no_long_uid / clean / likely_damaged / inconclusive"""
        if self.long_count == 0:
            return "no_long_uid"

        if self.scientific:
            # 出现小数点或指数符号，没有别的解释
            return "likely_damaged"

        if not self.truncated:
            return "clean"

        # 命中数明显超过巧合期望，且不是孤例，才敢下结论。
        # 3 倍这个倍数是经验取值：期望值小的时候泊松波动相对大，
        # 单个命中很常见，所以同时要求至少 2 个。
        if len(self.truncated) >= 2 and len(self.truncated) > 3 * self.expected_by_chance:
            return "likely_damaged"

        return "inconclusive"


def assess_uid_health(uids: list[str]) -> UidHealthReport:
    """对一批 UID 做 Excel 损伤评估。

    单个值的判定是弱信号，聚合之后才有说服力 —— 见 ``looks_excel_truncated``
    里的误报分析。
    """
    truncated: list[str] = []
    scientific: list[str] = []
    long_count = 0
    expected = 0.0

    for uid in uids:
        text = uid.strip()
        if not text:
            continue

        if looks_scientific_notation(text):
            scientific.append(text)
            continue

        if len(text) > EXCEL_SIGNIFICANT_DIGITS and text.isdigit():
            long_count += 1
            expected += chance_of_false_positive(text)
            if looks_excel_truncated(text):
                truncated.append(text)

    return UidHealthReport(
        total=len([u for u in uids if u.strip()]),
        long_count=long_count,
        truncated=truncated,
        scientific=scientific,
        expected_by_chance=expected,
    )


def _fmt_expected(value: float) -> str:
    """期望值常常远小于 1，直接 %.1f 会显示成「0.0 个」，看着像 bug。"""
    return "不到 0.1 个" if 0 < value < 0.1 else f"{value:.1f} 个"


def uid_health_advice(report: UidHealthReport) -> str:
    """把评估结果翻译成一句可执行的话。"""
    if report.verdict == "no_long_uid":
        return "没有超过 15 位的 UID，不存在 Excel 截断风险。"

    if report.verdict == "clean":
        return f"检查了 {report.long_count} 个长 UID，没有发现 Excel 截断特征。"

    if report.verdict == "inconclusive":
        return (
            f"{len(report.truncated)} 个长 UID 以连续 0 结尾"
            f"（纯属巧合预期约 {_fmt_expected(report.expected_by_chance)}），"
            "在巧合范围内，暂不能判定为损坏。留意后续是否变多。"
        )

    lines = ["发现 UID 疑似在进入 Base 之前就被 Excel 改坏了。"]

    if report.scientific:
        lines.append(
            f"  · {len(report.scientific)} 个值带小数点或指数符号"
            f"（如 {report.scientific[0]}），这是 Excel 导出 CSV 的典型产物"
        )

    if report.truncated:
        lines.append(
            f"  · {len(report.truncated)} / {report.long_count} 个长 UID 以连续 0 结尾"
            f"（纯属巧合只预期 {_fmt_expected(report.expected_by_chance)}），"
            f"如 {report.truncated[0]}"
        )

    lines.append(
        "  这类损伤在下游无法修复 —— 字符串本身已经是错的，会静默匹配到别的客户或匹配不上。"
    )
    lines.append(
        "  请让同事在从内部系统导出时把 UID 那一列设成【文本】格式，"
        "不要让 Excel 按数字处理；已经导入的可疑数据需要重新导一次。"
    )

    return "\n".join(lines)
