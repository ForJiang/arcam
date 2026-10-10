"""HUD 取景装饰层 —— index.html:1746-1972 的 PIL 对译（FEATURE-SPEC §2）。

六件套与原版逐字对应：

- :func:`frame_overlay`   ↔ ``drawFrame``        index.html:1746-1772
- :func:`grid_paths` / :func:`draw_grid` ↔ ``gridPaths``/``drawGrid``  1774-1824
- :func:`draw_horizon`    ↔ ``drawHorizon``      1826-1837
- :func:`draw_figure`     ↔ ``drawFigure``       1839-1876
- :func:`composition_hints` ↔ ``compositionHints`` 1890-1937（11 条）
- :func:`draw_hud_text`   ↔ ``hud``              1957-1972

口径：几何全部走 :func:`arcam.geometry.project`（不自己实现投影）；颜色/线宽/
半径按原版 canvas 常量逐字对译（canvas 的 ``lineWidth`` → PIL ``width``；
``rgba()`` → 预合成到 RGB；``setLineDash`` → 自绘虚线段）。
⚠ 出图链路（compose_shot）**不画** HUD——原版"那些只在实时预览的 draw() 里画"
（index.html:3487-3490）。本模块只服务预览与 CLI ``--overlay`` 显式开关。

字体：HUD 读数要渲染中文，PIL 位图默认字撑不住 CJK——按候选表找 truetype
（macOS Hiragino/STHeiti、Linux Noto、Windows Microsoft YaHei），全缺才回落
``load_default``（届时中文变方框，但不抛）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from arcam.geometry import Camera, focal_px, project
from arcam.placement import SHOTS, SpritePlacement, Subject

# ─────────────────────── 字体 ───────────────────────

_FONT_CANDIDATES = (
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "C:/Windows/Fonts/msyh.ttc",
)


def _font(size: int) -> ImageFont.FreeTypeFont:
    """按候选表取第一个可用的 truetype；全缺回落位图默认字（不抛）。"""
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                       # 老 Pillow 无 size 参数
        return ImageFont.load_default()


# ─────────────────────── 颜色常量（原版逐字） ───────────────────────

#: drawFrame：框外压暗 0.58、框线白 .9、三分线白 .30（index.html:1754-1763）
_DIM = (0, 0, 0, 0.58)
_FRAME_LINE = (255, 255, 255, 0.9)
_THIRD_LINE = (255, 255, 255, 0.30)

#: drawGrid：描边黑 .6；主线青 (120,255,255)、次线 (90,225,255,.9)（1807-1809）
_GRID_OUTLINE = (0, 0, 0, 0.6)
_GRID_MAJOR = (120, 255, 255, 1.0)
_GRID_MINOR = (90, 225, 255, 0.9)

#: drawHorizon：红 (255,120,120,.8)、标签 .95（1832-1834）
_HORIZON = (255, 120, 120, 0.8)
_HORIZON_LABEL = (255, 120, 120, 0.95)

#: drawFigure：骨色 (255,212,121,.95/.75)、足底点 (120,255,180,.95)（1858-1872）
_BONE = (255, 212, 121, 0.95)
_BONE_LIGHT = (255, 212, 121, 0.75)
_FOOT = (120, 255, 180, 0.95)

#: drawFigure 的关节比例（index.html:1842-1843，身高的分数）
_JOINT_RATIOS = {
    "TOP": 1.00, "HEAD": 0.9335, "CHIN": 0.867, "NECK": 0.855, "SHLD": 0.80,
    "WAIST": 0.61, "HIP": 0.53, "KNEE": 0.28, "ANKLE": 0.045, "SOLE": 0.0,
}
_SW = 0.105      # 肩半宽（×Hp）
_HW = 0.072      # 髋半宽
_FW = 0.048      # 踝/足间距半宽

#: 判接地用的景别覆盖门限（原版 cover >= 0.95 = 全身，index.html:1930）
_COVER_FULL = 0.95

#: drawGrid 的网格世界尺寸（index.html:1776-1792）
_GRID_X_RANGE = range(-6, 7)                    # 纵线 x ∈ [-6,6] 步长 1
_GRID_D_LINES = (1, 2, 3, 4, 5, 6, 8, 10, 14, 20)   # 横线纵深
_GRID_D_MIN, _GRID_D_MAX = 0.3, 26.0            # 纵线的纵深范围
_GRID_D_STEPS = 300                              # 纵线采样点数（i/300）
_GRID_X_SPAN = (-7.0, 7.0)                       # 横线的横向范围
_GRID_X_STEPS = 60                               # 横线采样点数（i/60）
_GRID_LABEL_X = -1.4                             # 标签锚点（project 的 x 参）


@dataclass(frozen=True)
class GridPath:
    """一条网格线（供测试与 JS 侧对拍）。"""

    pts: list[tuple[float, float] | None]   # None = 断点（投影被剔除）
    major: bool
    label: float | None = None              # 横线才有（米）


# ─────────────────────── 工具 ───────────────────────

def _to_pil(img: np.ndarray) -> Image.Image:
    """(H,W,3) uint8 → PIL RGB（带 alpha 的入参丢掉 alpha；浮点入参按 01 口径量化）。"""
    arr = np.asarray(img)
    if arr.dtype != np.uint8:
        arr = np.clip(arr * 255.0 if arr.max() <= 1.0 else arr, 0, 255).astype(np.uint8)
    if arr.shape[2] == 4:
        arr = arr[..., :3]
    return Image.fromarray(arr, "RGB")


def _flat(rgba: tuple[float, float, float, float],
          bg: tuple[int, int, int] = (0, 0, 0)) -> tuple[int, int, int]:
    """rgba(0-1 alpha) 预合成到黑底 → RGB（原版 canvas 浮层下是视频/照片，
    这里按黑底近似——HUD 是辅助线，与原版同款"压暗"观感一致）。"""
    r, g, b, a = rgba
    return (int(round((r * a + bg[0] * (1 - a)) * 255)),
            int(round((g * a + bg[1] * (1 - a)) * 255)),
            int(round((b * a + bg[2] * (1 - a)) * 255)))


# ─────────────────────── drawFrame ───────────────────────

def frame_overlay(img: np.ndarray, rect, canvas_size: tuple[int, int]) -> np.ndarray:
    """取景框压暗 + 框线 + 三分线（index.html:1746-1772 的 1:1 对译）。

    ``rect`` 是 :class:`arcam.numeric.Rect`（frame_rect 的输出）。框≈可见区时
    **不画**（早退条件逐字：``R.x <= V.x+0.5`` 等——画了只会给整屏描一圈白边）。
    这里 ``canvas_size`` 即可见区（离线无布局外壳，view_rect = 整块画布）。
    """
    from arcam.numeric import Rect as _Rect
    if not isinstance(rect, _Rect):
        rect = _Rect(*rect)
    W, H = canvas_size
    # 早退：框铺满可见区（0.5px 容差）→ 不画
    if (rect.x <= 0.5 and rect.y <= 0.5
            and rect.x + rect.w >= W - 0.5 and rect.y + rect.h >= H - 0.5):
        return img
    pil = _to_pil(img)
    draw = ImageDraw.Draw(pil)
    dim = _flat(_DIM)
    if rect.y > 0.5:
        draw.rectangle([0, 0, W, rect.y], fill=dim)
    if rect.y + rect.h < H - 0.5:
        draw.rectangle([0, rect.y + rect.h, W, H], fill=dim)
    if rect.x > 0.5:
        draw.rectangle([0, rect.y, rect.x, rect.y + rect.h], fill=dim)
    if rect.x + rect.w < W - 0.5:
        draw.rectangle([rect.x + rect.w, rect.y, W, rect.y + rect.h], fill=dim)
    draw.rectangle([rect.x + 1, rect.y + 1, rect.x + rect.w - 2, rect.y + rect.h - 2],
                   outline=_flat(_FRAME_LINE), width=2)
    third = _flat(_THIRD_LINE)
    for i in (1, 2):
        x = rect.x + rect.w * i / 3
        y = rect.y + rect.h * i / 3
        # ⚠ 原版三分线 lineWidth=1.5：PIL 的 width 只收整数（传 float 直接
        # TypeError），这里如实记下取整口径（向下取 1），不改渲染行为
        draw.line([(x, rect.y), (x, rect.y + rect.h)], fill=third, width=1)
        draw.line([(rect.x, y), (rect.x + rect.w, y)], fill=third, width=1)
    return np.asarray(pil)


# ─────────────────────── drawGrid ───────────────────────

def grid_paths(cam: Camera, subject_x: float) -> list[GridPath]:
    """地面网格的两组线（index.html:1774-1794 ``gridPaths`` 的 1:1 对译）。

    纵线：世界 x ∈ [-6,6]（步长 1），每条沿 d ∈ [0.3, 26] 采 301 点，投影被
    剔除处断线（pts 里插 None）；x==0 是 major。
    横线：世界 d ∈ {1,2,3,4,5,6,8,10,14,20}，每条沿 x ∈ [-7,7] 采 61 点；
    d==5 是 major，且带米数标签。
    """
    out: list[GridPath] = []
    for x in _GRID_X_RANGE:
        pts: list[tuple[float, float] | None] = []
        pen = False
        for i in range(_GRID_D_STEPS + 1):
            d = _GRID_D_MIN + (i / _GRID_D_STEPS) * (_GRID_D_MAX - _GRID_D_MIN)
            q = project(x, -cam.height_m, d, cam)
            if q is None:
                pen = False
                continue
            if pen:
                pts.append(q)
            else:
                pts.extend([None, q])       # 断点后再起笔（原版同款双 push）
                pen = True
        out.append(GridPath(pts=pts, major=(x == 0)))
    for d in _GRID_D_LINES:
        pts = []
        for i in range(_GRID_X_STEPS + 1):
            x = _GRID_X_SPAN[0] + (i / _GRID_X_STEPS) * (_GRID_X_SPAN[1] - _GRID_X_SPAN[0])
            q = project(x, -cam.height_m, float(d), cam)
            if q is not None:
                pts.append(q)
        out.append(GridPath(pts=pts, major=(d == 5), label=float(d)))
    return out


def draw_grid(img: np.ndarray, cam: Camera, subject_x: float = 0.0) -> np.ndarray:
    """地面网格两遍绘制（index.html:1796-1824）：先黑描边、再青色主线/次线。

    横线标签：``project(-1.4, -h, d)`` 锚点、偏移 (+8,-8)、黑描边后填青；
    ``q.v`` 出画布不画（1816）；字号 20（原版 ``bold 20px``，PIL 候选表没有
    粗体分支，用 4px 描边补重量感）。

    ⚠ 次线原版 lineWidth=1.8：PIL 只收整数，如实记下取整口径（向下取 1）。
    ``subject_x`` 与原版 ``gridPaths()`` 一样不参与几何（网格锚在世界系），
    留在签名里只为与 :func:`grid_paths` 对拍时同参。
    """
    pil = _to_pil(img)
    draw = ImageDraw.Draw(pil)
    paths = grid_paths(cam, subject_x)
    for pass_i in (0, 1):
        for path in paths:
            if pass_i == 0:
                color, width = _flat(_GRID_OUTLINE), (7 if path.major else 5)
            else:
                color = _flat(_GRID_MAJOR if path.major else _GRID_MINOR)
                width = 3 if path.major else 1
            # PIL 无 lineCap/lineJoin 的 round 直 API；用 joint="curve" 近似
            seg: list[tuple[float, float]] = []
            for q in path.pts:
                if q is None:
                    if len(seg) >= 2:
                        draw.line(seg, fill=color, width=width, joint="curve")
                    seg = []
                    continue
                seg.append(q)
            if len(seg) >= 2:
                draw.line(seg, fill=color, width=width, joint="curve")
    label_font = _font(20)
    H = pil.height
    for path in paths:
        if path.label is None:
            continue
        q = project(_GRID_LABEL_X, -cam.height_m, path.label, cam)
        if q is None or q[1] > H or q[1] < 0:
            continue
        text = f"{int(path.label)}m"
        x, y = q[0] + 8, q[1] - 8
        draw.text((x, y), text, font=label_font,
                  fill=_flat((0, 0, 0, 0.75)), stroke_width=4, stroke_fill=_flat((0, 0, 0, 0.75)))
        draw.text((x, y), text, font=label_font, fill=_flat(_GRID_MAJOR))
    return np.asarray(pil)


# ─────────────────────── drawHorizon ───────────────────────

def draw_horizon(img: np.ndarray, cam: Camera, label: str = "地平线") -> np.ndarray:
    """地平线（index.html:1826-1837）：按 pitch/roll 的直线 + 虚线 + 标签。

    参数式（原版逐字）：t ∈ [-12, 12]，
    ``u = W/2 + F·(t·cos r + tan p·sin r)``、``v = H/2 + F·(t·sin r − tan p·cos r)``；
    标签锚点 t = −0.22、偏移 (+10,−10)。虚线 [12,8] 自绘（PIL 无 setLineDash）。
    """
    pil = _to_pil(img)
    draw = ImageDraw.Draw(pil)
    F = focal_px(cam)
    p = math.radians(cam.pitch_deg)
    r = math.radians(cam.roll_deg)
    tp = math.tan(p)

    def pt(t: float) -> tuple[float, float]:
        return (pil.width / 2 + F * (t * math.cos(r) + tp * math.sin(r)),
                pil.height / 2 + F * (t * math.sin(r) - tp * math.cos(r)))

    a, b = pt(-12.0), pt(12.0)
    color = _flat(_HORIZON)
    _dashed_line(draw, a, b, color, width=2, dash=(12, 8))
    lab = pt(-0.22)
    draw.text((lab[0] + 10, lab[1] - 10), label, font=_font(22),
              fill=_flat(_HORIZON_LABEL))
    return np.asarray(pil)


def _dashed_line(draw: ImageDraw.ImageDraw, a, b, color, width=1, dash=(12, 8)):
    """自绘虚线（原版 setLineDash([12,8]) 的对译；沿线段按 dash 比例切）。"""
    ax, ay = a
    bx, by = b
    length = math.hypot(bx - ax, by - ay)
    if length < 1e-6:
        return
    ux, uy = (bx - ax) / length, (by - ay) / length
    period = dash[0] + dash[1]
    pos = 0.0
    while pos < length:
        end = min(pos + dash[0], length)
        draw.line([(ax + ux * pos, ay + uy * pos), (ax + ux * end, ay + uy * end)],
                  fill=color, width=width)
        pos += period


# ─────────────────────── drawFigure ───────────────────────

def draw_figure(img: np.ndarray, cam: Camera, subject: Subject) -> np.ndarray:
    """人体骨架参考（index.html:1839-1876 的 1:1 对译）。

    关节比例 / 半宽（SW=0.105Hp、HW=0.072Hp、FW=0.048Hp）/ 骨色与足底点全部
    逐字。``P(lx, ly) = project(S.x + lx, ly - S.h, S.d)``（局部坐标 → 相机系）；
    任何关节投影被剔除（None）时相关线段跳过（原版 seg() 的 ``if(!a||!b) return``）。
    """
    pil = _to_pil(img)
    draw = ImageDraw.Draw(pil)
    Hp = subject.height_m

    def P(lx: float, ly: float):
        return project(subject.lateral_m + lx, ly - cam.height_m, subject.distance_m, cam)

    J = {
        "soleL": P(-_FW * Hp, 0.0), "soleR": P(_FW * Hp, 0.0),
        "ankleL": P(-_FW * Hp, _JOINT_RATIOS["ANKLE"] * Hp),
        "ankleR": P(_FW * Hp, _JOINT_RATIOS["ANKLE"] * Hp),
        "kneeL": P(-_FW * 0.9 * Hp, _JOINT_RATIOS["KNEE"] * Hp),
        "kneeR": P(_FW * 0.9 * Hp, _JOINT_RATIOS["KNEE"] * Hp),
        "hipL": P(-_HW * Hp, _JOINT_RATIOS["HIP"] * Hp),
        "hipR": P(_HW * Hp, _JOINT_RATIOS["HIP"] * Hp),
        "waist": P(0.0, _JOINT_RATIOS["WAIST"] * Hp),
        "shldL": P(-_SW * Hp, _JOINT_RATIOS["SHLD"] * Hp),
        "shldR": P(_SW * Hp, _JOINT_RATIOS["SHLD"] * Hp),
        "neck": P(0.0, _JOINT_RATIOS["NECK"] * Hp),
        "chin": P(0.0, _JOINT_RATIOS["CHIN"] * Hp),
        "head": P(0.0, _JOINT_RATIOS["HEAD"] * Hp),
        "top": P(0.0, _JOINT_RATIOS["TOP"] * Hp),
        "elbowL": P(-_SW * 1.18 * Hp, 0.635 * Hp),
        "elbowR": P(_SW * 1.18 * Hp, 0.635 * Hp),
        "wristL": P(-_SW * 1.30 * Hp, 0.445 * Hp),
        "wristR": P(_SW * 1.30 * Hp, 0.445 * Hp),
    }
    bone, bone_l = _flat(_BONE), _flat(_BONE_LIGHT)

    def seg(a, b, w, c):
        if not a or not b:
            return
        draw.line([a, b], fill=c, width=w)

    seg(J["hipL"], J["hipR"], 7, bone)
    seg(J["shldL"], J["shldR"], 7, bone)
    seg(J["waist"], J["neck"], 9, bone)
    seg(J["hipL"], J["kneeL"], 9, bone_l)
    seg(J["kneeL"], J["ankleL"], 7, bone_l)
    seg(J["ankleL"], J["soleL"], 5, bone_l)
    seg(J["hipR"], J["kneeR"], 9, bone_l)
    seg(J["kneeR"], J["ankleR"], 7, bone_l)
    seg(J["ankleR"], J["soleR"], 5, bone_l)
    seg(J["shldL"], J["elbowL"], 7, bone_l)
    seg(J["elbowL"], J["wristL"], 6, bone_l)
    seg(J["shldR"], J["elbowR"], 7, bone_l)
    seg(J["elbowR"], J["wristR"], 6, bone_l)
    seg(J["neck"], J["chin"], 6, bone)
    if J["head"] and J["top"] and J["chin"]:
        ry = abs(J["top"][1] - J["chin"][1]) / 2
        draw.ellipse([J["head"][0] - ry * 0.72, J["head"][1] - ry,
                      J["head"][0] + ry * 0.72, J["head"][1] + ry],
                     outline=bone, width=4)
    for s in (J["soleL"], J["soleR"]):
        if not s:
            continue
        draw.ellipse([s[0] - 7, s[1] - 7, s[0] + 7, s[1] + 7], fill=_flat(_FOOT))
    return np.asarray(pil)


# ─────────────────────── compositionHints ───────────────────────

def composition_hints(cam: Camera, placement: SpritePlacement | None, *,
                      canvas_size: tuple[int, int], shot: str = "全身",
                      mode: str = "ground") -> list[str]:
    """构图体检 11 条（index.html:1890-1937 的 1:1 对译，全部并列无优先级）。

    判据（逐字）：地平线位置 hr∈[0.42,0.58]；pitch>40 压扁；pitch<−35 拉长；
    **整块出画早退**（中心出画但半个身子在画面里是常见构图，不报）；三分线
    near>0.11；fill>0.92 顶满；fill<0.10 偏小；cover≥0.95 且 ground 且
    cutV<hv−H*0.03 浮空。

    ⚠ 删掉的 4 条（反解身高 ×2 / 眼高差 ×2）**不许复活**——index.html:1939-1955
    有据：它们只是在把大小滑块读回给用户听（hp ≡ S.Hp × charScale，zc/focal/
    cos p 全约掉），与场景一点关系都没有。
    """
    W, H = canvas_size
    if W < 8 or H < 8:
        return []
    F = focal_px(cam)
    # 地平线的像素高度。俯角向下为正，所以压得越低头抬得越高，地平线往上跑
    hv = H / 2 - F * math.tan(math.radians(cam.pitch_deg))
    hr = hv / H
    out: list[str] = []

    if 0.42 <= hr <= 0.58:
        out.append("地平线在正中，抬一点或压一点")
    if cam.pitch_deg > 40:
        out.append(f"俯角 {cam.pitch_deg:.0f}°，人物会被压扁")
    if cam.pitch_deg < -35:
        out.append("仰角偏大，人物会被拉长")

    if placement is not None and placement.w > 0:
        hw, hh = placement.w / 2, placement.h / 2
        if (placement.u + hw < 0 or placement.u - hw > W
                or placement.v + hh < 0 or placement.v - hh > H):
            out.append("人物出画了 —— 抬/压一点镜头，或把距离调远")
            return out                       # 出画时其余几条都是拿画外坐标算的
        xr = placement.u / W
        near = min(abs(xr - 1 / 3), abs(xr - 2 / 3), abs(xr - 0.5))
        if near > 0.11:
            out.append("角色挪到三分线上更稳")
        fill = placement.h / H
        if fill > 0.92:
            out.append("角色快顶满画面")
        if fill < 0.10:
            out.append("角色偏小")
        # 要景别够全（cover ≥ 0.95 = 全身）才判接地——半身景别下"脚"不在画面里
        cover = SHOTS.get(shot, 1.0)
        if cover >= _COVER_FULL and mode == "ground":
            cut_v = placement.v + placement.h / 2
            if cut_v < hv - H * 0.03:
                out.append("脚在地平线以上，浮空了")
    return out


# ─────────────────────── hud ───────────────────────

def draw_hud_text(img: np.ndarray, cam: Camera, subject: Subject, *,
                  has_char: bool, canvas_size: tuple[int, int],
                  lang: str = "zh", shot: str = "全身",
                  mode: str = "ground",
                  placement: SpritePlacement | None = None) -> tuple[np.ndarray, list[str]]:
    """HUD 读数（index.html:1957-1972 的 1:1 对译）。

    三行：俯角/滚转（一位小数）；预览 W×H 与 focal（取整）；人物占画面高%
    （一位小数）或 behind/nochar 警告。第四行是构图提示（``⚠`` 前缀、全角
    中点连接）。返回 (画后的图, hints 列表)。
    """
    from arcam.i18n import t
    pil = _to_pil(img)
    draw = ImageDraw.Draw(pil)
    W, H = canvas_size
    a = project(subject.lateral_m, -cam.height_m, subject.distance_m, cam)
    b = project(subject.lateral_m, subject.height_m - cam.height_m, subject.distance_m, cam)
    px = abs(a[1] - b[1]) if (a and b) else 0.0
    lines: list[str] = []
    lines.append(t("hud.line1", f"{cam.pitch_deg:.1f}", f"{cam.roll_deg:.1f}", lang=lang))
    lines.append(t("hud.line2", W, H, f"{focal_px(cam):.0f}", lang=lang))
    if has_char:
        if a and b:
            lines.append(t("hud.person", f"{px / H * 100:.1f}", lang=lang))
        else:
            lines.append(t("hud.behind", lang=lang))
    else:
        lines.append(t("hud.nochar", lang=lang))
    font = _font(15)
    y = 8
    for line in lines:
        draw.text((8, y), _strip_tags(line), font=font, fill=(255, 255, 255),
                  stroke_width=2, stroke_fill=(0, 0, 0))
        y += 20
    hints = composition_hints(cam, placement, canvas_size=canvas_size,
                              shot=shot, mode=mode)
    return np.asarray(pil), hints


def _strip_tags(s: str) -> str:
    """HUD 文案表里带 <b> 标签（网页 innerHTML 用），PIL 文本要去掉。"""
    out = s.replace("<b>", "").replace("</b>", "")
    return out
