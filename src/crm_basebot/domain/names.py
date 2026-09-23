"""人名和机构名的比对用规整。只做比对的键，不改要写进 Base 的值。

三份资料里同一个人写法不一样，这是常态而不是意外：

  · 大小写、多余空格          ``JIANG JUN`` / ``Jiang  Jun``
  · 姓名顺序                  ``KE JIAHUI`` / ``JIAHUI KE``
  · 标点                      ``BG TECHNOLOGY VENTURE PTE LTD`` / ``… PTE. LTD.``
  · **全角半角**              ``Kevin Yu (于海峰）`` —— 左括号是半角 U+0028，
                              右括号是全角 U+FF09。肉眼几乎看不出来，字符串比较
                              却直接不相等。2026-09 就是在这里对不上人的。

所以 ``norm`` 先把全角折成半角再比。``tokens`` 在此之上再去标点、按词排序，
用来兜姓名顺序颠倒 —— 它**会**把不同的名字撞到一起（``LI MING`` 和 ``MING LI``），
所以调用方拿它命中时要单独标出来让人过目，不能当成和 ``norm`` 一样可靠。
"""

from __future__ import annotations

import re

# 全角 ！ 到 ～（U+FF01–U+FF5E）和半角 ! 到 ~（U+0021–U+007E）差一个固定的 0xFEE0。
# 全角空格 U+3000 不在这个区间，单独映射。
_WIDTH_FOLD = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
_WIDTH_FOLD[0x3000] = 0x20

_PUNCTUATION = re.compile(r"[.,'\"()\[\]{}*/\\&·—–-]")
_WHITESPACE = re.compile(r"\s+")


def fold_width(value: object) -> str:
    """全角标点和空格折成半角。中文汉字不受影响（不在映射区间里）。"""
    return str(value or "").translate(_WIDTH_FOLD)


def norm(value: object) -> str:
    """比对用的规整名：折全角、压空白、转大写。"""
    return _WHITESPACE.sub(" ", fold_width(value)).strip().upper()


def tokens(value: object) -> str:
    """顺序无关的键：``KE JIAHUI`` 和 ``JIAHUI KE`` 都归到 ``JIAHUI KE``。

    比 ``norm`` 宽，也因此比 ``norm`` 容易错配。用它命中的结果要让人确认。
    """
    cleaned = _PUNCTUATION.sub(" ", norm(value))
    return " ".join(sorted(cleaned.split()))
