"""L1 LiDAR 深度 IO 测试 —— LIDAR-SPEC §5 门禁 T1（读取/格式/对齐）。

覆盖：
- 16-bit PNG/TIF 毫米→米（换算只一处）、0/负→NaN、内参估计非实测；
- .npy 米制读入 + 针孔满幅 FOV≈60° 内参估计（手算）；
- .depth magic ARCD 小头二进制往返（含逐字节布局断言）；
- 有效比例 > 2% 门禁（含 1.99% / 2.00% / 2.01% 边界）；
- align 最近邻缩放 + 内参同比缩放 + NaN 传播不变量；
- heic 未装 pillow-heif 时的 RuntimeError 指引文案（monkeypatch 使 ImportError 确定性触发）。

数值断言均附手算过程注释（docs/SPEC.md §4.3）。文件一律写 tmp_path（§4.9）。
"""

from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from arcam.depth import (
    ARCD_HEADER,
    ARCD_MAGIC,
    LiDARFrame,
    _to_arcam_camera_coords,
    align_to_canvas,
    load_depth,
    save_depth,
)


def _write_png16(path: Path, arr_mm: np.ndarray) -> None:
    """uint16 毫米数组 → 16-bit PNG（Image.fromarray 对 uint16 自动给 mode I;16）。"""
    image = Image.fromarray(np.asarray(arr_mm, dtype=np.uint16))
    assert image.mode in ('I;16', 'I;16B', 'I;16L', 'I;16N')
    image.save(path, format='PNG')


def _ratio_mask(count: int, height: int = 100, width: int = 100) -> np.ndarray:
    """正好 count 个 True 的布尔掩码（总像素 height*width）。"""
    flat = np.zeros(height * width, dtype=bool)
    flat[np.linspace(0, height * width - 1, count).astype(int)] = True
    return flat.reshape(height, width)


# ── _to_arcam_camera_coords ───────────────────────────────────────────────────

def test_to_arcam_camera_coords_flips_only_y() -> None:
    """y 翻转（LIDAR-SPEC §1），x/z 不变；不改入参；输出 float64。"""
    src = np.array([[1.0, 2.0, 3.0], [-0.5, -0.25, 10.0]])
    out = _to_arcam_camera_coords(src)
    np.testing.assert_array_equal(out, [[1.0, -2.0, 3.0], [-0.5, 0.25, 10.0]])
    np.testing.assert_array_equal(src, [[1.0, 2.0, 3.0], [-0.5, -0.25, 10.0]])  # 入参未动
    assert out.dtype == np.float64


def test_to_arcam_camera_coords_rejects_bad_shape() -> None:
    with pytest.raises(ValueError, match=r'\(N,3\)'):
        _to_arcam_camera_coords(np.zeros(3))
    with pytest.raises(ValueError, match=r'\(N,3\)'):
        _to_arcam_camera_coords(np.zeros((4, 4)))


# ── 16-bit PNG / TIF（毫米 → 米）───────────────────────────────────────────────

def test_png16_millimetres_to_metres_zero_is_nan(tmp_path: Path) -> None:
    """16-bit PNG 是 3D 扫描 App 的通行约定：uint16 毫米，0=无效。

    手算：1500 mm → 1.5 m；2500 mm → 2.5 m；500 mm → 0.5 m；0 → NaN。
    """
    mm = np.array([[0, 1500], [2500, 500]], dtype=np.uint16)
    _write_png16(tmp_path / 'scene.png', mm)
    frame = load_depth(tmp_path / 'scene.png')
    assert frame.depth_m.dtype == np.float32
    assert np.isnan(frame.depth_m[0, 0])
    assert frame.depth_m[0, 1] == 1.5
    assert frame.depth_m[1, 0] == 2.5
    assert frame.depth_m[1, 1] == 0.5
    assert frame.source.endswith('scene.png')


def test_tif16_millimetres_to_metres(tmp_path: Path) -> None:
    """.tif 与 .png 同路径同口径（同为 16-bit 单通道）。"""
    mm = np.array([[1024, 2048]], dtype=np.uint16)
    image = Image.fromarray(mm)
    assert image.mode == 'I;16'
    image.save(tmp_path / 'scene.tif', format='TIFF')
    frame = load_depth(tmp_path / 'scene.tif')
    assert frame.depth_m[0, 0] == pytest.approx(1.024)
    assert frame.depth_m[0, 1] == pytest.approx(2.048)


