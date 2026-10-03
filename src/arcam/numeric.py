"""JS 数值语义对齐工具 —— 原版在 canvas 上逐像素取整，Python 的 round 是银行家舍入，
直接搬 round() 会让 bakeLit 的 LUT、footPrint 的 alpha、viewRect 原点全部系统性偏移。

对应关系（全部有 JS 源码出处，见 docs/SPEC.md「共享约定」）：
- ``Math.round``  → :func:`js_round`（半向 +∞，即正数半分进/负半分进）
- ``Math.clamp`` 类 → :func:`clampn`
- ``Number.prototype.toFixed`` → :func:`js_to_fixed`（半远离零到固定小数位）
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


def js_round(value: float) -> int:
    """JS ``Math.round``：向 +∞ 方向舍入（-0.5 → -0, 0.5 → 1, 2.5 → 3）。

    index.html 里所有 Math.round 调用点（bakeLit 三条 LUT 边界、footPrint alpha、
    viewRect 原点取整、charLum 高度取整、frameRect 原点）都必须走这里。
    """
    return math.floor(value + 0.5)


def clampn(value: float, lo: float, hi: float) -> float:
    """JS clampN(v, a, b)：先夹范围。两端不排序（原版同样不排序）。
    """
    return lo if value < lo else (hi if value > hi else value)


def clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def js_to_fixed(value: float, digits: int) -> float:
    """JS ``Number.prototype.toFixed(digits)`` 的数值版本（半远离零）。

    scene.json 里大量 ``+x.toFixed(2)`` / ``toFixed(3)``，取整语义不同会让
    配方里差 0.01。返回 float（JSON 序列化由调用方 str() 时决定位数，
    本函数只保证数值与 JS 一致 —— 需要字符串表示处用 :func:`js_fixed_str`）。
    """
    quant = Decimal(1).scaleb(-digits)
    return float(Decimal(repr(value)).quantize(quant, rounding=ROUND_HALF_UP))


def js_fixed_str(value: float, digits: int) -> str:
    """``toFixed`` 的字符串版本：固定位数、不带尾随处理。
    """
    quant = Decimal(1).scaleb(-digits)
    return str(Decimal(repr(value)).quantize(quant, rounding=ROUND_HALF_UP))


def to_u8(value: float) -> int:
    """[0,1] 浮点 → 0..255 int（半向 +∞，与画布逐通道量化一致）。"""
    return int(clamp01(value) * 255.0 + 0.5)


@dataclass(frozen=True)
class Rect:
    """画布坐标矩形（浮点宽高、原点多取整）。对应 JS 的 {x,y,w,h}。
    """

    x: float
    y: float
    w: float
    h: float

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def clamp_to(self, width: int, height: int) -> "Rect":
        """对应 clampRegion：整框越界时返回全图；否则四边夹回 [0, W/H]，
        保持宽高浮点语义。零填充/裁剪策略的细则见 docs/SPEC.md §clampRegion。
        """
        x = min(max(self.x, 0.0), float(width))
        y = min(max(self.y, 0.0), float(height))
        return Rect(x, y, min(self.w, float(width)), min(self.h, float(height)))
