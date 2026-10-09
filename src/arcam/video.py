"""位姿轨道 · 离线视频渲染。

原版没有「轨道」这个对象：真机取景器把 deviceorientation 的 pitch/roll 实时写进
``S``（index.html:1069 起 onOrient），桌面端一次只导一张图 —— 快门单文件 JPEG
q0.95（index.html:3797–3814）、素材包三件套 zip（index.html:3820–3851）。

本模块是 P5 新增的离线构件，语义上等价于「把真机那一段时间的 S 录下来再离线重放」：
轨道 JSON 的每个参数直接对应 ``S`` 的一个字段（同名、同单位），逐帧走**同一条**
composeShot 管线（index.html:3673–3701：同一 frameRect、同一 drawCharTo 落位、
同一 LAST_SHOT_LIGHT 思路），因此每一帧都与原版导出的单张图同构。

轨道文件 schema（README「轨道文件」一节有完整示例）：::

    {"width":1080,"height":1920,"fps":30,"height_m":1.6,"feq_mm":23,
     "subject_height_m":1.65,"char_scale":1.0,"size_pct":60.0,"mode":"ground",
     "frame_key":"满幅","shot":"全身",
     "keys":[{"t":0.0,"lateral_m":0,"distance_m":4,"pitch_deg":0,"roll_deg":0}]}

轨道级字段全部有默认值（= 原版 ``S`` 的默认值，index.html:921–937）；键内必需
``t`` 与 ``distance_m``，``lateral_m``/``pitch_deg``/``roll_deg`` 缺省 0，其余字段
（height_m/feq_mm/subject_height_m/char_scale/size_pct/mode）缺省 ``None`` = 沿用
轨道默认 —— 对应原版「没拖过的滑块保持 S 默认」。两键之间对数值字段线性插值。
"""

from __future__ import annotations

import json
import math
from bisect import bisect_right
from dataclasses import dataclass
from difflib import get_close_matches
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

from arcam.compose import compose_shot
from arcam.geometry import Camera
from arcam.numeric import js_round
from arcam.placement import FRAMES, SHOTS, Subject

__all__ = [
    "PoseKey",
    "PoseTrack",
    "FramePose",
    "PITCH_RANGE",
    "ROLL_RANGE",
    "DISTANCE_RANGE",
    "FRAME_FMT",
    "FRAME_SUFFIXES",
    "MP4_NAME",
    "load_track",
    "save_track",
    "sample_track",
    "frame_count",
    "track_issues",
    "render_track",
    "image_to_rgb01",
    "image_to_rgba01",
    "save_png",
    "encode_png",
    "encode_jpeg",
]


FRAME_FMT = "frame_%06d.png"
"""帧序列命名（与 ffmpeg 的默认序列命名一致，从 1 开始计数）。"""

MP4_NAME = "video.mp4"
"""imageio 可用时追加写入的成片文件名。"""

GIF_NAME = "video.gif"
"""``render_track(gif=True)`` 追加写入的动画 GIF 文件名（纯 PIL）。"""

FRAME_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
"""背景帧目录认得的图片后缀。"""

PITCH_RANGE = (-60.0, 60.0)
"""自检俯角范围（度）。原版 compositionHints 只提示不拦（>40° 压扁，index.html:1890），
这里把可渲染范围放宽到 ±60°，check/render 用它做硬校验。"""

ROLL_RANGE = (-45.0, 45.0)
"""自检滚转范围（度）。超过 ±45° 画面语义上接近翻转，视为误输入。"""

DISTANCE_RANGE = (0.05, 120.0)
"""自检纵向距离范围（米）。下界同 project 的 Zc<=0.05 相机后方判据（index.html:1048–1057）。"""