def test_png16_estimates_pinhole_intrinsics(tmp_path: Path) -> None:
    """16-bit 图像不带内参元数据 → 针孔满幅 FOV≈60° 估计（估计非实测）。

    手算（W=1920）：fx = W/(2·tan30°) = 960/tan30° = 960·√3 = 1662.7687752661223；
    cx = W/2 = 960；cy = H/2 = 540；fy = fx（方形像素假设，LIDAR-SPEC §2 只写死 fx）。
    """
    _write_png16(tmp_path / 'p.png', np.full((1080, 1920), 1500, dtype=np.uint16))
    frame = load_depth(tmp_path / 'p.png')
    assert frame.fx == pytest.approx(960.0 * math.sqrt(3.0), rel=1e-12)
    assert frame.fy == pytest.approx(960.0 * math.sqrt(3.0), rel=1e-12)
    assert frame.cx == 960.0
    assert frame.cy == 540.0
    assert frame.roll_deg is None and frame.pitch_deg is None and frame.height_m is None


def test_png16_rejects_non_16bit(tmp_path: Path) -> None:
    """8-bit 位图 / RGB 人像照片不是深度图：报错并指引导出 16-bit 灰度。"""
    Image.fromarray(np.zeros((4, 4), dtype=np.uint8)).save(tmp_path / 'l8.png')
    with pytest.raises(ValueError, match='16-bit 单通道'):
        load_depth(tmp_path / 'l8.png')
    Image.fromarray(np.zeros((4, 4, 3), dtype=np.uint8)).save(tmp_path / 'rgb.png')
    with pytest.raises(ValueError, match='16-bit 单通道'):
        load_depth(tmp_path / 'rgb.png')


# ── .npy（米，内参估计）────────────────────────────────────────────────────────

def test_npy_metres_and_invalid_to_nan(tmp_path: Path) -> None:
    """float32 (H,W) 米；0/负/NaN 一律 → NaN（LIDAR-SPEC §2 读入统一）。"""
    data = np.array([[1.0, np.nan], [-3.0, 4.0]], dtype=np.float32)
    path = tmp_path / 'd.npy'
    np.save(path, data)
    frame = load_depth(str(path))              # str 路径也接受（签名 str | Path）
    assert frame.depth_m[0, 0] == 1.0
    assert np.isnan(frame.depth_m[0, 1])
    assert np.isnan(frame.depth_m[1, 0])       # 负值 → NaN
    assert frame.depth_m[1, 1] == 4.0


def test_npy_estimates_pinhole_intrinsics(tmp_path: Path) -> None:
    """.npy 缺省内参按针孔满幅 FOV≈60° 估计（LIDAR-SPEC §2，估计非实测）。

    手算（W=640）：fx = 320/tan30° = 320·√3 = 554.2562584162048；fy=fx；
    cx = 320；cy = 240。
    """
    np.save(tmp_path / 'd.npy', np.full((480, 640), 2.0, dtype=np.float32))
    frame = load_depth(tmp_path / 'd.npy')
    assert frame.fx == pytest.approx(320.0 * math.sqrt(3.0), rel=1e-12)
    assert frame.fy == pytest.approx(320.0 * math.sqrt(3.0), rel=1e-12)
    assert frame.cx == 320.0
    assert frame.cy == 240.0


def test_npy_rejects_bad_shapes(tmp_path: Path) -> None:
    np.save(tmp_path / 'one.npy', np.zeros(5, dtype=np.float32))
    with pytest.raises(ValueError, match='二维深度数组'):
        load_depth(tmp_path / 'one.npy')
    np.save(tmp_path / 'three.npy', np.ones((2, 2, 2), dtype=np.float32))
    with pytest.raises(ValueError, match='二维深度数组'):
        load_depth(tmp_path / 'three.npy')
    np.save(tmp_path / 'empty.npy', np.zeros((0, 0), dtype=np.float32))
    with pytest.raises(ValueError, match='尺寸非法'):
        load_depth(tmp_path / 'empty.npy')


def test_valid_ratio_gate_strictly_greater_than_two_percent(tmp_path: Path) -> None:
    """门禁：有效比例须 > 2%（10000 像素的 2% = 200 个有效像素）。

    手算：199 px → 1.99% 报错；200 px → 2.00% 不满足「>」同样报错；
    201 px → 2.01% 放行。
    """
    for count, ok in ((199, False), (200, False), (201, True)):
        np.save(tmp_path / f'r{count}.npy',
                np.where(_ratio_mask(count), 1.5, np.nan).astype(np.float32))
        if ok:
            frame = load_depth(tmp_path / f'r{count}.npy')
            assert frame.depth_m.shape == (100, 100)
        else:
            with pytest.raises(ValueError, match='没有有效深度'):
                load_depth(tmp_path / f'r{count}.npy')


def test_all_invalid_frame_rejected(tmp_path: Path) -> None:
    np.save(tmp_path / 'all.npy', np.full((64, 64), np.nan, dtype=np.float32))
    with pytest.raises(ValueError, match='没有有效深度'):
        load_depth(tmp_path / 'all.npy')


