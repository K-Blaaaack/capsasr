"""引擎后端探测与选择：cuda → vulkan → cpu 自动降级，支持显式指定与按设备名选卡。

探测只用 stdlib：
- 后端可用 = 引擎二进制存在 + 驱动库可 dlopen（libcuda.so.1 / libvulkan.so.1）
- 设备枚举 = 优先引擎自带 --list-devices（补丁提供）；旧二进制无此参数时
  回退 vulkaninfo --summary / nvidia-smi -L
选卡结果通过环境变量注入子进程（GGML_VK_VISIBLE_DEVICES / CUDA_VISIBLE_DEVICES）。
"""

import ctypes
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import ENGINES_DIR

_BIN_NAMES = {
    "cuda": "capsasr-engine-cuda",
    "vulkan": "capsasr-engine-vulkan",
}
_AUTO_ORDER = ("cuda", "vulkan")


@dataclass
class Candidate:
    name: str                      # cuda | vulkan | cpu
    bin: Path
    env: dict = field(default_factory=dict)
    argv: list = field(default_factory=list)
    note: str = ""


def _lib_loadable(name: str) -> bool:
    try:
        ctypes.CDLL(name)
        return True
    except OSError:
        return False


def _engine_bin(backend: str) -> Path | None:
    p = ENGINES_DIR / _BIN_NAMES[backend]
    return p if p.is_file() and p.stat().st_mode & 0o111 else None


def _run(cmd: list, timeout: float = 10) -> str | None:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def missing_libs(bin_path: Path) -> list[str]:
    """ldd 检查动态库可解析；返回 not found 的库名（ldd 不可用时返回空表）。"""
    try:
        r = subprocess.run(
            ["ldd", str(bin_path)], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [
        line.split("=>")[0].strip()
        for line in r.stdout.splitlines()
        if "not found" in line
    ]


def list_vulkan_devices() -> list[tuple[str, str]]:
    """返回 [(设备名, 描述)]，按 Vulkan 枚举序。"""
    bin = _engine_bin("vulkan")
    if bin is not None:
        out = _run([str(bin), "--list-devices"])
        if out:
            devs = []
            for line in out.splitlines():
                name, _, desc = line.strip().partition(": ")
                if name.startswith("VULKAN"):
                    devs.append((name, desc))
            if devs:
                return devs

    out = _run(["vulkaninfo", "--summary"], timeout=30)
    if not out:
        return []
    devs = []
    for block in re.split(r"^GPU\d+:$", out, flags=re.MULTILINE)[1:]:
        m = re.search(r"^\s*deviceName\s*=\s*(.+)$", block, flags=re.MULTILINE)
        if m:
            devs.append((m.group(1).strip(), m.group(1).strip()))
    return devs


def list_cuda_devices() -> list[tuple[str, str]]:
    out = _run(["nvidia-smi", "-L"])
    if not out:
        return []
    devs = []
    for line in out.splitlines():
        m = re.match(r"GPU \d+: (.+?) \(UUID", line)
        if m:
            devs.append((m.group(1).strip(), m.group(1).strip()))
    return devs


def _match_index(devs: list[tuple[str, str]], spec: str) -> int | None:
    """device 配置 → 序号：纯数字透传；否则按名字子串（不区分大小写）匹配。"""
    if spec == "auto":
        return 0 if len(devs) > 1 else None  # 多卡不设过滤会被 llama.cpp 摊到所有卡
    if spec.isdigit():
        return int(spec)
    low = spec.lower()
    for i, (name, _) in enumerate(devs):
        if low in name.lower():
            return i
    return None


def _gpu_candidate(backend: str, cfg) -> Candidate | None:
    bin = _engine_bin(backend)
    if bin is None:
        return None
    if backend == "cuda" and not _lib_loadable("libcuda.so.1"):
        return None
    if backend == "vulkan" and not _lib_loadable("libvulkan.so.1"):
        return None
    if missing_libs(bin):  # 例如 CUDA 运行库(cudart/cublas/nccl)缺失：spawn 必失败
        return None

    devs = list_cuda_devices() if backend == "cuda" else list_vulkan_devices()
    env = {}
    note = "全部可见设备" if len(devs) <= 1 else f"多卡可见: {', '.join(n for n, _ in devs)}"
    if devs:
        idx = _match_index(devs, cfg.backend_device)
        if idx is not None and 0 <= idx < len(devs):
            name, desc = devs[idx]
            var = "CUDA_VISIBLE_DEVICES" if backend == "cuda" else "GGML_VK_VISIBLE_DEVICES"
            env[var] = str(idx)
            note = f"设备{idx}: {name}" + (f" ({desc})" if desc != name else "")
        elif len(devs) <= 1:
            note = f"唯一设备: {devs[0][0]}" if devs else "未发现设备，不过滤"
        else:
            note = f"未匹配到设备 '{cfg.backend_device}'，不过滤（可见: {', '.join(n for n, _ in devs)}）"

    return Candidate(
        name=backend,
        bin=bin,
        env=env,
        argv=["--ngl", str(cfg.backend_ngl), "--enc-threads", str(cfg.backend_enc_threads)],
        note=note,
    )


def _cpu_candidate(cfg) -> Candidate | None:
    bin = _engine_bin("vulkan") or _engine_bin("cuda")
    if bin is None:
        return None
    return Candidate(
        name="cpu",
        bin=bin,
        argv=["--ngl", "0", "--enc-device", "cpu",
              "--enc-threads", str(cfg.backend_enc_threads)],
        note="纯 CPU（零显存，延迟约翻倍）",
    )


def select(cfg) -> list[Candidate]:
    """按配置产出候选链（probe 通过才算）。

    auto：cuda→vulkan→cpu 全链降级；显式模式只带 CPU 兜底——
    用户点名某后端通常有理由（如另一张卡留给别的任务），不悄悄换卡。
    """
    mode = cfg.backend_mode
    if mode == "auto":
        order = list(_AUTO_ORDER)
    elif mode in ("cuda", "vulkan", "cpu"):
        order = [mode]
    else:
        raise RuntimeError(f"backend.mode 无效: {mode!r}（可选 auto/cuda/vulkan/cpu）")

    cands: list[Candidate] = []
    if mode == "cpu":
        cpu = _cpu_candidate(cfg)
        if cpu:
            cands.append(cpu)
        return cands

    for b in order:
        c = _gpu_candidate(b, cfg)
        if c and all(c.bin != x.bin or c.env != x.env for x in cands):
            cands.append(c)
    cpu = _cpu_candidate(cfg)
    if cpu:
        cands.append(cpu)
    return cands
