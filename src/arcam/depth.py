"""LiDAR 深度 IO —— iPhone 激光雷达深度图的读取、对齐与落盘（arcam 接入层 L1）。

约定出处 ``docs/LIDAR-SPEC.md``（本模块无原版 JS 对应实现，是 LiDAR 接入的新约）：

- 深度单位**米**、float32；无效值 = 0 或 NaN，读入统一 NaN（§1）；
- 深度相机坐标：**光学中心、z 前向、v 向下**，
  ``p = ((u-cx)/fx · z, (v-cy)/fy · z, z)``（§1）；
- **深度值 = 相机平面轴向 z（Apple 语义）**：ARKit/AVFoundation 官方口径
  "distance from the plane of the camera in meters"，官方点云样例反投影即
  ``point(u,v,1) · depth``（裁决记录见 plane.backproject docstring）；
- arcam 数学坐标（y 上、z 前向，见 docs/SPEC.md §1.5 与 artifact A §1.3）与深度
  相机坐标只差一个 **y 翻转**：:func:`_to_arcam_camera_coords` 是全仓唯一实现，
  其它模块（plane / compose …）一律 import，不得各自推导（§1）。

格式分派（§2 优先级）：``.png/.tif/.tiff`` 16-bit（**毫米**，uint16，0=无效）→
``.npy``（米，内参按针孔满幅 FOV≈60° **估计非实测**）→ ``.depth``（magic
``ARCD`` 小头）→ ``.heic``（人像辅助深度，可选，pillow-heif 懒加载，
representation_type 判别 + disparity 换算）→ ``.exr``（OpenEXR 浮点 Z，可选）。
所有读入路径共同门槛：尺寸 > 0 且有效比例 > 2%，否则
``ValueError('没有有效深度')``；uint16→米的**单位换算只发生在 16-bit 图像路径**。
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

ARCD_MAGIC = b'ARCD'
# [magic 4B][W u32][H u32][fx f32][fy f32][cx f32][cy f32][roll f32][pitch f32]
# [height f32][depth float32 H*W]，一律小头；roll/pitch/height 用 NaN 表示「无」。
ARCD_HEADER = struct.Struct('<4sIIfffffff')
ARCD_HEADER_SIZE = ARCD_HEADER.size                      # 40 B
DEPTH_NAN = float('nan')                                 # .depth 的「无外参」标记
MIN_VALID_RATIO = 0.02                                   # LIDAR-SPEC §2 有效比例门限
NPY_HALF_FOV_DEG = 30.0                                  # 满幅 FOV≈60° → 半角 30°（估计）
U16_MODES = ('I;16', 'I;16B', 'I;16L', 'I;16N', 'I')     # Pillow 16-bit 单通道模式
MM_PER_M = 1000.0                                        # 毫米→米（换算只此一处）


@dataclass(frozen=True)
class LiDARFrame:
    """一帧 LiDAR 深度 + 深度相机内参（LIDAR-SPEC §2，字段与默认值冻结）。

    - ``depth_m``：``(H, W)`` float32、米，无效 = NaN（0/负读入即 NaN，§1）；
    - ``fx/fy/cx/cy``：深度相机针孔内参（像素），§1 反投影公式里用；
    - ``roll_deg/pitch_deg/height_m``：ARKit 横滚 / 俯仰 / 已知相机高，None = 未提供
      （真机世界原点在地面时才会有 height_m，§0 E1）；
    - ``source``：来源描述（文件名 / URL / stream）。

    ⚠ frozen dataclass 的字段含 ndarray，故 ``==`` 比较两帧会触发 ndarray 的真值
    歧义 ValueError（dataclass 生成的 ``__eq__`` 逐字段比元组）。这是 SPEC §2 冻结
    签名的直接后果，不做「顺手修」：测试/上层请逐字段比较，或
    ``dataclasses.replace`` 派生新帧。
    """

    depth_m: np.ndarray
    fx: float
    fy: float
    cx: float
    cy: float
    roll_deg: float | None = None
    pitch_deg: float | None = None
    height_m: float | None = None
    source: str = ''


def _to_arcam_camera_coords(points: np.ndarray) -> np.ndarray:
    """深度相机坐标 → arcam 相机坐标（**只翻 y**，全仓唯一实现，LIDAR-SPEC §1）。

    入参 ``(N, 3)`` 深度相机坐标（光学中心、z 前向、**y 向下**，§1）；返回
    ``(N, 3)`` arcam 相机坐标（**y 向上**、z 前向，docs/SPEC.md §1.5）：x、z 不变，
    ``y_arcam = -y_depth``。两个坐标系的 z 轴同向、x 轴同向，只差 y 符号，
    所以这里是全仓唯一的翻转点——plane.py 拟合平面、compose.py 解环境量都 import
    本函数，任何模块不得各自推导。

    返回新数组（不改入参），float64（SPEC §1.2 计算口径）。
    """
    pts = np.asarray(points)
    if pts.ndim != 2 or pts.shape[1] != 3:
        raise ValueError(f'期望 (N,3) 深度相机坐标，得到形状 {pts.shape}')
    out = np.asarray(pts, dtype=np.float64).copy()
    out[:, 1] = -out[:, 1]
    return out


def load_depth(path: str | Path) -> LiDARFrame:
    """读取深度文件 → :class:`LiDARFrame`（LIDAR-SPEC §2 格式分派）。

    按扩展名（大小写不敏感）选解析器，优先级即 §2 列序：

    1. ``.png/.tif/.tiff`` 16-bit 单通道（mode ``I;16`` 系）：**毫米** uint16，
       0 = 无效 → 读入换算为米（换算只此一处）；内参按针孔满幅 FOV≈60° 估计
       （**估计非实测**，这些格式不带内参元数据）；
    2. ``.npy`` float32 ``(H, W)`` **米**；内参同样按 FOV≈60° **估计非实测**
       （规范只给出估计公式，没有实测元数据可读）；
    3. ``.depth`` magic ``ARCD`` 小头二进制，内参/外参齐备（实时流与模拟器落盘，
       :func:`save_depth` 的逆）；
    4. ``.heic`` 人像辅助深度（可选）：pillow-heif 懒加载，representation_type
       判别 + disparity→depth 换算，未装/不可转均抛带指引的 RuntimeError，
       绝不静默回落到 HEIC 主图；
    5. ``.exr`` OpenEXR 浮点深度（可选）：读 ``Z``（或首个）通道为 float32 米，
       行序假定顶行优先（假设项，见 :func:`_load_exr`）。

    共同收口（各加载器内部一致）：非有限 / 非正值 → NaN；尺寸必须 > 0；**有效比例
    须 > 2%**。值域 (0, 120]（§1）只作文档口径，读取不做上限裁剪——超出该量程的
    陈旧像素由 plane/遮挡层按需过滤，IO 层不悄悄丢数据。

    未知扩展名只做 ``ARCD`` magic 嗅探（服务端落盘可能无后缀），其余报错。
    嗅探只读文件头 4 字节（上传路径对未知后缀文件会走到这里，正文可能到
    64MB，不为 4 字节签名把整个文件吞进内存）。
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in ('.png', '.tif', '.tiff'):
        return _load_u16_image(p)
    if suffix == '.npy':
        return _load_npy_depth(p)
    if suffix == '.depth':
        return _load_arcd(p)
    if suffix in ('.heic', '.heif'):
        return _load_heic_depth(p)
    if suffix == '.exr':
        return _load_exr(p)
    try:
        with p.open('rb') as fh:
            head = fh.read(len(ARCD_MAGIC))
    except OSError as exc:
        raise ValueError(f'无法读取深度文件：{p}（{exc}）') from exc
    if head == ARCD_MAGIC:
        return _load_arcd(p)
    raise ValueError(
        f'无法识别深度文件格式：{p}（后缀 {p.suffix!r}）。'
        ' 支持：.png/.tif/.tiff（16-bit）、.npy、.depth（magic ARCD）、'
        '.heic、.exr'
    )


