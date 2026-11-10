import os
import subprocess
import sys
import threading
import time
from array import array
from collections import deque
from math import sqrt


class Capture:
    """parec(PipeWire-Pulse) 常驻采集流 + 预卷环形缓冲。

    流从守护进程启动就一直开着，避免按键瞬间开设备的延迟吞掉首音节。
    未录音时数据只进 preroll 环形缓冲；start() 后同时进录音缓冲。
    缓冲保持 parec 原生的 s16le bytes，引擎子进程可直接消费。
    parec 退出（拔麦等）会自动重连。
    """

    def __init__(self, cfg):
        self.cfg = cfg
        sr = cfg.sample_rate
        self._block = int(sr * 0.03)  # 30ms 一块
        self._ring: deque[bytes] = deque(
            maxlen=max(1, cfg.preroll_ms // 30 + 1)
        )
        self._chunks: list[bytes] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _spawn(self):
        return subprocess.Popen(
            [
                "parec",
                "--format=s16le",
                f"--rate={self.cfg.sample_rate}",
                "--channels=1",
                "--latency-msec=30",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )

    def _read_loop(self):
        nbytes = self._block * 2  # s16le
        while not self._stop.is_set():
            try:
                self._proc = self._spawn()
            except FileNotFoundError:
                print("[capture] parec 不存在，5s 后重试", flush=True)
                time.sleep(5)
                continue

            fd = self._proc.stdout.fileno()
            carry = b""
            while not self._stop.is_set():
                try:
                    data = os.read(fd, nbytes)
                except OSError:
                    break
                if not data:
                    break
                if carry:  # 上次读取劈开了样本，拼回来（见踩坑：奇长度读取）
                    data = carry + data
                    carry = b""
                if len(data) & 1:
                    carry = data[-1:]
                    data = data[:-1]
                with self._lock:
                    self._ring.append(data)
                    if self._chunks is not None:
                        self._chunks.append(data)

            if not self._stop.is_set():
                print("[capture] parec 流中断，2s 后重连", flush=True)
                time.sleep(2)

    def start(self):
        with self._lock:
            self._chunks = list(self._ring)

    def stop(self) -> tuple[bytes, float]:
        with self._lock:
            chunks = self._chunks or []
            self._chunks = None
        pcm = b"".join(chunks)
        return pcm, len(pcm) // 2 / self.cfg.sample_rate

    def close(self):
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()


def _s16le(pcm: bytes) -> array:
    samples = array("h", pcm)
    if sys.byteorder == "big":  # array 是本机序；s16le 小端机才免转换
        samples.byteswap()
    return samples


def trim_silence(pcm: bytes, cfg) -> bytes:
    """按帧 RMS 相对峰值裁剪首尾静音，保留头尾垫。输入输出均为 s16le bytes。

    阈值数学与旧 numpy 实现一致：帧 RMS 归一化到 [-1,1]，
    peak * 10^(trim_db/20) 以下视为静音。
    """
    if not pcm:
        return pcm
    sr = cfg.sample_rate
    fs = int(sr * 0.01)          # 每帧样本数（10ms）
    n_frames = len(pcm) // 2 // fs
    if n_frames == 0:
        return pcm

    samples = _s16le(pcm[: n_frames * fs * 2])
    norm = 32768.0
    rms = []
    for i in range(n_frames):
        acc = 0
        for v in samples[i * fs : (i + 1) * fs]:
            acc += v * v
        rms.append(sqrt(acc / fs / (norm * norm) + 1e-12))

    peak = max(rms)
    if peak < 1e-5:
        return b""

    thresh = peak * (10.0 ** (cfg.trim_db / 20.0))
    first = last = -1
    for i, r in enumerate(rms):
        if r > thresh:
            if first < 0:
                first = i
            last = i
    if first < 0:
        return b""

    head = int(cfg.head_pad_ms / 10)
    tail = int(cfg.tail_pad_ms / 10)
    first = max(0, first - head)
    last = min(n_frames, last + 1 + tail)
    out = samples[first * fs : last * fs]
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()
