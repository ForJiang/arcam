"""tests/test_webapp.py —— WEB-SPEC §5 测试门禁（后端 Agent W1）。

ephemeral 端口（``port=0`` 起服务后读实际端口）+ 线程 serve，覆盖 SPEC §5 全部项：

1. ``GET /`` 200 + text/html，且引用了 style.css / app.js（含静态穿越防护）；
2. upload 坏图 → 400 JSON；正常照片/立绘/深度 → JSON 形状（含 64MB 上限闸门）；
3. render 全参数字段往返（合成照片 + 立绘 numpy 直传）→ ok / dataURI /
   scene 与本地 build_scene 逐字段一致 / scale=0.5 出图更小；
4. export 三种 target：png（PNG 魔数）、layer（RGBA）、bundle（zip 三件套）；
5. matte：模型缺失环境 → {ok:false,error 含下载指引}，HTTP 不是 500；
6. lidar inspect：lidar_sim 合成深度（ARCD 落盘帧）→ env 数值接近真值；
7. 反复并发 render 20 次不锁死（且结果确定性一致）。

另附 params 校验（键名/choices/范围/有限数）+ 缺素材 400 + 路由 404 的边角。
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import socket
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from arcam.depth import LiDARFrame, save_depth
from arcam.frames import frame_rect
from arcam.geometry import Camera
from arcam.lidar_sim import synth_ground_frame
from arcam.matting import MattingResult
from arcam.placement import Subject, current_placement
from arcam.scene import build_scene
from arcam.video import image_to_rgb01, image_to_rgba01
from arcam.webapp import make_server

# ---------------------------------------------------------------- 基建


@pytest.fixture()
def server():
    """ephemeral 端口起服务 + daemon 线程 serve；用例结束即关。"""
    srv = make_server("127.0.0.1", 0, open_browser=False)
    thread = threading.Thread(target=srv.serve_forever, daemon=True,
                              kwargs={"poll_interval": 0.05}, name="arcam-web-test")
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=10)


def _port(server) -> int:
    return int(server.server_address[1])


class Client:
    """测试用 HTTP 客户端：**连接复用** + 本机回环缺端口时退避重试。

    连接复用：整个用例只占一条 TCP 连接（HTTP/1.1 keep-alive），不往本机
    TIME_WAIT 堆几千条残留 —— 这台开发机的临时端口区间（~16k 个）会被同机
    其它进程占满，此时 ``connect()`` 报 errno 49
    （Can't assign requested address）。这是环境现象不是被测代码的问题，
    所以 :meth:`_fresh` 只对 errno 49 退避重试（其余 OSError 立刻抛）。
    """

    def __init__(self, port: int):
        self.port = int(port)
        self._conn: http.client.HTTPConnection | None = None

    def _fresh(self) -> None:
        last: OSError | None = None
        for attempt in range(60):
            try:
                self._conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                                        timeout=60)
                self._conn.connect()
                return
            except OSError as exc:
                last = exc
                if exc.errno != 49:            # 不是缺端口 → 真错误，直接抛
                    raise
                time.sleep(0.25)               # errno 49：等端口释放再试
        assert last is not None
        raise last

    def _send(self, method, path, body, headers):
        assert self._conn is not None
        self._conn.request(method, path, body=body, headers=headers or {})
        response = self._conn.getresponse()
        data = response.read()
        if response.will_close:                 # 服务端主动断连（如 64MB 拒绝）
            self._conn.close()
            self._conn = None
        return response.status, dict(response.getheaders()), data

    def request(self, method, path, body=None, headers=None):
        try:
            if self._conn is None:
                self._fresh()
            return self._send(method, path, body, headers)
        except (OSError, http.client.HTTPException):
            # 连接断了/被服务端关过：重连重发一次。本测试所有请求都是幂等的
            # （同一份素材重复上传/渲染/导出结果一致），重发安全。
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            self._fresh()
            return self._send(method, path, body, headers)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _as(target) -> Client:
    return target if isinstance(target, Client) else Client(target)


def request(target, method: str, path: str, body=None, headers=None,
            timeout: float = 60.0):
    """一发请求 → (status, headers, body bytes)。target 可以是端口或 Client。"""
    _ = timeout                                # Client 自带 60s 超时
    return _as(target).request(method, path, body, headers)


def post_json(target, path: str, payload: dict):
    return _as(target).request("POST", path, json.dumps(payload).encode("utf-8"),
                               {"Content-Type": "application/json"})


_BOUNDARY = "----arcam-w1-test-boundary-9f2c"


def _multipart(fields: dict) -> tuple[bytes, str]:
    """手工拼 multipart body（与浏览器 fetch(FormData) 同形：CRLF + 段头 + 段体）。"""
    out = []
    for name, (filename, data) in fields.items():
        out.append(("--" + _BOUNDARY).encode())
        out.append(
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"'.encode())
        out.append(b"Content-Type: application/octet-stream")
        out.append(b"")
        out.append(data)
    out.append(("--" + _BOUNDARY + "--").encode())
    out.append(b"")
    return b"\r\n".join(out), f"multipart/form-data; boundary={_BOUNDARY}"


def post_upload(target, fields: dict):
    body, ctype = _multipart(fields)
    return _as(target).request("POST", "/api/upload", body,
                               {"Content-Type": ctype})


def post_multipart(target, path: str, fields: dict):
    body, ctype = _multipart(fields)
    return _as(target).request("POST", path, body, {"Content-Type": ctype})


def post_inspect(target, depth: bytes):
    body, ctype = _multipart({"depth": ("scan.depth", depth)})
    return _as(target).request("POST", "/api/lidar/inspect", body,
                               {"Content-Type": ctype})


def decode_data_uri(image: str) -> Image.Image:
    assert image.startswith("data:image/png;base64,"), image[:40]
    return Image.open(io.BytesIO(base64.b64decode(image.split(",", 1)[1])))


# ---------------------------------------------------------------- 素材

def photo_u8(width: int = 192, height: int = 256) -> np.ndarray:
    """128 级中灰底 + 暗角 + 轻噪点（避免全平面对采光退化）。"""
    rng = np.random.default_rng(7)
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    arr[..., 0], arr[..., 1], arr[..., 2] = 90, 110, 140
    arr[: height // 3, : width // 2] = (60, 70, 90)
    arr = np.clip(arr.astype(np.int16) + rng.integers(-8, 8, arr.shape),
                  0, 255).astype(np.uint8)
    return arr


def char_u8(height: int = 120, width: int = 60) -> np.ndarray:
    """暖色不透明矩形立绘（全 alpha=255，fg=1.0）。"""
    arr = np.zeros((height, width, 4), dtype=np.uint8)
    arr[..., 0], arr[..., 2], arr[..., 3] = 230, 200, 255
    return arr


def _png(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr, "RGBA" if arr.shape[2] == 4 else "RGB").save(buf, format="PNG")
    return buf.getvalue()


PHOTO = _png(photo_u8())
CHAR = _png(char_u8())

#: WEB-SPEC §2 全字段（= 原版 S 默认）
FULL_PARAMS: dict = {
    "x": 0.0, "d": 4.0, "h": 1.6, "hp": 1.65, "feq": 23.0, "pitch": 0.0, "roll": 0.0,
    "shot": "全身", "char_scale": 1.0, "frame": "满幅", "rot_basis": "world",
    "mode": "ground", "size_pct": 60.0, "edge_bar": 0, "tone": False,
    "depth_uploaded": False,
}


@pytest.fixture()
def uploaded(server):
    """上传照片 + 立绘，返回复用同一连接的 Client。"""
    client = Client(_port(server))
    status, _, raw = post_upload(client, {"photo": ("photo.png", PHOTO),
                                          "char": ("my-char.png", CHAR)})
    assert status == 200 and json.loads(raw)["ok"] is True
    try:
        yield client
    finally:
        client.close()


@pytest.fixture()
def photo_file(tmp_path: Path, request) -> Path:
    """把合成照片落成文件（image_to_rgb01 只吃路径）。"""
    p = tmp_path / "photo.png"
    Image.fromarray(photo_u8(), "RGB").save(p, format="PNG")
    return p


@pytest.fixture()
def char_file(tmp_path: Path) -> Path:
    p = tmp_path / "char.png"
    Image.fromarray(char_u8(), "RGBA").save(p, format="PNG")
    return p


# ---------------------------------------------------------------- 1. 静态页

class TestStatic:
    def test_index_html_200_and_references_assets(self, server):
        status, headers, raw = request(_port(server), "GET", "/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        text = raw.decode("utf-8")
        assert "style.css" in text and "app.js" in text

    def test_unknown_and_traversal_paths_404_json(self, server):
        port = _port(server)
        for path in ("/static/nope.css", "/static/../conftest.py",
                     "/static/..%2f..%2fconftest.py", "/static/", "/api/nope",
                     "/favicon.ico"):
            status, _, raw = request(port, "GET", path)
            assert status == 404, path
            payload = json.loads(raw)
            assert payload["ok"] is False and payload["error"]

    def test_static_serves_webui_file_when_present(self, server, tmp_path,
                                                   monkeypatch):
        """webui/ 就绪时 /static/* 透传该文件与 MIME（W2 的目录，这里临时造一个）。"""
        import arcam.webapp as webapp

        root = tmp_path / "webui"
        (root / "css").mkdir(parents=True)
        (root / "index.html").write_text("<html>w2</html>", encoding="utf-8")
        (root / "style.css").write_text("body{}", encoding="utf-8")
        (root / "css" / "app.js").write_text("//app", encoding="utf-8")
        (root / "logo.png").write_bytes(_png(char_u8(4, 4)))
        (root / "favicon.ico").write_bytes(b"\x00\x00\x01\x00ico")
        monkeypatch.setattr(webapp, "WEBUI_DIR", root)

        port = _port(server)
        status, headers, raw = request(port, "GET", "/")
        assert status == 200 and raw == b"<html>w2</html>"
        status, headers, raw = request(port, "GET", "/static/style.css")
        assert (status, raw) == (200, b"body{}")
        assert headers["Content-Type"].startswith("text/css")
        status, headers, raw = request(port, "GET", "/static/css/app.js")
        assert (status, raw) == (200, b"//app")
        assert "javascript" in headers["Content-Type"]
        status, headers, _ = request(port, "GET", "/static/logo.png")
        assert status == 200 and headers["Content-Type"] == "image/png"
        status, headers, _ = request(port, "GET", "/static/favicon.ico")
        assert status == 200 and headers["Content-Type"] == "image/x-icon"
        # 越过根目录依然 404
        status, _, _ = request(port, "GET", "/static/../webapp.py")
        assert status == 404


    def test_static_null_byte_and_oversize_path_400_not_500(self, server):
        """%00 等让路径解析本身失败的 URL → 400 人话 JSON（坏输入不升级成 500）。

        回归：``Path.resolve()`` 对含 NUL 的路径抛 ``ValueError: lstat: embedded
        null character``，旧实现掉进 500 兜底并把内部报错文本透给浏览器；
        超长路径同属此类。WEB-SPEC §1：坏输入 → 400，响应体永不出现 traceback。
        """
        port = _port(server)
        for path in ("/static/app.js%00.png", "/static/%00", "/static/a%00b.css",
                     "/static/" + "a" * 5000 + ".css"):
            status, _, raw = request(port, "GET", path)
            assert status in (400, 404), (path, status, raw[:120])
            payload = json.loads(raw)
            assert payload["ok"] is False and payload["error"]
            assert b"Traceback" not in raw and b"lstat" not in raw


# ---------------------------------------------------------------- 2. upload

class TestUpload:
    def test_bad_image_400_json(self, server):
        status, _, raw = post_upload(_port(server), {"photo": ("bad.png", b"not an image at all")})
        assert status == 400
        payload = json.loads(raw)
        assert payload["ok"] is False
        assert "照片" in payload["error"] and payload.get("hint")

    def test_bad_depth_400_json(self, server):
        status, _, raw = post_upload(_port(server), {"depth": ("x.npy", b"\x00\x01\x02")})
        assert status == 400
        payload = json.loads(raw)
        assert payload["ok"] is False and "深度" in payload["error"]

    def test_good_photo_and_char_shapes(self, server):
        port = _port(server)
        status, _, raw = post_upload(port, {"photo": ("photo.png", PHOTO)})
        assert status == 200
        payload = json.loads(raw)
        assert payload["ok"] is True and payload["kind"] == "photo"
        assert payload["width"] == 192 and payload["height"] == 256

        status, _, raw = post_upload(port, {"char": ("my-char.png", CHAR)})
        assert status == 200
        payload = json.loads(raw)
        assert payload["ok"] is True and payload["kind"] == "char"
        assert payload["width"] == 60 and payload["height"] == 120
        assert payload["fg"] == 1.0          # 全不透明立绘

    def test_char_without_alpha_reports_fg_false(self, server):
        """没 alpha 通道的立绘（JPEG/不透明图）→ fg=false：前端据此提示先抠图。

        这是与 webui/app.js 的对接契约（``r.fg === false`` → 显示「普通图可点抠图」）；
        带 alpha 的图返回不透明像素占比（见 test_good_photo_and_char_shapes）。
        """
        buf = io.BytesIO()
        Image.fromarray(photo_u8(60, 120), "RGB").save(buf, format="JPEG")
        status, _, raw = post_upload(_port(server), {"char": ("flat.jpg", buf.getvalue())})
        assert status == 200
        payload = json.loads(raw)
        assert payload["ok"] is True and payload["kind"] == "char"
        assert payload["fg"] is False
        assert payload["ms"] >= 0.0            # 上传也回耗时（前端状态条用）

    def test_wrong_field_name_400(self, server):
        status, _, raw = post_upload(_port(server), {"img": ("a.png", PHOTO)})
        assert status == 400
        assert json.loads(raw)["error"].find("photo / char / depth") >= 0

    def test_oversize_body_rejected_before_reading(self, server):
        """>64MB 请求体在读一字节之前就被 400（raw socket 只发头，不发垃圾）。"""
        port = _port(server)
        raw = b""
        for attempt in range(60):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=15) as sock:
                    sock.sendall(
                        b"POST /api/upload HTTP/1.1\r\n"
                        b"Host: 127.0.0.1\r\n"
                        b"Content-Type: multipart/form-data; boundary=xyz\r\n"
                        b"Content-Length: 67108865\r\n\r\n")
                    chunks = []
                    while True:
                        piece = sock.recv(65536)
                        if not piece:
                            break
                        chunks.append(piece)
                raw = b"".join(chunks)
                break
            except OSError as exc:
                if exc.errno != 49 or attempt == 59:
                    raise
                time.sleep(0.25)
        head, _, body = raw.partition(b"\r\n\r\n")
        assert b" 400 " in head.split(b"\r\n")[0]
        payload = json.loads(body)
        assert payload["ok"] is False
        assert "64MB" in payload["error"] and payload.get("hint")


# ---------------------------------------------------------------- 3. render

class TestRender:
    def test_full_params_roundtrip_matches_build_scene(self, server, uploaded,
                                                       photo_file, char_file):
        status, _, raw = post_json(uploaded, "/api/render",
                                   {"params": FULL_PARAMS, "scale": 1.0})
        assert status == 200
        payload = json.loads(raw)
        assert payload["ok"] is True
        assert payload["env"] is None                     # 无深度 → env null
        assert isinstance(payload["ms"], (int, float)) and payload["ms"] >= 0

        image = decode_data_uri(payload["image"])
        assert image.size == (192, 256)                   # 满幅 = 整块画布

        # 本地重放同一条链：Camera/Subject → placement → build_scene
        src01 = image_to_rgb01(photo_file)
        char01 = image_to_rgba01(char_file)
        cam = Camera(192, 256, 1.6, 23.0, 0.0, 0.0)
        subj = Subject(1.65, 4.0, 0.0)
        placement = current_placement(char01, subj, cam, mode="ground",
                                      char_scale=1.0, rot_basis="world",
                                      shot="全身")
        expected = build_scene(frame_rect("满幅", 192, 256), placement, subj, cam,
                              frame_key="满幅", char_rgba01=char01,
                              char_name="my-char.png", mode="ground", edge_bar=0,
                              shot="全身", char_scale=1.0)
        assert payload["scene"] == expected

    def test_scale_halves_output(self, server, uploaded):
        status, _, raw = post_json(uploaded, "/api/render",
                                   {"params": FULL_PARAMS, "scale": 1.0})
        full = json.loads(raw)
        status, _, raw = post_json(uploaded, "/api/render",
                                   {"params": FULL_PARAMS, "scale": 0.5})
        half = json.loads(raw)
        assert half["ok"] is True
        full_img = decode_data_uri(full["image"])
        half_img = decode_data_uri(half["image"])
        assert half_img.size == (96, 128)
        assert half_img.size[0] < full_img.size[0]
        # 预览更快（或至少不会更慢一个量级）；确定性输出
        assert len(half["image"]) < len(full["image"])
        assert half["scene"]["frame"]["source"] == {"width": 96, "height": 128}

    def test_render_without_photo_400(self, server):
        status, _, raw = post_json(_port(server), "/api/render",
                                   {"params": FULL_PARAMS})
        assert status == 400
        payload = json.loads(raw)
        assert payload["ok"] is False and "照片" in payload["error"]

    def test_free_mode_and_frame_crop(self, server, uploaded):
        params = {**FULL_PARAMS, "mode": "free", "frame": "1:1", "size_pct": 70.0,
                  "edge_bar": 2, "tone": True}
        status, _, raw = post_json(uploaded, "/api/render",
                                   {"params": params, "scale": 0.5})
        assert status == 200
        payload = json.loads(raw)
        assert payload["ok"] is True
        assert payload["scene"]["character"]["mode"] == "free"
        assert payload["scene"]["edge"] == {"bar": 2, "rotationLocked": True}
        assert decode_data_uri(payload["image"]).size == (96, 96)


class TestParamsValidation:
    @pytest.mark.parametrize("bad,hint_key", [
        ({"shot": "半身"}, "shot"),
        ({"frame": "4比3"}, "frame"),
        ({"mode": "air"}, "mode"),
        ({"rot_basis": "screen"}, "rot_basis"),
        ({"edge_bar": 5}, "edge_bar"),
        ({"edge_bar": 1.5}, "edge_bar"),
        ({"h": 0.0}, "h"),
        ({"feq": -3.0}, "feq"),
        ({"d": "4"}, "d"),
        ({"tone": "yes"}, "tone"),
        ({"depth_uploaded": 1}, "depth_uploaded"),
    ])
    def test_bad_values_400_with_hint(self, server, uploaded, bad, hint_key):
        status, _, raw = post_json(uploaded, "/api/render",
                                   {"params": {**FULL_PARAMS, **bad}})
        assert status == 400
        payload = json.loads(raw)
        assert payload["ok"] is False
        assert hint_key in payload["error"]
        assert payload.get("hint")

    def test_unknown_key_400_with_guess(self, server, uploaded):
        status, _, raw = post_json(uploaded, "/api/render",
                                   {"params": {**FULL_PARAMS, "distence": 5.0}})
        assert status == 400
        payload = json.loads(raw)
        assert payload["ok"] is False
        assert "distence" in payload["error"] and payload["hint"]

    def test_nan_infinity_and_unknown_type_rejected(self, server, uploaded):
        status, _, raw = post_json(uploaded, "/api/render",
                                   {"params": {"h": float("nan")}})
        assert status == 400 and json.loads(raw)["ok"] is False
        status, _, raw = post_json(uploaded, "/api/render",
                                   {"params": {"pitch": float("inf")}})
        assert status == 400 and json.loads(raw)["ok"] is False
        status, _, raw = post_json(uploaded, "/api/render",
                                   {"params": "全部默认"})
        assert status == 400 and json.loads(raw)["ok"] is False

    def test_scale_bounds(self, server, uploaded):
        for scale in (1.5, 0.0, -0.5, float("nan"), "half"):
            status, _, raw = post_json(uploaded, "/api/render",
                                       {"params": FULL_PARAMS, "scale": scale})
            assert status == 400, scale
            assert json.loads(raw)["ok"] is False

    def test_non_json_body_400(self, server, uploaded):
        status, _, raw = request(uploaded, "POST", "/api/render", b"<xml/>",
                                 {"Content-Type": "application/json"})
        assert status == 400 and json.loads(raw)["ok"] is False


# ---------------------------------------------------------------- keep-alive 卫生

class TestKeepAliveHygiene:
    """回归：每个 POST 接口都必须消费请求体（漏读会把残体拼进下一条请求行 → 501）。

    ``/api/matte-result-as-char`` 曾漏读 body（前端发的 ``{}`` 留在 socket 里，
    下一条 ``POST /api/render`` 的请求行变成 ``{}POST /api/render`` → 501），
    这里对全部 POST 路径逐一验证「带 body 请求 → 同连接下一条正常请求仍 200」。
    """

    @pytest.mark.parametrize("path", [
        "/api/matte-result-as-char", "/api/matte", "/api/render", "/api/export",
        "/api/lidar/inspect", "/api/nope",
    ])
    def test_body_consumed_and_connection_usable(self, server, uploaded, path):
        client = Client(_port(server))
        try:
            status, _, raw = client.request(
                "POST", path, b'{"pad":true}',
                {"Content-Type": "application/json"})
            # 未知路径 404 也算「读干净了 body 且响应正常」
            assert status in (200, 400, 404), (path, status, raw[:120])
            # 同一个 TCP 连接紧接着发一条正常 render：不能是 501/被搅坏
            again = post_json(client, "/api/render",
                              {"params": FULL_PARAMS, "scale": 0.5})
            assert again[0] == 200, (path, again[2][:160])
            assert json.loads(again[2])["ok"] is True
        finally:
            client.close()


# ---------------------------------------------------------------- 4. export

class TestExport:
    def test_png_target(self, server, uploaded):
        status, headers, raw = post_json(uploaded, "/api/export",
                                         {"params": FULL_PARAMS, "target": "png"})
        assert status == 200
        assert headers["Content-Type"] == "image/png"
        assert "attachment" in headers["Content-Disposition"]
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"

    def test_layer_target_rgba(self, server, uploaded):
        status, headers, raw = post_json(uploaded, "/api/export",
                                         {"params": FULL_PARAMS, "target": "layer"})
        assert status == 200
        assert headers["Content-Type"] == "image/png"
        assert "arcam-char-layer.png" in headers["Content-Disposition"]
        layer = Image.open(io.BytesIO(raw))
        assert layer.mode == "RGBA"
        assert layer.size == (192, 256)

    def test_bundle_target_zip_three_parts(self, server, uploaded):
        status, headers, raw = post_json(uploaded, "/api/export",
                                         {"params": FULL_PARAMS, "target": "bundle"})
        assert status == 200
        assert headers["Content-Type"] == "application/zip"
        assert "arcam-bundle.zip" in headers["Content-Disposition"]
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            assert zf.namelist() == ["shot.jpg", "scene.json", "character.png"]
            assert zf.read("shot.jpg")[:3] == b"\xff\xd8\xff"      # JPEG 魔数
            scene = json.loads(zf.read("scene.json"))
            assert scene["version"] == 1 and scene["image"]["file"] == "shot.jpg"
            character = Image.open(io.BytesIO(zf.read("character.png")))
            assert character.mode == "RGBA"

    def test_export_requires_char_for_layer_and_bundle(self, server):
        port = _port(server)
        post_upload(port, {"photo": ("photo.png", PHOTO)})
        for target in ("layer", "bundle"):
            status, _, raw = post_json(port, "/api/export",
                                       {"params": FULL_PARAMS, "target": target})
            assert status == 400, target
            assert json.loads(raw)["ok"] is False

    def test_bad_target_400(self, server, uploaded):
        status, _, raw = post_json(uploaded, "/api/export",
                                   {"params": FULL_PARAMS, "target": "mp4"})
        assert status == 400
        payload = json.loads(raw)
        assert payload["ok"] is False and "png" in payload["hint"]

    def test_export_ignores_scale_full_size(self, server, uploaded):
        status, _, raw = post_json(uploaded, "/api/export",
                                   {"params": FULL_PARAMS, "target": "png"})
        assert status == 200
        assert Image.open(io.BytesIO(raw)).size == (192, 256)


# ---------------------------------------------------------------- 5. matte

class TestMatte:
    def test_missing_model_400_never_500(self, server, uploaded, monkeypatch):
        """模型缺失环境：{ok:false, error 含下载指引}，HTTP 400（绝不是 500）。"""
        import arcam.matting as matting

        def boom(_path):
            raise FileNotFoundError(
                "未找到 ISNet-anime 模型。\n请从 HuggingFace skytnt/anime-seg 下载 "
                "isnet-anime-q8.onnx，放到仓库 anime/ 或 ~/.cache/arcam/，或设 "
                "$ARCAM_ISNET_MODEL 指向模型文件。")

        monkeypatch.setattr(matting, "_resolve_model_path", boom)
        status, _, raw = post_json(uploaded, "/api/matte", {"source": "photo"})
        assert status in (200, 400) and status != 500
        payload = json.loads(raw)
        assert payload["ok"] is False
        assert "HuggingFace" in payload["error"]
        assert payload.get("hint")

    def test_runtime_missing_400_with_pip_hint(self, server, uploaded, monkeypatch):
        import arcam.matting as matting

        def no_runtime(_path):
            raise ImportError("onnxruntime 未安装")

        monkeypatch.setattr(matting, "_resolve_model_path", no_runtime)
        status, _, raw = post_json(uploaded, "/api/matte", {"source": "photo"})
        assert status == 400
        payload = json.loads(raw)
        assert payload["ok"] is False and "onnxruntime" in payload["error"]
        assert "pip install" in payload["hint"]

    def test_matte_result_reusable_as_char(self, server, uploaded, monkeypatch):
        """抠图 → 结果设为当前立绘 → render 的 scene 用上它（fg/ms 透传）。"""
        import arcam.webapp as webapp

        cut = char_u8(120, 60)
        cut[:, :, 3] = 0
        cut[10:110, 5:55] = (230, 200, 255, 255)

        def fake_cut(rgb01, model_path=None):
            return MattingResult(rgba=cut, fg=0.3125, ms=7.5)

        monkeypatch.setattr(webapp, "anime_cut", fake_cut)
        status, _, raw = post_json(uploaded, "/api/matte", {"source": "photo"})
        assert status == 200
        payload = json.loads(raw)
        assert payload["ok"] is True
        assert payload["fg"] == 0.3125                 # 透传 MattingResult.fg
        assert payload["ms"] >= 0.0                    # ms 是服务端量的墙钟
        assert decode_data_uri(payload["image"]).size == (60, 120)

        status, _, raw = post_json(uploaded, "/api/matte-result-as-char", {})
        assert status == 200 and json.loads(raw)["ok"] is True

        status, _, raw = post_json(uploaded, "/api/render",
                                   {"params": FULL_PARAMS, "scale": 1.0})
        assert status == 200
        scene = json.loads(raw)["scene"]
        assert scene["character"]["name"] == "matte.png"
        assert scene["character"]["image"] == {"width": 60, "height": 120}

    def test_matte_as_char_without_matte_400(self, server, uploaded):
        status, _, raw = post_json(uploaded, "/api/matte-result-as-char", {})
        assert status == 400
        assert json.loads(raw)["ok"] is False

    def test_matte_bad_source_and_missing_material(self, server, uploaded):
        status, _, raw = post_json(uploaded, "/api/matte", {"source": "sky"})
        assert status == 400 and "source" in json.loads(raw)["error"]

        fresh = Client(_port(server))            # 没传立绘的干净服务
        status, _, raw = post_json(fresh, "/api/matte", {"source": "char"})
        assert status == 400 and json.loads(raw)["ok"] is False
        fresh.close()


# ---------------------------------------------------------------- 6. lidar

class TestLidar:
    @staticmethod
    def _arcd_bytes(h=1.55, pitch_deg=9.0, roll_deg=-5.0,
                    width=192, height=144) -> bytes:
        """lidar_sim 合成深度 → ARCD（.depth）落盘字节（内参 fp32 精确往返）。

        走 ARCD 而不是 .npy：ARCD 是实时流/模拟器的落盘格式（LIDAR-SPEC §2.3），
        头里带 fp32 内参与外参、往返无损；.npy 是裸数组，读回时内参只能按
        LIDAR-SPEC §2 的针孔满幅 FOV≈60° **估计**（``fy=fx`` 方形像素假设，
        与 ``pinhole_defaults`` 现口径一致，但估计非实测）。
        """
        meta, depth = synth_ground_frame(h=h, pitch_deg=pitch_deg,
                                        roll_deg=roll_deg, width=width, height=height)
        frame = LiDARFrame(depth_m=depth, fx=meta["fx"], fy=meta["fy"],
                           cx=meta["cx"], cy=meta["cy"])
        import tempfile

        with tempfile.TemporaryDirectory(prefix="arcam-w1-lidar-") as tmp:
            path = Path(tmp) / "scan.depth"
            save_depth(frame, path)
            return path.read_bytes()

    def test_inspect_env_close_to_ground_truth(self, server, uploaded):
        status, _, raw = post_inspect(uploaded, self._arcd_bytes())
        assert status == 200
        payload = json.loads(raw)
        assert payload["ok"] is True
        env = payload["env"]
        # LIDAR-SPEC T2 闭环容差：h<2cm / pitch<1° / roll<2°
        assert env["height_m"] == pytest.approx(1.55, abs=0.02)
        assert env["pitch_deg"] == pytest.approx(9.0, abs=1.0)
        assert env["roll_deg"] == pytest.approx(-5.0, abs=2.0)
        assert env["coverage"] > 0.5 and env["inliers"] > 1000 and env["rms"] < 0.05
        assert payload["frame"] == {"width": 192, "height": 144}

    def test_depth_driven_render_and_explicit_override(self, server, uploaded):
        status, _, raw = post_inspect(uploaded, self._arcd_bytes())
        assert status == 200 and json.loads(raw)["ok"] is True

        # params.depth_uploaded=true：scene 用深度测出的 h/pitch/roll
        status, _, raw = post_json(uploaded, "/api/render",
                                   {"params": {"depth_uploaded": True}})
        assert status == 200
        payload = json.loads(raw)
        assert payload["ok"] is True
        assert payload["env"] is not None
        assert payload["scene"]["camera"]["heightM"] == pytest.approx(1.55, abs=0.02)
        assert payload["scene"]["camera"]["pitchDeg"] == pytest.approx(9.0, abs=1.0)

        # 显式 h/x 优先（LIDAR-SPEC §0：显式 > 测出 > 标称）
        status, _, raw = post_json(uploaded, "/api/render",
                                   {"params": {"depth_uploaded": True, "h": 2.2,
                                               "x": 0.5, "pitch": 3.0}})
        assert status == 200
        scene = json.loads(raw)["scene"]
        assert scene["camera"]["heightM"] == 2.2
        assert scene["camera"]["pitchDeg"] == 3.0
        assert scene["subject"]["lateralM"] == pytest.approx(0.5, abs=1e-9)

    def test_depth_uploaded_without_frame_400(self, server, uploaded):
        status, _, raw = post_json(uploaded, "/api/render",
                                   {"params": {"depth_uploaded": True}})
        assert status == 400
        payload = json.loads(raw)
        assert payload["ok"] is False
        assert "depth_uploaded" in payload["error"]
        assert "lidar/inspect" in payload["hint"]

    def test_inspect_bad_depth_400(self, server, uploaded):
        status, _, raw = post_inspect(uploaded, b"\x00\x01\x02")
        assert status == 400
        assert json.loads(raw)["ok"] is False

    def test_inspect_missing_field_400(self, server, uploaded):
        status, _, raw = post_multipart(uploaded, "/api/lidar/inspect",
                                        {"photo": ("p.png", PHOTO)})
        assert status == 400 and json.loads(raw)["ok"] is False


# ---------------------------------------------------------------- 7. 并发

class TestConcurrency:
    def test_twenty_concurrent_renders_no_deadlock(self, server, uploaded):
        port = _port(server)

        def one(_i: int):
            # 每线程独立连接（http.client 连接非线程安全）
            client = Client(port)
            try:
                return post_json(client, "/api/render",
                                 {"params": FULL_PARAMS, "scale": 0.5})
            finally:
                client.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(one, i) for i in range(20)]
            results = [future.result(timeout=180) for future in futures]

        for status, _headers, raw in results:
            assert status == 200
            assert json.loads(raw)["ok"] is True
        images = {json.loads(raw)["image"] for _s, _h, raw in results}
        assert len(images) == 1        # 锁串行化输出仍然逐位一致（确定性）

    def test_depth_env_never_pairs_new_frame_with_old_env(self, monkeypatch):
        """并发换帧时 depth_env 必须返回**自洽的** (帧, env) 一对。

        复现过的静默失真：depth_env 在 store.lock 外拟合（几百 ms），此时另一线程
        （/api/upload 的 depth 字段或 /api/lidar/inspect，均不持 render_lock）
        set_depth 换帧 —— 旧实现在二次加锁后返回 (新帧, 旧 env)，新帧配旧环境量，
        遮挡/落位全错但不报错。修好后恒返回刚拟出的那一对，新帧下一遍再拟。
        """
        import arcam.webapp as webapp
        from arcam.lidar_sim import synth_ground_frame

        def frame_for(h: float) -> LiDARFrame:
            meta, depth = synth_ground_frame(h=h, pitch_deg=9.0, roll_deg=-5.0,
                                             width=128, height=96)
            return LiDARFrame(depth_m=depth, fx=meta["fx"], fy=meta["fy"],
                              cx=meta["cx"], cy=meta["cy"], source=f"h={h}")

        f1, f2 = frame_for(1.55), frame_for(2.20)
        store = webapp.Store()
        store.set_depth(f1, "f1")

        real_env_of = webapp._env_of
        entered_fit = threading.Event()

        def slow_env_of(frame):
            entered_fit.set()               # 已过第一次加锁、进入锁外拟合
            env = real_env_of(frame)
            time.sleep(0.4)               # 放大「拟合完 → 二次加锁」的竞态窗口
            return env

        monkeypatch.setattr(webapp, "_env_of", slow_env_of)

        result: dict = {}

        def worker() -> None:
            result["pair"] = store.depth_env()

        thread = threading.Thread(target=worker)
        thread.start()
        assert entered_fit.wait(timeout=30), "worker 应进入拟合"
        store.set_depth(f2, "f2")         # 并发换帧（模拟上传路径）
        thread.join(timeout=30)

        frame, env = result["pair"]
        assert frame is f1, "返回的帧必须是刚拟过的那一帧"
        assert env.height_m == pytest.approx(1.55, abs=0.02), \
            f"env 必须与所返回帧匹配，得到 h={env.height_m}"
        # 缓存不脏：下一次调用对新帧重拟
        frame2, env2 = store.depth_env()
        assert frame2 is f2 and env2.height_m == pytest.approx(2.20, abs=0.02)

    def test_concurrent_upload_and_render_mix(self, server):
        """上传与渲染并发跑：锁顺序固定（render_lock → store.lock）不死锁。

        先顺序传一份素材（之后并发 render 都有东西可渲），再让 10 对上传/渲染
        交错并发：全部必须 200（无 5xx、无被搅坏的 501）。渲染与上传的竞态
        窗口内，渲染要么看到旧素材要么看到新素材 —— 两者都合法，都能出图。
        """
        port = _port(server)
        warm = Client(port)
        status, _, raw = post_upload(warm, {"photo": ("photo.png", PHOTO),
                                            "char": ("my-char.png", CHAR)})
        assert status == 200 and json.loads(raw)["ok"] is True
        warm.close()

        def upload(_i: int):
            client = Client(port)
            try:
                return post_upload(client, {"photo": ("photo.png", PHOTO),
                                            "char": ("my-char.png", CHAR)})
            finally:
                client.close()

        def render(_i: int):
            client = Client(port)
            try:
                return post_json(client, "/api/render",
                                 {"params": FULL_PARAMS, "scale": 0.25})
            finally:
                client.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = []
            for i in range(10):
                futures.append(pool.submit(upload, i))
                futures.append(pool.submit(render, i))
            for future in futures:
                status, _headers, raw = future.result(timeout=180)
                assert status == 200, raw[:200]
                assert json.loads(raw)["ok"] is True
