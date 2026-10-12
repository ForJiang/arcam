"""tests/test_lidar_live.py —— 实时流协议（docs/LIDAR-SPEC §6）单测。

全部走 127.0.0.1 回环；不依赖真实设备 / iOS App。
覆盖：round-trip（bit 级）、save_depth 落盘、坏帧/短帧不挂线程、EOF 干净退出。
"""

from __future__ import annotations

import contextlib
import json
import socket
import struct
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from arcam import lidar_sim
from arcam.lidar_live import recv_frame, send_frame, serve

try:  # arcam.depth（落盘 .depth 的权威格式）
    from arcam import depth as arcam_depth
except Exception:  # pragma: no cover - arcam.depth 不可用时走字节数/magic 断言
    arcam_depth = None

HOST = '127.0.0.1'


def _free_port() -> int:
    """抢一个空闲回环端口（bind 后立刻释放，供 serve 复用的经典竞态可忽略）。"""
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _collect_async(port: int, out_dir: str | None = None,
                   max_frames: int | None = None) -> list[tuple[dict, np.ndarray]]:
    """后台 daemon 线程迭代 serve()，收到的帧 append 进列表并返回该列表。

    serve() 必须在**调用线程**里调用：它的 bind+listen 发生在调用时
    （yield 在内层 _frames()，外层是普通函数——lidar_live.py:219 的契约），
    在后台线程里才调用就得和「立即连接」赛跑，偶发 ConnectionRefused。
    这里调用线程建好 generator（监听已就绪），后台线程只负责迭代。
    """
    received: list[tuple[dict, np.ndarray]] = []
    frames = serve(port, HOST, out_dir, max_frames)     # 调用时即 bind+listen

    def _loop() -> None:
        for frame in frames:
            received.append(frame)

    threading.Thread(target=_loop, name='serve-loop', daemon=True).start()
    return received


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _good_meta(index: int = 0) -> dict:
    meta, _ = lidar_sim.synth_ground_frame(h=1.55, pitch_deg=12.0, roll_deg=8.0, index=index)
    return meta


# ---------------------------------------------------------------------------
# round-trip：sim 客户端 → serve 收 frames 帧，meta/depth 与发送端 bit 级一致
# ---------------------------------------------------------------------------

def test_round_trip_sim_client_to_serve():
    """simulate_scene_server（设备角色）连 serve，收 frames 帧后关闭；
    收到的 meta 与 depth 与 synth_ground_frame 产出逐位一致（float32 bit 级）。"""
    port = _free_port()
    received = _collect_async(port, max_frames=3)
    sim = lidar_sim.simulate_scene_server(port, h=1.55, pitch=12.0, roll=8.0, frames=3, fps=50)
    sim.join(timeout=10.0)
    assert not sim.is_alive(), 'sim 线程应已退出'
    assert _wait_for(lambda: len(received) == 3), '应收到 3 帧'
    assert len(received) == 3

    for i, (meta, depth) in enumerate(received):
        exp_meta, exp_depth = lidar_sim.synth_ground_frame(
            h=1.55, pitch_deg=12.0, roll_deg=8.0, index=i)
        assert (meta['W'], meta['H']) == (exp_meta['W'], exp_meta['H'])
        assert (depth.shape, depth.dtype.str) == (exp_depth.shape, '<f4')
        # JSON 往返对 float 是无损的（repr 最短往返）
        for key in ('fx', 'fy', 'cx', 'cy', 'roll_deg', 'pitch_deg', 'height_m'):
            assert meta[key] == pytest.approx(exp_meta[key]), key
        assert isinstance(meta['timestamp'], (int, float))
        assert np.isnan(exp_depth).any(), '合成帧应含 NaN 无效区'
        # bit 级一致（含 NaN 位型）：同一函数确定性复现 → 位型完全相同
        assert np.array_equal(depth.view(np.uint32), exp_depth.view(np.uint32))
        assert np.array_equal(depth, exp_depth, equal_nan=True)


def test_round_trip_plain_client_via_send_frame():
    """send_frame 写侧 + serve 读侧 round-trip：meta 数值与 depth bit 级一致。"""
    port = _free_port()
    received = _collect_async(port, max_frames=2)
    exp_frames = [lidar_sim.synth_ground_frame(h=1.6, index=i) for i in range(2)]
    with contextlib.closing(socket.create_connection((HOST, port), timeout=5.0)) as sock:
        for meta, depth in exp_frames:
            send_frame(sock, meta, depth)
    assert _wait_for(lambda: len(received) == 2)
    for (meta, depth), (exp_meta, exp_depth) in zip(received, exp_frames):
        assert meta['W'] == exp_meta['W'] and meta['H'] == exp_meta['H']
        assert np.array_equal(depth.view(np.uint32), exp_depth.view(np.uint32))