def align_to_canvas(frame: LiDARFrame, width: int, height: int) -> LiDARFrame:
    """深度图 → 画布尺寸（LIDAR-SPEC §2 对齐约定）。

    深度图分辨率通常低于照片（如 256×192 vs 3024×4032）。约定深度与照片「同一
    运营商视场」（Apple 人像深度即照片视场的子采样），故只做几何缩放：

    - **最近邻**取值：画布像素 ``(u, v)`` 采源图像素
      ``(floor((v+.5)·H/h), floor((u+.5)·W/w))``（输出像素中心投回源图，夹到
      边界内；与 OpenCV ``INTER_NEAREST_EXACT`` 同约定）。不做插值——插值会把
      障碍物边缘糊进背景，深度值之间不可平均。
    - ``fx/fy/cx/cy`` **同比缩放**：``fx·(w/W)``、``fy·(h/H)``、``cx``/``cy`` 同理
      （同一世界点在源/画布上的投影坐标线性相关，内参随之线性变换）。
    - ``roll/pitch/height`` 与 ``source`` 原样传递（缩放不改变相机外参语义）。

    NaN 传播策略：**先对源图有效掩码做同一最近邻映射，再按掩码取值**——目标像素
    的源像素无效则该目标像素为 NaN。两个方向的不变量：NaN 永远不会被邻域有效值
    「擦」成有效值（空洞不缩小），有效值也不会被 NaN 染脏（空洞不扩大）。掩码与
    取值共用同一套下标，故与「直接最近邻缩放原始数组」逐位一致，写成显式两步是为
    把该不变量钉死、并对将来换成任何滤波器都留出守卫点。最近邻只会让有效比例在
    边界附近轻微变化，:func:`load_depth` 的 2% 门禁只在读入时执行，此处不复查。

    有效比例断言失败（源帧本身不合法）属调用方输入问题；``width/height`` 非正 →
    ValueError。
    """
    if width < 1 or height < 1:
        raise ValueError(f'画布尺寸非法：{width}x{height}')
    src_h, src_w = int(frame.depth_m.shape[0]), int(frame.depth_m.shape[1])
    if src_h < 1 or src_w < 1:
        raise ValueError(f'源深度尺寸非法：{src_h}x{src_w}')
    rows = _nearest_indices(src_h, height)
    cols = _nearest_indices(src_w, width)
    valid = np.isfinite(frame.depth_m)
    valid_rs = valid[np.ix_(rows, cols)]                  # 先缩放有效掩码
    values_rs = frame.depth_m[np.ix_(rows, cols)]         # 再取值
    aligned = np.where(valid_rs, values_rs, np.nan).astype(np.float32)
    scale_x = width / float(src_w)
    scale_y = height / float(src_h)
    return LiDARFrame(
        depth_m=aligned,
        fx=frame.fx * scale_x,
        fy=frame.fy * scale_y,
        cx=frame.cx * scale_x,
        cy=frame.cy * scale_y,
        roll_deg=frame.roll_deg,
        pitch_deg=frame.pitch_deg,
        height_m=frame.height_m,
        source=frame.source,
    )


