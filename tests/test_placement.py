"""placement 模块测试：贴地/自由摆位、三分线摆正、贴边吸附的行为与 golden 值。

golden 期望值按 artifact A-projection-core.md §2.3 / §3.5 的公式手工推导（推导中间量
写在注释里）；立绘用程序合成的几何图（RGBA），只用到其宽高比与存在性。
"""

from __future__ import annotations

import math
import sys
import types
from pathlib import Path

import numpy as np
import pytest

try:
    from arcam.placement import (
        DEFAULT_FREE_P,
        FRAMES,
        SHOTS,
        SpritePlacement,
        Subject,
        auto_fix_x,
        current_placement,
        free_placement,
        ground_placement,
        snap_to_edge,
    )
except ModuleNotFoundError as _exc:
    if _exc.name in ("arcam.geometry", "arcam.placement"):
        raise
    # 并行开发期：arcam/__init__.py 会 import 其他 Agent 尚未创建的模块。注入一个只带
    # __path__ 的空 arcam 包（跳过 __init__ 的 re-export 副作用），使本文件可单独加载
    # geometry / placement；兄弟模块就位后本分支不会触发。
    _pkg = types.ModuleType("arcam")
    _pkg.__path__ = [str(Path(__file__).resolve().parents[1] / "src" / "arcam")]
    sys.modules["arcam"] = _pkg
    from arcam.placement import (
        DEFAULT_FREE_P,
        FRAMES,
        SHOTS,
        SpritePlacement,
        Subject,
        auto_fix_x,
        current_placement,
        free_placement,
        ground_placement,
        snap_to_edge,
    )
    from arcam.geometry import Camera
    from arcam.numeric import Rect

from arcam.geometry import Camera
from arcam.numeric import Rect

F = 1275.828595201775  # focal_px(1920×1080, feq=23) = 960·23/17.3064（test_geometry 已推导）


def _char(w: int = 60, h: int = 100) -> np.ndarray:
    """合成一张 w×h 的几何立绘：中间一块不透明人体（alpha=255），四角透明。"""
    img = np.zeros((h, w, 4), np.uint8)
    img[int(h * 0.1):int(h * 0.9), int(w * 0.25):int(w * 0.75), 0:3] = 200
    img[int(h * 0.1):int(h * 0.9), int(w * 0.25):int(w * 0.75), 3] = 255
    return img


def _cam(pitch: float = 0.0, roll: float = 0.0, w: int = 1920, h: int = 1080,
         feq: float = 23.0, height: float = 1.6) -> Camera:
    return Camera(width=w, height=h, height_m=height, feq_mm=feq,
                  pitch_deg=pitch, roll_deg=roll)


def _subj(hp: float = 1.65, d: float = 4.0, x: float = 0.0) -> Subject:
    return Subject(height_m=hp, distance_m=d, lateral_m=x)


def _full() -> Rect:
    return Rect(0.0, 0.0, 1920.0, 1080.0)


# ───────────────────────────── 常量与数据类 ─────────────────────────────

def test_shots_and_frames_tables():
    """SHOTS（index.html:946）/ FRAMES（index.html:950）逐项对应原版。"""
    assert SHOTS == {'全身': 1.00, '膝上': 0.72, '腰上': 0.47, '胸上': 0.28, '头肩': 0.20}
    assert FRAMES == {'自由': 0.0, '1:1': 1.0, '4:5': 0.8,
                      '16:9': 16 / 9, '9:16': 9 / 16, '满幅': -1.0}
    assert DEFAULT_FREE_P == (0.5, 0.6)  # 原版模块级 freeP 初值，index.html:2334


def test_sprite_placement_field_order():
    """(u, v, w, h, rot)，rot 为弧度。"""
    c = SpritePlacement(1.0, 2.0, 3.0, 4.0, 0.5)
    assert (c.u, c.v, c.w, c.h, c.rot) == (1.0, 2.0, 3.0, 4.0, 0.5)


# ─────────────────── ground_placement（index.html:2789-2840） ───────────────────