def test_meta_w_h_overridden_by_send_frame():
    """send_frame 用 depth.shape 覆写 meta 的 W/H，防止声明与数据不一致。"""
    port = _free_port()
    received = _collect_async(port, max_frames=1)
    meta, depth = lidar_sim.synth_ground_frame(index=0)
    meta = dict(meta, W=999, H=999)
    with contextlib.closing(socket.create_connection((HOST, port), timeout=5.0)) as sock:
        send_frame(sock, meta, depth)
    assert _wait_for(lambda: len(received) == 1)
    got_meta, got_depth = received[0]
    assert (got_meta['W'], got_meta['H']) == (256, 192)
    assert got_depth.shape == (192, 256)


# ---------------------------------------------------------------------------
# 落盘：save_depth → load_depth 读回（L1 未落地则断言字节数与 magic）
# ---------------------------------------------------------------------------

def test_write_depth_spec_matches_authoritative_save_depth(tmp_path: Path):
    """裁剪安装的兜底写盘与权威 ``arcam.depth.save_depth`` **逐字节同口径**。

    回归：兜底旧实现把无效值原样写 NaN 位型，而 ARCD 的无效标记是 **0**
    （save_depth 的口径）——第三方读者按「0=无效」解析时 NaN 字节会被当成
    离群深度。读回时两者都归一成 NaN，故修正后往返语义无损。
    """
    from arcam.lidar_live import _write_depth_spec

    meta, depth = lidar_sim.synth_ground_frame(h=1.55, index=0)
    d = np.array(depth, dtype=np.float32)
    d[0, 0] = np.nan                     # 无效三态：NaN / 0 / 负
    d[0, 1] = 0.0
    d[0, 2] = -1.0
    meta = dict(meta, roll_deg=None, pitch_deg=None, height_m=None)

    _write_depth_spec(tmp_path / "spec.depth", meta, d)
    frame = arcam_depth.LiDARFrame(depth_m=d, fx=meta["fx"], fy=meta["fy"],
                                   cx=meta["cx"], cy=meta["cy"])
    arcam_depth.save_depth(frame, tmp_path / "auth.depth")
    assert (tmp_path / "spec.depth").read_bytes() == (tmp_path / "auth.depth").read_bytes()

    back = arcam_depth.load_depth(tmp_path / "spec.depth")
    assert np.isnan(back.depth_m[0, 0]) and np.isnan(back.depth_m[0, 1])
    assert np.isnan(back.depth_m[0, 2])


def test_serve_out_dir_writes_depth_files(tmp_path: Path):
    """out_dir 非 None 时落盘 frame_%06d.depth；可被 arcam.depth.load_depth
    读回且 depth/intrinsics 一致（arcam.depth 不可用时按 spec §2.3 断言字节数与 magic）。"""
    port = _free_port()
    sent = [lidar_sim.synth_ground_frame(h=1.55, index=i) for i in range(2)]
    _collect_async(port, out_dir=str(tmp_path), max_frames=len(sent))
    with contextlib.closing(socket.create_connection((HOST, port), timeout=5.0)) as sock:
        for meta, depth in sent:
            send_frame(sock, meta, depth)
    assert _wait_for(lambda: len(sorted(tmp_path.glob('frame_*.depth'))) == len(sent))

    files = sorted(tmp_path.glob('frame_*.depth'))
    assert [f.name for f in files] == ['frame_000000.depth', 'frame_000001.depth']
    width, height = sent[0][0]['W'], sent[0][0]['H']
    # spec §2.3：40 B 头（magic/W/H/fx,fy,cx,cy,roll,pitch,height）+ 4WH B 深度体
    header_size = (arcam_depth.ARCD_HEADER_SIZE if arcam_depth is not None else 40)
    expected_bytes = header_size + 4 * width * height
    for f, (exp_meta, exp_depth) in zip(files, sent):
        raw = f.read_bytes()
        assert raw[:4] == b'ARCD'
        assert len(raw) == expected_bytes
        if arcam_depth is None:
            continue
        frame = arcam_depth.load_depth(f)
        assert (frame.fx, frame.fy, frame.cx, frame.cy) == pytest.approx(
            (exp_meta['fx'], exp_meta['fy'], exp_meta['cx'], exp_meta['cy']))
        assert np.array_equal(frame.depth_m, exp_depth, equal_nan=True)


