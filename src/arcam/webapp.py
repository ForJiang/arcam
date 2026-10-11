"""arcam.webapp —— 本地网页界面后端（``arcam web``，契约 = docs/WEB-SPEC.md §1）。

零第三方 web 框架：只用标准库 ``http.server``/``json``/``threading``/``webbrowser``/
``mimetypes`` 等 + numpy + PIL。单用户、本机回环、无账号、不上传任何东西；所有
上行数据（照片/立绘/深度帧/抠图结果）只活在**进程内存**里，重启即清空。

路由（WEB-SPEC §1，单用户内存态）::

    GET  /                         单页（webui/index.html；缺失时用内置占位页）
    GET  /static/*                 webui/ 下静态文件（MIME：html/css/js/png/svg/ico…）
    GET  /healthz                  存活探针（编排者/启动器冒烟用）
    POST /api/upload               multipart：字段 photo/char/depth
    POST /api/render               预览渲染（JSON：params + scale）
    POST /api/export               成品下载（target: png|layer|bundle）
    POST /api/matte                抠图（source: char|photo；模型缺失 → 400 带指引）
    POST /api/prep                 立绘自动修图链（steps 子集；替换会话立绘）
    POST /api/trim                 只裁边（修图按钮组单步入口）
    POST /api/reset                还原到上传时的立绘
    POST /api/batch                批量出图（JSON：photos dataURI 数组 + params）
    POST /api/lidar/inspect        深度帧诊断（multipart：字段 depth）
    POST /api/matte-result-as-char 把抠图结果设为当前立绘

 invariants（并发模型，改代码前必读）：

- **两把锁、固定顺序**：``render_lock``（重计算：渲染/导出/抠图/平面拟合，串行化）
  与 ``store.lock``（状态读写，短临界区）。顺序恒为 **render_lock → store.lock**，
  没有任何路径反向获取（上传只拿 store.lock，永不在持 store.lock 时碰
  render_lock）—— 遵守该顺序即不可能死锁；20 个并发 render 只会排队，不会卡死。
- **上传不阻塞渲染、渲染不阻塞上传**：渲染临界区里只对 store 做一次快照
  （:meth:`Store.snapshot`），随后在快照上算，新上传的图下一遍才生效。
- **异常全收敛**：任何 :class:`ApiError` / 参数错误 / 上游 ValueError/OSError 都以
  JSON ``{"ok": false, "error", "hint"?}`` 应答（已知输入问题 400，意外 bug 500），
  响应体里永远不出现 traceback（traceback 只打服务器 stderr）。

multipart 解析：**手写 buffer 解析**（不引 ``email``）—— 按 boundary 切段、逐段
拆头（Content-Disposition → name/filename），请求体 64MB 上限（超限在读 body 前
就 400，不吞字节）。
"""

from __future__ import annotations

import argparse
import base64
import difflib
import io
import json
import math
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import webbrowser
import zipfile
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image

from arcam.compose import _lidar_env, _merge_env, compose_shot
from arcam.depth import LiDARFrame, align_to_canvas, load_depth
from arcam.frames import frame_rect
from arcam.geometry import Camera
from arcam.matting import anime_cut
from arcam.numeric import Rect
from arcam.placement import FRAMES, SHOTS, Subject, current_placement
from arcam.plane import CameraEnv
from arcam.scene import build_scene
from arcam.video import encode_jpeg, encode_png

if TYPE_CHECKING:                                  # 仅标注期引用（运行时不需要）
    from arcam.placement import SpritePlacement

# ---------------------------------------------------------------- 常量

WEBUI_DIR = Path(__file__).resolve().parent / "webui"
"""前端三件套的落地目录（W2 的地盘；不存在时 GET / 退回内置占位页）。"""

MAX_UPLOAD_BYTES = 64 * 1024 * 1024
"""multipart 请求体上限（WEB-SPEC §1：>64MB → 400）。"""

PREVIEW_MAX_EDGE = 1280
"""预览 rendering 的照片长边上限（长边 ≤1280，保证 <1s，WEB-SPEC §1）。"""

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
"""zip 内文件时间戳写死 1980-01-01 —— 与 CLI bundle 同一口径（cli.ZIP_DATE_TIME）。"""

PNG_FILENAME = "arcam-shot.png"
LAYER_FILENAME = "arcam-char-layer.png"
BUNDLE_FILENAME = "arcam-bundle.zip"
MATTE_NAME = "matte.png"
"""抠图结果转为立绘时 build_scene 记的文件名（原版 cutout 结果无文件名概念）。"""

_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
}

#: params schema 默认值（WEB-SPEC §2 = 原版 S 默认，index.html:921-937）
PARAM_DEFAULTS: dict = {
    "x": 0.0, "d": 4.0, "h": 1.6, "hp": 1.65, "feq": 23.0, "pitch": 0.0, "roll": 0.0,
    "shot": "全身", "char_scale": 1.0, "frame": "满幅", "rot_basis": "world",
    "mode": "ground", "size_pct": 60.0, "edge_bar": 0, "tone": False,
    "light": True, "grain": True, "shadow": True,
    "shadow_az": 0.0, "shadow_opacity": 1.0,
    "base_only": False,
    "depth_uploaded": False,
}

#: 数值字段 → (人话描述, 附加约束)；None = 任意有限数
_NUM_SPECS: dict[str, tuple[str, str | None]] = {
    "x": ("人物横向偏移（米，+右）", None),
    "d": ("人物到相机的纵向距离（米）", "positive"),
    "h": ("相机离地高（米）", "positive"),
    "hp": ("人物身高（米）", "positive"),
    "feq": ("等效 35mm 焦距（mm）", "positive"),
    "pitch": ("俯角（度，低头为正）", None),
    "roll": ("滚转角（度，画面顺时针）", None),
    "char_scale": ("立绘倍率（只乘投影跨度）", "positive"),
    "size_pct": ("自由模式立绘占画面高度百分比", "positive"),
    "shadow_az": ("阴影光线方位角（度，0=正落脚下）", None),
    "shadow_opacity": ("阴影浓度倍率", "positive"),
}

#: choices 字段（与 CLI argparse 的 choices 逐字同源）
_CHOICE_SPECS: dict[str, tuple] = {
    "shot": tuple(SHOTS),
    "frame": tuple(FRAMES),
    "rot_basis": ("world", "frame"),
    "mode": ("ground", "free"),
    "edge_bar": (0, 1, 2, 3, 4),
}

_BOOL_FIELDS = ("tone", "light", "grain", "shadow", "base_only", "depth_uploaded")

#: /api/prep 的修图步骤名。**顺序即原版 autoPrepareChar 的执行序**
#: （index.html:2400-2440），steps 子集也按这个序跑，与整链逐字一致。
_PREP_STEPS = ("solid", "trim", "soften", "blur")

#: 环境量键（compose._ENV_OVERRIDE_KEYS 的 web 侧镜像）：params 里**显式出现**的键
#: 才进 env_overrides（LIDAR-SPEC §0「显式 > 测出 > 标称」的显式端）。
_ENV_KEYS = ("h", "pitch", "roll", "d", "x")

_FALLBACK_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AR 取景器</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<main id="app">
  <h1>AR 取景器</h1>
  <p class="hint" id="status">后端已就绪。前端界面 webui/index.html 尚未随包提供，
    本页为内置占位页：引用 /static/style.css 与 /static/app.js。</p>