_TRACK_DEFAULTS: dict[str, object] = {
    "width": 1080,
    "height": 1920,
    "fps": 30.0,
    "height_m": 1.6,
    "feq_mm": 23.0,
    "subject_height_m": 1.65,
    "char_scale": 1.0,
    "size_pct": 60.0,
    "mode": "ground",
    "frame_key": "满幅",
    "shot": "全身",
}
"""轨道级默认值。除 width/height/fps 外的八个字段（height_m/feq_mm/
subject_height_m/char_scale/size_pct/mode/frame_key/shot）与 S 的默认值逐字对齐
（index.html:921–937）；width/height/fps 是离线新增（原版 canvas 像素 = 视频像素，
index.html:3941）。"""

_TRACK_FIELDS = tuple(_TRACK_DEFAULTS) + ("keys",)
_KEY_BASE = ("t", "lateral_m", "distance_m", "pitch_deg", "roll_deg")
_KEY_FIELDS = _KEY_BASE + (
    "height_m", "feq_mm", "subject_height_m", "char_scale", "size_pct", "mode",
)


@dataclass(frozen=True)
class PoseKey:
    """一个关键帧的摆位快照（对应某一时刻的 ``S``）。

    除 ``t`` 外全部是标量：原版滑块拖到哪里就写哪里。可选字段为 ``None`` 时表示
    「这个键不表态」，插值时回落到轨道级默认（:data:`_TRACK_DEFAULTS`）。
    """

    t: float
    """时间戳（秒）。必须逐键**严格**递增（重复时间戳会让前一键永远取不到，
    属作者笔误，:func:`load_track` 直接报错）。"""
    lateral_m: float = 0.0
    """人物横向偏移（米，+右）。对应 S.x。"""
    distance_m: float = 4.0
    """人物到相机的纵向距离（米）。对应 S.d。"""
    pitch_deg: float = 0.0
    """俯角（度，低头为正）。对应 S.pitch，index.html:1048 的 p。"""
    roll_deg: float = 0.0
    """滚转角（度，画面顺时针）。对应 S.roll。"""
    height_m: float | None = None
    """相机离地高（米）。None = 沿用轨道默认（原版 S.h）。"""
    feq_mm: float | None = None
    """等效 35mm 焦距（mm）。None = 沿用轨道默认（原版 S.feq）。"""
    subject_height_m: float | None = None
    """人物身高（米）。None = 沿用轨道默认（原版 S.Hp）。"""
    char_scale: float | None = None
    """立绘倍率。None = 沿用轨道默认（原版 S.charScale）。"""
    size_pct: float | None = None
    """自由模式下立绘占画面高度百分比。None = 沿用轨道默认（原版 S.sizePct）。"""
    mode: str | None = None
    """摆放模式 'ground'|'free'。None = 沿用轨道默认（原版 S.mode）。"""


@dataclass(frozen=True)
class PoseTrack:
    """一条位姿轨道：画布规格 + 轨道级默认 + 关键帧序列。"""

    width: int
    """画布宽（像素）。"""
    height: int
    """画布高（像素）。"""
    fps: float
    """帧率（帧/秒）。"""
    height_m: float
    """相机离地高（米），键内未表态时用它。"""
    feq_mm: float
    """等效焦距（mm），键内未表态时用它。"""
    subject_height_m: float
    """人物身高（米），键内未表态时用它。"""
    char_scale: float
    size_pct: float
    mode: str
    frame_key: str
    """出图画幅，FRAMES 的中文 key（原版存的就是中文，index.html:2153–2154）。"""
    keys: list[PoseKey]
    shot: str = "全身"
    """景别，SHOTS 的中文 key。缺省 '全身' = 原版 S.shot 默认。"""


@dataclass(frozen=True)
class FramePose:
    """某一帧解算完成后的完整摆位（插值 + 缺省回落后的结果）。"""

    lateral_m: float
    distance_m: float
    pitch_deg: float
    roll_deg: float
    height_m: float
    feq_mm: float
    subject_height_m: float
    char_scale: float
    size_pct: float
    mode: str
    shot: str = "全身"

    def camera(self, width: int, height: int) -> Camera:
        """按画布尺寸拼相机（index.html:1041–1047 的 focal/project 入参）。"""
        return Camera(width, height, self.height_m, self.feq_mm,
                      self.pitch_deg, self.roll_deg)

    def subject(self) -> Subject:
        """拼人物（S.Hp / S.d / S.x）。"""
        return Subject(self.subject_height_m, self.distance_m, self.lateral_m)


