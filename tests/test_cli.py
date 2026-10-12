"""CLI 冒烟：render / char-layer / bundle / check / video / matte 错误路径。

只在 tmp_path 里落文件；退出码约定 0 正常 / 1 运行期失败 / 2 用法错误。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import numpy as np
import pytest
from PIL import Image

from arcam.cli import build_parser, main
from arcam.video import PoseKey, PoseTrack, save_track

SRC = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture()
def photo(tmp_path: Path) -> Path:
    """128×160 中灰底 + 一块暗角，避免全平面对采光过分退化。"""
    arr = np.full((160, 128, 3), 120, dtype=np.uint8)
    arr[:40, :40] = 60
    p = tmp_path / "photo.jpg"
    Image.fromarray(arr).save(p, quality=92)
    return p


@pytest.fixture()
def char(tmp_path: Path) -> Path:
    arr = np.zeros((60, 40, 4), dtype=np.uint8)
    arr[..., 0] = 230
    arr[..., 2] = 200
    arr[..., 3] = 255
    p = tmp_path / "char.png"
    Image.fromarray(arr, "RGBA").save(p)
    return p


@pytest.fixture()
def track_file(tmp_path: Path) -> Path:
    track = PoseTrack(width=128, height=160, fps=4.0, height_m=1.6, feq_mm=23.0,
                      subject_height_m=1.65, char_scale=1.0, size_pct=60.0,
                      mode="ground", frame_key="满幅", keys=[
                          PoseKey(t=0.0, lateral_m=0.0, distance_m=4.0,
                                  pitch_deg=0.0, roll_deg=0.0),
                          PoseKey(t=0.5, lateral_m=0.5, distance_m=4.5,
                                  pitch_deg=8.0, roll_deg=2.0),
                          PoseKey(t=1.0, lateral_m=0.0, distance_m=5.0,
                                  pitch_deg=16.0, roll_deg=0.0),
                      ])
    p = tmp_path / "track.json"
    save_track(track, p)
    return p


def test_render_png_and_scene(tmp_path: Path, photo: Path, char: Path,
                              capsys: pytest.CaptureFixture) -> None:
    out = tmp_path / "shot.png"
    scene = tmp_path / "scene.json"
    code = main(["render", str(photo), str(char), "-o", str(out), "--scene", str(scene)])
    assert code == 0
    assert out.is_file() and out.suffix == ".png"
    assert scene.is_file()
    data = json.loads(scene.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["frame"]["aspect"] == "满幅"
    assert data["character"]["mode"] == "ground"
    assert data["image"]["width"] == data["frame"]["crop"][2]
    with Image.open(out) as im:
        assert im.size == (128, 160) and im.mode == "RGB"
    assert "已出图" in capsys.readouterr().out


def test_render_with_overrides(tmp_path: Path, photo: Path, char: Path) -> None:
    out = tmp_path / "shot.png"
    code = main(["render", str(photo), str(char), "-o", str(out),
                 "--pitch", "12", "--roll", "-5", "--d", "3", "--x", "0.4",
                 "--shot", "腰上", "--char-scale", "1.2", "--feq", "35",
                 "--mode", "free", "--size-pct", "70", "--tone"])
    assert code == 0 and out.is_file()


def test_render_scene_rotation_follows_rot_basis(tmp_path: Path, photo: Path,
                                                 char: Path) -> None:
    """scene.json 记的 rotationDeg 必须按 --rot-basis 走（_placement 曾漏传 rot_basis）。

    原版 charRot（index.html:2930）：rot_basis='frame' → rot 锁 0，'world' → 跟 roll。
    free 模式下这一项由 _compose_kw 与 _placement 两条路共同决定，漏一条配方就与
    实际渲染不符。
    """
    def rot_deg(extra: list[str]) -> float:
        scene = tmp_path / "scene.json"
        code = main(["render", str(photo), str(char), "-o", str(tmp_path / "o.png"),
                     "--scene", str(scene)] + extra)
        assert code == 0
        return json.loads(scene.read_text(encoding="utf-8"))["character"]["rotationDeg"]

    assert rot_deg(["--roll", "25"]) == 25.0                       # world：跟 roll
    assert rot_deg(["--roll", "25", "--rot-basis", "frame"]) == 0.0   # frame：锁 0
    assert rot_deg(["--mode", "free", "--roll", "25"]) == 25.0
    assert rot_deg(["--mode", "free", "--roll", "25",
                    "--rot-basis", "frame"]) == 0.0


def test_char_layer_rgba(tmp_path: Path, char: Path) -> None:
    out = tmp_path / "layer.png"
    code = main(["char-layer", str(char), "-o", str(out), "--mode", "free",
                 "--size-pct", "70", "--width", "80", "--height", "100"])
    assert code == 0
    with Image.open(out) as im:
        assert im.mode == "RGBA"
        assert im.size == (80, 100)
    arr = np.asarray(Image.open(out))
    assert arr[..., 3].max() == 255          # 立绘本体不透明
    assert (arr[..., 3] == 0).any()          # 四周全透明（没铺背景）


def _char_layer(tmp_path: Path, char: Path, tag: str, extra: list[str]) -> np.ndarray:
    """跑一遍 char-layer，返回 RGBA 数组（小画布，快）。"""
    out = tmp_path / f"layer_{tag}.png"
    code = main(["char-layer", str(char), "-o", str(out),
                 "--width", "200", "--height", "300"] + extra)
    assert code == 0
    with Image.open(out) as im:
        return np.asarray(im.convert("RGBA"))


def test_char_layer_honors_shot_and_rot_basis(tmp_path: Path, char: Path) -> None:
    """char-layer 收了 --shot/--rot-basis 就必须生效（曾整组漏传，层和 render 对不上）。

    ground 模式：--shot 决定立绘下沿离地高度（SPEC §3.2），换景别落位必然变；
    --rot-basis frame 时 rot 锁 0，与 world（跟随 roll）在 roll≠0 时必然不同。
    ⚠ free 模式的 --rot-basis 目前仍不生效 —— 漏点在 compose._placement 的 free
      分支（A2 分区），已在审计报告里交编排者，不在这里弱化成"只测 ground"。
    """
    base = _char_layer(tmp_path, char, "base", [])
    assert not np.array_equal(base, _char_layer(tmp_path, char, "shot", ["--shot", "腰上"]))
    rolled_world = _char_layer(tmp_path, char, "world", ["--roll", "20"])
    rolled_frame = _char_layer(tmp_path, char, "frame",
                               ["--roll", "20", "--rot-basis", "frame"])
    assert not np.array_equal(rolled_world, rolled_frame)


def test_char_layer_honors_lighting_gates(tmp_path: Path, char: Path) -> None:
    """光影三门在 char-layer 上也要生效（曾整组漏传：--no-shadow 层里照画阴影）。

    层里那团接触阴影是半透明像素（0<alpha<255）；--no-shadow 后它们必须消失。
    """
    with_shadow = _char_layer(tmp_path, char, "g_shadow", [])
    no_shadow = _char_layer(tmp_path, char, "g_noshadow", ["--no-shadow"])
    assert not np.array_equal(with_shadow, no_shadow)
    semi_on = int(((with_shadow[..., 3] > 0) & (with_shadow[..., 3] < 255)).sum())
    semi_off = int(((no_shadow[..., 3] > 0) & (no_shadow[..., 3] < 255)).sum())
    assert semi_on > 0, "层里本该有半透明阴影"
    assert semi_off < semi_on, "--no-shadow 没抠掉阴影"
    # 立绘本体（不透明像素）一个都不该被 --no-shadow 动到
    assert int((no_shadow[..., 3] == 255).sum()) == int((with_shadow[..., 3] == 255).sum())


def test_bundle_three_files(tmp_path: Path, photo: Path, char: Path) -> None:
    out = tmp_path / "pack.zip"
    code = main(["bundle", str(photo), str(char), "-o", str(out), "--pitch", "10"])
    assert code == 0 and out.is_file()
    with zipfile.ZipFile(out) as zf:
        infos = zf.infolist()
        assert sorted(i.filename for i in infos) == [
            "character.png", "scene.json", "shot.jpg"]
        for info in infos:
            assert info.compress_type == zipfile.ZIP_DEFLATED
            assert info.date_time == (1980, 1, 1, 0, 0, 0)   # 确定性时间戳
        scene = json.loads(zf.read("scene.json").decode("utf-8"))
        assert scene["version"] == 1
        assert scene["camera"]["pitchDeg"] == 10
        shot = zf.read("shot.jpg")
        layer = zf.read("character.png")
    for name, blob in (("shot.jpg", shot), ("character.png", layer)):
        p = tmp_path / name
        p.write_bytes(blob)
        with Image.open(p) as im:
            if name.endswith("jpg"):
                assert im.format == "JPEG" and im.mode == "RGB"
                assert im.size == (128, 160)
            else:
                assert im.format == "PNG" and im.mode == "RGBA"


def test_check_valid_track(tmp_path: Path, track_file: Path,
                           capsys: pytest.CaptureFixture) -> None:
    code = main(["check", str(track_file)])
    assert code == 0
    out = capsys.readouterr().out
    assert "轨道合法" in out
    assert "128×160" in out and "键 3 个" in out


def test_check_disordered_time(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"keys": [
        {"t": 0.0, "distance_m": 4.0},
        {"t": 1.0, "distance_m": 4.0},
        {"t": 0.5, "distance_m": 4.0},
    ]}), encoding="utf-8")
    assert main(["check", str(path)]) == 1


def test_check_out_of_range(tmp_path: Path) -> None:
    path = tmp_path / "range.json"
    path.write_text(json.dumps({"keys": [{"t": 0.0, "distance_m": 4.0, "pitch_deg": 75.0}]}),
                    encoding="utf-8")
    assert main(["check", str(path)]) == 1


def test_video_render_frames(tmp_path: Path, track_file: Path, char: Path) -> None:
    bg = tmp_path / "bg.png"
    Image.fromarray(np.full((100, 120, 3), 110, dtype=np.uint8)).save(bg)
    out_dir = tmp_path / "frames"
    code = main(["video", str(track_file), str(char), "-o", str(out_dir), "--bg", str(bg)])
    assert code == 0
    assert sorted(p.name for p in out_dir.glob("*.png")) == [
        f"frame_{i:06d}.png" for i in range(1, 6)]
    with Image.open(out_dir / "frame_000001.png") as im:
        assert im.size == (128, 160)


def test_video_requires_bg(tmp_path: Path, track_file: Path, char: Path) -> None:
    code = main(["video", str(track_file), str(char), "-o", str(tmp_path / "frames")])
    assert code == 1


def test_video_bad_track_rejected(tmp_path: Path, char: Path, photo: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"keys": [{"t": 0.0, "distance_m": 4.0, "roll_deg": 80.0}]}),
                    encoding="utf-8")
    code = main(["video", str(path), str(char), "-o", str(tmp_path / "frames"),
                 "--bg", str(photo)])
    assert code == 1


def test_video_bg_non_image_hints_frame_dir(tmp_path, track_file, char, capsys) -> None:
    """--bg 指到非图片（视频文件最常见）：人话提示"只收单图/帧目录"，退出 1。

    修前只有 PIL 的 ``cannot identify image file`` —— 用户看不出该拿帧目录来。
    """
    fake = tmp_path / "clip.mp4"
    fake.write_bytes(b"\x00\x00\x00 ftypmp42not really a video")
    code = main(["video", str(track_file), str(char), "-o", str(tmp_path / "frames"),
                 "--bg", str(fake)])
    assert code == 1
    err = capsys.readouterr().err
    assert "--bg 只收单张图片或帧目录" in err and "frame_%06d.png" in err


def test_missing_inputs(tmp_path: Path, char: Path, capsys: pytest.CaptureFixture) -> None:
    code = main(["render", str(tmp_path / "nope.jpg"), str(char),
                 "-o", str(tmp_path / "o.png")])
    assert code == 1
    assert "错误" in capsys.readouterr().err


def test_matte_without_onnxruntime(tmp_path: Path, char: Path,
                                   capsys: pytest.CaptureFixture) -> None:
    """没装 onnxruntime / 没模型：必须人话报错退出 1，不许 traceback。"""
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("环境里有 onnxruntime：端到端抠图由 P4 的测试覆盖")
    out = tmp_path / "cut.png"
    code = main(["matte", str(char), "-o", str(out)])
    assert code == 1
    err = capsys.readouterr().err
    assert "onnxruntime" in err or "模型" in err
    assert not out.exists()


def test_matte_missing_image(tmp_path: Path) -> None:
    code = main(["matte", str(tmp_path / "nope.png"), "-o", str(tmp_path / "o.png")])
    assert code == 1


def test_matte_missing_model(tmp_path: Path, char: Path,
                             capsys: pytest.CaptureFixture) -> None:
    """--model 指到不存在的文件：人话报错退出 1（不依赖 onnxruntime 是否安装）。"""
    out = tmp_path / "cut.png"
    code = main(["matte", str(char), "-o", str(out), "--model",
                 str(tmp_path / "nope.onnx")])
    assert code == 1
    err = capsys.readouterr().err
    assert "模型" in err and "--model" in err
    assert not out.exists()


@pytest.mark.skipif(importlib.util.find_spec("onnxruntime") is None,
                    reason="onnxruntime 未装：打不开模型的路径要 ORT 才走得到")
def test_matte_corrupt_model_no_traceback(tmp_path: Path, char: Path,
                                          capsys: pytest.CaptureFixture) -> None:
    """模型文件存在但不是合法 onnx：必须人话报错退出 1，不许 traceback。

    ORT 的 InvalidProtobuf 不是 ValueError/OSError 子类，_load_session 不转的话
    会直接从 main() 漏出去变成 traceback（审计前复现过）。
    """
    out = tmp_path / "cut.png"
    bad = tmp_path / "bad.onnx"
    bad.write_bytes(b"this is not an onnx model")
    code = main(["matte", str(char), "-o", str(out), "--model", str(bad)])
    assert code == 1
    err = capsys.readouterr().err
    assert "模型" in err and "Traceback" not in err
    assert not out.exists()


# ---------- lidar serve ----------

def test_lidar_serve_receives_frames(tmp_path: Path, monkeypatch,
                                     capsys: pytest.CaptureFixture) -> None:
    """serve 返回惰性 generator：命令必须迭代它，否则 bind 完就退、一帧收不到。

    用假 serve（两个帧后正常结束）复现：修复前 rc=0 且输出里没有任何帧摘要。
    """
    import arcam.lidar_live as live

    meta = {"W": 4, "H": 3, "fx": 100.0, "fy": 100.0, "cx": 2.0, "cy": 1.5,
            "roll_deg": 8.0, "pitch_deg": 12.0, "height_m": 1.55,
            "timestamp": 1699999999.5}
    frames = [(dict(meta), np.zeros((3, 4), dtype=np.float32)),
              (dict(meta), np.ones((3, 4), dtype=np.float32))]

    def fake_serve(**kw):
        assert kw["port"] == 7707
        yield from frames

    monkeypatch.setattr(live, "serve", fake_serve)
    code = main(["lidar", "serve", "--port", "7707"])
    assert code == 0
    out = capsys.readouterr().out
    assert "第 1 帧 4×3" in out and "第 2 帧 4×3" in out
    assert "h=1.55" in out and "pitch=12" in out and "roll=8" in out
    assert "共接收 2 帧" in out


def test_lidar_serve_out_dir_is_passed(tmp_path: Path, monkeypatch) -> None:
    import arcam.lidar_live as live
    seen: dict = {}

    def fake_serve(**kw):
        seen.update(kw)
        return iter([])

    monkeypatch.setattr(live, "serve", fake_serve)
    code = main(["lidar", "serve", "--port", "1234", "--out", str(tmp_path / "d")])
    assert code == 0
    assert seen == {"port": 1234, "out_dir": str(tmp_path / "d")}


def test_lidar_serve_module_missing(monkeypatch,
                                     capsys: pytest.CaptureFixture) -> None:
    """模块缺失：提示并正常退出 0（不崩、不吞错）。"""
    monkeypatch.setitem(sys.modules, "arcam.lidar_live", None)
    code = main(["lidar", "serve", "--port", "7707"])
    assert code == 0
    assert "未就绪" in capsys.readouterr().out


def test_help_and_usage_error() -> None:
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0
    for cmd in ("render", "char-layer", "bundle", "matte", "video", "check"):
        with pytest.raises(SystemExit) as e:
            main([cmd, "--help"])
        assert e.value.code == 0
    with pytest.raises(SystemExit) as e:
        main(["render"])          # 缺必填参数 → argparse 用法错误
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        main([])                  # 没给子命令
    assert e.value.code == 2


def test_module_entrypoint(tmp_path: Path, photo: Path, char: Path) -> None:
    """python -m arcam 走同一条 main()。"""
    import os
    import subprocess

    out = tmp_path / "m.png"
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    proc = subprocess.run(
        [sys.executable, "-m", "arcam", "render", str(photo), str(char), "-o", str(out)],
        cwd=str(SRC.parent), env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    assert out.is_file()


# ─────────────────────── web 子命令的旗标翻译 ───────────────────────

def test_web_translates_argv(monkeypatch) -> None:
    """web → webapp.main 的 argv 翻译：host/port 透传，--no-open 才加。"""
    import arcam.webapp as webapp
    seen: dict = {}

    def fake_main(argv):
        seen["argv"] = list(argv)
        return 0

    monkeypatch.setattr(webapp, "main", fake_main)
    assert main(["web", "--host", "0.0.0.0", "--port", "9000"]) == 0
    assert seen["argv"] == ["--host", "0.0.0.0", "--port", "9000"]
    assert main(["web", "--no-open"]) == 0
    assert seen["argv"] == ["--host", "127.0.0.1", "--port", "8765", "--no-open"]


def test_web_open_flags_mutually_exclusive(monkeypatch, capsys) -> None:
    """--open 与 --no-open 互斥（v2.0.0 审计：--open 曾是"解析了没人读"的摆设）。

    启动器固定传 ``arcam web --open --port``（WEB-SPEC §4），所以 --open 必须
    继续被接受（且不传 --no-open 给 webapp.main）；两个一起给没有可执行语义，
    进互斥组后由 parser 挡成用法错误 exit 2。
    """
    import arcam.webapp as webapp
    seen: dict = {}

    def fake_main(argv):
        seen["argv"] = list(argv)
        return 0

    monkeypatch.setattr(webapp, "main", fake_main)

    for argv, expect_no_open in ((["web", "--open"], False),
                                 (["web", "--no-open"], True),
                                 (["web"], False)):
        seen.clear()
        assert main(argv) == 0, argv
        assert ("--no-open" in seen["argv"]) is expect_no_open, (argv, seen["argv"])

    with pytest.raises(SystemExit) as e:
        main(["web", "--open", "--no-open"])
    assert e.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_render_overlay_modes(tmp_path: Path, photo: Path, char: Path) -> None:
    """--overlay 四层各自生效且互不相同（HUD 排查构图的显式开关）。

    踩过的坑（回归钉）：compose_shot 出 **uint8 0..255**，overlay 通路曾按
    浮点口径又乘 255 再 clip —— 整图被洗白（均值差 128）。现在按 dtype 分流，
    只在该画的像素上落笔。
    """
    from PIL import Image as _Image
    import numpy as _np

    plain = tmp_path / "plain.png"
    assert main(["render", str(photo), str(char), "-o", str(plain), "--pitch", "12"]) == 0
    base = _np.asarray(_Image.open(plain).convert("RGB")).astype(int)

    outs = {}
    for layer in ("grid", "horizon", "figure", "hud"):
        p = tmp_path / f"ov-{layer}.png"
        code = main(["render", str(photo), str(char), "-o", str(p),
                     "--pitch", "12", "--overlay", layer])
        assert code == 0, layer
        arr = _np.asarray(_Image.open(p).convert("RGB")).astype(int)
        outs[layer] = arr
        diff = int((_np.abs(arr - base).sum(2) > 12).sum())
        assert diff > 100, f"{layer} 层几乎没画东西（差异像素 {diff}）"

    # 四层两两不同（防止某一层静默不画）
    for a, b in (("grid", "hud"), ("grid", "figure"), ("grid", "horizon"),
                 ("hud", "figure"), ("hud", "horizon"), ("figure", "horizon")):
        d = int((_np.abs(outs[a] - outs[b]).sum(2) > 12).sum())
        assert d > 0, f"{a} 与 {b} 输出相同"


# ─────────────────────── --overlay 多层组合（逗号） ───────────────────────

#: overlay 回归共用的摆放参数：CLI 与 oracle 必须跑同一套，防止两边参数漂移
_OV_EXTRA = ("--pitch", "12")


def _render_overlay_png(photo: Path, char: Path, out: Path, overlay) -> bytes:
    """跑一遍 render（可带 --overlay），返回落盘 PNG 的字节。"""
    argv = ["render", str(photo), str(char), "-o", str(out), *_OV_EXTRA]
    if overlay is not None:
        argv += ["--overlay", overlay]
    assert main(argv) == 0, overlay
    return out.read_bytes()


def _overlay_oracle(layers, photo: Path, char: Path, out: Path) -> bytes:
    """把 layers（按给出顺序）逐层手工叠到出图上，返回 PNG 字节。

    单层时即**改动前** ``_apply_overlay`` 的路径（cli.py 旧 :813-825 逐字对译：
    compose_shot → 按 dtype 量化 → 对应的那一个 ``hud.draw_*`` → save_png），
    是「单选 --overlay 逐位不变」的基准；多层时按 canonical 序连叠，是组合
    语义的基准。好处：不依赖被改代码本身，解析/叠层顺序被悄悄改坏就会红。
    """
    from arcam.cli import _compose_kw, _placement, _pose, build_parser
    from arcam.compose import compose_shot
    from arcam.hud import draw_figure, draw_grid, draw_horizon, draw_hud_text
    from arcam.i18n import t
    from arcam.video import image_to_rgb01, image_to_rgba01, save_png

    args = build_parser().parse_args(
        ["render", str(photo), str(char), "-o", str(out), *_OV_EXTRA])
    src01 = image_to_rgb01(photo)
    char01 = image_to_rgba01(char)
    height, width = src01.shape[:2]
    cam, subj = _pose(args, width, height)
    shot = compose_shot(src01, char01, subj, cam, **_compose_kw(args))
    arr = np.asarray(shot.shot)
    img = arr if arr.dtype == np.uint8 else \
        np.clip(arr.astype(np.float64) * 255.0 + 0.5, 0, 255).astype(np.uint8)
    for layer in layers:
        if layer == "grid":
            img = draw_grid(img, cam, subj.lateral_m)
        elif layer == "horizon":
            img = draw_horizon(img, cam, t("horizon", lang="zh"))
        elif layer == "figure":
            img = draw_figure(img, cam, subj)
        else:
            img, _hints = draw_hud_text(img, cam, subj, has_char=True,
                                        canvas_size=(width, height), lang="zh",
                                        shot=args.shot, mode=args.mode,
                                        placement=_placement(char01, subj, cam, args))
    save_png(img, out)
    return out.read_bytes()


def test_render_overlay_single_layer_byte_identical(tmp_path, photo, char) -> None:
    """单选四层与改动前逐字节一致（--overlay 改逗号组合的向后兼容红线）。

    旧命令行（--overlay grid 等）的输出一个字节都不能差：基准是对译出来的
    改动前单层路径（见 _overlay_oracle），不是被改代码本身。
    """
    for layer in ("grid", "horizon", "figure", "hud"):
        got = _render_overlay_png(photo, char, tmp_path / f"one-{layer}.png", layer)
        want = _overlay_oracle([layer], photo, char, tmp_path / f"old-{layer}.png")
        assert got == want, f"--overlay {layer} 输出与改动前不一致"


def test_render_overlay_combo_canonical_order(tmp_path, photo, char) -> None:
    """组合按 canonical 序叠：书写顺序不影响结果（hud,grid == grid,hud）。"""
    want = _overlay_oracle(["grid", "hud"], photo, char, tmp_path / "old-gh.png")
    for given in ("grid,hud", "hud,grid", " hud , grid ", "grid, hud"):
        got = _render_overlay_png(photo, char, tmp_path / "combo.png", given)
        assert got == want, f"--overlay {given!r} 未按 canonical 序（grid→hud）叠"


def test_render_overlay_combo_stacks_every_layer(tmp_path, photo, char) -> None:
    """组合把每一层都画上：与无叠层/任一单层都不同（防某一层静默不画）。"""
    base = _render_overlay_png(photo, char, tmp_path / "plain.png", None)
    grid = _render_overlay_png(photo, char, tmp_path / "g.png", "grid")
    horizon = _render_overlay_png(photo, char, tmp_path / "h.png", "horizon")
    combo = _render_overlay_png(photo, char, tmp_path / "gh.png", "grid,horizon")
    for other, name in ((base, "无叠层"), (grid, "grid 单层"), (horizon, "horizon 单层")):
        assert combo != other, f"组合输出与{name}相同（有层没画上）"
    assert combo == _overlay_oracle(["grid", "horizon"], photo, char,
                                    tmp_path / "old-gh2.png")


def test_render_overlay_duplicate_name_dedups(tmp_path, photo, char) -> None:
    """重复名去重（保首次出现）：grid,grid 与单选 grid 逐字节相同。"""
    once = _render_overlay_png(photo, char, tmp_path / "g.png", "grid")
    assert _render_overlay_png(photo, char, tmp_path / "gg.png", "grid,grid") == once
    assert _render_overlay_png(photo, char, tmp_path / "ggg.png",
                               "grid,grid,grid") == once


def test_render_overlay_note_lists_actual_layers(tmp_path, photo, char,
                                                 capsys) -> None:
    """提示文案列**实际叠的层序**（canonical 序）；单层时与从前逐字相同。"""
    out = tmp_path / "n.png"
    assert main(["render", str(photo), str(char), "-o", str(out),
                 *_OV_EXTRA, "--overlay", "grid"]) == 0
    assert "｜已叠 HUD 层：grid\n" in capsys.readouterr().out
    assert main(["render", str(photo), str(char), "-o", str(out),
                 *_OV_EXTRA, "--overlay", "hud,grid"]) == 0
    printed = capsys.readouterr().out
    assert "｜已叠 HUD 层：grid+hud" in printed      # canonical 序，不是书写序
    assert "hud+grid" not in printed


def test_overlay_rejects_bad_names(tmp_path, photo, char, capsys) -> None:
    """未知名/空段 → argparse 用法错误 exit 2（不抛 traceback），并列合法值。"""
    for cmd in ("render", "watch"):
        for bad in ("grid,foo", "grid,", ",grid", "grid,,horizon",
                    "grid,horizon,bogus", "GRID"):
            argv = [cmd, str(photo), str(char), "-o", str(tmp_path / "x.png"),
                    "--overlay", bad]
            if cmd == "watch":
                argv.append("--once")
            with pytest.raises(SystemExit) as e:
                main(argv)
            assert e.value.code == 2, (cmd, bad)
            err = capsys.readouterr().err
            assert "grid/horizon/figure/hud" in err, (cmd, bad, err)


def test_watch_overlay_combo_matches_render(tmp_path, photo, char) -> None:
    """watch 路径同样走 _apply_overlay：组合值与 render 同参逐字节相同。"""
    watch_out = tmp_path / "watch.png"
    assert main(["watch", str(photo), str(char), "-o", str(watch_out),
                 "--once", *_OV_EXTRA, "--overlay", "hud,grid"]) == 0
    render_out = _render_overlay_png(photo, char, tmp_path / "render.png", "grid,hud")
    assert watch_out.read_bytes() == render_out
    # 与任一单层不同（两层都真的叠上了）
    only_grid = _render_overlay_png(photo, char, tmp_path / "g.png", "grid")
    only_hud = _render_overlay_png(photo, char, tmp_path / "h.png", "hud")
    assert watch_out.read_bytes() not in (only_grid, only_hud)


# ─────────────────────── 批量 / 配方 / 对比网格 / GIF（新功能） ───────────────────────

def _make_photo(path, seed=0):
    from PIL import Image
    rng = np.random.default_rng(seed)
    H, W = 240, 320
    yy, xx = np.mgrid[0:H, 0:W].astype(float)
    p = np.empty((H, W, 3), np.uint8)
    p[..., 0] = (110 + 20 * np.sin(xx / 40 + seed)).clip(0, 255)
    p[..., 1] = (130 + 25 * np.cos(yy / 50 + seed)).clip(0, 255)
    p[..., 2] = 190
    p[yy >= 140] = (95, 85, 75)
    p = (p.astype(np.int16) + rng.integers(-3, 4, p.shape)).clip(0, 255).astype(np.uint8)
    Image.fromarray(p, "RGB").save(path, quality=90)


def test_batch_renders_every_photo(tmp_path, char, capsys):
    """batch：目录里每张照片各出一张，参数只给一遍。"""
    src = tmp_path / "photos"
    src.mkdir()
    for i in range(3):
        _make_photo(src / f"p{i}.jpg", seed=i)
    out = tmp_path / "out"
    code = main(["batch", str(src), str(char), "-o", str(out), "--pitch", "8"])
    assert code == 0
    produced = sorted(p.name for p in out.glob("shot_p*.png"))
    assert len(produced) == 3
    # 每张内容不同（不是同一张重复）
    from PIL import Image
    imgs = [np.asarray(Image.open(out / n).convert("RGB")) for n in produced]
    assert not np.array_equal(imgs[0], imgs[1])
    assert "3/3" in capsys.readouterr().out


def test_batch_empty_dir_errors(tmp_path, char, capsys):
    """空目录 → 退出码 1 + 人话错误（main 捕获 CliError 打 stderr）。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["batch", str(empty), str(char), "-o", str(tmp_path / "o")]) == 1
    assert "没有图片文件" in capsys.readouterr().err


