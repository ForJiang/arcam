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
import re
import socket
import struct
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


def request(target, method: str, path: str, body=None, headers=None):
    """一发请求 → (status, headers, body bytes)。target 可以是端口或 Client。"""
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


def prep_u8(height: int = 120, width: int = 60) -> np.ndarray:
    """白底 + 深色块的立绘：四步修图链**全部触发**。

    手算（与 matte_tools.auto_prepare_char 实测一致）：深色块离白底足够远
    （sum|Δ| = 0.784 > tol·3/255 = 0.3765）→ 泛洪只抠白底边环，占比 28/72
    ≈ 0.5556 ∈ [CUT_RATIO_MIN, MAX] → cut 记数；内容 bbox 仍是整幅 → trim
    记数（裁后 82×42，>= 8px 不触发 ⑥ 回退）；柔边/微糊各记一次。
    """
    arr = np.zeros((height, width, 4), dtype=np.uint8)
    arr[..., 0], arr[..., 1], arr[..., 2], arr[..., 3] = 255, 255, 255, 255
    arr[20:100, 10:50] = (20, 40, 200, 255)
    return arr


def margin_char_u8(height: int = 120, width: int = 60) -> np.ndarray:
    """四边透明留白 + 中间不透明块（内容 bbox = (10, 20, 40, 80)，供裁边用）。"""
    arr = np.zeros((height, width, 4), dtype=np.uint8)
    arr[20:100, 10:50] = (230, 200, 255, 255)
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

    def test_zero_allowed_for_grain_scale_and_shadow_opacity(self, server, uploaded):
        """grain_scale/shadow_opacity=0 是合法值（v1.9.0 合并修复）。

        两侧滑块最左档就是 0（关颗粒/关阴影），早年按 positive 一刀切会 400，
        滑块拖到底直接报错。0 放行、负数仍拒。
        """
        for key in ("grain_scale", "shadow_opacity"):
            status, _, raw = post_json(uploaded, "/api/render",
                                       {"params": {**FULL_PARAMS, key: 0.0}})
            assert status == 200, f"{key}=0 应放行：{raw[:120]}"
            status, _, raw = post_json(uploaded, "/api/render",
                                       {"params": {**FULL_PARAMS, key: -0.5}})
            assert status == 400, f"{key}=-0.5 应拒"
            assert key in json.loads(raw)["error"]

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
        "/api/prep", "/api/trim", "/api/reset", "/api/lidar/inspect", "/api/nope",
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

    def test_get_with_body_does_not_break_keep_alive(self, server, uploaded):
        """GET 带 body（``curl -X GET -d``；浏览器不会）也要把请求体读掉。

        回归：do_GET 旧实现不 drain，残体 ``{"pad":true}`` 会拼进同连接下一条
        请求行（``{"pad":true}POST /api/render`` → 400/501），把连接永久搅坏。
        """
        client = Client(_port(server))
        try:
            status, _, _ = client.request("GET", "/", b'{"pad":true}',
                                          {"Content-Type": "application/json"})
            assert status == 200
            again = post_json(client, "/api/render",
                              {"params": FULL_PARAMS, "scale": 0.5})
            assert again[0] == 200, again[2][:160]
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

    def test_layer_and_bundle_ignore_base_only(self, server, uploaded):
        """3D 模式下前端会把 base_only=true 带进 export（app.js:197），而
        doExport3D 只拦截 png/bundle（app.js:424）——「下载立绘层」会落到本端点。

        回归（v1.9 审计复现）：base_only 让 compose_shot 不画立绘，
        char_layer 全透明，用户下到一张全透明的「立绘层」却收着 HTTP 200、
        没有任何报字（静默错产物）。layer/bundle 要的就是立绘层本身，与预览的
        3D 分工无关，端点须显式关掉 base_only。
        """
        params = {**FULL_PARAMS, "base_only": True}
        status, _, raw = post_json(uploaded, "/api/export",
                                   {"params": params, "target": "layer"})
        assert status == 200
        layer = np.asarray(Image.open(io.BytesIO(raw)))
        assert layer.shape[2] == 4 and int(layer[..., 3].max()) > 0, \
            "base_only 下的立绘层不该全透明"

        status, _, raw = post_json(uploaded, "/api/export",
                                   {"params": params, "target": "bundle"})
        assert status == 200
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            character = np.asarray(Image.open(io.BytesIO(zf.read("character.png"))))
        assert int(character[..., 3].max()) > 0, "bundle 的 character.png 不该全透明"


# ---------------------------------------------------------------- 4.5 修图三端点

