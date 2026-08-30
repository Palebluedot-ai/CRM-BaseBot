"""把 Bitable 返回的字段值规整成 Python 值。

单独成模块是因为这里藏着这个项目最贵的一个 bug：客户UID 是 18–19 位数字，
超过 float64 能精确表示的整数上限（2^53 ≈ 9.0e15，16 位）。一旦它在某个环节
变成浮点数，尾数就被抹平了 —— 而抹平后的值看起来仍然是个合法的长数字，
join 时静默匹配到别的客户，佣金算到别人头上，没有任何报错。

所以 UID 全链路必须是字符串，且遇到浮点数要炸而不是凑合。
"""

from __future__ import annotations

from typing import Any

# float64 能精确表示的最大整数
MAX_EXACT_INT = 2**53


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
