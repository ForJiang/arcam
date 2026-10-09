"""arcam.lidar_live —— iPhone LiDAR 深度实时流的 TCP 接收与服务循环。

协议溯源：docs/LIDAR-SPEC §6（冻结）。坐标/单位约定见 docs/LIDAR-SPEC §1：
深度为**米**（float32，小端，行优先 (H, W)，无效 = 0 或 NaN）；深度相机坐标
（光学中心，y 向下、z 前向）下 ``p = ((u-cx)/fx*z, (v-cy)/fy*z, z)``，即
depth 值 = 该像素后投影点的 z 分量（与 arcam.plane.backproject 约定一致）。

wire format（每帧，无帧边界 magic，靠两个长度前缀定界）::

    [u32 BE meta_len][JSON meta UTF-8][u32 BE len][depth float32 LE, H*W*4 字节]

- ``meta_len`` 必须落在 (0, 64KiB]；``len`` 必须 == W*H*4。
- ``meta`` 至少含 ``W,H,fx,fy,cx,cy,roll_deg,pitch_deg,height_m,timestamp``；
  其中 ``roll_deg/pitch_deg/height_m`` 可缺省或为 ``null``（= 未知，对应
  LIDAR-SPEC §2 LiDARFrame 的 ``None``）。
- 落盘格式（out_dir）：``arcam.depth.save_depth`` 的 ``*.depth``（LIDAR-SPEC §2.3，
  magic ``ARCD``）。若 ``arcam.depth`` 不可导入（裁剪安装等），本模块以内置的、
  严格按 §2.3 字节布局的写盘函数兜底（``_write_depth_spec``），保证流式落盘可被
  后续 ``load_depth`` 读回。

连接模型：单客户端、顺序帧。一个客户端 EOF / 坏帧后服务端关闭该连接并继续
accept 下一个客户端（坏帧不挂服务线程）；``max_frames`` 达量后整体退出。
"""

from __future__ import annotations

import contextlib
import json
import math
import socket
import struct
from collections.abc import Iterator
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# 协议常量（冻结，docs/LIDAR-SPEC §6）
# ---------------------------------------------------------------------------

_META_LEN = struct.Struct('>I')
_DEPTH_LEN = struct.Struct('>I')

_MAX_META_BYTES = 1 << 16              # meta JSON 上限（宽松，防恶意长度）
_DEPTH_DTYPE = np.dtype('<f4')         # 小端 float32，与 .depth 文件体一致

#: meta 必须给出（非 null）的键：尺寸 + 针孔内参。
#: （roll_deg/pitch_deg/height_m 可缺省或为 null = 未知，见模块 docstring）
REQUIRED_META_KEYS: tuple[str, ...] = ('W', 'H', 'fx', 'fy', 'cx', 'cy')

#: LIDAR-SPEC §2.3 ``.depth`` 文件 magic。
DEPTH_MAGIC = b'ARCD'


class _StreamEOF(ValueError):
    """连接干净关闭（对端 shutdown/close）——仍是 ValueError（对外契约），
    服务端据此判定「这个客户端收完了」，继续 accept 下一个。"""


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """阻塞收满 ``n`` 字节；EOF（对端关闭）→ ``_StreamEOF``（ValueError 子类）。"""
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise _StreamEOF(f'unexpected EOF: got {n - remaining}/{n} bytes')
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def _require_number(meta: dict, key: str) -> float:
    value = meta.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f'meta[{key!r}] must be a number, got {value!r}')
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f'meta[{key!r}] must be finite, got {value!r}')
    return number