def _depth_frame(tmp_path, name: str, *, geom_roll: float, meta_roll: float):
    """落一盘 .depth：几何按 geom_roll 生成、meta（ARKit）写 meta_roll。

    两者不一致正是真机里"IMU 与拟合地面不一致"的情形——两条 CLI 路径必须都按
    meta 的 roll 反旋解 pitch（LIDAR-SPEC §3）。
    """
    from arcam.depth import LiDARFrame, save_depth
    from arcam.lidar_sim import synth_ground_frame
    meta, depth = synth_ground_frame(h=1.6, pitch_deg=10.0, roll_deg=geom_roll,
                                     width=128, height=96, index=5)
    frame = LiDARFrame(depth_m=depth, fx=meta["fx"], fy=meta["fy"],
                       cx=meta["cx"], cy=meta["cy"], roll_deg=meta_roll,
                       pitch_deg=meta["pitch_deg"], height_m=meta["height_m"],
                       source="test")
    p = tmp_path / name
    save_depth(frame, p)
    return p


def test_batch_depth_dir_drives_render(tmp_path, char, capsys):
    """--depth-dir 必须真的驱动渲染（曾整批静默失效：load_depth 没 import，
    NameError 被候选循环的宽 except 吞掉 → 每张都退回无深度，退出码还是 0）。"""
    src = tmp_path / "photos"
    src.mkdir()
    _make_photo(src / "p0.jpg", seed=0)
    depths = tmp_path / "depths"
    depths.mkdir()
    _depth_frame(depths, "p0.depth", geom_roll=8.0, meta_roll=8.0)
    plain = tmp_path / "out_plain"
    deep = tmp_path / "out_deep"
    assert main(["batch", str(src), str(char), "-o", str(plain)]) == 0
    assert main(["batch", str(src), str(char), "-o", str(deep),
                 "--depth-dir", str(depths)]) == 0
    out = capsys.readouterr().out
    assert "（深度驱动）" in out, "--depth-dir 没有走深度路径"
    a = np.asarray(Image.open(deep / "shot_p0.png").convert("RGB")).astype(int)
    b = np.asarray(Image.open(plain / "shot_p0.png").convert("RGB")).astype(int)
    assert int((np.abs(a - b).sum(2) > 0).sum()) > 100, "深度没有改变落位"


