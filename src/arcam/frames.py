"""出图画幅（取景框）—— ``viewRect`` / ``frameRect`` 的离线等价。

原版的 viewRect 量的是**布局**（浏览器 UI 外壳：cover 铺满后画布落在屏幕外的那一圈、
底部常驻滑块条、软转屏），属浏览器版式；离线渲染没有这层外壳，按 SPEC §5「已知偏离」
如实退化为**整块画布**（documented deviation，不是 bug）。

frameRect 的三条分支逐字对应 index.html:1023-1039：
  · '满幅'(-1) → 整块画布（不先缩进可见区）
  · '自由'(0)  → viewRect（= 整块画布）
  · 其余画幅   → 在 viewRect 内取该宽高比的最大内接矩形，居中；
                 宽高保持浮点（比例不能因取整歪掉），原点 js_round 取整。
"""

from __future__ import annotations

from arcam.numeric import Rect, js_round
from arcam.placement import FRAMES

__all__ = ["FRAMES", "view_rect", "frame_rect"]


def view_rect(width: int, height: int) -> Rect:
    """离线版 viewRect：无布局外壳 → 整块画布。

    对应 ``viewRect``（index.html:951-997）。原版在物理玻璃坐标里扣掉屏幕外那一圈
    与常驻条高度，再换回画布坐标；离线渲染没有 UI 外壳，按 documented deviation
    取整块画布（宽高即入参，原点 0,0）。
    """
    return Rect(0.0, 0.0, float(width), float(height))


def frame_rect(frame_key: str, width: int, height: int) -> Rect:
    """出图矩形（画布坐标）—— 取景框 = 贴边吸附目标 = 出图裁切框。

    对应 ``frameRect``（index.html:998-1039，函数体 1023-1039）：
      · ``FRAMES[key] === -1``（'满幅'）→ ``{x:0, y:0, w:cv.width, h:cv.height}``；
      · ``ar = FRAMES[key] || 0`` 为 0（'自由' 或未知名——JS 里 undefined||0）→ viewRect；
      · 否则 ``w=V.w, h=w/ar``，``h > V.h`` 时改为 ``h=V.h, w=h*ar``（最大内接、居中），
        原点 ``Math.round`` 取整，宽高保持浮点。
    """
    ar = FRAMES.get(frame_key) or 0.0
    if ar == -1.0:
        return Rect(0.0, 0.0, float(width), float(height))
    v = view_rect(width, height)
    if not ar:
        return v
    w = v.w
    h = w / ar
    if h > v.h:
        h = v.h
        w = h * ar
    return Rect(
        float(js_round(v.x + (v.w - w) / 2)),
        float(js_round(v.y + (v.h - h) / 2)),
        w,
        h,
    )