</main>
<script src="/static/app.js"></script>
</body>
</html>
"""


# ---------------------------------------------------------------- 错误类型

class ApiError(Exception):
    """可展示给用户的请求层错误：HTTP status + 人话 error + 可选 hint。

    400 用于「输入不合法/缺素材」这类用户能自己修的问题；500 只留给意外 bug
    （由 handler 的兜底 except 兜住，响应体仍是不带 traceback 的 JSON）。
    """

    def __init__(self, status: int, error: str, hint: str | None = None):
        super().__init__(error)
        self.status = int(status)
        self.error = str(error)
        self.hint = hint


class ParamError(ValueError):
    """params 校验错误（WEB-SPEC §1：错误值 → 400 带 hint：哪个参数、可选值）。"""

    def __init__(self, error: str, hint: str):
        super().__init__(error)
        self.error = str(error)
        self.hint = str(hint)


# ---------------------------------------------------------------- 内存态

class Store:
    """单用户进程内存态（WEB-SPEC §1：进程重启即清空）。

    字段（全部为 numpy float64 或 None）::

        photo01  (H,W,3)  照片 RGB，[0,1]
        char01   (H,W,4)  立绘 RGBA，[0,1]（抠图结果经 /api/matte-result-as-char 顶上）
        depth    LiDARFrame | None  LiDAR 深度帧（inspect/upload 后常驻）
        matte01  (H,W,4) | None     最近一次抠图结果（未转正为立绘前）

    ``lock`` 只保护这些字段的读写，临界区极短；重计算用 WebServer.render_lock。
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.photo01: np.ndarray | None = None
        self.photo_name: str = ""
        self.char01: np.ndarray | None = None
        self.char_name: str = ""
        self.char_orig01: np.ndarray | None = None
        """上传时的立绘原图（/api/reset 的还原基线；prep/matte 结果不刷新它）。"""
        self.depth: LiDARFrame | None = None
        self.depth_name: str = ""
        self.matte01: np.ndarray | None = None
        self._env_cache: tuple[LiDARFrame, CameraEnv] | None = None
        """(帧, 环境量) 缓存：同一帧的平面只拟合一次（见 :meth:`depth_env`）。"""

    def set_char(self, arr: np.ndarray, name: str, *,
                 original: bool = False) -> None:
        """设当前立绘。``original=True``（上传路径）同时刷新还原基线——
        原版 setCharset 只在装图时写 chOrig（index.html:2354），修图结果不改基线。
        """
        with self.lock:
            self.char01 = arr
            self.char_name = name
            if original:
                self.char_orig01 = arr

    def snapshot(self) -> dict:
        """一次加锁取全量快照（渲染/导出/抠图在临界区外算，只依赖快照）。"""
        with self.lock:
            return {
                "photo01": self.photo01,
                "photo_name": self.photo_name,
                "char01": self.char01,
                "char_name": self.char_name,
                "char_orig01": self.char_orig01,
                "depth": self.depth,
                "matte01": self.matte01,
            }

    def depth_env(self) -> tuple[LiDARFrame, CameraEnv] | None:
        """深度帧 + 环境量（帧变了才重拟合；锁外做重活）。

        返回的 **(帧, 环境量) 恒为自洽一对**：环境量总是对所返回帧拟合出来的
        （拟合期间帧被并发换走也如此，见实现内注释）。无深度帧 → None。

        环境量只取决于**深度帧本身**：:func:`align_to_canvas` 是最近邻几何缩放、
        内参同比变换，拟出的平面不变（LIDAR-SPEC §2 对齐约定）。所以 inspect 时
        拟合一次、之后每遍渲染复用，预览少跑一遍我们自己的 RANSAC（实测省
        ~200-300ms/遍）。

        ⚠ 省不掉的是 :func:`compose_shot` **内部**的那一次拟合（它拿 ``depth``
        必拟，LIDAR-SPEC §4 的合并链）—— 那是 compose 模块的既有行为，webapp
        不改它。故深度驱动预览的总耗时 ≈ 一遍 compose（两趟 engine）+ 一次
        内部 RANSAC + 遮挡掩码；1600×1200 照片预览（800×600）实测 ~1s，
        无深度 ~0.6s。想再快只能动 compose.py（不属本文件职责）。
        """
        with self.lock:
            frame = self.depth
            if frame is None:
                return None
            cached = self._env_cache
            if cached is not None and cached[0] is frame:
                return cached
        env = _env_of(frame)                       # 锁外拟合：store.lock 是短临界区
        with self.lock:
            if self.depth is frame:
                self._env_cache = (frame, env)
            # 恒返回**刚拟出的** (帧, env) 自洽一对：拟合耗几百 ms，期间另一线程
            # （上传/inspect 换帧，均不持 render_lock）可能已换走 self.depth ——
            # 那时绝不能把新帧与旧 env 配成一对返回（遮挡/落位会静默失真），
            # 新帧留给下一遍渲染按需重拟
            return (frame, env)

    def set_photo(self, arr: np.ndarray, name: str) -> None:
        with self.lock:
            self.photo01 = arr
            self.photo_name = name

    def set_depth(self, frame: LiDARFrame | None, name: str,
                  env: CameraEnv | None = None) -> None:
        """换帧（可选顺带喂已算好的环境量 —— inspect 刚拟过，别白算）。

        缓存键是帧对象 identity：不给 ``env`` 就清缓存，下一遍渲染按需重拟合。
        """
        with self.lock:
            self.depth = frame
            self.depth_name = name
            self._env_cache = (frame, env) if (env is not None and frame is not None) else None

    def set_matte(self, arr: np.ndarray | None) -> None:
        with self.lock:
            self.matte01 = arr


# ---------------------------------------------------------------- 服务对象

class WebServer(ThreadingHTTPServer):
    """ThreadingHTTPServer + 内存态 + 渲染锁（WEB-SPEC §1 线程模型）。

    ``request_queue_size`` 是 listen backlog：socketserver 默认只有 5，浏览器
    一次会并发拉 html/css/js/favicon + API 请求，8 路以上突发时内核直接 reset
    超出的连接（实测 ConnectionResetError，测试 8 线程并发下必现级抖动）。
    64 是 stdlib 文档示例同量级取值，对本机单用户服务绰绰有余。
    """

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address, handler_cls, *, open_browser: bool = True):
        super().__init__(address, handler_cls)
        self.store = Store()
        #: 重计算锁（render/export/matte/lidar-inspect 串行；防并发抢 numpy 与
        #: onnxruntime 会话）。与 Store.lock 的顺序恒为 render_lock → store.lock。
        self.render_lock = threading.Lock()
        self.open_browser = bool(open_browser)
        host, port = self.server_address[0], self.server_address[1]
        self.url = f"http://{host}:{port}/"
        if self.open_browser:
            threading.Thread(target=self._open_browser_later, name="arcam-web-open",
                             daemon=True).start()

    def _open_browser_later(self) -> None:
        time.sleep(0.4)          # 等服务打印出启动行，浏览器再抢焦点
        try:
            webbrowser.open(self.url)
        except Exception:
            pass                 # 无桌面环境（SSH/容器）不致命


def make_server(host: str, port: int, *, open_browser: bool = True) -> ThreadingHTTPServer:
    """起一个（尚未 serve 的）本地网页服务；``port=0`` 时内核分配临时端口。

    Args:
        host: 监听地址（``arcam web`` 默认 127.0.0.1，仅本机回环）。
        port: 监听端口；0 = ephemeral（测试用）。
        open_browser: 是否在起服务后自动打开浏览器（默认 True）。
    """
    return WebServer((host, port), Handler, open_browser=open_browser)