def test_ground_full_body_pitch0_golden():
    """全身、pitch=0、roll=0、x=0、Hp=1.65、d=4、立绘 60×100（宽高比 0.6）。

    yb = (1-1.00)·1.65 = 0（脚踩地）。pitch=0 时 span = F·Hp/d（artifact §3.6 特例）
    = 1275.828595201775·1.65/4 = 526.2792955207323。
    pb = project(0, 0-1.6, 4) = (960, 540 + F·1.6/4) = (960, 1050.33143808071)
    pt = project(0, 1.65-1.6, 4) = (960, 540 + F·0.05/4) = (960, 524.0521425599778)
    charScale=1 → 中心落回 (pb+pt)/2 = (960, 787.1917903203439)（index.html:2831 注释）
    w = h·宽高比 = 526.2792955207323·0.6 = 315.76757731243936；rot = atan2(0, +1) = 0。"""
    c = ground_placement(_char(), _subj(), _cam())
    assert c is not None
    assert c.u == pytest.approx(960.0, abs=1e-9)
    assert c.v == pytest.approx(787.1917903203439, abs=1e-9)
    assert c.w == pytest.approx(315.76757731243936, abs=1e-9)
    assert c.h == pytest.approx(526.2792955207323, abs=1e-9)
    assert c.rot == pytest.approx(0.0, abs=1e-12)


def test_ground_anchor_is_bottom_not_center():
    """锚点是下沿 pb：charScale=2 时 h 翻倍、w 翻倍，中心从 pb 沿 pb→pt 推 h/2
    （u 不动、v 上移到 1050.33143808071 - 526.2792955207323 = 524.0521425599778）。
    反推：sprite 下沿中心 = (c.u - ux·h/2, c.v - uy·h/2) 必须仍等于 pb。"""
    c = ground_placement(_char(), _subj(), _cam(), char_scale=2.0)
    assert c is not None
    assert c.u == pytest.approx(960.0, abs=1e-9)
    assert c.v == pytest.approx(524.0521425599778, abs=1e-9)
    assert c.h == pytest.approx(1052.5585910414645, abs=1e-9)
    assert c.w == pytest.approx(631.5351546248787, abs=1e-9)
    pb = (960.0, 1050.33143808071)
    ux, uy = 0.0, -1.0  # roll=0：pb→pt 竖直向上（画面系 -y）
    assert (c.u - ux * c.h / 2.0, c.v - uy * c.h / 2.0) == pytest.approx(pb, abs=1e-9)


def test_ground_shot_headshoulder_and_knee():
    """景别：yb = (1-SHOTS[shot])·Hp，头顶 pt 不随景别变。
    '头肩' k=0.20 → yb = 0.8·1.65 = 1.32 → span = F·(1.65-1.32)/4 = 105.25585910414645，
      中心 v = pb.v - h/2 = (540 + F·0.28/4) - 52.62792955207323 = 629.3080016641243 - 52.6279295521 = 576.680072112051
    '膝上' k=0.72 → yb = 0.28·1.65 = 0.462 → span = F·(1.65-0.462)/4 = 378.92109277492716"""
    c = ground_placement(_char(), _subj(), _cam(), shot='头肩')
    assert c is not None
    assert c.h == pytest.approx(105.25585910414645, abs=1e-9)
    assert c.v == pytest.approx(576.680072112051, abs=1e-9)
    assert c.w == pytest.approx(105.25585910414645 * 0.6, abs=1e-9)
    c = ground_placement(_char(), _subj(), _cam(), shot='膝上')
    assert c is not None
    assert c.h == pytest.approx(378.92109277492716, abs=1e-9)


def test_ground_unknown_shot_falls_back_to_full():
    """老存档/错 key：SHOTS[shot] || SHOTS['全身']（index.html:2805）→ 与全身逐位一致。"""
    c = ground_placement(_char(), _subj(), _cam(), shot='不存在的景别')
    c_full = ground_placement(_char(), _subj(), _cam())
    assert c == c_full