def save_depth(frame: LiDARFrame, path: str | Path) -> None:
    """:class:`LiDARFrame` → ``.depth``（magic ``ARCD`` 小头，LIDAR-SPEC §2）。

    实时流接收端与 L4 模拟器的落盘格式（:func:`load_depth` 的逆），布局见
    :data:`ARCD_HEADER`：40 B 头 + ``H·W·4`` B 深度数据（C 序，行主序，米）。
    头里 ``roll/pitch/height`` 为 None 时写 NaN（= 「无」标记）；深度数据里的
    NaN 一律写 0（该格式的无效标记、也是 PNG 路径的既有约定，读回时统一为 NaN，
    故往返语义无损）。内参/深度都用 float32 小头，跨平台字节序一致。
    """
    src = np.asarray(frame.depth_m)
    if src.ndim != 2 or src.shape[0] < 1 or src.shape[1] < 1:
        raise ValueError(f'无法保存的深度尺寸：{src.shape}')
    height, width = int(src.shape[0]), int(src.shape[1])
    body = np.array(src, dtype=np.float32, copy=True)
    body[~np.isfinite(body)] = 0.0
    blob = ARCD_HEADER.pack(
        ARCD_MAGIC, width, height,
        float(frame.fx), float(frame.fy), float(frame.cx), float(frame.cy),
        _nan_if_none(frame.roll_deg), _nan_if_none(frame.pitch_deg),
        _nan_if_none(frame.height_m),
    )
    Path(path).write_bytes(blob + body.astype('<f4', copy=False).tobytes())


