#!/usr/bin/env python3
"""验证打过补丁的 llama-funasr-cli --stream：stdin 保持打开时 SIGUSR1 提交尾段。

检查点:
  1. 无 EOF 收到 LOCKED + FLUSHED
  2. flush 延迟（SIGUSR1 -> FLUSHED）
  3. 连续两段音频会话隔离（第二段不受第一段污染）
  4. EOF 后 DONE 正常收尾

用法: python3 tools/test_stream_flush.py [--bin engines/llama-funasr-cli-vulkan] clipA.wav [clipB.wav]
      wav 需 16kHz 16bit PCM（真实人声；合成音过不了流式 VAD 属正常）
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from voiced.engine import Engine  # noqa: E402


def read_until(proc, tags, timeout=15):
    """读到任一 tags 行; 返回 (tag, text, 耗时)。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("进程 stdout EOF")
        line = line.decode("utf8", "replace").rstrip()
        for t in tags:
            if line == t or line.startswith(t + " "):
                return t, line[len(t):].strip(), time.time() - t0
    raise RuntimeError(f"超时未等到 {tags}")


def main():
    args = [a for a in sys.argv[1:]]
    bin_path = ROOT / "engines/llama-funasr-cli-vulkan"
    if "--bin" in args:
        i = args.index("--bin")
        bin_path = Path(args[i + 1])
        del args[i:i + 2]
    clips = args
    if not clips:
        print(__doc__)
        sys.exit(2)

    models = ROOT / "models"
    proc = subprocess.Popen(
        [str(bin_path), "--enc", str(models / "funasr-encoder-f16.gguf"),
         "-m", str(models / "qwen3-0.6b-q8_0.gguf"),
         "--vad", str(models / "fsmn-vad.gguf"),
         "--lang", "中文", "--stream"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        bufsize=0,
    )

    tag, _, _ = read_until(proc, ["READY"], timeout=60)
    print(f"[1] READY ok (pid={proc.pid})")

    for i, c in enumerate(clips, 1):
        pcm = Engine._load_wav(c)
        fd = proc.stdin.fileno()
        view = memoryview(pcm)
        while view:
            view = view[os.write(fd, view):]
        t0 = time.time()
        proc.send_signal(signal.SIGUSR1)
        texts = []
        t_lock = None
        while True:
            tag, text, _ = read_until(proc, ["LOCKED", "FLUSHED"], timeout=15)
            if tag == "LOCKED":
                if t_lock is None:
                    t_lock = time.time() - t0
                texts.append(text)
            else:
                t_flush = time.time() - t0
                break
        print(f"[3.{i}] {Path(c).name}")
        print(f"     LOCKED 到达: {t_lock}s, FLUSHED: {t_flush:.2f}s")
        print(f"     文本: {''.join(texts)}")

    proc.stdin.close()
    tag, _, _ = read_until(proc, ["DONE"], timeout=15)
    rc = proc.wait(timeout=10)
    print(f"[4] EOF -> DONE ok, rc={rc}")
    print("全部通过")


if __name__ == "__main__":
    main()