# ── .depth（magic ARCD 小头）───────────────────────────────────────────────────

def test_arcd_roundtrip_and_binary_layout(tmp_path: Path) -> None:
    """save_depth → load_depth 往返 + 逐字节核对 LIDAR-SPEC §2 布局。

    布局：magic 4B | W u32 | H u32 | fx fy cx cy roll pitch height 各 f32 | depth H*W f32，
    一律小头；roll/pitch/height 的 NaN = 「无」；数据区 NaN 写 0（无效标记）。
    """
    depth = np.full((3, 5), 2.5, dtype=np.float32)
    depth[0, 0] = np.nan          # 无效 → 落盘 0.0
    depth[1, 2] = 0.0             # 0 也是无效 → 读回 NaN
    source = LiDARFrame(depth, fx=600.0, fy=610.0, cx=320.0, cy=241.0,
                        roll_deg=7.5, pitch_deg=None, height_m=1.6, source='unit')
    path = tmp_path / 'f.depth'
    save_depth(source, path)
    raw = path.read_bytes()

    # 逐字节布局断言（头部 40 B + 15 px × 4 B）
    assert len(raw) == 40 + 15 * 4
    assert raw[:4] == b'ARCD'
    w, h = struct.unpack_from('<II', raw, 4)
    assert (w, h) == (5, 3)
    fx, fy, cx, cy, roll, pitch = struct.unpack_from('<6f', raw, 12)
    assert (fx, fy, cx, cy) == (600.0, 610.0, 320.0, 241.0)
    assert roll == 7.5                        # 提供的外参原样往返
    assert math.isnan(pitch)                  # None → NaN（「无」标记）
    assert struct.unpack_from('<f', raw, 36)[0] == pytest.approx(1.6, rel=1e-6)
    assert struct.unpack_from('<f', raw, 40)[0] == 0.0     # depth[0,0] NaN → 0
    assert struct.unpack_from('<f', raw, 44 + 4 * 7)[0] == 2.5   # depth[1,2] 源有效

    back = load_depth(path)
    np.testing.assert_array_equal(back.depth_m[0], [np.nan, 2.5, 2.5, 2.5, 2.5])
    np.testing.assert_array_equal(back.depth_m[1], [2.5, 2.5, np.nan, 2.5, 2.5])
    assert (back.fx, back.fy, back.cx, back.cy) == (600.0, 610.0, 320.0, 241.0)
    assert back.roll_deg == 7.5
    assert back.pitch_deg is None             # NaN 头 → None
    assert back.height_m == pytest.approx(1.6, rel=1e-6)


def test_arcd_negative_depth_reads_as_nan(tmp_path: Path) -> None:
    depth = np.full((2, 2), -1.0, dtype=np.float32)
    depth[0, 0] = 5.0
    save_depth(LiDARFrame(depth, 600.0, 600.0, 1.0, 1.0), tmp_path / 'neg.depth')
    back = load_depth(tmp_path / 'neg.depth')
    assert back.depth_m[0, 0] == 5.0
    assert np.isnan(back.depth_m[0, 1])        # 负值 → NaN
    assert np.isnan(back.depth_m[1, 0])


def test_arcd_bad_magic_and_truncated(tmp_path: Path) -> None:
    (tmp_path / 'bad.depth').write_bytes(b'XXXX' + b'\x00' * 36)
    with pytest.raises(ValueError, match='magic'):
        load_depth(tmp_path / 'bad.depth')
    good = tmp_path / 'good.depth'
    save_depth(LiDARFrame(np.full((3, 5), 2.5, np.float32), 600.0, 600.0, 320.0, 240.0),
               good)
    raw = good.read_bytes()
    (tmp_path / 'short_head.depth').write_bytes(raw[:20])
    with pytest.raises(ValueError, match='长度'):
        load_depth(tmp_path / 'short_head.depth')
    (tmp_path / 'short_body.depth').write_bytes(raw[:60])
    with pytest.raises(ValueError, match='截断'):
        load_depth(tmp_path / 'short_body.depth')
    # 数据区全 0（无有效深度）同样被门禁拦下
    zeros = b'ARCD' + struct.pack('<II', 3, 5) + b'\x00' * (9 * 4 + 5 * 3 * 4)
    (tmp_path / 'zero.depth').write_bytes(zeros)
    with pytest.raises(ValueError, match='没有有效深度'):
        load_depth(tmp_path / 'zero.depth')


