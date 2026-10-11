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
    """JS ``clampN(v, a, b)``：先夹范围。

    原版逐字是 ``Math.min(b, Math.max(a, x))``（index.html:1161）。两端有序
    （``a <= b``，本项目全部调用点都是）时两式逐值相同，这里写成比较链是省掉
    两次内置函数调用的等价形式（``clampn`` 在光照 LUT / 阴影量化里逐字节跑）。

    ⚠ 两端**无序**（``a > b``）时两式不同：原版退化成常数 ``b``（``Math.max``
    先抬到 ``a``、``Math.min`` 再压回 ``b``），本式在 ``x < a`` 时给 ``a``。
    当前调用点全部有序（``auto_fix_x`` 的 ±20、``free`` 分支的 [0,1]、光照的
    LIGHT_GAIN_MIN/MAX 等），该分歧不可达；真要传无序边界请按原版写
    ``min(hi, max(lo, value))``。
    """
    return lo if value < lo else (hi if value > hi else value)


def js_to_fixed(value: float, digits: int) -> float:
    """JS ``Number.prototype.toFixed(digits)`` 的数值版本（半远离零）。

    scene.json 里大量 ``+x.toFixed(2)`` / ``toFixed(3)``，取整语义不同会让
    配方里差 0.01。返回 float（JSON 序列化由调用方 str() 时决定位数，
    本函数只保证数值与 JS 一致）。

    ⚠ 与 ECMA-262 的 ``toFixed`` 有**一处**已冻结的偏离（红线：toFixed 口径
    不许动，docs/SPEC.md §1.4 与 docs/AUDIT-CHARTER.md §3）：
    ① 按 ``repr`` 最短串而非**精确二进制值**取整。规范第 5 步用 double 的精确
       数学值找最近的 n/10^f，而这里用 ``repr`` 的最短十进制串：``2.675`` 的
       二进制实为 2.674999…，JS 得 ``"2.67"``，这里给 ``2.68``；
       ``(-0.15).toFixed(1)`` 同样 —— JS 得 ``-0.1``，这里给 ``-0.2``
       （受影响的是 2 位的 centerPixel/sizePixel/focalPx 与 3 位的
       pitchDeg/rollDeg/rotationDeg，实测见 tests/test_frames.py）。
    非平分区间两者一致，scene.json 的实测值几乎不触平。改动前先读红线。

    ✅ **不**是偏离的一项（曾在此误记为偏离①，已按 node 实测更正）：
    平分方向。规范先取 ``x = -x`` 再对**幅值**找最近的 n，即半**远离零**
    （``(-0.125).toFixed(2)`` 在 JS 得 ``"-0.13"``、``(-0.135).toFixed(2)``
    得 ``"-0.14"``），与 ``ROUND_HALF_UP`` 逐字同向；负零也一致
    （``(-0.001).toFixed(2)`` → ``"-0.00"`` → ``float('-0.0')``）。

    ✅ 非有限入参原样返回：JS 的 ``(NaN).toFixed(n)`` 得字符串 ``"NaN"``、
    ``+"NaN"`` 回解析成 NaN（``Infinity`` 同理），而 ``Decimal('NaN').quantize``
    会抛 ``InvalidOperation``。``pitchDeg``/``rotationDeg`` 等字段的上游可能是
    非有限角（见 :func:`arcam.geometry.project` 的同款收口），这里一并兜住。
    """
    if not math.isfinite(value):
        return value
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
        """右边界（x + w）。原版 JS 一律手写 ``R.x + R.w``，离线版收成属性。"""
        return self.x + self.w

    @property
    def y2(self) -> float:
        """下边界（y + h）。原版 JS 一律手写 ``R.y + R.h``，离线版收成属性。"""
        return self.y + self.h