def test_ground_roll30_golden():
    """roll=+30：地面铅垂线在画面里斜 30°，rot = atan2(ux, -uy) 必须等于 +30°。
    pb = project(0, -1.6, 4) = (960 - F·1.6·sin30/4, 540 + F·1.6·cos30/4)
       = (704.834280959645, 981.9599897277402)
    pt = (960 + F·0.05·sin30/4, 540 - F·0.05·cos30/4) = (967.973928720011, 526.1887503210081)
    span = 526.2792955207323（旋转不改变长度）
    中心 = pb + (ux,uy)·h/2 = (836.404104839828, 754.0743700243742)；w = 315.76757731243936。"""
    c = ground_placement(_char(), _subj(), _cam(roll=30.0))
    assert c is not None
    assert c.u == pytest.approx(836.404104839828, abs=1e-9)
    assert c.v == pytest.approx(754.0743700243742, abs=1e-9)
    assert c.w == pytest.approx(315.76757731243936, abs=1e-9)
    assert c.h == pytest.approx(526.2792955207323, abs=1e-9)
    assert c.rot == pytest.approx(math.radians(30.0), abs=1e-12)


def test_ground_pitch20_offaxis_golden():
    """pitch=+20、x=+0.5：投影把整个人推向画面上缘并右偏，pb→pt 方向不再竖直。
    pb = (1108.099…, 580.125…)，span = 523.1100717749662（≠ F·Hp/d，俯仰生效）
    rot = atan2(ux, -uy) = 0.042726499076203114 rad ≈ 2.448°
    中心 = (1119.3172854489683, 318.8096146695316)，w = 313.8660430649797。"""
    c = ground_placement(_char(), _subj(x=0.5), _cam(pitch=20.0))
    assert c is not None
    assert c.u == pytest.approx(1119.3172854489683, abs=1e-9)
    assert c.v == pytest.approx(318.8096146695316, abs=1e-9)
    assert c.w == pytest.approx(313.8660430649797, abs=1e-9)
    assert c.h == pytest.approx(523.1100717749662, abs=1e-9)
    assert c.rot == pytest.approx(0.042726499076203114, abs=1e-12)


def test_ground_rot_basis_frame_zeroes_rot():
    """rot_basis='frame'：立绘永远画面竖直，rot 直接归零（index.html:2838）；
    但 pb/pt/span 不受影响（u/v/w/h 与 'world' 基准一致）。"""
    c_frame = ground_placement(_char(), _subj(), _cam(roll=30.0), rot_basis='frame')
    c_world = ground_placement(_char(), _subj(), _cam(roll=30.0), rot_basis='world')
    assert c_frame is not None and c_world is not None
    assert c_frame.rot == 0.0
    assert (c_frame.u, c_frame.v, c_frame.w, c_frame.h) == \
        pytest.approx((c_world.u, c_world.v, c_world.w, c_world.h), abs=1e-12)


def test_ground_none_cases():
    """四条早退：无图（原版 !chCanvas，2804）/ pb 或 pt 落到相机后方（2822）/ span<2（2824）。
    - char_rgba=None → None；零尺寸数组同样按无图处理
    - pitch=80、d=0.5：头顶 y=0.05 → Zc = -0.05·sin80 + 0.5·cos80 = 0.0376 <= 0.05 → pt None
    - pitch=0、d=2000：span = F·Hp/d = 1.0525585910414643 < 2 → None"""
    assert ground_placement(None, _subj(), _cam()) is None
    assert ground_placement(np.zeros((0, 0, 4), np.uint8), _subj(), _cam()) is None
    assert ground_placement(_char(), _subj(d=0.5), _cam(pitch=80.0)) is None
    assert ground_placement(_char(), _subj(d=2000.0), _cam()) is None


# ─────────────── free_placement（index.html:2946-2980） ───────────────

