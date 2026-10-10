"""环境光采样与立绘烘光 —— 原版「光线匹配」（index.html:1161-1465）的离线等价。

三条主线，全部逐字对应原版：
  · ``measure_region``（index.html:1212-1224）：区域 → N×N 降采样 → 三通道均值 +
    32 级亮度分布。降采样用**面积平均盒式滤波**（原版 ``drawImage`` 的滤波核未指定，
    见「已知偏离」）；采样框按 ``clampRegion``（index.html:1244-1251）的 sx/sy 公式
    钳制，源外区域按 canvas 规范的**透明黑零填充**（不 clamp 回可见区）。
  · ``char_lum``（index.html:1263-1283）：立绘自身统计（alpha<8 跳过）。
  · ``bake_lit``（index.html:1285-1447）：gamma 明暗曲线 + R/B 白平衡（高光限幅）+
    饱和度 + 固定种子颗粒噪声，三条 256 项 LUT 的边界取整一律 ``js_round``，
    最终落字节用 ``Uint8ClampedArray`` 的 round-half-even 语义。

已知偏离（SPEC §5，如实保留、不许顺手"修"）：
  · ``drawImage`` 降采样滤波原版未指定（canvas 默认 'low'）→ 离线用面积平均，
    纯色/整数对齐区域下与 nearest 等价。
  · 原版 ``imageSmoothingQuality``/开关（LIGHT_BRI/TMP/SAT/GRN/SFT）离线全部取默认值
    （BRI/TMP/SAT/GRN 开、SFT 关），冻结签名里没有开关参数。
  · ``lightRegion`` 返回**画布坐标**而 ``sampleLight`` 按**视频尺寸** clamp
    （index.html:1195）：离线两者同尺寸，调用方须自行保证入参 region 与 rgb01 同坐标系。
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass

import numpy as np

from arcam.numeric import Rect, clampn, js_round

# ── 光照常量（index.html:1148-1158）────────────────────────────────────
GRN_MAX = 6.5          # 颗粒幅度上限（255 里的 6.5）
GRN_MIN = 0.7          # 颗粒幅度下限
GRN_COMP_MAX = 3.2     # 缩放补偿上限
LIGHT_NEUTRAL = 0.42   # 「中性环境」亮度
LIGHT_GAIN_MAX = 1.15  # 明暗增益上限
LIGHT_GAIN_MIN = 0.6   # 明暗增益下限
WB_K = 0.75            # 白平衡对数空间推进比例
GMIN = 0.85            # 高光白平衡限幅下限
GMAX = 1.18            # 高光白平衡限幅上限
SAT_K = 0.3            # 饱和度匹配强度

# Rec.709 亮度权重（measureRegion / charLum 共用，index.html:1273）
_LUM_W = (0.2126, 0.7152, 0.0722)


@dataclass(frozen=True)
class LightSample:
    """一次环境光采样的结果（对应原版 LIGHT 的四个 EMA 字段的离线快照）。

    ``levels``：逐级亮度分布 —— 区域降采样成 N×N 后，按 Rec.709 亮度量化成
    ``levels`` 个桶的直方图（归一化占比，非计数）。原版只保留全局均值，
    分布是离线派生量，供 :func:`light_levels` 取对比度。
    ``mean``：N×N 网格上的 RGB 均值（0~1，对应原版 measureRegion 的 r/g/b）。
    派生量：亮度 = Rec.709(mean)，饱和度 = HSV-S(mean)。
    """

    levels: tuple[float, ...]
    mean: tuple[float, float, float]


# ── 盒式面积加权降采样（drawImage 的离线等价）──────────────────────────
def _axis_weighted_sum(flat: np.ndarray, edges: np.ndarray) -> np.ndarray:
    """沿 axis 0 做盒式面积加权求和：flat (n_src, *rest) → (n_dst, *rest)。

    目的格 k 覆盖源区间 ``[edges[k], edges[k+1])``，源外部分计 0
    （canvas drawImage 的透明黑零填充语义，不 clamp 回可见区）。
    """
    n_src = flat.shape[0]
    rest = flat.shape[1:]
    a = edges[:-1]
    b = edges[1:]
    n_dst = a.shape[0]
    l0 = np.floor(a).astype(np.int64)
    l1 = np.floor(b).astype(np.int64)
    wl = (l0 + 1) - a
    wr = b - l1
    pre = np.zeros((n_src + 1,) + rest, dtype=np.float64)
    np.cumsum(flat, axis=0, out=pre[1:])
    out = pre[np.clip(l1, 0, n_src)] - pre[np.clip(l0 + 1, 0, n_src)]
    shape_w = (n_dst,) + (1,) * len(rest)
    ok0 = ((l0 >= 0) & (l0 < n_src)).reshape(shape_w)
    ok1 = ((l1 >= 0) & (l1 < n_src)).reshape(shape_w)
    out = out + np.where(ok0, flat[np.clip(l0, 0, n_src - 1)] * wl.reshape(shape_w), 0.0)
    out = out + np.where(ok1, flat[np.clip(l1, 0, n_src - 1)] * wr.reshape(shape_w), 0.0)
    return out


def _box_resample(src: np.ndarray, x0: float, y0: float, w: float, h: float,
                  n_x: int, n_y: int) -> np.ndarray:
    """把源矩形 ``(x0, y0, w, h)`` 面积平均缩到 ``(n_y, n_x)`` 网格。

    对应 ``g.drawImage(源, sx, sy, sw, sh, 0, 0, N, N)``（index.html:1216）。
    通道各自独立重采样（非预乘）；源外区域按透明黑计入分母（零填充）。
    """
    h_px, w_px = src.shape[:2]
    n_c = src.shape[2] if src.ndim == 3 else 1
    if n_x <= 0 or n_y <= 0 or w <= 0 or h <= 0 or w_px <= 0 or h_px <= 0:
        return np.zeros((n_y, n_x, n_c), dtype=np.float64)
    flat = src.reshape(h_px, w_px, n_c).astype(np.float64, copy=False)
    xe = x0 + w * np.arange(n_x + 1, dtype=np.float64) / n_x
    ye = y0 + h * np.arange(n_y + 1, dtype=np.float64) / n_y
    rows = _axis_weighted_sum(flat, ye)                      # (n_y, W, C)
    cols = _axis_weighted_sum(np.ascontiguousarray(rows.transpose(1, 0, 2)), xe)
    grid = cols.transpose(1, 0, 2)
    grid /= np.diff(ye)[:, None, None] * np.diff(xe)[None, :, None]
    return grid


def _to_float01(img: np.ndarray, channels: int) -> np.ndarray:
    """uint8 → float64 [0,1]；float64 原样（clip 到 [0,1]）。

    **总是返回新数组**（走一次 ``copy=True`` + 原地 clip，比
    ``astype`` 再 ``clip`` 少一整幅 float64 临时缓冲）。
    """
    arr = np.asarray(img)
    if arr.dtype == np.uint8:
        if arr.ndim != 3 or arr.shape[2] != channels:
            raise ValueError(f"expected (H,W,{channels}) image, got {arr.shape}")
        out = arr.astype(np.float64)
        out /= 255.0
        return out
    if arr.ndim != 3 or arr.shape[2] != channels:
        raise ValueError(f"expected (H,W,{channels}) image, got {arr.shape}")
    out = np.array(arr, dtype=np.float64, copy=True)
    np.clip(out, 0.0, 1.0, out=out)
    return out


# ── 采样 ───────────────────────────────────────────────────────────────
def _clamp_region(region: Rect | None, width: int,
                  height: int) -> tuple[float, float, float, float]:
    """采样框钳制 —— ``clampRegion``（index.html:1244-1251）的 sx/sy 公式。

    ⚠ ``sw>W`` 越界**零填充**语义如实保留（artifact A §2.2.7 / §5.2、SPEC §5）：
      原版的 ``sw = Math.min(sw, W)`` 预夹会让 ``sw>W`` 永不可能发生、 zero-fill
      成为死代码；按契约这里**不预夹 sw/sh**，只套 JS 的 ``sx = max(0,
      min(W-sw, sx))``：``sw>W`` 时 ``W-sw<0`` → sx 被夹到 0 而 sw 原样保留，
      窗口越过图像右/下边界，越界段在降采样里按 canvas ``drawImage`` 的
      **透明黑**计入均值（把 lum 拉低）。**不要**改成"sx = max(0, min(sx,
      W-sw)) + sw 回夹"这类更安全的写法 —— 那正是把黑像素洗掉的 clamp 版。
      sw<=W 时与逐字 JS 完全一致（min(sw,W)=sw）。
    ``region`` 为 None 时按原版 ``!r`` 分支退化为整帧。
    """
    if region is None:
        return 0.0, 0.0, float(width), float(height)
    sw = region.w
    sh = region.h
    sx = max(0.0, min(float(width) - sw, region.x))
    sy = max(0.0, min(float(height) - sh, region.y))
    return sx, sy, sw, sh


def measure_region(rgb01: np.ndarray, region: Rect | None, levels: int = 32) -> LightSample:
    """采样框 → N×N 降采样 → 三通道均值 + 逐级亮度分布。

    对应 ``measureRegion``（index.html:1212-1231，函数体 1212-1224）+
    ``clampRegion``（1244-1251）。N = ``levels``（原版 sampleLight 恒定传 32）。
    ``region`` 为 None 时按 ``clampRegion`` 的 ``!r`` 分支退化为整帧。

    ⚠ clampRegion 的越界段按 canvas ``drawImage`` 的**透明黑零填充**计入均值
      （把黑像素混进去、lum 拉低），不要"改进"成 clamp 回可见区。
    ⚠ 原版返回 null 的退化分支（``sw<=0``/``sh<=0``/源尺寸为 0）离线返回全零
      采样（levels 全 0、mean 全 0），调用方应按「无有效采样」处理。
    """
    if levels < 1:
        raise ValueError(f"levels must be >= 1, got {levels}")
    src = _to_float01(rgb01, 3)
    h_px, w_px = src.shape[:2]
    sx, sy, sw, sh = _clamp_region(region, w_px, h_px)
    if w_px <= 0 or h_px <= 0 or sw <= 0 or sh <= 0:
        return LightSample(levels=tuple(0.0 for _ in range(levels)), mean=(0.0, 0.0, 0.0))
    grid = _box_resample(src, sx, sy, sw, sh, levels, levels)     # (N,N,3)
    flat = grid.reshape(-1, 3)
    mean = (float(flat[:, 0].mean()), float(flat[:, 1].mean()), float(flat[:, 2].mean()))
    lum = flat @ np.array(_LUM_W, dtype=np.float64)
    # 桶下标在精确边界上加 1e-9 保护：Rec.709 加权和落在 [i/n, (i+1)/n) 边界上时，
    # 浮点误差（如 0.625*8 → 4.999999999999999）会把亮度分到下一桶。
    idx = np.minimum(np.floor(lum * levels + 1e-9).astype(np.int64), levels - 1)
    hist = np.bincount(idx, minlength=levels).astype(np.float64)
    hist /= float(flat.shape[0])
    return LightSample(levels=tuple(float(v) for v in hist), mean=mean)


def light_levels(sample: LightSample) -> tuple[float, float]:
    """采样分布的 (lo, hi) —— 5% / 95% 分位亮度（离线派生统计）。

    原版 LIGHT 只保留全局均值，没有 lo/hi；这里是 Python 侧对比度描述：
    直方图第 i 桶代表亮度 ``(i+0.5)/n``，取累计占比首次越过 5% / 95% 的桶。
    空分布（无有效采样）返回 (0.0, 0.0)。
    """
    hist = np.asarray(sample.levels, dtype=np.float64)
    if hist.size == 0 or float(hist.sum()) <= 0.0:
        return 0.0, 0.0
    cum = np.cumsum(hist) / float(hist.sum())
    n = int(hist.shape[0])
    i_lo = int(np.searchsorted(cum, 0.05))
    i_hi = int(np.searchsorted(cum, 0.95))
    i_lo = min(i_lo, n - 1)
    i_hi = min(i_hi, n - 1)
    return (i_lo + 0.5) / n, (i_hi + 0.5) / n


# ── 颗粒噪声砖（index.html:1168-1180）─────────────────────────────────
_GRAIN_TILE: np.ndarray | None = None
_GRAIN_LOCK = threading.Lock()


def grain_tile() -> np.ndarray:
    """256×256 固定种子噪声砖，值域 [-1, 1)。

    对应 ``grainTile``（index.html:1168-1180）：xorshift32、种子 0x9E3779B9，
    ``s >>> 17`` 是无符号右移、``s / 2147483648`` 线性映射到 [-1,1)。
    ⚠ 必须按 32 位回绕实现（JS 位运算全是 int32），用 Python 原生无限精度
    整数会得到不同序列。**只读**，调用方不得改写 —— 返回数组已置
    ``write=False``，误改写会当场抛错而不是静默污染别人看到的同一张砖。

    线程安全（2026-10-09 审计）：``lru_cache`` 只保证值不出错，并发**首调**时
    多个线程可能各建一张值相同但身份不同的数组（8 线程并发首调可复现，
    ``a is b`` 不再成立）。改为双检锁单例：全进程同一对象、只建一次，
    与原版「只建一次」的承诺一致。
    """
    global _GRAIN_TILE
    tile = _GRAIN_TILE
    if tile is None:
        with _GRAIN_LOCK:
            tile = _GRAIN_TILE
            if tile is None:
                tile = _grain_tile_build()
                tile.setflags(write=False)   # 强制 docstring 的只读契约
                _GRAIN_TILE = tile
    return tile


def _grain_tile_build() -> np.ndarray:
    """xorshift32 序列本体（原版 ``grainTile``，index.html:1168-1180）。"""
    s = 0x9E3779B9 - 0x100000000          # |0 → int32
    out = np.empty(256 * 256, dtype=np.float64)
    for i in range(out.shape[0]):
        # s ^= s << 13（int32 回绕）
        t = (s << 13) & 0xFFFFFFFF
        if t >= 0x80000000:
            t -= 0x100000000
        s ^= t
        # s ^= s >>> 17（无符号右移：先取 uint32 再逻辑右移）
        s ^= (s & 0xFFFFFFFF) >> 17
        # s ^= s << 5（int32 回绕）
        t = (s << 5) & 0xFFFFFFFF
        if t >= 0x80000000:
            t -= 0x100000000
        s ^= t
        out[i] = s / 2147483648.0
    return out.reshape(256, 256)


def lit_scale_bucket(scale: float) -> float:
    """立绘屏幕/原生宽度比的分档（1 / 0.5 / 0.25 / 0.125 …）。

    对应 ``litScaleBucket``（index.html:1455-1458）：``clampN(s,0.05,1)`` 后
    ``2^Math.round(log2(s))``，round 用 js_round（半向 +∞）。
    """
    s = clampn(scale, 0.05, 1.0)
    if s >= 1.0:
        return 1.0
    return float(2.0 ** js_round(math.log2(s)))


# ── 立绘自身统计（charLum，index.html:1263-1283）────────────────────────
def _char_stats(char_rgba: np.ndarray) -> tuple[float, float, float, float]:
    """立绘自身 (亮度, 饱和度, R 均值, B 均值) —— charLum（index.html:1262-1283）的口径。

    缩到宽 32、高 ``max(1, js_round(H/W*32))``；**alpha < 8 跳过**（阈值 8/255，
    不是 0）；n==0 时亮度按原版返回 0.5，其余统计离线无残留态按 0。
    缩采样滤波用面积平均（原版 drawImage 滤波未指定，见模块 docstring）。

    ⚠ alpha 判据按原版 ``getImageData`` 的**字节**语义先量化再比（2026-10-09
      审计）：原版 drawImage 缩采样后读回的是 0..255 整数，``d[i+3] < 8`` 比的
      是字节；浮点 alpha 在精确 8/255 边界上会因求和顺序落在 7.9999999999998x /
      8.0000000000000x 两侧（整幅 alpha=8 的立绘实测 1/3 像素被误跳过），
      ``np.rint``（ToUint8Clamp 的 round-half-even）后比较与原版逐位一致。
    """
    src = _to_float01(char_rgba, 4)
    h_px, w_px = src.shape[:2]
    if w_px <= 0 or h_px <= 0:
        return 0.5, 0.0, 0.0, 0.0
    n_x = 32
    n_y = max(1, js_round(h_px / w_px * 32))
    grid = _box_resample(src, 0.0, 0.0, float(w_px), float(h_px), n_x, n_y)
    rgb = grid[..., :3] * 255.0
    alpha = np.rint(np.clip(grid[..., 3], 0.0, 1.0) * 255.0)   # 字节量化（见 docstring）
    solid = alpha >= 8.0
    n = int(solid.sum())
    if n == 0:
        return 0.5, 0.0, 0.0, 0.0
    r = rgb[..., 0][solid]
    g = rgb[..., 1][solid]
    b = rgb[..., 2][solid]
    lum = float(np.mean(_LUM_W[0] * r + _LUM_W[1] * g + _LUM_W[2] * b)) / 255.0
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    sat = float(np.mean(np.where(mx > 0, (mx - mn) / np.where(mx > 0, mx, 1.0), 0.0)))
    return lum, sat, float(r.mean()) / 255.0, float(b.mean()) / 255.0


def char_lum(char_rgba: np.ndarray) -> float:
    """立绘自己的平均亮度（charLum，index.html:1262-1283）。

    透明像素必须跳过 —— 不算的话大半透明的立绘会被当成很暗；全透明返回 0.5。
    """
    return _char_stats(char_rgba)[0]


# ── 烘光 LUT（bakeLit 的三条曲线，index.html:1360-1372）─────────────────
def _lut_bytes(values: np.ndarray) -> np.ndarray:
    """``Math.round(clamp255(x))``：先夹 [0,255] 再 js_round（半向 +∞）。"""
    return np.floor(np.clip(values, 0.0, 255.0) + 0.5).astype(np.uint8)


def _env_sat(mean_rgb: tuple[float, float, float]) -> float:
    """measureRegion 的 sat：HSV 的 S = (max-min)/max，max 为 0 时取 0。"""
    mx = max(mean_rgb)
    mn = min(mean_rgb)
    return (mx - mn) / mx if mx > 0.0 else 0.0


def _lit_luts(env_lum: float, env_r: float, env_b: float, env_sat: float,
              ch_sat: float, ch_r: float, ch_b: float
              ) -> tuple[np.ndarray, np.ndarray, np.ndarray, float, float, float]:
    """构建 bakeLit 的三条 256 项 LUT，返回 (lutR, lutG, lutB, gL, inv, sK)。

    逐字对应 bakeLit 的系数段（index.html:1319-1372）：
      gL  = clampN(lum/0.42, 0.6, 1.15)；inv = (1/gL)^0.6（gamma 指数软化）
      envRB  = max(.05,LIGHT.r)/max(.05,LIGHT.b)；charRB = max(.05,chR)/max(.05,chB)
      charRBg = charRB^inv —— 白平衡基准取"过完曲线之后"的色比
      ratio = (envRB/charRBg)^0.75；gRfull = sqrt(ratio)；gRcap 夹 [0.85, 1.18]
      sK  = clampN(1 + (env_sat - chSat)*0.3, 0.75, 1.25)
      cur = 255*(v/255)^inv；w = 0 / 1 / (v-165)/50（v<=165 / v>=215 / 中间）
      lutR = round(clamp255(cur*(gRfull+(gRcap-gRfull)*w)))
      lutB = round(clamp255(cur*(1/gRfull+(1/gRcap-1/gRfull)*w)))
      lutG = round(clamp255(cur))
    三条边界（165 / 215 及 gRfull→gRcap 过渡）的取整一律 js_round（半向 +∞）。
    """
    g_l = clampn(env_lum / LIGHT_NEUTRAL, LIGHT_GAIN_MIN, LIGHT_GAIN_MAX)
    inv = math.pow(1.0 / g_l, 0.6)
    env_rb = max(0.05, env_r) / max(0.05, env_b)
    char_rb = max(0.05, ch_r) / max(0.05, ch_b)
    char_rb_g = math.pow(char_rb, inv)
    ratio = math.pow(env_rb / char_rb_g, WB_K)
    g_r_full = math.sqrt(ratio)
    g_r_cap = GMIN if g_r_full < GMIN else (GMAX if g_r_full > GMAX else g_r_full)
    s_k = clampn(1.0 + (env_sat - ch_sat) * SAT_K, 0.75, 1.25)
    v = np.arange(256, dtype=np.float64)
    cur = np.power(v / 255.0, inv) * 255.0
    w = np.where(v <= 165.0, 0.0, np.where(v >= 215.0, 1.0, (v - 165.0) / 50.0))
    lut_r = _lut_bytes(cur * (g_r_full + (g_r_cap - g_r_full) * w))
    lut_b = _lut_bytes(cur * (1.0 / g_r_full + (1.0 / g_r_cap - 1.0 / g_r_full) * w))
    lut_g = _lut_bytes(cur)
    return lut_r, lut_g, lut_b, g_l, inv, s_k


# ── 立绘烘光（bakeLit，index.html:1285-1447）────────────────────────────
def _to_u8_rgba(char_rgba: np.ndarray) -> np.ndarray:
    """RGBA → uint8 (H,W,4)。float64 [0,1] 入参按 SPEC §1.2 存回语义量化。"""
    arr = np.asarray(char_rgba)
    if arr.dtype == np.uint8:
        if arr.ndim != 3 or arr.shape[2] != 4:
            raise ValueError(f"expected (H,W,4) RGBA, got {arr.shape}")
        return arr
    if arr.ndim != 3 or arr.shape[2] != 4:
        raise ValueError(f"expected (H,W,4) RGBA, got {arr.shape}")
    return np.floor(np.clip(arr.astype(np.float64), 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def bake_lit(char_rgba: np.ndarray, sample: LightSample | None,
             canvas: tuple[int, int], *, grain: bool = True) -> np.ndarray:
    """把立绘按采样到的环境亮度/色温烘一版（gamma 曲线 + 白平衡 + 饱和 + 颗粒）。

    对应 ``bakeLit``（index.html:1285-1447）+ ``charLum``（1263-1283）+
    ``grainTile``（1168-1180）+ ``litScaleBucket``（1455-1458）。
    ``sample`` 为 None 时按原版「总开关关 / 采样无效」分支**原样返回立绘**
    （index.html:1305-1306）。``grain=False`` 只门控颗粒那一段（原版
    ``LIGHT_GRN`` 开关，index.html:1142）——曲线/白平衡/饱和的数值一个字节
    都不动，关掉颗粒不等于关掉光。``canvas`` = 立绘落在画布上的目标矩形尺寸
    (目标宽, 目标高)，仅用于颗粒缩放补偿 —— 原版的 ``litScale`` 由 drawCharTo
    在绘制前填 ``c.w/chCanvas.width``（index.html:3153），离线没有绘制句柄，
    由调用方把这个比例的目标宽传进来；传整块画布尺寸时补偿退化为 1（不补偿），
    不影响其余分量。返回与输入同族：uint8 进出 uint8、float64[0,1] 进出 float64[0,1]，
    内部一律按 0..255 字节计算，保证 LUT 取整与原版一致。

    LUT 语义三行摘要：
      1. 明暗走**曲线**不走乘法：out = 255·(v/255)^inv，inv = (1/gL)^0.6，
         gL = clamp(环境亮度/0.42, 0.6, 1.15) —— 亮环境提亮、暗环境压暗但保亮部反差。
      2. 白平衡在曲线**之后**按线性增益乘：R 增益 gRfull→gRcap（高光限幅 [0.85,1.18]），
         B 增益取倒数（gR·gB=1，不动整体明暗）；增益基准是过完曲线的色比 charRB^inv。
      3. 逐字节出口：LUT 内 js_round，饱和度混合与颗粒在浮点里做，
         最终落字节用 Uint8ClampedArray 的 round-half-even；透明像素 RGB 清零、alpha 原样。
    """
    src = _to_u8_rgba(char_rgba)
    out_family_float = not (np.asarray(char_rgba).dtype == np.uint8)
    if sample is None:
        return (src.astype(np.float64) / 255.0) if out_family_float else src.copy()
    _ch_lum, ch_sat, ch_r, ch_b = _char_stats(char_rgba)
    mean = sample.mean
    env_lum = _LUM_W[0] * mean[0] + _LUM_W[1] * mean[1] + _LUM_W[2] * mean[2]
    lut_r, lut_g, lut_b, _g_l, _inv, s_k = _lit_luts(
        env_lum, mean[0], mean[2], _env_sat(mean), ch_sat, ch_r, ch_b)
    h_px, w_px = src.shape[:2]
    # 颗粒幅度：关掉颗粒时整段算法都不算（原版 gAmp 直接置 0）
    g_amp = 0.0
    if grain:
        lit_scale = (canvas[0] / w_px) if (canvas and canvas[0] > 0 and w_px > 0) else 1.0
        g_amp = (clampn(GRN_MAX * (1.0 - env_lum * 1.35), GRN_MIN, GRN_MAX)
                 * clampn(1.0 / lit_scale_bucket(lit_scale), 1.0, GRN_COMP_MAX))

    r = lut_r[src[..., 0]].astype(np.float64)
    g = lut_g[src[..., 1]].astype(np.float64)
    b = lut_b[src[..., 2]].astype(np.float64)
    if s_k != 1.0:
        y = _LUM_W[0] * r + _LUM_W[1] * g + _LUM_W[2] * b
        r = y + (r - y) * s_k
        g = y + (g - y) * s_k
        b = y + (b - y) * s_k
    if g_amp > 0.0:
        tile = grain_tile()
        # 广播索引代替 meshgrid：两块 (h,w) int64 整网在 1509×1000 上是 24 MB 白烧
        n = tile[(np.arange(h_px)[:, None] & 255), (np.arange(w_px)[None, :] & 255)]
        n_l = n * g_amp
        n_c = n * g_amp * 0.35
        r = r + n_l
        g = g + n_l - n_c
        b = b + n_l + n_c
    out = np.zeros_like(src)
    solid = src[..., 3] != 0
    rgb = np.rint(np.clip(np.stack([r, g, b], axis=-1), 0.0, 255.0)).astype(np.uint8)
    out[..., 0:3] = np.where(solid[..., None], rgb, 0)
    out[..., 3] = np.where(solid, src[..., 3], 0)
    if out_family_float:
        return out.astype(np.float64) / 255.0
    return out