# ---------------------------------------------------------------------------
# 协议健壮性：短帧/坏长度 → ValueError 不挂线程；EOF 干净退出
# ---------------------------------------------------------------------------

def test_recv_frame_truncated_meta_raises():
    """声明 meta_len=500 但只发 2 字节就 EOF → ValueError（不挂起）。"""
    a, b = socket.socketpair()
    with contextlib.closing(a), contextlib.closing(b):
        a.settimeout(5.0)
        b.sendall(struct.pack('>I', 500) + b'{}')
        b.shutdown(socket.SHUT_WR)
        with pytest.raises(ValueError):
            recv_frame(a)


def test_recv_frame_bad_length_mismatch_raises():
    """len 字段 != W*H*4 → 立刻 ValueError（不等 payload）。"""
    a, b = socket.socketpair()
    with contextlib.closing(a), contextlib.closing(b):
        a.settimeout(5.0)
        meta = _good_meta(0)
        meta_bytes = json.dumps(meta).encode('utf-8')
        b.sendall(struct.pack('>I', len(meta_bytes)) + meta_bytes + struct.pack('>I', 999))
        with pytest.raises(ValueError, match='bad depth length'):
            recv_frame(a)


def test_recv_frame_bad_meta_json_raises():
    """meta 不是合法 JSON → ValueError。"""
    a, b = socket.socketpair()
    with contextlib.closing(a), contextlib.closing(b):
        a.settimeout(5.0)
        junk = b'not json at all'
        b.sendall(struct.pack('>I', len(junk)) + junk)
        with pytest.raises(ValueError):
            recv_frame(a)


def test_recv_frame_missing_required_key_raises():
    """meta 缺 fx → ValueError。"""
    a, b = socket.socketpair()
    with contextlib.closing(a), contextlib.closing(b):
        a.settimeout(5.0)
        meta = _good_meta(0)
        del meta['fx']
        meta_bytes = json.dumps(meta).encode('utf-8')
        b.sendall(struct.pack('>I', len(meta_bytes)) + meta_bytes)
        with pytest.raises(ValueError, match='fx'):
            recv_frame(a)


def test_recv_frame_rejects_bool_w_h():
    """W/H 传布尔（JSON true）→ ValueError（bool 是 Python int 子类，须显式拒）。"""
    a, b = socket.socketpair()
    with contextlib.closing(a), contextlib.closing(b):
        a.settimeout(5.0)
        meta = _good_meta(0)
        meta['W'] = True
        meta_bytes = json.dumps(meta).encode('utf-8')
        b.sendall(struct.pack('>I', len(meta_bytes)) + meta_bytes)
        with pytest.raises(ValueError, match='bad W/H'):
            recv_frame(a)


def test_recv_frame_rejects_nonfinite_intrinsics():
    """内参 NaN/Inf → ValueError（Python json 默认放行 NaN 字面量，协议层须拒）。"""
    a, b = socket.socketpair()
    with contextlib.closing(a), contextlib.closing(b):
        a.settimeout(5.0)
        for key, bad in (('fx', float('nan')), ('fy', float('inf'))):
            meta = dict(_good_meta(0))
            meta[key] = bad
            meta_bytes = json.dumps(meta).encode('utf-8')   # allow_nan 默认 True
            b.sendall(struct.pack('>I', len(meta_bytes)) + meta_bytes)
            with pytest.raises(ValueError, match=f'meta\\[{key!r}\\]'):
                recv_frame(a)


def test_send_frame_rejects_nonfinite_intrinsics():
    """写侧同样拒非有限内参（垃圾不外发）。"""
    meta, depth = lidar_sim.synth_ground_frame(index=0)
    with pytest.raises(ValueError, match='fx'):
        send_frame(_NullSocket(), dict(meta, fx=float('nan')), depth)


class _NullSocket:
    """send_frame 在校验阶段就抛，不会真发包；占位满足类型。"""

    def sendall(self, _data: bytes) -> None:               # pragma: no cover
        raise AssertionError('不应到达发包步骤')


def test_recv_frame_eof_mid_payload_raises():
    """帧头发全、payload 中途 EOF → ValueError。"""
    a, b = socket.socketpair()
    with contextlib.closing(a), contextlib.closing(b):
        a.settimeout(5.0)
        meta_bytes = json.dumps(_good_meta(0)).encode('utf-8')
        b.sendall(struct.pack('>I', len(meta_bytes)) + meta_bytes
                  + struct.pack('>I', 192 * 256 * 4) + b'\x00' * 64)
        b.shutdown(socket.SHUT_WR)
        with pytest.raises(ValueError):
            recv_frame(a)