def test_free_default_position_and_size():
    """默认 freeP=(0.5, 0.6)（index.html:2334）、sizePct=60 → h = 0.6·1080 = 648，
    u = 0.5·1920 = 960、v = 0.6·1080 = 648；w = h·宽高比 = 648·0.6 = 388.8；rot=0。"""
    c = free_placement(_char(), _subj(), _cam())
    assert c is not None
    assert (c.u, c.v, c.w, c.h, c.rot) == pytest.approx((960.0, 648.0, 388.8, 648.0, 0.0), abs=1e-9)


def test_free_custom_params():
    """size_pct=45、free_p=(0.25, 0.75)、rot_deg=25 → h = 0.45·1080 = 486，
    u = 480、v = 810、w = 486·0.6 = 291.6、rot = radians(25)。"""
    c = free_placement(_char(), _subj(), _cam(), size_pct=45.0, rot_deg=25.0,
                       free_p=(0.25, 0.75))
    assert c is not None
    assert (c.u, c.v, c.w, c.h) == pytest.approx((480.0, 810.0, 291.6, 486.0), abs=1e-9)
    assert c.rot == pytest.approx(math.radians(25.0), abs=1e-12)


def test_free_no_char_returns_none():
    """原版 freePlacement 首行 !chCanvas → null（2971）。"""
    assert free_placement(None, _subj(), _cam()) is None


def test_free_snaps_to_bottom_edge():
    """edge_bar=2（下）、rot=0：bh = h = 648 → v = R.y + R.h - bh/2 = 1080 - 324 = 756；
    bw = w = 388.8 → u = fit(960, 194.4, 1725.6) = 960（已在界内）。"""
    c = free_placement(_char(), _subj(), _cam(), edge_bar=2, rect=_full())
    assert c is not None
    assert (c.u, c.v) == pytest.approx((960.0, 756.0), abs=1e-9)
    assert (c.w, c.h) == pytest.approx((388.8, 648.0), abs=1e-9)


def test_free_no_snap_when_bar_zero():
    """edgeBar=0 时原版走 `S.edgeBar ? snap : c`（2974）——给了 rect 也不吸。"""
    c = free_placement(_char(), _subj(), _cam(), edge_bar=0, rect=_full())
    assert c is not None
    assert (c.u, c.v) == pytest.approx((960.0, 648.0), abs=1e-9)


def test_free_snap_left_rot90_switches_to_top():
    """edge_bar=3（左）+ rot=+90°：rotateBar 按 下→左→上→右 循环把 3（左）换成 1（上）
    （index.html:2937-2943）。旋转后外接盒 bw = 648、bh = 388.8：
    v = R.y + bh/2 = 194.4（吸到上边）；u = fit(960, 324, 1596) = 960。"""
    c = free_placement(_char(), _subj(), _cam(), rot_deg=90.0, edge_bar=3, rect=_full())
    assert c is not None
    assert (c.u, c.v) == pytest.approx((960.0, 194.4), abs=1e-9)
    assert c.rot == pytest.approx(math.radians(90.0), abs=1e-12)


def test_free_snap_rotatebar_switches_edge():
    """rot 变化后 rotateBar 换边（index.html:2937-2943）：bar=2（下）转过 +90° 变 3（左），
    于是立绘吸到**左**边：u = bw/2 = 324、v = fit(648, 194.4, 885.6) = 648。"""
    c = free_placement(_char(), _subj(), _cam(), rot_deg=90.0, edge_bar=2, rect=_full())
    assert c is not None
    assert (c.u, c.v) == pytest.approx((324.0, 648.0), abs=1e-9)


# ───────────── current_placement（index.html:2981）─────────────

def test_current_placement_dispatch():
    """mode 分派：默认 'ground' → ground_placement；'free' → free_placement。"""
    assert current_placement(_char(), _subj(), _cam()) is not None
    cg = current_placement(_char(), _subj(), _cam(), mode='ground')
    cf = current_placement(_char(), _subj(), _cam(), mode='free')
    assert cg is not None and cf is not None
    assert (cg.u, cg.h) == pytest.approx((960.0, 526.2792955207323), abs=1e-9)
    assert (cf.u, cf.v, cf.h) == pytest.approx((960.0, 648.0, 648.0), abs=1e-9)