def _reject_unknown(data: dict, allowed: tuple[str, ...], where: str) -> None:
    """把 JSON 里的错别字段名报出来（difflib 猜一个相近的正确名），不静默忽略。"""
    for name in data:
        if name not in allowed:
            guess = get_close_matches(name, allowed, n=1)
            hint = f"（是否想写 {guess[0]!r}？）" if guess else ""
            raise ValueError(f"{where} 出现未知字段 {name!r}{hint}；可用字段：{', '.join(allowed)}")


def _num(data: dict, name: str, where: str) -> float:
    if name not in data:
        raise ValueError(f"{where} 缺少字段 {name!r}")
    value = data[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where} 字段 {name!r} 应为数字，得到 {value!r}")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{where} 字段 {name!r} 不是有限数：{value}")
    return value


def _opt_num(data: dict, name: str, where: str) -> float | None:
    value = data.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where} 字段 {name!r} 应为数字或 null，得到 {value!r}")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{where} 字段 {name!r} 不是有限数：{value}")
    return value


def _text(data: dict, name: str, where: str) -> str:
    if name not in data:
        raise ValueError(f"{where} 缺少字段 {name!r}")
    value = data[name]
    if not isinstance(value, str):
        raise ValueError(f"{where} 字段 {name!r} 应为字符串，得到 {value!r}")
    return value


def _num_or(data: dict, name: str, where: str, default: float) -> float:
    return default if name not in data else _num(data, name, where)


def _text_or(data: dict, name: str, where: str, default: str) -> str:
    return default if name not in data else _text(data, name, where)


def _key_num(data: dict, name: str, where: str, default: float) -> float:
    """键内数字字段：缺省走 default（lateral/pitch/roll 对应 S 的 0）。"""
    value = _opt_num(data, name, where)
    return default if value is None else value