def main(argv: list[str] | None = None) -> int:
    """``arcam web`` 入口：起服务 → 打印本机 URL → serve_forever 到 Ctrl-C。"""
    parser = argparse.ArgumentParser(
        prog="arcam web",
        description="本地网页界面（标准库 HTTP 服务）：照片/立绘上传、预览渲染、抠图、"
                    "LiDAR 深度诊断、成品下载。仅监听本机、单用户、进程退出即清空。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="契约见 docs/WEB-SPEC.md；前端在 src/arcam/webui/。")
    parser.add_argument("--host", default=DEFAULT_HOST, metavar="地址",
                        help=f"监听地址（默认 {DEFAULT_HOST}，仅本机回环，不做网络暴露）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, metavar="端口",
                        help=f"监听端口（默认 {DEFAULT_PORT}；占用时换个端口）")
    parser.add_argument("--no-open", action="store_true",
                        help="不自动打开浏览器（只用自己贴 URL）")
    args = parser.parse_args(argv)

    try:
        server = make_server(args.host, args.port, open_browser=not args.no_open)
    except OSError as exc:
        print(f"错误：起不来服务（{args.host}:{args.port}）：{exc}", file=sys.stderr)
        print("人话：端口大概被占用了 —— 换个 --port，或先停掉上次的服务。", file=sys.stderr)
        return 1

    host, port = server.server_address[0], server.server_address[1]
    # flush=True：启动器（.command/.bat）常把输出重定向到日志或管道，Python 对
    # 非终端的 stdout 是块缓冲 —— 不 flush 的话用户/上游脚本读不到启动行
    print(f"AR 取景器网页服务已启动：http://{host}:{port}/", flush=True)
    print("仅监听本机回环；照片/立绘/深度只在内存，Ctrl-C 后全部清空。", flush=True)
    if not args.no_open:
        print("正在打开浏览器；没反应就手动贴上面的地址。", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n已停止（内存态已清空）。", flush=True)
    finally:
        server.server_close()
    return 0


# ---------------------------------------------------------------- params 校验

def parse_params(raw) -> tuple[dict, set]:
    """校验/补全 params（WEB-SPEC §2 schema）。

    Args:
        raw: 前端传来的 ``params``（缺省 ``{}``）。

    Returns:
        ``(params, explicit)``：``params`` 是全量补默认后的 dict（键名/取值与
        WEB-SPEC §2 逐字一致）；``explicit`` 是**请求里显式出现**的键集合 ——
        LiDAR 优先级链（显式 > 测出 > 标称，LIDAR-SPEC §0）的 "显式" 判定口径，
        缺键即未动（原版 S 的布尔「用户动过没」由前端传显式键）。

    Raises:
        ParamError: 键名打错 / choices 越界 / 数值非有限（→ handler 400 + hint）。
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ParamError(f"params 应为 JSON 对象，得到 {type(raw).__name__}",
                         "params 形如 {\"x\":0.0,\"d\":4.0,…}（见 docs/WEB-SPEC.md §2）")

    out = dict(PARAM_DEFAULTS)
    explicit: set = set()
    for key, value in raw.items():
        if key not in PARAM_DEFAULTS:
            guess = difflib.get_close_matches(str(key), list(PARAM_DEFAULTS), n=1)
            maybe = f"（是否想写 {guess[0]!r}？）" if guess else ""
            raise ParamError(
                f"未知参数 {key!r}{maybe}",
                f"可用参数：{'、'.join(PARAM_DEFAULTS)}")
        if key in _NUM_SPECS:
            desc, rule = _NUM_SPECS[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ParamError(
                    f"参数 {key}（{desc}）应为数字，得到 {value!r}",
                    f"{key} 请给有限数字（米/度/倍率等，见 docs/WEB-SPEC.md §2）")
            number = float(value)
            if not math.isfinite(number):
                raise ParamError(
                    f"参数 {key}（{desc}）不是有限数：{value!r}",
                    f"{key} 请给有限数字，不要 NaN/Infinity")
            if rule == "positive" and number <= 0.0:
                raise ParamError(
                    f"参数 {key}（{desc}）应为正数，得到 {number:g}",
                    f"{key} 请给 > 0 的数")
            out[key] = number
        elif key in _CHOICE_SPECS:
            choices = _CHOICE_SPECS[key]
            if key == "edge_bar":
                ok = isinstance(value, int) and not isinstance(value, bool) \
                    and value in choices
            else:
                ok = value in choices
            if not ok:
                raise ParamError(
                    f"参数 {key} 取值 {value!r} 不在可选列表",
                    f"{key} 可选：{'、'.join(str(c) for c in choices)}")
            out[key] = value
        else:                                   # _BOOL_FIELDS
            if not isinstance(value, bool):
                raise ParamError(
                    f"参数 {key} 应为布尔值，得到 {value!r}",
                    f"{key} 请给 true 或 false")
            out[key] = value
        explicit.add(key)
    return out, explicit


def _parse_scale(raw) -> float:
    """scale 校验：缺省 1.0；须是 (0, 1] 的有限数（WEB-SPEC §1 ``{scale:≤1}``）。"""
    if raw is None:
        return 1.0
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ParamError(f"scale 应为数字，得到 {raw!r}", "scale 是预览降采样倍率，如 0.5")
    scale = float(raw)
    if not math.isfinite(scale) or scale <= 0.0 or scale > 1.0:
        raise ParamError(f"scale 取值 {scale:g} 不在 (0, 1]",
                         "scale 是预览降采样倍率（1 = 全尺寸预览）；export 永远全尺寸")
    return scale


def _prep_steps_of(data: dict) -> list[str]:
    """解析 ``/api/prep`` 请求体的 ``steps`` → 去重且按 :data:`_PREP_STEPS` 排好序的步骤表。

    - 键缺省 / ``null`` → 四步全跑（原版整链的默认行为）；
    - 类型不对（字符串/数字/布尔/对象…）或步骤名不认 → :class:`ApiError` 400
      带可选值清单（WEB-SPEC §1：输入不合法是用户能自己修的问题，不许掉进
      500 兜底—— 旧实现对 ``steps: 3`` 这类输入抛 TypeError，响应体写着
      「这是 arcam 的 bug」，把前端的错报成后端的病）；
    - 空数组 → 400（什么都不跑却回「修图完成」是误导，要么缺省全跑要么报错）；
    - 重复/乱序都接受：同一步只跑一次（原版每步也只执行一次）。
    """
    steps = data.get("steps")
    if steps is None:
        return list(_PREP_STEPS)
    if isinstance(steps, str) or not isinstance(steps, (list, tuple)):
        raise ApiError(
            400, f"steps 应为字符串数组，得到 {type(steps).__name__}",
            'steps 形如 ["solid","trim","soften","blur"]；不给 steps 键时四步全跑')
    if not steps:
        raise ApiError(400, "steps 是空的",
                       'steps 形如 ["solid","trim","soften","blur"]；'
                       '不给 steps 键时四步全跑')
    bad = [s for s in steps if s not in _PREP_STEPS]
    if bad:
        raise ApiError(
            400, f"steps 里有不认识的步骤 {bad}",
            "steps 可选：solid（纯色底）/ trim（裁边）/ soften（柔边）/ blur（微糊）")
    return [s for s in _PREP_STEPS if s in set(steps)]


# ---------------------------------------------------------------- 图片 IO

def _decode_image(data: bytes, name: str, mode: str) -> tuple[np.ndarray, bool]:
    """上传字节 → ``(uint8 (H,W,3|4) 数组, 源图是否带 alpha 通道)``。

    坏图 → 400（人话，不吐 PIL 栈）。第二个返回值是**源图**的透明语义：
    ``convert('RGBA')`` 会给没有 alpha 通道的图（JPEG、不透明 PNG）补一层全
    255 的 alpha，看不出「原图本来没透明底」—— 上传响应据此把 ``fg`` 置
    ``false``（WEB-SPEC §1 的 ``fg?`` 语义），前端才能提示「普通图先抠图」。
    """
    if not data:
        raise ApiError(400, f"{name} 是空文件", "请重新选择图片后再上传")
    try:
        with Image.open(io.BytesIO(data)) as im:
            has_alpha = _has_alpha(im)
            arr = np.asarray(im.convert(mode), dtype=np.uint8)
    except Exception as exc:                    # PIL 解析失败/炸弹图/截断都在这
        raise ApiError(
            400, f"{name} 不是可读的图片（{type(exc).__name__}: {exc}）",
            "照片/立绘请传 JPEG/PNG/WebP 等常见格式；深度帧请走「LiDAR 诊断」入口") from None
    if arr.ndim != 3 or arr.shape[2] != len(mode) or arr.shape[0] < 1 or arr.shape[1] < 1:
        raise ApiError(400, f"{name} 解出来的尺寸异常：{arr.shape}", "请换一张图再试")
    return arr, has_alpha


def _has_alpha(im: Image.Image) -> bool:
    """源图是否带 alpha 通道（mode RGBA/LA/PA、info['transparency'] 或 Pillow 判定）。"""
    if im.mode in ("RGBA", "LA", "PA"):
        return True
    if im.info.get("transparency") is not None:
        return True
    return bool(getattr(im, "has_transparency_data", False))


def _to_f01(arr_u8: np.ndarray) -> np.ndarray:
    """uint8 → float64 [0,1]（SPEC §1.2 计算口径，与其他模块一致）。"""
    return arr_u8.astype(np.float64) / 255.0


def _downscale01(arr01: np.ndarray, factor: float) -> np.ndarray:
    """按 factor 双线性降采样 float01 图（预览专用；factor ≥1 原样返回）。"""
    if factor >= 1.0:
        return arr01
    height, width = arr01.shape[:2]
    new_w = max(1, int(round(width * factor)))
    new_h = max(1, int(round(height * factor)))
    u8 = (np.clip(arr01, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    mode = "RGBA" if u8.shape[2] == 4 else "RGB"
    resized = Image.fromarray(np.ascontiguousarray(u8), mode).resize(
        (new_w, new_h), Image.BILINEAR)
    return np.asarray(resized, dtype=np.uint8).astype(np.float64) / 255.0


def _preview_factor(height: int, width: int, scale: float) -> float:
    """预览降采样系数：scale × （长边 ≤1280 的夹取），>1 不可能。"""
    long_edge = max(height, width)
    capped = min(1.0, PREVIEW_MAX_EDGE / float(long_edge)) if long_edge > 0 else 1.0
    return max(1e-3, min(float(scale), capped))


def _decode_depth_upload(data: bytes, filename: str) -> LiDARFrame:
    """上传的深度字节 → :class:`LiDARFrame`（落临时文件借 :func:`load_depth`）。

    ``load_depth`` 只吃路径（按后缀分派 PNG/TIF/npy/depth/heic/exr），所以先把
    字节写进临时目录再读；读不懂（不是深度/有效比例不足/格式不支持）→ 400 带
    hint。成功后帧已整体在内存，临时目录随即删除，返回时把 ``source`` 换成用户
    原始文件名（诊断信息里好看）。
    """
    if not data:
        raise ApiError(400, "深度文件是空的", "重新导出/选择一帧深度再上传")
    suffix = Path(filename or "").suffix.lower()
    known = (".png", ".tif", ".tiff", ".npy", ".depth", ".heic", ".heif", ".exr")
    with tempfile.TemporaryDirectory(prefix="arcam-depth-") as tmp:
        # 未知后缀 → 无后缀文件名，让 load_depth 走 ARCD magic 嗅探（.depth 落盘
        # 帧可能没后缀），嗅不到再报错，不猜格式
        path = Path(tmp) / ("depth" + (suffix if suffix in known else ""))
        path.write_bytes(data)
        try:
            frame = load_depth(path)
        except ValueError as exc:
            raise ApiError(
                400, f"深度帧读不懂：{exc}",
                "支持 16-bit PNG/TIF（毫米）/ .npy（米）/ .depth（magic ARCD）/"
                " 人像 HEIC；有效深度比例须 > 2%") from None
        except Exception as exc:                # heic/exr 可选依赖等的运行期错误
            raise ApiError(400, f"深度帧读取失败：{type(exc).__name__}: {exc}",
                           "可改用 16-bit PNG/TIF 或 .npy 深度文件") from None
    return LiDARFrame(depth_m=frame.depth_m, fx=frame.fx, fy=frame.fy, cx=frame.cx,
                      cy=frame.cy, roll_deg=frame.roll_deg, pitch_deg=frame.pitch_deg,
                      height_m=frame.height_m, source=filename or "上传的深度帧")


def _data_uri(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


# ---------------------------------------------------------------- 合成核心

@dataclass(frozen=True)
class _Render:
    """一次合成的完整产物（预览/导出共用）。"""
    shot01: np.ndarray          # uint8 (H,W,3) 出图
    scene: dict                  # build_scene 配方（与实际渲染同源）
    env: dict | None             # 深度测出的环境量（无深度 → None）
    char_layer: np.ndarray | None
    rect: Rect                   # 出图裁切框（frameRect，与 CLI 同口径）


#: /api/batch 单次照片数上限（防一次吃满 64MB 请求体 + 渲染时间爆炸）
_BATCH_MAX = 12


def _data_uri_to_rgb01(uri, what: str) -> np.ndarray:
    """dataURI → RGB float64 [0,1]。非 dataURI / 解不开 → ApiError 400。"""
    import base64
    import binascii
    import io
    if not isinstance(uri, str) or not uri.startswith("data:"):
        raise ApiError(400, f"{what} 不是 dataURI",
                       "应形如 data:image/png;base64,…（FileReader.readAsDataURL 的产物）")
    comma = uri.find(",")
    if comma < 0:
        raise ApiError(400, f"{what} 的 dataURI 缺逗号分隔", "检查 dataURI 是否完整")
    head = uri[:comma]
    if ";base64" not in head:
        raise ApiError(400, f"{what} 只支持 base64 dataURI", "URL 编码（非 base64）暂不支持")
    try:
        raw = base64.b64decode(uri[comma + 1:], validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ApiError(400, f"{what} 的 base64 解不开：{exc}", "重新生成 dataURI 再试") from None
    try:
        with Image.open(io.BytesIO(raw)) as im:
            arr = np.asarray(im.convert("RGB"), dtype=np.uint8)
    except Exception as exc:
        raise ApiError(400, f"{what} 不是合法图片：{type(exc).__name__}",
                       "确认 dataURI 对应的是 PNG/JPEG 等栅格图") from None
    return arr.astype(np.float64) / 255.0


def _compose(store: Store, params: dict, explicit: set, scale: float = 1.0,
             src_override: np.ndarray | None = None) -> _Render:
    """照片 + 立绘（可选深度）→ 出图/配方/环境量。

    ``src_override``：批量路径逐张换照片用（/api/batch）——其余（立绘、参数、
    深度帧）完全复用会话态，与 CLI ``arcam batch`` 一folder 一 char 同语义。

    与 ``arcam render`` 逐字同构（cli.cmd_render）：

    - ``depth_uploaded`` 且内存里有深度帧 → 深度驱动：先对齐到本遍画布
      （:func:`align_to_canvas`）再喂 ``compose_shot(depth=…)``；显式值 =
      params 里显式出现的键（x/d/h/pitch/roll，hp/feq 不参与环境量合并）。
      compose_shot 内部会按 LIDAR-SPEC §0 再做一次同样的合并，两遍结果逐位
      一致（E3 在显式 d/x 存在时被覆盖）—— 与 CLI 的两遍合并同一手法。
    - scene.json 用的 effective cam/subj（合并后）单独算一次，配方记的就是实际
      落位那一套值。

    Raises:
        ApiError: 没传照片 / depth_uploaded 但没有深度帧 / compose 的 ValueError。
    """
    snap = store.snapshot()
    photo01 = src_override if src_override is not None else snap["photo01"]
    if photo01 is None:
        raise ApiError(400, "还没有上传照片",
                       "先在左栏上传背景照片（POST /api/upload，字段 photo）再渲染")
    char01 = snap["char01"]
    char_name = snap["char_name"] or "char.png"

    factor = _preview_factor(photo01.shape[0], photo01.shape[1], scale)
    if factor < 1.0:
        photo01 = _downscale01(photo01, factor)
        if char01 is not None:
            char01 = _downscale01(char01, factor)
    height, width = photo01.shape[:2]

    cam = Camera(int(width), int(height), params["h"], params["feq"],
                 params["pitch"], params["roll"])
    subj = Subject(params["hp"], params["d"], params["x"])
    kw = dict(frame_key=params["frame"], mode=params["mode"],
              size_pct=params["size_pct"], char_scale=params["char_scale"],
              rot_basis=params["rot_basis"], tone=params["tone"],
              shadow=params["shadow"], grain=params["grain"],
              sample_light=params["light"],
              shadow_az=params["shadow_az"], shadow_opacity=params["shadow_opacity"],
              shot=params["shot"], edge_bar=params["edge_bar"])

    depth = None
    cam_eff, subj_eff, env = cam, subj, None
    if params["depth_uploaded"]:
        pair = store.depth_env()                  # (帧, 环境量)：平面拟合结果有缓存
        if pair is None:
            raise ApiError(
                400, "params.depth_uploaded=true，但内存里还没有深度帧",
                "先用「LiDAR 诊断」（POST /api/lidar/inspect，字段 depth）上传一帧深度")
        frame, env = pair
        # 深度帧分辨率通常与画布不同 → 先对齐到本遍画布（compose 内部会对同一
        # 尺寸的帧做恒等对齐，两遍不打架）。env 按原始帧拟合（缓存）：对齐是
        # 几何等比缩放，平面不变（Store.depth_env）
        depth = align_to_canvas(frame, int(width), int(height))
        overrides = {k: params[k] for k in _ENV_KEYS if k in explicit}
        cam_eff, subj_eff, _notes = _merge_env(cam, subj, overrides, env,
                                               depth=depth, mode=params["mode"],
                                               shot=params["shot"])
        # 把 effective 值全量显式喂回去：compose_shot 内部再合并一次得到同一结果
        # （cli.cmd_render 同款两遍合并），scene 记的也就是这一遍实际落位的值
        kw.update(depth=depth, env_overrides={
            "h": cam_eff.height_m, "pitch": cam_eff.pitch_deg,
            "roll": cam_eff.roll_deg, "d": subj_eff.distance_m,
            "x": subj_eff.lateral_m})

    try:
        if params["base_only"]:
            # 3D 路径（webui）：后端只出**照片底**（同裁切框），立绘层由客户端
            # WebGL（mmd.js）合成——Python 侧没有 WebGL，这是唯一分工方式。
            # scene 仍记 2D 落位（与 2D 路径同构），mmd 段由客户端挂。
            composed = compose_shot(photo01, None, subj, cam, **kw)
        else:
            composed = compose_shot(photo01, char01, subj, cam, **kw)
    except ValueError as exc:
        raise ApiError(400, f"合成失败：{exc}",
                       "检查摆放参数（h/d/x/pitch/roll/feq…）与素材尺寸是否合理") from None

    placement = _scene_placement(char01, subj_eff, cam_eff, params)
    scene = build_scene(composed.rect, placement, subj_eff, cam_eff,
                        frame_key=params["frame"], char_rgba01=char01,
                        char_name=char_name, mode=params["mode"],
                        edge_bar=params["edge_bar"], shot=params["shot"],
                        char_scale=params["char_scale"])
    env_out = None
    if composed.env is not None:
        env_out = {"height_m": float(composed.env.height_m),
                   "pitch_deg": float(composed.env.pitch_deg),
                   "roll_deg": float(composed.env.roll_deg)}
    return _Render(shot01=composed.shot, scene=scene, env=env_out,
                   char_layer=composed.char_layer, rect=composed.rect)


def _scene_placement(char01: np.ndarray | None, subj: Subject, cam: Camera,
                    params: dict) -> SpritePlacement | None:
    """给 build_scene 单独算一次落位（cli._placement 的 web 同构）。

    只传当前模式收得下的 kwargs（ground 不收 size_pct/edge_bar/rect、free 不收
    char_scale/shot）—— placement.py 两个分支的签名决定的，否则 scene 记的
    参数与实际渲染不符。

    ⚠ free 分支必须与 compose._placement 同参：``rot_basis`` 与 ``rect``
    （本遍 frame_rect）都要传——漏传会让 scene.json 记的落位与预览/导出图
    不一致（rot_basis=frame 时记 roll° 而渲染 0°；edge_bar 吸附记不吸而渲染
    吸住）。ground 分支不收 rect（贴地模式无吸附）。
    """
    if char01 is None:
        return None
    if params["mode"] == "free":
        return current_placement(char01, subj, cam, mode="free",
                                 size_pct=params["size_pct"],
                                 edge_bar=params["edge_bar"],
                                 rot_basis=params["rot_basis"],
                                 rect=frame_rect(params["frame"], cam.width, cam.height))
    return current_placement(char01, subj, cam, mode="ground",
                             char_scale=params["char_scale"],
                             rot_basis=params["rot_basis"], shot=params["shot"])


def _env_of(frame: LiDARFrame) -> CameraEnv:
    """深度帧 → CameraEnv（LIDAR-SPEC §4①：fit_ground_plane → camera_env_from_plane）。

    ARKit 给了 roll（``frame.roll_deg``）就交给 camera_env_from_plane 先反旋再解
    pitch。实现走 :func:`arcam.compose._lidar_env`—— 「拟合 → 解环境量」这条链
    全仓只此一份（compose/cli 也用它）：roll 透传口径曾经在 cli 的 batch 分支
    漏过一次（同一帧解出另一组 roll/pitch），各写一份就会再漏，故这里不重复
    推导。点数不足等 ValueError → 400 人话。
    """
    try:
        return _lidar_env(frame)
    except ValueError as exc:
        raise ApiError(400, f"深度帧拟不出地面平面：{exc}",
                       "换一帧覆盖率更高的深度（有效深度比例 > 2% 才会被读入）") from None


def _scene_bytes(scene: dict) -> bytes:
    """scene dict → JSON 字节（与 CLI bundle 同款：ensure_ascii=False, indent=2）。"""
    return json.dumps(scene, ensure_ascii=False, indent=2).encode("utf-8")


# ---------------------------------------------------------------- multipart

def parse_multipart(body: bytes, content_type: str) -> dict[str, tuple[bytes, str]]:
    """**手写 buffer** multipart/form-data 解析（不引 email 模块）。

    按 boundary 切段 → 逐段 ``\\r\\n\\r\\n`` 拆头体 → 从 Content-Disposition 取
    ``name``/``filename``。返回 ``{字段名: (原始字节, 文件名)}``；无 boundary /
    一个字段都解不出 → :class:`ApiError` 400。字段内容不会含 boundary 串（这是
    multipart 编码的基本保证），故直接切分是安全的。

    请求体大小上限由调用方（:meth:`Handler._read_body`）在**读之前**把关——
    坏 multipart 最多把已经读到的 64MB 切几刀，不会无限吃内存。
    """
    marker = "boundary="
    idx = content_type.find(marker)
    if idx < 0:
        raise ApiError(400, "上传请求缺少 multipart boundary",
                       "Content-Type 应为 multipart/form-data; boundary=…（fetch 的 FormData 会自动带）")
    boundary = content_type[idx + len(marker):].strip().strip('"').strip()
    if not boundary:
        raise ApiError(400, "multipart boundary 是空的", "换 fetch + FormData 重新上传")
    delimiter = b"--" + boundary.encode("latin-1", "replace")
    out: dict[str, tuple[bytes, str]] = {}
    for chunk in body.split(delimiter)[1:]:
        if chunk.startswith(b"--"):              # 收尾 delimiter
            break
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        elif chunk.startswith(b"\n"):
            chunk = chunk[1:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        elif chunk.endswith(b"\n"):
            chunk = chunk[:-1]
        if not chunk:
            continue
        sep = chunk.find(b"\r\n\r\n")
        tail = 4
        if sep < 0:
            sep = chunk.find(b"\n\n")
            tail = 2
        if sep < 0:
            continue                             # 没有头/体分隔的段：跳过
        headers = chunk[:sep].decode("utf-8", "replace")
        data = chunk[sep + tail:]
        name = _disposition_value(headers, "name")
        if name is None:
            continue
        filename = _disposition_value(headers, "filename") or ""
        # 同名多段（罕见）：保留第一段，与浏览器 FormData 单字段语义一致
        if name not in out:
            out[name] = (data, filename)
    if not out:
        raise ApiError(400, "multipart 里没有解析出任何字段",
                       "请确认字段名是 photo / char / depth（fetch 的 FormData.append('photo', file)）")
    return out


def _disposition_value(headers: str, key: str) -> str | None:
    """段头 Content-Disposition 里的 ``name`` / ``filename``（去引号）。"""
    for line in headers.splitlines():
        if line.lower().startswith("content-disposition:"):
            for part in line.split(";")[1:]:
                name, _, value = part.strip().partition("=")
                if name.strip().lower() == key:
                    return value.strip().strip('"')
    return None


# ---------------------------------------------------------------- HTTP 处理

class Handler(BaseHTTPRequestHandler):
    """全部路由 + 异常收敛（WEB-SPEC §1 硬规则）。"""

    protocol_version = "HTTP/1.1"                # keep-alive；每个响应都带 Content-Length
    server_version = "arcam-web/1.0"
    _body_read = False                           # 本连接当前请求的 body 是否已读完

    # ---- 小工具 ----

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_bytes(self, status: int, body: bytes, content_type: str,
                    extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_error(self, exc: ApiError) -> None:
        payload: dict = {"ok": False, "error": exc.error}
        if exc.hint:
            payload["hint"] = exc.hint
        self._send_json(exc.status, payload)

    def _read_body(self) -> bytes:
        """读请求体（Content-Length 或 chunked），64MB 上限；超限在读之前就 400。

        读完会置 ``self._body_read = True``：:meth:`_drain_body` 靠它知道请求体
        已被消费（HTTP/1.1 keep-alive 的卫生保证，见 :meth:`_drain_body`）。
        """
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            self._body_read = True
            return self._read_chunked()
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._body_read = True
            return b""
        try:
            length = int(raw_length)
        except ValueError:
            self.close_connection = True
            raise ApiError(400, f"Content-Length 不是整数：{raw_length!r}",
                           "用 fetch 发请求会自动带正确的 Content-Length") from None
        if length < 0:
            self.close_connection = True
            raise ApiError(400, "Content-Length 是负数", "用 fetch 发请求会自动带正确的 Content-Length")
        if length > MAX_UPLOAD_BYTES:
            # 一个字节都不读：直接 400 并断连，免得为 64MB+ 的垃圾吃带宽/内存
            self.close_connection = True
            raise ApiError(
                400, f"请求体 {length} B 超过上限 {MAX_UPLOAD_BYTES} B（64MB）",
                "本地工具只吃单张照片/立绘/深度帧；请压缩或换更小的文件")
        self._body_read = True
        return self._read_exact(length)

    def _read_exact(self, length: int) -> bytes:
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            piece = self.rfile.read(min(remaining, 1 << 20))
            if not piece:
                self.close_connection = True
                raise ApiError(400, "请求体比 Content-Length 短（连接被掐断）",
                               "重新上传试试；持续失败就刷新页面")
            chunks.append(piece)
            remaining -= len(piece)
        return b"".join(chunks)

    def _read_chunked(self) -> bytes:
        """最小 chunked 解码器（fetch 流式 body 用；总长度同样受 64MB 限制）。"""
        chunks: list[bytes] = []
        total = 0
        while True:
            line = self.rfile.readline(1024)
            if not line:
                self.close_connection = True
                raise ApiError(400, "chunked 请求体不完整", "重新上传试试")
            try:
                size = int(line.split(b";")[0].strip() or b"0", 16)
            except ValueError:
                self.close_connection = True
                raise ApiError(400, f"chunk 大小行不是十六进制：{line!r}",
                               "用非流式 fetch（Blob/FormData）发请求") from None
            if size == 0:
                # 拖车头部读到空行为止
                while True:
                    trailer = self.rfile.readline(1024)
                    if not trailer or trailer in (b"\r\n", b"\n"):
                        break
                return b"".join(chunks)
            total += size
            if total > MAX_UPLOAD_BYTES:
                self.close_connection = True
                raise ApiError(
                    400, f"chunked 请求体超过上限 {MAX_UPLOAD_BYTES} B（64MB）",
                    "本地工具只吃单张照片/立绘/深度帧；请压缩或换更小的文件")
            chunks.append(self._read_exact(size))
            self.rfile.readline(1024)            # chunk 结尾 CRLF

    def _read_json(self) -> dict:
        body = self._read_body()
        if not body:
            return {}
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, f"请求体不是合法 JSON：{exc}",
                           "用 fetch(…, {method:'POST', body: JSON.stringify({…})}) 并带 "
                           "Content-Type: application/json") from None
        if not isinstance(data, dict):
            raise ApiError(400, "请求体顶层应是 JSON 对象", "形如 {\"params\": {…}, \"scale\": 1}")
        return data

    def _params_of(self, data: dict) -> tuple[dict, set, float]:
        """解析请求里的 params / scale（校验失败 → 400 带 hint）。"""
        try:
            params, explicit = parse_params(data.get("params", {}))
            scale = _parse_scale(data.get("scale"))
        except ParamError as exc:
            raise ApiError(400, exc.error, exc.hint) from None
        return params, explicit, scale

    def _expected_length(self) -> int | None:
        """请求体应有长度（Content-Length；chunked → None）。不消费任何字节。"""
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            return None
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        try:
            return max(0, int(raw))
        except ValueError:
            return 0

    def _drain_body(self, expected: int | None) -> None:
        """keep-alive 卫生：把 handler 没读的请求体读掉（或断连）。

        HTTP/1.1 同一条连接会顺序处理多条请求；某个 handler 忘了读 body 时，
        残体会拼进下一条请求行（"{}POST /api/…" → 501），把连接永久搅坏。
        这里兜底：Content-Length 已知就把剩余字节读掉；chunked 猜不出长度，
        直接断连重开（宁慢不漏）。GET 也走这条（curl ``-X GET -d`` 会带 body，
        浏览器不会）—— 响应发完再决定读不读，所以对正常 GET 是 no-op。
        """
        if self._body_read or self.close_connection:
            return
        if expected is None:                       # chunked 且没读：无法安全续接
            self.close_connection = True
            return
        if expected <= 0:
            return
        if expected > MAX_UPLOAD_BYTES:
            # 超出上传上限的残留 body：不读（读它就是在替垃圾消耗带宽/内存），
            # 直接断连让下一条请求开新连接
            self.close_connection = True
            return
        try:
            self._read_exact(expected)
        except ApiError:
            self.close_connection = True
        self._body_read = True

    def _store(self) -> Store:
        return self.server.store                   # type: ignore[attr-defined]

    # ---- GET ----

    def do_GET(self) -> None:
        expected = self._expected_length()
        try:
            path = urllib.parse.urlsplit(self.path).path
            if path in ("/", "/index.html"):
                self._serve_index()
            elif path.startswith("/static/"):
                self._serve_static(path)
            elif path == "/healthz":
                self._send_json(200, {"ok": True, "service": "arcam-web"})
            else:
                raise ApiError(404, f"未知路径 {path}",
                               "可用：GET / 、GET /static/* 、POST /api/upload|render|export|"
                               "matte|prep|trim|reset|batch|lidar/inspect|matte-result-as-char")
        except ApiError as exc:
            self._send_error(exc)
        except Exception as exc:                    # 兜底：不吐 traceback 给浏览器
            traceback.print_exc()
            self._send_json(500, {
                "ok": False,
                "error": f"服务器内部错误（{type(exc).__name__}）：{exc}",
                "hint": "这是 arcam 的 bug，请把操作步骤反馈给维护者"})
        finally:
            self._drain_body(expected)

    def _serve_index(self) -> None:
        index = WEBUI_DIR / "index.html"
        if index.is_file():
            self._send_bytes(200, index.read_bytes(), _MIME_TYPES[".html"])
            return
        # 前端（webui/）还没就绪：内置占位页照样引用 /static/style.css 与
        # /static/app.js，编排者冒烟与测试门禁（引用了两个静态文件）不依赖 W2
        self._send_bytes(200, _FALLBACK_HTML.encode("utf-8"), _MIME_TYPES[".html"])

    def _serve_static(self, path: str) -> None:
        relative = urllib.parse.unquote(path[len("/static/"):])
        root = WEBUI_DIR.resolve()
        try:
            target = (root / relative).resolve() if relative else root
            is_file = target.is_file()
        except (OSError, ValueError):
            # 路径解析本身失败（%00 空字节 / 超长名 / 非法字符）：坏输入 → 400，
            # 不进 500（也不把 lstat 的内部报错文本透出响应体）
            raise ApiError(400, f"静态路径无法解析：/static/{relative}",
                           "URL 里不要带 %00 这类控制字符或超长文件名") from None
        if target != root and root not in target.parents or not is_file:
            raise ApiError(404, f"静态文件不存在：/static/{relative}",
                           "前端三件套在 src/arcam/webui/（style.css / app.js 等）")
        suffix = target.suffix.lower()
        self._send_bytes(200, target.read_bytes(),
                         _MIME_TYPES.get(suffix, "application/octet-stream"))

    # ---- POST ----

    def do_POST(self) -> None:
        self._body_read = False
        expected = self._expected_length()
        try:
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/upload":
                self._api_upload()
            elif path == "/api/render":
                self._api_render()
            elif path == "/api/export":
                self._api_export()
            elif path == "/api/matte":
                self._api_matte()
            elif path == "/api/lidar/inspect":
                self._api_lidar_inspect()
            elif path == "/api/matte-result-as-char":
                self._api_matte_as_char()
            elif path == "/api/prep":
                self._api_prep()
            elif path == "/api/trim":
                self._api_trim()
            elif path == "/api/reset":
                self._api_reset()
            elif path == "/api/batch":
                self._api_batch()
            else:
                raise ApiError(404, f"未知接口 {path}",
                               "可用：POST /api/upload|render|export|matte|prep|trim|reset|"
                               "batch|lidar/inspect|matte-result-as-char")
        except ApiError as exc:
            self._send_error(exc)
        except Exception as exc:                    # 兜底：不吐 traceback 给浏览器
            traceback.print_exc()
            self._send_json(500, {
                "ok": False,
                "error": f"服务器内部错误（{type(exc).__name__}）：{exc}",
                "hint": "这是 arcam 的 bug，请把操作步骤反馈给维护者"})
        finally:
            self._drain_body(expected)

    def _api_upload(self) -> None:
        body = self._read_body()
        fields = parse_multipart(body, self.headers.get("Content-Type", ""))
        store = self._store()
        started = time.perf_counter()
        info: dict = {}
        kinds: list[str] = []
        # 固定顺序 photo → char → depth：一次请求传多个字段时响应里的 kind 可预期
        if "photo" in fields:
            data, filename = fields["photo"]
            arr, _has_alpha = _decode_image(data, "照片", "RGB")
            store.set_photo(_to_f01(arr), _safe_name(filename, "photo.png"))
            kinds.append("photo")
            info = {"width": int(arr.shape[1]), "height": int(arr.shape[0])}
        if "char" in fields:
            data, filename = fields["char"]
            arr, has_alpha = _decode_image(data, "立绘", "RGBA")
            store.set_char(_to_f01(arr), _safe_name(filename, "char.png"), original=True)
            kinds.append("char")
            # fg 语义（与前端 app.js 对齐）：源图没 alpha 通道 → false（提示先抠图）；
            # 有 alpha → 不透明像素占比（原版 cutout 的 fg 口径同族）
            opaque = float((arr[..., 3] >= 128).mean())
            info = {"width": int(arr.shape[1]), "height": int(arr.shape[0]),
                    "fg": round(opaque, 4) if has_alpha else False}
        if "depth" in fields:
            data, filename = fields["depth"]
            frame = _decode_depth_upload(data, _safe_name(filename, "depth.npy"))
            store.set_depth(frame, frame.source)
            kinds.append("depth")
            info = {"width": int(frame.depth_m.shape[1]),
                    "height": int(frame.depth_m.shape[0])}
        if not kinds:
            raise ApiError(400, "上传请求里没有 photo / char / depth 字段",
                           "字段名固定为 photo（照片）/ char（立绘）/ depth（深度帧）")
        self._send_json(200, {
            "ok": True, "kind": kinds[-1], "kinds": kinds,
            "ms": round((time.perf_counter() - started) * 1000.0, 3), **info})

    def _api_render(self) -> None:
        data = self._read_json()
        params, explicit, scale = self._params_of(data)
        started = time.perf_counter()
        with self.server.render_lock:                # type: ignore[attr-defined]
            result = _compose(self._store(), params, explicit, scale=scale)
        ms = (time.perf_counter() - started) * 1000.0
        self._send_json(200, {
            "ok": True,
            "image": _data_uri(encode_png(result.shot01)),
            "scene": result.scene,
            "env": result.env,
            "ms": round(ms, 3),
        })

    def _api_export(self) -> None:
        data = self._read_json()
        params, explicit, _scale = self._params_of(data)
        target = data.get("target", "png")
        if target not in ("png", "layer", "bundle"):
            raise ApiError(400, f"target 取值 {target!r} 不在可选列表",
                           "target 可选：png（出图）、layer（RGBA 立绘层）、"
                           "bundle（shot.jpg + scene.json + character.png 三件套 zip）")
        started = time.perf_counter()
        with self.server.render_lock:                # type: ignore[attr-defined]
            snap = self._store().snapshot()
            result = _compose(self._store(), params, explicit, scale=1.0)   # export 永远全尺寸
            elapsed = (time.perf_counter() - started) * 1000.0
            if target == "png":
                body, content_type, filename = encode_png(result.shot01), "image/png", PNG_FILENAME
            elif target == "layer":
                layer = result.char_layer
                if layer is None or snap["char01"] is None:
                    raise ApiError(400, "还没有上传立绘，出不了立绘层",
                                   "先上传立绘（POST /api/upload，字段 char）；普通图可先"
                                   "用 /api/matte 抠图再 /api/matte-result-as-char")
                body, content_type, filename = encode_png(layer), "image/png", LAYER_FILENAME
            else:
                if snap["char01"] is None:
                    raise ApiError(400, "素材包需要立绘",
                                   "先上传立绘（POST /api/upload，字段 char）")
                parts = (("shot.jpg", encode_jpeg(result.shot01, 95)),
                         ("scene.json", _scene_bytes(result.scene)),
                         ("character.png", encode_png(result.char_layer)))
                body, content_type, filename = _zip_bytes(parts), "application/zip", BUNDLE_FILENAME
        self._send_bytes(200, body, content_type, {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Arcam-Export": f"{target}; {elapsed:.0f}ms",
        })

    def _api_batch(self) -> None:
        """批量出图（CLI arcam batch 的 web 版；FEATURE-SPEC §7）。

        JSON：``{photos: [dataURI, …], params: {…}}`` → zip（每张一个
        ``shot_<i>.png``，STORED 不压缩、时间戳固定——与 CLI bundle 同口径，
        同参同图逐字节可复现）。照片以 dataURI 数组进（不扩 multipart 解析：
        parse_multipart 是同名字段覆盖的 dict，批量要多个 photo 得改它的契约）。
        单张失败不拖垮整批：跳过的张数进 ``X-Arcam-Batch-Failed`` 头（**只放
        数字**——HTTP 头是 latin-1，中文原因放进去会 UnicodeEncodeError 直接
        500，v1.6 踩过），具体原因写进 zip 里的 ``_errors.txt``（UTF-8，只在
        有失败时才加，不污染全成功的产物）。
        """
        data = self._read_json()
        params, explicit, _scale = self._params_of(data)
        photos = data.get("photos")
        if not isinstance(photos, list) or not photos:
            raise ApiError(400, "photos 要是非空 dataURI 数组",
                           "photos: ['data:image/png;base64,…', …]（先经 /api/upload "
                           "或 FileReader 转 dataURI）")
        if len(photos) > _BATCH_MAX:
            raise ApiError(400, f"一次最多 {_BATCH_MAX} 张（收到 {len(photos)}）",
                           "分批处理；单张渲染不便宜，别一次塞太多")
        snap = self._store().snapshot()
        char01 = snap["char01"]
        if char01 is None:
            raise ApiError(400, "批量出图需要立绘",
                           "先上传立绘（POST /api/upload，字段 char）")
        started = time.perf_counter()
        parts, failed = [], []               # failed: [(1-based 序号, 原因)]
        with self.server.render_lock:                # type: ignore[attr-defined]
            for i, uri in enumerate(photos):
                try:
                    src01 = _data_uri_to_rgb01(uri, f"第 {i + 1} 张照片")
                except ApiError as exc:
                    failed.append((i + 1, exc.error))
                    continue
                result = _compose(self._store(), params, explicit, scale=1.0,
                                  src_override=src01)
                parts.append((f"shot_{i + 1:03d}.png", encode_png(result.shot01)))
        if not parts:
            detail = "；".join(f"第 {i} 张 {why}" for i, why in failed[:3])
            raise ApiError(400, f"一张都没出图：{detail}",
                           "检查 photos 里的 dataURI 是否是合法图片")
        if failed:
            # 明细进 zip（UTF-8 文本）而不是响应头（latin-1，见 docstring）
            lines = ["arcam batch —— 跳过的照片及原因：", ""]
            lines += [f"  第 {i} 张：{why}" for i, why in failed]
            parts.append(("_errors.txt", "\n".join(lines).encode("utf-8")))
        body = _zip_bytes(parts)
        elapsed = time.perf_counter() - started
        headers = {"Content-Disposition": 'attachment; filename="arcam-batch.zip"',
                   "X-Arcam-Batch": f"{len(parts) - (1 if failed else 0)} ok, "
                                    f"{len(failed)} failed; {elapsed * 1000.0:.0f}ms"}
        if failed:
            headers["X-Arcam-Batch-Failed"] = str(len(failed))
        self._send_bytes(200, body, "application/zip", headers)

    def _api_prep(self) -> None:
        """立绘自动修图链（FEATURE-SPEC §1；index.html:2400-2440 的 1:1 对译）。

        steps 缺省四步全跑；结果**替换会话内的立绘**（原版修图线改
        chBase/chCanvas 的同语义），后续 render 直接用它。

        四步全点走 :func:`arcam.matte_tools.auto_prepare_char`（原版整链：同参
        同序 + 「吃过头就整条回退」兜底）；只点部分步走 :meth:`_prep_subset`
        （同序同参，没点的步跳过）。**两条路都只跑一遍**——旧实现先算整链再把
        子集结果覆盖上去：3000×1500 立绘 steps=["trim"] 实测 HTTP 层
        3514ms → 57ms（整链本身 ~2.4s，全白烧）。
        """
        data = self._read_json()
        steps = _prep_steps_of(data)
        snap = self._store().snapshot()
        char01 = snap["char01"]
        if char01 is None:
            raise ApiError(400, "还没有上传立绘，没东西可修",
                           "先上传立绘（POST /api/upload 的 char 字段）再修图")
        started = time.perf_counter()
        from arcam.matte_tools import auto_prepare_char
        with self.server.render_lock:                # type: ignore[attr-defined]
            if len(steps) == len(_PREP_STEPS):
                prep = auto_prepare_char(char01, enabled=True)
            else:
                prep = self._prep_subset(char01, steps)
        self._store().set_char(prep.rgba01, snap["char_name"])
        self._send_json(200, {
            "ok": True,
            "image": _data_uri(encode_png(prep.rgba01)),
            "cutRatio": None if prep.cut_ratio is None else round(float(prep.cut_ratio), 4),
            "trimmed": prep.trimmed,
            # PreparedChar.half_body 就是对返回图实算的（两个构造路径都如此），
            # 不必再扫一遍整图
            "halfBody": prep.half_body,
            "actions": list(prep.actions),
            "ms": round((time.perf_counter() - started) * 1000.0, 3),
        })

    @staticmethod
    def _prep_subset(char01: np.ndarray, steps: list[str]):
        """按 steps 子集跑修图链（顺序与原版一致，没点的步跳过）。

        与 :func:`arcam.matte_tools.auto_prepare_char` **同参同序**，收尾两条
        规则也逐字一致（否则单步入口会比整链更容易产出碎立绘）：

        - ④ blur 只在结果真变了时记 ``note.autoBlur``（整链的判据）；
        - ⑥ 动过手（cut 或 trim）且最终内容 bbox 任一边 < 8px → 整条回退原图
          （index.html:2435-2439 的兜底，与整链共用 matte_tools 的门限常量）。
        """
        from arcam.matte_tools import (
            PreparedChar, _BLUR_K, _CUT_FEATHER, _CUT_TOL, _MIN_CONTENT_SIDE,
            _SOFTEN_ERODE, _SOFTEN_FEATHER, _flood_solid, auto_trim,
            content_bounds, lens_blur, soften_edge,
        )
        from arcam.matte_tools import CUT_RATIO_MAX, CUT_RATIO_MIN, is_half_body
        arr = char01
        cut_ratio = None
        trimmed = False
        actions: list[str] = []
        if "solid" in steps:
            try:
                out, ratio = _flood_solid(arr, _CUT_TOL, _CUT_FEATHER)
                if out is not None and CUT_RATIO_MIN <= ratio <= CUT_RATIO_MAX:
                    arr = out
                    cut_ratio = ratio
                    actions.append("note.autoCut")
            except Exception:
                pass
        if "trim" in steps:
            try:
                res = auto_trim(arr)
                if res is not None:
                    arr = res[0]
                    trimmed = True
                    actions.append("note.autoTrim")
            except Exception:
                pass
        if "soften" in steps:
            try:
                soft, changed = soften_edge(arr, erode=_SOFTEN_ERODE,
                                            feather=_SOFTEN_FEATHER)
                if changed:
                    arr = soft
                    actions.append("note.autoSoft")
            except Exception:
                pass
        if "blur" in steps:
            try:
                blurred = lens_blur(arr, _BLUR_K)
                if not np.array_equal(blurred, arr):
                    arr = blurred
                    actions.append("note.autoBlur")
            except Exception:
                pass
        # ⑥ 兜底：动过手且最终内容只剩一条缝 → 整条回退（与整链同一门限）
        if cut_ratio is not None or trimmed:
            bounds = content_bounds(arr)
            if (bounds is None or bounds[2] < _MIN_CONTENT_SIDE
                    or bounds[3] < _MIN_CONTENT_SIDE):
                return PreparedChar(char01, None, False, is_half_body(char01), ())
        return PreparedChar(arr, cut_ratio, trimmed, is_half_body(arr),
                            tuple(actions))

    def _api_trim(self) -> None:
        """只裁边（修图按钮组的单步入口；从当前立绘重算，不叠加）。"""
        from arcam.matte_tools import auto_trim
        self._read_json()
        snap = self._store().snapshot()
        char01 = snap["char01"]
        if char01 is None:
            raise ApiError(400, "还没有上传立绘，没东西可裁",
                           "先上传立绘再点「自动裁边」")
        with self.server.render_lock:                # type: ignore[attr-defined]
            res = auto_trim(char01)
        if res is None:
            raise ApiError(400, "裁不动：四周没有透明边距，或内容太小",
                           "只有在立绘四周有透明留白时裁边才有意义")
        arr, rect = res
        self._store().set_char(arr, snap["char_name"])
        self._send_json(200, {
            "ok": True,
            "image": _data_uri(encode_png(arr)),
            "rect": [int(v) for v in rect],
            "trimmed": True,
        })

    def _api_reset(self) -> None:
        """还原到上传时的立绘（原版 chReset → chOrig，index.html:2783-2786）。"""
        self._read_json()
        snap = self._store().snapshot()
        orig = snap["char_orig01"]
        if orig is None:
            raise ApiError(400, "还没有上传过立绘，没东西可还原",
                           "先上传一张立绘")
        self._store().set_char(orig, snap["char_name"])
        self._send_json(200, {"ok": True,
                              "image": _data_uri(encode_png(orig)),
                              "width": int(orig.shape[1]),
                              "height": int(orig.shape[0])})

    def _api_matte(self) -> None:
        data = self._read_json()
        source = data.get("source", "char")
        if source not in ("char", "photo"):
            raise ApiError(400, f"source 取值 {source!r} 不在可选列表",
                           "source 可选：photo（抠照片里的人）、char（重新抠当前立绘）")
        snap = self._store().snapshot()
        if source == "photo":
            src01, what = snap["photo01"], "照片"
        else:
            src01, what = snap["char01"], "立绘"
        if src01 is None:
            raise ApiError(400, f"还没有上传{what}，没东西可抠",
                           f"先上传{what}（POST /api/upload）再点「抠图」")
        started = time.perf_counter()
        with self.server.render_lock:                # type: ignore[attr-defined]
            try:
                result = anime_cut(src01)
            except ImportError as exc:
                raise ApiError(
                    400, f"{exc}（AI 抠图运行时未安装）",
                    "装一下再试：.venv/bin/python -m pip install onnxruntime"
                    "（或 pip install -e \".[matting]\"）") from None
            except FileNotFoundError as exc:
                # _resolve_model_path 的报错自带 HuggingFace 下载指引，原样带出去
                raise ApiError(400, str(exc),
                               "把 isnet-anime-q8.onnx 放到仓库 anime/ 或 ~/.cache/arcam/，"
                               "或设 $ARCAM_ISNET_MODEL 指向模型文件") from None
            except ValueError as exc:
                raise ApiError(400, f"抠图失败：{exc}",
                               "换一张主体更明确、前景占比更大的图再试") from None
        ms = (time.perf_counter() - started) * 1000.0
        self._store().set_matte(_to_f01(result.rgba))
        self._send_json(200, {
            "ok": True,
            "image": _data_uri(encode_png(result.rgba)),
            "fg": round(float(result.fg), 4),
            "ms": round(ms, 3),
        })

    def _api_matte_as_char(self) -> None:
        self._read_json()                          # 先把请求体读干净（keep-alive 卫生：
        # 不读的话残体会拼到下一条请求行上，把连接搅坏 —— HTTP/1.1 的隐藏契约）
        matte01 = self._store().snapshot()["matte01"]
        if matte01 is None:
            raise ApiError(400, "还没有抠图结果可以转成立绘",
                           "先 POST /api/matte 抠一次，再把结果设成当前立绘")
        self._store().set_char(matte01, MATTE_NAME)
        self._send_json(200, {"ok": True, "name": MATTE_NAME,
                              "width": int(matte01.shape[1]),
                              "height": int(matte01.shape[0])})

    def _api_lidar_inspect(self) -> None:
        body = self._read_body()
        fields = parse_multipart(body, self.headers.get("Content-Type", ""))
        if "depth" not in fields:
            raise ApiError(400, "LiDAR 诊断请求里没有 depth 字段",
                           "字段名固定为 depth（16-bit PNG/TIF、.npy 或 .depth）")
        data, filename = fields["depth"]
        started = time.perf_counter()
        with self.server.render_lock:                # type: ignore[attr-defined]
            frame = _decode_depth_upload(data, _safe_name(filename, "depth.npy"))
            env = _env_of(frame)
            coverage = float(np.isfinite(np.asarray(frame.depth_m, dtype=np.float64)).mean())
            plane = env.plane                        # type: ignore[attr-defined]
        ms = (time.perf_counter() - started) * 1000.0
        # 帧常驻内存：之后 /api/render 可用 params.depth_uploaded=true 走深度驱动；
        # 顺手把刚拟出的 env 塞进缓存，第一遍预览渲染不用重拟合
        self._store().set_depth(frame, frame.source, env=env)
        self._send_json(200, {
            "ok": True,
            "env": {"height_m": float(env.height_m),           # type: ignore[attr-defined]
                    "pitch_deg": float(env.pitch_deg),         # type: ignore[attr-defined]
                    "roll_deg": float(env.roll_deg),           # type: ignore[attr-defined]
                    "coverage": round(coverage, 4),
                    "inliers": int(plane.inliers),
                    "rms": float(plane.rms)},
            "frame": {"width": int(frame.depth_m.shape[1]),
                      "height": int(frame.depth_m.shape[0])},
            "source": frame.source,
            "ms": round(ms, 3),
        })


def _safe_name(filename: str | None, fallback: str) -> str:
    """用户上传文件名 → 安全基名（scene.json 的 character.name 用它）。

    只取 basename、拒空/拒路径穿越；非 ASCII（中文名）原样保留（JSON
    ``ensure_ascii=False``，与原版一致）。畸形名回落到 fallback。
    """
    if not filename:
        return fallback
    base = Path(filename.replace("\\", "/")).name.strip()
    if not base or base in (".", "..") or "\x00" in base:
        return fallback
    return base


def _zip_bytes(parts) -> bytes:
    """三件套 → zip 字节（时间戳写死 1980-01-01，同 CLI bundle 确定性口径）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in parts:
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    return buffer.getvalue()
