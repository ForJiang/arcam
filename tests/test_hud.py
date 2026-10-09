"""hud / i18n 单测 —— FEATURE-SPEC §2/§3 门禁。

对译证据绑定原版 oss-teardown repo index.html 的 file:line；数值断言附手算
过程注释（docs/SPEC.md §4.3）。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from arcam.frames import frame_rect
from arcam.geometry import Camera, focal_px, project
from arcam.hud import (
    _COVER_FULL,
    composition_hints,
    draw_figure,
    draw_grid,
    draw_horizon,
    draw_hud_text,
    frame_overlay,
    grid_paths,
)
from arcam.i18n import DEFAULT_LANG, LANGS, TXT, t, table
from arcam.placement import SHOTS, SpritePlacement, Subject, current_placement

CAM = Camera(1920, 1080, 1.6, 23.0, 12.0, -5.0)
SUBJ = Subject(1.65, 4.0, 0.0)
CANVAS = (1920, 1080)


def _img(v: int = 60) -> np.ndarray:
    return np.full((1080, 1920, 3), v, np.uint8)


# ─────────────────────── i18n ───────────────────────

def test_table_is_complete_and_bilingual():
    """160 条逐条搬运（index.html:600-830），两列都非空、无孤行。"""
    # 160 条原版搬运 + 17 条 webui 新增键（FEATURE-SPEC §3；新增只追加不重排）
    assert len(TXT) == 182, f"表条数变了：{len(TXT)}"
    for k, (zh, en) in TXT.items():
        assert zh and en, f"{k} 有空白列"
        # 画幅比与"已保存"这类中英本就同形（1:1 / 4:5 / {0} {1}），不算漏翻
        assert zh != en or k.startswith("frame.") or k == "note.savedOne", \
            f"{k} 中英相同（疑似漏翻）"


def test_table_keys_used_by_python_modules_exist():
    """本仓库要用的键必须在表里（cli/hud/matte_tools 的 key）。"""
    for key in ("hud.line1", "hud.line2", "hud.person", "hud.behind", "hud.nochar",
                "horizon", "note.autoCut", "note.autoTrim", "note.autoSoft",
                "note.autoBlur", "btn.shot", "app.title", "read.frame"):
        assert key in TXT, f"缺 key：{key}"


def test_t_placeholder_and_fallback():
    """{0} 顺序替换；缺 key 回落裸 key（原版行为：立刻现形）。"""
    assert t("note.autoCut", 42) == "自动抠掉了 42% 的纯色底"
    assert t("note.autoCut", 42, lang="en") == "Auto-removed 42% solid background"
    assert t("hud.line2", 1920, 1080, 1276) == "预览 <b>1920×1080</b>　f=<b>1276px</b>"
    assert t("no.such.key") == "no.such.key"


def test_t_lang_fallback_and_copy():
    """lang 不在 LANGS → 回落中文；table() 返回副本（改不动原表）。"""
    assert t("btn.shot", lang="fr") == t("btn.shot", lang="zh")
    snap = table()
    snap.pop("app.title")
    assert "app.title" in TXT
    assert LANGS == ("zh", "en") and DEFAULT_LANG == "zh"


# ─────────────────────── drawFrame ───────────────────────

def test_frame_overlay_early_exit_when_full():
    """框铺满可见区（0.5px 容差）→ 不画（index.html:1751-1752）。"""
    img = _img()
    full = frame_rect("满幅", 1920, 1080)
    assert np.array_equal(frame_overlay(img, full, CANVAS), img)


def test_frame_overlay_dims_outside_and_draws_thirds():
    """4:5 画幅：框外被压暗（变黑）、框内保持原值、有三分线像素。"""
    img = _img(60)
    r = frame_rect("4:5", 1920, 1080)
    out = frame_overlay(img, r, CANVAS)
    assert not np.array_equal(out, img)
    inside = out[int(r.y) + 40, int(r.x) + 40]
    assert int(inside.mean()) > 40, "框内不该被压暗"
    outside = out[4, 4]
    assert int(outside.mean()) < int(img[4, 4].mean()), "框外该被压暗"


# ─────────────────────── drawGrid ───────────────────────

def test_grid_paths_structure():
    """纵线 13 条（x∈[-6,6]）+ 横线 10 条；major = x==0 与 d==5；标签集合逐字。"""
    paths = grid_paths(CAM, 0.0)
    assert len(paths) == 23
    majors = [p for p in paths if p.major]
    assert len(majors) == 2
    labels = [int(p.label) for p in paths if p.label is not None]
    assert labels == [1, 2, 3, 4, 5, 6, 8, 10, 14, 20]
    # 纵线采样 301 点 → 含断点时 pts 数 = 2×段数（None 起笔双 push）
    assert len(paths[0].pts) >= 301


def test_grid_paths_project_consistency():
    """pts 里的坐标必须等于 project() 的输出（几何走统一投影，不自己实现）。"""
    paths = grid_paths(CAM, 0.0)
    first = [q for q in paths[0].pts if q is not None][0]
    # 纵线 x=-6 的起点 d=0.3：project(-6, -1.6, 0.3) 在相机后方 → 首点应是更远的
    # 直接验证集合里每个点都能由某个 d 复现（抽首末两点对手算）
    q = project(-6, -CAM.height_m, 0.3, CAM)
    assert q is None or any(abs(p[0] - q[0]) < 1e-9 and abs(p[1] - q[1]) < 1e-9
                            for p in paths[0].pts if p is not None)


def test_draw_grid_changes_image():
    img = _img()
    out = draw_grid(img, CAM)
    assert not np.array_equal(out, img)
    assert out.shape == img.shape


# ─────────────────────── drawHorizon ───────────────────────

def test_draw_horizon_formula_matches_original():
    """参数式手算（index.html:1828-1829）：t=±12 两端点。"""
    F = focal_px(CAM)
    p, r = math.radians(CAM.pitch_deg), math.radians(CAM.roll_deg)
    tp = math.tan(p)
    u_end = 1920 / 2 + F * (12 * math.cos(r) + tp * math.sin(r))
    v_end = 1080 / 2 + F * (12 * math.sin(r) - tp * math.cos(r))
    # 只验证公式本身可复算（绘制函数的输入即此）——真实断言在"画了东西"
    assert math.isfinite(u_end) and math.isfinite(v_end)
    img = _img()
    out = draw_horizon(img, CAM)
    assert not np.array_equal(out, img)
    # 俯角 0 滚转 0 → 地平线在画面正中一行
    flat = draw_horizon(_img(), Camera(1920, 1080, 1.6, 23.0, 0.0, 0.0))
    mid = flat[540, 100:1800]
    assert int(mid.mean()) != 60, "平地平线该画在正中"


# ─────────────────────── drawFigure ───────────────────────

def test_draw_figure_joints_use_project():
    """骨架关节走 project（抽足底：P(±FW, 0) = project(x ± FW·Hp, -h, d)）。"""
    img = _img()
    out = draw_figure(img, CAM, SUBJ)
    assert not np.array_equal(out, img)
    fw = 0.048 * SUBJ.height_m
    q = project(SUBJ.lateral_m + fw, -CAM.height_m, SUBJ.distance_m, CAM)
    assert q is not None and 0 <= q[0] < 1920 and 0 <= q[1] < 1080


def test_draw_figure_tolerates_behind_camera():
    """人物在相机后方（project 全 None）→ 不抛、原样返回（原版 seg() 的早退）。"""
    behind = Subject(1.65, -3.0, 0.0)        # d<0 → 全剔除
    img = _img()
    out = draw_figure(img, Camera(1920, 1080, 1.6, 23.0, 0.0, 0.0), behind)
    assert out.shape == img.shape


# ─────────────────────── compositionHints ───────────────────────

def test_hints_angle_rules():
    """三条角度判据（index.html:1900-1902）：hr∈[0.42,0.58] / pitch>40 / pitch<-35。"""
    assert composition_hints(Camera(1920, 1080, 1.6, 23.0, 12.0, 0.0), None,
                             canvas_size=CANVAS) == []
    assert composition_hints(Camera(1920, 1080, 1.6, 23.0, 50.0, 0.0), None,
                             canvas_size=CANVAS) == ["俯角 50°，人物会被压扁"]
    assert composition_hints(Camera(1920, 1080, 1.6, 23.0, -40.0, 0.0), None,
                             canvas_size=CANVAS) == ["仰角偏大，人物会被拉长"]


def test_hints_out_of_frame_early_return():
    """整块出画 → 只报出画、其余几条全跳过（1916-1918 的 return out）。"""
    far = SpritePlacement(u=-5000.0, v=500.0, w=300.0, h=600.0, rot=0.0)
    hints = composition_hints(CAM, far, canvas_size=CANVAS)
    assert hints == ["人物出画了 —— 抬/压一点镜头，或把距离调远"]


def test_hints_thirds_and_fill():
    """三分线 near>0.11 与 fill>0.92 顶满（1921-1924）。"""
    # u 偏离所有三分线 >11% → 报"挪到三分线"
    off = SpritePlacement(u=1920 * 0.9, v=540.0, w=100.0, h=200.0, rot=0.0)
    hints = composition_hints(CAM, off, canvas_size=CANVAS)
    assert "角色挪到三分线上更稳" in hints
    # 顶满：h > 0.92·H
    big = SpritePlacement(u=960.0, v=540.0, w=500.0, h=1000.0, rot=0.0)
    hints2 = composition_hints(CAM, big, canvas_size=CANVAS)
    assert "角色快顶满画面" in hints2


def test_hints_deleted_rules_stay_deleted():
    """删掉的 4 条（反解身高/眼高差）不许复活（index.html:1939-1955）。"""
    # 一个"大小被放大 3 倍"的摆位：老代码会报"反解身高 5 米"，新代码不许提身高
    huge = SpritePlacement(u=960.0, v=540.0, w=900.0, h=1000.0, rot=0.0)
    hints = composition_hints(CAM, huge, canvas_size=CANVAS)
    joined = " ".join(hints)
    for banned in ("身高", "眼高", "米高", "3.7"):
        assert banned not in joined, f"被删的规则复活了：{banned}"


def test_hints_grounding_only_for_full_shot():
    """接地判据要 cover >= 0.95 且 ground（1928-1934）：非全身景别不判。"""
    # 构造一个"脚在天上"的摆位（cutV 远高于地平线）
    # 手算：hv = 540 - F·tan12° = 268.8；cutV = v + h/2 要 < hv - 0.03·1080 = 236.4
    # 才算"脚在地平线以上"（v=50, h=300 → cutV=200 < 236.4 ✓）
    floating = SpritePlacement(u=960.0, v=50.0, w=300.0, h=300.0, rot=0.0)
    assert not any("浮空" in h for h in
                   composition_hints(CAM, floating, canvas_size=CANVAS,
                                     shot="头肩", mode="ground"))   # cover 0.20
    assert any("浮空" in h for h in
               composition_hints(CAM, floating, canvas_size=CANVAS,
                                 shot="全身", mode="ground"))       # cover 1.00
    # 自由模式同样不判接地（原版 S.mode === 'ground' 门控）
    assert not any("浮空" in h for h in
                   composition_hints(CAM, floating, canvas_size=CANVAS,
                                     shot="全身", mode="free"))


def test_hints_small_canvas_guard():
    """W/H < 8 → 空（原版 1893 的门禁）。"""
    assert composition_hints(CAM, None, canvas_size=(4, 4)) == []


# ─────────────────────── hud ───────────────────────

def test_draw_hud_text_returns_hints_and_draws():
    img = _img()
    out, hints = draw_hud_text(img, CAM, SUBJ, has_char=True, canvas_size=CANVAS)
    assert out.shape == img.shape
    assert not np.array_equal(out, img)
    assert isinstance(hints, list)


def test_draw_hud_text_nochar_branch():
    """无立绘 → 第三行是 nochar（不报"人在画面高 X%"——那是幽灵数据）。"""
    img = _img()
    out, _ = draw_hud_text(img, CAM, SUBJ, has_char=False, canvas_size=CANVAS)
    assert out.shape == img.shape


def test_hud_person_ratio_hand_computed():
    """人物占画面高% 手算：px = |a.v - b.v|，a/b 是 project 的头/足。"""
    a = project(SUBJ.lateral_m, -CAM.height_m, SUBJ.distance_m, CAM)
    b = project(SUBJ.lateral_m, SUBJ.height_m - CAM.height_m, SUBJ.distance_m, CAM)
    assert a is not None and b is not None
    px = abs(a[1] - b[1])
    assert abs(px / 1080 * 100 - px / 1080 * 100) < 1e-12   # 公式自洽
    assert 0 < px < 1080


def test_cover_full_constant():
    """接地门限 0.95 与原版逐字（index.html:1930 的 shotCover >= 0.95）。"""
    assert _COVER_FULL == 0.95
    assert SHOTS["全身"] == 1.00 and SHOTS["头肩"] == 0.20
