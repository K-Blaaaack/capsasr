#!/usr/bin/env python3
"""trim_silence 新旧实现等价性回归（一次性对照，numpy 仅本测试使用）。

旧实现（numpy float32 帧统计）与新实现（array('h') 纯 stdlib）在相同 PCM 上
必须裁出完全相同的字节区间。
"""

import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from voiced.capture import trim_silence  # noqa: E402

CFG = SimpleNamespace(sample_rate=16000, trim_db=-40.0, head_pad_ms=100, tail_pad_ms=200)


def trim_old_bounds(pcm: bytes) -> tuple[int, int] | None:
    """旧算法的样本区间 [first*frame, last*frame)；None = 全静音丢弃。"""
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    frame = int(CFG.sample_rate * 0.01)
    n_frames = len(audio) // frame
    if n_frames == 0:
        return (0, len(audio))  # 旧实现原样返回
    frames = audio[: n_frames * frame].reshape(n_frames, frame)
    rms = np.sqrt(np.mean(frames**2, axis=1) + 1e-12)
    peak = rms.max()
    if peak < 1e-5:
        return None
    thresh = peak * (10.0 ** (CFG.trim_db / 20.0))
    active = np.nonzero(rms > thresh)[0]
    if len(active) == 0:
        return None
    head = int(CFG.head_pad_ms / 10)
    tail = int(CFG.tail_pad_ms / 10)
    first = max(0, active[0] - head)
    last = min(n_frames, active[-1] + 1 + tail)
    return (first * frame, last * frame)


def check(name: str, pcm: bytes):
    bounds = trim_old_bounds(pcm)
    got = trim_silence(pcm, CFG)
    if bounds is None:
        assert got == b"", f"{name}: 期望全丢弃，得到 {len(got)} 字节"
    else:
        lo, hi = bounds
        expect = pcm[lo * 2 : hi * 2]
        assert got == expect, (
            f"{name}: 区间不一致 旧=[{lo},{hi}) 新={len(got)//2} 样本"
        )
    print(f"  ok: {name} ({len(pcm)//2} 样本 -> {len(got)//2})")


def sine_pcm(seconds: float, amp: float, lead_sil: float = 0.0, tail_sil: float = 0.0) -> bytes:
    import math

    n = int(16000 * seconds)
    out = bytearray()
    for ms in (lead_sil, seconds, tail_sil):
        cnt = int(16000 * ms) if ms else 0
        if ms == seconds:
            for i in range(cnt):
                v = int(amp * 32767 * math.sin(2 * math.pi * 220 * i / 16000))
                out += struct.pack("<h", v)
        else:
            out += b"\x00\x00" * cnt
    return bytes(out)


def main():
    rng = np.random.default_rng(42)

    check("首尾静音", sine_pcm(2.0, 0.6, lead_sil=0.5, tail_sil=0.4))
    check("无静音", sine_pcm(1.5, 0.5))
    check("纯静音", b"\x00\x00" * 16000)
    check("短于 1 帧", sine_pcm(0.005, 0.5))
    odd = sine_pcm(1.2, 0.4)[:-1]  # 奇字节：旧实现直接抛异常，新实现应安全截断
    got_odd = trim_silence(odd, CFG)
    assert isinstance(got_odd, bytes) and len(got_odd) % 2 == 0
    print(f"  ok: 奇字节安全截断 ({len(got_odd)//2} 样本)")
    check("空输入", b"")

    noise = (rng.standard_normal(16000 * 3) * 8000).astype("<i2").tobytes()
    check("随机噪声 3s", noise)

    quiet_loud = b"\x00\x00" * (16000 * 5) + sine_pcm(1.0, 0.9)
    check("前 5s 静音后说话", quiet_loud)

    for db in (-20.0, -30.0, -50.0, -60.0):
        CFG.trim_db = db
        check(f"trim_db={db}", sine_pcm(2.0, 0.6, lead_sil=0.3, tail_sil=0.3))
    CFG.trim_db = -40.0

    import time

    t0 = time.perf_counter()
    trim_silence((rng.standard_normal(16000 * 60) * 8000).astype("<i2").tobytes(), CFG)
    print(f"  60s 音频耗时: {time.perf_counter() - t0:.3f}s")
    print("等价性全部通过")


if __name__ == "__main__":
    main()