def _nearest_indices(src: int, dst: int) -> np.ndarray:
    """最近邻下标映射：输出第 i 像素 ← 源 ``floor((i+.5)·src/dst)``（夹边界）。"""
    idx = np.floor((np.arange(dst, dtype=np.float64) + 0.5) * (src / dst)).astype(np.int64)
    return np.clip(idx, 0, src - 1)


def _make_frame(depth: np.ndarray, fx: float, fy: float, cx: float, cy: float,
                roll_deg: float | None, pitch_deg: float | None,
                height_m: float | None, source: str) -> LiDARFrame:
    """各加载器统一收口：无效值归一化 → 尺寸/有效比例断言 → :class:`LiDARFrame`。

    「尺寸 > 0 且有效比例 > 2%」是 LIDAR-SPEC §2 对**所有**格式的读入门槛，放在
    一处实现是为了让报错口径一致（文案带来源，便于区分是哪份文件没数据）。
    """
    arr = np.asarray(depth)
    if arr.ndim != 2 or int(arr.shape[0]) < 1 or int(arr.shape[1]) < 1:
        raise ValueError(f'没有有效深度：尺寸非法 {arr.shape}（{source}）')
    values = arr.astype(np.float64)
    valid = np.isfinite(values) & (values > 0.0)
    ratio = float(valid.mean()) if valid.size else 0.0
    if ratio <= MIN_VALID_RATIO:
        raise ValueError(
            f'没有有效深度：有效比例 {ratio:.2%} 不超过 {MIN_VALID_RATIO:.0%}'
            f'（{source}）'
        )
    depth_m = np.where(valid, values, np.nan).astype(np.float32)
    for name, value in (('fx', fx), ('fy', fy)):
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f'{source}：内参 {name}={value} 非有限正数')
    for name, value in (('cx', cx), ('cy', cy)):
        if not math.isfinite(value):
            raise ValueError(f'{source}：内参 {name}={value} 非有限数')
    return LiDARFrame(depth_m=depth_m, fx=float(fx), fy=float(fy), cx=float(cx),
                      cy=float(cy), roll_deg=roll_deg, pitch_deg=pitch_deg,
                      height_m=height_m, source=source)


def _estimate_pinhole(width: int, height: int) -> tuple[float, float, float, float]:
    """针孔满幅 FOV≈60° 的 (fx, fy, cx, cy) 估计（LIDAR-SPEC §2，**估计非实测**）。

    规范写死 ``fx = W/(2·tan(30°))``、``cx=W/2``、``cy=H/2``；``fy`` 规范未单列，
    此处取 ``fy = fx``（方形像素假设，竖 FOV 由横 FOV 加该假设导出）。用于不带
    内参元数据的格式（16-bit 图像 / .npy / .heic）。若后续要竖 FOV 60° 语义，
    只需替换本函数一处。
    """
    half = math.tan(math.radians(NPY_HALF_FOV_DEG))
    fx = width / (2.0 * half)
    return fx, fx, width / 2.0, height / 2.0


def _u16_from_pil(image: Image.Image, source: str) -> np.ndarray:
    """Pillow 16-bit 单通道图像 → uint16 数组。

    ⚠ 不走 ``image.convert('I;16')``：实测 Pillow 12 对 ``I;16B`` → ``I;16`` 会
    把值截到 8bit（1000 → 255）。改为按 mode 分别取 ``np.asarray`` 后转 uint16
    （numpy 自带字节序处理，``I;16B`` 的 ``>u2`` 能正确转成原生序）。
    """
    mode = image.mode
    if mode not in U16_MODES:
        raise ValueError(
            f'{source}：期望 16-bit 单通道深度图（Pillow mode I;16），得到 mode={mode}。'
            ' 请确认扫描 App 导出的是 16-bit 灰度 PNG/TIF（不是 8-bit 位图，'
            '也不是 RGB 人像照片）'
        )
    if mode == 'I':
        wide = np.asarray(image, dtype=np.float64)
        if wide.size and (float(wide.min()) < 0.0 or float(wide.max()) > 65535.0):
            raise ValueError(
                f'{source}：32-bit 模式下的取值超出 uint16 量程，'
                f'范围 [{wide.min()}, {wide.max()}]，不是 16-bit 深度图'
            )
        return wide.astype(np.uint16)
    return np.asarray(image).astype(np.uint16)


