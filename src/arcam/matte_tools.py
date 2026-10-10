"""立绘修图工具链 —— cutout.js 非 AI 线的 Python 等价实现（FEATURE-SPEC §1）。

对应原版（`oss-teardown` 仓库 cutout.js / index.html）：

- :func:`cut_solid_bg`   ↔ ``cutSolidBg``      cutout.js:16-98
- :func:`content_bounds` ↔ ``contentBounds``  cutout.js:246-263
- :func:`auto_trim`      ↔ ``cropToCanvas``   cutout.js:267-276（+ contentBounds）
- :func:`soften_edge`    ↔ ``softenEdge``     cutout.js（柔边段）
- :func:`auto_prepare_char` ↔ ``autoPrepareChar``  index.html:2400-2440

与 AI 抠图（:mod:`arcam.matting`）**互不调用**：两条线并列，调用方按素材选。
纯色底那条 0 依赖、几毫秒，平涂立绘（白底/黑底 JPG）走它；照片级立绘走 AI。

口径：图像 float64 [0,1] (H,W,4) 进出（与 arcam 其余模块一致）；判定阈值按
**字节语义**（alpha >= 8/255，与原版 canvas 的 8 同口径）换算到 01 空间。
所有函数确定性（同输入同输出），无随机、无全局态。

已知微偏差（SPEC §5 口径，不"顺手修"）：半身判定读的是微糊**之后**的倒数第二行
（index.html:2426-2434），而 lens_blur 的重采样内核与原版 canvas drawImage 的
'high' 不在同一实现上——实测默认素材 00_半身1.jpg 裁后二值 31.96%、柔边后
30.93%、PIL 双线性微糊后 29.90%（阈值 30%），浏览器内核可能落在另一侧。该 flag
只决定**默认走贴地还是自由**（原版"只是问，不改任何状态"，index.html:2451-2454），
用户可随时改，故不为单张素材调阈值。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from arcam.numeric import js_round

#: 原版 canvas 的 alpha 判定阈值（cutout.js contentBounds 的 alphaMin 默认 8、
#: index.html:2365 半身判定的同款门限）——01 空间的等价值。
ALPHA_MIN_01 = 8.0 / 255.0

#: 纯色底抠掉的占比门控（index.html:2411 注释：太小=没抠到东西，太大=把人也吃了）。
CUT_RATIO_MIN = 0.05
CUT_RATIO_MAX = 0.85

#: 原版 autoPrepareChar 写死的各步参数（index.html:2410/2423/2427/2431）。
_CUT_TOL = 32.0
_CUT_FEATHER = 1.2
_SOFTEN_ERODE = 1
_SOFTEN_FEATHER = 2
_BLUR_K = 0.62

#: 裁后最小边长（index.html:2413 的 r.canvas.width >= 4 门限）。
_MIN_SIDE = 4

#: 整链兜底的最小内容边长（index.html:2437 的 b.w >= 8 && b.h >= 8）——
#:比裁边自身的 4 更严：修图链动过手之后还只剩一条缝，说明吃过头了，回退。
_MIN_CONTENT_SIDE = 8


def _as_rgba01(img: np.ndarray) -> np.ndarray:
    """(H,W,4) float64 [0,1]；不是 RGBA 的抛 ValueError（与 matting._as_rgb01 同风格）。"""
    arr = np.asarray(img)
    if arr.ndim != 3 or arr.shape[2] != 4:
        raise ValueError(f"期望 (H,W,4) RGBA，收到 {arr.shape}")
    if arr.dtype == np.uint8:
        return arr.astype(np.float64) / 255.0
    return arr.astype(np.float64)


def _box_blur(a: np.ndarray, radius: int) -> np.ndarray:
    """(2r+1)² 盒式均值，**越界像素不计入分母**（cutout.js:80-90 的 n 只数界内邻居）。"""
    h, w = a.shape
    acc = np.zeros_like(a)
    cnt = np.zeros_like(a)
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            ys, ye = max(0, dy), min(h, h + dy)
            xs, xe = max(0, dx), min(w, w + dx)
            if ys >= ye or xs >= xe:
                continue
            acc[ys:ye, xs:xe] += a[ys - dy:ye - dy, xs - dx:xe - dx]
            cnt[ys:ye, xs:xe] += 1.0
    return np.where(cnt > 0, acc / np.maximum(cnt, 1.0), a)


def _erode(a: np.ndarray, radius: int) -> np.ndarray:
    """(2r+1)² 最小值（只收不扩）；边缘外按边缘值（np.pad edge）——画布外不参与。"""
    if radius <= 0:
        return a
    pad = np.pad(a, radius, mode="edge")
    out = pad[radius:radius + a.shape[0], radius:radius + a.shape[1]].copy()
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if not dy and not dx:
                continue
            out = np.minimum(out, pad[radius + dy:radius + dy + a.shape[0],
                                     radius + dx:radius + dx + a.shape[1]])
    return out


def _flood_solid(rgba01: np.ndarray, tol: float,
                 feather: float) -> tuple[np.ndarray | None, float]:
    """纯色底泛洪的单一内部实现（cutout.js:16-98 逐行对译）。

    ``tol`` 按原版口径传**字节值**（0-255 空间，cutout.js:23 的默认 32）——原版
    读的是 canvas 像素字节；本模块进出是 [0,1]，所以在入口换算成
    ``tol/255``（判定 ``sum|Δ| <= tol*3`` 换算后为 ``tol*3/255``）。传 01 空间的
    值会把门限放大 255 倍、整图放行。

    返回 ``(新数组 or None, 抠掉占比)``：不是纯色底 / 没抠掉东西 → ``(None, 0.0)``。
    :func:`cut_solid_bg` 与 :func:`auto_prepare_char` 共用它——逻辑只许有一份。
    """
    arr = rgba01
    tol01 = tol / 255.0
    h, w = int(arr.shape[0]), int(arr.shape[1])
    if not w or not h:
        return None, 0.0
    rgb = arr[..., :3]
    alpha = arr[..., 3]

    # 四角取样（cutout.js:33-36）：索引顺序 [左上, 右上, 左下, 右下]
    corners = ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))
    corner_rgb = np.stack([rgb[y, x] for x, y in corners])          # (4,3)
    corner_a = np.array([alpha[y, x] for x, y in corners])          # (4,)
    # 本来就已经是透明底的（PNG 素材多半是），四角 RGB 都是 0、看着像"纯黑底"，
    # 不拦的话会一本正经地报"抠掉 40%"，其实什么都没发生（cutout.js:37-40）。
    if bool(np.all(corner_a < 10.0 / 255.0)):
        return None, 0.0
    avg = corner_rgb.mean(axis=0)                                    # (3,)
    if float(np.abs(corner_rgb - avg).max()) > tol01:
        return None, 0.0                                             # 四角不一致
    lim = tol01 * 3.0

    # 从四条边框整圈起手（cutout.js:69-72）——不从四角点：那一个像素可能压在角色身上。
    # ⚠ 栈用动态 list 而非原版的定长 Int32Array(w*h)：JS 的 typed array 越界写入是
    #   静默丢弃，且被丢的必是「已在栈里的重复条目」（seen 在弹出时才置位，同一像素
    #   可能被两个邻居各推一次），所以原版不会漏区域；Python 越界会抛 IndexError。
    #   动态栈把所有重复条目都处理（弹出时 seen 挡掉），最终结果与原版逐像素一致。
    stack: list[int] = []
    for x in range(w):
        stack.append(x)
        stack.append(x + (h - 1) * w)
    for y in range(1, h - 1):
        stack.append(y * w)
        stack.append(y * w + w - 1)

    seen = np.zeros(w * h, dtype=bool)
    is_bg = np.zeros(w * h, dtype=bool)
    # ⚠ seen（走到过）与 isBg（确实是底色）必须是两个数组：泛洪撞到人物边缘时那个
    #   像素也会被访问到，但它不是底色；混用一个会把人物最外一圈一起抠掉（cutout.js:64-67）。
    # 逐像素颜色距离预计算（语义不变，避免内层重复索引）。
    dist = (np.abs(rgb[..., 0] - avg[0]) + np.abs(rgb[..., 1] - avg[1])
            + np.abs(rgb[..., 2] - avg[2])).ravel()
    removed = 0
    while stack:
        p = stack.pop()
        if seen[p]:
            continue
        seen[p] = True
        if dist[p] > lim:
            continue                    # 被挡住了，到此为止，但不算底色
        is_bg[p] = True
        removed += 1
        x = p % w
        y = p // w
        if x > 0 and not seen[p - 1]:
            stack.append(p - 1)
        if x < w - 1 and not seen[p + 1]:
            stack.append(p + 1)
        if y > 0 and not seen[p - w]:
            stack.append(p - w)
        if y < h - 1 and not seen[p + w]:
            stack.append(p + w)
    if not removed:
        return None, 0.0

    a = np.where(is_bg, 0.0, 255.0).reshape(h, w)
    if feather > 0:
        # 盒式均值 + **(s/n)|0 字节截断**（cutout.js:88）——原版写回的是
        # Uint8Array，不截断会让下游 soften 的字节域入口多一分小数。
        a = np.floor(_box_blur(a, max(1, js_round(feather))))
    out = arr.copy()
    out[..., 3] = a / 255.0
    return out, removed / float(w * h)


def cut_solid_bg(rgba01: np.ndarray, *, tol: float = _CUT_TOL,
                 feather: float = _CUT_FEATHER) -> float | None:
    """纯色底抠图（cutout.js:16-98 的 1:1 对译）。返回抠掉占比；不是纯色底 → None。

    四角颜色一致（平涂底）就从**边框整圈**往里泛洪，把连通的底色刷成透明，
    再对 alpha 做一次 3×3 均值柔化收边。``tol`` 是**字节值**（0-255，原版默认
    32）：四角 RGB 与四角均值的最大逐通道偏差 > ``tol/255`` → None；flood 内按
    ``sum|Δ| <= tol*3/255``。四角 alpha 全 < 10/255（已透明底）→ None。
    要拿处理后的图用 :func:`auto_prepare_char`。
    """
    _out, ratio = _flood_solid(_as_rgba01(rgba01), tol, feather)
    return ratio if _out is not None else None


def content_bounds(rgba01: np.ndarray,
                   alpha_min: float = ALPHA_MIN_01) -> tuple[int, int, int, int] | None:
    """内容 bbox（cutout.js:246-263 的 1:1 对译）。

    alpha >= ``alpha_min`` 的像素外接框，返回 ``(x, y, w, h)``（**含边界像素**，
    w = x1-x0+1）；无有效像素返回 None。
    """
    arr = _as_rgba01(rgba01)
    ys, xs = np.nonzero(arr[..., 3] >= alpha_min)
    if ys.size == 0:
        return None
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    return (x0, y0, x1 - x0 + 1, y1 - y0 + 1)


def auto_trim(rgba01: np.ndarray,
              alpha_min: float = ALPHA_MIN_01
              ) -> tuple[np.ndarray, tuple[int, int, int, int]] | None:
    """按内容 bbox 裁到新画布（cutout.js:267-276 + contentBounds）。

    裁切框夹在画布内（x ∈ [0, W-1]、w >= 1 且 <= W-x，y/h 同理）；bbox 为 None
    或裁后任一边 < 4 像素（index.html:2413 门限）→ None。返回
    ``(裁后数组, (x, y, w, h))``。
    """
    arr = _as_rgba01(rgba01)
    bounds = content_bounds(arr, alpha_min)
    if bounds is None:
        return None
    bx, by, bw, bh = bounds
    W, H = int(arr.shape[1]), int(arr.shape[0])
    x = max(0, min(W - 1, bx))
    y = max(0, min(H - 1, by))
    w = max(1, min(W - x, bw))
    h = max(1, min(H - y, bh))
    if w < _MIN_SIDE or h < _MIN_SIDE:
        return None
    return arr[y:y + h, x:x + w].copy(), (x, y, w, h)


def _shift_valid(a: np.ndarray, k: int, axis: int) -> tuple[np.ndarray, np.ndarray]:
    """沿 ``axis`` 平移 ``k``（越界补 0），返回 (平移结果, 有效掩码)。

    有效掩码让均值只按**界内样本数**取分母——原版画布外 ``continue`` 的语义。
    """
    out = np.zeros_like(a)
    valid = np.zeros(a.shape, dtype=bool)
    n = a.shape[axis]
    sl_src: list = [slice(None)] * a.ndim
    sl_dst: list = [slice(None)] * a.ndim
    if k > 0:
        sl_src[axis], sl_dst[axis] = slice(0, n - k), slice(k, n)
    elif k < 0:
        sl_src[axis], sl_dst[axis] = slice(-k, n), slice(0, n + k)
    out[tuple(sl_dst)] = a[tuple(sl_src)]
    valid[tuple(sl_dst)] = True
    return out, valid


def _separable_3tap_mean(a: np.ndarray) -> np.ndarray:
    """原版 softenEdge 的 feather 内核（cutout.js:805-817 逐字对译）。

    ⚠ 与 :func:`_box_blur` **不是同一个核**：原版是**可分离 3 抽头均值、横竖
    各一遍、每遍 ``Math.round``（等效 5×5 三角形核，比 5×5 盒式更接近高斯）；
    且 ``fe`` 只当**布尔**用——半径硬编码 1，``feather=2`` 里的 2 不是半径。
    实测差别：24×24 图单个 8×8 块，盒式 vs 原版最大差 153/255、96/576 像素
    不同（盒式留下峰值 36% 的软团，原版留下硬 3×3 边）。
    ``a`` 为**字节值**（0..255 的 float64）；返回同为字节值。
    """
    cur = a
    for axis in (1, 0):                       # 先横向后纵向（原版 pass 0 = horiz）
        acc = cur.copy()                      # k=0 恒在界内
        cnt = np.ones_like(cur)
        for k in (-1, 1):
            shifted, valid = _shift_valid(cur, k, axis)
            acc = acc + shifted
            cnt = cnt + valid
        # 每遍 Math.round：字节非负，floor(x+0.5) 即 ECMA Math.round
        cur = np.floor(acc / cnt + 0.5)
    return cur


def soften_edge(rgba01: np.ndarray, *, erode: int = _SOFTEN_ERODE,
                feather: int = _SOFTEN_FEATHER) -> tuple[np.ndarray, bool]:
    """柔边整理（cutout.js softenEdge）：erode 收轮廓 + feather 做软边。

    原版动机（index.html:2417-2420）：不做的两个症状——边是刀口级的（照片里没有
    这种边）+ 亮底抠出来一圈发白描边。erode 用 (2r+1)² **最小值**（只收不扩），
    feather 用盒式均值；画布外不参与。返回 ``(新数组, changed)``。
    """
    arr = _as_rgba01(rgba01)
    if erode <= 0 and feather <= 0:
        return arr.copy(), False
    # ⚠ 原版全程在**字节域**跑（getImageData 的 0..255）：入口量化、出口除回。
    a = np.floor(np.clip(arr[..., 3], 0.0, 1.0) * 255.0 + 0.5)
    if erode > 0:
        a = _erode(a, int(erode))
    if feather > 0:
        a = _separable_3tap_mean(a)
    out = arr.copy()
    out[..., 3] = a / 255.0
    return out, True


def lens_blur(rgba01: np.ndarray, k: float = 0.62) -> np.ndarray:
    """镜头微糊（index.html:2384-2398 ``lensBlur`` 的 1:1 对译）。

    手机照片不是像素级锐利的，而立绘是矢量级干净的——差的就是这点"软"。
    做法：降采样到 ``k`` 倍再升回来（``k=0.62`` 约等于手机主摄 1–2 米处的软度）。

    ⚠ 必须在**预乘 alpha** 空间插值（原版注释：浏览器 drawImage 内部预乘，
    不会把透明像素的黑拉进边缘）；直接对 RGBA 四通道各自插值会把透明处的
    黑色渗进半透明边（亮底素材一圈发黑描边的来源）。所以 RGB 先乘 alpha、
    两路分别双线性缩放、回来再除回。

    ⚠ 目标尺寸的取整走 :func:`arcam.numeric.js_round`（原版 ``Math.round``
    半向 +∞）—— 用 Python 内置 ``round``（半数取偶）在 ``w·k`` 恰好落在
    .5 时会小一档（实测 w=75/k=0.62 → 46.5：内置 46、JS 47），整张图糊得
    不一样（364/6000 像素变、最大差 2.16/255）。
    """
    from PIL import Image
    arr = _as_rgba01(rgba01)
    h, w = int(arr.shape[0]), int(arr.shape[1])
    if h < 8 or w < 8:
        return arr.copy()
    tw = max(2, js_round(w * k))
    th = max(2, js_round(h * k))
    a = arr[..., 3]
    premul = arr[..., :3] * a[..., None]

    def _rs(img01: np.ndarray, size: tuple[int, int]) -> np.ndarray:
        im = Image.fromarray((np.clip(img01, 0.0, 1.0) * 255.0).round().astype(np.uint8))
        return np.asarray(im.resize(size, Image.BILINEAR), dtype=np.float64) / 255.0

    small_p = _rs(premul, (tw, th))
    small_a = _rs(a, (tw, th))
    big_p = _rs(small_p, (w, h))
    big_a = _rs(small_a, (w, h))
    rgb = np.where(big_a[..., None] > 1e-6,
                   big_p / np.maximum(big_a[..., None], 1e-6), 0.0)
    out = np.dstack([rgb, big_a]).astype(np.float64)
    return out


@dataclass(frozen=True)
class PreparedChar:
    """auto_prepare_char 的结论（index.html:2399 的 out 对象对译）。"""

    rgba01: np.ndarray              #: 处理后的立绘（01 口径）
    cut_ratio: float | None         #: 纯色底抠掉的比例（没抠为 None）
    trimmed: bool                   #: 是否裁过边
    half_body: bool                 #: 下沿被切断 → 半身（不能贴地）
    actions: tuple[str, ...]        #: 人话动作（i18n key：note.autoCut/autoTrim/autoSoft/autoBlur）


def is_half_body(rgba01: np.ndarray) -> bool:
    """半身判定（index.html:2428-2434 的 1:1 对译）。

    原版逐字：取**倒数第二行**（``getImageData(0, max(0, h-2), w, 1)``）的
    alpha，数 ``> 200``（字节）的像素，``solid / width > 0.3`` → 半身。
    三个口径都不许改：行位（-2 不是 -1，避开最底行可能的压缩伪影）、阈值
    （200/255 不是 8/255——这是"实"与"边"的分界）、比例（30% 而不是任一点
    ——切口只要盖住三成宽就算被切断）。

    行下标照抄原版的 ``Math.max(0, h-2)``：h>=2 时就是倒数第二行，h==1 时
    退化到唯一的第 0 行（原版同样读它，不是一律返回 False）。没有行可读
    （h==0）或没有列（w==0，原版 ``0/0=NaN > 0.3`` 为 false）时返回 False。

    这种图**不能贴地**——贴地把下沿钉在地面，等于把"腰以上"当成整个人放大
    （§4.6）。半身图应走自由模式（+ 贴下边）。
    """
    arr = _as_rgba01(rgba01)
    h = int(arr.shape[0])
    if h < 1 or int(arr.shape[1]) < 1:
        return False
    row = arr[max(0, h - 2), :, 3]
    solid = int(np.count_nonzero(row > 200.0 / 255.0))
    return (solid / float(arr.shape[1])) > 0.3


def auto_prepare_char(rgba01: np.ndarray, *,
                      enabled: bool = True) -> PreparedChar:
    """装图后自动做的修图链（index.html:2400-2440 的 1:1 对译）。

    顺序逐字照抄原版：
    ① **纯色底抠底**：``cut_solid_bg(tol=32, feather=1.2)``，占比须在
       ``[0.05, 0.85]`` 才算数（太小=没抠到东西，太大=把人也吃了）；
    ② **自动裁边**：从上一步结果出发（从基准重算，绝不叠在处理过的结果上，
       否则越裁越小回不去，index.html:2323）；
    ③ **柔边**：``soften_edge(erode=1, feather=2)``；
    ④ **镜头微糊**：``lens_blur(k=0.62)``（预乘 alpha 空间降采样往返）；
    ⑤ **半身判定**：对 ③④ 之后的结果数倒数第二行（index.html:2428-2434）；
    ⑥ **兜底**：动过手（cut 或 trim）且最终内容 bbox 任一边 < 8 → 整条回退原图。

    风控（原版逐字）：任一步抛错 → 该步静默跳过（自动处理绝不能把"装图"这件事
    本身搞挂）；``enabled=False`` → 原样返回（只报 half_body）。

    ⚠ 回退时的**上报字段**与原版有一处已记录的偏离（像素行为一致）：原版只把
    ``out.cut/trimmed/half`` 重置、``out.soft/blur`` 留着（于是 loadChar 仍会显示
    autoSoft/autoBlur），且 ``half`` 直接置 false；这里回退即"什么都没发生"，故
    ``actions`` 全清、``half_body`` 按**回退后的原图**实算（比原版的 false 更能
    反映真相，调用方拿到的图就是原图）。
    """
    arr = _as_rgba01(rgba01)
    if not enabled:
        return PreparedChar(arr, None, False, is_half_body(arr), ())

    actions: list[str] = []
    cut_ratio: float | None = None
    trimmed = False
    original = arr                            # 回退用（原版 before = copyOf(chBase)）

    # ① 纯色底抠底
    try:
        cut, ratio = _flood_solid(arr, _CUT_TOL, _CUT_FEATHER)
        if cut is not None and CUT_RATIO_MIN <= ratio <= CUT_RATIO_MAX:
            arr = cut
            cut_ratio = ratio
            actions.append("note.autoCut")
    except Exception:
        pass

    # ② 自动裁边（从①的结果出发，不叠加在原图上）
    try:
        res = auto_trim(arr)
        if res is not None:
            arr, _rect = res
            trimmed = True
            actions.append("note.autoTrim")
    except Exception:
        pass

    # ③ 柔边整理
    try:
        softened, changed = soften_edge(arr, erode=_SOFTEN_ERODE,
                                        feather=_SOFTEN_FEATHER)
        if changed:
            arr = softened
            actions.append("note.autoSoft")
    except Exception:
        pass

    # ④ 镜头微糊
    try:
        blurred = lens_blur(arr, _BLUR_K)
        if not np.array_equal(blurred, arr):
            arr = blurred
            actions.append("note.autoBlur")
    except Exception:
        pass

    # ⑤ 半身判定（③④ 之后）
    half = is_half_body(arr)

    # ⑥ 兜底：动过手且最终内容 bbox 任一边 < 8 → 整条回退（index.html:2435-2439）
    if cut_ratio is not None or trimmed:
        bounds = content_bounds(arr)
        if bounds is None or bounds[2] < _MIN_CONTENT_SIDE or bounds[3] < _MIN_CONTENT_SIDE:
            return PreparedChar(original, None, False, is_half_body(original), ())

    return PreparedChar(arr, cut_ratio, trimmed, half, tuple(actions))
