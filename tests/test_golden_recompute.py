"""golden.json 重算门禁：fixture 与 arcam 实现逐向量比对（SITE-SPEC §0）。

docs/tests/golden.json 是 Pages 纯 JS 移植（arcam-core.js / arcam-lighting.js）
的数值基准，浏览器差分脚本按它逐组核对 JS 输出。本测试把方向反过来钉 Python 侧：
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

光照烘焙组（arcam-lighting.js 移植门禁，W4 2026-10-11 追加）：
  fixtures      —— 合成 RGBA(uint8) 图（base64）：photoA/B/C 是 measureRegion
                   的采样源，charA..G 是 bakeLit / charStats 的输入
  measureRegion —— measure_region（整帧/正常/越界零填充/浮点 region、
                   levels 16·32、退化 w=0）
  bakeLit       —— bake_lit（逐**字节**全等，0 容差；sample 一律由同 fixture
                   的 measure_region 现采，不是手编常数）
  charStats     —— _char_stats（含全透明立绘）
  litScaleBucket—— lit_scale_bucket
  grainTile     —— grain_tile（前 64 项 + 抽样下标）
fixture 由固定种子合成（photo 种子 20261011，char 无噪声纯渐变），种子只作
溯源；基准是 golden.json 里冻结的字节本身。
"""

from __future__ import annotations

import base64
import json
import math
from pathlib import Path

import numpy as np
import pytest

from arcam.geometry import Camera, focal_px, place, project
from arcam.lighting import _char_stats, bake_lit, grain_tile, lit_scale_bucket, measure_region
from arcam.numeric import Rect
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


def _fixture(name: str) -> np.ndarray:
    """光照组 fixture：base64 RGBA → (H, W, 4) uint8（JS 侧字节口径同源）。"""
    f = _load()["fixtures"][name]
    arr = np.frombuffer(base64.b64decode(f["rgba"]), dtype=np.uint8)
    assert arr.size == int(f["w"]) * int(f["h"]) * 4, f"fixture {name} 尺寸不符"
    return arr.reshape(int(f["h"]), int(f["w"]), 4)


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
                kw["rect"] = Rect(*v["rect"])
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


# ── 光照烘焙组（arcam-lighting.js 移植门禁）────────────────────────────
def test_measure_region_vectors_recompute():
    """measureRegion 组：region 钳制 + 盒式降采样的均值/直方图重算。"""
    for i, v in enumerate(_load()["measureRegion"]):
        rgba = _fixture(v["img"])
        region = None if v["region"] is None else Rect(*v["region"])
        s = measure_region(rgba[..., :3], region, levels=v["levels"])
        assert len(s.levels) == v["levels"], f"measureRegion[{i}] levels 长度"
        assert _close(list(s.mean), v["out"]["mean"]), f"measureRegion[{i}] mean"
        assert _close(list(s.levels), v["out"]["levels"]), f"measureRegion[{i}] levels"


def test_bake_lit_vectors_recompute():
    """bakeLit 组：**逐字节全等（0 容差）**。

    sample 一律由同 fixture 的 measure_region 从向量里记的 sample_src 现采
    （真实链路），采到的 sample 还必须与冻结值一致——两侧互相钉死。
    """
    for i, v in enumerate(_load()["bakeLit"]):
        char = _fixture(v["char"])
        src = v["sample_src"]
        if src is None:
            assert v["sample"] is None, f"bakeLit[{i}] sample_src 为 null 但 sample 非空"
            sample = None
        else:
            photo = _fixture(src["img"])[..., :3]
            region = None if src["region"] is None else Rect(*src["region"])
            sample = measure_region(photo, region, levels=src["levels"])
            assert _close(list(sample.mean), v["sample"]["mean"]), f"bakeLit[{i}] sample.mean"
            assert _close(list(sample.levels), v["sample"]["levels"]), f"bakeLit[{i}] sample.levels"
        before = char.copy()
        out = bake_lit(char, sample, (float(v["canvasW"]), 0.0),
                       grain=v["grain"], grain_scale=float(v["grainScale"]))
        assert np.array_equal(char, before), f"bakeLit[{i}] 改写了入参"
        assert out.dtype == np.uint8, f"bakeLit[{i}] 出参族不是 uint8"
        want = np.frombuffer(base64.b64decode(v["out"]), dtype=np.uint8).reshape(char.shape)
        if not np.array_equal(out, want):
            d = np.argwhere(out != want)[:4]
            detail = "; ".join(f"idx{tuple(j)}: {want[tuple(j)]} != {out[tuple(j)]}" for j in d)
            raise AssertionError(f"bakeLit[{i}]（{v['char']} canvasW={v['canvasW']} "
                                 f"grain={v['grain']} scale={v['grainScale']}）字节不全等：{detail}")


