import os
import tomllib
from pathlib import Path

from .notify import CHANNELS, DEFAULT_CHANNEL

# 应用根：capsasr/ 的父目录。仓库直跑 = 仓库根；安装后 = /usr/lib/capsasr（或 ~/.local/...）
APP_ROOT = Path(__file__).resolve().parent.parent

# 用户数据单目录：模型/配置/热词/日志全在这，卸载 = rm -rf。
# 环境变量 CAPSASR_HOME 可覆盖（测试/多实例用）。
CAPSASR_HOME = Path(os.environ.get("CAPSASR_HOME") or Path.home() / ".capsasr")

ENGINES_DIR = APP_ROOT / "engines"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


def models_dir() -> Path:
    """模型目录：应用根带 models/（仓库开发态）优先，否则 ~/.capsasr/models。"""
    bundled = APP_ROOT / "models"
    return bundled if bundled.is_dir() else CAPSASR_HOME / "models"


DEFAULTS: dict = {
    "backend": {
        "mode": "auto",       # auto | cuda | vulkan | cpu（auto 按 cuda→vulkan→cpu 探测降级）
        "device": "auto",     # auto | 设备名子串（如 "RADV"/"9070"）| 序号
        "ngl": 99,
        "enc_threads": 16,
    },
    "models": {
        "enc": "funasr-encoder-f16.gguf",
        "llm": "qwen3-0.6b-q8_0.gguf",
        "vad": "fsmn-vad.gguf",
    },
    "engine": {"language": "中文", "hotwords": "hotwords.txt"},
    "hotkey": {"layer": "voice", "hold_ms": 325},
    # channel: desktop=notify-send（通知中心留档）| dms=DMS 悬浮 toast | both=两者
    "notify": {"enabled": False, "channel": "desktop"},
    "capture": {
        "sample_rate": 16000,
        "preroll_ms": 300,
        "trim_db": -40.0,
        "head_pad_ms": 100,
        "tail_pad_ms": 200,
        "min_seconds": 0.35,
        "max_seconds": 60.0,
    },
    "paste": {"restore_clipboard": True, "restore_delay_ms": 500},
    "server": {"socket": "capsasr.sock"},
}


def config_path() -> Path:
    return CAPSASR_HOME / "config.toml"


def load_raw(path: Path | None = None) -> dict:
    """读一层 TOML 覆盖（文件不存在返回空 dict）。"""
    p = path or config_path()
    if not p.exists():
        return {}
    with p.open("rb") as f:
        return tomllib.load(f)


def _resolve(p, base: Path) -> Path:
    path = Path(os.path.expanduser(str(p)))
    return path if path.is_absolute() else base / path


class Config:
    def __init__(self, path: Path | None = None):
        merged: dict = {sec: dict(kv) for sec, kv in DEFAULTS.items()}
        for sec, kv in load_raw(path).items():
            if sec in merged:
                merged[sec].update(kv)
            else:
                merged[sec] = dict(kv)
        self.path = path or config_path()

        bk = merged["backend"]
        self.backend_mode = str(bk.get("mode", "auto"))
        self.backend_device = str(bk.get("device", "auto"))
        self.backend_ngl = int(bk.get("ngl", 99))
        self.backend_enc_threads = int(bk.get("enc_threads", 16))

        md = models_dir()
        self.model_enc = self._resolve(merged["models"].get("enc"), md)
        self.model_llm = self._resolve(merged["models"].get("llm"), md)
        self.model_vad = self._resolve(merged["models"].get("vad"), md)

        eng = merged["engine"]
        self.language = str(eng.get("language", "中文"))
        hw = eng.get("hotwords", "hotwords.txt")
        # 空串=显式禁用热词；指向一个不存在的路径，load_hotwords 自然返回空
        self.hotwords_file = self._resolve(hw, CAPSASR_HOME) if hw else CAPSASR_HOME / "hotwords.disabled"

        self.trigger_layer = str(merged["hotkey"].get("layer", "voice"))
        self.hold_ms = int(merged["hotkey"].get("hold_ms", 325))
        self.notify_enabled = bool(merged["notify"].get("enabled", True))
        ch = str(merged["notify"].get("channel", DEFAULT_CHANNEL)).strip().lower()
        self.notify_channel = ch if ch in CHANNELS else DEFAULT_CHANNEL

        cap = merged["capture"]
        self.sample_rate = int(cap.get("sample_rate", 16000))
        self.preroll_ms = int(cap.get("preroll_ms", 300))
        self.trim_db = float(cap.get("trim_db", -40.0))
        self.head_pad_ms = int(cap.get("head_pad_ms", 100))
        self.tail_pad_ms = int(cap.get("tail_pad_ms", 200))
        self.min_seconds = float(cap.get("min_seconds", 0.35))
        self.max_seconds = float(cap.get("max_seconds", 60.0))

        pa = merged["paste"]
        self.restore_clipboard = bool(pa.get("restore_clipboard", True))
        self.restore_delay_ms = int(pa.get("restore_delay_ms", 500))

        self.socket_path = str(RUNTIME_DIR / merged["server"].get("socket", "capsasr.sock"))
        self.log_dir = CAPSASR_HOME / "logs"

    @staticmethod
    def _resolve(p, base: Path) -> Path:
        return _resolve(p, base)

    def load_hotwords(self) -> list[str]:
        if not self.hotwords_file.exists():
            return []
        words = []
        for line in self.hotwords_file.read_text(encoding="utf8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                words.append(line)
        return words