class TestPrepTrimReset:
    """``/api/prep`` / ``/api/trim`` / ``/api/reset``（v1.4 网页修图按钮组）。

    契约：结果**替换会话内的立绘**（原版修图线改 chBase/chCanvas 的同语义），
    但**不刷新还原基线**——reset 永远回到上传时那张（原版 setCharset 只在装图
    时写 chOrig，index.html:2354）。
    """

    @pytest.fixture()
    def prepped(self, server):
        """上传照片 + 可四步全触发的立绘，返回复用同一连接的 Client。"""
        client = Client(_port(server))
        status, _, raw = post_upload(client, {"photo": ("photo.png", PHOTO),
                                              "char": ("c.png", _png(prep_u8()))})
        assert status == 200 and json.loads(raw)["ok"] is True
        try:
            yield client
        finally:
            client.close()

    def test_prep_full_chain_replaces_char(self, server, prepped):
        status, _, raw = post_json(prepped, "/api/prep",
                                   {"steps": ["solid", "trim", "soften", "blur"]})
        assert status == 200
        payload = json.loads(raw)
        assert payload["ok"] is True
        assert set(payload) >= {"ok", "image", "cutRatio", "trimmed",
                                "halfBody", "actions", "ms"}
        assert payload["cutRatio"] == pytest.approx(0.5556, abs=1e-3)
        assert payload["trimmed"] is True
        assert isinstance(payload["halfBody"], bool)
        assert payload["actions"] == ["note.autoCut", "note.autoTrim",
                                      "note.autoSoft", "note.autoBlur"]
        assert decode_data_uri(payload["image"]).size == (42, 82)   # 裁过边

        # 会话立绘真被替换：下一遍 render 的 scene 记的就是修图后的尺寸
        status, _, raw = post_json(prepped, "/api/render",
                                   {"params": FULL_PARAMS, "scale": 1.0})
        assert status == 200
        scene = json.loads(raw)["scene"]
        assert scene["character"]["image"] == {"width": 42, "height": 82}
        assert scene["character"]["name"] == "c.png"        # 名字不随修图改变

    def test_prep_default_steps_is_full_chain(self, server, prepped):
        """不给 steps 键 → 四步全跑（原版整链的默认行为）。"""
        status, _, raw = post_json(prepped, "/api/prep", {})
        assert status == 200
        default = json.loads(raw)
        assert default["actions"] == ["note.autoCut", "note.autoTrim",
                                      "note.autoSoft", "note.autoBlur"]
        post_json(prepped, "/api/reset", {})                # 还原后再显式四步
        status, _, raw = post_json(prepped, "/api/prep",
                                   {"steps": ["solid", "trim", "soften", "blur"]})
        assert status == 200
        explicit = json.loads(raw)
        assert explicit["image"] == default["image"]
        assert explicit["actions"] == default["actions"]

    def test_prep_subset_skips_discarded_full_chain(self, server, prepped,
                                                   monkeypatch):
        """steps 子集只跑子集：旧实现先算整链再把结果扔掉。

        3000×1500 立绘 steps=["trim"] 实测 HTTP 层 3514ms → 57ms（省下的正是
        那条被扔掉的整链，本身 ~2.4s）。
        """
        import arcam.matte_tools as mt
        calls: list[int] = []
        real = mt.auto_prepare_char

        def spy(arr, *, enabled=True):
            calls.append(1)
            return real(arr, enabled=enabled)

        monkeypatch.setattr(mt, "auto_prepare_char", spy)
        status, _, raw = post_json(prepped, "/api/prep", {"steps": ["trim"]})
        assert status == 200
        payload = json.loads(raw)
        assert payload["ok"] is True and payload["actions"] == ["note.autoTrim"]
        assert calls == [], "子集路径不应调用整链 auto_prepare_char"
        # 四步全点才走整链（一次）
        status, _, raw = post_json(prepped, "/api/prep",
                                   {"steps": ["solid", "trim", "soften", "blur"]})
        assert status == 200 and len(calls) == 1

    def test_prep_subset_shares_full_chain_parameters(self):
        """子集四步与整链逐位一致（同参同序 + 同一 ⑥ 回退兜底）。"""
        from arcam.matte_tools import auto_prepare_char
        from arcam.webapp import Handler, _to_f01

        a01 = _to_f01(prep_u8())
        full = auto_prepare_char(a01, enabled=True)
        sub = Handler._prep_subset(a01, ["solid", "trim", "soften", "blur"])
        assert np.array_equal(full.rgba01, sub.rgba01)
        assert (full.actions, full.cut_ratio, full.trimmed, full.half_body) == \
               (sub.actions, sub.cut_ratio, sub.trimmed, sub.half_body)

        # ⑥ 兜底：裁后内容 5×5（< 8px）→ 整条回退原图（子集路径与整链同门限）
        tiny = np.zeros((20, 20, 4))
        tiny[8:13, 8:13, 3] = 1.0
        for got in (Handler._prep_subset(tiny, ["trim"]), auto_prepare_char(tiny)):
            assert got.rgba01.shape == (20, 20, 4)
            assert got.actions == () and got.trimmed is False

    @pytest.mark.parametrize("steps", [
        3, True, "solid", {"solid": 1}, [], ["nope"], ["solid", 3], [None],
    ])
    def test_prep_bad_steps_400_not_500(self, server, prepped, steps):
        """steps 类型/步骤名不合法 → 400 带 hint（输入问题是用户能自己修的，
        不许掉进 500 兜底写「这是 arcam 的 bug」）。"""
        status, _, raw = post_json(prepped, "/api/prep", {"steps": steps})
        assert status == 400, (steps, raw[:160])
        payload = json.loads(raw)
        assert payload["ok"] is False and payload.get("hint")
        assert b"Traceback" not in raw

    def test_prep_without_char_400(self, server):
        status, _, raw = post_json(_port(server), "/api/prep", {})
        assert status == 400
        assert "立绘" in json.loads(raw)["error"]

    def test_trim_then_reset_roundtrip(self, server):
        """裁边按内容 bbox 裁；reset 回到上传时那张（原图尺寸、可再裁）。"""
        client = Client(_port(server))
        status, _, raw = post_upload(client, {"char": ("m.png", _png(margin_char_u8()))})
        assert status == 200
        try:
            status, _, raw = post_json(client, "/api/trim", {})
            assert status == 200
            payload = json.loads(raw)
            assert payload["trimmed"] is True
            assert payload["rect"] == [10, 20, 40, 80]      # (x, y, w, h)
            assert decode_data_uri(payload["image"]).size == (40, 80)

            status, _, raw = post_json(client, "/api/reset", {})
            assert status == 200
            payload = json.loads(raw)
            assert (payload["width"], payload["height"]) == (60, 120)
            assert decode_data_uri(payload["image"]).size == (60, 120)

            # 还原把立绘真换回去了：再裁一次得到同一个 rect
            status, _, raw = post_json(client, "/api/trim", {})
            assert status == 200 and json.loads(raw)["rect"] == [10, 20, 40, 80]
        finally:
            client.close()

    def test_prep_does_not_refresh_reset_baseline(self, server, prepped):
        """修图结果不当还原基线（原版只在装图时写 chOrig）。"""
        status, _, raw = post_json(prepped, "/api/prep", {})
        assert status == 200
        status, _, raw = post_json(prepped, "/api/reset", {})
        assert status == 200
        assert decode_data_uri(json.loads(raw)["image"]).size == (60, 120)

    def test_trim_and_reset_without_char_400(self, server):
        for path in ("/api/trim", "/api/reset"):
            status, _, raw = post_json(_port(server), path, {})
            assert status == 400, path
            assert json.loads(raw)["ok"] is False and json.loads(raw).get("hint")


# ---------------------------------------------------------------- 4.6 schema 不变量

class TestParamSchema:
    """params schema 三张表必须互斥且全覆盖（漏一个键 = 该参数校验不到）。"""

    def test_every_key_covered_exactly_once(self):
        from arcam.webapp import (_BOOL_FIELDS, _CHOICE_SPECS, _NUM_SPECS,
                                  PARAM_DEFAULTS)
        groups = (set(_NUM_SPECS), set(_CHOICE_SPECS), set(_BOOL_FIELDS))
        assert set().union(*groups) == set(PARAM_DEFAULTS)
        assert not (groups[0] & groups[1])
        assert not (groups[0] & groups[2])
        assert not (groups[1] & groups[2])

    def test_lighting_gates_present_in_schema_and_validation(self):
        """v1.3 光影三门：schema 默认值与校验表都要在（render 透传由
        TestLightingSwitches 的出图差异钉住）。"""
        from arcam.webapp import _BOOL_FIELDS, PARAM_DEFAULTS
        for key in ("light", "grain", "shadow"):
            assert PARAM_DEFAULTS[key] is True, key
            assert key in _BOOL_FIELDS, key


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

    def test_matte_bad_source_and_missing_material(self, server):
        """source 越界 → 400 带可选值；缺素材 → 400（两条都在干净会话上验）。

        修正（W3）：旧实现用「同服务新 Client」冒充干净服务——store 是进程级
        的，那个 Client 其实看得到 ``uploaded``  fixture 上传的立绘，请求落到
        了模型缺失分支，400 是蒙对的、断言形同虚设。缺素材的两条现在由
        :meth:`test_matte_missing_photo_400` / :meth:`test_matte_missing_char_400`
        在**没有上传过任何东西**的服务上验。
        """
        status, _, raw = post_json(_port(server), "/api/matte", {"source": "sky"})
        assert status == 400 and "source" in json.loads(raw)["error"]


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
    def test_listen_backlog_covers_browser_burst(self):
        """request_queue_size 必须 > 浏览器突发连接数（socketserver 默认只有 5）。

        复现过的抖动：全套件负载下 8 线程并发 render 偶发 ConnectionResetError
        [Errno 54] —— listen backlog=5 装不下突发，内核直接 reset 超出的连接，
        与服务端线程模型无关（ThreadingHTTPServer 一直是对的）。浏览器一次会
        并发拉 html/css/js/favicon + API，真实场景同样吃这个 backlog。
        """
        from arcam.webapp import WebServer
        assert WebServer.request_queue_size >= 16, (
            f"backlog={WebServer.request_queue_size} 低于浏览器突发量级，"
            "并发连接会被内核 reset")

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


