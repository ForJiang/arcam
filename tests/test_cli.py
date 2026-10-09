"""CLI 冒烟：render / char-layer / bundle / check / video / matte 错误路径。

只在 tmp_path 里落文件；退出码约定 0 正常 / 1 运行期失败 / 2 用法错误。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from arcam.cli import main
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