def load_track(path: str | Path) -> PoseTrack:
    """读轨道 JSON（schema 见 README）。

    做结构校验（字段名、类型、必须项、时间戳严格递增、枚举成员），缺省字段回落
    :data:`_TRACK_DEFAULTS`。数值范围自检交给 :func:`track_issues`（``check`` 子命令
    与 :func:`render_track` 共用），这里只拦一定会算错的结构问题。
    """
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"轨道文件不存在：{p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"轨道文件不是合法 JSON：{p}\n  {e}") from None
    if not isinstance(data, dict):
        raise ValueError(f"轨道文件顶层应是一个 JSON 对象，得到 {type(data).__name__}")

    _reject_unknown(data, _TRACK_FIELDS, "轨道顶层")
    raw_keys = data.get("keys")
    if not isinstance(raw_keys, list) or not raw_keys:
        raise ValueError("轨道必须至少有一个关键帧（keys 是非空数组）")

    defaults = _TRACK_DEFAULTS
    kwargs: dict[str, object] = {}
    for name in ("width", "height"):
        kwargs[name] = int(_num_or(data, name, "轨道顶层", float(defaults[name])))
    kwargs["fps"] = _num_or(data, "fps", "轨道顶层", float(defaults["fps"]))
    for name in ("height_m", "feq_mm", "subject_height_m", "char_scale", "size_pct"):
        kwargs[name] = _num_or(data, name, "轨道顶层", float(defaults[name]))
    for name in ("mode", "frame_key", "shot"):
        kwargs[name] = _text_or(data, name, "轨道顶层", str(defaults[name]))

    if kwargs["mode"] not in ("ground", "free"):
        raise ValueError(f"mode 只能是 'ground' 或 'free'，得到 {kwargs['mode']!r}")
    if kwargs["frame_key"] not in FRAMES:
        raise ValueError(f"frame_key 必须是 FRAMES 的中文 key（{', '.join(FRAMES)}），"
                         f"得到 {kwargs['frame_key']!r}")
    if kwargs["shot"] not in SHOTS:
        raise ValueError(f"shot 必须是 SHOTS 的中文 key（{', '.join(SHOTS)}），"
                         f"得到 {kwargs['shot']!r}")
    if kwargs["fps"] <= 0:
        raise ValueError(f"fps 应为正数，得到 {kwargs['fps']}")
    if kwargs["width"] < 1 or kwargs["height"] < 1:
        raise ValueError(f"画布尺寸应为正整数，得到 {kwargs['width']}×{kwargs['height']}")

    keys: list[PoseKey] = []
    prev_t: float | None = None
    for i, raw in enumerate(raw_keys):
        where = f"keys[{i}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{where} 应是一个 JSON 对象，得到 {type(raw).__name__}")
        _reject_unknown(raw, _KEY_FIELDS, where)
        t = _num(raw, "t", where)
        if prev_t is not None and t <= prev_t:
            raise ValueError(f"{where} 时间戳 {t} 早于上一键 {prev_t} —— "
                             "键必须按时间严格递增（不允许重复）")
        prev_t = t
        key_kwargs: dict[str, object] = {
            "t": t,
            "lateral_m": _key_num(raw, "lateral_m", where, 0.0),
            "distance_m": _num(raw, "distance_m", where),
            "pitch_deg": _key_num(raw, "pitch_deg", where, 0.0),
            "roll_deg": _key_num(raw, "roll_deg", where, 0.0),
        }
        for name in ("height_m", "feq_mm", "subject_height_m", "char_scale", "size_pct"):
            key_kwargs[name] = _opt_num(raw, name, where)
        mode = raw.get("mode")
        if mode is not None and mode not in ("ground", "free"):
            raise ValueError(f"{where} mode 只能是 'ground' 或 'free'，得到 {mode!r}")
        key_kwargs["mode"] = mode
        keys.append(PoseKey(**key_kwargs))

    return PoseTrack(keys=keys, **kwargs)


def save_track(track: PoseTrack, path: str | Path) -> None:
    """写轨道 JSON（与 :func:`load_track` 严格往返：顶层全字段、键内只写非 None）。

    顶层把默认值也显式写全（对齐 README 示例的完整形态），键内可选字段只在表态时
    出现，保持文件短、diff 干净。
    """
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {
        "width": track.width,
        "height": track.height,
        "fps": track.fps,
        "height_m": track.height_m,
        "feq_mm": track.feq_mm,
        "subject_height_m": track.subject_height_m,
        "char_scale": track.char_scale,
        "size_pct": track.size_pct,
        "mode": track.mode,
        "frame_key": track.frame_key,
        "shot": track.shot,
    }
    keys: list[dict[str, object]] = []
    for k in track.keys:
        entry: dict[str, object] = {
            "t": k.t,
            "lateral_m": k.lateral_m,
            "distance_m": k.distance_m,
            "pitch_deg": k.pitch_deg,
            "roll_deg": k.roll_deg,
        }
        for name in ("height_m", "feq_mm", "subject_height_m",
                     "char_scale", "size_pct", "mode"):
            value = getattr(k, name)
            if value is not None:
                entry[name] = value
        keys.append(entry)
    data["keys"] = keys
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def frame_count(track: PoseTrack) -> int:
    """帧数 = ``js_round(末键时间 × fps) + 1``：时间轴是闭区间 [0, 末键时间]。

    取整用 JS 语义（半向 +∞，原版 frameRect 原点同一套，index.html:1034）；
    末帧正好落在最后一个键上。单键轨道 → 1 帧。
    """
    last = track.keys[-1].t
    return max(1, js_round(last * track.fps) + 1)


def _mix(a: float, b: float, alpha: float) -> float:
    return a + (b - a) * alpha


def _mix_opt(v0: float | None, v1: float | None, default: float, alpha: float) -> float:
    a = default if v0 is None else v0
    b = default if v1 is None else v1
    return _mix(a, b, alpha)