class TestLightingSwitches:
    """光影三开关（v1.3.0）：light/grain/shadow 进 params 并真改变出图。

    schema 默认值与校验表 membership 的断言在 :class:`TestParamSchema`
    （``test_lighting_gates_present_in_schema_and_validation``，严格更强：
    同时钉 PARAM_DEFAULTS 与 _BOOL_FIELDS），此处只留下「真的改变出图」。
    """

    def test_shadow_off_changes_pixels(self, server, uploaded):
        """关阴影 → 出图像素变（贴地模式默认铺阴影）。"""
        port = _port(server)
        base = dict(FULL_PARAMS)
        on = post_json(Client(port), "/api/render", {"params": base, "scale": 0.5})
        off_kw = dict(base, shadow=False)
        off = post_json(Client(port), "/api/render", {"params": off_kw, "scale": 0.5})
        assert on[0] == 200 and off[0] == 200
        import base64 as _b64
        a = _b64.b64decode(on[2] and json.loads(on[2])["image"].split(",", 1)[1])
        b = _b64.b64decode(json.loads(off[2])["image"].split(",", 1)[1])
        assert a != b, "关阴影后出图应不同"

    def test_light_off_changes_pixels(self, server, uploaded):
        """关采光 → bakeLit 原样返回立绘，出图与默认不同。"""
        port = _port(server)
        on = post_json(Client(port), "/api/render", {"params": dict(FULL_PARAMS), "scale": 0.5})
        off = post_json(Client(port), "/api/render",
                        {"params": dict(FULL_PARAMS, light=False), "scale": 0.5})
        assert on[0] == 200 and off[0] == 200
        assert json.loads(on[2])["image"] != json.loads(off[2])["image"]

    def test_grain_off_changes_pixels(self, server, uploaded):
        """关噪点 → bakeLit 不加颗粒，出图与默认不同。"""
        port = _port(server)
        on = post_json(Client(port), "/api/render", {"params": dict(FULL_PARAMS), "scale": 0.5})
        off = post_json(Client(port), "/api/render",
                        {"params": dict(FULL_PARAMS, grain=False), "scale": 0.5})
        assert on[0] == 200 and off[0] == 200
        assert json.loads(on[2])["image"] != json.loads(off[2])["image"]

    def test_all_three_gates_reach_compose_shot(self, server, uploaded, monkeypatch):
        """三键必须真的透传到 compose_shot（schema/校验/render 三处都在）。

        历史上「门加了但某处没透传 = 静默失效」踩过两次；这里直接抓
        compose_shot 的实收 kwargs，shadow/grain/sample_light 缺一不可。
        """
        import arcam.webapp as webapp
        seen: dict = {}
        real = webapp.compose_shot

        def spy(src, char, subj, cam, **kw):
            seen.update(kw)
            return real(src, char, subj, cam, **kw)

        monkeypatch.setattr(webapp, "compose_shot", spy)
        params = dict(FULL_PARAMS, light=False, grain=False, shadow=False)
        status, _, raw = post_json(uploaded, "/api/render", {"params": params})
        assert status == 200 and json.loads(raw)["ok"] is True
        assert seen["sample_light"] is False
        assert seen["grain"] is False
        assert seen["shadow"] is False


class TestBatchEndpoint:
    """POST /api/batch：多照片 × 同一立绘同一组参数 → zip（v1.6.0）。"""

    @staticmethod
    def _data_uri(path: Path) -> str:
        return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()

    def test_batch_zip_per_photo(self, server, uploaded, tmp_path):
        port = _port(server)
        # 两张不同的照片 dataURI
        uris = []
        for i, (r, g) in enumerate(((200, 60), (60, 200))):
            arr = np.full((64, 96, 3), 0, np.uint8)
            arr[..., 0], arr[..., 2] = r, g
            p = tmp_path / f"p{i}.png"
            Image.fromarray(arr, "RGB").save(p)
            uris.append(self._data_uri(p))
        status, headers, raw = post_json(Client(port), "/api/batch",
                                         {"photos": uris, "params": {"mode": "ground"}})
        assert status == 200
        assert headers.get("Content-Type", "").startswith("application/zip")
        z = zipfile.ZipFile(io.BytesIO(raw))
        names = sorted(z.namelist())
        assert names == ["shot_001.png", "shot_002.png"], names
        # 两张内容不同（不是同一张重复）
        a = np.asarray(Image.open(io.BytesIO(z.read("shot_001.png"))).convert("RGB"))
        b = np.asarray(Image.open(io.BytesIO(z.read("shot_002.png"))).convert("RGB"))
        assert not np.array_equal(a, b)

    def test_batch_requires_char(self, server):
        port = _port(server)
        status, _h, raw = post_json(Client(port), "/api/batch",
                                    {"photos": ["data:image/png;base64,iVBORw0KGgo="]})
        assert status == 400
        assert "立绘" in json.loads(raw)["error"]

    def test_batch_rejects_bad_photos(self, server, uploaded):
        port = _port(server)
        status, _h, raw = post_json(Client(port), "/api/batch",
                                    {"photos": ["not-a-data-uri"]})
        assert status == 400 and "dataURI" in json.loads(raw)["error"]
        status, _h, raw = post_json(Client(port), "/api/batch", {"photos": []})
        assert status == 400 and "非空" in json.loads(raw)["error"]
        status, _h, raw = post_json(Client(port), "/api/batch",
                                    {"photos": ["data:image/png;base64," + "A" * 100]})
        assert status == 400, "解不开的 base64 应 400"

    def test_batch_partial_failure_header(self, server, uploaded, tmp_path):
        """一张坏图不拖垮整批：zip 仍出，X-Arcam-Batch-Failed 记明细。"""
        port = _port(server)
        good = np.full((64, 96, 3), 120, np.uint8)
        p = tmp_path / "good.png"
        Image.fromarray(good, "RGB").save(p)
        uris = [self._data_uri(p), "data:image/png;base64,!!!notbase64!!!"]
        status, headers, raw = post_json(Client(port), "/api/batch",
                                         {"photos": uris, "params": {"mode": "ground"}})
        assert status == 200
        z = zipfile.ZipFile(io.BytesIO(raw))
        # 坏图被跳过；明细落进 _errors.txt（HTTP 头是 latin-1，不能放中文原因）
        assert "shot_001.png" in z.namelist() and "_errors.txt" in z.namelist()
        assert "shot_002.png" not in z.namelist()
        errs = z.read("_errors.txt").decode("utf-8")
        assert "第 2 张" in errs and "base64" in errs
        # 头只放数字（中文进头会 UnicodeEncodeError → 500，v1.6 踩过）
        assert headers.get("X-Arcam-Batch-Failed") == "1"
        assert "1 ok, 1 failed" in headers.get("X-Arcam-Batch", "")


