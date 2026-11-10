"""模型下载：HF 官方源优先，hf-mirror.com 回退；sha256 校验；已就绪文件自动跳过。

校验基线来自官方发布文件（本机已验证可用副本的指纹）。
"""

import hashlib
import urllib.request
from pathlib import Path

MODELS: dict[str, dict] = {
    "funasr-encoder-f16.gguf": {
        "repo": "FunAudioLLM/Fun-ASR-Nano-GGUF",
        "sha256": "f92f91d01a24fbed6c863495b2ee8c6a6788144a02858b75743f0946668de8a2",
        "size": 469331008,
    },
    "qwen3-0.6b-q8_0.gguf": {
        "repo": "FunAudioLLM/Fun-ASR-Nano-GGUF",
        "sha256": "819f385dc0e035dccc3d9e7edaf6b7b044b8ba7ace63cbcbf84c7e397eecbf27",
        "size": 804753280,
    },
    "fsmn-vad.gguf": {
        "repo": "FunAudioLLM/fsmn-vad-GGUF",
        "sha256": "1270f2559c495f4e7b6e739541151027d360761a3fda43fc147034f5719f5479",
        "size": 1720512,
    },
}

_BASES = (
    lambda repo, name: f"https://huggingface.co/{repo}/resolve/main/{name}",
    lambda repo, name: f"https://hf-mirror.com/{repo}/resolve/main/{name}",
    lambda repo, name: f"https://www.modelscope.cn/models/{repo}/resolve/master/{name}",
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(path: Path, name: str) -> bool:
    meta = MODELS[name]
    return path.stat().st_size == meta["size"] and sha256_file(path) == meta["sha256"]


def download(name: str, dest_dir: Path, log=print) -> Path:
    """下载并校验单个模型；成功返回路径，全部源失败抛 RuntimeError。"""
    if name not in MODELS:
        raise RuntimeError(f"未知模型 {name}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    if dest.exists() and verify(dest, name):
        log(f"  {name}: 已就绪")
        return dest

    repo = MODELS[name]["repo"]
    for make_url in _BASES:
        part = dest.with_suffix(dest.suffix + ".part")
        log(f"  {name}: 下载中…")
        try:
            req = urllib.request.Request(make_url(repo, name), headers={"User-Agent": "capsasr/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp, part.open("wb") as out:
                total = int(resp.headers.get("Content-Length") or MODELS[name]["size"])
                done = 0
                for chunk in iter(lambda: resp.read(1 << 20), b""):
                    out.write(chunk)
                    done += len(chunk)
                    pct = done * 100 // total if total else 0
                    log(f"\r  {name}: {pct:3d}%  {done >> 20}/{total >> 20} MB",
                        end="", flush=True)
        except OSError:
            part.unlink(missing_ok=True)
            log(f"\r  {name}: 源不可达，换下一个源")
            continue
        log("")
        if verify(part, name):
            part.replace(dest)
            log(f"  {name}: 校验通过")
            return dest
        part.unlink(missing_ok=True)
        log(f"  {name}: 校验失败，重试")

    raise RuntimeError(f"{name}: Hugging Face 与 hf-mirror 均失败")