def sample_track(track: PoseTrack, t: float) -> FramePose:
    """解算时刻 ``t`` 的摆位：定位左右键 → 线性插值 → 缺省回落。

    语义约定（原版没有对应物，这里的行为写进 README）：
    - ``t`` 早于首键 → 用首键；晚于末键 → 停在末键（不外推）；
    - 数值字段（lateral/distance/pitch/roll 及四个可缺省标量）在两键间线性插值；
    - ``mode`` 是离散字段，经过键时刻即切换，不做插值。
    """
    keys = track.keys
    times = [k.t for k in keys]
    i = bisect_right(times, t)
    if i <= 0:
        k0 = k1 = keys[0]
        alpha = 0.0
    elif i >= len(keys):
        k0 = k1 = keys[-1]
        alpha = 0.0
    else:
        k0, k1 = keys[i - 1], keys[i]
        span = k1.t - k0.t
        alpha = (t - k0.t) / span if span > 0 else 0.0
    return FramePose(
        lateral_m=_mix(k0.lateral_m, k1.lateral_m, alpha),
        distance_m=_mix(k0.distance_m, k1.distance_m, alpha),
        pitch_deg=_mix(k0.pitch_deg, k1.pitch_deg, alpha),
        roll_deg=_mix(k0.roll_deg, k1.roll_deg, alpha),
        height_m=_mix_opt(k0.height_m, k1.height_m, track.height_m, alpha),
        feq_mm=_mix_opt(k0.feq_mm, k1.feq_mm, track.feq_mm, alpha),
        subject_height_m=_mix_opt(k0.subject_height_m, k1.subject_height_m,
                                   track.subject_height_m, alpha),
        char_scale=_mix_opt(k0.char_scale, k1.char_scale, track.char_scale, alpha),
        size_pct=_mix_opt(k0.size_pct, k1.size_pct, track.size_pct, alpha),
        mode=k0.mode or track.mode,
        shot=track.shot,
    )


def track_issues(track: PoseTrack) -> list[str]:
    """轨道自检：返回人话问题列表（空 = 没问题）。

    ``check`` 子命令逐条打印；:func:`render_track` 也先过这一关，非空即拒绝渲染
    （原版不静默失败）。阈值见 :data:`PITCH_RANGE` / :data:`ROLL_RANGE` /
    :data:`DISTANCE_RANGE`。

    :func:`load_track` 已经拦过枚举字段，这里再查一遍是因为 ``PoseTrack`` 是普通
    dataclass、可以直接构造：写错的 ``mode``/``frame_key``/``shot`` 若漏过去，
    ``frame_rect`` 会把未知画幅当 0（不裁）、``SHOTS[...]`` 直接 KeyError ——
    都是难查的静默失败，所以在自检关一起拦掉。
    """
    problems: list[str] = []
    if track.fps <= 0:
        problems.append(f"fps 应为正数，得到 {track.fps:g}")
    if track.width < 8 or track.height < 8:
        problems.append(f"画布太小（{track.width}×{track.height}）；原版 compositionHints "
                        f"在 W/H<8 时直接罢工（index.html:1890），这里同样拒绝")
    if track.mode not in ("ground", "free"):
        problems.append(f"mode 只能是 'ground' 或 'free'，得到 {track.mode!r}")
    if track.frame_key not in FRAMES:
        problems.append(f"frame_key 必须是 FRAMES 的中文 key（{', '.join(FRAMES)}），"
                        f"得到 {track.frame_key!r}")
    if track.shot not in SHOTS:
        problems.append(f"shot 必须是 SHOTS 的中文 key（{', '.join(SHOTS)}），"
                        f"得到 {track.shot!r}")
    if not (0.0 < track.height_m <= 50.0):
        problems.append(f"相机高度 {track.height_m:g} m 不在合理范围 (0, 50]")
    if not (0.0 < track.feq_mm <= 500.0):
        problems.append(f"等效焦距 {track.feq_mm:g} mm 不在合理范围 (0, 500]")
    if not (0.0 < track.subject_height_m <= 5.0):
        problems.append(f"人物身高 {track.subject_height_m:g} m 不在合理范围 (0, 5]")
    if not (0.0 < track.char_scale <= 10.0):
        problems.append(f"立绘倍率 {track.char_scale:g} 不在合理范围 (0, 10]")
    if not (0.0 < track.size_pct <= 1000.0):
        problems.append(f"立绘占比 {track.size_pct:g}% 不在合理范围 (0, 1000]")
    for i, key in enumerate(track.keys):
        if i and key.t <= track.keys[i - 1].t:
            problems.append(f"keys[{i}] 时间戳 {key.t:g}s 早于上一键 "
                            f"{track.keys[i - 1].t:g}s —— 键必须按时间严格递增（不允许重复）")
        if not PITCH_RANGE[0] <= key.pitch_deg <= PITCH_RANGE[1]:
            problems.append(f"keys[{i}] 俯角 {key.pitch_deg:g}° 超出 "
                            f"[{PITCH_RANGE[0]:g}, {PITCH_RANGE[1]:g}]")
        if not ROLL_RANGE[0] <= key.roll_deg <= ROLL_RANGE[1]:
            problems.append(f"keys[{i}] 滚转角 {key.roll_deg:g}° 超出 "
                            f"[{ROLL_RANGE[0]:g}, {ROLL_RANGE[1]:g}]")
        if not DISTANCE_RANGE[0] < key.distance_m <= DISTANCE_RANGE[1]:
            problems.append(f"keys[{i}] 距离 {key.distance_m:g} m 超出 "
                            f"({DISTANCE_RANGE[0]:g}, {DISTANCE_RANGE[1]:g}]")
        for name in ("height_m", "feq_mm", "subject_height_m",
                     "char_scale", "size_pct"):
            value = getattr(key, name)
            if value is not None and value <= 0:
                problems.append(f"keys[{i}] {name} 应为正数，得到 {value:g}")
    return problems


