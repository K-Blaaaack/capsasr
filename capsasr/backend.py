"""引擎后端探测与选择：cuda → vulkan → cpu 自动降级，支持显式指定与按设备名选卡。

探测只用 stdlib：
- 后端可用 = 引擎二进制存在 + 驱动库可 dlopen（libcuda.so.1 / libvulkan.so.1）
- 设备枚举 = 优先引擎自带 --list-devices（补丁提供）；旧二进制无此参数时
  回退 vulkaninfo --summary / nvidia-smi -L
选卡结果通过环境变量注入子进程（GGML_VK_VISIBLE_DEVICES / CUDA_VISIBLE_DEVICES）。
CUDA 运行库（cudart/cublas 等）不在系统默认路径且包内引擎无 RUNPATH，
探测与 spawn 统一经 LD_LIBRARY_PATH 注入解析路径。
"""

import ctypes
import os
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


def missing_libs(bin_path: Path, env: dict | None = None) -> list[str]:
    """ldd 检查动态库可解析；返回 not found 的库名（ldd 不可用时返回空表）。

    env 必须与 spawn 环境一致，否则 CUDA 引擎（无 RUNPATH）的探测结果失真。
    """
    try:
        r = subprocess.run(
            ["ldd", str(bin_path)], capture_output=True, text=True, timeout=10,
            env={**os.environ, **env} if env else None,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [
        line.split("=>")[0].strip()
        for line in r.stdout.splitlines()
        if "not found" in line
    ]


_CUDA_LIB_DIRS = (
    "/opt/cuda/lib64",                     # Arch cuda 包
    "/opt/cuda/targets/x86_64-linux/lib",  # 上游 CUDA 布局
    "/usr/local/cuda/lib64",               # runfile 默认安装位
)


def _cuda_runtime_path() -> str:
    """CUDA 运行库候选目录（存在的才收录），供 LD_LIBRARY_PATH 注入。"""
    dirs: list[str] = []
    for var in ("CUDA_HOME", "CUDA_ROOT"):
        base = os.environ.get(var)
        if base:
            dirs += [f"{base}/lib64", f"{base}/lib"]
    out: list[str] = []
    for d in dirs + list(_CUDA_LIB_DIRS):
        if d not in out and Path(d).is_dir():
            out.append(d)
    return ":".join(out)


def _lib_env(backend: str) -> dict:
    """后端库搜索环境；探测（ldd）与 spawn 必须共用，保证判据一致。"""
    if backend != "cuda":
        return {}
    lp = os.environ.get("LD_LIBRARY_PATH", "")
    merged = ":".join(p for p in (_cuda_runtime_path(), lp) if p)
    return {"LD_LIBRARY_PATH": merged} if merged else {}


def list_vulkan_devices() -> list[tuple[str, str]]:
    """返回 [(设备名, 描述)]，按 Vulkan 枚举序。"""
    bin = _engine_bin("vulkan")
    if bin is not None:
        out = _run([str(bin), "--list-devices"])
        if out:
            devs = []
            for line in out.splitlines():
                name, _, desc = line.strip().partition(": ")
                # ggml 设备名是 "Vulkan0" 这类内部序号名，无辨识度；
                # 展示与选卡匹配都用描述（真实设备名），序号沿用 ggml 枚举序
                if name.lower().startswith("vulkan"):
                    devs.append((desc or name, desc))
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
    """device 配置 → 序号：纯数字按序号（越界=None）；否则按名字子串（不区分大小写）。"""
    if spec == "auto":
        return 0 if len(devs) > 1 else None  # 多卡不设过滤会被 llama.cpp 摊到所有卡
    if spec.isdigit():
        idx = int(spec)
        return idx if 0 <= idx < len(devs) else None
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
    env = _lib_env(backend)
    if missing_libs(bin, env):  # 例如 CUDA 运行库(cudart/cublas/nccl)缺失：spawn 必失败
        return None

    devs = list_cuda_devices() if backend == "cuda" else list_vulkan_devices()
    note = "全部可见设备" if len(devs) <= 1 else f"多卡可见: {', '.join(n for n, _ in devs)}"
    if devs:
        idx = _match_index(devs, cfg.backend_device)
        if idx is not None and 0 <= idx < len(devs):
            name, desc = devs[idx]
            var = "CUDA_VISIBLE_DEVICES" if backend == "cuda" else "GGML_VK_VISIBLE_DEVICES"
            env[var] = str(idx)
            note = f"设备{idx}: {name}" + (f" ({desc})" if desc != name else "")
        elif cfg.backend_device != "auto":
            # 显式点名的设备不存在（名字不匹配/序号越界）：宁可不跑 GPU，
            # 也不悄悄占用别的卡——点名某张卡通常有理由
            hint = "纯数字按序号解释，按名字匹配请用含字母的子串（如 RADV/RTX）" if cfg.backend_device.isdigit() else ""
            print(
                f"[backend] {backend}: 未匹配到设备 '{cfg.backend_device}'"
                f"（可见: {', '.join(n for n, _ in devs)}），跳过该后端。{hint}",
                flush=True,
            )
            return None
        else:
            note = f"唯一设备: {devs[0][0]}" if devs else "未发现设备，不过滤"

    return Candidate(
        name=backend,
        bin=bin,
        env=env,
        argv=["--ngl", str(cfg.backend_ngl), "--enc-threads", str(cfg.backend_enc_threads)],
        note=note,
    )


def _cpu_candidate(cfg) -> Candidate | None:
    for b in ("vulkan", "cuda"):
        bin = _engine_bin(b)
        if bin is None:
            continue
        return Candidate(
            name="cpu",
            bin=bin,
            env=_lib_env(b),
            argv=["--ngl", "0", "--enc-device", "cpu",
                  "--enc-threads", str(cfg.backend_enc_threads)],
            note="纯 CPU（零显存，延迟约翻倍）",
        )
    return None


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