def _load_u16_image(path: Path) -> LiDARFrame:
    """16-bit PNG/TIF（**毫米**，uint16，0=无效）→ LiDARFrame（LIDAR-SPEC §2 第 1 优先级）。

    3D 扫描 App 导出的通行约定：深度以**毫米**无符号整数量化，0 表示无效。本例是
    **全仓唯一的毫米→米换算点**（×1e-3），之后所有路径都以米为口径。内参按
    针孔满幅 FOV≈60° **估计非实测**（该格式不带内参元数据）。
    """
    with Image.open(path) as image:
        raw_mm = _u16_from_pil(image, str(path))
    height, width = int(raw_mm.shape[0]), int(raw_mm.shape[1])
    depth_m = raw_mm.astype(np.float64) / MM_PER_M
    fx, fy, cx, cy = _estimate_pinhole(width, height)
    return _make_frame(depth_m, fx, fy, cx, cy, None, None, None, str(path))


def _load_npy_depth(path: Path) -> LiDARFrame:
    """``.npy`` float32 ``(H, W)``（**米**）→ LiDARFrame（LIDAR-SPEC §2 第 2 优先级）。

    内参缺省按针孔满幅 FOV≈60° 估计：``fx = W/(2·tan(30°))``、``fy = fx``
    （方形像素假设）、``cx = W/2``、``cy = H/2``——**估计非实测**，规范未提供从
    npy 读实测内参的通道（npy 是裸数组，不带元数据；要带实测内参请用 ``.depth``）。
    """
    try:
        data = np.load(path, allow_pickle=False)
    except Exception as exc:                              # OSError/ValueError/EOFError
        raise ValueError(f'npy 读取失败：{path}（{type(exc).__name__}: {exc}）') from exc
    if not isinstance(data, np.ndarray):
        raise ValueError(
            f'期望 .npy 单份深度数组（.npz 多数组文件请另存为 .npy）：{path}'
        )
    if data.ndim != 2:
        raise ValueError(
            f'期望 (H, W) 二维深度数组，得到形状 {data.shape}（{path}）'
        )
    height, width = int(data.shape[0]), int(data.shape[1])
    fx, fy, cx, cy = _estimate_pinhole(width, height)
    return _make_frame(data, fx, fy, cx, cy, None, None, None, str(path))


def _load_arcd(path: Path) -> LiDARFrame:
    """``.depth``（magic ``ARCD`` 小头）→ LiDARFrame（LIDAR-SPEC §2 第 3 优先级）。

    布局 = :data:`ARCD_HEADER`：40 B 头（magic 4B、W/H u32、fx/fy/cx/cy/roll/
    pitch/height 各 f32）+ ``H·W`` float32 深度（米，C 序）。头里的外参
    roll/pitch/height 用 NaN 表示「无」，读成 ``None``；数据区 0/负读成 NaN。
    这是实时流接收端与模拟器的落盘格式（:func:`save_depth` 的逆）。
    """
    raw = Path(path).read_bytes()
    if len(raw) < ARCD_HEADER_SIZE:
        raise ValueError(
            f'{path}：.depth 文件长度 {len(raw)} B < 头部 {ARCD_HEADER_SIZE} B'
        )
    magic, width, height, fx, fy, cx, cy, roll, pitch, height_m = \
        ARCD_HEADER.unpack_from(raw, 0)
    if magic != ARCD_MAGIC:
        raise ValueError(
            f'{path}：.depth magic 应为 {ARCD_MAGIC!r}，得到 {magic!r}'
        )
    need = ARCD_HEADER_SIZE + int(width) * int(height) * 4
    if len(raw) < need:
        raise ValueError(
            f'{path}：.depth 数据截断，声明 {width}x{height} 需 {need} B，'
            f'实际 {len(raw)} B'
        )
    count = int(width) * int(height)
    depth = np.frombuffer(raw, dtype='<f4', count=count,
                          offset=ARCD_HEADER_SIZE).reshape(int(height), int(width))
    return _make_frame(depth, float(fx), float(fy), float(cx), float(cy),
                       _none_if_nan(roll), _none_if_nan(pitch), _none_if_nan(height_m),
                       str(path))


