"""golden.json 重算门禁：fixture 与 arcam 实现逐向量比对（SITE-SPEC §0）。

docs/tests/golden.json 是 Pages 纯 JS 移植（arcam-core.js）的数值基准，
浏览器差分脚本按它逐组核对 JS 输出。本测试把方向反过来钉 Python 侧：
**fixture 必须能用当前实现逐向量重算出来**。任何改动若让 focal/project/
place/placement/scene 的数值漂移，这里立刻红——避免 fixture 悄悄过期后，
浏览器差分反而在捍卫一个错误的基准。

分组口径与生成端逐字一致（见 git show 5d0e8be 的说明）：
  focal     —— focal_px
  project   —— project(x, y, d)
  place     —— place(u, v)（反投影）
  placement —— current_placement（ground/free 两分支，free 含
               rot_basis=frame / edge_bar 吸附回归钉）
  scene     —— buildScene 的字段同构 + alpha 覆盖统计
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from arcam.frames import frame_rect
from arcam.geometry import Camera, focal_px, place, project
from arcam.placement import Subject, current_placement
from arcam.scene import build_scene

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "docs" / "tests" / "golden.json"

TOL = 1e-9   # JS/Python 双精度尾差量级（实测最大 5.551e-17，留 8 个数量级余量）


def _load() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _cam_of(spec: list) -> Camera:
    w, h, hm, feq, pitch, roll = spec
    return Camera(int(w), int(h), float(hm), float(feq), float(pitch), float(roll))


def _subj_of(spec: list) -> Subject:
    hp, d, x = spec
    return Subject(float(hp), float(d), float(x))


def _fake_char(spec: list) -> np.ndarray:
    w, h = int(spec[0]), int(spec[1])
    return np.zeros((h, w, 4), np.uint8)


def _close(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=TOL, abs_tol=TOL)
    return a == b


def test_focal_vectors_recompute():
    for i, v in enumerate(_load()["focal"]):
        assert _close(focal_px(_cam_of(v["cam"])), v["F"]), f"focal[{i}]"


def test_project_vectors_recompute():
    for i, v in enumerate(_load()["project"]):
        cam = _cam_of(v["cam"])
        expect = project(v["p"][0], v["p"][1], v["p"][2], cam)
        got = None if expect is None else list(expect)
        assert _close(got, v["uv"]), f"project[{i}]"


def test_place_vectors_recompute():
    for i, v in enumerate(_load()["place"]):
        cam = _cam_of(v["cam"])
        expect = place(v["uv"][0], v["uv"][1], cam)
        got = None if expect is None else list(expect)
        assert _close(got, v["xd"]), f"place[{i}]"


def test_placement_vectors_recompute():
    for i, v in enumerate(_load()["placement"]):
        cam = _cam_of(v["cam"])
        subj = _subj_of(v["subj"])
        char = _fake_char(v["char"])
        kw: dict = {"mode": v["kind"]}
        if v["kind"] == "ground":
            kw.update(char_scale=v["char_scale"], shot=v["shot"],
                      rot_basis=v.get("rot_basis", "world"))
        else:
            kw.update(size_pct=v["size_pct"],
                      rot_basis=v.get("rot_basis", "world"),
                      rot_deg=v.get("rot_deg"),
                      edge_bar=v.get("edge_bar", 0))
            if v.get("rect") is not None:
                kw["rect"] = type(frame_rect("满幅", 1, 1))(*v["rect"])
        p = current_placement(char, subj, cam, **kw)
        got = None if p is None else [p.u, p.v, p.w, p.h, p.rot]
        assert _close(got, v["out"]), f"placement[{i}]（{v['kind']}）"
        if "out_nosnap" in v:
            kw2 = dict(kw, rect=None)
            p0 = current_placement(char, subj, cam, **kw2)
            got0 = None if p0 is None else [p0.u, p0.v, p0.w, p0.h, p0.rot]
            assert _close(got0, v["out_nosnap"]), f"placement[{i}] nosnap 对照"


def test_scene_vectors_recompute():
    """scene 组：buildScene dict 全字段重算比对（alpha 统计属 JS 画布侧合成，
    由浏览器差分脚本核对，这里只钉 Python 的 scene dict）。"""
    from arcam.numeric import Rect
    for i, v in enumerate(_load()["scene"]):
        p = v["params"]
        cam = Camera(int(p["W"]), int(p["H"]), 1.6, float(p["feq"]),
                     float(p["pitch"]), float(p["roll"]))
        subj = Subject(1.65, float(p["d"]), float(p["x"]))
        char = _fake_char([120, 200])
        pl = current_placement(char, subj, cam)
        got = build_scene(Rect(0, 0, p["W"], p["H"]), pl, subj, cam,
                          frame_key="满幅", char_rgba01=char,
                          char_name="char.png", mode="ground")
        _assert_scene_equal(got, v["scene"], f"scene[{i}]")


def _assert_scene_equal(got: dict, want: dict, where: str) -> None:
    """逐字段深比较：数字走 TOL，键集合必须一致（字段序由 buildScene 保证）。"""
    if isinstance(want, dict):
        assert isinstance(got, dict) and set(got.keys()) == set(want.keys()), \
            f"{where} 键位不一致：{set(got) ^ set(want)}"
        for k in want:
            _assert_scene_equal(got[k], want[k], f"{where}.{k}")
    elif isinstance(want, list):
        assert isinstance(got, list) and len(got) == len(want), f"{where} 长度不一致"
        for j, (a, b) in enumerate(zip(got, want)):
            _assert_scene_equal(a, b, f"{where}[{j}]")
    elif isinstance(want, (int, float)) and not isinstance(want, bool):
        assert math.isclose(float(got), float(want), rel_tol=TOL, abs_tol=TOL), \
            f"{where}：{got!r} != {want!r}"
    else:
        assert got == want, f"{where}：{got!r} != {want!r}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
