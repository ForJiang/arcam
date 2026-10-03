"""集成测试：全链路验收（编排者所有）。

三条线：
1. 库 API 全链路：project → placement → compose → scene → 落盘 → 读回；
2. CLI 子命令冒烟（render/char-layer/bundle/video/check + `python -m arcam`）；
3. 关键数值锚点：投影 golden 与独立手算一致、scene.json 字段同构。
"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest

from arcam import (
    Camera,
    Subject,
    compose_shot,
    ground_placement,
    place,
    project,
)
from arcam.cli import main
from arcam.compose import compose_char_layer
from arcam.scene import build_scene, load_scene, save_scene

ROOT = Path(__file__).resolve().parents[1]


def _make_char(h: int = 200, w: int = 120) -> np.ndarray:
    """程序化立绘：中立灰 + 底部不透明带（模拟落地），alpha 外圈 0。"""
    img = np.zeros((h, w, 4), dtype=np.float64)
    img[..., 0] = 0.7
    img[..., 1] = 0.7
    img[..., 2] = 0.7
    img[..., 3] = 1.0
    img[:6, :, 3] = 0.0          # 顶部透明 → bake/shadow 有边界可判
    return img


def _make_bg(h: int = 480, w: int = 640) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    base = 0.35 + 0.2 * (yy / h) + 0.1 * (xx / w)
    return np.stack([base, base * 0.96, base * 0.9], axis=-1)


# ---------- 1. 数值锚点（独立手算，不用被测代码推导） ----------

def test_projection_golden_matches_manual_computation():
    """focal/project 手算 golden（公式逐字来自 index.html:1043-1057）。

    画布 640×480（非 wide）：L=640… 实际 max=640,min=480 → 640/480=1.333<4/3? 否——
    640/480 = 4/3 恰等于阈值 → wide。走 K_LONG：
    F = (640/2) * 23 / 17.3064 = 320 * 1.32899...
    """
    cam = Camera(width=640, height=480, height_m=1.6, feq_mm=23.0,
                 pitch_deg=0.0, roll_deg=0.0)
    assert cam.width == 640
    from arcam.geometry import focal_px
    # F = 320 * 23/17.3064
    expect_f = 320.0 * 23.0 / 17.3064
    assert focal_px(cam) == pytest.approx(expect_f, rel=1e-12)

    # pitch=0 时 project(x, -h, d)：u=320, v=240 + F*(d-h)/d*… 手算：
    # Zc = d, Yc = -(-h)*1 - 0 = h? 不——index.html:1051-1052 逐字：
    #   Zc = -y*sin(0) + d*cos(0) = d
    #   Yc = -y*cos(0) - d*sin(0) = -y
    # y=-1.6 → Yc = 1.6
    u, v = project(0.0, -1.6, 4.0, cam)
    assert u == pytest.approx(320.0)
    assert v == pytest.approx(240.0 + expect_f * 1.6 / 4.0)

    # 相机正后方 → None
    assert project(0.0, -1.6, 0.01, cam) is None


def test_focal_short_side_branch():
    """非 wide 画布（高>宽的竖屏手机）：F 走 K_SHORT。"""
    cam = Camera(width=720, height=1280, height_m=1.6, feq_mm=23.0,
                 pitch_deg=0.0, roll_deg=0.0)
    from arcam.geometry import focal_px
    # L=1280, Sd=720, 1280/720=1.778 ≥ 4/3 → 仍是 wide！
    # 竖屏 720×1280 长边 1280 在 H → 走 K_LONG 的 (L/2)=640
    assert focal_px(cam) == pytest.approx(640.0 * 23.0 / 17.3064)
    # 真正的 short 分支：1.1:1 画布
    cam2 = Camera(width=1000, height=900, height_m=1.6, feq_mm=23.0,
                  pitch_deg=0.0, roll_deg=0.0)
    assert focal_px(cam2) == pytest.approx(450.0 * 23.0 / 12.9798)


def test_place_roundtrip_pitch_roll():
    """project↔place 往返（原版声明 3.6e-15；离线同量级）。

    ⚠ place 是 project 在 **y 固定 = -cam.height_m**（地面点）下的解析逆
    （index.html:3186-3198），所以往返测试必须投 y=-h 的点。
    """
    cam = Camera(width=1920, height=1080, height_m=1.7, feq_mm=28.0,
                 pitch_deg=-15.0, roll_deg=22.0)
    for x, d in ((-0.6, 2.5), (1.2, 5.0), (0.0, 3.0)):
        uv = project(x, -cam.height_m, d, cam)
        assert uv is not None
        x2, d2 = place(uv[0], uv[1], cam)
        assert x2 is not None and d2 is not None
        assert (x2, d2) == pytest.approx((x, d), abs=1e-8)


# ---------- 2. 库 API 全链路 ----------

def test_full_pipeline_lib(tmp_path: Path):
    char01 = _make_char()
    bg = _make_bg()
    subj = Subject(height_m=1.65, distance_m=4.0, lateral_m=-0.4)
    cam = Camera(width=640, height=480, height_m=1.6, feq_mm=23.0,
                 pitch_deg=8.0, roll_deg=0.0)

    c = ground_placement(char01, subj, cam)
    assert c is not None
    # 锚点在足底投影附近（pitch 小 → 人站在画面中心偏下）
    assert 250 < c.v < 420
    assert c.w == pytest.approx(c.h * 0.6)      # 120/200 宽高比

    shot = compose_shot(bg, char01, subj, cam, frame_key='满幅')
    assert shot.shot.shape == (480, 640, 3)
    assert shot.rect.x == 0 and shot.rect.w == 640
    assert shot.char_layer is not None
    assert shot.char_layer.shape == (480, 640, 4)

    # 立绘层单独出：同一变换、同一套光 → 覆盖回照片应仍严丝合缝
    layer, rect = compose_char_layer(char01, subj, cam, light=shot.light)
    assert layer.shape == (480, 640, 4)
    assert rect == shot.rect

    scene = build_scene(shot.rect, c, subj, cam, frame_key='满幅',
                        char_rgba01=char01, char_name='c.png', mode='ground',
                        shot='全身', char_scale=1.0)
    assert list(scene) == ['version', 'image', 'frame', 'camera', 'subject',
                           'ground', 'shot', 'charScale', 'edge', 'character', 'mmd']
    assert scene['subject']['footPixel'] is not None
    # footPixel 应与 ground 锚点同源（pitch=0、x≠0 时 u ≠ 中心，逐位比）
    fp = scene['subject']['footPixel']
    fp2 = project(subj.lateral_m, -cam.height_m, subj.distance_m, cam)
    assert fp['u'] == pytest.approx(fp2[0])
    assert fp['v'] == pytest.approx(fp2[1])

    p = tmp_path / 'scene.json'
    save_scene(scene, p)
    back = load_scene(p)
    assert back == scene
    raw = json.loads(p.read_text(encoding='utf-8'))
    assert raw['camera']['focalPx'] == round(raw['camera']['focalPx'], 2)


# ---------- 3. CLI 冒烟（与 accept 手工验证同参数） ----------

def _write_png(path: Path, arr: np.ndarray) -> None:
    from PIL import Image
    Image.fromarray(arr).save(path)


def test_cli_render_and_scene(tmp_path: Path):
    from PIL import Image
    bg_path = tmp_path / 'bg.jpg'
    char_path = tmp_path / 'char.png'
    Image.fromarray((_make_bg() * 255).astype('uint8')).save(bg_path, quality=92)
    Image.fromarray((_make_char() * 255).astype('uint8')).save(char_path)

    out = tmp_path / 'out.png'
    scene_path = tmp_path / 'scene.json'
    rc = main(['render', str(bg_path), str(char_path), '-o', str(out),
               '--scene', str(scene_path), '--shot', '腰上', '--char-scale', '1.2',
               '--pitch', '10'])
    assert rc == 0
    assert out.is_file()
    scene = json.loads(scene_path.read_text(encoding='utf-8'))
    assert scene['version'] == 1
    assert scene['shot'] == '腰上' and scene['charScale'] == 1.2
    assert scene['camera']['pitchDeg'] == 10.0


def test_cli_char_layer_and_bundle(tmp_path: Path):
    from PIL import Image
    char_path = tmp_path / 'char.png'
    Image.fromarray((_make_char() * 255).astype('uint8')).save(char_path)
    bg_path = tmp_path / 'bg.jpg'
    Image.fromarray((_make_bg() * 255).astype('uint8')).save(bg_path, quality=92)

    layer = tmp_path / 'layer.png'
    rc = main(['char-layer', str(char_path), '-o', str(layer), '--mode', 'free',
               '--size-pct', '70', '--edge-bar', '1',
               '--width', '640', '--height', '480'])
    assert rc == 0
    got = np.asarray(Image.open(layer))
    assert got.shape == (480, 640, 4)

    pack = tmp_path / 'pack.zip'
    rc = main(['bundle', str(bg_path), str(char_path), '-o', str(pack)])
    assert rc == 0
    with zipfile.ZipFile(pack) as z:
        names = z.namelist()
        assert sorted(names) == ['character.png', 'scene.json', 'shot.jpg']
        scene = json.loads(z.read('scene.json').decode('utf-8'))
        assert scene['character'] is not None


def test_cli_video_and_check(tmp_path: Path):
    track = {
        'width': 320, 'height': 240, 'fps': 12,
        'keys': [{'t': 0.0, 'lateral_m': 0.0, 'distance_m': 4.0,
                  'pitch_deg': 0.0, 'roll_deg': 0.0},
                 {'t': 1.0, 'lateral_m': 0.0, 'distance_m': 4.0,
                  'pitch_deg': 20.0, 'roll_deg': 0.0}],
    }
    tp = tmp_path / 't.json'
    tp.write_text(json.dumps(track), encoding='utf-8')
    assert main(['check', str(tp)]) == 0

    bad = dict(track)
    bad['keys'] = list(reversed(bad['keys']))
    bp = tmp_path / 'bad.json'
    bp.write_text(json.dumps(bad), encoding='utf-8')
    assert main(['check', str(bp)]) != 0

    char_path = tmp_path / 'char.png'
    from PIL import Image
    Image.fromarray((_make_char(200, 120) * 255).astype('uint8')).save(char_path)
    frames = tmp_path / 'frames'
    rc = main(['video', str(tp), str(char_path), '-o', str(frames),
               '--bg', str(tmp_path / 'bg.jpg')])
    # bg 不存在 → 人话报错、非 0
    assert rc == 1


def test_cli_module_entrypoint(tmp_path: Path):
    """python -m arcam --help 走子进程。"""
    proc = subprocess.run([sys.executable, '-m', 'arcam', '--help'],
                          capture_output=True, text=True, cwd=str(ROOT))
    assert proc.returncode == 0
    assert 'render' in proc.stdout and 'bundle' in proc.stdout