def test_serve_survives_bad_frame_then_serves_next_client():
    """客户端发坏长度帧后断开 → 服务线程不挂，下一个客户端的正常帧仍被收到。"""
    port = _free_port()
    received = _collect_async(port, max_frames=1)

    with contextlib.closing(socket.create_connection((HOST, port), timeout=5.0)) as bad:
        meta_bytes = json.dumps(_good_meta(0)).encode('utf-8')
        bad.sendall(struct.pack('>I', len(meta_bytes)) + meta_bytes
                    + struct.pack('>I', 12345))  # 坏长度
    time.sleep(0.1)  # 让 serve 断开坏连接、回到 accept

    exp_meta, exp_depth = lidar_sim.synth_ground_frame(h=1.55, index=0)
    with contextlib.closing(socket.create_connection((HOST, port), timeout=5.0)) as good:
        send_frame(good, exp_meta, exp_depth)
    assert _wait_for(lambda: len(received) == 1)
    got_meta, got_depth = received[0]
    assert np.array_equal(got_depth.view(np.uint32), exp_depth.view(np.uint32))
    assert got_meta['height_m'] == pytest.approx(exp_meta['height_m'])


def test_serve_eof_clean_exit():
    """客户端发 1 帧后干净关闭 → serve 恰好 yield 1 帧后结束（max_frames 收尾）。"""
    port = _free_port()
    received = _collect_async(port, max_frames=1)
    with contextlib.closing(socket.create_connection((HOST, port), timeout=5.0)) as sock:
        meta, depth = lidar_sim.synth_ground_frame(h=1.6, index=7)
        send_frame(sock, meta, depth)
        sock.shutdown(socket.SHUT_WR)
        # 客户端半关闭后服务端读到 EOF，accept 下一个；max_frames=1 已达成 → 干净退出
    assert _wait_for(lambda: len(received) == 1)
    assert len(received) == 1


def test_sim_thread_is_daemon_and_sends_exact_frames():
    """sim 推送帧数可数（frames=2，fps 拉高）：serve 收到恰好 2 帧后监听关闭。"""
    port = _free_port()
    received = _collect_async(port, max_frames=2)
    sim = lidar_sim.simulate_scene_server(port, frames=2, fps=1000)
    assert sim.daemon is True
    sim.join(timeout=10.0)
    assert not sim.is_alive()
    assert _wait_for(lambda: len(received) == 2)
    assert len(received) == 2


# ---------------------------------------------------------------------------
# W3 健壮性增补（覆盖缺口：协议守卫 / 落盘兜底 / accept 失败 / 空客户端）
# ---------------------------------------------------------------------------

def test_recv_frame_zero_meta_length_raises():
    """meta_len=0：长度前缀必须落在 (0, 64KiB]——0 字节 meta 不是合法帧。"""
    a, b = socket.socketpair()
    with contextlib.closing(a), contextlib.closing(b):
        a.settimeout(5.0)
        b.sendall(struct.pack('>I', 0))
        with pytest.raises(ValueError, match='bad meta length'):
            recv_frame(a)


def test_recv_frame_oversize_meta_length_raises():
    """meta_len 超 64KiB 上限：在读 meta 之前就拒（不为垃圾长度吞内存）。"""
    a, b = socket.socketpair()
    with contextlib.closing(a), contextlib.closing(b):
        a.settimeout(5.0)
        b.sendall(struct.pack('>I', (1 << 16) + 1))
        with pytest.raises(ValueError, match='bad meta length'):
            recv_frame(a)


def test_recv_frame_meta_not_object_raises():
    """meta 是 JSON 数组/标量 → ValueError（协议要求 JSON object）。"""
    a, b = socket.socketpair()
    with contextlib.closing(a), contextlib.closing(b):
        a.settimeout(5.0)
        junk = b'[1, 2, 3]'
        b.sendall(struct.pack('>I', len(junk)) + junk)
        with pytest.raises(ValueError, match='must be a JSON object'):
            recv_frame(a)