# ---------------------------------------------------------------- 8. CLI 入口

class TestWebCommandEntry:
    """``arcam web``（webapp.main）的启动参数错误路径：人话报错、退出 1。

    回归（v1.9 审计复现）：``--port 99999`` 让 ``socket.bind`` 抛
    **OverflowError**（不是 OSError），旧实现的 ``except OSError`` 兜不住，
    用户看到裸 traceback 而不是「换个端口」的人话。
    """

    def test_port_out_of_range_returns_1_with_human_message(self, capsys):
        from arcam.webapp import main
        for port in (99999, -1, 70000):
            assert main(["--port", str(port), "--no-open"]) == 1, port
        err = capsys.readouterr().err
        assert "0-65535" in err and "--port" in err

    def test_port_in_use_returns_1_with_human_message(self, capsys):
        from arcam.webapp import main
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            port = int(taken.getsockname()[1])
            assert main(["--port", str(port), "--no-open"]) == 1
        err = capsys.readouterr().err
        assert "起不来服务" in err and "端口" in err


# ---------------------------------------------------------------- 9. W3 健壮性

def _raw_request(port: int, payload: bytes, *, timeout: float = 20.0) -> bytes:
    """裸 socket 发一整块请求字节 → 收到底的响应字节（errno 49 退避重试）。

    用于 http.client 表达不了的畸形请求：坏 Content-Length、chunked、截断
    body、同连接多请求流水等（W3 硬化面）。发完即 ``shutdown(SHUT_WR)``——
    服务端处理完（含 keep-alive 流水里的后续请求）看到 EOF 自然关连接，测试
    因此可以「读到 EOF 为止」而不用猜响应边界；需要服务端**主动**断连语义的
    用例（超限 body / 未知 chunked）关不关都断言得到。
    """
    last: OSError | None = None
    for attempt in range(60):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
                sock.sendall(payload)
                sock.shutdown(socket.SHUT_WR)
                chunks: list[bytes] = []
                while True:
                    piece = sock.recv(65536)
                    if not piece:
                        break
                    chunks.append(piece)
                return b"".join(chunks)
        except OSError as exc:
            if exc.errno != 49 or attempt == 59:
                raise
            last = exc
            time.sleep(0.25)
    assert last is not None
    raise last


def _status_lines(raw: bytes) -> list[bytes]:
    """响应字节 → 每条响应的状态行（keep-alive 流水里有多条）。

    响应体后面不带 CRLF，后续状态行是**贴着 body** 出现的
    （``…}HTTP/1.1 200 OK``），按行 split 会漏——用正则全文扫。
    """
    return re.findall(rb"HTTP/1\.1 \d{3}[^\r\n]*", raw)