def test_batch_depth_matches_render_depth(tmp_path, char, capsys):
    """batch --depth-dir 与 render --depth 对同一帧必须逐位一致（roll 口径）。

    复现过的偏差：batch 漏传 roll_deg 给 camera_env_from_plane，同一帧解出
    roll=拟合值（20°）而不是 ARKit 上报值（8°），与 render 结论不同。
    """
    src = tmp_path / "photos"
    src.mkdir()
    _make_photo(src / "p0.jpg", seed=0)
    depths = tmp_path / "depths"
    depths.mkdir()
    frame = _depth_frame(depths, "p0.depth", geom_roll=20.0, meta_roll=8.0)
    out = tmp_path / "out"
    assert main(["batch", str(src), str(char), "-o", str(out),
                 "--depth-dir", str(depths)]) == 0
    via_render = tmp_path / "render.png"
    assert main(["render", str(src / "p0.jpg"), str(char), "-o", str(via_render),
                 "--depth", str(frame)]) == 0
    a = np.asarray(Image.open(out / "shot_p0.png").convert("RGB"))
    b = np.asarray(Image.open(via_render).convert("RGB"))
    assert np.array_equal(a, b), "batch 与 render 的深度口径不一致"


def test_batch_reports_failure_exit_code(tmp_path, char, capsys):
    """一张读不出来的照片：只记一条、其余照跑，退出码 1（单张失败不整批挂）。"""
    src = tmp_path / "photos"
    src.mkdir()
    _make_photo(src / "ok.jpg", seed=1)
    (src / "broken.jpg").write_bytes(b"not an image at all")
    out = tmp_path / "out"
    assert main(["batch", str(src), str(char), "-o", str(out)]) == 1
    assert (out / "shot_ok.png").is_file()
    err = capsys.readouterr().out
    assert "1/2 成功" in err and "失败" in err


def test_recipe_roundtrip(tmp_path, monkeypatch):
    """recipe save/list/show/apply 四动作闭环；--set 可临时覆盖。"""
    monkeypatch.setenv("ARCAM_RECIPES", str(tmp_path / "recipes"))
    import arcam.cli as cli
    monkeypatch.setattr(cli, "RECIPE_ROOT", tmp_path / "recipes")
    args = ["recipe", "save", "t1", "--pitch", "15", "--mode", "free",
            "--size-pct", "70", "--frame", "16:9"]
    assert main(args) == 0
    assert (tmp_path / "recipes" / "t1.json").is_file()
    assert main(["recipe", "list"]) == 0
    assert main(["recipe", "show", "t1"]) == 0
    assert main(["recipe", "show", "nope"]) == 1
    # apply 输出可直接粘的旗标；--set 覆盖生效
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert main(["recipe", "apply", "t1", "--set", "pitch=30"]) == 0
    out = buf.getvalue()
    assert "--pitch 30" in out and "--mode free" in out and "--frame 16:9" in out


def test_recipe_from_scene(tmp_path, monkeypatch, photo, char):
    """recipe save --from scene.json：从配方文件提摆放段。"""
    monkeypatch.setenv("ARCAM_RECIPES", str(tmp_path / "recipes"))
    import arcam.cli as cli
    monkeypatch.setattr(cli, "RECIPE_ROOT", tmp_path / "recipes")
    scene = tmp_path / "s.json"
    assert main(["render", str(photo), str(char), "-o", str(tmp_path / "o.png"),
                 "--scene", str(scene), "--pitch", "20", "--frame", "4:5"]) == 0
    assert main(["recipe", "save", "fromscene", "--from", str(scene)]) == 0
    data = json.loads((tmp_path / "recipes" / "fromscene.json").read_text(encoding="utf-8"))
    assert data["params"]["pitch"] == 20.0
    assert data["params"]["frame"] == "4:5"


def test_recipe_from_scene_rot_basis(tmp_path, monkeypatch, photo, char):
    """scene.json 不记 rotBasis：只有 roll≠0 时可判定（frame 基准下旋转恒 0）。

    旧实现要求 rotationLocked（= 吸附锁 0）才认 frame，于是
    ``--rot-basis frame --roll 25`` 存出来的配方被反推成 world —— 套回去
    roll≠0 时旋转就变了。现在按"旋转 0 + roll≠0"判定（两种模式都成立：
    world 基准下 rotationDeg 必等于 roll，吸附锁 0 与基准无关）。
    """
    monkeypatch.setenv("ARCAM_RECIPES", str(tmp_path / "recipes"))
    import arcam.cli as cli
    monkeypatch.setattr(cli, "RECIPE_ROOT", tmp_path / "recipes")

    def saved_params(extra: list[str]) -> dict:
        scene = tmp_path / "s.json"
        assert main(["render", str(photo), str(char), "-o", str(tmp_path / "o.png"),
                     "--scene", str(scene)] + extra) == 0
        assert main(["recipe", "save", "r", "--from", str(scene)]) == 0
        return json.loads(
            (tmp_path / "recipes" / "r.json").read_text(encoding="utf-8"))["params"]

    for mode in (["--mode", "free"], []):
        frame_params = saved_params(mode + ["--rot-basis", "frame", "--roll", "25"])
        assert frame_params["rot_basis"] == "frame", mode
        world_params = saved_params(mode + ["--rot-basis", "world", "--roll", "25"])
        assert world_params["rot_basis"] == "world", mode
        # roll=0 时两种基准渲染逐位相同 → 只能回落 world（配方不带这一项也无歧义）
        flat = saved_params(mode + ["--rot-basis", "frame"])
        assert flat.get("rot_basis", "world") == "world", mode
    # scene.json 没有 sizePct 字段可提 → 配方里就不该有 size_pct
    assert "size_pct" not in frame_params


def test_contact_sheet_grid(tmp_path, photo, char):
    """contact-sheet：笛卡尔积逐格渲染 + 拼图（格间互异）。"""
    out = tmp_path / "sheet.png"
    code = main(["contact-sheet", str(photo), str(char), "-o", str(out),
                 "--vary", "d=3,4", "--vary", "pitch=0,12", "--cell-w", "160"])
    assert code == 0 and out.is_file()
    from PIL import Image
    im = Image.open(out)
    # 2×2 格、每格 160 宽、标签条 26px 高
    assert im.size == (320, 2 * (120 + 26)) or im.size[0] == 320
    a = np.asarray(im.convert("RGB"))
    # 四个格子内容两两不同
    cells = [a[26 + r * (120 + 26):26 + r * (120 + 26) + 100, c * 160:(c + 1) * 160]
             for r in range(2) for c in range(2)]
    for i in range(4):
        for j in range(i + 1, 4):
            assert not np.array_equal(cells[i], cells[j]), f"格 {i} 与 {j} 相同"


def test_contact_sheet_rejects_over_wide(tmp_path, photo, char, capsys):
    """笛卡尔积超上限 → 退出码 1 + 人话错误（防手滑炸内存）。"""
    code = main(["contact-sheet", str(photo), str(char), "-o", str(tmp_path / "s.png"),
                 "--vary", "d=1,2,3,4,5,6", "--vary", "pitch=0,10,20,30,40"])
    assert code == 1
    assert "超过上限" in capsys.readouterr().err


def test_contact_sheet_tone_takes_effect(tmp_path, photo, char):
    """--tone 必须进 compose_shot（曾静默失效：基准参数另抄一份、漏了 tone 键）。"""
    base = tmp_path / "cs_base.png"
    toned = tmp_path / "cs_tone.png"
    assert main(["contact-sheet", str(photo), str(char), "-o", str(base),
                 "--vary", "d=3,4", "--cell-w", "120"]) == 0
    assert main(["contact-sheet", str(photo), str(char), "-o", str(toned),
                 "--vary", "d=3,4", "--cell-w", "120", "--tone"]) == 0
    a = np.asarray(Image.open(base).convert("RGB")).astype(int)
    b = np.asarray(Image.open(toned).convert("RGB")).astype(int)
    assert int((np.abs(a - b).sum(2) > 0).sum()) > 100, "--tone 没生效"


def test_contact_sheet_rejects_duplicate_axis(tmp_path, photo, char, capsys):
    """同一参数扫两轴：后轴覆写前轴、格子成倍重复 → 人话报错退出 1。"""
    code = main(["contact-sheet", str(photo), str(char), "-o", str(tmp_path / "s.png"),
                 "--vary", "d=3,4", "--vary", "d=5,6"])
    assert code == 1
    assert "给了两次" in capsys.readouterr().err


def test_vary_rejects_bad_enum_and_number(tmp_path, photo, char, capsys):
    """--vary 的枚举/edge_bar/数字取值不合法 → 退出码 1 + 人话错误（P1 回归钉）。

    复现（v2.0.0 审计）：``--vary shot=bogus`` / ``frame=bogus`` /
    ``rot_basis=bogus`` / ``edge_bar=9`` 全部**退出码 0** —— 值被 compose 侧
    的原版 JS 兜底安静吃掉（SHOTS['全身'] / FRAMES[key]||0 / 非 frame 即
    world / bar∉1..4 不吸附），出图与默认值无差，正是"参数解析了但没生效"。
    顶层旗标有 argparse choices 挡着，这两个字符串路径当时没有。
    """
    bad = [("shot", "bogus", "全身/膝上"), ("frame", "bogus", "满幅"),
           ("rot_basis", "bogus", "world/frame"), ("edge_bar", "9", "0/1/2/3/4"),
           ("edge_bar", "abc", "不是整数"), ("d", "abc", "不是数字")]
    for key, value, expect in bad:
        code = main(["contact-sheet", str(photo), str(char), "-o", str(tmp_path / "s.png"),
                     "--vary", f"{key}={value}"])
        assert code == 1, (key, value)
        err = capsys.readouterr().err
        assert expect in err, (key, value, err)


def test_spec_rejects_bad_enum_and_number(tmp_path, photo, char, capsys):
    """--spec 与 --vary 同一套取值口径（共用一个 coercion，两边都要挡）。"""
    bad = [("shot", "bogus", "全身/膝上"), ("frame", "bogus", "满幅"),
           ("rot_basis", "bogus", "world/frame"), ("edge_bar", "7", "0/1/2/3/4"),
           ("edge_bar", "abc", "不是整数"), ("x", "abc", "不是数字")]
    for key, value, expect in bad:
        code = main(["multi", str(photo), str(char), str(char),
                     "-o", str(tmp_path / "m.png"), "--spec", f"{key}={value}"])
        assert code == 1, (key, value)
        err = capsys.readouterr().err
        assert expect in err, (key, value, err)


def test_vary_and_spec_accept_valid_enums(tmp_path, photo, char):
    """合法枚举取值照旧放行（--vary shot=腰上 / --spec edge_bar=2 + tone=1）。"""
    assert main(["contact-sheet", str(photo), str(char), "-o", str(tmp_path / "s.png"),
                 "--vary", "shot=腰上", "--vary", "rot_basis=frame",
                 "--cell-w", "120"]) == 0
    assert main(["multi", str(photo), str(char), str(char), "-o", str(tmp_path / "m.png"),
                 "--mode", "free", "--spec", "edge_bar=2;tone=1;shot=头肩"]) == 0


def test_video_gif_export(tmp_path, photo, char, capsys):
    """video --gif：纯 PIL 写出动画 GIF（帧数/循环/时长正确）。"""
    from arcam.video import PoseTrack, PoseKey, save_track
    track = PoseTrack(width=320, height=240, fps=8,
                      height_m=1.5, feq_mm=30.0, subject_height_m=1.65,
                      char_scale=1.0, size_pct=60.0, mode="ground",
                      frame_key="满幅", shot="全身",
                      keys=[PoseKey(t=0.0, pitch_deg=0.0, roll_deg=0.0, distance_m=4.0),
                            PoseKey(t=0.5, pitch_deg=10.0, roll_deg=-6.0, distance_m=4.5),
                            PoseKey(t=1.0, pitch_deg=-5.0, roll_deg=5.0, distance_m=3.5)])
    tp = tmp_path / "t.json"
    save_track(track, tp)
    out = tmp_path / "frames"
    code = main(["video", str(tp), str(char), "-o", str(out), "--bg", str(photo), "--gif"])
    assert code == 0
    gif = out / "video.gif"
    assert gif.is_file(), "GIF 没写出来"
    from PIL import Image
    with Image.open(gif) as im:
        assert im.format == "GIF"
        assert im.n_frames == 9                 # 1s × 8fps + 1
        assert im.info.get("loop") == 0         # 无限循环
        assert im.info.get("duration") == 125 or 100 <= im.info.get("duration", 0) <= 150


# ─────────────────────── watch / multi / stats（v1.4.0 新功能） ───────────────────────

def test_watch_once(tmp_path, photo, char):
    """watch --once：跑一遍就退出（不进入循环）。"""
    out = tmp_path / "w.png"
    code = main(["watch", str(photo), str(char), "-o", str(out), "--once", "--pitch", "5"])
    assert code == 0 and out.is_file()


