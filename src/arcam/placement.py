"""摆位 —— 贴地/自由两种立绘放置算法 + 三分线摆正 + 贴边吸附（index.html:2903-3025）。

纯数学模块：只依赖 numpy 的类型标注与 ``arcam.numeric`` / ``arcam.geometry``。
角度约定：对外度（``rot_deg``），内部弧度（``SpritePlacement.rot``、``snap_to_edge``
的 ``rot`` 都是弧度，与原版 ``charRot()`` 的 ``S.roll*D2R`` 一致）。

如实保留的已知偏离（docs/SPEC.md §5，artifact A-projection-core.md §6.5）：
- ``S.x`` 两处 clamp 不一致：``place`` ±6（index.html:3198）vs ``autoFix`` ±20
  （index.html:3020）——都保留，auto_fix_x 用 ±20、place 用 ±6。
- index.html:2911 的旧注释「吸附时锁 0」被 2912-2930 的注释明确推翻：吸附时**不**锁 0，
  立绘始终按 rotBasis 转（charRot，index.html:2930）。按代码行为实现。
- SPEC §3.2 冻结签名之外追加的关键字参数（ground 的 ``shot``、free 的
  ``free_p`` / ``edge_bar`` / ``rect``）全部带默认值，冻结签名调用点的行为不变；
  原版这些量来自全局 ``S`` / ``freeP`` / ``snapRect()``，离线版只能显式传入
  （snapRect 的布局外壳属浏览器专属，见 SPEC §5）。
- ``auto_fix_x`` 只回传归一化横坐标；原版 resnap 还会改 freeP.v，该信息不进返回值。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np

from arcam.geometry import Camera, project
from arcam.numeric import Rect, clampn, js_round

SHOTS: dict[str, float] = {'全身': 1.00, '膝上': 0.72, '腰上': 0.47, '胸上': 0.28, '头肩': 0.20}
"""景别表：值 = 立绘下沿离地高度 / 人物身高。index.html:946。"""

FRAMES: dict[str, float] = {'自由': 0.0, '1:1': 1.0, '4:5': 0.8, '16:9': 16 / 9, '9:16': 9 / 16, '满幅': -1.0}
"""出图画幅表：值 = 宽/高，0 = 不裁，-1 = 整块画布。index.html:950。"""

DEFAULT_FREE_P: tuple[float, float] = (0.5, 0.6)
"""自由模式图心的归一化默认位置（原版模块级 freeP 初值）。index.html:2334。"""

_DEG2RAD = math.pi / 180.0


@dataclass(frozen=True)
class Subject:
    """被拍摄的人物位姿。字段即原版 ``S`` 的对应量（index.html:921-937）。
    """

    height_m: float    # S.Hp 人物身高（米）
    distance_m: float  # S.d 人物到相机竖平面的纵深（米）
    lateral_m: float   # S.x 人物横向偏移（米）


@dataclass(frozen=True)
class SpritePlacement:
    """立绘外接矩形在画布上的位姿（像素 / 弧度）。即原版 groundPlacement /
    freePlacement 返回的 {u, v, h, w, rot}。rot 绕图心，正方向顺时针（画面 y 向下）。
    """

    u: float   # 外接矩形中心（画布像素）
    v: float
    w: float   # 外接矩形宽（像素）
    h: float   # 外接矩形高（像素）
    rot: float  # 弧度，绕图心


def _char_size(char_rgba: np.ndarray | None) -> tuple[int, int] | None:
    """立绘像素 (w, h)；无图/零尺寸返回 None（对应原版 !chCanvas 判据，2804/2971）。"""
    if char_rgba is None:
        return None
    shape = getattr(char_rgba, "shape", ())
    if len(shape) < 2 or shape[0] <= 0 or shape[1] <= 0:
        return None
    return (int(shape[1]), int(shape[0]))


def _free_p_of(value: tuple[float, float] | None) -> tuple[float, float]:
    """freeP 归一化：None → 模块级默认（对应 arcam-core.js ``freePU`` 的 None 兜底）。

    ``current_placement`` / ``auto_fix_x`` 是公开 API，调用方传 ``free_p=None``
    表示「用默认」；不收的话会在 ``free_p[0]`` 上抛 TypeError。
    """
    return DEFAULT_FREE_P if value is None else value


def _rotate_bar(bar: int, rot: float) -> int:
    """把基准边按旋转量转过去（切口朝向变了 → 要吸的边也换）。index.html:2937-2943。

    画面系 y 向下、正向旋转顺时针，故「下」转过 +90° 变「左」：下(2)→左(3)→上(1)→右(4)。
    """
    q = ((js_round(rot / (math.pi / 2.0)) % 4) + 4) % 4
    if not q:
        return bar
    order = (2, 3, 1, 4)
    i = order.index(bar) if bar in order else -1
    return bar if i < 0 else order[(i + q) % 4]


def ground_placement(char_rgba: np.ndarray | None, subj: Subject, cam: Camera,
                     char_scale: float = 1.0, rot_basis: str = 'world',
                     shot: str = '全身') -> SpritePlacement | None:
    """贴地摆位：立绘下沿落在景别那个高度上，上沿顶在头顶投影点。index.html:2789-2840
    （函数体 2803-2840）。

    yb = (1 - SHOTS[shot])·Hp；pb = project(x, yb-h, d)（下沿）；pt = project(x, Hp-h, d)
    （头顶）；span = hypot(pb→pt)；h = span·char_scale；中心 = pb 沿 pb→pt 方向推 h/2
    （锚点是下沿 pb，charScale=1 时中心正好落回 (pb+pt)/2）；w = h·立绘宽高比；
    rot = rot_basis=='frame' ? 0 : atan2(ux, -uy)（局部 -y 对齐 pb→pt）。

    返回 None 的情形（对应原版三条早退）：无图 / pb 或 pt 落到相机后方 / span < 2。
    ``shot`` 缺省 '全身'（原版 ``SHOTS[S.shot] || SHOTS['全身']``，2805，老存档兜底）。
    """
    size = _char_size(char_rgba)
    if size is None:
        return None
    k = SHOTS.get(shot) or SHOTS['全身']
    yb = (1.0 - k) * subj.height_m
    pb = project(subj.lateral_m, yb - cam.height_m, subj.distance_m, cam)
    pt = project(subj.lateral_m, subj.height_m - cam.height_m, subj.distance_m, cam)
    if pb is None or pt is None:
        return None
    span = math.hypot(pb[0] - pt[0], pb[1] - pt[1])
    if span < 2.0:
        return None
    h = span * char_scale
    ux = (pt[0] - pb[0]) / span
    uy = (pt[1] - pb[1]) / span
    rot = 0.0 if rot_basis == 'frame' else math.atan2(ux, -uy)
    return SpritePlacement(
        u=pb[0] + ux * h / 2.0,
        v=pb[1] + uy * h / 2.0,
        w=h * size[0] / size[1],
        h=h,
        rot=rot,
    )


def free_placement(char_rgba: np.ndarray | None, subj: Subject, cam: Camera,
                   size_pct: float = 60.0, rot_deg: float = 0.0,
                   free_p: tuple[float, float] = DEFAULT_FREE_P,
                   edge_bar: int = 0, rect: Rect | None = None) -> SpritePlacement | None:
    """自由模式：按画面高度 size_pct% 摆，图心在归一化 free_p。index.html:2945-2981
    （freeRaw 2946-2948、freePlacement 2970-2980）。

    原版位置来自模块级 freeP（初值 (0.5, 0.6)，index.html:2334）；贴边开着
    （edgeBar 非 0）时过 snapToEdge 吸附。rect 为 None 时退化为不吸附 —— 原版的
    snapRect 取「取景框 ∩ 可见区」，可见区属浏览器布局外壳，离线由调用方从
    arcam.frames 的 frame_rect/view_rect 交出来后显式传入。rot 由调用方给的
    rot_deg（度）转弧度 —— 经 current_placement 分派时默认跟随 cam.roll_deg
    （原版 charRot，2930），rot_basis='frame' 时为 0；直接调用本函数的冻结默认是
    rot_deg=0.0（不随相机斜）。
    """
    size = _char_size(char_rgba)
    if size is None:
        return None
    aspect = size[0] / size[1]
    rot = rot_deg * _DEG2RAD
    u = free_p[0] * cam.width
    v = free_p[1] * cam.height
    h = size_pct / 100.0 * cam.height
    if edge_bar and rect is not None:
        s = snap_to_edge(SpritePlacement(u=u, v=v, w=h * aspect, h=h, rot=rot),
                         edge_bar, rect, aspect, rot)
        u, v = s.u, s.v
    return SpritePlacement(u=u, v=v, w=h * aspect, h=h, rot=rot)


def current_placement(char_rgba: np.ndarray | None, subj: Subject, cam: Camera, *,
                      mode: str = 'ground', **kw) -> SpritePlacement | None:
    """按 mode 分派：'free' → free_placement，其余 → ground_placement。index.html:2981。

    kw 透传对应分支的参数（ground: char_scale/rot_basis/shot；free: size_pct/rot_deg/
    free_p/edge_bar/rect），对应原版全局 S / freeP / snapRect 的显式化。
    """
    if mode == 'free':
        rot_basis = str(kw.get('rot_basis', 'world'))
        rot_deg = kw.get('rot_deg')
        if rot_deg is None:
            rot_deg = 0.0 if rot_basis == 'frame' else cam.roll_deg
        return free_placement(
            char_rgba, subj, cam,
            size_pct=float(kw.get('size_pct', 60.0)),
            rot_deg=float(rot_deg),
            free_p=_free_p_of(kw.get('free_p')),
            edge_bar=int(kw.get('edge_bar', 0)),
            rect=kw.get('rect'),
        )
    return ground_placement(
        char_rgba, subj, cam,
        char_scale=float(kw.get('char_scale', 1.0)),
        rot_basis=str(kw.get('rot_basis', 'world')),
        shot=str(kw.get('shot', '全身')),
    )


def auto_fix_x(char_rgba: np.ndarray | None, subj: Subject, cam: Camera, *,
               mode: str = 'ground', **kw) -> tuple[float, str]:
    """自动摆正：把立绘横向吸到最近的三分线（1/3、1/2、2/3 三选一）。index.html:2983-3025
    （函数体 2995-3025）。

    返回 (new_x, status)：status ∈ 'nochar' | 'already' | 'moved:<pct>'。
    - ground：project() 非线性 → 取数值导数（dx=0.05）一步解，new_x 为新的 S.x，
      clamp ±20（index.html:3020；与 place 的 ±6 故意不一致，如实保留）。
    - free：原版改的是归一化 freeP.u（clamp [0,1]）后 resnap；new_x = 归一化横坐标
      （吸附后的 s.u/W）。'nochar'/'already' 时 new_x = 未动的当前位置
      （ground 为 subj.lateral_m，free 为 free_p[0]）。
    原版还有 draw()/syncSizeSliders 两个 UI 副作用，离线版无对应物，省略。
    """
    free_p = _free_p_of(kw.get('free_p'))
    keep_x = subj.lateral_m if mode != 'free' else float(free_p[0])
    if not cam.width or not cam.height:
        return (keep_x, 'nochar')          # 原版 !cv.width 分支（0×0 时 JS 同样落这里）
    c0 = current_placement(char_rgba, subj, cam, mode=mode, **kw)
    if c0 is None:
        return (keep_x, 'nochar')
    width = float(cam.width)
    xr = c0.u / width
    best = 1.0 / 3.0
    for cand in (0.5, 2.0 / 3.0):          # reduce 无初值：1/3 → 0.5 → 2/3，平手留前者
        if abs(xr - cand) < abs(xr - best):
            best = cand
    target_u = best * width
    if abs(c0.u - target_u) < 1.0:
        return (keep_x, 'already')
    moved = 'moved:' + str(js_round(best * 100.0))
    if mode == 'free':
        new_u = clampn(target_u / width, 0.0, 1.0)
        edge_bar = int(kw.get('edge_bar', 0))
        rect = kw.get('rect')
        if edge_bar and rect is not None:
            size = _char_size(char_rgba)
            if size is not None:
                aspect = size[0] / size[1]
                rot_basis = str(kw.get('rot_basis', 'world'))
                rot_deg = kw.get('rot_deg')
                if rot_deg is None:
                    rot_deg = 0.0 if rot_basis == 'frame' else cam.roll_deg
                rot = float(rot_deg) * _DEG2RAD
                h = float(kw.get('size_pct', 60.0)) / 100.0 * cam.height
                raw = SpritePlacement(u=new_u * width, v=float(free_p[1]) * cam.height,
                                      w=h * aspect, h=h, rot=rot)
                s = snap_to_edge(raw, edge_bar, rect, aspect, rot)
                new_u = s.u / width
        return (new_u, moved)
    keep = subj.lateral_m
    c1 = current_placement(char_rgba, replace(subj, lateral_m=keep + 0.05), cam,
                           mode='ground', **kw)
    if c1 is None:
        return (keep, 'nochar')
    grad = (c1.u - c0.u) / 0.05
    if not math.isfinite(grad) or abs(grad) < 1e-6:
        return (keep, 'nochar')
    return (clampn(keep - (c0.u - target_u) / grad, -20.0, 20.0), moved)


def snap_to_edge(c: SpritePlacement, bar: int, rect: Rect, aspect: float,
                 rot: float) -> SpritePlacement:
    """贴边吸附：把立绘吸到 rect 的某条边（1 上 / 2 下 / 3 左 / 4 右）。index.html:2874-2902
    （edgeGeom 2864-2872）。

    bw/bh 是**旋转后**外接盒（w·|cos|+h·|sin| / w·|sin|+h·|cos|）；rot 变化后
    rotateBar 换边。沿自由轴用 fit 钳位，两端**不排序**（图比框大时范围自然反过来，
    ＂让框始终被图盖住＂，可自由平移）。返回的 w = h·aspect、rot 原样带过去
    （原版 snapToEdge 只产出 {u,v,h}，w 由调用方按 charAspect 另算）。
    rot 为弧度（与 SpritePlacement.rot 同单位；原版收 charRot() 的弧度值）。
    """
    h = c.h
    if not (h > 0):
        return c
    if not rot:
        rot = 0.0
    w = h * aspect
    ac = abs(math.cos(rot))
    asn = abs(math.sin(rot))
    bw = w * ac + h * asn
    bh = w * asn + h * ac
    bar = _rotate_bar(bar, rot)

    def fit(v: float, a: float, b: float) -> float:
        lo = a if a < b else b
        hi = a if a > b else b
        return lo if v < lo else (hi if v > hi else v)

    if bar == 1:
        return SpritePlacement(fit(c.u, rect.x + bw / 2.0, rect.x + rect.w - bw / 2.0),
                               rect.y + bh / 2.0, w, h, c.rot)
    if bar == 2:
        return SpritePlacement(fit(c.u, rect.x + bw / 2.0, rect.x + rect.w - bw / 2.0),
                               rect.y + rect.h - bh / 2.0, w, h, c.rot)
    if bar == 3:
        return SpritePlacement(rect.x + bw / 2.0,
                               fit(c.v, rect.y + bh / 2.0, rect.y + rect.h - bh / 2.0),
                               w, h, c.rot)
    if bar == 4:
        return SpritePlacement(rect.x + rect.w - bw / 2.0,
                               fit(c.v, rect.y + bh / 2.0, rect.y + rect.h - bh / 2.0),
                               w, h, c.rot)
    return c