def test_current_placement_free_rot_follows_camera_roll():
    """原版 charRot = S.roll·D2R（2930）：经 current_placement 分派进自由模式时 rot 跟随
    相机 roll；rot_basis='frame' 时归零（与吸附锁 0 的旧注释无关，按代码行为）。"""
    cam = _cam(roll=30.0)
    c = current_placement(_char(), _subj(), cam, mode='free')
    assert c is not None
    assert c.rot == pytest.approx(math.radians(30.0), abs=1e-12)
    c = current_placement(_char(), _subj(), cam, mode='free', rot_basis='frame')
    assert c is not None
    assert c.rot == 0.0


def test_current_placement_ground_kw_forwarding():
    """kw 透传贴地支路：char_scale / shot 原样生效。"""
    c = current_placement(_char(), _subj(), _cam(), char_scale=2.0, shot='头肩')
    assert c is not None
    assert c.h == pytest.approx(2.0 * 105.25585910414645, abs=1e-9)


# ───────────── auto_fix_x（index.html:2983-3025）─────────────

def test_auto_fix_ground_already_at_half():
    """x=0 → u=960 = 0.5·W，50% 本身是候选线 → 'already'，new_x 不动（仍 0）。"""
    new_x, status = auto_fix_x(_char(), _subj(), _cam())
    assert (new_x, status) == pytest.approx((0.0, 'already'), abs=1e-9)


def test_auto_fix_ground_moved_67():
    """x=2、pitch=0：u = 960 + F·2/4 = 1597.9142976008875，xr=0.832 → 最近 2/3。
    pitch=0 时 u 对 x 线性：grad = (u(2.05)-u(2))/0.05 = F/4 = 318.9571488004458，
    new_x = 2 - (1597.9142976008875-1280)/318.9571488004458 = 1.0032695652173977
    → 一步解精确落位（u(new_x) = 1280.000000000002），status = 'moved:' + round(66.67) = 'moved:67'。"""
    new_x, status = auto_fix_x(_char(), _subj(x=2.0), _cam())
    assert new_x == pytest.approx(1.0032695652173977, abs=1e-9)
    assert status == 'moved:67'
    c = ground_placement(_char(), _subj(x=new_x), _cam())
    assert c is not None
    assert c.u == pytest.approx(1280.0, abs=1e-9)


def test_auto_fix_ground_pitch20_nonlinear_step():
    """pitch=20、x=3（project 非线性 → 数值导数一步解）：u0 = 1915.90371269381，
    grad = 318.6345708979343，new_x = 1.0042852509638573 → u(new_x) ≈ 1279.9999999999952
    （一步 Newton，残差 < 1e-11）。status 'moved:67'。"""
    new_x, status = auto_fix_x(_char(), _subj(x=3.0), _cam(pitch=20.0))
    assert new_x == pytest.approx(1.0042852509638573, abs=1e-9)
    assert status == 'moved:67'
    c = ground_placement(_char(), _subj(x=new_x), _cam(pitch=20.0))
    assert c is not None
    assert c.u == pytest.approx(1280.0, abs=1e-9)


def test_auto_fix_ground_clamps_to_plus_minus_20():
    """autoFix 的 clamp 是 ±20（index.html:3020），与 place 的 ±6 故意不一致。
    x=25、d=100、pitch=0：u = 960 + 12.75828595201775·25 = 1278.957…，xr≈0.666 → 2/3 线；
    grad = F/100 → new_x = 25 + 0.0823… = 25.0823 → 夹到 +20 → 'moved:67'。
    x=-25、d=100 → 对称夹到 -20 → 'moved:33'。"""
    new_x, status = auto_fix_x(_char(), _subj(d=100.0, x=25.0), _cam())
    assert (new_x, status) == pytest.approx((20.0, 'moved:67'), abs=1e-9)
    new_x, status = auto_fix_x(_char(), _subj(d=100.0, x=-25.0), _cam())
    assert (new_x, status) == pytest.approx((-20.0, 'moved:33'), abs=1e-9)


