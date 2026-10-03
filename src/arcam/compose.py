"""合成入口：把立绘按透视落位画进照片帧，并按画幅裁切出图（composeShot）。

原版六步（index.html:3673-3701）在移植侧被拆成两个公开入口：
:func:`compose_shot`（照片路径，``src`` 有效）与 :func:`compose_char_layer`
（立绘层路径，``composeShot(null, LAST_SHOT_LIGHT)``，index.html:3709）。
两者共用同一个内部引擎，保证"同一个裁切框、同一套光"，与原件"两张图逐像素对应"
的承诺一致。

已如实保留的原版行为 / 已知偏离：
- 原版 ``TONE_ON = !!src``：色调匹配**只发生在照片路径**；移植侧再叠加调用方的
  ``tone`` 开关，即 ``tone and src is not None`` 才匹配。
- 原版第 4 步先 ``draw3DLayer`` 再回退 ``drawCharTo``（只有 3D 层走
  toneMatchedLayer）；移植侧没有 3D 运行时，统一走 2D 落位，tone 匹配因此作用在
  2D 立绘层上。
- **立绘层的透明底怎么来**：原版 ``composeShot(null, …)`` 是往一张空 canvas 上画，
  天然带 alpha；移植侧 ``composite_sprite`` 的入参/出参都是 RGB 画布
  （SPEC §3.5），没有"透明画布"可传。做法是把同一套 (阴影+立绘) 以 source-over
  合成到**黑底**上得到预乘色，再用第二次同变换合成取回累加后的 alpha，最后反预乘
  —— 只依赖 source-over 这一条混合语义，不依赖 P2 是否支持 RGBA 底。
- 原版阴影色吃全局 ``LIGHT``（预览平滑值）；移植侧改为显式传
  ``LightSample.mean``（照片路径就是刚采到的那一份，语义是"出图即所用"）。
- 原版 ``drawContactShadow`` 在自由模式直接 return（不知道地面在哪）；移植侧在
  compose 这一层按 ``mode == 'ground'`` 决定铺不铺阴影。
- 缩放滤波：原版 ``imageSmoothingQuality='high'`` → 双线性，README 记录。

LiDAR 环境接入（本文件追加，契约 = ``docs/LIDAR-SPEC.md`` §4）：``compose_shot`` /
``compose_char_layer`` 追加**带默认值**的 ``depth`` / ``env_overrides`` 参数，
``depth`` 非 None 时按 LIDAR-SPEC §0 优先级（显式 > 深度测出 > 标称默认）合并出
effective Camera/Subject，合并逻辑抽成模块级 :func:`_merge_env` 供单测；遮挡区
在立绘层 alpha 上置 0（照片路径与立绘层路径都抠，SPEC §4③）。``depth`` 为 None
（不带深度）时所有行为与追加前逐位相同。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from arcam.frames import frame_rect
from arcam.geometry import Camera, project
from arcam.lighting import LightSample, bake_lit, measure_region
from arcam.numeric import Rect, js_round
from arcam.placement import SpritePlacement, Subject, current_placement
from arcam.sprite import composite_sprite, contact_shadow_layer
from arcam.tone import tone_matched_layer

if TYPE_CHECKING:                                  # 仅标注期引用：arcam.depth/plane 属
    from arcam.depth import LiDARFrame              # LiDAR 接入层（L1/L2），懒加载，
    from arcam.plane import CameraEnv               # 不依赖它们是否已安装

#: 原版 measureRegion 的 N（index.html:3687：出图采样固定 32 级）
LIGHT_LEVELS: int = 32
#: 原版 ``S.frame`` 的默认值（index.html:925）—— ``compose_char_layer`` 的签名里
#: 没有画幅参数，只能取这个默认值；换画幅请走 ``compose_shot``。
DEFAULT_FRAME: str = '满幅'

_MODES = ("ground", "free")

#: ``env_overrides`` 认得的键 → 合并目标（LIDAR-SPEC §4）。h/pitch/roll 走 Camera，
#: d/x 走 Subject；hp/feq 不参与合并（LiDAR 测不出，永远取调用方给的标称值）。
_ENV_CAM_KEYS: tuple[str, ...] = ('h', 'pitch', 'roll')
_ENV_SUBJ_KEYS: tuple[str, ...] = ('d', 'x')
_ENV_OVERRIDE_KEYS: tuple[str, ...] = _ENV_CAM_KEYS + _ENV_SUBJ_KEYS


@dataclass(frozen=True)
class ComposedShot:
    """一次合成的产物（原版 ``composeShot`` 返回值三元组 + 立绘层）。

    Attributes:
        shot: RGB uint8 出图（``off`` 画布，素材包里 JPEG 用的就是它）。
        rect: 实际裁切框 ``R``（浮点宽高、整数原点，同 ``frameRect``）。
        light: 这一遍实际用到的光。照片路径调用方没给就是当场从这一帧量的
            （index.html:3683-3687）；立绘层路径由调用方给。``None`` 语义一致。
        char_layer: RGBA uint8 立绘层，与 ``shot`` 同尺寸同裁切框，
            直接盖在 ``shot.jpg`` 上严丝合缝（原版素材包 ``character_*.png``）。
            **没有做过色调匹配**（原版 ``TONE_ON = !!src``，立绘层路径为 false）。
        env: 本次合成实际用到的相机环境量（LIDAR-SPEC §4 诊断字段）。``depth``
            为 None 时恒为 None；给了深度则是这一遍落位所用的 CameraEnv。
    """

    shot: np.ndarray
    rect: Rect
    light: LightSample | None
    char_layer: np.ndarray | None
    env: CameraEnv | None = None


def compose_shot(src_rgb01: np.ndarray | None, char_rgba01: np.ndarray | None,
                 subj: Subject, cam: Camera, *, frame_key: str,
                 light: LightSample | None = None, mode: str = 'ground',
                 size_pct: float = 60.0, char_scale: float = 1.0,
                 rot_basis: str = 'world', tone: bool = False,
                 shot: str = '全身', edge_bar: int = 0,
                 depth: LiDARFrame | None = None,
                 env_overrides: dict | None = None) -> ComposedShot:
    """composeShot 六步（index.html:3673-3701）。

    ① ``R = frameRect(frame_key)``
    ② 铺源帧：``src_rgb01`` 非 None 时整屏铺满（原版 ``g.drawImage(src,0,0,W,H)``）；
       None 时画布透明（立绘层路径）
    ③ 采光：``light`` 参数优先；否则**仅照片路径**当场从这一帧量
       （``lightRegion`` → ``clampRegion`` → ``measureRegion(…, 32)``）
    ④ 画立绘：``TONE_ON = !!src``（色调匹配只照片路径，且 ``tone`` 开）→
       ``bake_lit``（``light`` 有值才烘）→ 先铺阴影再画立绘
    ⑤ 小数偏移裁切：``off`` 尺寸 ``max(1, js_round(R.w)) × max(1, js_round(R.h))``，
       ``drawImage(full, -R.x, -R.y)``
    ⑥ 返回 ``{shot, rect, light, char_layer}``；``char_layer`` 是同一裁切框、
       同一套光下的透明立绘层（等价 ``compose_char_layer(char, …, light=m)``）

    Args:
        src_rgb01: 照片帧，float64 (H,W,3)，H/W 必须等于 ``cam.height/cam.width``。
            ``None`` 表示立绘层路径（不铺帧、不采现场光）。
        char_rgba01: 立绘，float64 (H,W,4)；``None`` 表示没有立绘。
        subj: 人物位姿（身高/距离/横向）。
        cam: 相机（画布尺寸、离地高、焦距、俯角、滚转）。
        frame_key: FRAMES 的中文 key。
        light: 预置光（立绘层路径必填，否则镜头没接上）。
        mode: ``'ground'`` 贴地 / ``'free'`` 自由。
        size_pct: 自由模式立绘占画面高度百分比。
        char_scale: 贴地模式大小系数（只乘投影跨度，位置不动）。
        rot_basis: ``'world'`` 跟世界竖直 / ``'frame'`` 永远画面竖直。
        tone: 色调匹配开关（照片路径才可能生效，见 ``TONE_ON``）。
        shot: 景别（SHOTS key），决定贴地模式下立绘下沿离地高度（自由模式忽略）。
        edge_bar: 框条吸附编号 0–4（自由模式生效，贴地模式忽略）。
        depth: LiDAR 深度帧（LIDAR-SPEC §4）。非 None 时本遍改用**测出**的相机
            环境量（地面平面拟合 → h/pitch/roll）与人物纵深（公告板足底处反解
            d/x），并按 §0 优先级与显式值/标称默认合并；``None`` = 不接深度，
            行为与追加本参数前逐位相同。
        env_overrides: 显式环境量，形如 ``{'h': 1.7}``（键见
            :data:`_ENV_OVERRIDE_KEYS`）。显式值优先于深度测出值（LIDAR-SPEC §0）；
            仅 ``depth`` 或本参数非空时参与合并。argparse 侧由 CLI 负责只把用户
            真显式给过的键放进来（compose 侧不解析 argv）。

    Returns:
        ComposedShot（``env`` 字段带回本遍实际用到的 CameraEnv，诊断用）。
    """
    env = None
    if depth is not None or env_overrides:
        env = _lidar_env(depth) if depth is not None else None
        cam, subj, _notes = _merge_env(cam, subj, env_overrides, env,
                                       depth=depth, mode=mode, shot=shot)
    rect, light_used, shot_rgba = _engine(
        src_rgb01, char_rgba01, subj, cam, frame_key,
        light=light, mode=mode, size_pct=size_pct, char_scale=char_scale,
        rot_basis=rot_basis, tone=tone, shot=shot, edge_bar=edge_bar,
        depth=depth)
    # 立绘层与照片**同一个裁切框**：同一份 frame_key、同一套光，两张图才逐像素对应
    _, _, layer_rgba = _engine(
        None, char_rgba01, subj, cam, frame_key,
        light=light_used, mode=mode, size_pct=size_pct, char_scale=char_scale,
        rot_basis=rot_basis, tone=False, shot=shot, edge_bar=edge_bar,
        depth=depth)
    return ComposedShot(shot=shot_rgba[..., :3], rect=rect,
                        light=light_used, char_layer=layer_rgba, env=env)


def compose_char_layer(char_rgba01: np.ndarray, subj: Subject, cam: Camera, *,
                       light: LightSample | None = None, mode: str = 'ground',
                       size_pct: float = 60.0, char_scale: float = 1.0,
                       rot_basis: str = 'world', tone: bool = False,
                       shot: str = '全身', edge_bar: int = 0,
                       depth: LiDARFrame | None = None,
                       env_overrides: dict | None = None
                       ) -> tuple[np.ndarray, Rect]:
    """buildCharCanvas（index.html:3709-3712）：同变换、不铺帧的透明立绘层。

    就是 ``composeShot(null, LAST_SHOT_LIGHT)`` —— 同一个裁切框、同一套光，
    尺寸与照片逐像素相同，直接盖在 ``shot_*.jpg`` 上严丝合缝；摆位、裁切、贴地
    倾角都已烘进去，所以它**不是**原始立绘文件。

    Args:
        char_rgba01: 立绘，float64 (H,W,4)。
        subj: 人物位姿。
        cam: 相机。
        light: 与照片**同一套**的光（原版 ``LAST_SHOT_LIGHT``）。
        mode: ``'ground'`` / ``'free'``。
        size_pct: 自由模式立绘占画面高度百分比。
        char_scale: 贴地模式大小系数。
        rot_basis: 对正基准。
        tone: 立绘层路径恒不匹配（``TONE_ON = !!src`` 为 false）；参数只为签名对称。
        shot: 景别（SHOTS key），贴地模式决定立绘下沿离地高度。
        edge_bar: 框条吸附编号 0–4（自由模式生效）。
        depth: LiDAR 深度帧（LIDAR-SPEC §4）。非 None 时本层也按测出的环境量
            落位，并把遮挡区 alpha 置 0（SPEC §4③：立绘层路径同样抠）。
        env_overrides: 显式环境量（同 :func:`compose_shot`），优先于深度测出值。

    Returns:
        ``(rgba_layer_uint8, rect)``，层尺寸与 rect 的取整尺寸一致。
    """
    env = None
    if depth is not None or env_overrides:
        env = _lidar_env(depth) if depth is not None else None
        cam, subj, _notes = _merge_env(cam, subj, env_overrides, env,
                                       depth=depth, mode=mode, shot=shot)
    rect, _, layer_rgba = _engine(
        None, char_rgba01, subj, cam, DEFAULT_FRAME,
        light=light, mode=mode, size_pct=size_pct, char_scale=char_scale,
        rot_basis=rot_basis, tone=False, shot=shot, edge_bar=edge_bar,
        depth=depth)
    return layer_rgba, rect


def _engine(src_rgb01: np.ndarray | None, char_rgba01: np.ndarray | None,
            subj: Subject, cam: Camera, frame_key: str, *,
            light: LightSample | None, mode: str, size_pct: float,
            char_scale: float, rot_basis: str, tone: bool,
            shot: str = '全身', edge_bar: int = 0,
            depth: LiDARFrame | None = None
            ) -> tuple[Rect, LightSample | None, np.ndarray]:
    """composeShot 的公共引擎：返回 ``(rect, light_used, rgba_uint8)``。

    ``frame_key`` 由调用方给出（立绘层路径固定 ``DEFAULT_FRAME``）—— 照片与立绘层
    两条路共用同一套取景/落位/裁切代码，避免"两处迟早改漏一处"。
    出图已经按 ``R`` 裁好：照片路径取 RGB 通道，立绘层路径连 alpha 一起用。

    ``depth`` 非 None（LIDAR-SPEC §4）时按 effective ``cam``（本函数入参，已由
    调用方合并好）算遮挡掩码。照片路径与立绘层路径**各调一遍**本函数，遮挡因此
    两条路都抠——立绘层里的公告板被挡区 alpha 同样置 0。
    """
    if mode not in _MODES:
        raise ValueError(f"mode 必须是 {_MODES} 之一，收到 {mode!r}")
    if cam.width < 1 or cam.height < 1:
        raise ValueError(f"相机画布尺寸非法：{cam.width}×{cam.height}")

    # ① 取裁切框（frameRect）
    rect = frame_rect(frame_key, cam.width, cam.height)

    # ② 铺源帧：有源且非 None 才整屏铺满；否则整块透明（立绘层路径）
    if src_rgb01 is None:
        base = np.zeros((cam.height, cam.width, 3), dtype=np.float64)
        opaque = False
    else:
        if src_rgb01.shape[0] != cam.height or src_rgb01.shape[1] != cam.width:
            raise ValueError(
                f"src_rgb01 尺寸 {src_rgb01.shape[1]}x{src_rgb01.shape[0]} 与相机画布 "
                f"{cam.width}x{cam.height} 不一致（原版 cv 像素 = 视频像素，1:1）")
        base = np.asarray(src_rgb01, dtype=np.float64)
        opaque = True

    # ③ 采光：调用方给了就用；否则仅照片路径当场量"正要导出的这一帧"
    m = light
    if m is None and opaque:
        region = _light_region_rect(char_rgba01, subj, cam, mode=mode,
                                    size_pct=size_pct, char_scale=char_scale,
                                    rot_basis=rot_basis, shot=shot,
                                    edge_bar=edge_bar)
        m = measure_region(base, region, LIGHT_LEVELS)

    # ④ 画立绘层。原版 draw3DLayer 的做法是"先在帧坐标 (0,0) 上出一张**帧尺寸**
    #    的 RGBA 层，再 toneMatchedLayer 匹配、最后 drawImage 回帧"；移植侧照这个
    #    顺序走，所以立绘层的构建与色调匹配都以帧为尺度。
    c = _placement(char_rgba01, subj, cam, mode=mode, size_pct=size_pct,
                   char_scale=char_scale, rot_basis=rot_basis, shot=shot,
                   edge_bar=edge_bar)
    # E4 遮挡：公告板前方有更近的深度 → 该区立绘 alpha 置 0（LIDAR-SPEC §4③）。
    # 照片路径与立绘层路径都过这里，所以两条路一样抠。cam 是 effective 渲染相机
    # （显式 --h/--pitch/--roll 时即显式值，符合 §0「显式优先」）。
    occ = _occlusion_mask(depth, cam, c)
    rgba = None
    if c is not None and char_rgba01 is not None:
        ambient = m.mean if m is not None else None
        drawn = char_rgba01
        if m is not None:
            drawn = bake_lit(drawn, m, (js_round(c.w), js_round(c.h)))
        # 阴影是"场景"的一部分：自由模式不知道地面在哪，不铺（index.html:3131）
        shadow_layer = None
        if mode == 'ground':
            shadow_layer = contact_shadow_layer(char_rgba01, c, ambient)
        draw_shadow = shadow_layer is not None
        kw = dict(shadow=draw_shadow, ambient=ambient, shadow_layer=shadow_layer)
        black = np.zeros((cam.height, cam.width, 3), dtype=np.float64)
        white = np.full((cam.height, cam.width, 3), 1.0, dtype=np.float64)
        # 黑底上 source-over 的结果 = 预乘色；白底再合成一次反解出累加 alpha
        on_black = _to_float01(composite_sprite(black, drawn, c, **kw))
        on_white = _to_float01(composite_sprite(white, drawn, c, **kw))
        alpha = _alpha_from(on_black, on_white)
        if occ is not None:
            # 遮挡区 alpha 归零、RGB 清零：on_black 清零后照片路径该处露出原照片，
            # 立绘层路径 alpha=0（_unpremultiply 在 alpha≤0 处本就出 0，无半透明残留）
            alpha = np.where(occ, 0.0, alpha)
            on_black = np.where(occ[..., None], 0.0, on_black)
        layer = np.concatenate([_unpremultiply(on_black, alpha), alpha[..., None]],
                               axis=2)
        # TONE_ON = !!src：只有照片路径做色调匹配，且 tone 开关打开
        matched = tone_matched_layer(layer, base) if (tone and opaque) else None
        if opaque:
            if matched is not None:
                base = _over(base, matched)
            else:
                # 没做色调匹配时直接用预乘色回贴：与"一次 compositing 到底"逐位相同，
                # 免得反预乘/回乘那一圈的舍入把立绘边缘磨掉一两级
                base = base * (1.0 - alpha[..., None]) + on_black
        else:
            # 立绘层路径（src 为 null）：画布本身就是这张层，TONE_ON 恒 false
            rgba = matched if matched is not None else layer
    if rgba is None:
        rgba = np.zeros((cam.height, cam.width, 4), dtype=np.float64)
        rgba[..., :3] = base[..., :3]
        rgba[..., 3] = 1.0 if opaque else 0.0

    # ⑤ 小数偏移裁切（off 画布；越界目标像素保持透明黑，对应 JPEG 的黑边）
    out = _crop_offset(rgba, rect)
    # ⑥ 把这一遍实际用到的光带出去（立绘层必须跟照片用同一套）
    return rect, m, out


def _lidar_env(depth: LiDARFrame) -> CameraEnv:
    """深度帧 → CameraEnv（LIDAR-SPEC §4①：fit_ground_plane → camera_env_from_plane）。

    ARCKit 给了横滚（``depth.roll_deg``）就交给 ``camera_env_from_plane``（先反旋
    再解 pitch）；没给则由平面法向同时分解 pitch/roll（§3）。本函数只管按契约把
    两步串起来，反投影/拟合/分解的数学都在 arcam.plane，compose 不各自推导。
    """
    from arcam.plane import camera_env_from_plane, fit_ground_plane

    plane = fit_ground_plane(depth)
    return camera_env_from_plane(plane, roll_deg=depth.roll_deg)


def _occlusion_mask(depth: LiDARFrame | None, cam: Camera,
                    c: SpritePlacement | None) -> np.ndarray | None:
    """公告板前方遮挡掩码（H,W bool，True = 该像素应抠掉立绘）；无深度/无落位 → None。

    ``cam`` 须是**渲染相机**（与深度帧对齐到同一画布坐标），契约见
    LIDAR-SPEC §3 ``occlusion_mask`` 第二形参。深度帧分辨率通常与画布不同
    （人像辅图 256×192 vs 照片 3024×4032）——按 §2 对齐约定**最近邻缩放到
    画布尺寸**后交给 plane.occlusion_mask；只用一帧的 stub 尺寸与画布相同时
    才看不出来，真实链路（CLI + 真机照片）必经本对齐。
    """
    if depth is None or c is None:
        return None
    from arcam.depth import align_to_canvas
    from arcam.plane import occlusion_mask

    aligned = align_to_canvas(depth, cam.width, cam.height)
    return occlusion_mask(aligned, cam, c)


def _merge_env(cam: Camera, subj: Subject, overrides: dict | None,
               env: CameraEnv | None, *, depth: LiDARFrame | None = None,
               mode: str = 'ground',
               shot: str = '全身') -> tuple[Camera, Subject, dict]:
    """环境量三态合并：**显式 > 深度测出 > 标称默认**（LIDAR-SPEC §0/§4）。

    Args:
        cam: 标称相机（argparse 默认值那一份；深度来了可被覆盖的只有
            ``height_m`` / ``pitch_deg`` / ``roll_deg``，``feq_mm``/尺寸不动）。
        subj: 标称人物位姿（可被覆盖 ``distance_m`` / ``lateral_m``）。
        overrides: 显式值 dict，键 ∈ :data:`_ENV_OVERRIDE_KEYS`
            （``h``/``pitch``/``roll`` → 相机，``d``/``x`` → 人物）。**值为 None
            视为「没给」**（argparse 侧约定）；未知键 → ValueError（人话列出合法键）。
        env: 深度测出的 CameraEnv（``depth`` 给了才有；None = 只有显式值与标称值）。
        depth: 原始深度帧。给了 env 才用：E3 在公告板足底屏幕位置反解 (lateral, d)。
        mode: ``'free'`` 跳过 E3——自由模式立绘不站在地面上，「足底」无意义；
            h/pitch/roll 的合并与遮挡照旧。
        shot: 景别（签名对称保留；足底位置取世界接地点，与景别无关）。

    Returns:
        ``(camera, subject, notes)``。notes 逐字段记录
        ``{'used', 'source', 'lidar', 'explicit'}``，source ∈
        ``'explicit'|'lidar'|'nominal'``，供 CLI 打印「显式值优先」提示与测试断言。

    E3 语义（LIDAR-SPEC §0 表 E3）：足底屏幕位置 = 标称人物 (x, d) 在**合并后**
    相机下的接地点投影 ``project(x, -h, d)``——即「用户本想让人站在哪儿」；在该
    像素处用 ``sample_subject`` 反解世界 (lateral, d)，覆写标称 d/x（显式除外）。
    覆写后足底屏幕位置会变（摆了新人站位），这是该定义的固有性质：LiDAR 测的是
    「画面这个位置实际上有什么」，不是「人站在哪」。
    """
    ov = dict(overrides or {})
    unknown = [k for k in ov if k not in _ENV_OVERRIDE_KEYS]
    if unknown:
        raise ValueError(
            f"env_overrides 有不认识的键：{sorted(unknown)}（可用："
            f"{list(_ENV_OVERRIDE_KEYS)}）")
    ov = {k: float(v) for k, v in ov.items() if v is not None}

    def pick(key: str, nominal: float,
             measured: float | None) -> tuple[float, dict]:
        if key in ov:
            used, source = ov[key], 'explicit'
        elif measured is not None:
            used, source = float(measured), 'lidar'
        else:
            used, source = float(nominal), 'nominal'
        return used, {'used': used, 'source': source,
                      'lidar': None if measured is None else float(measured),
                      'explicit': ov.get(key)}

    h, note_h = pick('h', cam.height_m, None if env is None else env.height_m)
    pitch, note_p = pick('pitch', cam.pitch_deg,
                         None if env is None else env.pitch_deg)
    roll, note_r = pick('roll', cam.roll_deg,
                        None if env is None else env.roll_deg)
    cam2 = Camera(cam.width, cam.height, h, cam.feq_mm, pitch, roll)

    measured_x: float | None = None
    measured_d: float | None = None
    if depth is not None and env is not None and mode == 'ground':
        from arcam.depth import align_to_canvas
        from arcam.plane import sample_subject

        # E3 在**画布坐标**里的足底像素上取深度：深度帧分辨率通常与画布不同
        # （人像辅图 256×192 vs 照片 3024×4032），按 §2 对齐约定先缩放到画布。
        aligned = align_to_canvas(depth, cam.width, cam.height)
        uv = project(subj.lateral_m, -cam2.height_m, subj.distance_m, cam2)
        if uv is not None:
            measured_x, measured_d = sample_subject(aligned, env, uv[0], uv[1])
    d, note_d = pick('d', subj.distance_m, measured_d)
    x, note_x = pick('x', subj.lateral_m, measured_x)
    subj2 = Subject(subj.height_m, d, x)
    return cam2, subj2, {'h': note_h, 'pitch': note_p, 'roll': note_r,
                         'd': note_d, 'x': note_x}


def _placement(char_rgba01, subj, cam, *, mode, size_pct, char_scale,
               rot_basis, shot='全身', edge_bar=0) -> SpritePlacement | None:
    """currentPlacement 的移植侧封装：按模式分派贴地/自由（index.html:2981）。"""
    if mode == 'free':
        return current_placement(char_rgba01, subj, cam, mode='free',
                                 size_pct=size_pct, edge_bar=edge_bar)
    return current_placement(char_rgba01, subj, cam, mode='ground',
                             char_scale=char_scale, rot_basis=rot_basis,
                             shot=shot)


def _light_region_rect(char_rgba01, subj, cam, *, mode, size_pct, char_scale,
                       rot_basis, shot='全身', edge_bar=0) -> Rect | None:
    """lightRegion + clampRegion（index.html:1254-1260 / 1244-1251）。

    返回画布坐标的采样矩形；无立绘（``currentPlacement`` 为 null）时退化成整帧
    （原版 ``clampRegion`` 的 ``!r`` 分支）。原版 ``lightRegion`` 返回画布坐标、
    ``sampleLight`` 按视频尺寸 clamp；离线两者同尺寸（``cv`` 像素 = 视频像素），
    不通用化。
    """
    c = _placement(char_rgba01, subj, cam, mode=mode, size_pct=size_pct,
                  char_scale=char_scale, rot_basis=rot_basis, shot=shot,
                  edge_bar=edge_bar)
    if c is None:
        return Rect(0.0, 0.0, float(cam.width), float(cam.height))
    mx = c.w * 0.35
    sx = c.u - c.w / 2 - mx
    sy = c.v - c.h / 2 - mx
    sw = c.w + mx * 2
    sh = c.h + mx * 2
    return _clamp_region(sx, sy, sw, sh, cam.width, cam.height)


def _clamp_region(sx: float, sy: float, sw: float, sh: float,
                  width: int, height: int) -> Rect:
    """clampRegion（index.html:1244-1251）：先缩尺寸、再夹原点，顺序不能换。"""
    sw = min(sw, float(width))
    sh = min(sh, float(height))
    sx = max(0.0, min(float(width) - sw, sx))
    sy = max(0.0, min(float(height) - sh, sy))
    return Rect(sx, sy, sw, sh)


def _to_float01(canvas: np.ndarray) -> np.ndarray:
    """把 composite_sprite 的返回统一成 (H,W,3) float01。

    按 SPEC §3.5，``base_rgb01``/``char_rgba01`` 进出都是 ``_rgb01``（0..1 float）；
    这里对 uint8 返回做一次兜底缩放，免得上游换成整数字节时整条链静默失真。
    """
    arr = np.asarray(canvas)
    if arr.ndim != 3 or arr.shape[2] not in (3, 4):
        raise ValueError(f"composite_sprite 返回了无法识别的数组：{arr.shape}")
    if arr.dtype == np.uint8:
        arr = arr.astype(np.float64) / 255.0
    out = np.asarray(arr, dtype=np.float64)
    return out[..., :3]


def _over(base_rgb01: np.ndarray, layer_rgba01: np.ndarray) -> np.ndarray:
    """把一张**帧尺寸** RGBA 层以 source-over 盖回 RGB 底。

    对应原版最后那句 ``g.drawImage(layer, 0, 0)`` —— 落位是标识（不缩放不旋转），
    所以这里直接做 alpha 混合，不借 :func:`arcam.sprite.composite_sprite`，免得
    1:1 缩放被重采样一遍、把立绘边缘糊掉。
    """
    a = layer_rgba01[..., 3:4]
    return base_rgb01 * (1.0 - a) + layer_rgba01[..., :3] * a


def _alpha_from(on_black: np.ndarray, on_white: np.ndarray) -> np.ndarray:
    """(阴影 + 立绘) 叠在**透明底**上之后的累加 alpha（H,W）float01。

    source-over 对任意底 ``B`` 满足 ``result(B) = B·(1-a) + pm``，于是
    ``a = 1 + result(0) - result(1)``（``result(0)`` 就是预乘色）。黑白两次合成走
    **同一个** ``SpritePlacement``、同一份 ``shadow_layer``，几何逐像素一致，
    也不对 shadow_layer 的尺寸/含义做任何假设。三通道各解一次再取中位，消掉
    逐通道舍入噪声。
    """
    est = np.clip(1.0 + on_black - on_white, 0.0, 1.0)
    return np.median(est, axis=2)


def _unpremultiply(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """黑底上 source-over 的结果是预乘色，按 alpha 反预乘回源色（alpha=0 处置 0）。"""
    out = np.zeros_like(rgb)
    solid = alpha > 1e-9
    a = alpha[solid][:, None]
    out[solid] = rgb[solid] / a
    return np.clip(out, 0.0, 1.0)


def _crop_offset(rgba01: np.ndarray, r: Rect) -> np.ndarray:
    """``off`` 画布的 Python 等价：``drawImage(full, -R.x, -R.y)``。

    目标尺寸 ``max(1, js_round(R.w)) × max(1, js_round(R.h))``，偏移照抄 ``R.x/R.y``。
    ``frameRect`` 的原点已取整，故这一步是整数平移；越界的目标像素保持透明黑
    （对应 canvas 上未绘制区，导出 JPEG 时即黑边）。
    """
    out_w = max(1, js_round(r.w))
    out_h = max(1, js_round(r.h))
    src_h, src_w = rgba01.shape[0], rgba01.shape[1]
    out = np.zeros((out_h, out_w, 4), dtype=np.float64)
    ox = js_round(r.x)
    oy = js_round(r.y)
    dx = max(0, -ox)
    dy = max(0, -oy)
    x0 = max(0, ox)
    y0 = max(0, oy)
    x1 = min(src_w, ox + out_w)
    y1 = min(src_h, oy + out_h)
    if x1 > x0 and y1 > y0:
        out[dy:dy + (y1 - y0), dx:dx + (x1 - x0)] = rgba01[y0:y1, x0:x1]
    return (np.clip(out, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
