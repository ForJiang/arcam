"""arcam —— 「让她走出屏幕」的 Python 移植。

浏览器原版（AR 取景器）的离线合成管线等价实现：
透视投影引擎、贴地/自由摆位、接触阴影、环境光匹配、ISNet-anime 抠图、
scene.json 配方、位姿轨道视频渲染。

契约与源码溯源见仓库根目录 docs/SPEC.md（对原版 index.html / cutout.js / mmd.js 的
逐条 file:line 映射）。
"""

from arcam.numeric import Rect, clampn, js_round, js_to_fixed
from arcam.geometry import K_LONG, K_SHORT, Camera, focal_px, place, project
from arcam.placement import (
    FRAMES,
    SHOTS,
    SpritePlacement,
    Subject,
    auto_fix_x,
    current_placement,
    free_placement,
    ground_placement,
    snap_to_edge,
)
from arcam.frames import frame_rect, view_rect
from arcam.lighting import LightSample, bake_lit, char_lum, light_levels, measure_region
from arcam.sprite import composite_sprite, contact_shadow_layer
from arcam.compose import ComposedShot, compose_char_layer, compose_shot
from arcam.scene import build_scene, load_scene, save_scene
from arcam.tone import tone_matched_layer
from arcam.matting import MattingResult, anime_cut
from arcam.video import PoseKey, PoseTrack, load_track, render_track, save_track
from arcam.depth import (
    LiDARFrame,
    align_to_canvas,
    load_depth,
    save_depth,
)
from arcam.plane import (
    CameraEnv,
    DepthStats,
    GroundPlane,
    backproject,
    camera_env_from_plane,
    fit_ground_plane,
    occlusion_mask,
    sample_subject,
)

__all__ = [
    "K_LONG", "K_SHORT", "Camera", "focal_px", "project", "place",
    "FRAMES", "SHOTS", "Subject", "SpritePlacement",
    "ground_placement", "free_placement", "current_placement", "auto_fix_x", "snap_to_edge",
    "Rect", "clampn", "js_round", "js_to_fixed",
    "view_rect", "frame_rect",
    "LightSample", "measure_region", "light_levels", "char_lum", "bake_lit",
    "contact_shadow_layer", "composite_sprite",
    "ComposedShot", "compose_shot", "compose_char_layer",
    "build_scene", "save_scene", "load_scene",
    "tone_matched_layer",
    "MattingResult", "anime_cut",
    "PoseKey", "PoseTrack", "load_track", "save_track", "render_track",
    "LiDARFrame", "load_depth", "align_to_canvas", "save_depth",
    "GroundPlane", "CameraEnv", "DepthStats", "backproject",
    "fit_ground_plane", "camera_env_from_plane", "sample_subject", "occlusion_mask",
    "numeric",
]