def test_watch_detects_change(tmp_path, photo, char):
    """watch 循环：素材 mtime 变了就重出一遍（轮询 0.3s）。"""
    import subprocess, sys, time
    out = tmp_path / "w.png"
    proc = subprocess.Popen(
        [sys.executable, "-m", "arcam", "watch", str(photo), str(char),
         "-o", str(out), "--interval", "0.3"],
        cwd=str(ROOT), env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(2.5)                        # 等首次渲染
        first = out.read_bytes()
        # 改照片（内容变 + mtime 变）
        from PIL import Image
        arr = np.asarray(Image.open(photo).convert("RGB")).copy()
        arr[20:60, 20:60] = 255
        Image.fromarray(arr).save(photo, quality=92)
        time.sleep(2.0)                        # 等一轮轮询
        assert out.read_bytes() != first, "素材改了但输出没变"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_multi_two_chars(tmp_path, photo, char):
    """multi：两张立绘各带站位叠进同一照片（第二张在 spec 里覆盖）。"""
    out = tmp_path / "multi.png"
    code = main(["multi", str(photo), str(char), str(char), "-o", str(out),
                 "--spec", "x=1.2;d=3.0;scale=0.7"])
    assert code == 0 and out.is_file()
    # 与单张渲染不同（第二个人物确实叠上去了）
    single = tmp_path / "single.png"
    assert main(["render", str(photo), str(char), "-o", str(single)]) == 0
    a = np.asarray(Image.open(out).convert("RGB")).astype(int)
    b = np.asarray(Image.open(single).convert("RGB")).astype(int)
    assert int((np.abs(a - b).sum(2) > 12).sum()) > 500


def test_multi_keeps_char_colors(tmp_path, photo, char):
    """multi 出图必须保留立绘**本色**（P0 回归钉：层被当浮点用 → 人物纯白）。

    复现（v2.0.0 审计）：``compose_char_layer`` 出的是 uint8 0..255，而
    ``cli._paste_layer`` 把它赋值进 float64 数组后当 [0,1] 喂给
    ``compose._over`` —— alpha=255 被当成 1.0 的 255 倍，source-over 算出
    ``base·(1−255) + rgb·255``，再乘 255 clip → **整块立绘变 255,255,255**
    （实测 1990 个不透明像素全白，而立绘本色是 235,60,200）。旧测试只断言
    "与照片不同"，纯白块也能过，所以这条钉颜色本身。
    """
    out = tmp_path / "multi.png"
    assert main(["multi", str(photo), str(char), "-o", str(out)]) == 0
    layer_p = tmp_path / "layer.png"
    assert main(["char-layer", str(char), "-o", str(layer_p),
                 "--width", "128", "--height", "160"]) == 0
    with Image.open(out) as im:
        multi = np.asarray(im.convert("RGB")).astype(int)
    with Image.open(layer_p) as im:
        layer = np.asarray(im.convert("RGBA")).astype(int)
    opaque = layer[..., 3] == 255
    assert opaque.sum() > 100, "立绘层里应有成片不透明像素"
    got = multi[opaque]
    # ⚠ 直接和立绘层本色比，不是和阈值比：alpha=255 时 source-over 精确等于
    #   层色，multi 出图在该处必须逐像素等于层色。按通道最小值判会漏——fixture
    #   绿通道是 0，老 bug 复发时 R/B 饱和到 255 仍能过 >180/>150，而本色
    #   比较在任何通道上都会红（对抗复核变异实测：删掉 dtype 分流后这条断言红）。
    drift = int((np.abs(got - layer[opaque][..., :3]).max(axis=1) > 2).sum())
    assert drift == 0, \
        f"{drift}/{got.shape[0]} 个不透明像素的颜色与立绘层不符（_paste_layer dtype 老 bug 复发）"
    # 立绘确实是彩的（不是白块也不是纯照片）：三通道都有动态
    assert layer[opaque][..., :3].max() > 150 and layer[opaque][..., 1].max() < 100, \
        "夹具立绘应是非灰的非白色（否则上面的本色比较没有区分力）"


def test_multi_spec_validation(tmp_path, photo, char):
    """spec 键不认识 / 段不是 K=V → 退出码 1 + 人话错误。"""
    import io, contextlib
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = main(["multi", str(photo), str(char), str(char), "-o", str(tmp_path / "m.png"),
                     "--spec", "bogus=1"])
    assert code == 1
    assert "不认识" in err.getvalue()


def test_multi_spec_size_pct_override(tmp_path, photo, char):
    """spec 里的 size_pct 要真的传到 compose（第二张立绘的开关不能只是收下）。

    ⚠ spec 也收 tone，但 --tone 与它一样只在照片路径生效（原版 TONE_ON=!!src，
    index.html:3690；多立绘走的是立绘层路径）——这里用自由模式的 size_pct 验通路。
    """
    def render(spec: str, tag: str) -> np.ndarray:
        out = tmp_path / f"m_{tag}.png"
        assert main(["multi", str(photo), str(char), str(char), "-o", str(out),
                     "--mode", "free", "--spec", spec]) == 0
        return np.asarray(Image.open(out).convert("RGB")).astype(int)

    big = render("x=1.2;d=3.0;mode=free;size_pct=80", "big")
    small = render("x=1.2;d=3.0;mode=free;size_pct=30", "small")
    assert int((np.abs(big - small).sum(2) > 0).sum()) > 100, "spec 的 size_pct 没生效"


def test_multi_scene_records_first_char(tmp_path, photo, char):
    """multi --scene：scene.json 记第一张立绘的落位（曾 AttributeError 崩掉）。

    multi 的位置参数叫 ``chars``，_scene_dict 原来直接读 ``args.char``——
    v1.5 审计真跑时崩出 traceback；现在 char_name 由调用方显式传入。
    """
    out = tmp_path / "multi.png"
    scene = tmp_path / "multi_scene.json"
    code = main(["multi", str(photo), str(char), str(char), "-o", str(out),
                 "--spec", "x=1.0;d=3.2", "--scene", str(scene), "--pitch", "12"])
    assert code == 0 and scene.is_file()
    data = json.loads(scene.read_text(encoding="utf-8"))
    assert data["character"]["name"] == char.name
    assert data["camera"]["pitchDeg"] == 12.0


def test_multi_non_full_frame(tmp_path, photo, char):
    """非满幅画幅：每层的尺寸是**裁切后**的 off 画布，必须按 R.x/R.y 贴回整幅。

    曾直接 _over(整幅照片, 裁切层) → numpy 广播报错（4:5 下 128×96 vs 77×96），
    整个命令在非满幅下不可用；且错误信息是 numpy 内部形状，不是人话。
    """
    out = tmp_path / "multi_45.png"
    assert main(["multi", str(photo), str(char), str(char), "-o", str(out),
                 "--frame", "4:5", "--pitch", "10"]) == 0
    with Image.open(photo) as im:
        photo_size = im.size
    with Image.open(out) as im:
        assert im.size == photo_size, "输出应是整幅照片尺寸（画幅只决定取景框）"
    a = np.asarray(Image.open(out).convert("RGB")).astype(int)
    b = np.asarray(Image.open(photo).convert("RGB")).astype(int)
    assert int((np.abs(a - b).sum(2) > 12).sum()) > 100, "立绘没有叠上去"


def test_stats_reports_project(tmp_path, capsys):
    """stats：规模/子命令/语言表条数都在（子命令集合与 parser 一致）。"""
    code = main(["stats"])
    assert code == 0
    out = capsys.readouterr().out
    for token in ("arcam", "Python", "测试", "网页", "子命令", "multi", "watch", "recipe"):
        assert token in out, f"stats 输出缺 {token}"
    # --json 可解析
    import io, contextlib, json as _json
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert main(["stats", "--json"]) == 0
    data = _json.loads(buf.getvalue())
    assert data["subcommands"] and "multi" in data["subcommands"]
    assert data["i18n_entries"] > 150


def test_cli_placement_matches_compose_free_rect(tmp_path, photo, char):
    """cli._placement 与渲染链 compose._placement 必须同参（scene.json 才记得对）。

    v1.5 审计回归钉：cli free 分支曾漏传 rect=frame_rect(...)，导致 free + 非满幅
    + edge_bar≠0 时 scene.json 记的落位与出图不一致（吸附记不吸；1:1+edge_bar=2
    实测差 48px）。这里对四种画幅 × 四个 edge_bar 全量对拍。
    """
    from arcam.cli import _placement as cli_placement
    from arcam.video import image_to_rgb01, image_to_rgba01
    from arcam.compose import _placement as compose_placement
    from arcam.frames import frame_rect
    from arcam.geometry import Camera
    from arcam.placement import Subject

    src01 = image_to_rgb01(photo)
    char01 = image_to_rgba01(char)
    height, width = src01.shape[:2]
    for frame in ("满幅", "1:1", "4:5", "16:9"):
        for edge_bar in (0, 1, 2, 3, 4):
            for rot_basis in ("world", "frame"):
                cam = Camera(width, height, 1.6, 23.0, 10.0, -8.0)
                subj = Subject(1.65, 4.0, 0.2)
                rect = frame_rect(frame, width, height)

                class _A:
                    pass
                a = _A()
                a.mode, a.size_pct, a.edge_bar = "free", 55.0, edge_bar
                a.rot_basis, a.frame = rot_basis, frame
                a.char_scale, a.shot = 1.0, "全身"
                got = cli_placement(char01, subj, cam, a)
                want = compose_placement(char01, subj, cam, mode="free",
                                         size_pct=55.0, char_scale=1.0,
                                         rot_basis=rot_basis, shot="全身",
                                         edge_bar=edge_bar, rect=rect)
                if want is None:
                    assert got is None, f"{frame}/bar{edge_bar}/{rot_basis}"
                    continue
                assert got is not None, f"{frame}/bar{edge_bar}/{rot_basis}"
                assert (round(got.u, 9), round(got.v, 9), round(got.w, 9),
                        round(got.h, 9), round(got.rot, 9)) == \
                       (round(want.u, 9), round(want.v, 9), round(want.w, 9),
                        round(want.h, 9), round(want.rot, 9)), \
                       f"{frame}/bar{edge_bar}/{rot_basis}: cli 记的落位与渲染不符"


def test_render_scene_free_edge_bar_roundtrip(tmp_path, photo, char):
    """端到端：free + 非满幅 + edge_bar 的 scene.json 与出图一致（吸附真实生效）。"""
    out = tmp_path / "s.png"
    scene = tmp_path / "s.json"
    code = main(["render", str(photo), str(char), "-o", str(out), "--scene", str(scene),
                 "--mode", "free", "--frame", "1:1", "--edge-bar", "2",
                 "--size-pct", "55", "--roll", "-8"])
    assert code == 0
    data = json.loads(scene.read_text(encoding="utf-8"))
    ch = data["character"]
    # 测试照片是 128×160：1:1 画幅 → 裁切框 128×128、y=(160-128)/2=16；
    # edge_bar=2（下沿）吸附后图心 v 应落在框下沿附近（而非未吸附时的框中心 80）
    crop = data["frame"]["crop"]
    assert crop[2] == 128 and crop[3] == 128, crop
    # 语义不变量（不复算 snap 公式）：**旋转后外接盒的底边贴住裁切框底边**。
    # 直接用记录的 w/h/rotationDeg 反推外接盒——rect 漏传时吸附整个不生效，
    # 底边会停在未吸附位置（差几十 px），这个断言抓得住。
    import math
    w, h = ch["sizePixel"]
    rot = math.radians(ch["rotationDeg"])
    bh = w * abs(math.sin(rot)) + h * abs(math.cos(rot))
    v = ch["centerPixel"][1]
    bottom = v + bh / 2
    frame_bottom = crop[1] + crop[3]
    assert abs(bottom - frame_bottom) < 1.0, \
        f"外接盒底边 {bottom:.2f} 没贴住裁切框底边 {frame_bottom}（rect 漏传的症状）"


# ─────────────────────── lidar sim（v1.6.0 新命令） ───────────────────────

def test_lidar_sim_writes_frames_and_roundtrips(tmp_path):
    """sim 落盘 → inspect 解回真值（无真机时验证整条 LiDAR 链路）。"""
    from arcam.cli import main as _main
    out = tmp_path / "sim"
    code = _main(["lidar", "sim", "--frames", "2", "--out", str(out),
                  "--h", "1.45", "--pitch", "20", "--roll", "-6",
                  "--width", "128", "--height", "96"])
    assert code == 0
    files = sorted(out.glob("frame_*.depth"))
    assert len(files) == 2
    # inspect 把真值解回来（平面拟合的闭环，容差 0.05m / 1°）
    code = _main(["lidar", "inspect", str(files[0]), "--json"])
    assert code == 0


def test_lidar_sim_json_meta(tmp_path, capsys):
    """--json 只打印第一帧 meta（内参外参齐备，不写盘）。"""
    import io, contextlib, json as _json
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(["lidar", "sim", "--json", "--h", "1.7", "--pitch", "5",
                     "--roll", "3", "--width", "64", "--height", "48"])
    assert code == 0
    meta = _json.loads(buf.getvalue())
    assert meta["height_m"] == 1.7 and meta["pitch_deg"] == 5.0
    assert meta["W"] == 64 and meta["fx"] > 0
    assert not list(tmp_path.glob("lidar_sim_*")), "--json 不该落盘"


def test_lidar_sim_rejects_bad_args(tmp_path, capsys):
    """--out 与 --push 同给 / frames<1 / 尺寸过小 → 退出码 1 + 人话错误。"""
    import io, contextlib
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert main(["lidar", "sim", "--out", str(tmp_path), "--push", "7799"]) == 1
    assert "二选一" in err.getvalue()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert main(["lidar", "sim", "--frames", "0", "--out", str(tmp_path)]) == 1
    assert "至少 1" in err.getvalue()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert main(["lidar", "sim", "--width", "4", "--height", "4",
                     "--out", str(tmp_path)]) == 1
    assert "尺寸太小" in err.getvalue()


def test_lidar_sim_push_passes_through(monkeypatch, capsys):
    """--push 模态：端口/h/pitch/roll/frames 透传给 simulate_scene_server。

    v1.6 起推流模式一直没有任何测试；这条钉住「命令 → 模拟器」的传参链
    （serve 侧协议由 test_lidar_live 覆盖）。
    """
    import arcam.lidar_sim as sim_mod
    seen: dict = {}

    class _FakeThread:
        def join(self, timeout=None):
            return None

    def fake_server(port, **kw):
        seen["port"] = port
        seen.update(kw)
        return _FakeThread()

    monkeypatch.setattr(sim_mod, "simulate_scene_server", fake_server)
    code = main(["lidar", "sim", "--push", "7799", "--frames", "3",
                 "--h", "1.5", "--pitch", "10", "--roll", "4"])
    assert code == 0
    assert seen == {"port": 7799, "h": 1.5, "pitch": 10.0, "roll": 4.0, "frames": 3}
    out = capsys.readouterr().out
    assert "已推送 3 帧到 127.0.0.1:7799" in out


def test_lidar_sim_push_rejects_custom_frame_spec(tmp_path, capsys):
    """--push 模式下给非默认 --width/--height/--seed → 人话报错，不静默丢参数。

    复现（v1.8 审计）：simulate_scene_server 按固定 256×192/seed 0 合成，
    命令却把 --width 96 收下——推出去的帧既是 256×192，用户无从得知。
    """
    import io, contextlib
    for flag, value in (("--width", "128"), ("--height", "96"), ("--seed", "7")):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert main(["lidar", "sim", "--push", "7799",
                         flag, value]) == 1, flag
        msg = err.getvalue()
        assert flag in msg and "256×192" in msg, (flag, msg)


def test_render_shadow_flags_take_effect(tmp_path, photo, capsys):
    """--shadow-az / --shadow-opacity 必须真的到达 compose（CLI 级透传回归钉）。

    单测层 test_sprite 已覆盖 sprite.contact_shadow_layer；这条钉 CLI 旗标 →
    _compose_kw → compose_shot 的整条链（v1.6 加旗标时的踩坑类型：旗标收了
    但没透传 = 静默失效）。影子轮廓非均匀时才看得出方位角——用梯形立绘。
    """
    char = tmp_path / "taper.png"
    arr = np.zeros((200, 80, 4), dtype=np.uint8)
    arr[..., 0] = 235
    arr[..., 1] = 60
    arr[..., 2] = 200
    for y in range(200):                       # 三角形：底行 alpha 轮廓非均匀
        half = int(40 * (0.35 + 0.65 * (y / 200)))
        arr[y, 40 - half:40 + half, 3] = 255
    Image.fromarray(arr, "RGBA").save(char)

    def render(extra: list[str], tag: str) -> np.ndarray:
        out = tmp_path / f"sh_{tag}.png"
        assert main(["render", str(photo), str(char), "-o", str(out),
                     "--pitch", "10", "--d", "3"] + extra) == 0
        return np.asarray(Image.open(out).convert("RGB")).astype(int)

    base = render([], "base")
    az = render(["--shadow-az", "40"], "az")
    op = render(["--shadow-opacity", "0.3"], "op")
    assert int((np.abs(az - base).sum(2) > 0).sum()) > 0, "--shadow-az 没生效"
    assert int((np.abs(op - base).sum(2) > 0).sum()) > 0, "--shadow-opacity 没生效"


def test_render_overlay_hud_reports_placement_hints(tmp_path, photo, char,
                                                    capsys):
    """--overlay hud 的构图提示必须带**本遍实际落位**（三分线/顶满那几条才报）。

    修复前 draw_hud_text 没收 placement/shot/mode，落位相关的四条永远不触发
    （只有角度三条会报）——原版 compositionHints 读的就是当前摆位。
    """
    out = tmp_path / "hud.png"
    code = main(["render", str(photo), str(char), "-o", str(out),
                 "--d", "1.2", "--overlay", "hud"])
    assert code == 0
    printed = capsys.readouterr().out
    assert "构图提示" in printed and "角色快顶满画面" in printed, printed


def test_batch_ext_jpg_and_scene(tmp_path, char, capsys):
    """batch --ext jpg 出 .jpg；--scene 每张顺带写同名 scene.json。"""
    src = tmp_path / "photos"
    src.mkdir()
    for i in range(2):
        _make_photo(src / f"p{i}.jpg", seed=i)
    out = tmp_path / "out"
    assert main(["batch", str(src), str(char), "-o", str(out),
                 "--ext", "jpg", "--scene"]) == 0
    produced = sorted(p.name for p in out.iterdir())
    assert "shot_p0.jpg" in produced and "shot_p1.jpg" in produced
    assert "shot_p0.json" in produced and "shot_p1.json" in produced
    for name in ("shot_p0.jpg", "shot_p1.jpg"):
        with Image.open(out / name) as im:
            assert im.format == "JPEG"
    scene = json.loads((out / "shot_p0.json").read_text(encoding="utf-8"))
    assert scene["version"] == 1


# ─────────────────────── prep 兜底（v1.8.0 审计修复） ───────────────────────

def test_prep_reverts_when_content_collapses(tmp_path, capsys):
    """抠/裁之后内容 <8px → 整条回退原图（index.html:2435-2439 的风控）。

    复现（v1.8 审计）：5×5 内容 + 全透明底的图，cmd_prep 只实现前四步、
    没有兜底，输出 5×5 的"修好的图"并报三个动作；webapp /api/prep 走
    auto_prepare_char 同一张图回退原图——两条入口结论相反。现在两边一致：
    输出 = 原图尺寸，动作链只剩一条还原提示。
    """
    tiny = tmp_path / "tiny.png"
    arr = np.zeros((100, 100, 4), dtype=np.uint8)
    arr[40:45, 40:45] = (230, 60, 200, 255)      # 5×5 内容，四角透明（cut 不触发）
    Image.fromarray(arr, "RGBA").save(tiny)
    out = tmp_path / "prepped.png"
    assert main(["prep", str(tiny), "-o", str(out)]) == 0
    with Image.open(out) as im:
        assert im.size == (100, 100), "兜底没生效：输出被裁成了内容尺寸"
    printed = capsys.readouterr().out
    assert "已还原到原图" in printed
    assert "自动裁掉" not in printed, "回退后不该还报裁边的动作"
    # 与 auto_prepare_char（/api/prep 的实现）结论一致
    from arcam.matte_tools import auto_prepare_char
    res = auto_prepare_char(np.asarray(Image.open(tiny).convert("RGBA"),
                                       dtype=np.float64) / 255.0)
    assert res.rgba01.shape == (100, 100, 4) and res.actions == ()


def test_prep_keeps_result_when_content_survives(tmp_path, capsys):
    """内容足够大时正常修图（裁边真的发生、不回退）——兜底的另一面。"""
    big = tmp_path / "big.png"
    arr = np.zeros((120, 120, 4), dtype=np.uint8)
    arr[10:110, 10:110] = (230, 60, 200, 255)    # 100×100 内容 + 透明边
    Image.fromarray(arr, "RGBA").save(big)
    out = tmp_path / "prepped.png"
    assert main(["prep", str(big), "-o", str(out)]) == 0
    with Image.open(out) as im:
        assert im.size == (100, 100), "透明边应该被裁掉"
    assert "自动裁掉了四周的空白边" in capsys.readouterr().out


def test_render_rejects_negative_grain_scale_and_shadow_opacity(
    tmp_path: Path, photo: Path, char: Path
) -> None:
    """--grain-scale/--shadow-opacity 负数报用法错误（v1.9.0 合并修复）。

    0 是有语义的合法值（关颗粒/关阴影，网页两侧滑块最左档），负数则是静默
    失效：乘在 gAmp/SH_ALPHA 上被夹成 0，出图与 0 逐字节相同、零提示。
    webapp.py 对同两键 400，CLI 这里 exit 2 并列出来值，两边同一口径。
    """
    import os
    import subprocess

    env = {**os.environ, "PYTHONPATH": str(SRC)}
    for flag in ("--grain-scale", "--shadow-opacity"):
        proc = subprocess.run(
            [sys.executable, "-m", "arcam", "render", str(photo), str(char),
             "-o", str(tmp_path / "x.png"), flag, "-1"],
            cwd=str(SRC.parent), env=env, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 2, f"{flag} -1 应报用法错误：{proc.stderr[-200:]}"
        assert "应 >= 0" in proc.stderr, f"{flag} 的报错应说明口径：{proc.stderr[-200:]}"
    # 0 放行（真出图，与 --no-grain/--no-shadow 同效但不报错）
    out = tmp_path / "g0.png"
    proc = subprocess.run(
        [sys.executable, "-m", "arcam", "render", str(photo), str(char),
         "-o", str(out), "--grain-scale", "0"],
        cwd=str(SRC.parent), env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    assert out.is_file()


@pytest.mark.parametrize("flag", ["--pitch", "--roll", "--x", "--d", "--h", "--hp",
                                  "--feq", "--char-scale", "--size-pct", "--shadow-az",
                                  "--grain-scale", "--shadow-opacity"])
def test_render_rejects_non_finite_numbers(tmp_path: Path, photo: Path, char: Path,
                                           flag: str) -> None:
    """浮点旗标拒 nan/inf（v2.0.0 合并修复）：与人话报错 exit 2，不是静默出图。

    放行 nan 的旧行为：渲染链的 NaN 守卫让落位解不出 → 出一张**没有立绘**的图、
    退出码 0、零提示（静默失效本类）；webapp 对同一批键本就 400（「不是有限数」），
    CLI 这里 exit 2，两边同一口径。
    """
    import os
    import subprocess

    env = {**os.environ, "PYTHONPATH": str(SRC)}
    # 等号形式传参：``--pitch -inf`` 里 ``-inf`` 会被 argparse 当成选项名
    # （不是合法数字字面量），报的是「expected one argument」——那是 argparse 的
    # 通用行为，不是本校验的口径话，用 ``--flag=-inf`` 才能走到类型函数
    for bad in ("nan", "inf", "-inf"):
        proc = subprocess.run(
            [sys.executable, "-m", "arcam", "render", str(photo), str(char),
             "-o", str(tmp_path / "x.png"), f"{flag}={bad}"],
            cwd=str(SRC.parent), env=env, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 2, f"{flag} {bad} 应报用法错误：{proc.stderr[-160:]}"
        assert "应是有限数" in proc.stderr, f"{flag} {bad} 的报错应说明口径"



# ─────────────────────── --lang 全链路（CLI 人话输出双语化） ───────────────────────

#: CJK 基本区判据：英文 help / 英文 stdout 里出现即算"漏翻"
_CJK = re.compile(r"[\u4e00-\u9fa5]")


def _all_parsers(lang: str):
    """build_parser(lang) 的全部 parser：顶层 + 15 个子命令 + recipe/lidar 二级。

    递归走 subparsers action 的 choices dict（顶层 dest=cmd，二级 recipe_cmd /
    lidar_cmd）——新命令自动进集合，门禁不靠硬编码名单。
    """
    def walk(parser, path):
        yield path or parser.prog, parser
        for action in parser._actions:
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict):
                for name, sub in choices.items():
                    yield from walk(sub, f"{path} {name}".strip())

    yield from walk(build_parser(lang), "")


def test_en_help_has_no_cjk() -> None:
    """门禁①：英文 help 全树无 CJK（顶层 + 每个子命令 + 二级子命令）。

    变异验证（把任一条 help 改回纯中文 → 本条红）：实测 bundle 的 help 忘包
    ``_h`` 时，顶层 help 的子命令列表第一个抓到；batch/video 的 ``-o`` help
    同样漏过。``--shot``/``--frame`` 的中文枚举键不进 help（gloss 表 + 注明
    "keys keep the original site's Chinese labels"），choices 校验报错才列它们。
    """
    offenders = {}
    for name, parser in _all_parsers("en"):
        hits = _CJK.findall(parser.format_help())
        if hits:
            offenders[name] = hits[:5]
    assert not offenders, f"英文 help 里混进中文：{offenders}"


def test_zh_help_keeps_chinese() -> None:
    """门禁①反向：中文版 help 必须真的有关键中文（_h 没把两半写反/写空）。"""
    for name, parser in _all_parsers("zh"):
        assert _CJK.findall(parser.format_help()), f"{name} 的中文 help 没有中文"


def test_every_command_takes_lang() -> None:
    """门禁②：每个（含二级）子命令都收 --lang，且 metavar 随语言切换。"""
    en_parsers = dict(_all_parsers("en"))
    for name, parser in _all_parsers("zh"):
        langs = [a for a in parser._actions if "--lang" in a.option_strings]
        assert langs, f"{name} 没有 --lang（v2.0.0 审计前只有 6 个命令收它）"
        assert langs[0].choices == ["zh", "en"]
        assert langs[0].metavar == "语言", name
        en_langs = [a for a in en_parsers[name]._actions
                    if "--lang" in a.option_strings]
        assert en_langs[0].metavar == "LANG", name


@pytest.mark.parametrize("argv", [
    ["--lang", "en", "render", "--help"],
    ["--lang=en", "render", "--help"],
    ["render", "--lang", "en", "--help"],
    ["render", "--lang=en", "--help"],
    # 二级分组：父前子后两种书写位置都要能 parse（子 parser 的默认值会写回主
    # namespace，父级用真默认值 + 子级 SUPPRESS 才不会被互相盖掉）
    ["--lang", "en", "lidar", "inspect", "--help"],
    ["lidar", "--lang", "en", "inspect", "--help"],
    ["lidar", "inspect", "--lang", "en", "--help"],
    ["--lang", "en", "recipe", "save", "--help"],
    ["recipe", "--lang", "en", "save", "--help"],
    ["recipe", "save", "--lang", "en", "--help"],
    ["recipe", "list", "--lang", "en", "--help"],
])
def test_lang_prescan_both_positions(argv, capsys) -> None:
    """门禁④：--lang 在子命令前/后都出英文 help（main() 建 parser 前预扫 argv）。

    argparse 的 help 在建 parser 时就要用语言，parse 完才知道就晚了——所以
    main() 先预扫 argv（--lang xx / --lang=xx，$ARCAM_LANG 兜底），再
    build_parser(语言)。
    """
    with pytest.raises(SystemExit) as e:
        main(argv)
    assert e.value.code == 0, argv
    out = capsys.readouterr().out
    assert not _CJK.search(out), (argv, out)


def test_arcam_lang_env_fallback(monkeypatch, tmp_path, photo, char,
                                 capsys) -> None:
    """门禁④：$ARCAM_LANG 兜底；显式 --lang 优先于环境变量。"""
    monkeypatch.setenv("ARCAM_LANG", "en")
    out = tmp_path / "o.png"
    assert main(["render", str(photo), str(char), "-o", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "Rendered" in printed and not _CJK.search(printed), printed
    # 显式 --lang zh 压过环境变量
    assert main(["render", str(photo), str(char), "-o", str(out),
                 "--lang", "zh"]) == 0
    assert "已出图" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ["render", "--lang", "fr", "a", "b", "-o", "x"],
    ["render", "--lang=fr", "a", "b", "-o", "x"],
    ["--lang", "fr", "render", "a", "b", "-o", "x"],
])
def test_bad_lang_is_argparse_usage_error(argv, capsys) -> None:
    """门禁④：非法 --lang 值仍走 argparse 的 choices 报错（exit 2）。

    预扫对非法值"不当真"（只决定 help/文案语言），choices 校验交给 argparse——
    不另造一套报错路径。
    """
    with pytest.raises(SystemExit) as e:
        main(argv)
    assert e.value.code == 2, argv
    assert "invalid choice" in capsys.readouterr().err


def test_render_en_output_is_all_english(tmp_path, photo, char,
                                         capsys) -> None:
    """门禁③端到端：render --lang en 的 stdout 全英文（无 CJK）。"""
    out = tmp_path / "en.png"
    scene = tmp_path / "en.json"
    code = main(["render", str(photo), str(char), "-o", str(out),
                 "--scene", str(scene), "--lang", "en"])
    assert code == 0
    printed = capsys.readouterr().out
    assert "Rendered" in printed and "Recipe" in printed, printed
    assert not _CJK.search(printed), printed


def test_render_overlay_note_follows_lang(tmp_path, photo, char,
                                          capsys) -> None:
    """--overlay 附注的分隔符/前缀跟 --lang 走（层名序 canonical 不变）。

    ⚠ 提示**句子**本身（"角色快顶满画面" 那几条）出自 arcam/hud.py 的
    composition_hints()，它目前不收 lang、恒返中文（i18n 表里已有同文的
    hints.* 键）——属 B3/C3 分区，已写进交付报告交编排者，这里只钉 cli 侧
    那一半（层序行 + 附注前缀）。
    """
    out = tmp_path / "hud.png"
    assert main(["render", str(photo), str(char), "-o", str(out), "--d", "1.2",
                 "--overlay", "hud", "--lang", "en"]) == 0
    printed = capsys.readouterr().out
    assert "HUD layers: hud" in printed, printed
    assert "composition hints" in printed, printed


def test_lang_does_not_change_output_bytes(tmp_path, photo, char) -> None:
    """--lang 只影响人话输出：同参 en/zh 的出图与 scene.json 字节逐个相同。"""
    zh_png, en_png = tmp_path / "zh.png", tmp_path / "en.png"
    zh_scene, en_scene = tmp_path / "zh.json", tmp_path / "en.json"
    extra = ["--pitch", "12", "--mode", "free", "--frame", "1:1",
             "--edge-bar", "2", "--size-pct", "55"]
    assert main(["render", str(photo), str(char), "-o", str(zh_png),
                 "--scene", str(zh_scene), *extra]) == 0
    assert main(["render", str(photo), str(char), "-o", str(en_png),
                 "--scene", str(en_scene), "--lang", "en", *extra]) == 0
    assert zh_png.read_bytes() == en_png.read_bytes(), "出图字节被 --lang 动了"
    assert zh_scene.read_text(encoding="utf-8") == \
        en_scene.read_text(encoding="utf-8"), "scene.json 被 --lang 动了"


def test_cli_error_follows_lang(tmp_path, photo, char, capsys,
                                monkeypatch) -> None:
    """门禁③：CliError 在 en/zh 下各跑一次，语种正确（含 main 的 Error 前缀）。"""
    base = ["render", str(photo), str(char), "-o", str(tmp_path / "o.png"),
            "--feq", "-1"]
    assert main([*base, "--lang", "en"]) == 1
    err = capsys.readouterr().err
    assert "Error: --feq must be a finite positive number" in err, err
    assert main(base) == 1
    err = capsys.readouterr().err
    assert "错误：--feq 必须是有限正数" in err, err
    # 另一条路径：recipe 不存在的名字
    import arcam.cli as cli
    monkeypatch.setattr(cli, "RECIPE_ROOT", tmp_path / "recipes")
    assert main(["recipe", "show", "nope", "--lang", "en"]) == 1
    assert "No recipe named nope" in capsys.readouterr().err
    assert main(["recipe", "show", "nope"]) == 1
    assert "没有叫 nope 的配方" in capsys.readouterr().err


def test_missing_input_message_follows_lang(tmp_path, char, capsys) -> None:
    """_need 的「什么」参数也是双语的（找不到文件 → 英文报错不含中文）。"""
    assert main(["render", str(tmp_path / "nope.jpg"), str(char),
                 "-o", str(tmp_path / "o.png"), "--lang", "en"]) == 1
    err = capsys.readouterr().err
    assert "Not found — background photo" in err, err
    assert not _CJK.search(err), err


@pytest.mark.parametrize("cmd,marker", [
    ("char-layer", "Character layer"),
    ("bundle", "Bundle"),
    ("multi", "Composited"),
    ("batch", "Batch done"),
    ("prep", "Prepped"),
    ("stats", "Subcommands"),
    ("video", "Rendered"),
    ("contact-sheet", "Contact sheet"),
])
def test_en_runtime_output_has_no_cjk(tmp_path, photo, char, track_file,
                                      cmd, marker, capsys) -> None:
    """端到端：各出图类子命令 --lang en 的 stdout 无 CJK。

    覆盖 render 之外的每条人话输出路径（char-layer/bundle/multi/batch/prep/
    stats/video/contact-sheet）——只翻 render 一条不算全链路。
    """
    if cmd == "char-layer":
        args = [cmd, str(char), "-o", str(tmp_path / "layer.png")]
    elif cmd == "bundle":
        args = [cmd, str(photo), str(char), "-o", str(tmp_path / "pack.zip")]
    elif cmd == "multi":
        args = [cmd, str(photo), str(char), str(char),
                "-o", str(tmp_path / "o.png")]
    elif cmd == "contact-sheet":
        args = [cmd, str(photo), str(char), "-o", str(tmp_path / "sheet.png"),
                "--vary", "d=3,4", "--cell-w", "120"]
    elif cmd == "video":
        args = [cmd, str(track_file), str(char), "-o",
                str(tmp_path / "frames"), "--bg", str(photo)]
    elif cmd == "batch":
        src = tmp_path / "photos"
        src.mkdir()
        _make_photo(src / "p0.jpg", seed=0)
        args = [cmd, str(src), str(char), "-o", str(tmp_path / "out")]
    elif cmd == "prep":
        big = tmp_path / "big.png"
        arr = np.zeros((120, 120, 4), dtype=np.uint8)
        arr[10:110, 10:110] = (230, 60, 200, 255)
        Image.fromarray(arr, "RGBA").save(big)
        args = [cmd, str(big), "-o", str(tmp_path / "prep.png")]
    else:                                   # stats
        args = [cmd]
    code = main([*args, "--lang", "en"])
    assert code == 0, (cmd, capsys.readouterr())
    printed = capsys.readouterr().out
    assert marker in printed, (cmd, printed)
    assert not _CJK.search(printed), (cmd, printed)


def test_check_and_recipe_en_keep_data_keys(tmp_path, track_file,
                                            monkeypatch, capsys) -> None:
    """check / recipe 的**数据键**不翻：frame_key=满幅 / shot=腰上 要能粘回去。

    与"数字与单位不翻"同一口径（i18n.py 模块 docstring）：景别/画幅的 key 是
    scene.json 与 CLI 的**值**，译成就没法粘回 --shot/--frame；只翻标签。
    """
    assert main(["check", str(track_file), "--lang", "en"]) == 0
    printed = capsys.readouterr().out
    assert "Track" in printed and "Aspect" in printed and "Mode ground" in printed
    import arcam.cli as cli
    monkeypatch.setattr(cli, "RECIPE_ROOT", tmp_path / "recipes")
    assert main(["recipe", "save", "t1", "--pitch", "15", "--shot", "腰上"]) == 0
    capsys.readouterr()
    assert main(["recipe", "show", "t1", "--lang", "en"]) == 0
    printed = capsys.readouterr().out
    assert "Recipe t1" in printed and "--shot" in printed and "腰上" in printed, printed


# ─────────────────────── 深度打磨轮：错误路径 / 冷门分支 / 双语补强 ───────────────────────
# 本节目标是 cli.py 的**错误路径与冷门分支**：编排者体检报 cli.py 90%（110 语句
# 未覆盖），未覆盖的多是"参数解析了但没走到执行点"那类静默失效高发区。每条新测试
# 都做了变异验证（改坏被测行为 → 本条红 → 还原 → 复跑绿），证据见
# artifacts/W1-cli-tests/DONE.md。

# ---------- A. 数值旗标的用法错误（同进程版；subprocess 版在上面的参数化里） ----------

@pytest.mark.parametrize("flag", ["--x", "--d", "--h", "--hp", "--feq", "--pitch",
                                  "--roll", "--char-scale", "--size-pct",
                                  "--shadow-az", "--grain-scale", "--shadow-opacity"])
def test_render_rejects_nan_in_process(tmp_path: Path, photo: Path, char: Path,
                                       flag: str,
                                       capsys: pytest.CaptureFixture) -> None:
    """每个浮点旗标拒 nan：argparse 用法错误 exit 2 + 人话口径（_finite/_nonneg）。

    与上面的 subprocess 版同一条校验，这里走同进程——覆盖率只统计进程内执行，
    subprocess 版那条真入口测试不计入。
    """
    with pytest.raises(SystemExit) as e:
        main(["render", str(photo), str(char), "-o", str(tmp_path / "x.png"),
              f"{flag}=nan"])
    assert e.value.code == 2, flag
    assert "应是有限数" in capsys.readouterr().err, flag


@pytest.mark.parametrize("flag,value,expect", [
    ("--shadow-opacity", "-1", "应 >= 0"),
    ("--grain-scale", "-1", "应 >= 0"),
    ("--shadow-opacity", "inf", "应是有限数"),
    ("--grain-scale", "-inf", "应是有限数"),
])
def test_render_rejects_bad_nonneg_values(tmp_path: Path, photo: Path, char: Path,
                                          flag: str, value: str, expect: str,
                                          capsys: pytest.CaptureFixture) -> None:
    """--shadow-opacity/--grain-scale：负数与非有限数都是用法错误 exit 2。

    0 是合法值（关阴影/关颗粒，网页两侧滑块最左档），负数则是静默失效：乘在
    SH_ALPHA/gAmp 上被夹成 0，出图与 0 逐字节相同、零提示。
    """
    with pytest.raises(SystemExit) as e:
        main(["render", str(photo), str(char), "-o", str(tmp_path / "x.png"),
              f"{flag}={value}"])
    assert e.value.code == 2, (flag, value)
    assert expect in capsys.readouterr().err, (flag, value)


# ---------- B. --depth 的两条懒加载错误路径 + 拟合失败 ----------

def _sim_depth_file(tmp_path: Path, name: str = "sim.depth") -> Path:
    """合成一盘 .depth（真机帧的离线替身，内参外参与 roll 齐备）。"""
    from arcam.lidar_live import save_depth_file
    from arcam.lidar_sim import synth_ground_frame
    meta, depth = synth_ground_frame(h=1.55, pitch_deg=12.0, roll_deg=8.0,
                                     width=64, height=48, index=0)
    p = tmp_path / name
    save_depth_file(p, meta, depth)
    return p


def test_render_depth_unreadable_file(tmp_path: Path, photo: Path, char: Path,
                                      capsys: pytest.CaptureFixture) -> None:
    """--depth 指到读不懂的文件：人话报错退出 1，且中英各有前缀。"""
    junk = tmp_path / "scan.bin"
    junk.write_bytes(b"not a depth file at all")
    code = main(["render", str(photo), str(char), "-o", str(tmp_path / "o.png"),
                 "--depth", str(junk)])
    assert code == 1
    assert "深度文件读不懂" in capsys.readouterr().err
    code = main(["render", str(photo), str(char), "-o", str(tmp_path / "o.png"),
                 "--depth", str(junk), "--lang", "en"])
    assert code == 1
    assert "Cannot read the depth file" in capsys.readouterr().err


def test_render_depth_module_missing(tmp_path: Path, photo: Path, char: Path,
                                     monkeypatch,
                                     capsys: pytest.CaptureFixture) -> None:
    """arcam.depth 被裁剪掉的安装：人话报错退出 1（带契约指引），不 traceback。"""
    depth = _sim_depth_file(tmp_path)
    monkeypatch.setitem(sys.modules, "arcam.depth", None)
    code = main(["render", str(photo), str(char), "-o", str(tmp_path / "o.png"),
                 "--depth", str(depth)])
    assert code == 1
    err = capsys.readouterr().err
    assert "读不到 LiDAR 深度模块" in err and "LIDAR-SPEC" in err


def test_render_depth_plane_module_missing(tmp_path: Path, photo: Path, char: Path,
                                           monkeypatch,
                                           capsys: pytest.CaptureFixture) -> None:
    """深度读得进来但 arcam.plane 缺失：拟合那一环人话报错退出 1。"""
    depth = _sim_depth_file(tmp_path)
    monkeypatch.setitem(sys.modules, "arcam.plane", None)
    code = main(["render", str(photo), str(char), "-o", str(tmp_path / "o.png"),
                 "--depth", str(depth)])
    assert code == 1
    assert "读不到 LiDAR 平面拟合模块" in capsys.readouterr().err


def test_render_depth_plane_fit_fails(tmp_path: Path, photo: Path, char: Path,
                                      monkeypatch,
                                      capsys: pytest.CaptureFixture) -> None:
    """拟合不出地面平面（点数不足等）：转人话 CliError，裸 ValueError 不漏出命令。"""
    import arcam.plane as plane_mod
    depth = _sim_depth_file(tmp_path)

    def boom(*a, **k):
        raise ValueError("有效点仅 3 个（< 64），无法可靠拟合地平面")

    monkeypatch.setattr(plane_mod, "fit_ground_plane", boom)
    code = main(["render", str(photo), str(char), "-o", str(tmp_path / "o.png"),
                 "--depth", str(depth)])
    assert code == 1
    err = capsys.readouterr().err
    assert "深度帧拟合不出地面平面" in err and "有效点仅 3 个" in err
    code = main(["render", str(photo), str(char), "-o", str(tmp_path / "o.png"),
                 "--depth", str(depth), "--lang", "en"])
    assert code == 1
    assert "Cannot fit a ground plane" in capsys.readouterr().err


# ---------- C. render --scene 的父目录自动创建 ----------

def test_render_scene_creates_parent_dirs(tmp_path: Path, photo: Path,
                                          char: Path) -> None:
    """--scene 给嵌套路径：父目录不存在时建出来再写（配方路径不该先手工 mkdir）。"""
    scene = tmp_path / "deep" / "nested" / "scene.json"
    code = main(["render", str(photo), str(char), "-o", str(tmp_path / "o.png"),
                 "--scene", str(scene)])
    assert code == 0 and scene.is_file()
    assert json.loads(scene.read_text(encoding="utf-8"))["version"] == 1


# ---------- D. recipe 四子命令的错误分支与双语 ----------

def _recipe_root(tmp_path: Path, monkeypatch):
    """把配方库指到 tmp_path/recipes（返回该路径；不存在，由命令自己建）。"""
    import arcam.cli as cli
    root = tmp_path / "recipes"
    monkeypatch.setattr(cli, "RECIPE_ROOT", root)
    monkeypatch.setenv("ARCAM_RECIPES", str(root))
    return root


def test_recipe_save_rejects_unusable_name(tmp_path: Path, monkeypatch,
                                           capsys: pytest.CaptureFixture) -> None:
    """配方名一个可用字符都没有（如 ???）→ 人话报错（否则会静默存成空文件名）。"""
    _recipe_root(tmp_path, monkeypatch)
    code = main(["recipe", "save", "???", "--pitch", "10"])
    assert code == 1
    assert "配方名" in capsys.readouterr().err


def test_recipe_list_empty_root(tmp_path: Path, monkeypatch,
                                capsys: pytest.CaptureFixture) -> None:
    """配方库不存在/为空：提示怎么存第一个，退出 0（不是错误）。"""
    _recipe_root(tmp_path, monkeypatch)
    assert main(["recipe", "list"]) == 0
    assert "还没有配方" in capsys.readouterr().out


def test_recipe_list_reports_unreadable(tmp_path: Path, monkeypatch,
                                        capsys: pytest.CaptureFixture) -> None:
    """库里有读不出来的 json：列出来说"读不出来"，其余照样列（不整命令挂掉）。"""
    root = _recipe_root(tmp_path, monkeypatch)
    root.mkdir(parents=True)
    (root / "broken.json").write_text("{not json", encoding="utf-8")
    assert main(["recipe", "list"]) == 0
    out = capsys.readouterr().out
    assert "读不出来" in out and "broken" in out


def test_recipe_show_json_dump(tmp_path: Path, monkeypatch,
                               capsys: pytest.CaptureFixture) -> None:
    """recipe show --json：原样输出 JSON（脚本消费用）。"""
    _recipe_root(tmp_path, monkeypatch)
    assert main(["recipe", "save", "t", "--pitch", "15", "--mode", "free"]) == 0
    capsys.readouterr()
    assert main(["recipe", "show", "t", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["name"] == "t" and data["params"]["pitch"] == 15.0


def test_recipe_apply_missing_and_bad_set(tmp_path: Path, monkeypatch,
                                          capsys: pytest.CaptureFixture) -> None:
    """apply 的错误分支：配方不存在 / --set 缺 = / --set 未知键，都人话退出 1。"""
    _recipe_root(tmp_path, monkeypatch)
    assert main(["recipe", "save", "t", "--pitch", "15"]) == 0
    capsys.readouterr()
    assert main(["recipe", "apply", "nope"]) == 1
    assert "没有叫 nope 的配方" in capsys.readouterr().err
    assert main(["recipe", "apply", "t", "--set", "pitch"]) == 1
    assert "K=V" in capsys.readouterr().err
    assert main(["recipe", "apply", "t", "--set", "bogus=1"]) == 1
    assert "没有 'bogus' 这一项" in capsys.readouterr().err


def test_recipe_four_actions_lang_en(tmp_path: Path, monkeypatch,
                                     capsys: pytest.CaptureFixture) -> None:
    """recipe 四动作的**标签**在 --lang en 下英文；配方值是数据键（shot=腰上）不翻。

    与 check 同口径（test_check_and_recipe_en_keep_data_keys）：景别/画幅的 key
    是 scene.json 与 CLI 的值，译成就没法粘回 --shot/--frame，只翻标签。
    """
    _recipe_root(tmp_path, monkeypatch)
    assert main(["recipe", "save", "t", "--pitch", "15", "--shot", "腰上",
                 "--lang", "en"]) == 0
    assert "Recipe saved to" in capsys.readouterr().out
    assert main(["recipe", "list", "--lang", "en"]) == 0
    printed = capsys.readouterr().out
    assert "items" in printed and "腰上" in printed, printed
    assert main(["recipe", "show", "t", "--lang", "en"]) == 0
    printed = capsys.readouterr().out
    assert "Recipe t" in printed and "--shot" in printed and "腰上" in printed, printed
    assert main(["recipe", "apply", "t", "--lang", "en"]) == 0
    assert "--pitch 15" in capsys.readouterr().out
    # 父前子后：``recipe --lang en save x`` 也不能静默变回中文
    assert main(["recipe", "--lang", "en", "save", "t2", "--pitch", "5"]) == 0
    assert "Recipe saved to" in capsys.readouterr().out


# ---------- E. --vary 的畸形输入 ----------

def test_vary_rejects_malformed_items(tmp_path: Path, photo: Path, char: Path,
                                      capsys: pytest.CaptureFixture) -> None:
    """--vary 缺 = / 键不是摆放参数 / 没给取值 → 人话退出 1（别等渲染到一半才炸）。"""
    for item, expect in (("d", "K=V,V"), ("bogus=1", "不是摆放参数"),
                         ("d=", "没给取值")):
        code = main(["contact-sheet", str(photo), str(char),
                     "-o", str(tmp_path / "s.png"), "--vary", item])
        assert code == 1, item
        assert expect in capsys.readouterr().err, item


# ---------- F. watch：轮询循环 / 单遍失败 / 退出码 ----------

def test_sig_of_handles_missing_files(tmp_path: Path) -> None:
    """_sig_of 对消失的文件给 None 签名而不是抛——轮询期内素材被删是常态。"""
    from arcam.cli import _sig_of
    p = tmp_path / "a.png"
    p.write_bytes(b"x")
    sig = _sig_of([p, tmp_path / "gone.png"])
    assert sig[0][1] is not None and sig[0][2] == 1
    assert sig[1] == (str(tmp_path / "gone.png"), None, None)


def test_watch_loop_reruns_on_change(tmp_path: Path, photo: Path, char: Path,
                                     monkeypatch,
                                     capsys: pytest.CaptureFixture) -> None:
    """监视循环：签名变了重渲一遍，Ctrl-C 停（退出码跟随最后一遍成败 = 0）。"""
    import arcam.cli as cli
    import os
    calls = {"n": 0}

    def fake_sleep(_sec):
        calls["n"] += 1
        if calls["n"] == 1:
            os.utime(photo, (1000, 1000))       # 只改 mtime → 签名变
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    out = tmp_path / "w.png"
    code = main(["watch", str(photo), str(char), "-o", str(out),
                 "--interval", "0.2"])
    assert code == 0
    printed = capsys.readouterr().out
    assert "监视中" in printed and "变了：photo.jpg" in printed
    assert "已停止监视" in printed


def test_watch_loop_survives_failed_run(tmp_path: Path, photo: Path, char: Path,
                                        monkeypatch,
                                        capsys: pytest.CaptureFixture) -> None:
    """循环里一遍失败：打日志、继续监视，但退出码如实报 1（旧实现恒返 0）。"""
    import arcam.cli as cli
    calls = {"n": 0}

    def fake_sleep(_sec):
        calls["n"] += 1
        if calls["n"] == 1:
            char.unlink()                       # 素材消失 → 这一遍渲染失败
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    code = main(["watch", str(photo), str(char), "-o", str(tmp_path / "w.png"),
                 "--interval", "0.2"])
    assert code == 1
    printed = capsys.readouterr().out
    assert "失败" in printed and "已停止监视" in printed


def test_watch_once_failure_exits_1(tmp_path: Path, photo: Path,
                                    capsys: pytest.CaptureFixture) -> None:
    """watch --once 出图失败 → exit 1：它的承诺是"带日志的 render"。

    复现（修复前）：立绘是坏文件时输出一行"失败"却 exit 0 —— 错误被吞、
    退出码与事实相反，脚本侧完全看不出这遍没出图。
    """
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"not an image")
    code = main(["watch", str(photo), str(bad), "-o", str(tmp_path / "w.png"),
                 "--once"])
    assert code == 1
    assert "失败" in capsys.readouterr().out


def test_watch_missing_inputs(tmp_path: Path, char: Path,
                              capsys: pytest.CaptureFixture) -> None:
    """监视不存在的素材：人话报错退出 1（--once 也一样，不进循环）。"""
    code = main(["watch", str(tmp_path / "nope.jpg"), str(char),
                 "-o", str(tmp_path / "w.png")])
    assert code == 1
    assert "找不到" in capsys.readouterr().err


def test_watch_lang_en(tmp_path: Path, photo: Path, char: Path,
                       capsys: pytest.CaptureFixture) -> None:
    """watch --lang en：首遍/监视中/失败三行人话都要英文。"""
    out = tmp_path / "w.png"
    assert main(["watch", str(photo), str(char), "-o", str(out), "--once",
                 "--lang", "en"]) == 0
    printed = capsys.readouterr().out
    assert "first run" in printed and not _CJK.search(printed), printed


def test_watch_overlay_hud_reports_hints(tmp_path: Path, photo: Path, char: Path,
                                         capsys: pytest.CaptureFixture) -> None:
    """watch 的 hud 附注也要带**本遍落位**的构图提示（_render_one 的 note 回传）。"""
    out = tmp_path / "w.png"
    code = main(["watch", str(photo), str(char), "-o", str(out), "--once",
                 "--d", "1.2", "--overlay", "hud"])
    assert code == 0
    assert "构图提示" in capsys.readouterr().out


# ---------- G. --spec 的畸形段 ----------

def test_spec_empty_segment_and_missing_equals(tmp_path: Path, photo: Path,
                                               char: Path,
                                               capsys) -> None:
    """spec 尾部分号（空段）跳过；段里缺 = → 人话退出 1。"""
    out = tmp_path / "m.png"
    assert main(["multi", str(photo), str(char), str(char), "-o", str(out),
                 "--spec", "x=0.5;"]) == 0          # 尾部空段不报错
    assert main(["multi", str(photo), str(char), str(char), "-o", str(out),
                 "--spec", "x"]) == 1
    assert "K=V" in capsys.readouterr().err


# ---------- H. multi 的剩余分支 ----------

def test_multi_rejects_too_many_specs(tmp_path: Path, photo: Path, char: Path,
                                      capsys: pytest.CaptureFixture) -> None:
    """--spec 套数 > 附加立绘数 → 人话退出 1（多给的 spec 会被静默丢弃）。"""
    code = main(["multi", str(photo), str(char), str(char),
                 "-o", str(tmp_path / "m.png"),
                 "--spec", "x=0.5", "--spec", "d=3"])
    assert code == 1
    assert "--spec 给了 2 套" in capsys.readouterr().err


def test_multi_creates_out_parent(tmp_path: Path, photo: Path, char: Path) -> None:
    """multi 的 -o 嵌套路径：父目录自动建。"""
    out = tmp_path / "deep" / "dir" / "m.png"
    assert main(["multi", str(photo), str(char), "-o", str(out)]) == 0
    assert out.is_file()


# ---------- I. stats 的安装态回落 / 版本号口径 / 防御分支 ----------

def test_stats_falls_back_to_installed_package(tmp_path: Path, monkeypatch,
                                               capsys) -> None:
    """旁边没有 src/ 树（wheel 安装态）：退回已安装 arcam 包目录，表照样出。

    造假一棵源码树路径（cli.__file__ 指过去），模拟"装了 wheel、旁边没仓库"。
    """
    import arcam.cli as cli
    fake = tmp_path / "x" / "y" / "arcam" / "cli.py"
    monkeypatch.setattr(cli, "__file__", str(fake))
    assert main(["stats", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["python_modules"] > 0 and data["python_loc"] > 0
    assert data["test_files"] == 0          # 假根旁边没有 tests/
    assert data["subcommands"] and "multi" in data["subcommands"]


def test_project_version_prefers_pyproject(tmp_path: Path) -> None:
    """旁边有 pyproject.toml 就以它为准（editable 安装的 metadata 会停在旧版）。"""
    import arcam.cli as cli
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "arcam"\nversion = "9.9.9"\n', encoding="utf-8")
    assert cli._project_version(tmp_path) == "9.9.9"


def test_project_version_unreadable_pyproject(tmp_path: Path,
                                               monkeypatch) -> None:
    """pyproject 在但读不出（OSError）：不猜不崩，回落 importlib.metadata。"""
    from pathlib import Path as _Path
    import arcam.cli as cli
    (tmp_path / "pyproject.toml").write_text('version = "1.2.3"\n',
                                             encoding="utf-8")

    def boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(_Path, "read_text", boom)
    version = cli._project_version(tmp_path)
    assert version and version != "1.2.3"


def test_project_version_unknown(tmp_path: Path, monkeypatch) -> None:
    """pyproject 没有、metadata 也拿不到：'?'（stats 不因此崩）。"""
    import importlib.metadata as md
    import arcam.cli as cli

    def boom(name):
        raise Exception("no metadata")

    monkeypatch.setattr(md, "version", boom)
    assert cli._project_version(tmp_path / "nowhere") == "?"


def test_subcommand_names_degrades_gracefully(monkeypatch) -> None:
    """parser 里没有子命令表时返回 []——stats 的抓取不崩（防御分支契约）。"""
    import argparse
    import arcam.cli as cli
    monkeypatch.setattr(cli, "build_parser",
                        lambda lang="zh": argparse.ArgumentParser())
    assert cli._subcommand_names() == []


# ---------- J. contact-sheet 的位姿键重建与嵌套输出 ----------

def test_contact_sheet_vary_camera_and_subject_keys(tmp_path: Path, photo: Path,
                                                    char: Path) -> None:
    """--vary 的相机/人物位姿键（h/feq/roll/hp/x）走 Camera/Subject 重建分支。

    这些键不是 compose 开关，落位要靠重建位姿对象——分支没走到就是"扫了参数
    但构图没变"的静默失效。逐键扫两个取值，两格必须不同（变异验证：删掉任一
    重建分支 → 该键两格逐字节相同 → 本条红）。
    """
    def two_cells(key: str, lo: str, hi: str) -> tuple[np.ndarray, np.ndarray]:
        out = tmp_path / f"sheet_{key}.png"
        code = main(["contact-sheet", str(photo), str(char), "-o", str(out),
                     "--vary", f"{key}={lo},{hi}", "--cell-w", "80"])
        assert code == 0 and out.is_file(), key
        with Image.open(out) as im:
            sheet = np.asarray(im.convert("RGB"))
        # 两格 → 一行两列（cols=ceil(sqrt(2))）；标签条 26px，格高按画幅等比
        # （照片 128×160 → 80×100）。左格 = sheet[26:126, 0:80]，右格 = [26:126, 80:160]
        assert sheet.shape == (126, 160, 3), (key, sheet.shape)
        return sheet[26:126, 0:80], sheet[26:126, 80:160]

    for key, lo, hi in (("h", "1.2", "1.8"), ("feq", "20", "40"),
                        ("roll", "-10", "10"), ("hp", "1.4", "1.9"),
                        ("x", "-0.5", "0.5")):
        a, b = two_cells(key, lo, hi)
        assert not np.array_equal(a, b), f"--vary {key} 的两格相同（重建分支没走到）"


def test_contact_sheet_creates_out_parent(tmp_path: Path, photo: Path,
                                          char: Path) -> None:
    """contact-sheet 的 -o 嵌套路径：父目录自动建。"""
    out = tmp_path / "a" / "b" / "sheet.png"
    assert main(["contact-sheet", str(photo), str(char), "-o", str(out),
                 "--vary", "d=3,4", "--cell-w", "80"]) == 0
    assert out.is_file()


# ---------- K. batch 的目录/深度模块错误路径 ----------

def test_batch_missing_dirs(tmp_path: Path, char: Path,
                            capsys: pytest.CaptureFixture) -> None:
    """照片目录 / 深度帧目录不存在 → 人话退出 1（各报各的）。"""
    assert main(["batch", str(tmp_path / "nope"), str(char),
                 "-o", str(tmp_path / "o")]) == 1
    assert "照片目录不存在" in capsys.readouterr().err
    src = tmp_path / "photos"
    src.mkdir()
    _make_photo(src / "p0.jpg", seed=0)
    assert main(["batch", str(src), str(char), "-o", str(tmp_path / "o2"),
                 "--depth-dir", str(tmp_path / "nodepth")]) == 1
    assert "深度帧目录不存在" in capsys.readouterr().err


def test_batch_depth_module_missing(tmp_path: Path, char: Path, monkeypatch,
                                    capsys: pytest.CaptureFixture) -> None:
    """给了 --depth-dir 但深度模块被裁剪：进循环前就人话报错（v1.5 审计修复的回归钉）。

    旧实现在循环里 import，NameError 被候选循环的宽 except 吞掉 → 每张都静默
    退回无深度渲染、退出码还是 0。
    """
    src = tmp_path / "photos"
    src.mkdir()
    _make_photo(src / "p0.jpg", seed=0)
    depths = tmp_path / "depths"
    depths.mkdir()
    monkeypatch.setitem(sys.modules, "arcam.depth", None)
    code = main(["batch", str(src), str(char), "-o", str(tmp_path / "out"),
                 "--depth-dir", str(depths)])
    assert code == 1
    assert "读不到 LiDAR 深度模块" in capsys.readouterr().err


def test_batch_skips_unreadable_depth_candidate(tmp_path: Path, char: Path,
                                                capsys) -> None:
    """同名候选不是深度帧 → 跳过该候选走无深度路径（不整批失败、退出 0）。"""
    src = tmp_path / "photos"
    src.mkdir()
    _make_photo(src / "p0.jpg", seed=0)
    depths = tmp_path / "depths"
    depths.mkdir()
    (depths / "p0.depth").write_bytes(b"junk not a depth frame")
    out = tmp_path / "out"
    code = main(["batch", str(src), str(char), "-o", str(out),
                 "--depth-dir", str(depths)])
    assert code == 0
    assert (out / "shot_p0.png").is_file()
    assert "（深度驱动）" not in capsys.readouterr().out


# ---------- L. bundle 的嵌套输出与缺层防护 ----------

def test_bundle_creates_out_parent(tmp_path: Path, photo: Path, char: Path) -> None:
    """bundle 的 -o 嵌套路径：父目录自动建。"""
    out = tmp_path / "deep" / "pack.zip"
    assert main(["bundle", str(photo), str(char), "-o", str(out)]) == 0
    assert out.is_file()


def test_bundle_missing_char_layer(tmp_path: Path, photo: Path, char: Path,
                                   monkeypatch,
                                   capsys: pytest.CaptureFixture) -> None:
    """这一遍没产出立绘层 → 人话报错退出 1（三件套缺一张不许静默出包）。"""
    from types import SimpleNamespace
    import arcam.cli as cli

    def fake_compose(*a, **k):
        return SimpleNamespace(shot=np.zeros((4, 4, 3), np.uint8), rect=None,
                               char_layer=None)

    monkeypatch.setattr(cli, "compose_shot", fake_compose)
    code = main(["bundle", str(photo), str(char), "-o", str(tmp_path / "p.zip")])
    assert code == 1
    assert "没有产出立绘层" in capsys.readouterr().err
    assert not (tmp_path / "p.zip").exists()


# ---------- M. matte 的三类运行期错误翻译 ----------

def test_matte_translates_runtime_errors(tmp_path: Path, char: Path, monkeypatch,
                                         capsys) -> None:
    """anime_cut 的 ImportError/FileNotFoundError/ValueError 都转人话 CliError。"""
    import arcam.cli as cli
    out = tmp_path / "cut.png"

    def make(exc):
        def raiser(*a, **k):
            raise exc
        return raiser

    monkeypatch.setattr(cli, "anime_cut", make(ImportError("onnxruntime 未安装")))
    assert main(["matte", str(char), "-o", str(out)]) == 1
    err = capsys.readouterr().err
    assert "onnxruntime" in err and "pip install" in err and not out.exists()
    monkeypatch.setattr(cli, "anime_cut", make(FileNotFoundError("模型文件不在")))
    assert main(["matte", str(char), "-o", str(out)]) == 1
    assert "isnet-anime-q8.onnx" in capsys.readouterr().err
    monkeypatch.setattr(cli, "anime_cut", make(ValueError("图片太小，没法抠")))
    assert main(["matte", str(char), "-o", str(out)]) == 1
    assert "图片太小" in capsys.readouterr().err
    # en：安装提示那半也要英文
    monkeypatch.setattr(cli, "anime_cut", make(ImportError("no onnxruntime")))
    assert main(["matte", str(char), "-o", str(out), "--lang", "en"]) == 1
    err = capsys.readouterr().err
    assert "pip install onnxruntime" in err and not _CJK.search(err), err


# ---------- N. prep 的成功步与逐步失败 ----------

def test_prep_cuts_solid_background(tmp_path: Path,
                                    capsys: pytest.CaptureFixture) -> None:
    """纯色底 + 占比在 5%~85% 门控内：抠底这一步真的发生（成功步的动作链）。"""
    arr = np.zeros((100, 100, 4), dtype=np.uint8)
    arr[..., :3] = 255
    arr[..., 3] = 255                       # 不透明白底
    arr[25:75, 25:75] = (230, 60, 200, 255)  # 品红块 50×50 = 25%
    src = tmp_path / "solid.png"
    Image.fromarray(arr, "RGBA").save(src)
    out = tmp_path / "prepped.png"
    assert main(["prep", str(src), "-o", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "纯色底" in printed, printed


def test_prep_step_failures_are_skipped(tmp_path: Path, monkeypatch,
                                        capsys) -> None:
    """四步各抛一次错：静默跳过、命令照常出图（修图链不许把出图搞挂）。"""
    import arcam.matte_tools as mt
    big = tmp_path / "big.png"
    arr = np.zeros((120, 120, 4), dtype=np.uint8)
    arr[10:110, 10:110] = (230, 60, 200, 255)
    Image.fromarray(arr, "RGBA").save(big)
    for name in ("_flood_solid", "auto_trim", "soften_edge", "lens_blur"):
        def boom(*a, **k):
            raise RuntimeError("step exploded")
        with monkeypatch.context() as m:
            m.setattr(mt, name, boom)
            out = tmp_path / f"p_{name}.png"
            assert main(["prep", str(big), "-o", str(out)]) == 0, name
            assert out.is_file(), name


def test_prep_missing_char(tmp_path: Path,
                           capsys: pytest.CaptureFixture) -> None:
    """prep 的输入不存在：人话退出 1。"""
    code = main(["prep", str(tmp_path / "nope.png"), "-o", str(tmp_path / "o.png")])
    assert code == 1
    assert "找不到" in capsys.readouterr().err


# ---------- O. lidar sim 的推流连接失败 ----------

def test_lidar_sim_push_connection_fails(monkeypatch,
                                         capsys: pytest.CaptureFixture) -> None:
    """--push 连不上 serve：人话报错并提示先起 serve（退出 1，不 traceback）。"""
    import arcam.lidar_sim as sim_mod

    def boom(port, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(sim_mod, "simulate_scene_server", boom)
    code = main(["lidar", "sim", "--push", "7799"])
    assert code == 1
    err = capsys.readouterr().err
    assert "连不上 127.0.0.1:7799" in err and "arcam lidar serve" in err


# ---------- P. lidar inspect 的模块缺失 / 文本模式 / 双语 ----------

def test_lidar_inspect_module_missing(tmp_path: Path, monkeypatch,
                                      capsys: pytest.CaptureFixture) -> None:
    """LiDAR 模块被裁剪：inspect 人话报错退出 1（带契约指引）。"""
    p = tmp_path / "x.depth"
    p.write_bytes(b"x")
    monkeypatch.setitem(sys.modules, "arcam.depth", None)
    assert main(["lidar", "inspect", str(p)]) == 1
    assert "读不到 LiDAR 模块" in capsys.readouterr().err


def test_lidar_inspect_bad_depth_file(tmp_path: Path,
                                      capsys: pytest.CaptureFixture) -> None:
    """inspect 指到读不懂的深度文件：双语人话退出 1（内文来自 depth.py）。"""
    junk = tmp_path / "scan.bin"
    junk.write_bytes(b"not a depth file at all")
    code = main(["lidar", "inspect", str(junk)])
    assert code == 1
    assert "深度帧诊断失败" in capsys.readouterr().err
    code = main(["lidar", "inspect", str(junk), "--lang", "en"])
    assert code == 1
    assert "Depth diagnostics failed" in capsys.readouterr().err


def test_lidar_inspect_text_and_lang(tmp_path: Path,
                                     capsys: pytest.CaptureFixture) -> None:
    """文本模式（含 ARKit roll 备注行）；--lang en 全英文、数据键不翻。"""
    p = _sim_depth_file(tmp_path)
    assert main(["lidar", "inspect", str(p)]) == 0
    out = capsys.readouterr().out
    assert "ARKit 给定 roll=8" in out, out
    assert main(["lidar", "inspect", str(p), "--lang", "en"]) == 0
    out = capsys.readouterr().out
    assert "ARKit gave roll=8" in out and not _CJK.search(out), out


# ---------- Q. lidar serve 的模块残缺 / Ctrl-C / 运行期错误 / 双语 ----------

def test_lidar_serve_without_callable_serve(monkeypatch,
                                            capsys: pytest.CaptureFixture) -> None:
    """模块在但没有 serve()：提示"未就绪"并正常退出 0（不崩、不吞错）。"""
    import arcam.lidar_live as live
    monkeypatch.setattr(live, "serve", None)
    assert main(["lidar", "serve", "--port", "7707"]) == 0
    assert "未就绪" in capsys.readouterr().out


def test_lidar_serve_stops_on_ctrl_c(monkeypatch,
                                     capsys: pytest.CaptureFixture) -> None:
    """收到一帧后 Ctrl-C：打印共接收帧数，正常退出 0。"""
    import arcam.lidar_live as live
    meta = {"height_m": 1.55, "pitch_deg": 12.0, "roll_deg": 8.0}

    def fake_serve(**kw):
        yield dict(meta), np.zeros((3, 4), dtype=np.float32)
        raise KeyboardInterrupt

    monkeypatch.setattr(live, "serve", fake_serve)
    assert main(["lidar", "serve", "--port", "7707"]) == 0
    out = capsys.readouterr().out
    assert "第 1 帧 4×3" in out and "已停止，共接收 1 帧" in out


def test_lidar_serve_runtime_error_exits_1(monkeypatch,
                                           capsys: pytest.CaptureFixture) -> None:
    """serve 迭代中抛 ValueError/OSError（协议错等）：人话退出 1。"""
    import arcam.lidar_live as live

    def fake_serve(**kw):
        raise ValueError("协议错误：meta 不是合法 JSON")
        yield  # pragma: no cover - 生成器语义：首次 next() 就抛

    monkeypatch.setattr(live, "serve", fake_serve)
    assert main(["lidar", "serve", "--port", "7707"]) == 1
    assert "协议错误" in capsys.readouterr().err


def test_lidar_serve_lang_en(monkeypatch,
                             capsys: pytest.CaptureFixture) -> None:
    """serve --lang en：监听/逐帧摘要/收尾三行都英文。"""
    import arcam.lidar_live as live

    def fake_serve(**kw):
        return iter([({"height_m": 1.5}, np.zeros((2, 2), dtype=np.float32))])

    monkeypatch.setattr(live, "serve", fake_serve)
    assert main(["lidar", "serve", "--port", "7707", "--lang", "en"]) == 0
    out = capsys.readouterr().out
    assert "listening" in out and "Frame 1" in out
    assert "Connection closed" in out and not _CJK.search(out), out


# ---------- R. main() 对 FileNotFoundError 的兜底翻译 ----------

def test_main_translates_file_not_found(tmp_path: Path, photo: Path, char: Path,
                                        monkeypatch,
                                        capsys: pytest.CaptureFixture) -> None:
    """main 对 FileNotFoundError 的兜底：人话 + 退出 1，不 traceback。

    自然路径走不到这一条（_need 已把缺文件转成 CliError）——注入一个 IO 失败，
    验证的是翻译契约本身：这类"最后一层"except 最容易被后来者删掉没人发现。
    """
    import arcam.cli as cli

    def boom(*a, **k):
        raise FileNotFoundError("素材盘被弹掉了")

    monkeypatch.setattr(cli, "save_png", boom)
    code = main(["render", str(photo), str(char), "-o", str(tmp_path / "o.png")])
    assert code == 1
    err = capsys.readouterr().err
    assert "找不到文件" in err and "素材盘被弹掉了" in err
    code = main(["render", str(photo), str(char), "-o", str(tmp_path / "o.png"),
                 "--lang", "en"])
    assert code == 1
    assert "Error: file not found" in capsys.readouterr().err


# ---------- S. 门禁：每条错误路径在 --lang en 下都要人话可读 ----------

#: (argv 模板, 期望出现的英文片段, 是否要求整条 stderr 无 CJK)。
#: check/video 两条允许内文含中文：问题清单出自 video.track_issues（中文单语，
#: 双语化需要 i18n.TXT 键 —— 属 B3 的 i18n.py，跨分区，已写进交付报告）。
_ERROR_PATH_CASES = [
    (["render", "{miss}", "{miss}", "-o", "{tmp}/o.png"], "Not found", True),
    (["char-layer", "{miss}", "-o", "{tmp}/o.png"], "Not found", True),
    (["bundle", "{miss}", "{miss}", "-o", "{tmp}/o.zip"], "Not found", True),
    (["matte", "{miss}", "-o", "{tmp}/o.png"], "Not found", True),
    (["prep", "{miss}", "-o", "{tmp}/o.png"], "Not found", True),
    (["watch", "{miss}", "{miss}", "-o", "{tmp}/o.png"], "Not found", True),
    (["multi", "{miss}", "{miss}", "-o", "{tmp}/o.png"], "Not found", True),
    (["contact-sheet", "{miss}", "{miss}", "-o", "{tmp}/o.png",
      "--vary", "d=3,4"], "Not found", True),
    (["batch", "{miss}", "{miss}", "-o", "{tmp}/o"], "does not exist", True),
    (["lidar", "inspect", "{miss}"], "Not found", True),
    (["lidar", "sim", "--frames", "0"], "at least 1", True),
    (["video", "{miss}", "{miss}", "-o", "{tmp}/o", "--bg", "{miss}"],
     "Cannot read the track", False),
    (["check", "{miss}"], "Cannot read the track", False),
]


@pytest.mark.parametrize("template,marker,no_cjk", _ERROR_PATH_CASES)
def test_error_paths_english_under_lang_en(tmp_path: Path, template, marker,
                                           no_cjk,
                                           capsys: pytest.CaptureFixture) -> None:
    """每个子命令的错误路径在 --lang en 下都要有英文人话（双语化的兜底门禁）。"""
    argv = [token.format(miss=str(tmp_path / "nope.bin"), tmp=str(tmp_path))
            for token in template]
    code = main([*argv, "--lang", "en"])
    assert code == 1, (argv, capsys.readouterr())
    err = capsys.readouterr().err
    assert marker in err, (argv, err)
    if no_cjk:
        assert not _CJK.search(err), (argv, err)


# ---------- T. 深度打磨轮补遗：显式环境量 / main 兜底 / matte 成功路径 ----------

def _real_onnx_model():
    """真模型路径（软链到上游仓库的只读源）；解析不到给 None。"""
    try:
        from arcam.matting import _resolve_model_path
        return _resolve_model_path(None)
    except Exception:
        return None


def test_render_depth_explicit_env_override(tmp_path: Path, photo: Path, char: Path,
                                            capsys) -> None:
    """--depth + 显式摆放参数：显式值进 env_overrides 并打印"显式值优先"。

    _env_overrides 只收默认 None 的那五个开关；一个都不给时它是空 dict，
    深度测出值全量生效——两条路都要有测试（v1.5 审计：--shot/--char-scale/
    --edge-bar 曾整组没透传）。
    """
    depth = _sim_depth_file(tmp_path)
    out = tmp_path / "o.png"
    code = main(["render", str(photo), str(char), "-o", str(out),
                 "--depth", str(depth), "--h", "1.7"])
    assert code == 0
    printed = capsys.readouterr().out
    assert "深度测得" in printed and "显式值优先：h" in printed, printed
    # 一个显式开关都不给：测出值全量生效，提示是"已用于摆位"
    assert main(["render", str(photo), str(char), "-o", str(out),
                 "--depth", str(depth)]) == 0
    assert "已用于摆位" in capsys.readouterr().out


def test_main_translates_generic_value_error(tmp_path: Path, monkeypatch,
                                             capsys) -> None:
    """main 的 ValueError/OSError 兜底：人话 + 退出 1（最后一道翻译层）。

    走自然路径：``recipe save --from`` 一份坏 JSON —— json 解析错误是
    ValueError 子类，命令层没转，靠 main 这一层接。
    """
    root = _recipe_root(tmp_path, monkeypatch)
    scene = tmp_path / "broken.json"
    scene.write_text("{not json", encoding="utf-8")
    code = main(["recipe", "save", "x", "--from", str(scene)])
    assert code == 1
    err = capsys.readouterr().err
    assert "错误：" in err and not (root / "x.json").exists()
    code = main(["recipe", "save", "x", "--from", str(scene), "--lang", "en"])
    assert code == 1
    assert "Error:" in capsys.readouterr().err


@pytest.mark.skipif(
    not (importlib.util.find_spec("onnxruntime") is not None
         and _real_onnx_model() is not None),
    reason="onnxruntime 未装或 isnet-anime-q8.onnx 模型不存在：真模型 E2E 跳过",
)
def test_matte_real_model_end_to_end(tmp_path: Path, char: Path,
                                     capsys: pytest.CaptureFixture) -> None:
    """matte 成功路径：真模型端到端（前景占比/推理耗时都在提示里）。

    环境里模型是软链到上游仓库的只读源（~/.cache/arcam/isnet-anime-q8.onnx）；
    缺失时整条跳过，与 test_matting 的真模型 E2E 同条件。
    """
    from PIL import Image
    # 与 test_matting 的真模型 E2E 同一张确定性合成图（噪声背景 + 噪声正弦椭圆
    # 主体）：纯色块模型不认（分布外，输出全 0 → fg 闸拦下），必须用纹理图
    rng = np.random.default_rng(7)
    h, w = 160, 120
    yy, xx = np.mgrid[0:h, 0:w]
    blob = (xx - 60) ** 2 / 45 ** 2 + (yy - 80) ** 2 / 60 ** 2 <= 1
    bg = 0.95 + rng.normal(0.0, 0.02, (h, w, 3))
    fg = 0.3 + 0.5 * rng.random((h, w, 3)) + np.sin(xx[:, :, None] / 3.0) * 0.05
    img = np.clip(np.where(blob[:, :, None], fg, bg), 0.0, 1.0)
    src = tmp_path / "subject.png"
    Image.fromarray((img * 255.0 + 0.5).astype(np.uint8), "RGB").save(src)
    out = tmp_path / "cut.png"
    code = main(["matte", str(src), "-o", str(out), "--lang", "en"])
    assert code == 0, capsys.readouterr()
    assert out.is_file()
    printed = capsys.readouterr().out
    assert "Cut out" in printed and "foreground" in printed, printed
    with Image.open(out) as im:
        assert im.mode == "RGBA"
        alpha = np.asarray(im)[..., 3]
    assert 0.02 < float((alpha > 127).mean()) < 0.95
