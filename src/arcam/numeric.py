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
    """JS ``Math.round``：最近整数，平分向 +∞（-0.5 → -0, 0.5 → 1, 2.5 → 3）。

    index.html 里所有 Math.round 调用点（bakeLit 三条 LUT 边界、footPrint alpha、
    viewRect 原点取整、charLum 高度取整、frameRect 原点）都必须走这里。

    ⚠ 刻意不用教科书里的 ``math.floor(value + 0.5)`` 快捷式：``0.5`` 的下一个
    可表示 double（``0.49999999999999994``）加 0.5 时会向上溢到 1.0、得 1，而
    ECMA-262 规定「大于 0 且小于 0.5 的 x 结果为 +0」，JS 给 0。这里按规范逐字
    写：先 floor 再与 0.5 比（``value - lower`` 落在 [0,1) 且由 Sterbenz 精确），
    全定义域与 ``Math.round`` 一致（扫描 300 万随机样本 + 全部「整数±0.5」边界，
    与快捷式仅该一个输入不同）。
    """
    lower = math.floor(value)
    return lower + 1 if value - lower >= 0.5 else lower


def clampn(value: float, lo: float, hi: float) -> float:
    """JS clampN(v, a, b)：先夹范围。两端不排序（原版同样不排序）。
    """
    return lo if value < lo else (hi if value > hi else value)


def js_to_fixed(value: float, digits: int) -> float:
    """JS ``Number.prototype.toFixed(digits)`` 的数值版本（半远离零）。

    scene.json 里大量 ``+x.toFixed(2)`` / ``toFixed(3)``，取整语义不同会让
    配方里差 0.01。返回 float（JSON 序列化由调用方 str() 时决定位数，
    本函数只保证数值与 JS 一致）。

    ⚠ 与 ECMA-262 的 ``toFixed`` 有两处**已冻结**的偏离（红线：toFixed 口径
    不许动，docs/SPEC.md §1.4 与 docs/AUDIT-CHARTER.md §3）：
    ① 平分时半**远离零**，JS 取较大者（负半分向 +∞，如 ``(-0.125).toFixed(2)``
       在 JS 得 ``"-0.12"``，这里给 ``-0.13``）；
    ② 按 ``repr`` 最短串而非精确二进制值取整（2.675 的二进制实为 2.674999…，
       JS 得 ``"2.67"``，这里给 ``"2.68"``）。
    非平分区间两者一致，scene.json 的实测值几乎不触平。改动前先读红线。
    """
    quant = Decimal(1).scaleb(-digits)
    return float(Decimal(repr(value)).quantize(quant, rounding=ROUND_HALF_UP))


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
