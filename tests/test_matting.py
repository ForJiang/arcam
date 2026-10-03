"""matting 模块测试（cutout.js AI 抠图线的 Python 等价实现）。

单测（预处理/后处理/连通域/模型解析）不依赖模型也不依赖 onnxruntime；
fake-session 端到端用 monkeypatch 掉 _load_session；真模型 E2E 允许 skipif
（onnxruntime 未装或模型文件不存在时跳过）。

真模型 E2E 若要跑起来：.venv/bin/python -m pip install onnxruntime，并把模型
软链到 ~/.cache/arcam/isnet-anime-q8.onnx（源仓库只读，不复制）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import types
from dataclasses import FrozenInstanceError
from pathlib import Path

import numpy as np
import pytest

import arcam.matting as M

# 以下公开符号经属性取出（模块属性直取，保持与旧版测试引用一致）。
MattingResult = M.MattingResult
anime_cut = M.anime_cut
keep_largest_blob = M.keep_largest_blob
seg_postprocess = M.seg_postprocess
seg_preprocess = M.seg_preprocess

try:
    import onnxruntime as _ort
    _HAS_ORT = True
    _ORT_NOTE = f"onnxruntime {_ort.__version__} 已装"
except ImportError:
    _HAS_ORT = False
    _ORT_NOTE = "onnxruntime 未装"


def _real_model() -> Path | None:
    try:
        return M._resolve_model_path(None)
    except FileNotFoundError:
        return None


LO, HI = M.SEG_LO, M.SEG_HI


def _norm(v8: int, ch: int) -> float:
    """uint8 通道值 → 预处理归一化后的张量值（cutout.js:658-662）。"""
    return (v8 / 255.0 - M.SEG_MEAN[ch]) / M.SEG_STD[ch]


def _u8(x: np.ndarray) -> np.ndarray:
    return (np.clip(x, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def _character_like_image() -> np.ndarray:
    """确定性合成图：噪声纹理背景 + 噪声纹理+正弦调制椭圆主体（float64 [0,1]）。"""
    rng = np.random.default_rng(7)
    h, w = 160, 120
    yy, xx = np.mgrid[0:h, 0:w]
    blob = (xx - 60) ** 2 / 45 ** 2 + (yy - 80) ** 2 / 60 ** 2 <= 1
    bg = 0.95 + rng.normal(0.0, 0.02, (h, w, 3))
    fg = 0.3 + 0.5 * rng.random((h, w, 3)) + np.sin(xx[:, :, None] / 3.0) * 0.05
    return np.clip(np.where(blob[:, :, None], fg, bg), 0.0, 1.0)


def _gradient(h: int, w: int) -> np.ndarray:
    """确定性渐变图（float64 [0,1]，(h,w,3)）——RGB 三通道取值互不相同，
    便于验证 NCHW 通道序与 alpha 回贴时 RGB 不被改动。"""
    yy, xx = np.mgrid[0:h, 0:w]
    r = (0.10 + 0.60 * yy / max(h - 1, 1))[:, :, None]
    g = (0.90 - 0.50 * xx / max(w - 1, 1))[:, :, None]
    b = np.full((h, w, 1), 0.35)
    return np.clip(np.concatenate([r, g, b], axis=2), 0.0, 1.0)


def _fake_mask(region: tuple[int, int, int, int], fill: float = 1.0) -> np.ndarray:
    """模型输出尺寸 (1,1,1024,1024) 的确定性假蒙版：图片区域填 fill、补边区 0。"""
    ox, oy, nw, nh = region
    m = np.zeros((1, 1, M.SEG_SZ, M.SEG_SZ), dtype=np.float32)
    m[0, 0, oy:oy + nh, ox:ox + nw] = fill
    return m


class _FakeSession:
    """最小 ORT 会话替身：喂名 + 确定性输出。"""

    def __init__(self, mask: np.ndarray):
        self._mask = mask
        self.feeds: dict | None = None
        self.intra_op: int | None = None

    def get_inputs(self):
        return [types.SimpleNamespace(name="img")]

    def run(self, output_names, feeds):
        self.feeds = dict(feeds)
        return [self._mask]


def _patch_session(monkeypatch, mask: np.ndarray) -> _FakeSession:
    sess = _FakeSession(mask)
    monkeypatch.setattr(
        M, "_load_session",
        lambda path, *, intra_op, simd: (setattr(sess, "intra_op", intra_op), sess)[1],
    )
    return sess


# ────────────────────────── seg_preprocess ──────────────────────────

def test_preprocess_shapes_and_crop():
    """竖/横/方图：张量形状 [1,3,1024,1024] float32，crop 手推（cutout.js:621-623）。"""
    cases = [
        ((300, 400), (1024, 768, 0, 128)),    # 横图：长边 400 → nw=1024, oy=(1024-768)//2
        ((400, 300), (768, 1024, 128, 0)),    # 竖图：长边 400 → nh=1024, ox=(1024-768)//2
        ((256, 256), (1024, 1024, 0, 0)),     # 方图：s=4，无补边
    ]
    for (h, w), (nw, nh, ox, oy) in cases:
        tensor, crop = seg_preprocess(_gradient(h, w))
        assert tensor.shape == (1, 3, M.SEG_SZ, M.SEG_SZ)
        assert tensor.dtype == np.float32
        assert crop == (ox, oy, nw, nh)


def test_preprocess_normalization_and_channel_order():
    """归一化 (x/255-mean)/std 与 NCHW/RGB 布局（cutout.js:311-312, 658-663）。"""
    img = np.zeros((64, 64, 3))
    img[:, :] = (1.0, 0.0, 0.0)                      # 纯红
    tensor, _ = seg_preprocess(img)
    # 平面 0=R、1=G、2=B：红图的 R 平面为高正值，G/B 平面为负
    assert tensor[0, 0, 500, 500] == pytest.approx(_norm(255, 0), rel=1e-6)
    assert tensor[0, 1, 500, 500] == pytest.approx(_norm(0, 1), rel=1e-6)
    assert tensor[0, 2, 500, 500] == pytest.approx(_norm(0, 2), rel=1e-6)
    assert tensor[0, 0, 0, 0] > 0.0
    assert tensor[0, 1, 0, 0] < 0.0 and tensor[0, 2, 0, 0] < 0.0

    gray = np.full((32, 32, 3), 128 / 255.0)         # 128 灰
    t2, _ = seg_preprocess(gray)
    for ch in range(3):
        assert t2[0, ch, 10, 10] == pytest.approx(_norm(128, ch), rel=1e-6)


def test_preprocess_edge_replication_padding():
    """补边 = 边缘延展：补边区 == 缩放图的首/末行/列（cutout.js:636-655，
    letterbox 几何下只有短边两侧有补边，四角 ext 恒空操作）。"""
    # 竖图 (H=400, W=300)：ox=128 → 左/右两侧补边
    img = np.full((400, 300, 3), 0.5)
    img[:, :16] = (0.0, 1.0, 0.0)                    # 左缘绿带（宽 16，覆盖双线性支撑）
    img[:, 284:] = (1.0, 1.0, 0.0)                   # 右缘黄带
    tensor, (ox, oy, nw, nh) = seg_preprocess(img)
    assert oy == 0 and ox == 128 and nw == 768
    mid = M.SEG_SZ // 2
    for col in (0, 3, ox - 1):                       # 左补边区
        assert tensor[0, 1, mid, col] == pytest.approx(_norm(255, 1), rel=1e-6)   # G=255
        assert tensor[0, 0, mid, col] == pytest.approx(_norm(0, 0), rel=1e-6)     # R=0
    for col in (ox + nw, M.SEG_SZ - 2, M.SEG_SZ - 1):  # 右补边区
        assert tensor[0, 0, mid, col] == pytest.approx(_norm(255, 0), rel=1e-6)   # R=255
        assert tensor[0, 2, mid, col] == pytest.approx(_norm(0, 2), rel=1e-6)     # B=0
    # 补边区与图片自身边缘列相同
    assert np.allclose(tensor[0, :, mid, :ox], tensor[0, :, mid, ox:ox + 1])
    assert np.allclose(tensor[0, :, mid, ox + nw:], tensor[0, :, mid, ox + nw - 1:ox + nw])

    # 横图 (H=300, W=400)：oy=128 → 上/下两侧补边
    img2 = np.full((300, 400, 3), 0.5)
    img2[:16, :] = (1.0, 0.0, 0.0)                   # 上缘红带
    img2[284:, :] = (0.0, 0.0, 1.0)                  # 下缘蓝带
    t2, (ox2, oy2, nw2, nh2) = seg_preprocess(img2)
    assert ox2 == 0 and oy2 == 128 and nh2 == 768
    row_mid = M.SEG_SZ // 2
    for row in (0, 5, oy2 - 1):
        assert t2[0, 0, row, row_mid] == pytest.approx(_norm(255, 0), rel=1e-6)
    for row in (oy2 + nh2, M.SEG_SZ - 3, M.SEG_SZ - 1):
        assert t2[0, 2, row, row_mid] == pytest.approx(_norm(255, 2), rel=1e-6)
    assert np.allclose(t2[0, :, :oy2, row_mid], t2[0, :, oy2:oy2 + 1, row_mid])


def test_preprocess_accepts_uint8_and_rgba():
    """uint8 入参按 0..255 归一、RGBA 丢 alpha（原版只读 RGB 三通道）。"""
    u8img = (_u8(_gradient(64, 48))[:, :, :3]).astype(np.uint8)
    ref, _ = seg_preprocess(_gradient(64, 48))
    t1, _ = seg_preprocess(u8img)
    assert np.array_equal(t1, ref)
    rgba = np.dstack([_u8(_gradient(64, 48)), np.full((64, 48, 1), 255, np.uint8)])
    t2, _ = seg_preprocess(rgba)
    assert np.array_equal(t2, ref)
    with pytest.raises(ValueError):
        seg_preprocess(np.zeros((8, 8), dtype=np.float64))       # 非三维
    with pytest.raises(ValueError):
        seg_preprocess(np.zeros((8, 8, 5), dtype=np.float64))    # 通道数不对


# ────────────────────────── seg_postprocess ──────────────────────────

def test_postprocess_crop_and_band_values():
    """裁回图片区域 + 窄带线性二值化（cutout.js:681-718）。"""
    crop = (200, 100, 600, 800)                       # ox, oy, nw, nh
    raw = np.zeros((1024, 1024))
    raw[100:900, 200:800] = 1.0                       # 图片区域：中心实块
    raw[150, 250] = 0.9                               # 孤立噪点（不去除——keep_largest_blob 的事）
    raw[0:50, 0:50] = 1.0                             # 补边区高热值
    raw[100, 200] = 0.20
    raw[101, 200] = 0.37
    raw[102, 200] = 0.50
    raw[103, 200] = 0.00                              # 证明无 sigmoid（有则 0.5→1）
    out = seg_postprocess(raw, crop, (1024, 1024))

    assert out.shape == (1024, 1024)
    # 去补边区为 0
    assert out[0, 0] == 0.0 and out[0:50, 0:50].max() == 0.0
    assert out[950, 950] == 0.0 and out[950, 250] == 0.0
    # 裁回区域正确：图片区域内 = binarize(原值)，含孤立噪点
    assert out[500, 500] == 1.0
    assert out[150, 250] == 1.0
    # 窄带二值化手推：lo=0.28, hi=0.46, 带宽 0.18
    assert out[100, 200] == pytest.approx(0.0)                    # 0.20 < lo → 0
    assert out[101, 200] == pytest.approx(0.5)                    # (0.37-0.28)/0.18 = 线性中值
    assert out[102, 200] == pytest.approx(1.0)                    # 0.50 > hi → 1
    assert out[103, 200] == pytest.approx(0.0)                    # 0.00 → 0（无 sigmoid）
    band = (raw[100:900, 200:800] - LO) / (HI - LO)
    assert np.allclose(out[100:900, 200:800], np.clip(band, 0.0, 1.0))


def test_postprocess_band_math_boundary():
    """窄带端点与中点、带外饱和（cutout.js:712-718）。"""
    crop = (0, 0, 64, 64)
    vals = {0.28: 0.0, 0.37: 0.5, 0.46: 1.0, 0.30: (0.30 - LO) / (HI - LO),
            0.40: (0.40 - LO) / (HI - LO), 0.10: 0.0, 0.90: 1.0}
    raw = np.zeros((64, 64))
    for i, v in enumerate(vals):
        raw[i, 0] = v
    out = seg_postprocess(raw, crop, (64, 64))
    for i, (v, exp) in enumerate(vals.items()):
        assert out[i, 0] == pytest.approx(exp), f"p={v}"


def test_postprocess_input_layouts_and_errors():
    """[1,1,H,W]/[H,W] 布局均可；尺寸不符/越界显式报错（JS 会读到越界内存）。"""
    raw = np.full((1, 1, 64, 48), 0.37, dtype=np.float32)
    out = seg_postprocess(raw, (0, 0, 48, 64), (64, 48))
    assert np.allclose(out, 0.5)
    out2 = seg_postprocess(raw[0, 0], (0, 0, 48, 64), (64, 48))
    assert np.array_equal(out, out2)
    with pytest.raises(ValueError):                   # crop 超出模型输出尺寸
        seg_postprocess(np.zeros((64, 48)), (0, 0, 64, 64), (64, 48))
    with pytest.raises(ValueError):
        seg_postprocess(np.zeros((64, 48)), (40, 0, 16, 64), (64, 48))
    with pytest.raises(ValueError):                   # 元素数与 shape 不符
        seg_postprocess(np.zeros((32, 32)), (0, 0, 8, 8), (64, 48))


# ────────────────────────── keep_largest_blob ──────────────────────────

def test_keep_largest_blob_holes_and_domains():
    """双连通域 + 带洞：只剩最大域、洞被补、软值保留（cutout.js:554-606）。"""
    m = np.zeros((30, 30))
    m[5:25, 5:25] = 0.9                              # 最大域（软值 0.9）
    m[10:15, 10:15] = 0.1                            # 域内洞（背景）
    m[0:3, 0:3] = 0.7                                # 第二连通域（外部）
    out = keep_largest_blob(m)

    assert out.shape == m.shape
    assert out[20, 20] == pytest.approx(0.9)         # 保留下来的像素用回原软值，不写 1.0
    assert out[12, 12] == pytest.approx(0.1)         # 洞被补上，值仍是原软值
    assert out[0, 0] == 0.0 and out[1, 1] == 0.0     # 第二域被丢掉
    kept = np.count_nonzero(out)
    assert kept == (20 * 20 - 5 * 5) + 5 * 5         # 最大域面积（含洞）
    # 入参不被就地修改
    assert m[20, 20] == 0.9 and m[12, 12] == 0.1


def test_keep_largest_blob_enclosed_speck_is_filled():
    """被最大域整个包住的小块对边界泛洪是障碍 → 被当成洞补回来（JS 同行为）。"""
    m = np.zeros((24, 24))
    m[4:20, 4:20] = 0.9
    m[10:13, 10:13] = 0.6                            # 包在最大域内部的小块
    out = keep_largest_blob(m)
    assert out[11, 11] == pytest.approx(0.6)
    assert np.count_nonzero(out) == 16 * 16


def test_keep_largest_blob_diagonal_eight_connected():
    """8 连通：只斜着连的发丝不会被拆成多块丢掉（cutout.js:552-553）。"""
    m = np.zeros((16, 16))
    m[2:12, 2:6] = 0.9                               # 主体
    for i in range(6, 12):                           # 从主体角上斜着伸出去的一根发丝
        m[i, i - 2] = 0.8
    out = keep_largest_blob(m)
    assert out[11, 9] == pytest.approx(0.8)          # 发丝连在主体上，整根保留
    # 对照：同样形状但发丝断了（与主体 4 连通也不连）→ 只留主体
    m2 = m.copy()
    m2[8, 6] = 0.0
    out2 = keep_largest_blob(m2)
    assert out2[11, 9] == 0.0
    assert np.count_nonzero(out2) == 4 * 10


def test_keep_largest_blob_threshold_is_half():
    """阈值 0.5 与 JS 的 uint8 >=128 逐位等价。

    等价式：js_round(255·band) >= 128 ⟺ 255·band >= 127.5 ⟺ band >= 0.5。
    ⚠ 0.37 这个「窄带中值」在 float64 下是 0.49999999999999994，差一点点没过阈；
      JS 侧 js_round(0.49999999999999994·255)=127 < 128 同样判背景 —— 两边一致，
      这正是浮点阈值能逐位复现 uint8 域 >=128 的证据，故按 JS 口径断言。
    """
    for p in (0.0, 0.20, 0.28, 0.30, 0.37, 0.40, 0.46, 0.90, 1.0):
        band = (p - LO) / (HI - LO)
        js_on = M.js_round(band * 255) >= 128
        m = np.zeros((4, 4))
        m[1, 1] = band
        assert (np.count_nonzero(keep_largest_blob(m)) == 1) == js_on, f"p={p}"
    # 带宽内一档：0.38 稳过阈；0.37 公差不过 —— 都与 JS 同判
    assert np.count_nonzero(keep_largest_blob(np.full((4, 4), (0.38 - LO) / (HI - LO)))) == 16
    assert np.count_nonzero(keep_largest_blob(np.full((4, 4), (0.37 - LO) / (HI - LO)))) == 0


def test_keep_largest_blob_degenerate():
    """全背景 → 全 0；全前景 → 原样；入参形状保护。"""
    z = np.zeros((6, 6))
    assert np.array_equal(keep_largest_blob(z), z)
    ones = np.ones((6, 6))
    assert np.array_equal(keep_largest_blob(ones), ones)
    with pytest.raises(ValueError):
        keep_largest_blob(np.zeros((2, 2, 2)))


# ─────────────────────── anime_cut（fake session 端到端） ───────────────────────

def test_anime_cut_full_foreground(monkeypatch, tmp_path):
    """假 session 返回「图片区域全 1」：RGBA 通道对应、fg=1、输入名 'img'。"""
    img = _gradient(128, 96)                          # (H,W)=128,96 → 竖图，s=8
    _, (ox, oy, nw, nh) = seg_preprocess(img)
    assert (nw, nh) == (768, 1024) and ox == 128 and oy == 0
    sess = _patch_session(monkeypatch, _fake_mask((ox, oy, nw, nh)))
    model = tmp_path / "fake.onnx"
    model.write_bytes(b"not-a-real-model")

    r = anime_cut(img, str(model))
    assert isinstance(r, MattingResult)
    assert r.rgba.shape == (128, 96, 4) and r.rgba.dtype == np.uint8
    assert np.array_equal(r.rgba[..., :3], _u8(img))          # RGB 保留原图
    assert np.all(r.rgba[..., 3] == 255)                      # alpha = 蒙版
    assert r.fg == pytest.approx(1.0)
    assert r.ms > 0.0
    # 喂给会话的输入名与张量形状
    assert list(sess.feeds) == ["img"]
    tensor = sess.feeds["img"]
    assert tensor.shape == (1, 3, M.SEG_SZ, M.SEG_SZ) and tensor.dtype == np.float32
    with pytest.raises(FrozenInstanceError):
        r.fg = 0.5                                             # frozen dataclass


def test_anime_cut_half_foreground(monkeypatch, tmp_path):
    """假 session 返回「上半区域」：fg=0.5 手推，alpha 边界与插值后仍二值主导。"""
    img = _gradient(128, 96)
    _, (ox, oy, nw, nh) = seg_preprocess(img)
    m = np.zeros((1, 1, M.SEG_SZ, M.SEG_SZ), dtype=np.float32)
    m[0, 0, 0:512, ox:ox + nw] = 1.0                           # 恰好一半的图片区域
    _patch_session(monkeypatch, m)
    (tmp_path / "m.onnx").write_bytes(b"x")

    r = anime_cut(img, str(tmp_path / "m.onnx"))
    assert r.fg == pytest.approx(0.5)
    assert r.rgba[32, 48, 3] == 255                            # 上半内部
    assert r.rgba[100, 48, 3] == 0                             # 下半外部（离边界足够远）
    assert 0 < r.rgba[64, 48, 3] < 255                         # 边界落在窄带过渡里
    assert np.array_equal(r.rgba[..., :3], _u8(img))
    assert r.rgba.shape == img.shape[:2] + (4,)


def test_anime_cut_fg_gate(monkeypatch, tmp_path):
    """fg<0.02 → ValueError('没有检出前景')（index.html:2703）。"""
    img = _gradient(128, 96)
    _, (ox, oy, nw, nh) = seg_preprocess(img)
    m = np.zeros((1, 1, M.SEG_SZ, M.SEG_SZ), dtype=np.float32)
    m[0, 0, 500, ox + 300] = 1.0                               # 单个前景像素
    _patch_session(monkeypatch, m)
    (tmp_path / "m.onnx").write_bytes(b"x")

    with pytest.raises(ValueError, match="没有检出前景"):
        anime_cut(img, str(tmp_path / "m.onnx"))


def test_anime_cut_session_options(monkeypatch, tmp_path):
    """intra_op 透传到会话构造；模型参数参与解析。"""
    img = _gradient(64, 64)
    _, (ox, oy, nw, nh) = seg_preprocess(img)
    sess = _patch_session(monkeypatch, _fake_mask((ox, oy, nw, nh)))
    model = tmp_path / "m2.onnx"
    model.write_bytes(b"x")
    anime_cut(img, str(model), intra_op=4, simd=False)
    assert sess.intra_op == 4
    with pytest.raises(FileNotFoundError, match="model_path"):
        anime_cut(img, str(tmp_path / "missing.onnx"))


# ────────────────────────── 模型解析 ──────────────────────────

def test_resolve_model_priority(monkeypatch, tmp_path):
    """参数 → $ARCAM_ISNET_MODEL → 仓库 anime/ → ~/.cache/arcam（cutout.js:509-530）。"""
    param = tmp_path / "param.onnx"; param.write_bytes(b"x")
    env = tmp_path / "env.onnx"; env.write_bytes(b"x")
    repo = tmp_path / "repo.onnx"; repo.write_bytes(b"x")
    cache = tmp_path / "cache.onnx"; cache.write_bytes(b"x")
    monkeypatch.setenv(M.MODEL_ENV, str(env))
    monkeypatch.setattr(M, "MODEL_REPO", repo)
    monkeypatch.setattr(M, "MODEL_CACHE", cache)

    assert M._resolve_model_path(str(param)) == param                # 参数最优先
    monkeypatch.delenv(M.MODEL_ENV)
    monkeypatch.setattr(M, "MODEL_REPO", tmp_path / "nope.onnx")
    assert M._resolve_model_path(None) == cache                       # 落到缓存
    monkeypatch.setattr(M, "MODEL_CACHE", tmp_path / "nope2.onnx")
    monkeypatch.setattr(M, "MODEL_REPO", repo)
    assert M._resolve_model_path(None) == repo                        # 仓库次优先
    monkeypatch.setattr(M, "MODEL_REPO", tmp_path / "nope.onnx")
    with pytest.raises(FileNotFoundError) as ei:
        M._resolve_model_path(None)
    msg = str(ei.value)
    assert "skytnt/anime-seg" in msg and "sigmoid" in msg             # 下载指引 + sigmoid 差异
    assert M.MODEL_SHA256.startswith("cb5f7850")                       # cutout.js:368 钉的 sha


def test_resolve_model_missing_sources(monkeypatch, tmp_path):
    """显式路径/环境变量指向不存在的文件 → 立刻报错（不静默 fallthrough）。"""
    monkeypatch.setattr(M, "MODEL_CACHE", tmp_path / "nope.onnx")
    monkeypatch.setattr(M, "MODEL_REPO", tmp_path / "nope.onnx")
    with pytest.raises(FileNotFoundError, match="model_path"):
        M._resolve_model_path(str(tmp_path / "missing.onnx"))
    monkeypatch.setenv(M.MODEL_ENV, str(tmp_path / "missing-env.onnx"))
    with pytest.raises(FileNotFoundError, match=M.MODEL_ENV):
        M._resolve_model_path(None)


# ────────────────────────── 真模型 E2E（allow skipif） ──────────────────────────

@pytest.mark.skipif(
    not (_HAS_ORT and _real_model() is not None),
    reason=f"{_ORT_NOTE} 或 isnet-anime-q8.onnx 模型不存在",
)
def test_anime_cut_real_model():
    """真模型端到端：带纹理的合成「主体」图 → 前景占比、alpha 二值性、RGB 保留。

    ⚠ 纯色块模型不认（isnet-anime 在动漫数据上训，扁平色块属分布外，输出全 0）；
    这里用噪声纹理+正弦调制的椭圆（固定种子、确定性），模型实测判出约四成前景。
    """
    img = _character_like_image()

    r = anime_cut(img)
    assert r.rgba.shape == (160, 120, 4) and r.rgba.dtype == np.uint8
    assert np.array_equal(r.rgba[..., :3], _u8(img))
    assert 0.02 < r.fg < 0.95
    alpha = r.rgba[..., 3]
    assert alpha[80, 60] == 255                                       # 主体内部
    assert alpha[2, 2] == 0                                           # 四角背景
    assert np.mean((alpha == 0) | (alpha == 255)) > 0.9                # 二值主导（窄带）
    assert r.ms > 0.0


def test_import_does_not_pull_onnxruntime(tmp_path):
    """懒加载硬规则：全新解释器里 import matting 不会导入 onnxruntime。

    隔离验证：只放 numeric/matting 两个文件的空 arcam 包 + PYTHONPATH，
    子进程里 import arcam.matting，断言 onnxruntime 不在 sys.modules。
    """
    src = Path(__file__).resolve().parents[1] / "src"
    pkg = tmp_path / "arcam"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    shutil.copy(src / "arcam" / "numeric.py", pkg / "numeric.py")
    shutil.copy(src / "arcam" / "matting.py", pkg / "matting.py")
    code = (
        "import sys; import arcam.matting as m; "
        "assert 'onnxruntime' not in sys.modules; "
        "assert callable(m.anime_cut)"
    )
    env = dict(os.environ, PYTHONPATH=str(tmp_path))
    proc = subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
