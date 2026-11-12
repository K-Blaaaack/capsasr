import os
import queue
import signal
import subprocess
import threading
import time
import wave
from array import array
from pathlib import Path

from . import backend
from .notify import configure, notify


class Engine:
    """GGUF 常驻流式 ASR 引擎。

    包装打了补丁的 llama-funasr-cli --stream 常驻子进程：
    每段音频写入 stdin 后发 SIGUSR1 触发尾段 is_final 提交，
    按序读 LOCKED 行直到 FLUSHED 标记。补丁见 tools/funasr-cli.patch。

    后端（CUDA/Vulkan/CPU）由 capsasr.backend 探测决定；候选链逐个尝试，
    READY 握手失败的自动降级到下一个。
    """

    READY_TIMEOUT = 60

    def __init__(self, cfg):
        self.cfg = cfg
        configure(cfg.notify_enabled, cfg.notify_channel)
        self._lock = threading.Lock()
        self.hotwords = cfg.load_hotwords()

        for name, path in (
            ("encoder", cfg.model_enc),
            ("llm", cfg.model_llm),
            ("vad", cfg.model_vad),
        ):
            if not path.exists():
                raise RuntimeError(f"引擎缺少 {name}: {path}")

        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue | None = None
        self._cand = None
        self.backend_name = None

        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        self._stderr_sink = open(cfg.log_dir / "engine.log", "ab")

        candidates = backend.select(cfg)
        if not candidates:
            raise RuntimeError(
                "没有可用的引擎后端（engines/ 下无二进制或驱动库不可加载）"
            )
        tried = []
        for i, cand in enumerate(candidates):
            try:
                self._spawn(cand)
            except Exception as e:
                tried.append(f"{cand.name}: {e}")
                print(f"[engine] 后端 {cand.name} 启动失败（{cand.note}）: {e}", flush=True)
                if i + 1 < len(candidates):
                    notify(
                        f"后端 {cand.name} 启动失败",
                        f"降级到 {candidates[i + 1].name}",
                        level="warn",
                    )
                continue
            self._cand = cand
            self.backend_name = cand.name
            break
        else:
            raise RuntimeError("所有后端启动失败: " + "; ".join(tried))

        print(
            f"[engine] 就绪 backend={self.backend_name}（{self._cand.note}），"
            f"热词 {len(self.hotwords)} 个",
            flush=True,
        )

    def _spawn(self, cand):
        cfg = self.cfg
        if self._proc and self._proc.poll() is None:
            self._proc.kill()

        argv = [
            str(cand.bin),
            "--enc", str(cfg.model_enc),
            "-m", str(cfg.model_llm),
            "--vad", str(cfg.model_vad),
            "--lang", cfg.language,
            *cand.argv,
            "--stream",
        ]
        if self.hotwords:
            argv += ["--hotwords", ", ".join(self.hotwords)]

        self._lines = queue.Queue()
        env = {**os.environ, **cand.env}
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_sink,
            bufsize=0,
            env=env,
        )
        threading.Thread(
            target=self._reader, args=(self._proc, self._lines), daemon=True
        ).start()

        try:
            line = self._lines.get(timeout=self.READY_TIMEOUT)
        except queue.Empty:
            raise RuntimeError(f"{self.READY_TIMEOUT}s 未 READY")
        if line != "READY":
            raise RuntimeError(f"引擎子进程未就绪: {line!r}")

    @staticmethod
    def _reader(proc, lines):
        """队列随参数绑定：旧进程退出时其 reader 只会污染旧队列，不影响重载后的新队列。"""
        for raw in proc.stdout:
            lines.put(raw.decode("utf8", "replace").rstrip("\r\n"))
        lines.put(None)

    def reload_hotwords(self):
        self.hotwords = self.cfg.load_hotwords()
        with self._lock:
            self._spawn(self._cand)  # 热词在启动参数里，重启常驻进程生效
        print(f"[engine] hotwords reloaded: {len(self.hotwords)}", flush=True)

    def close(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        self._stderr_sink.close()

    @staticmethod
    def _load_wav(path) -> bytes:
        """16kHz 16bit PCM wav → s16le bytes（多声道取平均）。仅依赖 stdlib wave。"""
        with wave.open(str(path), "rb") as w:
            if w.getframerate() != 16000:
                raise RuntimeError(f"采样率 {w.getframerate()} 不支持（需要 16kHz）")
            if w.getsampwidth() != 2:
                raise RuntimeError("仅支持 16bit PCM wav")
            nch = w.getnchannels()
            raw = w.readframes(w.getnframes())
        if nch == 1:
            return raw
        samples = array("h", raw)
        if samples.itemsize != 2:
            raise RuntimeError("array 'h' 非 16bit，平台异常")
        mixed = array("h", bytes(2 * (len(samples) // nch)))
        for i in range(0, len(samples) - nch + 1, nch):
            acc = 0
            for c in range(nch):
                acc += samples[i + c]
            mixed[i // nch] = acc // nch
        return mixed.tobytes()

    def transcribe(self, audio) -> str:
        """audio: s16le bytes（16kHz 单声道）或 16kHz 16bit PCM wav 路径。"""
        if isinstance(audio, (str, os.PathLike, Path)):
            pcm = self._load_wav(audio)
        else:
            pcm = bytes(audio)

        with self._lock:
            for attempt in (1, 2):
                try:
                    fd = self._proc.stdin.fileno()
                    view = memoryview(pcm)
                    while view:
                        view = view[os.write(fd, view):]
                    self._proc.send_signal(signal.SIGUSR1)
                    break
                except (BrokenPipeError, OSError) as e:
                    if attempt == 2:
                        raise RuntimeError(f"引擎子进程管道断裂: {e}")
                    print("[engine] 子进程失联，重启", flush=True)
                    self._spawn(self._cand)

            texts = []
            deadline = time.time() + 30
            while True:
                try:
                    line = self._lines.get(timeout=max(0.1, deadline - time.time()))
                except queue.Empty:
                    # 超时后旧引擎可能仍在推理：立即重启换新队列，丢弃迟到的
                    # LOCKED/FLUSHED，避免污染下一次 transcribe（请求-响应对应关系）
                    try:
                        self._spawn(self._cand)
                    except Exception as e:
                        print(f"[engine] 超时后重启失败: {e}", flush=True)
                    raise RuntimeError("转写超时（30s 未收到 FLUSHED），引擎已重启")
                if line is None:
                    raise RuntimeError("引擎子进程意外退出")
                if line == "FLUSHED":
                    return "".join(texts).strip()
                if line.startswith("LOCKED"):
                    seg = line[6:].strip()
                    if seg:
                        texts.append(seg)
