"""ISNet-anime 抠图 —— cutout.js AI 抠图线的 Python 等价实现。

对应原版（`oss-teardown` 仓库 cutout.js，全局钩子 ``window.ARCAM_CUT``）：

- :func:`seg_preprocess`  ↔ ``segPreprocess``  cutout.js:619-664
- :func:`seg_postprocess` ↔ animeCut 的裁回+二值化段  cutout.js:681-718
- :func:`keep_largest_blob` ↔ ``keepLargestBlob``  cutout.js:554-606
- :func:`anime_cut` ↔ ``animeCut``  cutout.js:670-747（+ index.html:2703 闸门）

模型：isnet-anime int8 量化（44,214,375 B，sha256
``cb5f78506887f5250352c04ec8ea242874db3434f0b4a216d041a810b20377e3``，cutout.js:366-369），
输入 1024×1024 写死、sigmoid 已烧进模型图（cutout.js:697-701），所以 Python 侧同样
**不做 sigmoid**。⚠ 若换 HuggingFace skytnt/anime-seg 的 FP32 原版（输出 logits），
需在调用 seg_postprocess 之前补 ``1/(1+exp(-x))`` —— 这是本模块与上游模型的唯一量纲差异。

onnxruntime **懒加载**（import 在 :func:`_load_session` 内）：环境没装只是 AI 抠图
不可用，``import arcam`` 本身不受影响（SPEC §1.1）。

口径：图像 float64 [0,1] 进出、uint8 RGBA 出（RGB 保留原图只换 alpha，cutout.js:740-744）；
缩放滤波取双线性（预处理）/ BICUBIC（蒙版回贴）—— 原版
imageSmoothingQuality='high' 未指定具体核，属 SPEC §5 已记录偏离。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from arcam.numeric import js_round

SEG_SZ = 1024                                    # cutout.js:310
SEG_MEAN = (0.485, 0.456, 0.406)                 # cutout.js:311
SEG_STD = (0.229, 0.224, 0.225)                  # cutout.js:312
SEG_LO = 0.28                                    # cutout.js:712
SEG_HI = 0.46                                    # cutout.js:712
FG_MIN = 0.02                                    # index.html:2703
MODEL_ENV = 'ARCAM_ISNET_MODEL'
MODEL_REPO = Path('anime') / 'isnet-anime-q8.onnx'
MODEL_CACHE = Path.home() / '.cache' / 'arcam' / 'isnet-anime-q8.onnx'
MODEL_SIZE = 44214375                            # cutout.js:367（仅文档记录，不强制）
MODEL_SHA256 = 'cb5f78506887f5250352c04ec8ea242874db3434f0b4a216d041a810b20377e3'  # cutout.js:368
MODEL_HF = 'skytnt/anime-seg'


@dataclass(frozen=True)
class MattingResult:
    """anime_cut 的返回（对应 cutout.js:746 的 ``{canvas, fg, ms}``，Python 侧无画布句柄）。"""

    rgba: np.ndarray   # uint8 (H,W,4)，RGB 保留原图、alpha=蒙版
    fg: float          # 前景占比（kept / 图片区域像素数）
    ms: float          # 耗时毫秒（含预处理+推理+后处理，对应 cutout.js:673 起的 t0）


def anime_cut(rgb01: np.ndarray, model_path: str | None = None,
              *, intra_op: int = 1, simd: bool = True) -> MattingResult:
    """AI 抠图主入口（cutout.js:670-747）。

    管线：模型解析 → 会话 → seg_preprocess → ``sess.run`` → seg_postprocess
    → keep_largest_blob → 蒙版高质量缩回原尺寸（BICUBIC）后回贴 alpha。

    - ``model_path`` 缺省按顺序解析：参数 → 环境 ``$ARCAM_ISNET_MODEL`` →
      仓库相对路径 ``anime/isnet-anime-q8.onnx``（当前工作目录，对应原版
      cutout.js:303-309 按脚本 URL 解出的 ``./anime/`` 邻居目录）→
      ``~/.cache/arcam/isnet-anime-q8.onnx``；全缺 → :class:`FileNotFoundError`
      （带 HuggingFace skytnt/anime-seg 下载指引）。
    - ``intra_op`` → ORT ``intra_op_num_threads``（对应 cutout.js:507 的
      ``numThreads = 1``：GitHub Pages 给不了 COOP/COEP，原版钉死单线程）。
    - ``simd`` 仅为与 cutout.js:508 ``ort.env.wasm.simd`` 对齐而保留：onnxruntime
      CPU 构建的 SIMD 是编译期能力、无运行时开关，此参数在 Python 侧无操作。
    - ``fg < 0.02`` → ``ValueError('没有检出前景')``（index.html:2703 闸门；
      原版文案为 t('note.aiNothing')，见 index.html:767）。
    - fg 口径 = 最大连通域+补洞后的前景像素数 / (nw·nh)（cutout.js:746
      ``kept/(W*H)``）。Python 侧 keep_largest_blob 只回传蒙版，故以「非零像素」
      复现；被补的洞其原软值恰为 0 时原版计入而此处不计（已知微差）。
    """
    img = _as_rgb01(rgb01)
    height, width = img.shape[:2]
    path = _resolve_model_path(model_path)
    sess = _load_session(path, intra_op=intra_op, simd=simd)
    t0 = time.perf_counter()
    tensor, (ox, oy, nw, nh) = seg_preprocess(img)
    in_name = sess.get_inputs()[0].name          # 输入名 'img'，不写死（SPEC §3.9）
    raw = sess.run(None, {in_name: tensor})[0]   # 取第一个输出，[1,1,1024,1024]
    mask = seg_postprocess(raw, (ox, oy, nw, nh), shape=tuple(int(v) for v in raw.shape[-2:]))
    mask = keep_largest_blob(mask)
    region = mask[oy:oy + nh, ox:ox + nw]
    fg = float(np.count_nonzero(region)) / float(nw * nh)
    alpha01 = _resize_mask(region, (height, width))
    rgba = np.empty((height, width, 4), dtype=np.uint8)
    rgba[..., :3] = _to_u8(img)                  # RGB 保留原图（cutout.js:740-744）
    rgba[..., 3] = _to_u8(alpha01)
    ms = (time.perf_counter() - t0) * 1000.0
    if fg < FG_MIN:
        raise ValueError('没有检出前景')
    return MattingResult(rgba=rgba, fg=fg, ms=ms)


def seg_preprocess(rgb01: np.ndarray) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """原图 → 1024×1024 NCHW float32 + ImageNet 归一化（cutout.js:619-664）。

    返回 ``(tensor, (ox, oy, nw, nh))``：tensor 形状 ``[1,3,1024,1024]`` float32；
    crop 是图片区域在方图里的偏移/尺寸，后处理按它把蒙版裁回图片区域。

    五步，与原版逐步对应：
      1. 长边等比缩到 1024：``s = 1024/max(w,h)``，``nw/nh = max(1, round(w·s))``
         （cutout.js:621-622，取整走 js_round）
      2. 8bit 量化后双线性缩放 —— 原版 canvas 本身就是 8bit（drawImage 读出即 8bit），
         Python 侧先把 float64 [0,1] 量化回 uint8 再缩放，保持同一量化时点
         （滤波核的偏离见模块 docstring）
      3. 居中贴 1024 方图：``ox/oy = (1024-n)>>1``（cutout.js:623, 635）
      4. 补边 = **边缘延展**（最外一圈像素往外拉，cutout.js:636-655：17 张实测
         外圈误判率 4.01% vs 拉伸/填灰 11.17%）。⚠ 原版 8 次 ext() 里四角那 4 次
         在 letterbox 几何下恒为空操作——长边 round 后恰好铺满 1024，ox/oy 必有一个
         为 0，角区不存在；此处照样补角只为对任意几何都成立。
      5. 归一化 ``(x/255 - mean)/std``，RGB 通道序、NCHW 平面布局（cutout.js:658-663；
         无 BGR 翻转、不带 alpha、无任何 blur/median）
    """
    img = _as_rgb01(rgb01)
    height, width = img.shape[:2]
    if height < 1 or width < 1:
        raise ValueError(f'图像尺寸非法：{height}x{width}')
    scale = SEG_SZ / max(width, height)
    nw = max(1, js_round(width * scale))
    nh = max(1, js_round(height * scale))
    ox = (SEG_SZ - nw) // 2
    oy = (SEG_SZ - nh) // 2
    small = np.asarray(
        Image.fromarray(_to_u8(img), 'RGB').resize((nw, nh), Image.BILINEAR),
        dtype=np.uint8,
    )
    canvas = np.zeros((SEG_SZ, SEG_SZ, 3), dtype=np.uint8)
    canvas[oy:oy + nh, ox:ox + nw] = small
    if ox:
        canvas[oy:oy + nh, :ox] = small[:, :1]            # 左：重复首列
        canvas[oy:oy + nh, ox + nw:] = small[:, -1:]      # 右：重复末列
    if oy:
        canvas[:oy, ox:ox + nw] = small[:1, :]            # 上：重复首行
        canvas[oy + nh:, ox:ox + nw] = small[-1:, :]      # 下：重复末行
    if ox and oy:
        canvas[:oy, :ox] = small[0, 0]                    # 四角：重复角像素
        canvas[:oy, ox + nw:] = small[0, -1]
        canvas[oy + nh:, :ox] = small[-1, 0]
        canvas[oy + nh:, ox + nw:] = small[-1, -1]
    norm = (canvas.astype(np.float64) / 255.0 - np.asarray(SEG_MEAN)) / np.asarray(SEG_STD)
    tensor = np.ascontiguousarray(norm.transpose(2, 0, 1), dtype=np.float32)[None]
    return tensor, (ox, oy, nw, nh)


def seg_postprocess(mask: np.ndarray, crop: tuple[int, int, int, int],
                    shape: tuple[int, int]) -> np.ndarray:
    """模型软概率输出 → 裁回图片区域 + 窄带线性二值化（cutout.js:681-718）。

    ``mask`` 是模型第一个输出（``[1,1,MH,MW]`` / ``[MH,MW]`` / 一维均可）。
    **不做 sigmoid** —— q8 模型已把 sigmoid 烧进图内（cutout.js:697-701）；上游
    HuggingFace FP32 原版输出 logits，需调用方先补 sigmoid（见模块 docstring）。

    ``crop = (ox, oy, nw, nh)`` 来自 seg_preprocess；``shape`` 是模型输出的空间
    尺寸（cutout.js:678 从 ``first.dims`` 读，输出画布即此尺寸）。返回 float64 [0,1]：

    - 图片区域 = ``clip((p - 0.28) / 0.18, 0, 1)`` 的窄带线性二值化（cutout.js:712-718；
      上界从 0.42 压到 0.28 防浅色头发/半透衣料被啃，同时收窄带子保边缘清晰）
    - 补边区置 0（cutout.js:681-683：不裁的话补边会被当成角色的一部分一起抠出来）

    孤立噪点在此不去除 —— 那是 keep_largest_blob 的职责（cutout.js:554-606）。
    crop 超出模型输出尺寸 → ValueError（原版会读到越界内存，Python 侧显式失败）。
    """
    ox, oy, nw, nh = (int(v) for v in crop)
    out_h, out_w = (int(v) for v in shape)
    if out_h < 1 or out_w < 1:
        raise ValueError(f'模型输出尺寸非法：{out_h}x{out_w}')
    if ox < 0 or oy < 0 or nw < 1 or nh < 1 or ox + nw > out_w or oy + nh > out_h:
        raise ValueError(f'crop {(ox, oy, nw, nh)} 超出模型输出尺寸 {(out_h, out_w)}')
    raw = np.asarray(mask, dtype=np.float64)
    if raw.size != out_h * out_w:
        raise ValueError(f'mask 元素数 {raw.size} 与 shape {(out_h, out_w)} 不符')
    raw = raw.reshape(out_h, out_w)
    band = (raw[oy:oy + nh, ox:ox + nw] - SEG_LO) / (SEG_HI - SEG_LO)
    out = np.zeros((out_h, out_w), dtype=np.float64)
    out[oy:oy + nh, ox:ox + nw] = np.clip(band, 0.0, 1.0)
    return out


def keep_largest_blob(mask01: np.ndarray) -> np.ndarray:
    """去杂点 + 补洞（cutout.js:554-606），保下来的像素**用回原软值**。

    输入/输出 float64 [0,1]，二值化阈值取 0.5 —— 与 JS 在 uint8 域上的 ``>=128``
    逐位等价（``js_round(v*255) >= 128 ⟺ v >= 0.5``）。两步：

    1. **8 连通**标连通域只留最大那块（4 连会把斜着的细线——发丝、描边——判成
       两块丢掉，cutout.js:552-553）
    2. 从四边往里 **4 连通**泛洪「外部背景」，泛不到的就是角色内部的洞（闭着的
       眼睛、衣服镂空），补上。⚠ 被丢弃的小连通域对泛洪是障碍：整个被最大域
       包住的小块会被当成洞补回来（cutout.js:588-592 同样行为，如实保留）

    返回新数组（不改入参）；全背景输入原样返回全 0（对应 JS 的 ``return 0``）。
    """
    m = np.asarray(mask01, dtype=np.float64)
    if m.ndim != 2:
        raise ValueError(f'期望二维蒙版，得到形状 {m.shape}')
    on = m >= 0.5
    if not on.any():
        return np.zeros_like(m)
    labels, count = _label_components(on, connectivity=8)
    sizes = np.bincount(labels.ravel(), minlength=count + 1)
    sizes[0] = 0
    keep = labels == int(sizes.argmax())
    bg_labels, bg_count = _label_components(~keep, connectivity=4)
    if bg_count:
        border = np.unique(np.concatenate([
            bg_labels[0, :], bg_labels[-1, :], bg_labels[:, 0], bg_labels[:, -1],
        ]))
        outside = border[border > 0]
        holes = ~np.isin(bg_labels, outside)
    else:
        holes = np.zeros_like(keep)
    return np.where(keep | holes, m, 0.0)


def _as_rgb01(img: np.ndarray) -> np.ndarray:
    """入参规整为 float64 (H,W,3) [0,1]；uint8 按 0..255 归一，RGBA 丢 alpha
    （对应原版 getImageData 后只取 RGB 三通道，cutout.js:658-662）。"""
    a = np.asarray(img)
    if a.ndim != 3 or a.shape[2] not in (3, 4):
        raise ValueError(f'期望 (H,W,3) 或 (H,W,4) 图像，得到形状 {a.shape}')
    a = a.astype(np.float64) / 255.0 if np.issubdtype(a.dtype, np.integer) else a.astype(np.float64)
    return np.ascontiguousarray(a[:, :, :3])


def _to_u8(x: np.ndarray) -> np.ndarray:
    """[0,1] 浮点 → uint8（SPEC §1.2：clip 后 *255+0.5）。"""
    return (np.clip(x, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def _resize_mask(mask01: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """蒙版缩回原尺寸（cutout.js:734-735，imageSmoothingQuality='high' 的
    Python 等价取 BICUBIC；float64 域先插值再量化，避免二次量化损失）。"""
    height, width = (int(v) for v in shape)
    im = Image.fromarray(np.ascontiguousarray(mask01, dtype=np.float32), mode='F')
    return np.asarray(im.resize((width, height), Image.BICUBIC), dtype=np.float64)


def _resolve_model_path(model_path: str | None) -> Path:
    """模型解析（cutout.js:509-530 的 Python 等价，但按「固定四源」而非三级缓存）：
    参数 → 环境 $ARCAM_ISNET_MODEL → 仓库 anime/isnet-anime-q8.onnx（CWD 相对，
    对应原版 cutout.js:303-309 按脚本 URL 解出的 anime/ 邻居目录）→
    ~/.cache/arcam/isnet-anime-q8.onnx。"""
    if model_path:
        p = Path(model_path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f'model_path 指向的模型文件不存在：{p}')
        return p
    env = os.environ.get(MODEL_ENV)
    if env:
        p = Path(env).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f'${MODEL_ENV} 指向的模型文件不存在：{p}')
        return p
    if MODEL_REPO.is_file():
        return MODEL_REPO
    if MODEL_CACHE.is_file():
        return MODEL_CACHE
    raise FileNotFoundError(
        '未找到 ISNet-anime 模型（已依次尝试：参数 model_path、'
        f'环境 ${MODEL_ENV}、{MODEL_REPO}、{MODEL_CACHE}）。\n'
        f'请从 HuggingFace {MODEL_HF} 下载 isnet-anime-q8.onnx '
        f'（{MODEL_SIZE} B，sha256 {MODEL_SHA256[:8]}…{MODEL_SHA256[-4:]}，'
        '与 cutout.js:366-369 写死值一致），放到上述任一位置；'
        f'或设 ${MODEL_ENV} 指向模型文件。\n'
        '⚠ 若使用 HuggingFace 上的 FP32 原版：其未把 sigmoid 烧进模型图、输出是 '
        'logits，需在调用 seg_postprocess 前补 1/(1+exp(-x))，否则窄带二值化'
        '（lo=0.28/hi=0.46）读到的是错误量纲。'
    )


def _load_session(path: Path, *, intra_op: int, simd: bool):
    """建 ORT 会话（cutout.js:497-537 的 Python 等价）。onnxruntime 懒加载。"""
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = max(1, int(intra_op))
    # simd：原版 ort.env.wasm.simd=true（cutout.js:508）在 ORT CPU 无运行时开关，
    # SIMD 由构建决定；参数保留只为签名对齐 cutout.js:508。
    _ = simd
    try:
        return ort.InferenceSession(str(path), sess_options=opts,
                                    providers=['CPUExecutionProvider'])
    except Exception as e:
        # ORT 的异常体系（InvalidProtobuf/Fail/NoSuchFile…）不是 ValueError/OSError
        # 子类，直接抛会在 CLI 侧变成 traceback。转成人话，由调用方按 ValueError 接。
        raise ValueError(
            f'onnxruntime 打不开模型 {path}：{e}\n'
            '（确认是 isnet-anime-q8.onnx 本体、没下断；换模型要连大小/sha256 一起换，'
            f'期望 {MODEL_SIZE} B / sha256 {MODEL_SHA256[:8]}…）') from None


def _find(parent: list[int], i: int) -> int:
    root = i
    while parent[root] != root:
        root = parent[root]
    while parent[i] != root:
        parent[i], i = root, parent[i]
    return root


def _label_components(binary: np.ndarray, connectivity: int) -> tuple[np.ndarray, int]:
    """连通域标记（run 合并 + 并查集，纯 numpy 无 scipy）。

    ``binary`` 二维 bool；返回 ``(labels, n)``，labels 为 int32、0=背景。
    ``connectivity`` 4 或 8：相邻行 run 的半开区间 [s,e) 在 4 连下要求列区间
    相交（``ps < e and s < pe``），8 连下允许斜邻（``ps <= e and s <= pe``）——
    斜着的细线只有 8 连才连成一块（cutout.js:552-553）。
    """
    if connectivity not in (4, 8):
        raise ValueError(f'connectivity 只能是 4 或 8，得到 {connectivity}')
    height, width = binary.shape
    labels = np.zeros((height, width), dtype=np.int32)
    parent = [0]
    rows: list[list[tuple[int, int, int]]] = []
    next_id = 1
    diag = connectivity == 8
    for y in range(height):
        row = binary[y]
        runs: list[tuple[int, int, int]] = []
        if row.any():
            d = np.diff(row.astype(np.int8), prepend=np.int8(0), append=np.int8(0))
            starts = np.flatnonzero(d == 1)
            ends = np.flatnonzero(d == -1)
            prev = rows[y - 1] if y else []
            j = 0
            for s, e in zip(starts.tolist(), ends.tolist()):
                while j < len(prev) and prev[j][1] < s:
                    j += 1
                linked: list[int] = []
                k = j
                while k < len(prev) and prev[k][0] <= e:
                    ps, pe, pid = prev[k]
                    if (ps <= e and s <= pe) if diag else (ps < e and s < pe):
                        linked.append(pid)
                    k += 1
                if linked:
                    for pid in linked[1:]:
                        ra, rb = _find(parent, linked[0]), _find(parent, pid)
                        if ra != rb:
                            parent[rb] = ra
                    rid = _find(parent, linked[0])
                else:
                    rid = next_id
                    parent.append(rid)
                    next_id += 1
                runs.append((s, e, rid))
        rows.append(runs)
    roots = [0] * next_id
    for i in range(1, next_id):
        roots[i] = _find(parent, i)
    final: dict[int, int] = {}
    next_label = 1
    for y, runs in enumerate(rows):
        for s, e, rid in runs:
            root = roots[rid]
            lab = final.get(root)
            if lab is None:
                lab = next_label
                final[root] = lab
                next_label += 1
            labels[y, s:e] = lab
    return labels, next_label - 1
