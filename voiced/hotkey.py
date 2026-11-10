import re
import subprocess
import threading
import time


class HotkeyListener:
    """监听 keyd 层状态（`keyd listen`），把 voice 层激活/关闭映射为按住/松手。

    keyd 配置（tools/setup-keyd.sh 写入）:
        capslock = overloadt(voice, capslock, <阈值ms，当前 325>)
    短敲 CapsLock 由 keyd 原生发出，不经过本进程。
    """

    def __init__(self, cfg, on_hold_start, on_hold_end, on_tap=None):
        self.layer = cfg.trigger_layer
        self.on_hold_start = on_hold_start
        self.on_hold_end = on_hold_end
        self._active = False
        self._started_at = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._proc: subprocess.Popen | None = None

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(
                    ["keyd", "listen"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
            except FileNotFoundError:
                print("[hotkey] keyd 未安装，5s 后重试", flush=True)
                time.sleep(5)
                continue

            print("[hotkey] keyd listen 已连接", flush=True)
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                if self._stop.is_set():
                    break
                self._parse(line.strip())

            rc = self._proc.wait()
            if not self._stop.is_set():
                print(f"[hotkey] keyd listen 退出(rc={rc})，2s 后重连", flush=True)
                time.sleep(2)

    def _parse(self, line: str):
        line = line.strip()
        if not line:
            return

        # keyd listen 实际格式: +voice / -voice 表示层激活/关闭，
        # 连接时先输出一次当前层栈如 /main
        if line == f"+{self.layer}":
            self._set(True)
        elif line == f"-{self.layer}":
            self._set(False)
        elif line.startswith("/"):
            self._set(self.layer in re.split(r"[/\s]+", line))

    def _set(self, active: bool):
        if active == self._active:
            return
        self._active = active
        if active:
            self._started_at = time.monotonic()
            self.on_hold_start()
        else:
            self.on_hold_end(time.monotonic() - self._started_at)