def _load_heic_depth(path: Path) -> LiDARFrame:
    """HEIC 人像辅助深度 → LiDARFrame（LIDAR-SPEC §2 第 4 优先级，**可选**）。

    pillow-heif **懒加载**（import 在函数内）：未安装 → RuntimeError 带安装指引；
    辅图定位失败 / 表示类型不可转换 → RuntimeError 带替代格式指引；**永不静默回落
    到 HEIC 主图**（那是 RGB 照片，当深度用会得到一堆废数）。

    表示类型按 pillow-heif 1.8+ 的 ``HeifDepthRepresentationType``
    （调研 ``oss-teardown/lidar-research.md`` §5，2026-10-03）：
    - ``UNIFORM_Z(2)``：深度即米（Apple 'fdep'），直接用；
    - ``UNIFORM_INVERSE_Z(0)``：1/z（1/m），z = 1/value；
    - ``UNIFORM_DISPARITY(1)``：归一化视差 → 用元数据 focal pixel pitch 换算成米，
      缺 pitch 时报错指引（量纲未经标定不可猜）；
    - ``NON_UNIFORM_DISPARITY(3)``：非均匀视差，无逐像素表不可转 → 报错指引。
    内参按辅助图尺寸 :func:`_estimate_pinhole` **估计**（估计非实测）；外设
    roll/pitch/height 取 None，交 §3 平面拟合解出。
    """
    try:
        import pillow_heif
    except ImportError as exc:
        raise RuntimeError(
            'HEIC 人像深度需要 pillow-heif（当前未安装）。\n'
            '  安装二选一：\n'
            '    1) .venv/bin/python -m pip install pillow-heif\n'
            '    2) 改用 scan 类 App 导出的 16-bit PNG/TIF / .npy / .depth'
        ) from exc
    pillow_heif.register_heif_opener()
    try:
        from PIL import Image
        with Image.open(str(path)) as im:
            depth_images = im.info.get('depth_images')
            if not depth_images:
                raise LookupError('照片内没有人像深度辅图')
            depth_im = depth_images[0]
            meta = dict(depth_im.info.get('metadata', {}))
            pil = depth_im.to_pillow()
    except Exception as exc:
        raise RuntimeError(
            f'从 HEIC 提取辅助深度图失败：{path}（{type(exc).__name__}: {exc}）。\n'
            '  HEIC 深度依赖 pillow-heif ≥1.8 与 Apple 辅图编码；若持续失败，'
            '请改用扫描 App 导出的 16-bit PNG/TIF 序列或 .depth 落盘帧'
        ) from exc

    arr = np.asarray(pil)
    if arr.ndim != 2:
        raise RuntimeError(
            f'{path}：辅助深度图形状 {arr.shape}，期望二维。请改用 16-bit PNG/TIF 或 .depth'
        )
    height, width = int(arr.shape[0]), int(arr.shape[1])
    raw = arr.astype(np.float64)
    rep = meta.get('representation_type', 2)
    # HeifDepthRepresentationType: 0=UNIFORM_INVERSE_Z 1=UNIFORM_DISPARITY
    #                               2=UNIFORM_Z 3=NON_UNIFORM_DISPARITY
    if rep == 2:
        depth = raw                                   # 米（Apple 'fdep'）
    elif rep == 0:
        with np.errstate(divide='ignore', invalid='ignore'):
            depth = np.where(raw > 0.0, 1.0 / raw, np.nan)   # 1/m → m
    elif rep == 1:
        pitch = meta.get('focalPixelPitch')
        if not isinstance(pitch, (int, float)) or pitch <= 0.0:
            raise RuntimeError(
                f'{path}：人像深度是归一化视差（UNIFORM_DISPARITY）但元数据没有 '
                'focalPixelPitch，无法安全换算成米。请改用扫描 App 导出的 '
                '16-bit PNG/TIF 或 .depth'
            )
        focal_px = _estimate_pinhole(width, height)[0]
        with np.errstate(divide='ignore', invalid='ignore'):
            disparity_px = raw * focal_px             # 归一化视差 × 焦距(px)
            depth = np.where(disparity_px > 0.0,
                             (focal_px * float(pitch)) / disparity_px, np.nan)
    else:
        raise RuntimeError(
            f'{path}：人像深度是非均匀视差（NON_UNIFORM_DISPARITY），缺少逐像素'
            '换算表，无法安全转成米。请改用扫描 App 导出的 16-bit PNG/TIF 或 .depth'
        )
    # §1 无效口径先归一（0/负/非有限 → NaN）再打量程：真机人像 HEIC 的无效像素
    # 大量编码为 0，不先归一会把 min 打成 0，撞上下面「疑似标定差异」的误导报错
    # （_make_frame 的收口在预检之后，救不回来）。归一后各 rep 分支与 PNG16/EXR
    # 路径的无效值口径一致，且量程下界 (0, …] 已由该 where 保证 —— 只剩上界
    # 120m 需要查（曾多查一个恒为假的下界条件，删掉）。
    depth = np.where(np.isfinite(depth) & (depth > 0.0), depth, np.nan)
    finite = depth[np.isfinite(depth)]
    if finite.size and float(finite.max()) > 120.0:
        raise RuntimeError(
            f'{path}：辅助深度换算后取值 [{finite.min():.4g}, {finite.max():.4g}] '
            '不在米制量程 (0, 120]，疑似标定差异。请改用 16-bit PNG/TIF 或 .depth'
        )
    fx, fy, cx, cy = _estimate_pinhole(width, height)
    return _make_frame(depth, fx, fy, cx, cy, None, None, None, str(path))