def test_recv_frame_non_number_intrinsic_raises():
    """内参给字符串 → ValueError（_require_number：bool/str 都拒）。"""
    a, b = socket.socketpair()
    with contextlib.closing(a), contextlib.closing(b):
        a.settimeout(5.0)
        meta = dict(_good_meta(0), fx='1.0')
        meta_bytes = json.dumps(meta).encode('utf-8')
        b.sendall(struct.pack('>I', len(meta_bytes)) + meta_bytes)
        with pytest.raises(ValueError, match=r"meta\['fx'\]"):
            recv_frame(a)


def test_send_frame_rejects_non_2d_depth():
    """写侧同样只发 (H,W)：三维数组直接 ValueError（不发畸形帧）。"""
    meta, _ = lidar_sim.synth_ground_frame(index=0)
    with pytest.raises(ValueError, match=r'\(H, W\)'):
        send_frame(_NullSocket(), meta, np.zeros((4, 4, 3), np.float32))


def test_send_frame_rejects_oversize_meta():
    """meta JSON 超 64KiB → ValueError（与读侧上限同口径，垃圾不外发）。"""
    meta, depth = lidar_sim.synth_ground_frame(index=0)
    huge = dict(meta, junk='x' * (1 << 16))
    with pytest.raises(ValueError, match='meta too large'):
        send_frame(_NullSocket(), huge, depth)


def test_save_depth_file_falls_back_to_spec_writer(tmp_path: Path):
    """arcam.depth 不可导入（裁剪安装）时走内置 §2.3 写盘，读回仍自洽。

    兜底路径与权威实现的逐字节同口径由
    ``test_write_depth_spec_matches_authoritative_save_depth`` 钉死；这里验
    的是**选择逻辑**：``_arcam_depth=None`` 时必须真的落到兜底写盘函数。
    """
    from arcam import lidar_live

    meta, depth = lidar_sim.synth_ground_frame(h=1.55, index=0)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(lidar_live, '_arcam_depth', None)
    try:
        out = tmp_path / 'fallback.depth'
        lidar_live.save_depth_file(out, meta, depth)
    finally:
        monkey.undo()
    frame = arcam_depth.load_depth(out)
    assert frame.depth_m.shape == depth.shape
    assert np.array_equal(frame.depth_m, depth, equal_nan=True)
    assert (frame.fx, frame.cx) == pytest.approx((meta['fx'], meta['cx']))


class _FakeListener:
    """accept 立刻抛 OSError 的假监听 socket（验 serve 的 accept 失败退出）。"""

    def __init__(self):
        self.closed = False

    def setsockopt(self, *_a):
        pass

    def bind(self, _addr):
        pass

    def listen(self, _n):
        pass

    def accept(self):
        raise OSError('listener closed')

    def close(self):
        self.closed = True


def test_serve_accept_oserror_exits_cleanly(monkeypatch):
    """监听 socket 被关（accept 抛 OSError）→ serve 干净结束，不挂线程。"""
    from arcam import lidar_live

    monkeypatch.setattr(lidar_live.socket, 'socket', lambda *a, **k: _FakeListener())
    frames = lidar_live.serve(0)
    assert list(frames) == []


def test_serve_eof_without_frames_serves_next_client():
    """客户端连上就断（一帧不发）→ EOF 只断这个连接，下一个客户端照收。"""
    port = _free_port()
    received = _collect_async(port, max_frames=1)
    with contextlib.closing(socket.create_connection((HOST, port), timeout=5.0)):
        pass                                   # 干净 EOF，零帧
    time.sleep(0.2)                            # 让 serve 回到 accept

    exp_meta, exp_depth = lidar_sim.synth_ground_frame(h=1.55, index=0)
    with contextlib.closing(socket.create_connection((HOST, port), timeout=5.0)) as sock:
        send_frame(sock, exp_meta, exp_depth)
    assert _wait_for(lambda: len(received) == 1)
    assert np.array_equal(received[0][1].view(np.uint32), exp_depth.view(np.uint32))


def test_serve_out_dir_frames_are_numbered_across_clients(tmp_path: Path):
    """序号跨客户端连续累计：两个客户端各推一帧 → frame_000000/000001。"""
    port = _free_port()
    received = _collect_async(port, out_dir=str(tmp_path), max_frames=2)
    for i in range(2):
        meta, depth = lidar_sim.synth_ground_frame(h=1.55, index=i)
        with contextlib.closing(socket.create_connection((HOST, port), timeout=5.0)) as sock:
            send_frame(sock, meta, depth)
        assert _wait_for(lambda: len(received) == i + 1), i
    files = sorted(p.name for p in tmp_path.glob('frame_*.depth'))
    assert files == ['frame_000000.depth', 'frame_000001.depth']