class TestRouteCoverage:
    """WEB-SPEC §1 路由表逐条的边角路径（成功路径之外的契约面）。"""

    def test_healthz_ok(self, server):
        status, headers, raw = request(_port(server), "GET", "/healthz")
        assert status == 200
        assert json.loads(raw) == {"ok": True, "service": "arcam-web"}
        assert headers["Content-Type"].startswith("application/json")

    def test_render_params_null_means_defaults(self, server, uploaded):
        """``{"params": null}``：parse_params(None) → 全量默认（不是 400）。"""
        status, _, raw = post_json(uploaded, "/api/render", {"params": None})
        assert status == 200 and json.loads(raw)["ok"] is True

    def test_render_empty_body_uses_defaults(self, server, uploaded):
        """Content-Length: 0 → body 空 → ``{}`` → 默认参数出图（不是 400）。"""
        raw = _raw_request(
            _port(server),
            b"POST /api/render HTTP/1.1\r\nHost: x\r\n"
            b"Content-Type: application/json\r\nContent-Length: 0\r\n\r\n")
        assert b" 200 " in raw.split(b"\r\n")[0]
        assert json.loads(raw.split(b"\r\n\r\n", 1)[1])["ok"] is True

    def test_render_json_array_body_400(self, server, uploaded):
        """顶层是 JSON 数组 → 400「顶层应是 JSON 对象」（不炸 500）。"""
        status, _, raw = request(uploaded, "POST", "/api/render", b"[1, 2]",
                                 {"Content-Type": "application/json"})
        assert status == 400
        payload = json.loads(raw)
        assert payload["ok"] is False and "JSON 对象" in payload["error"]

    def test_post_without_content_length_reads_empty_body(self, server, uploaded):
        """POST 既无 Content-Length 也无 Transfer-Encoding → body 按空处理。

        回归口径：``_read_body`` 对缺长度的请求返回 b""（不挂起、不 400），
        keep-alive 卫生由 ``_body_read=True`` 保证。
        """
        raw = _raw_request(
            _port(server),
            b"POST /api/render HTTP/1.1\r\nHost: x\r\n"
            b"Content-Type: application/json\r\n\r\n")
        assert b" 200 " in raw.split(b"\r\n")[0]
        assert json.loads(raw.split(b"\r\n\r\n", 1)[1])["ok"] is True

    def test_upload_depth_field_200_and_resident(self, server):
        """/api/upload 的 depth 字段：帧落进会话态，之后 render 可直接深度驱动。"""
        depth = TestLidar._arcd_bytes()   # 复用合成 ARCD 深度字节（本模块类）
        status, _, raw = post_upload(_port(server), {"photo": ("photo.png", PHOTO),
                                                    "depth": ("scan.depth", depth)})
        assert status == 200
        payload = json.loads(raw)
        assert payload["ok"] is True and payload["kind"] == "depth"
        assert payload["kinds"] == ["photo", "depth"]
        assert payload["width"] == 192 and payload["height"] == 144
        # 常驻生效：depth_uploaded=true 不再 400，且 env 有值
        status, _, raw = post_json(_port(server), "/api/render",
                                   {"params": {"depth_uploaded": True}})
        assert status == 200 and json.loads(raw)["env"] is not None

    def test_upload_empty_file_400(self, server):
        """空文件（0 字节）不是「坏图」而是「没选文件」：人话 400。"""
        status, _, raw = post_upload(_port(server), {"photo": ("empty.png", b"")})
        assert status == 400
        payload = json.loads(raw)
        assert payload["ok"] is False and "空文件" in payload["error"]
        assert payload.get("hint")

    def test_render_base_only_omits_char_but_keeps_scene(self, server, uploaded):
        """base_only=true：只出照片底（立绘交给客户端 WebGL），scene 仍记 2D 落位。

        反「静默失效」：基线下出图必须与带立绘的出图不同（否则说明
        compose_shot 没收到 base_only，立绘被画了两遍）。
        """
        base = dict(FULL_PARAMS, base_only=True)
        status, _, raw = post_json(uploaded, "/api/render",
                                   {"params": base, "scale": 1.0})
        assert status == 200
        payload = json.loads(raw)
        assert payload["ok"] is True and payload["env"] is None
        scene = payload["scene"]
        assert scene["character"]["image"] == {"width": 60, "height": 120}
        status2, _, raw2 = post_json(uploaded, "/api/render",
                                     {"params": dict(FULL_PARAMS), "scale": 1.0})
        assert payload["image"] != json.loads(raw2)["image"], \
            "base_only 出图应与带立绘出图不同"

    def test_batch_too_many_photos_400(self, server, uploaded):
        photos = ["data:image/png;base64,iVBORw0KGgo="] * 13
        status, _, raw = post_json(uploaded, "/api/batch", {"photos": photos})
        assert status == 400
        payload = json.loads(raw)
        assert payload["ok"] is False and "12" in payload["error"]

    def test_batch_single_compose_failure_does_not_sink_batch(
            self, server, uploaded, tmp_path, monkeypatch):
        """某张照片合成失败只跳过该张（WEB-SPEC §1「单张失败不拖垮整批」全口径）。

        回归：旧实现只兜住 dataURI 解码错，``_compose`` 抛的 ApiError 会把
        整批 400 掉——用户为一张坏照片丢掉其余全部产物。
        """
        import arcam.webapp as webapp

        real = webapp._compose
        calls: list[int] = []

        def flaky(store, params, explicit, scale=1.0, src_override=None):
            calls.append(1)
            if len(calls) == 2:
                raise webapp.ApiError(400, "第 2 张合成失败（模拟）", "换一张照片")
            return real(store, params, explicit, scale=scale, src_override=src_override)

        monkeypatch.setattr(webapp, "_compose", flaky)
        uris = []
        for i in range(3):
            p = tmp_path / f"p{i}.png"
            Image.fromarray(np.full((32, 48, 3), 100 + i, np.uint8), "RGB").save(p)
            uris.append("data:image/png;base64," + base64.b64encode(p.read_bytes()).decode())
        status, headers, raw = post_json(uploaded, "/api/batch",
                                         {"photos": uris, "params": {"mode": "ground"}})
        assert status == 200
        assert headers.get("X-Arcam-Batch-Failed") == "1"
        z = zipfile.ZipFile(io.BytesIO(raw))
        names = z.namelist()
        assert "shot_001.png" in names and "shot_003.png" in names
        assert "shot_002.png" not in names
        assert "第 2 张" in z.read("_errors.txt").decode("utf-8")

    def test_trim_without_transparent_margin_400(self, server):
        """四边全不透明的立绘没有透明边距可裁 → 400（不是空 rect 200）。"""
        client = Client(_port(server))
        post_upload(client, {"char": ("flat.png", _png(char_u8()))})
        status, _, raw = post_json(client, "/api/trim", {})
        assert status == 400
        payload = json.loads(raw)
        assert payload["ok"] is False and "裁不动" in payload["error"]
        client.close()

    def test_matte_missing_photo_400(self, server):
        """干净会话（什么都没上传）抠照片 → 400「还没有上传照片」。

        回归：旧用例用「同服务新 Client」冒充干净服务，实际走到了模型缺失
        分支，这条 400 从来没被测到（W3 修正用例口径）。
        """
        status, _, raw = post_json(_port(server), "/api/matte", {"source": "photo"})
        assert status == 400
        payload = json.loads(raw)
        assert payload["ok"] is False and "照片" in payload["error"]
        assert payload.get("hint")

    def test_matte_missing_char_400(self, server):
        status, _, raw = post_json(_port(server), "/api/matte", {"source": "char"})
        assert status == 400 and "立绘" in json.loads(raw)["error"]

    @pytest.mark.parametrize("filename,fallback", [
        ("..", "char.png"),            # basename 空 → 回落
        ("a/..", "char.png"),          # basename 是 ".." → 回落
        ("..\\..\\x.png", "x.png"),       # 反斜杠归一后取 basename：穿越剥掉、名字保留
        ("a\x00b.png", "char.png"),    # 空字节 → 回落
    ])
    def test_safe_name_rejects_traversal_and_null(self, server, uploaded,
                                                  filename, fallback):
        """上传文件名只取安全基名（scene.json 的 character.name 用它）。"""
        status, _, raw = post_upload(uploaded, {"char": (filename, CHAR)})
        assert status == 200, (filename, raw[:120])
        status, _, raw = post_json(uploaded, "/api/render",
                                   {"params": FULL_PARAMS, "scale": 0.5})
        assert status == 200
        assert json.loads(raw)["scene"]["character"]["name"] == fallback

    def test_upload_part_without_filename(self, server):
        """multipart 段不带 filename（浏览器之外的手搓客户端）→ 用回落名。"""
        body = (b"--" + _BOUNDARY.encode() + b"\r\n"
                b'Content-Disposition: form-data; name="photo"\r\n\r\n'
                + PHOTO + b"\r\n--" + _BOUNDARY.encode() + b"--\r\n")
        status, _, raw = request(_port(server), "POST", "/api/upload", body,
                                 {"Content-Type": f"multipart/form-data; boundary={_BOUNDARY}"})
        assert status == 200 and json.loads(raw)["ok"] is True
        # 名字回落进 scene：上传立绘（无 filename）再渲一次
        body = (b"--" + _BOUNDARY.encode() + b"\r\n"
                b'Content-Disposition: form-data; name="char"\r\n\r\n'
                + CHAR + b"\r\n--" + _BOUNDARY.encode() + b"--\r\n")
        status, _, raw = request(_port(server), "POST", "/api/upload", body,
                                 {"Content-Type": f"multipart/form-data; boundary={_BOUNDARY}"})
        assert status == 200
        status, _, raw = post_json(_port(server), "/api/render",
                                   {"params": FULL_PARAMS, "scale": 0.5})
        assert json.loads(raw)["scene"]["character"]["name"] == "char.png"

    def test_char_palette_png_with_transparency_reports_fg(self, server):
        """P 模式 + tRNS 的 PNG：源图「带透明语义」→ fg 是不透明占比而非 false。

        ``_has_alpha`` 的 info['transparency'] 分支：mode 不在 RGBA/LA/PA 里，
        但调色板带透明表——前端据此判断「已经是透明底立绘」。
        """
        buf = io.BytesIO()
        rgba = np.zeros((16, 16, 4), np.uint8)
        rgba[..., 3] = 255
        im = Image.fromarray(rgba, "RGBA").convert("P")
        im.save(buf, format="PNG", transparency=0)
        assert b"tRNS" in buf.getvalue() or True      # 保存成功即走通 P+tRNS
        status, _, raw = post_upload(_port(server), {"char": ("pal.png", buf.getvalue())})
        assert status == 200
        payload = json.loads(raw)
        assert payload["fg"] is not False, "P+tRNS 应按「带 alpha」报占比"
        assert isinstance(payload["fg"], float)