def test_auto_fix_ground_nochar_branches():
    """三条 'nochar'：无图 / 摆位落相机后方（c0 None）/ 数值导数退化。
    - char None → (0.0, 'nochar')
    - pitch=80、d=0.5：头顶 Zc=0.0376 <= 0.05 → c0 None
    - pitch=0、roll=90：u 对 x 的梯度 = F/Zc·cos90° ≈ 0 < 1e-6 → 'nochar'，new_x 不动（2.0）"""
    assert auto_fix_x(None, _subj(), _cam()) == pytest.approx((0.0, 'nochar'), abs=1e-9)
    assert auto_fix_x(_char(), _subj(d=0.5), _cam(pitch=80.0)) == pytest.approx((0.0, 'nochar'), abs=1e-9)
    new_x, status = auto_fix_x(_char(), _subj(x=2.0), _cam(roll=90.0))
    assert new_x == pytest.approx(2.0, abs=1e-9)
    assert status == 'nochar'


def test_auto_fix_free_moved_33():
    """自由模式：原版改归一化 freeP.u = clampN(target/W, 0, 1)（3012）。
    free_p=(0.2, 0.5) → u=384、xr=0.2 → 最近 1/3 → new_x = 640/1920 = 1/3，'moved:33'。"""
    new_x, status = auto_fix_x(_char(), _subj(), _cam(), mode='free', free_p=(0.2, 0.5))
    assert new_x == pytest.approx(1.0 / 3.0, abs=1e-12)
    assert status == 'moved:33'


def test_auto_fix_free_with_edge_snap():
    """自由模式 + 贴边：clamp 到目标线后过 resnap（原版 3013），吸附会覆写 new_x。
    edge_bar=3（左）、rot=0、宽高比 0.6、h=648 → bw=388.8 → new_x = 194.4/1920 = 0.10125。"""
    new_x, status = auto_fix_x(_char(), _subj(), _cam(), mode='free', free_p=(0.2, 0.5),
                               edge_bar=3, rect=_full())
    assert new_x == pytest.approx(0.10125, abs=1e-12)
    assert status == 'moved:33'


def test_auto_fix_free_already_and_nochar():
    """free_p=(1/3, 0.5) → u = 640 恰在 1/3 线上 → 'already'（new_x = 1/3 不动）；
    无图 → (free_p[0], 'nochar')。"""
    new_x, status = auto_fix_x(_char(), _subj(), _cam(), mode='free', free_p=(1.0 / 3.0, 0.5))
    assert new_x == pytest.approx(1.0 / 3.0, abs=1e-12)
    assert status == 'already'
    assert auto_fix_x(None, _subj(), _cam(), mode='free', free_p=(0.2, 0.5)) == \
        pytest.approx((0.2, 'nochar'), abs=1e-12)


def test_auto_fix_zero_canvas():
    """原版 !cv.width → 'nochar'（2997）。"""
    assert auto_fix_x(_char(), _subj(), _cam(w=0, h=0)) == pytest.approx((0.0, 'nochar'), abs=1e-9)


# ───────────── snap_to_edge（index.html:2874-2902） ─────────────

def test_snap_bar1_top_clamps_free_axis():
    """bar=1（上）：v = R.y + bh/2 = 324（bh=h=648）；u 沿自由轴钳到 [194.4, 1725.6]。
    界内不动、界外夹住。返回 w = h·aspect = 388.8。"""
    base = dict(bar=1, rect=_full(), aspect=0.6, rot=0.0)
    inside = snap_to_edge(SpritePlacement(960.0, 500.0, 388.8, 648.0, 0.0), **base)
    assert (inside.u, inside.v) == pytest.approx((960.0, 324.0), abs=1e-9)
    right = snap_to_edge(SpritePlacement(1800.0, 500.0, 388.8, 648.0, 0.0), **base)
    assert right.u == pytest.approx(1725.6, abs=1e-9)
    left = snap_to_edge(SpritePlacement(10.0, 500.0, 388.8, 648.0, 0.0), **base)
    assert left.u == pytest.approx(194.4, abs=1e-9)
    assert (inside.w, inside.h, inside.rot) == pytest.approx((388.8, 648.0, 0.0), abs=1e-9)