def test_char_stats_vectors_recompute():
    """charStats 组：立绘自身统计（alpha<8 跳过、全透明退化为 0.5）。"""
    for i, v in enumerate(_load()["charStats"]):
        got = list(_char_stats(_fixture(v["img"])))
        want = [v["out"]["lum"], v["out"]["sat"], v["out"]["r"], v["out"]["b"]]
        assert _close(got, want), f"charStats[{i}]"


def test_lit_scale_bucket_vectors_recompute():
    for i, v in enumerate(_load()["litScaleBucket"]):
        assert _close(lit_scale_bucket(v["s"]), v["out"]), f"litScaleBucket[{i}] s={v['s']}"


def test_grain_tile_vectors_recompute():
    """grainTile 组：xorshift32 固定种子砖（前 64 项 + 抽样下标，扁平口径）。"""
    tile = grain_tile().reshape(-1)
    assert tile.size == 65536
    for i, v in enumerate(_load()["grainTile"]):
        got = [float(tile[j]) for j in v["idx"]]
        assert _close(got, v["out"]), f"grainTile[{i}]"


def test_lighting_golden_coverage_floors():
    """覆盖下限钉死：光照组的场景清单不许在后续编辑里被悄悄削掉。"""
    g = _load()
    mr, bl = g["measureRegion"], g["bakeLit"]
    fx = g["fixtures"]
    assert len(mr) >= 6, "measureRegion 向量不足 6"
    assert len(bl) >= 8, "bakeLit 向量不足 8"
    assert len(g["charStats"]) >= 2, "charStats 向量不足 2"
    assert len(g["litScaleBucket"]) >= 3, "litScaleBucket 向量不足 3"
    assert len(g["grainTile"]) == 2, "grainTile 应为 2 个向量（前 64 项 + 抽样）"
    # measureRegion：整帧 / 整数 region / 越界零填充 / 浮点 region / levels 16·32 / 退化
    assert any(v["region"] is None for v in mr), "缺 region=null 整帧向量"
    assert any(v["region"] is not None and all(float(x).is_integer() for x in v["region"])
               for v in mr), "缺正常整数 region 向量"
    assert any(v["region"] is not None and float(v["region"][2]) > fx[v["img"]]["w"]
               for v in mr), "缺 sw>W 越界零填充向量"
    assert any(v["region"] is not None and not all(float(x).is_integer() for x in v["region"])
               for v in mr), "缺浮点 region 向量"
    assert {v["levels"] for v in mr} >= {16, 32}, "levels 未同时覆盖 16 与 32"
    assert any(v["region"] is not None and float(v["region"][2]) == 0.0
               for v in mr), "缺退化 region（w=0）向量"
    # bakeLit：直通 / 关颗粒 / grainScale 0·1·3 / litScaleBucket 各档 / 全透明 / alpha=8 边界
    assert any(v["sample"] is None for v in bl), "缺 sample=null 直通向量"
    assert any(not v["grain"] for v in bl), "缺 grain=false 向量"
    assert {v["grainScale"] for v in bl} >= {0.0, 1.0, 3.0}, "grainScale 未覆盖 0/1/3"
    ratios = {round(v["canvasW"] / fx[v["char"]]["w"], 6) for v in bl}
    for r in (1.0, 0.5, round(1 / 3, 6), 0.125):
        assert r in ratios, f"canvasW 未覆盖比例 {r}"
    assert any(np.frombuffer(base64.b64decode(fx[v["char"]]["rgba"]), np.uint8)[3::4].max() == 0
               for v in bl), "缺全透明立绘向量"
    assert any(np.frombuffer(base64.b64decode(fx[v["char"]]["rgba"]), np.uint8)[3::4].min() == 8
               and np.frombuffer(base64.b64decode(fx[v["char"]]["rgba"]), np.uint8)[3::4].max() == 8
               for v in bl), "缺 alpha=8 边界立绘向量"
    assert any(v["sample_src"] is not None and v["sample_src"]["region"] is not None
               for v in bl), "缺局部 region 采样的真实链路向量"
    # charStats 含全透明；litScaleBucket 含 0.03/0.3/0.6/2；grainTile 首向量是前 64 项
    assert any(np.frombuffer(base64.b64decode(fx[v["img"]]["rgba"]), np.uint8)[3::4].max() == 0
               for v in g["charStats"]), "charStats 缺全透明向量"
    assert {v["s"] for v in g["litScaleBucket"]} >= {0.03, 0.3, 0.6, 2.0}, \
        "litScaleBucket 缺 0.03/0.3/0.6/2"
    assert g["grainTile"][0]["idx"] == list(range(64)), "grainTile 首向量应为前 64 项"
    assert len(g["grainTile"][1]["idx"]) >= 8, "grainTile 抽样下标过少"


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