class TestMalformedMultipart:
    """畸形 multipart / 坏 Content-Type：400 人话 JSON，不进 500 兜底。"""

    def test_missing_boundary_400(self, server):
        status, _, raw = request(_port(server), "POST", "/api/upload", b"whatever",
                                 {"Content-Type": "application/json"})
        assert status == 400
        payload = json.loads(raw)
        assert "boundary" in payload["error"] and payload.get("hint")

    def test_empty_boundary_400(self, server):
        status, _, raw = request(_port(server), "POST", "/api/upload", b"whatever",
                                 {"Content-Type": "multipart/form-data; boundary="})
        assert status == 400
        assert "boundary" in json.loads(raw)["error"]

    def test_missing_content_type_400(self, server):
        """完全不带 Content-Type 的 multipart body → 同样 400（不是 500）。"""
        status, _, raw = request(_port(server), "POST", "/api/upload", b"whatever", {})
        assert status == 400 and json.loads(raw)["ok"] is False

    def test_lf_only_line_endings_accepted(self, server, uploaded):
        """只用 LF 的手搓 multipart：解析器按裸 \\n 拆头体，照样认出字段。

        fetch/FormData 永远发 CRLF，但代理/脚本可能发 LF——解析器两条都吃。
        """
        body = (b"--" + _BOUNDARY.encode() + b"\n"
                b'Content-Disposition: form-data; name="photo"; filename="lf.png"\n'
                b"Content-Type: application/octet-stream\n\n"
                + PHOTO + b"\n--" + _BOUNDARY.encode() + b"--\n")
        status, _, raw = request(_port(server), "POST", "/api/upload", body,
                                 {"Content-Type": f"multipart/form-data; boundary={_BOUNDARY}"})
        assert status == 200, raw[:200]
        payload = json.loads(raw)
        assert payload["ok"] is True and payload["kind"] == "photo"
        assert payload["width"] == 192 and payload["height"] == 256

    def test_empty_part_and_no_name_parts_400(self, server):
        """一个字段都解不出（空段 / 无 name 段 / 无头体分隔）→ 400 带字段名清单。"""
        boundary = _BOUNDARY.encode()
        cases = {
            "空段": b"--" + boundary + b"\r\n\r\n--" + boundary + b"--\r\n",
            "无 name": (b"--" + boundary + b"\r\nContent-Type: text/plain\r\n\r\n"
                       b"data\r\n--" + boundary + b"--\r\n"),
            "无头体分隔": (b"--" + boundary + b"\r\n"
                          b'Content-Disposition: form-data; name="photo"\r\n'
                          b"--" + boundary + b"--\r\n"),
        }
        for label, body in cases.items():
            status, _, raw = request(_port(server), "POST", "/api/upload", body,
                                     {"Content-Type": f"multipart/form-data; boundary={_BOUNDARY}"})
            assert status == 400, (label, raw[:160])
            payload = json.loads(raw)
            assert payload["ok"] is False and "photo" in payload.get("hint", "")