def recv_frame(sock: socket.socket) -> tuple[dict, np.ndarray]:
    """阻塞读一帧 → ``(meta, depth)``。

    异常：EOF / meta 长度非法 / JSON 坏 / 必需键缺失 / ``len != W*H*4`` 均抛
    ``ValueError``（EOF 是内部 ``_StreamEOF`` 子类，仍是 ValueError）。
    ``depth`` 是新鲜的 (H, W) 小端 float32 米数组（无效值保留 0/NaN 原样，
    统一 NaN 化由 arcam.depth.load_depth 负责，见 LIDAR-SPEC §1/§2）。
    """
    (meta_len,) = _META_LEN.unpack(_recv_exact(sock, _META_LEN.size))
    if not 0 < meta_len <= _MAX_META_BYTES:
        raise ValueError(f'bad meta length: {meta_len}')
    meta_bytes = _recv_exact(sock, meta_len)
    try:
        meta = json.loads(meta_bytes.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'bad meta JSON: {exc}') from exc
    if not isinstance(meta, dict):
        raise ValueError(f'meta must be a JSON object, got {type(meta).__name__}')
    for key in REQUIRED_META_KEYS:
        if key not in meta:
            raise ValueError(f'meta missing required key {key!r}')
    width, height = meta['W'], meta['H']
    if (not isinstance(width, int) or isinstance(width, bool)
            or not isinstance(height, int) or isinstance(height, bool)
            or width <= 0 or height <= 0):
        raise ValueError(f'bad W/H in meta: {width!r} x {height!r}')
    for key in ('fx', 'fy', 'cx', 'cy'):
        _require_number(meta, key)

    (body_len,) = _DEPTH_LEN.unpack(_recv_exact(sock, _DEPTH_LEN.size))
    expected = width * height * _DEPTH_DTYPE.itemsize
    if body_len != expected:
        raise ValueError(f'bad depth length: {body_len} != W*H*4 ({expected})')
    payload = _recv_exact(sock, body_len)
    depth = np.frombuffer(payload, dtype=_DEPTH_DTYPE).reshape(height, width).copy()
    return meta, depth


def send_frame(sock: socket.socket, meta: dict, depth: np.ndarray) -> None:
    """协议写侧：``[u32 BE meta_len][JSON][u32 BE len][depth f32 LE]``。

    ``W/H`` 一律由 ``depth.shape`` 覆写（防止 meta 与数据不一致）；
    ``fx,fy,cx,cy`` 必须已在 ``meta`` 中给出。单次 ``sendall``。
    """
    if depth.ndim != 2:
        raise ValueError(f'depth must be (H, W), got shape {depth.shape}')
    height, width = depth.shape
    for key in ('fx', 'fy', 'cx', 'cy'):
        _require_number(meta, key)
    payload = np.ascontiguousarray(depth, dtype=_DEPTH_DTYPE).tobytes()
    if len(payload) != width * height * _DEPTH_DTYPE.itemsize:
        raise ValueError('depth payload size mismatch')  # 防御：不可能分支
    frame_meta = dict(meta)
    frame_meta['W'] = int(width)
    frame_meta['H'] = int(height)
    meta_bytes = json.dumps(frame_meta, allow_nan=True).encode('utf-8')
    if len(meta_bytes) > _MAX_META_BYTES:
        raise ValueError(f'meta too large: {len(meta_bytes)} bytes')
    sock.sendall(
        _META_LEN.pack(len(meta_bytes))
        + meta_bytes
        + _DEPTH_LEN.pack(len(payload))
        + payload
    )


# ---------------------------------------------------------------------------
# 落盘：优先 arcam.depth.save_depth；不可用时按 spec §2.3 字节布局兜底
# ---------------------------------------------------------------------------

try:  # arcam.depth（.depth 格式的权威实现）；缺失时退回下方按 spec §2.3 的写盘
    from arcam import depth as _arcam_depth  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - 仅当 arcam.depth 不可导入（裁剪安装）时
    _arcam_depth = None


def _write_depth_spec(path: str | Path, meta: dict, depth: np.ndarray) -> None:
    """严格按 docs/LIDAR-SPEC §2.3 写 ``.depth``（magic ``ARCD``，小头，40 B 头）::

        [magic 4B][W u32][H u32][fx f32][fy f32][cx f32][cy f32]
        [roll f32(NaN=无)][pitch f32][height f32][depth f32 H*W 米]
    """
    height, width = depth.shape
    roll = float(meta['roll_deg']) if meta.get('roll_deg') is not None else float('nan')
    pitch = float(meta['pitch_deg']) if meta.get('pitch_deg') is not None else float('nan')
    height_m = float(meta['height_m']) if meta.get('height_m') is not None else float('nan')
    header = (
        DEPTH_MAGIC
        + struct.pack('<II', int(width), int(height))
        + struct.pack('<fffffff', float(meta['fx']), float(meta['fy']),
                      float(meta['cx']), float(meta['cy']), roll, pitch, height_m)
    )
    body = np.ascontiguousarray(depth, dtype=_DEPTH_DTYPE).tobytes()
    with open(path, 'wb') as fh:
        fh.write(header)
        fh.write(body)