def test_unknown_extension_sniffs_arcd_magic(tmp_path: Path) -> None:
    """无扩展名/未知扩展名只嗅探自家 magic（.depth），其余报错列支持格式。"""
    stem = tmp_path / 'stream_frame'          # 实时流落盘可能无后缀
    save_depth(LiDARFrame(np.full((3, 5), 2.5, np.float32), 600.0, 600.0, 320.0, 240.0),
               stem)
    assert load_depth(stem).depth_m.shape == (3, 5)
    other = tmp_path / 'x.bin'
    other.write_bytes(b'\x00' * 10)
    with pytest.raises(ValueError, match='无法识别深度文件格式'):
        load_depth(other)


# ── LiDARFrame 契约字段 ────────────────────────────────────────────────────────

def test_lidar_frame_defaults() -> None:
    """字段名与默认值冻结自 LIDAR-SPEC §2（未提供的外参 None、source 空串）。"""
    frame = LiDARFrame(np.zeros((2, 2), np.float32), 1.0, 2.0, 3.0, 4.0)
    assert frame.roll_deg is None
    assert frame.pitch_deg is None
    assert frame.height_m is None
    assert frame.source == ''
    with pytest.raises(Exception):             # frozen：禁止就地改字段
        frame.fx = 9.0                            # type: ignore[misc]


# ── align_to_canvas ────────────────────────────────────────────────────────────

def test_align_scales_intrinsics_linearly(tmp_path: Path) -> None:
    """最近邻缩放画布；内参同比缩放（LIDAR-SPEC §2）。

    手算（256×192 → 512×384，2× 放大）：fx/fy 200→400，cx 128→256，cy 96→192；
    再缩回 1/2（→128×96）：fx/fy 100，cx 64，cy 48。
    """
    depth = np.full((192, 256), 2.0, dtype=np.float32)
    frame = LiDARFrame(depth, fx=200.0, fy=210.0, cx=128.0, cy=96.0,
                       roll_deg=3.0, pitch_deg=None, height_m=1.55, source='s')
    up = align_to_canvas(frame, 512, 384)
    assert up.depth_m.shape == (384, 512)
    assert up.fx == pytest.approx(400.0)
    assert up.fy == pytest.approx(420.0)
    assert up.cx == pytest.approx(256.0)
    assert up.cy == pytest.approx(192.0)
    assert up.roll_deg == 3.0 and up.pitch_deg is None and up.height_m == 1.55
    assert up.source == 's'
    down = align_to_canvas(frame, 128, 96)
    assert down.fx == pytest.approx(100.0)
    assert down.fy == pytest.approx(105.0)
    assert down.cx == pytest.approx(64.0)
    assert down.cy == pytest.approx(48.0)


def test_align_nearest_keeps_nan_invariant() -> None:
    """NaN 传播：先缩放掩码再取值 —— 空洞不缩小也不扩大。

    源 4×4 全 3.0，NaN 在 (1,1) 与 (2,2)。放大到 8×8，源下标映射
    r(i)=floor((i+.5)·4/8)=[0,0,1,1,2,2,3,3]，故源第 1/2 行各复制两行两列：
    NaN 必须**原样**落在画布 (2..3, 2..3) 与 (4..5, 4..5)，邻域全保持 3.0。
    缩到 2×2 时采样源下标 [1,3]，(2,2) 这个源 NaN 不再被采到（最近邻只会让
    空洞收缩，不会让有效值污染）——NaN 簇只能变小，绝不会变大。
    """
    src = np.full((4, 4), 3.0, dtype=np.float32)
    src[1, 1] = np.nan
    src[2, 2] = np.nan
    frame = LiDARFrame(src, 100.0, 100.0, 2.0, 2.0)
    up = align_to_canvas(frame, 8, 8).depth_m
    assert up.shape == (8, 8)
    expect = np.full((8, 8), 3.0, dtype=np.float32)
    expect[2:4, 2:4] = np.nan
    expect[4:6, 4:6] = np.nan
    np.testing.assert_array_equal(up, expect)
    # 两个源 NaN 各自 2×2 放大 = 4+4 = 8 个 NaN，且邻域有效值一个没被染脏
    assert np.isnan(up).sum() == 8
    assert np.count_nonzero(up == 3.0) == 64 - 8
    down = align_to_canvas(frame, 2, 2).depth_m
    assert np.isnan(down[0, 0]) and not np.isnan(down[0, 1])
    assert not np.isnan(down[1, 0]) and not np.isnan(down[1, 1])