# ---------- 图片 IO（PIL，RGB/RGBA uint8 ↔ float64 [0,1]） ----------

def _to_pil(arr: np.ndarray) -> Image.Image:
    a = np.asarray(arr)
    if a.dtype != np.uint8:
        a = np.clip(a.astype(np.float64), 0.0, 1.0) * 255.0 + 0.5
        a = a.astype(np.uint8)
    if a.ndim != 3 or a.shape[2] not in (3, 4):
        raise ValueError(f"图片数组应为 (H,W,3) 或 (H,W,4)，得到 {a.shape}")
    return Image.fromarray(np.ascontiguousarray(a), "RGB" if a.shape[2] == 3 else "RGBA")


def _read_image(path) -> np.ndarray:
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"找不到图片：{p}")
    with Image.open(p) as im:
        return np.asarray(im.convert("RGB"), dtype=np.uint8)


def image_to_rgb01(path) -> np.ndarray:
    """读图片 → float64 RGB，已归一化到 [0,1]（对应原版把视频帧铺进 canvas 的那一步）。"""
    return _read_image(path).astype(np.float64) / 255.0


def image_to_rgba01(path) -> np.ndarray:
    """读图片 → float64 RGBA，已归一化到 [0,1]（立绘；无 alpha 的图补 255）。"""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"找不到图片：{p}")
    with Image.open(p) as im:
        rgba = np.asarray(im.convert("RGBA"), dtype=np.uint8)
    return rgba.astype(np.float64) / 255.0


def encode_png(arr: np.ndarray) -> bytes:
    """PNG 编码（无损、带 alpha —— 素材包的立绘层必须是 PNG，index.html:3843–3846）。"""
    buf = BytesIO()
    _to_pil(arr).save(buf, format="PNG")
    return buf.getvalue()