def _load_exr(path: Path) -> LiDARFrame:
    """OpenEXR 浮点深度 → LiDARFrame（LIDAR-SPEC §2 第 5 优先级，**可选**）。

    调研 must-support（``oss-teardown/lidar-research.md`` §5/§7，2026-10-03）：
    OpenEXR 3.5.1 有 macOS ARM64 wheel。读 ``Z`` 通道（无则第一个 HALF/FLOAT/UINT
    通道）为 float32 **米**（Apple 语义：相机平面轴向 z，见 LIDAR-SPEC §1）；
    行序假定为 OpenEXR 惯用的**顶行优先**（readPixels 自 dataWindow 顶部起）——
    这是本函数的假设项，装 extra 后首次使用请在真机 EXR 上核对。内参同
    :func:`_estimate_pinhole` 估计（估计非实测）；外设 roll/pitch/height 取 None。
    """
    try:
        import OpenEXR
    except ImportError as exc:
        raise RuntimeError(
            'EXR 深度需要 OpenEXR 绑定（当前未安装）。\n'
            '  安装：.venv/bin/python -m pip install "arcam[lidar]"'
            '（或 pip install OpenEXR Imath）\n'
            '  或改用 16-bit PNG/TIF / .npy / .depth'
        ) from exc
    try:
        import Imath
        header = OpenEXR.InputFile(str(path)).header()
        channels = header.get('channels', {})
        name = 'Z' if 'Z' in channels else next(iter(channels), None)
        if name is None:
            raise LookupError('EXR 没有任何通道')
        data_window = header['dataWindow']
        width = int(data_window.max.x - data_window.min.x + 1)
        height = int(data_window.max.y - data_window.min.y + 1)
        exr = OpenEXR.InputFile(str(path))
        raw = exr.channel(name, Imath.PixelType(Imath.PixelType.FLOAT))
        depth = np.frombuffer(raw, dtype=np.float32).reshape(height, width).astype(np.float64)
    except Exception as exc:
        raise RuntimeError(
            f'从 EXR 读取深度通道失败：{path}（{type(exc).__name__}: {exc}）'
        ) from exc
    depth = np.where(np.isfinite(depth) & (depth > 0.0), depth, np.nan)
    fx, fy, cx, cy = _estimate_pinhole(width, height)
    return _make_frame(depth, fx, fy, cx, cy, None, None, None, str(path))


def _none_if_nan(value: float) -> float | None:
    """.depth 头的 NaN 外参 → None；头里的 roll/pitch/height 允许为 NaN = 「无」。"""
    return None if value is None or math.isnan(value) else float(value)


def _nan_if_none(value: float | None) -> float:
    """落盘双向映射：None → NaN（LIDAR-SPEC §2 用 NaN 表示「无外参」）。"""
    return DEPTH_NAN if value is None else float(value)