def test_align_identity_and_guards() -> None:
    src = np.full((4, 4), 3.0, dtype=np.float32)
    src[1, 1] = np.nan
    frame = LiDARFrame(src, 100.0, 100.0, 2.0, 2.0)
    same = align_to_canvas(frame, 4, 4)
    np.testing.assert_array_equal(same.depth_m, frame.depth_m)
    assert same.fx == 100.0 and same.fy == 100.0 and same.cx == 2.0 and same.cy == 2.0
    assert align_to_canvas(frame, 4, 4).depth_m.dtype == np.float32
    with pytest.raises(ValueError, match='画布尺寸非法'):
        align_to_canvas(frame, 0, 8)
    with pytest.raises(ValueError, match='画布尺寸非法'):
        align_to_canvas(frame, 8, -1)


# ── heic（未装 pillow-heif）────────────────────────────────────────────────────

def test_heic_without_pillow_heif_raises_runtime_error(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """pillow-heif 懒加载失败必须抛带指引的 RuntimeError，绝不静默。

    monkeypatch 把 sys.modules['pillow_heif'] 置 None，使函数内
    ``import pillow_heif`` 确定性触发 ImportError（与是否真装无关）。
    """
    path = tmp_path / 'portrait.heic'
    path.write_bytes(b'')
    monkeypatch.setitem(sys.modules, 'pillow_heif', None)
    with pytest.raises(RuntimeError, match='pillow-heif'):
        load_depth(path)
    with pytest.raises(RuntimeError) as info:
        load_depth(path)
    message = str(info.value)
    assert 'pip install pillow-heif' in message     # 安装指引
    assert '16-bit' in message or '.depth' in message  # 替代格式指引


# ── EXR / HEIC 规范路径（编排者集成增补，LIDAR-SPEC §2 第 4/5 优先级）─────────

def test_exr_without_openexr_raises_guidance_error(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenEXR 懒加载失败 → 带指引的 RuntimeError（pip install arcam[lidar]）。"""
    path = tmp_path / 'depth.exr'
    path.write_bytes(b'')
    monkeypatch.setitem(sys.modules, 'OpenEXR', None)
    with pytest.raises(RuntimeError) as info:
        load_depth(path)
    message = str(info.value)
    assert 'OpenEXR' in message
    assert 'arcam[lidar]' in message or 'pip install OpenEXR' in message
    assert '16-bit' in message or '.depth' in message


def test_exr_round_trip_when_installed(tmp_path: Path) -> None:
    """装有 OpenEXR 时：写一帧 Z=浮点米的 EXR，读回按 Apple 轴向语义逐位一致。"""
    pytest.importorskip('OpenEXR')
    pytest.importorskip('Imath')
    import Imath
    import OpenEXR

    height, width = 24, 32
    z = np.zeros((height, width), dtype=np.float32)
    z[4:16, 8:24] = 3.5                     # 中心一块前景 3.5m
    header = OpenEXR.Header(width, height)
    header['channels'] = {'Z': Imath.Channel(Imath.PixelType(Imath.PixelType.FLOAT))}
    out = OpenEXR.OutputFile(str(tmp_path / 'z.exr'), header)
    out.writePixels({'Z': z.tobytes()})
    out.close()

    frame = load_depth(tmp_path / 'z.exr')
    assert frame.depth_m.shape == (height, width)
    got = frame.depth_m.astype(np.float64)
    assert float(got[10, 16]) == pytest.approx(3.5)
    assert math.isnan(float(got[0, 0]))     # 0 → NaN（§1 无效口径）


def test_heic_depth_images_path_when_installed(tmp_path: Path) -> None:
    """装有 pillow-heif 时：真 HEIC 容器 + 注入 pillow-heif 真实返回结构的
    ``depth_images``，逐表示类型验证 Python 侧转换逻辑。

    pillow-heif 1.8 只暴露读侧 API（HeifDepthImage 是 c_image 的包装），
    合成不了带 Apple 辅图的真机文件；这里用**真 HEIC 容器**（RGB 主图真由
    libheif 编码）+ 与 pillow-heif 逐字段同构的假 depth_images 注入，
    覆盖 depth.py 里全部转换分支（rep 0/1/2/3 + 两条错误路径）。
    pillow-heif 自身的辅图提取是第三方代码，不在本测试范围。
    """
    pytest.importorskip('pillow_heif')
    import pillow_heif
    pillow_heif.register_heif_opener()      # 否则 PIL 不认 HEIF 保存格式
    from arcam.depth import _load_heic_depth

    # ── 真 HEIC 容器：主图真过 libheif 编解码 ──
    rgb = np.zeros((48, 64, 3), np.uint8)
    rgb[..., 0] = 180
    rgb[10:30, 20:40] = (90, 140, 200)
    heic_path = tmp_path / 'portrait.heic'
    Image.fromarray(rgb, 'RGB').save(heic_path, format='HEIF')
    assert heic_path.is_file() and heic_path.stat().st_size > 0

    class _FakeDepth:
        """ pillow-heif HeifDepthImage 的字段级同构：.info['metadata'] + .to_pillow()。"""

        def __init__(self, arr: np.ndarray, metadata: dict):
            self.info = {'metadata': metadata}
            self._arr = arr

        def to_pillow(self) -> Image.Image:
            im = Image.fromarray(self._arr)
            im.info = dict(self.info)      # 真机 to_pillow 会把 info 复制过去
            return im

    class _FakeHeif:
        """Image.open 的返回同构：.info['depth_images'] + 上下文管理协议。"""

        def __init__(self, depths):
            self.info = {'depth_images': depths}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def patch_open(depths, monkeypatch):
        real_open = Image.open

        def fake_open(path, *a, **kw):
            if str(path).endswith(('.heic', '.heif')):
                return _FakeHeif(depths)
            return real_open(path, *a, **kw)

        monkeypatch.setattr(Image, 'open', fake_open)

    h, w = 48, 64
    # ── rep 2 UNIFORM_Z：深度即米，直接用 ──
    z2 = np.full((h, w), 4.0, dtype=np.float32)
    z2[0, 0] = 0.0                          # 0 → NaN（§1 无效口径）
    monkeypatch = pytest.MonkeyPatch()
    patch_open([_FakeDepth(z2, {'representation_type': 2})], monkeypatch)
    try:
        frame = _load_heic_depth(heic_path)
    finally:
        monkeypatch.undo()
    assert frame.depth_m.shape == (h, w)
    assert float(frame.depth_m[10, 20]) == pytest.approx(4.0)
    assert math.isnan(float(frame.depth_m[0, 0]))
    # 内参按辅助图尺寸估计（估计非实测，LIDAR-SPEC §2）：cx/cy 落在图心
    assert frame.fx > 0.0 and frame.fy > 0.0
    assert frame.cx == pytest.approx(w / 2.0) and frame.cy == pytest.approx(h / 2.0)
    # 外参 roll/pitch/height 取 None，交平面拟合解出（LIDAR-SPEC §2）
    assert frame.roll_deg is None and frame.pitch_deg is None
    assert frame.height_m is None

    # ── rep 0 UNIFORM_INVERSE_Z：1/m → m ──
    inv = np.full((h, w), 0.25, dtype=np.float32)     # 1/0.25 = 4m
    inv[0, 0] = 0.0                                   # 0 → NaN（除零保护区）
    monkeypatch = pytest.MonkeyPatch()
    patch_open([_FakeDepth(inv, {'representation_type': 0})], monkeypatch)
    try:
        frame = _load_heic_depth(heic_path)
    finally:
        monkeypatch.undo()
    assert float(frame.depth_m[10, 20]) == pytest.approx(4.0)
    assert math.isnan(float(frame.depth_m[0, 0]))

    # ── rep 1 UNIFORM_DISPARITY：归一化视差 × focalPixelPitch → 米 ──
    # 手算：focal_px = _estimate_pinhole(w, h)[0]；disparity_px = raw·focal；
    #       depth = focal·pitch / disparity_px。raw=0.5, pitch=0.001 时
    #       depth = focal·0.001/(0.5·focal) = 0.002m —— focal 约掉，只验量纲路径。
    disp = np.full((h, w), 0.5, dtype=np.float32)
    disp[0, 0] = 0.0                                  # disparity 0 → NaN
    monkeypatch = pytest.MonkeyPatch()
    patch_open([_FakeDepth(disp, {'representation_type': 1,
                                  'focalPixelPitch': 0.001})], monkeypatch)
    try:
        frame = _load_heic_depth(heic_path)
    finally:
        monkeypatch.undo()
    assert float(frame.depth_m[10, 20]) == pytest.approx(0.002)
    assert math.isnan(float(frame.depth_m[0, 0]))

    # ── rep 1 缺 focalPixelPitch → 人话 RuntimeError（不许猜量纲） ──
    monkeypatch = pytest.MonkeyPatch()
    patch_open([_FakeDepth(disp, {'representation_type': 1})], monkeypatch)
    try:
        with pytest.raises(RuntimeError, match='focalPixelPitch'):
            _load_heic_depth(heic_path)
    finally:
        monkeypatch.undo()

    # ── rep 3 NON_UNIFORM_DISPARITY → 人话 RuntimeError ──
    monkeypatch = pytest.MonkeyPatch()
    patch_open([_FakeDepth(disp, {'representation_type': 3})], monkeypatch)
    try:
        with pytest.raises(RuntimeError, match='非均匀视差'):
            _load_heic_depth(heic_path)
    finally:
        monkeypatch.undo()

    # ── 无人像辅图 → LookupError 包成 RuntimeError（绝不静默回落到 RGB 主图） ──
    monkeypatch = pytest.MonkeyPatch()
    patch_open([], monkeypatch)
    try:
        with pytest.raises(RuntimeError, match='没有人像深度辅图'):
            _load_heic_depth(heic_path)
    finally:
        monkeypatch.undo()

    # ── 辅图形状非二维 → 人话 RuntimeError（不外泄裸栈、不静默回落 RGB 主图）。
    # 注：3D 数组在 to_pillow() 就被 PIL 挡下（TypeError），由外层 try 包成
    # 「提取辅助深度图失败」——ndim 检查是 to_pillow 成功后仍非二维时的防御分支。
    monkeypatch = pytest.MonkeyPatch()
    patch_open([_FakeDepth(np.zeros((h, w, 3), np.float32),
                           {'representation_type': 2})], monkeypatch)
    try:
        with pytest.raises(RuntimeError, match='(提取辅助深度图失败|期望二维)'):
            _load_heic_depth(heic_path)
    finally:
        monkeypatch.undo()

    # ── representation_type 缺省按 2（UNIFORM_Z）兜底 ──
    monkeypatch = pytest.MonkeyPatch()
    patch_open([_FakeDepth(z2, {})], monkeypatch)
    try:
        frame = _load_heic_depth(heic_path)
    finally:
        monkeypatch.undo()
    assert float(frame.depth_m[10, 20]) == pytest.approx(4.0)


# ── W3 健壮性增补（覆盖缺口：错误路径 / 可选格式的运行期失败） ─────────────────

def test_tif32_mode_i_reads_and_rejects_out_of_range(tmp_path: Path) -> None:
    """32-bit 单通道 TIFF（Pillow mode ``I``）：量程内照读、越界报错。

    ⚠ PNG 的 32-bit 单通道存盘会被 Pillow 归一回 ``I;16``（实测），走不到
    ``_u16_from_pil`` 的 mode=='I' 分支；TIFF 才保留 mode ``I``。越界值
    （>65535）不是深度图——silently clamp 成 65535 会把 70m 外的点算成
    65.5m，必须显式拒。
    """
    image = Image.fromarray(np.array([[0, 1500], [2500, 500]], dtype=np.int32), mode='I')
    assert image.mode == 'I'
    image.save(tmp_path / 'i32.tif', format='TIFF')
    frame = load_depth(tmp_path / 'i32.tif')
    assert np.isnan(frame.depth_m[0, 0])
    assert frame.depth_m[0, 1] == pytest.approx(1.5)
    assert frame.depth_m[1, 0] == pytest.approx(2.5)

    big = Image.fromarray(np.array([[0, 70000], [2500, 500]], dtype=np.int32), mode='I')
    big.save(tmp_path / 'big32.tif', format='TIFF')
    with pytest.raises(ValueError, match='超出 uint16 量程'):
        load_depth(tmp_path / 'big32.tif')


def test_npy_npz_container_rejected(tmp_path: Path) -> None:
    """.npy 后缀里装着 npz（多数组）→ 人话 ValueError（不当成裸数组硬读）。"""
    with open(tmp_path / 'multi.npy', 'wb') as fh:
        np.savez(fh, a=np.full((8, 8), 2.0, np.float32))
    with pytest.raises(ValueError, match='单份深度数组'):
        load_depth(tmp_path / 'multi.npy')


def test_load_depth_unreadable_file_raises_value_error(tmp_path: Path) -> None:
    """未知后缀 + 文件读不了（不存在/权限）→ ValueError 带原因（不泄裸栈）。"""
    missing = tmp_path / 'nope.xyz'
    with pytest.raises(ValueError, match='无法读取深度文件'):
        load_depth(missing)
    denied = tmp_path / 'denied.xyz'
    denied.write_bytes(b'ARCD')
    denied.chmod(0o000)
    try:
        with pytest.raises(ValueError, match='无法读取深度文件'):
            load_depth(denied)
    finally:
        denied.chmod(0o644)


@pytest.mark.parametrize('fx,cx,match', [
    (0.0, 32.0, '内参 fx'),          # 零焦距：反投影全塌缩到光心
    (50.0, float('inf'), '内参 cx'),  # 主点非有限：投影坐标全 NaN
])
def test_arcd_rejects_non_finite_or_non_positive_intrinsics(
        tmp_path: Path, fx: float, cx: float, match: str) -> None:
    """.depth 头里的内参不合法 → ValueError（``_make_frame`` 收口，四种格式共用）。"""
    raw = (ARCD_HEADER.pack(ARCD_MAGIC, 8, 8, fx, 50.0, cx, 4.0,
                            float('nan'), float('nan'), float('nan'))
           + np.full((8, 8), 2.0, np.float32).tobytes())
    (tmp_path / 'bad.depth').write_bytes(raw)
    with pytest.raises(ValueError, match=match):
        load_depth(tmp_path / 'bad.depth')


def test_align_to_canvas_rejects_non_positive_canvas(tmp_path: Path) -> None:
    """画布尺寸 0/负 → ValueError（调用方输入问题，不是静默出空图）。"""
    from arcam.depth import align_to_canvas

    frame = load_depth(_write_arcd(tmp_path))
    for size in ((0, 10), (10, 0), (-4, 10)):
        with pytest.raises(ValueError, match='画布尺寸非法'):
            align_to_canvas(frame, *size)


def test_save_depth_rejects_non_2d(tmp_path: Path) -> None:
    """非二维深度不许落盘（.depth 布局按 (H,W) 行主序，silently reshape 会写坏帧）。"""
    from arcam.depth import save_depth

    bad = LiDARFrame(depth_m=np.zeros((4, 4, 3), np.float32),
                     fx=50.0, fy=50.0, cx=2.0, cy=2.0)
    with pytest.raises(ValueError, match='无法保存的深度尺寸'):
        save_depth(bad, tmp_path / 'bad.depth')


def test_exr_read_failure_wraps_runtime_error(tmp_path: Path) -> None:
    """装着的 OpenEXR 打不开的坏文件 → RuntimeError 带上下文（不泄裸栈）。

    webapp 的 ``_decode_depth_upload`` 靠这条走 400「深度帧读取失败」分支。
    """
    pytest.importorskip('OpenEXR')
    (tmp_path / 'broken.exr').write_bytes(b'not an exr at all')
    with pytest.raises(RuntimeError, match='从 EXR 读取深度通道失败'):
        load_depth(tmp_path / 'broken.exr')


class _FakeAuxDepth:
    """pillow-heif HeifDepthImage 的字段级同构（``to_pillow`` 可返回任意对象）。"""

    def __init__(self, pil, metadata: dict):
        self.info = {'metadata': metadata}
        self._pil = pil

    def to_pillow(self):
        return self._pil


class _FakeHeifFile:
    """Image.open 的返回同构：``.info['depth_images']`` + 上下文管理协议。"""

    def __init__(self, depths):
        self.info = {'depth_images': depths}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_heif_open(depths, monkeypatch: pytest.MonkeyPatch, suffix='.heic') -> None:
    real_open = Image.open

    def fake_open(path, *a, **kw):
        if str(path).endswith(('.heic', '.heif')):
            return _FakeHeifFile(depths)
        return real_open(path, *a, **kw)

    monkeypatch.setattr(Image, 'open', fake_open)


@pytest.fixture()
def heic_container(tmp_path: Path) -> Path:
    """真 HEIC 容器（libheif 编码的 RGB 主图），辅图由 fake 注入。"""
    pytest.importorskip('pillow_heif')
    import pillow_heif
    pillow_heif.register_heif_opener()
    rgb = np.zeros((32, 48, 3), np.uint8)
    rgb[..., 0] = 180
    path = tmp_path / 'portrait.heic'
    Image.fromarray(rgb, 'RGB').save(path, format='HEIF')
    assert path.is_file()
    return path


def test_heic_aux_depth_non_2d_array_runtime_error(heic_container: Path,
                                                   monkeypatch) -> None:
    """辅图 ``to_pillow`` 返回非二维数组 → RuntimeError（绝不静默回落 RGB 主图）。"""
    from arcam.depth import _load_heic_depth

    _patch_heif_open([_FakeAuxDepth(np.zeros((32, 48, 3), np.float32),
                                    {'representation_type': 2})], monkeypatch)
    with pytest.raises(RuntimeError, match='期望二维'):
        _load_heic_depth(heic_container)


def test_heic_aux_depth_over_120m_runtime_error(heic_container: Path,
                                                monkeypatch) -> None:
    """换算后超出米制量程 (0, 120] → RuntimeError（疑似标定差异，不猜）。"""
    from arcam.depth import _load_heic_depth

    far = np.full((32, 48), 200.0, np.float32)          # 200m：超出量程
    _patch_heif_open([_FakeAuxDepth(far, {'representation_type': 2})], monkeypatch)
    with pytest.raises(RuntimeError, match='米制量程'):
        _load_heic_depth(heic_container)


def _write_arcd(tmp_path: Path) -> Path:
    """写一份最小合法 .depth（8x8、全 2m），供对齐/尺寸守卫类用例复用。"""
    path = tmp_path / 'mini.depth'
    save_depth(LiDARFrame(depth_m=np.full((8, 8), 2.0, np.float32),
                          fx=50.0, fy=50.0, cx=4.0, cy=4.0), path)
    return path