def encode_jpeg(arr: np.ndarray, quality: int = 95) -> bytes:
    """JPEG 编码，质量默认 0.95（对齐快门/素材包的 toBlobP q0.95，index.html:3803）。"""
    buf = BytesIO()
    _to_pil(arr).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def save_png(arr: np.ndarray, path) -> None:
    """存 PNG（帧序列/立绘层/抠图输出都走这里）。"""
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    _to_pil(arr).save(p, format="PNG")


# ---------- 背景（单图 / 帧目录） ----------

def _fit_rgb01(arr: np.ndarray, track: PoseTrack) -> np.ndarray:
    """背景 → 轨道画布尺寸的 float64 [0,1]。

    收两种 dtype：**uint8**（0..255，:func:`_read_image` 的产物）与 **float64
    [0,1]**（:func:`image_to_rgb01` 的产物 —— 调用方很自然会把它直接喂进来）。
    浮点入参先量化回 uint8 再走后面的缩放/归一，与 matting 预处理同一量化时点；
    不这么做的话 PIL 的 ``fromarray`` 根本不认 3 通道 float64（TypeError），
    而尺寸恰好一致时又会被静默多除一次 255（整张背景近黑）。
    """
    a = np.asarray(arr)
    if a.ndim != 3 or a.shape[2] not in (3, 4):
        raise ValueError(f"背景图片应为 (H,W,3) 或 (H,W,4)，得到 {a.shape}")
    if a.shape[2] == 4:
        a = a[..., :3]
    if np.issubdtype(a.dtype, np.floating):
        a = (np.clip(a, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    if a.shape[0] != track.height or a.shape[1] != track.width:
        img = Image.fromarray(np.ascontiguousarray(a)).resize(
            (track.width, track.height), Image.BILINEAR)
        a = np.asarray(img, dtype=np.uint8)
    return a.astype(np.float64) / 255.0


def _resolve_bg(bg_source, track: PoseTrack) -> tuple[list[Path] | None, np.ndarray | None]:
    """把 bg_source 归一成「帧文件列表」或「单张静止背景」。

    目录按文件名排序（frame_%06d.png 天然有序）。
    """
    if isinstance(bg_source, np.ndarray):
        return None, _fit_rgb01(bg_source, track)
    p = Path(bg_source)
    if not p.exists():
        raise ValueError(f"背景不存在：{p}")
    if p.is_dir():
        files = sorted(q for q in p.iterdir()
                       if q.is_file() and q.suffix.lower() in FRAME_SUFFIXES)
        if not files:
            raise ValueError(f"背景帧目录里没有图片：{p}（支持 {', '.join(FRAME_SUFFIXES)}）")
        return files, None
    return None, _fit_rgb01(_read_image(p), track)


def _write_gif(frame_paths: list[Path], path: Path, fps: float) -> None:
    """PNG 帧序列 → 动画 GIF（纯 PIL；逐帧自适应调色板）。

    GIF 的 256 色限制对合成图是硬约束：全局调色板在光线变化大的轨道上会出色带，
    所以每帧独立 ``quantize``（Fast OCTree）。``duration`` 取 1000/fps 毫秒
    （GIF 以 1/100s 为单位，PIL 会自行取整）；``loop=0`` 无限循环。
    """
    from PIL import Image
    duration_ms = max(20, int(round(1000.0 / max(fps, 0.001))))
    frames: list[Image.Image] = []
    for p in frame_paths:
        with Image.open(p) as im:
            frames.append(im.convert("RGB").quantize(colors=256, method=Image.FASTOCTREE))
    if not frames:
        return
    frames[0].save(path, format="GIF", save_all=True, append_images=frames[1:],
                   duration=duration_ms, loop=0, optimize=False)


def _open_mp4(path: Path, fps: float):
    """懒加载 imageio 开一个 mp4 writer；不可用/失败一律返回 None（只出 PNG）。

    imageio 与 imageio-ffmpeg 都是可选依赖（pyproject 的 video extra），
    没装不报错，只提示。
    """
    try:
        import imageio.v2 as iio
    except Exception:
        return None
    for extra in ({"macro_block_size": None}, {}):
        try:
            return iio.get_writer(path, fps=fps, codec="libx264", quality=8,
                                  pixelformat="yuv420p", **extra)
        except TypeError:
            continue
        except Exception:
            return None
    return None


def render_track(bg_source: str | Path | np.ndarray, track: PoseTrack,
                 char_rgba01: np.ndarray, out_dir: str | Path,
                 *, tone: bool = False, gif: bool = False) -> list[Path]:
    """按轨道逐帧渲染：每帧解算摆位 → 走 compose_shot → 写 PNG 序列。

    :param bg_source: 背景。单张图片路径、帧目录（按文件名排序）、已就绪的
        RGB uint8 数组或 float64 [0,1] 数组（每帧复用）。尺寸与轨道不一致时
        缩放到轨道画布（双线性）。
    :param track:     位姿轨道（键间线性插值，见 :func:`sample_track`）。
    :param char_rgba01: 立绘 RGBA，float64 已归一化到 [0,1]。
    :param out_dir:   输出目录；帧命名 ``frame_%06d.png``，从 1 开始。
    :param tone:      色调匹配开关，透传给 compose_shot（原版 TONE_ON=!!src，
        只有照片路径生效，index.html:3690）。
    :param gif:      追加写动画 GIF（``video.gif``，纯 PIL、零依赖）。GIF 只有
        256 色，逐帧自适应调色板（每帧独立 quantize）——比全局调色板耐得住
        光线变化大的轨道；要更小/更通用的文件用 mp4（需 imageio-ffmpeg）。
    :return: 写出的 PNG 帧路径列表（按帧序）。

    与原版的差异（如实记录，README 同步）：
    - 原版一次只导一张图，轨道/逐帧渲染是离线新增；
    - 帧数 = :func:`frame_count`（闭区间到末键）；
    - 帧目录的帧数少于轨道帧数时，最后一帧一直 hold；多于轨道帧数时多余的忽略
      （时间轴由轨道定义）；
    - imageio 可用时追加写 ``video.mp4``（libx264 yuv420p，懒加载，装了才写）。
    """
    problems = track_issues(track)
    if problems:
        raise ValueError("轨道有问题，已拒绝渲染：\n  - " + "\n  - ".join(problems))
    if char_rgba01 is None:
        raise ValueError("渲染需要一张立绘（RGBA）")
    char = np.asarray(char_rgba01)
    if char.ndim != 3 or char.shape[2] != 4:
        raise ValueError(f"立绘应为 (H,W,4) RGBA，得到 {char.shape}")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    bg_files, bg_still = _resolve_bg(bg_source, track)
    n_frames = frame_count(track)
    writer = _open_mp4(out / MP4_NAME, track.fps)
    written: list[Path] = []

    try:
        for i in range(n_frames):
            t = i / track.fps
            pose = sample_track(track, t)
            if bg_still is not None:
                src01 = bg_still
            else:
                assert bg_files is not None
                src01 = _fit_rgb01(_read_image(bg_files[min(i, len(bg_files) - 1)]), track)
            shot = compose_shot(
                src01, char, pose.subject(), pose.camera(track.width, track.height),
                frame_key=track.frame_key, mode=pose.mode, size_pct=pose.size_pct,
                char_scale=pose.char_scale, tone=tone, shot=pose.shot,
            ).shot
            frame_path = out / (FRAME_FMT % (i + 1))
            save_png(shot, frame_path)
            written.append(frame_path)
            if writer is not None:
                try:
                    writer.append_data(np.ascontiguousarray(shot))
                except Exception:
                    writer = None
                    print("警告：mp4 写入失败，已保留 PNG 序列（检查 imageio-ffmpeg 是否可用）",
                          flush=True)
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                writer = None
    if gif and written:
        _write_gif(written, out / GIF_NAME, track.fps)
    if not (out / MP4_NAME).exists():
        print("提示：未安装 imageio / imageio-ffmpeg，仅输出 PNG 序列；"
              "pip install imageio imageio-ffmpeg 可追加 mp4", flush=True)
    return written