def save_depth_file(path: str | Path, meta: dict, depth: np.ndarray) -> None:
    """把一帧写成 ``.depth``。``arcam.depth.save_depth``（LIDAR-SPEC §2 的权威
    实现，签名冻结为 ``(frame, path)``）可用时优先走它，否则用内置 §2.3 写盘。"""
    if _arcam_depth is not None and hasattr(_arcam_depth, 'save_depth'):
        frame_cls = getattr(_arcam_depth, 'LiDARFrame', None)
        if frame_cls is not None:
            height, width = depth.shape
            frame = frame_cls(
                depth_m=np.ascontiguousarray(depth, dtype=np.float32),
                fx=float(meta['fx']), fy=float(meta['fy']),
                cx=float(meta['cx']), cy=float(meta['cy']),
                roll_deg=(None if meta.get('roll_deg') is None else float(meta['roll_deg'])),
                pitch_deg=(None if meta.get('pitch_deg') is None else float(meta['pitch_deg'])),
                height_m=(None if meta.get('height_m') is None else float(meta['height_m'])),
                source='lidar_live',
            )
            _arcam_depth.save_depth(frame, str(path))
            return
    _write_depth_spec(path, meta, depth)


# ---------------------------------------------------------------------------
# 服务循环
# ---------------------------------------------------------------------------

def serve(
    port: int,
    host: str = '127.0.0.1',
    out_dir: str | None = None,
    max_frames: int | None = None,
) -> Iterator[tuple[dict, np.ndarray]]:
    """实时流服务：监听 ``(host, port)``，逐帧 yield ``(meta, depth)``。

    - 单客户端顺序帧；客户端 EOF / 坏帧后关闭该连接并 accept 下一个，
      **坏帧不挂服务线程**（recv_frame 的 ValueError 在此被吞掉并换连接）。
    - ``out_dir`` 非 None 时用（优先 L1 的）``save_depth`` 落盘
      ``frame_%06d.depth``（序号跨客户端连续累计）。
    - ``max_frames`` 非 None 时收满即整体退出。
    - bind + listen 在**调用时**立即完成（非首次迭代时），返回的是惰性
      generator：客户端可以先连上再开始读。关闭语义：generator 被
      close()/GC 或迭代结束时监听 socket 经 contextlib.closing 干净关闭；
      无人迭代时监听 socket 随 generator 回收关闭。
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    # backlog 8（不是 socketserver 那种默认 5、更不是 1）：协议是单客户端顺序帧，
    # 但客户端重连/探测连接常常和上一个连接的关闭竞态，backlog=1 时新连接直接被
    # 拒（ConnectionRefused）。8 个待接连接对单客户端协议绰绰有余。
    listener.listen(8)
    out_path = Path(out_dir) if out_dir is not None else None
    if out_path is not None:
        out_path.mkdir(parents=True, exist_ok=True)
    saved = 0

    def _frames() -> Iterator[tuple[dict, np.ndarray]]:
        nonlocal saved
        with contextlib.closing(listener):
            while max_frames is None or saved < max_frames:
                try:
                    conn, _addr = listener.accept()
                except OSError:
                    return
                with contextlib.closing(conn):
                    while True:
                        if max_frames is not None and saved >= max_frames:
                            return
                        try:
                            meta, depth = recv_frame(conn)
                        except _StreamEOF:
                            break                    # 干净 EOF：等下一个客户端
                        except ValueError:
                            break                    # 坏帧：丢弃该连接，不挂线程
                        if out_path is not None:
                            save_depth_file(out_path / f'frame_{saved:06d}.depth', meta, depth)
                        saved += 1
                        yield meta, depth

    return _frames()
