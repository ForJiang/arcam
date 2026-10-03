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

try:  # L1 的 arcam.depth（落盘 .depth 的权威格式）
    from arcam import depth as arcam_depth
except Exception:  # pragma: no cover - L1 未就绪
    arcam_depth = None

HOST = '127.0.0.1'


def _free_port() -> int:
    """抢一个空闲回环端口（bind 后立刻释放，供 serve 复用的经典竞态可忽略）。"""
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _collect_async(port: int, out_dir: str | None = None,
                   max_frames: int | None = None) -> list[tuple[dict, np.ndarray]]:
    """后台 daemon 线程跑 serve()，收到的帧 append 进列表并返回该列表。
    serve() 在调用时即 bind+listen，无需 sleep 等就绪。"""
    received: list[tuple[dict, np.ndarray]] = []

    def _loop() -> None:
        for frame in serve(port, HOST, out_dir, max_frames):
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

def test_serve_out_dir_writes_depth_files(tmp_path: Path):
    """out_dir 非 None 时落盘 frame_%06d.depth；可被 arcam.depth.load_depth
    读回且 depth/intrinsics 一致（L1 未落地时按 spec §2.3 断言字节数与 magic）。"""
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