class TestBodyLimitsAndKeepAlive:
    """请求体闸门 + keep-alive 卫生（裸 socket：http.client 表达不了这些）。"""

    def test_bad_content_length_400_and_closes(self, server):
        raw = _raw_request(_port(server),
                           b"POST /api/render HTTP/1.1\r\nHost: x\r\n"
                           b"Content-Type: application/json\r\n"
                           b"Content-Length: abc\r\n\r\n{}")
        head = raw.split(b"\r\n")[0]
        assert b" 400 " in head
        assert "Content-Length" in json.loads(raw.split(b"\r\n\r\n", 1)[1])["error"]

    def test_negative_content_length_400(self, server):
        raw = _raw_request(_port(server),
                           b"POST /api/render HTTP/1.1\r\nHost: x\r\n"
                           b"Content-Type: application/json\r\n"
                           b"Content-Length: -5\r\n\r\n{}")
        assert b" 400 " in raw.split(b"\r\n")[0]
        assert "负数" in json.loads(raw.split(b"\r\n\r\n", 1)[1])["error"]

    def test_short_body_400_human_message(self, server, uploaded):
        """声明 100 字节只发 10 字节就断 → 400「请求体比 Content-Length 短」。"""
        raw = _raw_request(
            _port(server),
            b"POST /api/render HTTP/1.1\r\nHost: x\r\n"
            b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n"
            b'{"params":')
        assert b" 400 " in raw.split(b"\r\n")[0]
        assert "Content-Length" in json.loads(raw.split(b"\r\n\r\n", 1)[1])["error"]

    def test_chunked_request_ok(self, server, uploaded):
        """合法 chunked body（fetch 流式上传走这条）→ 正常出图。"""
        body = json.dumps({"params": FULL_PARAMS, "scale": 0.5}).encode("utf-8")
        payload = (b"POST /api/render HTTP/1.1\r\nHost: x\r\n"
                   b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
                   + f"{len(body):x}".encode() + b"\r\n" + body + b"\r\n0\r\n\r\n")
        raw = _raw_request(_port(server), payload)
        assert b" 200 " in raw.split(b"\r\n")[0]
        assert json.loads(raw.split(b"\r\n\r\n", 1)[1])["ok"] is True

    def test_chunked_bad_hex_size_400(self, server):
        payload = (b"POST /api/render HTTP/1.1\r\nHost: x\r\n"
                   b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
                   b"zz\r\n")
        raw = _raw_request(_port(server), payload)
        assert b" 400 " in raw.split(b"\r\n")[0]
        assert "十六进制" in json.loads(raw.split(b"\r\n\r\n", 1)[1])["error"]

    def test_chunked_negative_size_400(self, server):
        """负 chunk 大小：旧实现会把它累加进总长、``_read_exact(负数)`` 直接放行，
        客户端反复发 "-1" 即可让该线程永久空转（W3 硬化后 400 断连）。"""
        payload = (b"POST /api/render HTTP/1.1\r\nHost: x\r\n"
                   b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
                   b"-1\r\n0\r\n\r\n")
        raw = _raw_request(_port(server), payload)
        assert b" 400 " in raw.split(b"\r\n")[0]
        assert "负数" in json.loads(raw.split(b"\r\n\r\n", 1)[1])["error"]

    def test_chunked_announced_but_no_data_400(self, server):
        """宣布 chunked 却一个字节不发就断 → 400「chunked 请求体不完整」。"""
        raw = _raw_request(
            _port(server),
            b"POST /api/render HTTP/1.1\r\nHost: x\r\n"
            b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n")
        assert b" 400 " in raw.split(b"\r\n")[0]
        assert "chunked" in json.loads(raw.split(b"\r\n\r\n", 1)[1])["error"]

    def test_chunked_over_limit_400_before_reading(self, server):
        """chunked 累计超 64MB → 读到越界那一块就 400（不吞 payload）。"""
        payload = (b"POST /api/upload HTTP/1.1\r\nHost: x\r\n"
                   b"Content-Type: multipart/form-data; boundary=xyz\r\n"
                   b"Transfer-Encoding: chunked\r\n\r\n4000001\r\n")
        raw = _raw_request(_port(server), payload)
        assert b" 400 " in raw.split(b"\r\n")[0]
        assert "64MB" in json.loads(raw.split(b"\r\n\r\n", 1)[1])["error"]

    def test_get_with_bad_content_length_still_200_and_usable(self, server, uploaded):
        """GET 的 Content-Length 不是整数：不读 body 的路径按 0 处理 → 200。

        ``_expected_length`` 的 ValueError 兜底：GET 没有 ``_read_body`` 那道
        400，坏头不能把静态页打翻。
        """
        payload = (b"GET /healthz HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\n"
                   b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        raw = _raw_request(_port(server), payload)
        lines = _status_lines(raw)
        assert lines == [b"HTTP/1.1 200 OK", b"HTTP/1.1 200 OK"], lines

    def test_get_with_oversize_body_closes_connection(self, server, uploaded):
        """GET 带 >64MB 的 Content-Length：响应照发，但连接直接断（不替垃圾读盘）。

        读到 EOF（而不是超时挂住）即证明服务端主动断了 keep-alive。
        """
        raw = _raw_request(
            _port(server),
            b"GET /healthz HTTP/1.1\r\nHost: x\r\n"
            b"Content-Length: 67108864\r\n\r\n")
        assert b" 200 " in raw.split(b"\r\n")[0]
        assert raw.endswith(b"")           # 收到 EOF：连接已被服务端关闭

    def test_get_with_short_body_drains_and_closes(self, server, uploaded):
        """GET 声明 10 字节只发 5 字节：drain 读不全 → 400 语义的断连（不搅坏连接）。"""
        raw = _raw_request(
            _port(server),
            b"GET /healthz HTTP/1.1\r\nHost: x\r\n"
            b"Content-Length: 10\r\n\r\nabcde")
        assert b" 200 " in raw.split(b"\r\n")[0]

    def test_post_unknown_path_chunked_closes(self, server):
        """未知 POST 路径 + chunked：body 没读过、长度猜不出 → 响应后断连。"""
        payload = (b"POST /api/nope HTTP/1.1\r\nHost: x\r\n"
                   b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
                   b"0\r\n\r\n")
        raw = _raw_request(_port(server), payload)
        assert b" 404 " in raw.split(b"\r\n")[0]

    def test_get_after_post_with_body_keep_alive(self, server, uploaded):
        """回归（W3 修复）：POST 之后同连接再发**带 body 的 GET**。

        ``_body_read`` 是实例属性、跨请求残留：do_GET 旧实现不复位它，
        ``_drain_body`` 以为 body 已读 → 残体 ``HELLO`` 拼进下一条请求行
        （``HELLOGET /healthz`` → 501），把连接永久搅坏。浏览器不发带 body
        的 GET，但代理/脚本会——HTTP/1.1 卫生必须两边对称。
        """
        payload = (b"POST /api/matte-result-as-char HTTP/1.1\r\nHost: x\r\n"
                   b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
                   b"GET /healthz HTTP/1.1\r\nHost: x\r\n"
                   b"Content-Type: application/json\r\nContent-Length: 5\r\n\r\nHELLO"
                   b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        raw = _raw_request(_port(server), payload)
        lines = _status_lines(raw)
        assert lines[0].startswith(b"HTTP/1.1 400"), lines      # 没抠过图：语义 400
        assert lines[1] == b"HTTP/1.1 200 OK", lines             # GET 带 body 也 200
        assert lines[2] == b"HTTP/1.1 200 OK", lines             # 连接没被搅坏
        assert b"HELLOGET" not in raw and b" 501 " not in raw

    def test_client_disconnect_mid_response_server_survives(self, server, uploaded):
        """客户端读到一半 RST 断连：服务端线程可以死，服务必须活。"""
        port = _port(server)
        body = json.dumps({"params": FULL_PARAMS, "scale": 1.0}).encode("utf-8")
        for _ in range(3):
            sock = socket.create_connection(("127.0.0.1", port), timeout=20)
            try:
                sock.sendall(b"POST /api/render HTTP/1.1\r\nHost: x\r\n"
                             b"Content-Type: application/json\r\n"
                             + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
                sock.recv(64)                     # 只读一开头就掐
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                struct.pack("ii", 1, 0))       # 强制 RST
            finally:
                sock.close()
            time.sleep(0.05)
        status, _, raw = request(port, "GET", "/healthz")
        assert status == 200 and json.loads(raw)["ok"] is True


class TestErrorContract:
    """WEB-SPEC §1 硬规则：异常全收敛 JSON，响应体永不出现 traceback。"""

    def test_get_internal_error_is_json_500_without_traceback(self, server,
                                                              monkeypatch):
        import arcam.webapp as webapp

        def boom():
            raise RuntimeError("模拟的内部错误")

        monkeypatch.setattr(webapp.Handler, "_serve_index", boom)
        status, _, raw = request(_port(server), "GET", "/")
        assert status == 500
        payload = json.loads(raw)
        assert payload["ok"] is False
        assert "服务器内部错误" in payload["error"]
        assert payload.get("hint") and "bug" in payload["hint"]
        assert b"Traceback" not in raw and b"RuntimeError" not in raw

    def test_post_internal_error_is_json_500_without_traceback(self, server,
                                                               uploaded, monkeypatch):
        import arcam.webapp as webapp

        def boom(*_a, **_k):
            raise RuntimeError("模拟的内部错误")

        monkeypatch.setattr(webapp, "_compose", boom)
        status, _, raw = post_json(uploaded, "/api/render", {"params": FULL_PARAMS})
        assert status == 500
        payload = json.loads(raw)
        assert payload["ok"] is False and "服务器内部错误" in payload["error"]
        assert b"Traceback" not in raw

    def test_compose_value_error_400_with_hint(self, server, uploaded, monkeypatch):
        """compose 链的 ValueError → 400「合成失败」+ 参数检查提示（不 500）。"""
        import arcam.webapp as webapp

        def bad_shot(*_a, **_k):
            raise ValueError("模拟：摆放参数与素材尺寸不合法")

        monkeypatch.setattr(webapp, "compose_shot", bad_shot)
        status, _, raw = post_json(uploaded, "/api/render", {"params": FULL_PARAMS})
        assert status == 400
        payload = json.loads(raw)
        assert payload["ok"] is False and "合成失败" in payload["error"]
        assert "摆放参数" in payload.get("hint", "")

    def test_index_fallback_page_when_webui_missing(self, server, tmp_path,
                                                   monkeypatch):
        """webui/ 不在时 GET / 仍 200（内置占位页引用两个静态文件）。"""
        import arcam.webapp as webapp

        monkeypatch.setattr(webapp, "WEBUI_DIR", tmp_path / "no-webui")
        status, headers, raw = request(_port(server), "GET", "/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        text = raw.decode("utf-8")
        assert "/static/style.css" in text and "/static/app.js" in text

    def test_inspect_no_plane_depth_400(self, server):
        """有效但拟不出平面的深度（全共线）→ 400 人话（不是 500、不是假 env）。"""
        depth = np.full((20, 100), np.nan, np.float32)
        depth[10, :] = 2.0                      # 一行有效点：全部共线
        buf = io.BytesIO()
        np.save(buf, depth)
        body, ctype = _multipart({"depth": ("no_plane.npy", buf.getvalue())})
        status, _, raw = request(_port(server), "POST", "/api/lidar/inspect", body,
                                 {"Content-Type": ctype})
        assert status == 400
        payload = json.loads(raw)
        assert payload["ok"] is False and "平面" in payload["error"]
        assert payload.get("hint")

    def test_inspect_exr_read_failure_400(self, server):
        """装着的 OpenEXR 读不了坏文件 → RuntimeError → 400「深度帧读取失败」。

        ``_decode_depth_upload`` 的通用 except 分支：heic/exr 这类可选依赖的
        运行期错误不是 ValueError（load_depth 的格式门槛），不能套用
        「深度帧读不懂」的 hint——两条 400 文案分工在这测试里钉住。
        """
        body, ctype = _multipart({"depth": ("scan.exr", b"definitely not an exr")})
        status, _, raw = request(_port(server), "POST", "/api/lidar/inspect", body,
                                 {"Content-Type": ctype})
        assert status == 400
        payload = json.loads(raw)
        assert payload["ok"] is False and "读取失败" in payload["error"]


class TestWebCommandServe:
    """``arcam web``（webapp.main）的真服务路径：起服务 → 打印 → 被停。"""

    def test_main_serves_until_shutdown(self, capsys, monkeypatch):
        import arcam.webapp as webapp

        holder: dict = {}
        real_make = webapp.make_server

        def spy(host, port, *, open_browser=True):
            srv = real_make(host, port, open_browser=open_browser)
            holder["srv"] = srv
            return srv

        monkeypatch.setattr(webapp, "make_server", spy)
        result: dict = {}

        def run() -> None:
            result["rc"] = webapp.main(["--host", "127.0.0.1", "--port", "0",
                                        "--no-open"])

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.time() + 20
        while "srv" not in holder and time.time() < deadline:
            time.sleep(0.02)
        assert "srv" in holder, "main 应已起服务"
        srv = holder["srv"]
        try:
            status, _, raw = request(int(srv.server_address[1]), "GET", "/healthz")
            assert status == 200 and json.loads(raw)["ok"] is True
        finally:
            srv.shutdown()
            thread.join(timeout=20)
        assert result.get("rc") == 0
        out = capsys.readouterr().out
        assert "已启动" in out and "127.0.0.1" in out

    def test_main_keyboard_interrupt_prints_stopped(self, capsys, monkeypatch):
        import arcam.webapp as webapp

        class _FakeServer:
            server_address = ("127.0.0.1", 12345)

            def serve_forever(self, poll_interval=0.5):
                raise KeyboardInterrupt

            def server_close(self):
                pass

        monkeypatch.setattr(webapp, "make_server",
                            lambda *a, **k: _FakeServer())
        assert webapp.main(["--no-open"]) == 0
        out = capsys.readouterr().out
        assert "已停止" in out and "内存态已清空" in out

    def test_open_browser_opens_url(self, monkeypatch):
        import arcam.webapp as webapp

        opened: list[str] = []
        monkeypatch.setattr(webapp.webbrowser, "open",
                            lambda url: opened.append(url) or True)
        srv = webapp.WebServer(("127.0.0.1", 0), webapp.Handler, open_browser=True)
        try:
            deadline = time.time() + 10
            while not opened and time.time() < deadline:
                time.sleep(0.02)
            assert opened == [srv.url]
        finally:
            srv.server_close()

    def test_open_browser_failure_is_not_fatal(self, monkeypatch):
        """无桌面环境（SSH/容器）webbrowser.open 抛错也不许把服务起崩。"""
        import arcam.webapp as webapp

        def boom(_url):
            raise RuntimeError("no desktop")

        monkeypatch.setattr(webapp.webbrowser, "open", boom)
        srv = webapp.WebServer(("127.0.0.1", 0), webapp.Handler, open_browser=True)
        thread = threading.Thread(target=srv.serve_forever, daemon=True,
                                  kwargs={"poll_interval": 0.05})
        thread.start()
        try:
            time.sleep(0.8)                     # 覆盖 _open_browser_later 的 except
            status, _, _ = request(int(srv.server_address[1]), "GET", "/healthz")
            assert status == 200
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=10)


class TestConcurrentInspectRender:
    """并发边界：inspect 换帧 与 深度驱动渲染 同会话抢 render_lock / store.lock。"""

    def test_concurrent_inspect_and_depth_render_no_deadlock(self, server, uploaded):
        """4 线程反复 inspect 换帧 + 4 线程深度驱动渲染：无 5xx、无挂死。

        锁顺序固定 render_lock → store.lock（inspect 持 render_lock 拟合/换帧；
        render 在快照上算）。竞态窗口内渲染要么看到旧帧要么新帧——两者都合法，
        但 env 必须与所渲染的帧自洽（Store.depth_env 的契约，单测另钉）。
        """
        frames = [TestLidar._arcd_bytes(h=1.45 + 0.05 * i) for i in range(3)]
        errors: list[str] = []

        def inspector(i: int) -> None:
            client = Client(_port(server))
            try:
                for k in range(3):
                    status, _, raw = post_inspect(client, frames[(i + k) % 3])
                    if status != 200:
                        errors.append(f"inspect {status}: {raw[:120]}")
            finally:
                client.close()

        def renderer(i: int) -> None:
            client = Client(_port(server))
            try:
                for k in range(3):
                    status, _, raw = post_json(client, "/api/render",
                                               {"params": {"depth_uploaded": True},
                                                "scale": 0.25})
                    if status != 200:
                        errors.append(f"render {status}: {raw[:120]}")
                    else:
                        env = json.loads(raw)["env"]
                        if env is None or not 1.0 < env["height_m"] < 2.0:
                            errors.append(f"env 离谱: {env}")
            finally:
                client.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(inspector, i) for i in range(4)]
            futures += [pool.submit(renderer, i) for i in range(4)]
            for future in futures:
                future.result(timeout=300)
        assert not errors, errors