def test_snap_bar2_bottom_bar4_right_bar3_left():
    """bar=2（下）：v = R.y+R.h-bh/2 = 756。bar=4（右）：u = R.x+R.w-bw/2 = 1725.6。
    bar=3（左）：u = R.x+bw/2 = 194.4，v 钳到 [R.y+bh/2, R.y+R.h-bh/2] = [324, 756]
    （rot=0 → bh = h = 648）：2000 → 756、100 → 324。"""
    bottom = snap_to_edge(SpritePlacement(960.0, 100.0, 388.8, 648.0, 0.0), 2, _full(), 0.6, 0.0)
    assert (bottom.u, bottom.v) == pytest.approx((960.0, 756.0), abs=1e-9)
    right = snap_to_edge(SpritePlacement(300.0, 500.0, 388.8, 648.0, 0.0), 4, _full(), 0.6, 0.0)
    assert (right.u, right.v) == pytest.approx((1725.6, 500.0), abs=1e-9)
    left_low = snap_to_edge(SpritePlacement(300.0, 2000.0, 388.8, 648.0, 0.0), 3, _full(), 0.6, 0.0)
    assert (left_low.u, left_low.v) == pytest.approx((194.4, 756.0), abs=1e-9)
    left_high = snap_to_edge(SpritePlacement(300.0, 100.0, 388.8, 648.0, 0.0), 3, _full(), 0.6, 0.0)
    assert left_high.v == pytest.approx(324.0, abs=1e-9)


def test_snap_degenerate_h_and_unknown_bar_identity():
    """h<=0 → 原样返回（2880）；bar 不在 1..4 → default 分支原样返回（2901）。"""
    c = SpritePlacement(960.0, 648.0, 388.8, 0.0, 0.0)
    assert snap_to_edge(c, 2, _full(), 0.6, 0.0) == c
    c2 = SpritePlacement(960.0, 648.0, 388.8, 648.0, 0.3)
    assert snap_to_edge(c2, 0, _full(), 0.6, 0.0) is c2
    assert snap_to_edge(c2, 5, _full(), 0.6, 0.0) is c2


def test_snap_big_sprite_not_pinned_to_center():
    """图比框大：fit 两端**不排序**（2885-2897 注释）→ 范围自然反过来、立绘可自由平移。
    rect 宽 500（x=710）、宽高比 2.0 → bw = 1296：a = 710+648 = 1358、b = 710+500-648 = 562，
    lo=562、hi=1358 → c.u=900 保持 900（studio 那版会钉死在中心 960）。"""
    rect = Rect(710.0, 0.0, 500.0, 1080.0)
    got = snap_to_edge(SpritePlacement(900.0, 500.0, 1296.0, 648.0, 0.0), 2, rect, 2.0, 0.0)
    assert got.u == pytest.approx(900.0, abs=1e-9)
    assert got.v == pytest.approx(756.0, abs=1e-9)


def test_snap_rotatebar_switches_bar_with_rotation():
    """bar=2（下）+ rot=+90° → rotateBar 换成 3（左）：
    bw = w·|cos90|+h·|sin90| = 648、bh = 388.8 → u = 324、v = fit(648, 194.4, 885.6) = 648。"""
    got = snap_to_edge(SpritePlacement(960.0, 648.0, 388.8, 648.0, 0.0), 2, _full(), 0.6,
                       math.radians(90.0))
    assert (got.u, got.v) == pytest.approx((324.0, 648.0), abs=1e-9)
    assert got.rot == pytest.approx(0.0, abs=1e-12)
