import argparse
import fcntl
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path

from .capture import Capture, trim_silence
from .config import Config
from .engine import Engine
from .hotkey import HotkeyListener
from .notify import configure, notify
from .paste import paste_text
from .server import Server


class Daemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        configure(cfg.notify_enabled, cfg.notify_channel)
        print("[daemon] loading engine...", flush=True)
        self.engine = Engine(cfg)
        self.capture = Capture(cfg)
        self.server = Server(cfg.socket_path, self.engine.transcribe)

        self._jobs: queue.Queue = queue.Queue(maxsize=1)
        self._worker = threading.Thread(target=self._work, daemon=True)
        self._recording = False
        self._rec_started = 0.0

        self.hotkey = HotkeyListener(
            cfg,
            on_hold_start=self._on_start,
            on_hold_end=self._on_end,
        )

    def _on_start(self):
        self._recording = True
        self._rec_started = time.monotonic()
        self.capture.start()
        notify("● 录音中", timeout_ms=1500)

    def _on_end(self, dur: float):
        self._recording = False
        audio, total = self.capture.stop()
        if total < self.cfg.min_seconds:
            notify("○ 过短，已丢弃", f"{total:.2f}s")
            return
        try:
            self._jobs.put_nowait((audio, total))
        except queue.Full:
            notify("识别忙碌，已丢弃本次录音", level="warn")

    def _work(self):
        while True:
            audio, total = self._jobs.get()
            try:
                self._process(audio, total)
            except Exception as e:
                print(f"[daemon] 处理失败: {e}", flush=True)
                notify("识别失败", str(e)[:120], level="error")

    def _process(self, audio, total):
        cfg = self.cfg
        max_bytes = int(cfg.max_seconds * cfg.sample_rate) * 2
        if len(audio) > max_bytes:
            audio = audio[:max_bytes]
            notify("超过时长上限，已截断", f"{cfg.max_seconds:.0f}s", level="warn")

        audio = trim_silence(audio, cfg)
        dur = len(audio) // 2 / cfg.sample_rate
        if dur < cfg.min_seconds:
            notify("○ 静音，已丢弃")
            return

        t0 = time.time()
        text = self.engine.transcribe(audio)
        elapsed = time.time() - t0

        if not text:
            notify("未识别到内容", f"{dur:.1f}s / {elapsed:.2f}s")
            return

        ok = paste_text(
            text,
            restore=cfg.restore_clipboard,
            restore_delay_ms=cfg.restore_delay_ms,
        )
        if ok:
            notify("✓ 已粘贴", f"{text[:80]}", timeout_ms=3000)
        print(f"[daemon] {dur:.1f}s audio -> {elapsed:.2f}s: {text}", flush=True)

    def run(self):
        self.server.start()
        self._worker.start()
        self.hotkey.start()

        def _reload(*_):
            self.engine.reload_hotwords()

        def _quit(*_):
            raise KeyboardInterrupt

        signal.signal(signal.SIGHUP, _reload)
        signal.signal(signal.SIGTERM, _quit)
        print("[daemon] ready", flush=True)

        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            self.hotkey.stop()
            self.server.stop()
            self.capture.close()
            self.engine.close()


def _acquire_singleton(cfg) -> object | None:
    """登录自启撞上已运行实例时安静退出，避免双录音/引擎双份显存。

    必须在 Engine 构造（spawn 引擎子进程）之前调用。
    """
    lock_path = os.path.join(os.path.dirname(cfg.socket_path), "capsasr.lock")
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def main():
    parser = argparse.ArgumentParser(prog="capsasr")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    cfg = Config(Path(args.config) if args.config else None)
    lock = _acquire_singleton(cfg)
    if lock is None:
        print("[daemon] 已有实例在运行，退出", flush=True)
        sys.exit(0)
    try:
        Daemon(cfg).run()
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    main()
