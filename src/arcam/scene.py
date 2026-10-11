"""scene.json 配方（buildScene / saveScene / loadScene）。

配方是与原版**互通**的那一份几何数据：照片之外的相机、人物、落位、画幅。
Python 侧输出必须字段同名、同顺序、同单位、同取整，下游（人或脚本）照着它
才能把立绘摆回同一个位置。

已如实保留的原版行为 / 已知偏离：
- ``mmd`` 恒为 ``null``：Python 侧没有 three.js 3D 运行时（SPEC §0），原版
  ``use3D && M3D && M3D.ready`` 这一段离线无从触发。
- 冻结签名里没有 ``shot`` / ``charScale`` 两个入参（SPEC §3.7 与 index.html:3756/3760
  的字段表在这里对不上）。为保住 scene.json 100% 同构，二者做成带默认值的关键字
  参数（默认 = 原版 ``S.shot`` / ``S.charScale`` 的默认值，index.html:931-936），
  已有调用点的写法一个字都不用改。
- 原版 ``JSON.stringify(scene, null, 2)``（index.html:3839）→ ``json.dump(indent=2)``，
  ``ensure_ascii=False`` 让中文画幅 key 与 JS 一样原样落盘。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from arcam.geometry import Camera, focal_px, project
from arcam.numeric import Rect, js_round, js_to_fixed
from arcam.placement import SpritePlacement, Subject

#: 配方格式版本（index.html:3731）
SCENE_VERSION: int = 1
#: 原版 ``S.shot`` 默认值（index.html:934）
DEFAULT_SHOT: str = '全身'
#: 原版 ``S.charScale`` 默认值（index.html:937）
DEFAULT_CHAR_SCALE: float = 1.0
#: 原版 image.file（index.html:3732）：只记基名，实际下载名带时间戳前缀
SCENE_IMAGE_FILE: str = 'shot.jpg'


def build_scene(rect: Rect, placement: SpritePlacement | None, subj: Subject,
                cam: Camera, *, frame_key: str, char_rgba01: np.ndarray | None,
                char_name: str, mode: str, edge_bar: int = 0,
                shot: str = DEFAULT_SHOT,
                char_scale: float = DEFAULT_CHAR_SCALE) -> dict:
    """配方 dict（index.html:3729-3789 buildScene 的 1:1 同构）。

    字段顺序、单位、取整语义全部照抄原版：

    ==================== ================== ====================================
    JSON 路径            类型               来源 / 取整
    ==================== ================== ====================================
    ``version``          ``1``
    ``image.width``      ``js_round(R.w)``
    ``image.height``      ``js_round(R.h)``
    ``image.file``       固定 ``'shot.jpg'``
    ``frame.aspect``     FRAMES 中文 key
    ``frame.crop``       ``[round(R.x), round(R.y), round(R.w), round(R.h)]``
    ``frame.source``     ``cv.width / cv.height``（裁切前整屏）
    ``camera.focalPx``   ``js_to_fixed(focal(), 2)``
    ``camera.focal35mm`` ``S.feq`` 原值
    ``camera.pitchDeg``  ``js_to_fixed(S.pitch, 3)``
    ``camera.rollDeg``   ``js_to_fixed(S.roll, 3)``
    ``camera.heightM``   ``S.h`` 原值
    ``subject.heightM``  ``S.Hp`` 原值
    ``subject.distanceM`` ``js_to_fixed(S.d, 3)``
    ``subject.lateralM`` ``js_to_fixed(S.x, 3)``
    ``subject.footPixel`` ``project(S.x, -S.h, S.d)`` → ``{u,v}`` 或 ``null``
    ``subject.headPixel`` ``project(S.x, S.Hp - S.h, S.d)`` → ``{u,v}`` 或 ``null``
    ``ground.y``         ``0``
    ``ground.normal``    ``[0, 1, 0]``
    ``shot``             SHOTS 中文 key
    ``charScale``        倍率（原值，``character.sizePixel`` 里已经乘过了）
    ``edge.bar``         0–4
    ``edge.rotationLocked`` ``bool(S.edgeBar)``
    ``character``        ``null``（无立绘）/ ``{name,mode,image,centerPixel,sizePixel,rotationDeg}``
    ``character.centerPixel`` ``[toFixed(2), toFixed(2)]``
    ``character.sizePixel``   ``[toFixed(2), toFixed(2)]``
    ``character.rotationDeg`` ``toFixed(pc.rot*R2D, 3)``
    ``mmd``              离线恒 ``null``
    ==================== ================== ====================================

    Args:
        rect: 出图裁切框 ``R``（同 ``frameRect``）。
        placement: 立绘落位 ``pc``；None 或 ``char_rgba01`` 为 None 时
            ``character`` 记 ``null``（原版"下游别当成有立绘但位置是 0"）。
        subj: 人物位姿。
        cam: 相机（画布尺寸/离地高/焦距/俯角/滚转）。
        frame_key: 画幅中文 key，原样写入 ``frame.aspect``。
        char_rgba01: 立绘（只取它的宽高写 ``character.image``）。
        char_name: 立绘文件名（``character.name``）。
        mode: ``'ground'`` / ``'free'``。
        edge_bar: 框条编号 0–4。
        shot: 景别中文 key。
        char_scale: 纯大小系数。

    Returns:
        可直接 ``json.dump`` 的 dict。
    """
    char = None
    if placement is not None and char_rgba01 is not None:
        char = {
            "name": char_name,
            "mode": mode,
            "image": {"width": int(char_rgba01.shape[1]),
                      "height": int(char_rgba01.shape[0])},
            "centerPixel": [js_to_fixed(placement.u, 2), js_to_fixed(placement.v, 2)],
            "sizePixel": [js_to_fixed(placement.w, 2), js_to_fixed(placement.h, 2)],
            "rotationDeg": js_to_fixed(placement.rot * 180.0 / math.pi, 3),
        }
    return {
        "version": SCENE_VERSION,
        "image": {"width": js_round(rect.w), "height": js_round(rect.h),
                  "file": SCENE_IMAGE_FILE},
        "frame": {
            "aspect": frame_key,
            "crop": [js_round(rect.x), js_round(rect.y), js_round(rect.w), js_round(rect.h)],
            "source": {"width": int(cam.width), "height": int(cam.height)},
        },
        "camera": {
            "focalPx": js_to_fixed(focal_px(cam), 2),
            "focal35mm": cam.feq_mm,
            "pitchDeg": js_to_fixed(cam.pitch_deg, 3),
            "rollDeg": js_to_fixed(cam.roll_deg, 3),
            "heightM": cam.height_m,
        },
        "subject": {
            "heightM": subj.height_m,
            "distanceM": js_to_fixed(subj.distance_m, 3),
            "lateralM": js_to_fixed(subj.lateral_m, 3),
            "footPixel": _project_point(subj.lateral_m, -cam.height_m, subj.distance_m, cam),
            "headPixel": _project_point(subj.lateral_m, subj.height_m - cam.height_m,
                                        subj.distance_m, cam),
        },
        "ground": {"y": 0, "normal": [0, 1, 0]},
        "shot": shot,
        "charScale": char_scale,
        "edge": {"bar": int(edge_bar), "rotationLocked": bool(edge_bar)},
        "character": char,
        # 3D 立绘（MMD）：离线没有运行时，这一枪恒是 2D 渲的
        "mmd": None,
    }


def _project_point(x: float, y: float, d: float, cam: Camera) -> dict | None:
    """``project(S.x, y, S.d)`` 的 dict 化：相机后方（Zc<=0.05）→ ``null``。

    原版返回 ``{u, v}`` 裸浮点、不做任何取整，JSON 写全精度双精度。
    """
    p = project(x, y, d, cam)
    if p is None:
        return None
    return {"u": p[0], "v": p[1]}


def save_scene(scene: dict, path: Any, *, indent: int = 2) -> None:
    """配方落盘：``JSON.stringify(scene, null, 2)``（index.html:3839）的等价。

    ``ensure_ascii=False`` —— 画幅/景别是中文 key，JS 的 ``JSON.stringify``
    原样写 UTF-8，Python 这边也不转 ``\\uXXXX``。

    非有限浮点（NaN / ±Infinity）落 ``null`` —— ``JSON.stringify`` 的既定行为
    （``JSON.stringify({u: NaN})`` → ``{"u":null}``）。Python 的 ``json.dumps``
    默认吐 ``NaN``/``Infinity`` 字面量，那不是合法 JSON，原版工具读不回。
    有限值一个字节都不动。

    Args:
        scene: :func:`build_scene` 的产物。
        path: 目标路径（``str`` 或 ``Path``）。
        indent: 缩进，默认 2（与原版一致）。
    """
    text = json.dumps(_json_safe(scene), indent=indent, ensure_ascii=False)
    with open(Path(path), "w", encoding="utf-8") as f:
        f.write(text)


def _json_safe(node: Any) -> Any:
    """把非有限 float 换成 None（= JS ``JSON.stringify`` 的 NaN/Infinity → null）。

    只可能在非有限入参（``--x/--d/--hp/--h/--pitch/--roll`` 传 ``nan``/``inf``）
    下出现；正常配方走不到这个分支，逐字节不变。
    """
    if isinstance(node, float):
        return node if math.isfinite(node) else None
    if isinstance(node, dict):
        return {k: _json_safe(v) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [_json_safe(v) for v in node]
    return node


def load_scene(path: Any) -> dict:
    """读回配方，只做最小校验：能解析、``version == 1``。

    Args:
        path: 配方文件（``str`` 或 ``Path``）。

    Returns:
        dict。

    Raises:
        ValueError: 内容不是 JSON object，或 ``version`` 不是 1。
    """
    with open(Path(path), "r", encoding="utf-8") as f:
        scene = json.load(f)
    if not isinstance(scene, dict):
        raise ValueError("scene.json 顶层必须是 object")
    if scene.get("version") != SCENE_VERSION:
        raise ValueError(f"不支持的 scene.json 版本：{scene.get('version')!r}，"
                         f"期望 {SCENE_VERSION}")
    return scene
