"""立绘层合成 —— 接触阴影与立绘落位（``drawCharTo`` 的 Python 等价）。

两条公开入口：
  · ``contact_shadow_layer``（index.html:3056-3143 的 footPrint + drawContactShadow
    颜色段）：形状 = 立绘底部 8.5% 高度带内**逐列 alpha 最大值**（落地轮廓，
    双脚/裙摆自动带出），横向移动平均抹边，纵向高斯带峰值压在接触线上；
    颜色 = 环境色暗化版（无环境采样时按原版 LIGHT 初值 0.5 回退）。
  · ``composite_sprite``（index.html:3146-3166 的 drawCharTo）：
    translate(c.u, c.v) → rotate(c.rot) → **先铺阴影** → 再画立绘，
    锚点 = 立绘外接矩形中心，立绘占局部矩形 (-c.w/2, -c.h/2, c.w, c.h)。

与画布的对应关系（Python 侧没有 canvas 句柄）：
  · ``contact_shadow_layer`` 返回**立绘原生分辨率**的落地轮廓 RGBA 层
    （宽 = 立绘宽、高 = max(4, js_round(w*SH_THICK))），即原版 ``drawImage``
    的源 ``footCv``；``composite_sprite`` 把它直接单次重采样到旋转后的局部矩形
    (-c.w/2, top, c.w, c.w*SH_THICK)，与原版一次 ``drawImage`` + ``rotate``
    的滤波次数一致。
  · 落位/旋转用**逆映射双线性 warp**（原生 → 画布一次重采样，亚像素精度），
    边缘按夹取处理；已知偏离：原版 ``imageSmoothingQuality='high'`` 未指定具体核
    （SPEC §5），canvas 实现在预乘空间滤波，这里同样预乘（避免黑边）。
  · 阴影只在**贴地模式**画（原版 index.html:3117：自由模式不知道地面在哪，
    凭空补影子就是猜）—— 离线签名里没有 mode，调用方在自由模式传 ``shadow=False``。
  · 颗粒缩放补偿用的 ``litScale``（index.html:3153）属 bake_lit 的关注点，
    由调用方在烘光时传入画布目标尺寸；本模块只负责落位。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

from arcam.numeric import clampn, js_round

if TYPE_CHECKING:  # SpritePlacement 归属 placement.py（P1）；运行期只按 u/v/w/h/rot 属性协议
    from arcam.placement import SpritePlacement

# ── 接触阴影常量（index.html:3044-3049）────────────────────────────────
SH_BAND = 0.085    # 取立绘底部 8.5% 当"落地轮廓"
SH_THICK = 0.24    # 阴影厚度 ÷ 立绘宽（地面被斜着看，压得很扁）
SH_ALPHA = 0.42    # 最深处浓度
SH_DARK = 0.55     # 阴影亮度 = 环境亮度 × 这个系数
SH_PEAK = 0.30     # 纵向峰值位置（0 = 上边缘，1 = 下边缘）
SH_SPREAD = 0.26   # 纵向高斯宽度

_LUM_W = (0.2126, 0.7152, 0.0722)
_LIGHT_DEFAULT = (0.5, 0.5, 0.5)   # index.html:1132 LIGHT 初值（无采样时的回退）
_WARP_CHUNK = 128                  # warp 分块行数（限制内存，不影响结果）


# ── 图像通道规整 ───────────────────────────────────────────────────────
def _to_float01(img: np.ndarray, channels: int) -> np.ndarray:
    """uint8 → float64 [0,1]；float 入参夹到 [0,1]（口径不变，只是按 dtype 抄近路）。

    **总是返回新数组**（调用方拿到手就可以就地改，不会写回入参）——
    ``composite_sprite`` 靠这条把「规整」和「拷贝画布」合成一步。
    """
    arr = np.asarray(img)
    if arr.ndim != 3 or arr.shape[2] != channels:
        raise ValueError(f"expected (H,W,{channels}) image, got {arr.shape}")
    if arr.dtype == np.uint8:
        # 一趟 ufunc 完成「升精度 + 归一」（astype 再 /= 是两趟整幅扫描）；
        # uint8→float64 精确、除法与先 cast 再除逐位相同
        return arr / 255.0
    if arr.dtype == np.float64:
        # 夹取与拷贝合成一趟（clip 一定返回新数组）—— 4K 画布上省一整遍读写
        return np.clip(arr, 0.0, 1.0)
    out = np.array(arr, dtype=np.float64, copy=True)
    np.clip(out, 0.0, 1.0, out=out)
    return out


# ── 逆映射双线性 warp（drawImage + rotate 的离线等价）───────────────────
def _premul_source(src01: np.ndarray) -> np.ndarray:
    """源 → (H,W,4) **预乘**数组：``concat(RGB·alpha, alpha)``。

    分块 warp 下它与块无关、每幅源只算一次（见 :func:`_bilinear_rgba_premul`）。
    拼成一张 4 通道而不是「RGB 一张 + alpha 两张」：每个角点一次 fancy-gather
    就拿齐 4 通道，比 gather×2 + concatenate 少两轮全块拷贝。
    """
    src_pm = np.empty((src01.shape[0], src01.shape[1], 4), dtype=np.float64)
    np.multiply(src01[..., :3], src01[..., 3:4], out=src_pm[..., :3])
    src_pm[..., 3] = src01[..., 3]
    return src_pm


def _bilinear_rgba_premul(src_pm: np.ndarray, fx: np.ndarray, fy: np.ndarray) -> np.ndarray:
    """在**预乘** RGBA 源上按连续坐标 (fx, fy) 双线性采样；滤波后解除预乘。

    ``src_pm`` 是 :func:`_premul_source` 的产物（预乘 RGB + alpha 拼成的
    (H,W,4)）。边缘按夹取（canvas 滤波的常见实现）；源外由调用方先把坐标夹进
    [0, n-1]。预乘源**与块无关**，提到分块循环外算一次（每块重算等于白烧：
    实测 1509×1000 源 + 4K 画布，单次 composite_sprite 0.51 s → 0.36 s）。
    """
    sh, sw = src_pm.shape[:2]
    x0 = np.floor(fx)
    y0 = np.floor(fy)
    wx = (fx - x0)[..., None]
    wy = (fy - y0)[..., None]
    x0i = np.clip(x0.astype(np.int64), 0, sw - 1)
    x1i = np.clip((x0 + 1).astype(np.int64), 0, sw - 1)
    y0i = np.clip(y0.astype(np.int64), 0, sh - 1)
    y1i = np.clip((y0 + 1).astype(np.int64), 0, sh - 1)

    top = src_pm[y0i, x0i] * (1.0 - wx) + src_pm[y0i, x1i] * wx
    bot = src_pm[y1i, x0i] * (1.0 - wx) + src_pm[y1i, x1i] * wx
    out = top * (1.0 - wy) + bot * wy
    a = out[..., 3:4]
    safe = a > 1e-9
    rgb = np.where(safe, out[..., :3] / np.where(safe, a, 1.0), 0.0)
    return np.concatenate([rgb, a], axis=-1)


def _warp_rect(src01: np.ndarray, lx0: float, ly0: float, rw: float, rh: float,
               rot: float, cu: float, cv: float, out_hw: tuple[int, int]) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """把 src01 贴到旋转后的局部矩形 ``[lx0, lx0+rw] × [ly0, ly0+rh]``（相对锚点）。

    等价于 canvas 的 ``translate(cu,cv); rotate(rot); drawImage(src, lx0, ly0, rw, rh)``：
    对画布每个像素中心做逆旋转 + 逆缩放，双线性采样源（一次重采样、亚像素精度）。
    局部矩形外 / 画布外 → 全透明。

    返回 ``(层, span)``：span = ``(ya0, ya1, xa0, xa1)`` 是**有墨水可能**的
    画布包围盒（层只写这一块、盒外恒全透明）。:func:`_over` 拿它当扫描范围，
    4K 画布上不必通读一整幅全零层找 bbox。
    """
    h_out, w_out = out_hw
    out = np.zeros((h_out, w_out, 4), dtype=np.float64)
    if rw <= 0 or rh <= 0:
        return out, (0, 0, 0, 0)
    sh, sw = src01.shape[:2]
    if sw <= 0 or sh <= 0:
        return out, (0, 0, 0, 0)
    ca, sa = math.cos(rot), math.sin(rot)
    # 预乘源只跟源有关、跟块无关：提到循环外算一次（分块 warp 每块都重算就是白烧）
    src_pm = _premul_source(src01)
    corners = ((lx0, ly0), (lx0 + rw, ly0), (lx0 + rw, ly0 + rh), (lx0, ly0 + rh))
    rxs = [cu + px * ca - py * sa for px, py in corners]
    rys = [cv + px * sa + py * ca for px, py in corners]
    xa0 = max(0, int(math.floor(min(rxs))) - 1)
    xa1 = min(w_out, int(math.ceil(max(rxs))) + 1)
    ya0 = max(0, int(math.floor(min(rys))) - 1)
    ya1 = min(h_out, int(math.ceil(max(rys))) + 1)
    if xa1 <= xa0 or ya1 <= ya0:
        return out, (ya0, ya0, xa0, xa0)
    for y0 in range(ya0, ya1, _WARP_CHUNK):
        y1 = min(ya1, y0 + _WARP_CHUNK)
        jj, ii = np.mgrid[y0:y1, xa0:xa1]
        u = ii + 0.5 - cu
        v = jj + 0.5 - cv
        lx = u * ca + v * sa
        ly = -u * sa + v * ca
        inside = ((lx >= lx0) & (lx <= lx0 + rw) & (ly >= ly0) & (ly <= ly0 + rh))
        fx = np.clip((lx - lx0) / rw * sw - 0.5, 0.0, sw - 1.0)
        fy = np.clip((ly - ly0) / rh * sh - 0.5, 0.0, sh - 1.0)
        warped = _bilinear_rgba_premul(src_pm, fx, fy)
        warped[..., 3] = np.where(inside, warped[..., 3], 0.0)
        rgb = np.where(warped[..., 3:4] > 0.0, warped[..., 0:3], 0.0)
        out[y0:y1, xa0:xa1] = np.concatenate([rgb, warped[..., 3:4]], axis=-1)
    return out, (ya0, ya1, xa0, xa1)


def _over(dst_rgb: np.ndarray, layer_rgba: np.ndarray,
          span: tuple[int, int, int, int] | None = None) -> np.ndarray:
    """canvas source-over：透明浮层盖到不透明底上（Cr = Cs·αs + Cb·(1-αs)）。

    只在浮层**有墨水**（α>0）的包围盒里混合，并就地写回 ``dst_rgb``：
    盒外 α=0 时公式退化成 ``Cb·1 + Cs·0 = Cb``，逐位等于原底，所以裁出盒子算
    与整幅算结果相同（3024×4032 随机数据逐位比对过）；而立绘只占画布一小块，
    整幅乘加在手机照片（3024×4032 float64 ≈ 291 MB/通道组）上是纯浪费 ——
    实测单次 1189 ms → 118 ms。``dst_rgb`` 必须是调用方私有的副本
    （:func:`composite_sprite` 传的是 :func:`_to_float01` 的返回值，契约上
    就是新数组）。

    ``span`` = ``(ya0, ya1, xa0, xa1)``：可选的**墨水搜索范围**。warp 出来的层
    在 span 外 alpha 恒 0（:func:`_warp_rect` 只写自己那块包围盒），把扫描限制
    在 span 里逐位相同，而 4K 画布上不必通读一整幅 265 MB 的全零层。
    """
    if span is None:
        span = (0, layer_rgba.shape[0], 0, layer_rgba.shape[1])
    ya0, ya1, xa0, xa1 = span
    ys, xs = np.nonzero(layer_rgba[ya0:ya1, xa0:xa1, 3] > 0.0)
    if ys.size == 0:
        return dst_rgb
    y0, y1 = ya0 + int(ys.min()), ya0 + int(ys.max()) + 1
    x0, x1 = xa0 + int(xs.min()), xa0 + int(xs.max()) + 1
    box = (slice(y0, y1), slice(x0, x1))
    a = layer_rgba[box][..., 3:4]
    dst_rgb[box] = dst_rgb[box] * (1.0 - a) + layer_rgba[box][..., 0:3] * a
    return dst_rgb


# ── 接触阴影 ───────────────────────────────────────────────────────────
def _shadow_color(ambient: tuple[float, float, float] | None) -> tuple[int, int, int]:
    """阴影颜色 = 环境色暗化版（drawContactShadow，index.html:3133-3138）。

    lumS = clampN(亮度, 0.04, 1)·SH_DARK；rb = sqrt(max(.05,r)/max(.05,b))
    （暖冷比的平方根，只施加一半）；q(v) = js_round(clampN(v,0,1)·255/6)·6
    （量化到 6 的倍数）。无环境采样时按原版 LIGHT 初值 0.5 回退（rb=1）。
    """
    if ambient is None:
        lum, env_r, env_b = _LIGHT_DEFAULT
        rb = 1.0
    else:
        env_r, env_g, env_b = float(ambient[0]), float(ambient[1]), float(ambient[2])
        lum = _LUM_W[0] * env_r + _LUM_W[1] * env_g + _LUM_W[2] * env_b
        rb = math.sqrt(max(0.05, env_r) / max(0.05, env_b))
    lum_s = clampn(lum, 0.04, 1.0) * SH_DARK

    def q(v: float) -> int:
        return js_round(clampn(v, 0.0, 1.0) * 255.0 / 6.0) * 6

    return q(lum_s * rb), q(lum_s), q(lum_s / rb)


def _shear_profile(sm: np.ndarray, out_h: int, shadow_az: float) -> np.ndarray:
    """斜射影子的拖偏：逐列轮廓按每行横移重采样（sprite.py 新增，原版无此项）。

    第 ``y`` 行的采样位置左移 ``shear·(y+0.5)`` 列——影子离脚线越远（y 越大）
    拖得越偏，方向由 ``shadow_az`` 决定。``shear`` 取 ``tan(az)`` 并按影子自身
    宽高比归一（横向移动量以影子宽度为单位、纵向以影子高度为单位，两个方向
    在画布上的缩放才一致）。

    ⚠ ``shadow_az == 0`` 时调用方**不该**进来：整数位置的双线性虽精确返回原值，
    但没必要为默认路径冒浮点风险（原版行为必须逐位不变）。
    边界夹取（不反射/不补零）：夹取会让最外侧列被重复，视觉上影子沿拖偏方向
    「续」了一截，比反射产生的对称鬼影合理。
    """
    w = sm.shape[0]
    shear = math.tan(math.radians(shadow_az)) * (out_h / float(w))
    xs = np.arange(w, dtype=np.float64)
    out = np.empty((out_h, w), dtype=np.float64)
    for y in range(out_h):
        src = xs - shear * (y + 0.5)
        lo = np.floor(src)
        frac = src - lo
        lo_c = np.clip(lo.astype(np.int64), 0, w - 1)
        hi_c = np.clip(lo.astype(np.int64) + 1, 0, w - 1)
        out[y] = sm[lo_c] * (1.0 - frac) + sm[hi_c] * frac
    return out


def contact_shadow_layer(char_rgba: np.ndarray, c: "SpritePlacement",
                         ambient: tuple[float, float, float] | None,
                         shadow_az: float = 0.0,
                         shadow_opacity: float = 1.0) -> np.ndarray:
    """立绘原生分辨率下的接触阴影层（index.html:3027-3143；footPrint 3056-3113
    + drawContactShadow 的颜色段 3115-3138）。

    步骤（逐字对应原版）：
      1. ``c.w < 14`` → 不画，返回 1×1 全透明层；立绘 ``w < 12 || h < 12`` 同样。
      2. ``outH = max(4, js_round(w·SH_THICK))``（w = 立绘**像素**宽）；
         ``bandH = max(2, js_round(h·SH_BAND))`` 取底部 8.5% 那一条带。
      3. **逐列 alpha 最大值** → 落地轮廓 prof[x]（那一列有没有踩在地上、踩得多实）。
      4. 横向移动平均，半径 ``r = max(1, js_round(w·0.02))``，越界样本跳过、按有效数取均值
         （不抹的话影子边缘就是脚本身的硬轮廓，比立绘还清楚）。
      5. 纵向高斯带：``t=(y+0.5)/outH``，``dy=t-SH_PEAK``，
         ``fall=exp(-dy²/(2·SH_SPREAD²))``；``fall<0.004`` 整行透明；
         ``a = sm[x]·fall·SH_ALPHA``，``a<=0.002`` 该像素透明，否则
         ``alpha = js_round(a·255)``，RGB = 阴影色。

    出口按原版 ``Uint8ClampedArray`` 语义**钳到 [0,255]** 再落字节
    （index.html:3096/3107：``o`` 是 ``createImageData(...).data``，赋值即钳）——
    q() 在极暖环境（lumS·rb≥1，rb=√(r/b) 最大 √20）会吐出 258，原版存 255，
    numpy 的 ``astype(uint8)`` 会回绕成 2（阴影一个通道从白翻成黑）。

    ``shadow_az``（度，默认 0 = 原版行为）：光线方位角。0 = 光在相机正后
    方（影子正落在脚下，与原版逐位一致）；非 0 = 斜射，影子沿背光方向拖偏
    （逐行横移轮廓，见 :func:`_shear_profile`）。**原版 2D 阴影是旋转对称的、
    没有方向**——这是离线侧新增的创作控制（MMD 3D 路径一向有 keyAz），
    默认值保证与原版逐位相同。
    ``shadow_opacity``（默认 1.0）：浓度倍率，乘在 ``SH_ALPHA`` 上。

    返回 (outH, w, 4) uint8 RGBA —— 即原版 ``drawImage(fp, -sw/2, top, sw, shh)``
    的源。颜色语义：阴影里没有直射光只剩漫射，所以更暗也更淡；不用 multiply
    而用 source-over 盖一层（index.html:3119-3132：cv 是透明浮层，multiply 在
    透明底上退化成 source-over；后果是这层只会压暗/提亮背景，用**平均**地面色
    代替**局部**地面色 —— 局部比平均暗的地方仍会被略微提亮，如实保留）。
    """
    char = _to_float01(char_rgba, 4)
    h_px, w_px = char.shape[:2]
    if c.w < 14 or w_px < 12 or h_px < 12:
        return np.zeros((1, 1, 4), dtype=np.uint8)
    col_r, col_g, col_b = _shadow_color(ambient)
    out_h = max(4, js_round(w_px * SH_THICK))
    band_h = max(2, js_round(h_px * SH_BAND))
    band = char[h_px - band_h:, :, 3] * 255.0
    prof = band.max(axis=0) / 255.0                      # 逐列 alpha 最大值
    r = max(1, js_round(w_px * 0.02))
    # 横向移动平均（窗口 ±r，越界跳过、按有效样本数取均值）
    prof_pad = np.concatenate([np.full(r, np.nan), prof, np.full(r, np.nan)])
    sm = np.empty(w_px, dtype=np.float64)
    for x in range(w_px):
        seg = prof_pad[x:x + 2 * r + 1]
        sm[x] = seg[~np.isnan(seg)].mean()
    ys = np.arange(out_h, dtype=np.float64)
    t = (ys + 0.5) / out_h
    dy = t - SH_PEAK
    fall = np.exp(-(dy * dy) / (2.0 * SH_SPREAD * SH_SPREAD))
    if shadow_az:
        sm = _shear_profile(sm, out_h, shadow_az)
    else:
        sm = sm[None, :]                     # (1, w)：两支之后 sm 都是可广播行/整幅
    a = sm * fall[:, None] * (SH_ALPHA * shadow_opacity)
    keep = (fall[:, None] >= 0.004) & (a > 0.002)
    alpha = np.where(keep, np.floor(a * 255.0 + 0.5), 0.0)
    out = np.zeros((out_h, w_px, 4), dtype=np.float64)
    out[..., 0] = col_r
    out[..., 1] = col_g
    out[..., 2] = col_b
    out[..., 3] = alpha
    # Uint8ClampedArray 存回语义：钳到 [0,255]（index.html:3096/3107）。
    # 两道都会越界：色通道 q() 极暖环境出 258（→255 而不是回绕成 2）；
    # alpha 在 shadow_opacity > 2.4 时 a·255 超 255（→255 而不是回绕成小值）。
    return np.clip(out, 0.0, 255.0).astype(np.uint8)


# ── 立绘落位合成（drawCharTo，index.html:3146-3166）─────────────────────
def composite_sprite(base_rgb01: np.ndarray, char_rgba01: np.ndarray, c: "SpritePlacement",
                     *, shadow: bool = True, ambient: tuple[float, float, float] | None = None,
                     shadow_layer: np.ndarray | None = None) -> np.ndarray:
    """把（已烘光的）立绘按摆位合成进画布，返回**新画布**（float64 [0,1]，不改入参）。

    对应 ``drawCharTo``（index.html:3146-3166）：
      ``translate(c.u, c.v); rotate(c.rot);`` 先 ``drawContactShadow`` 再
      ``drawImage(litChar(), -c.w/2, -c.h/2, c.w, c.h)``。
    · 锚点 = 立绘外接矩形中心 (c.u, c.v)，旋转绕该中心，立绘占局部矩形
      (-c.w/2, -c.h/2, c.w, c.h)。
    · 阴影层局部矩形 (-c.w/2, c.h/2 - c.w·SH_THICK·SH_PEAK, c.w, c.w·SH_THICK)
      —— 峰值那条线压在"立绘下沿"（脚踩地面的地方）。
    · 缩放双线性（已知偏离：原版 imageSmoothingQuality='high' 未指定核，SPEC §5）。
    · 阴影默认画；**自由模式应传 shadow=False**（原版 index.html:3117 只在贴地模式画，
      离线签名没有 mode，由调用方把关）。``shadow_layer`` 可传入
      :func:`contact_shadow_layer` 的结果以复用（原版 footCv 缓存）。
    · ``c`` 为 None 或尺寸退化 → 原样返回画布副本。
    """
    base = _to_float01(base_rgb01, 3)
    out = base                       # _to_float01 已保证是新数组，省一次整幅拷贝
    if c is None or c.w <= 0 or c.h <= 0:
        return out
    char01 = _to_float01(char_rgba01, 4)
    h_out, w_out = out.shape[:2]
    if shadow:
        layer = shadow_layer if shadow_layer is not None else contact_shadow_layer(
            char_rgba01, c, ambient)
        layer01 = _to_float01(layer, 4)
        if layer01.shape[0] > 0 and layer01.shape[1] > 0 and float(layer01[..., 3].max()) > 0.0:
            sh_thick = c.w * SH_THICK
            warped, span = _warp_rect(layer01, -c.w / 2.0, c.h / 2.0 - sh_thick * SH_PEAK,
                                      c.w, sh_thick, c.rot, c.u, c.v, (h_out, w_out))
            out = _over(out, warped, span)
    warped_char, span = _warp_rect(char01, -c.w / 2.0, -c.h / 2.0, c.w, c.h,
                                   c.rot, c.u, c.v, (h_out, w_out))
    out = _over(out, warped_char, span)
    return out
