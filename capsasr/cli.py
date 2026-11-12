"""capsasr 控制命令：init / config / doctor / cli / daemon。

安装布局: /usr/bin/capsasr + /usr/lib/capsasr/{capsasr,engines}；
仓库布局: bin/capsasr + capsasr/。用户数据统一在 ~/.capsasr。
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tomllib
from pathlib import Path

from . import backend, downloader
from .config import APP_ROOT, CAPSASR_HOME, DEFAULTS, RUNTIME_DIR, Config, config_path, models_dir

# dotted key → (节, 键, 类型)；类型用于 set 时的值解析/校验
KEYS: dict[str, tuple[str, str, type]] = {}
for _sec, _kv in DEFAULTS.items():
    for _k, _v in _kv.items():
        KEYS[f"{_sec}.{_k}"] = (_sec, _k, type(_v))


# ---------- TOML 写入（保留注释的行级编辑；schema 全为扁平标量） ----------

def _toml_str(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return _toml_str(value)


_CONFIG_TEMPLATE_HEAD = """\
# CapsASR 配置。全部字段可省略，默认值见 /usr/lib/capsasr/capsasr/config.py
# 后端: mode = auto（cuda→vulkan→cpu 降级）| cuda | vulkan | cpu
# 选卡: device = auto | 设备名子串（如 "RADV"、"9070"）| 序号
"""


def write_config(overrides: dict[str, dict], path: Path | None = None) -> Path:
    """写全量注释配置（只写非默认项，节内保持 DEFAULTS 顺序）。"""
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [_CONFIG_TEMPLATE_HEAD]
    for sec, kv in DEFAULTS.items():
        out = {k: v for k, v in (overrides.get(sec) or {}).items() if k in kv}
        if not out:
            continue
        lines.append(f"[{sec}]")
        for k in kv:
            if k in out:
                lines.append(f"{k} = {_toml_value(out[k])}")
        lines.append("")
    p.write_text("\n".join(lines) + "\n", encoding="utf8")
    return p


def set_config_value(dotted: str, value: str, path: Path | None = None) -> None:
    """行级编辑：已有键原位改值（保留注释与节顺序），新键入节尾，新节追加文件尾。"""
    if dotted not in KEYS:
        raise SystemExit(f"未知配置键 {dotted}（可用: {' '.join(sorted(KEYS))}）")
    sec, key, typ = KEYS[dotted]
    if typ is bool:
        val = value.lower() in ("1", "true", "yes", "on")
    elif typ is int:
        val = int(value)
    elif typ is float:
        val = float(value)
    else:
        val = value

    p = path or config_path()
    lines = p.read_text(encoding="utf8").splitlines() if p.exists() else []

    sec_start = None
    sec_pat = re.compile(rf"^\s*\[\s*{re.escape(sec)}\s*\]\s*(?:#.*)?$")
    for i, l in enumerate(lines):
        if sec_pat.match(l):  # 容忍缩进与节头尾注释，如 "[backend]  # 说明"
            sec_start = i
            break
    if sec_start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"[{sec}]", f"{key} = {_toml_value(val)}"]
    else:
        sec_end = len(lines)
        for i in range(sec_start + 1, len(lines)):
            if lines[i].lstrip().startswith("["):
                sec_end = i
                break
        # 容忍缩进（如 "  mode = ..."），避免改成追加重复键导致文件不可解析
        pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
        for i in range(sec_start + 1, sec_end):
            if pat.match(lines[i]):
                lines[i] = f"{key} = {_toml_value(val)}"
                break
        else:
            while sec_end > sec_start + 1 and not lines[sec_end - 1].strip():
                sec_end -= 1
            lines.insert(sec_end, f"{key} = {_toml_value(val)}")

    new_text = "\n".join(lines) + "\n"
    try:
        tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as e:
        # 编辑结果非法（如文件里本就有重复键）：宁可中止也不写坏配置
        raise SystemExit(f"写入中止：编辑结果不是合法 TOML（{e}），原文件未改动")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(new_text, encoding="utf8")


# ---------- capsasr init ----------

def _ask(prompt: str, default: str = "") -> str:
    suffix = f"[{default}] " if default else ""
    try:
        ans = input(f"{prompt} {suffix}").strip()
    except EOFError:
        ans = ""
    return ans or default


def _download_models() -> bool:
    """下载三个模型；已就绪（校验通过）的自动跳过，支持断点续跑。"""
    try:
        for name in downloader.MODELS:
            downloader.download(name, models_dir())
    except RuntimeError as e:
        print(f"下载失败: {e}")
        print("重跑 capsasr init 可续传（已就绪的文件自动跳过）")
        return False
    return True


def cmd_init(args) -> int:
    if config_path().exists() and not args.force:
        print(f"已初始化: {config_path()}（重配用 capsasr init --force）")
        return 0

    CAPSASR_HOME.mkdir(parents=True, exist_ok=True)
    if not _download_models():
        return 1

    print("\n后端:")
    for line in _probe_summary():
        print("  " + line)
    mode = "auto" if args.yes else _ask("后端模式 auto/cuda/vulkan/cpu:", "auto")
    overrides: dict[str, dict] = {"backend": {"mode": mode}}
    if mode in ("auto", "vulkan", "cuda"):
        devs = backend.list_vulkan_devices() if mode != "cuda" else backend.list_cuda_devices()
        if mode == "auto" and not devs:
            devs = backend.list_cuda_devices() or backend.list_vulkan_devices()
        if len(devs) > 1:
            for i, (n, _) in enumerate(devs):
                print(f"  设备{i}: {n}")
            overrides["backend"]["device"] = "0" if args.yes else _ask("选卡（名字子串或序号）:", "0")

    hold = 325 if args.yes else _ask_int("按住激活时长 ms:", 325, 50, 2000)
    if hold != 325:
        overrides["hotkey"] = {"hold_ms": hold}
    use_notify = (not args.yes) and _ask("启用右下角通知? y/n:", "n").lower() in ("y", "yes")
    if use_notify:
        overrides["notify"] = {"enabled": True}

    hw = CAPSASR_HOME / "hotwords.txt"
    enable_hw = (not args.yes) and _ask("启用热词表? y/n:", "y").lower() in ("y", "yes")
    if enable_hw:
        if not hw.exists():
            hw.write_text(
                "# 每行一个热词，# 开头为注释。改后执行: pkill -HUP -f [c]apsasr.daemon\n",
                encoding="utf8",
            )
        print(f"热词: {hw}")
    else:
        overrides["engine"] = {"hotwords": ""}

    p = write_config(overrides)
    kind = "autostart" if args.yes else _ask("自启 autostart/systemd/none:", "autostart")
    _setup_autostart(kind, _installed_bin_dir())
    if kind == "autostart":
        _start_daemon_now()

    try:
        conf = Path("/etc/keyd/default.conf").read_text()
    except OSError:
        conf = ""
    m = re.search(r"overloadt\([^)]*?(\d+)\s*\)", conf)
    capsasr_bin = _installed_bin_dir() / "capsasr"
    if not m:
        print(f"keyd 未配置（热键不可用），执行: sudo {capsasr_bin} setup-keyd")
    elif int(m.group(1)) != hold:
        print(f"keyd 当前阈值 {m.group(1)}ms ≠ 配置 {hold}ms，执行: sudo {capsasr_bin} setup-keyd 同步")

    print(f"\n完成: {p}")
    print("体检: capsasr doctor")
    return 0


def _ask_int(prompt: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(_ask(prompt, str(default)))
    except ValueError:
        return default
    return v if lo <= v <= hi else default


def _start_daemon_now() -> None:
    """立即后台拉起守护进程（init 选 autostart 时）：不等下次登录。"""
    launcher = _installed_bin_dir() / "capsasr-daemon"
    if not launcher.exists():
        print(f"未找到 {launcher}，请手动运行 capsasr-daemon")
        return
    subprocess.Popen(
        [str(launcher)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print("守护进程已在后台启动（立即可用；自启条目负责以后的登录）")


def _installed_bin_dir() -> Path:
    """控制命令所在 bin 目录（自启 Exec/systemd ExecStart 用）。

    优先取实际运行的 capsasr 所在目录（PATH 解析，安装态=/usr/bin，仓库态=repo/bin），
    兜底按布局推导：{prefix}/lib/capsasr → {prefix}/bin（注意两层 parent）。
    """
    exe = shutil.which("capsasr") or (
        Path(sys.argv[0]).resolve() if Path(sys.argv[0]).exists() else None
    )
    if exe:
        own = Path(exe).resolve().parent
        if (own / "capsasr-daemon").exists():
            return own
    repo_bin = APP_ROOT / "bin"
    if repo_bin.is_dir():
        return repo_bin
    return APP_ROOT.parent.parent / "bin"


def _probe_summary() -> list[str]:
    lines = []
    for b in ("cuda", "vulkan"):
        bin_path = backend._engine_bin(b)
        bin_ok = bin_path is not None
        lib_ok = backend._lib_loadable(
            "libcuda.so.1" if b == "cuda" else "libvulkan.so.1"
        )
        missing = backend.missing_libs(bin_path, backend._lib_env(b)) if bin_path else []
        devs = backend.list_cuda_devices() if b == "cuda" else backend.list_vulkan_devices()
        names = ", ".join(n for n, _ in devs) or "无设备"
        status = "可用" if bin_ok and lib_ok and not missing else "不可用"
        detail = (f"二进制{'✓' if bin_ok else '✗'} 驱动{'✓' if lib_ok else '✗'} "
                  f"运行库{'✗缺 ' + ', '.join(missing) if missing else '✓'}")
        lines.append(f"{b}: {status}（{detail}；{names}）")
    return lines


def _setup_autostart(kind: str, bin_dir: Path) -> None:
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    if kind == "autostart":
        d = xdg / "autostart"
        d.mkdir(parents=True, exist_ok=True)
        (d / "capsasr.desktop").write_text(
            "[Desktop Entry]\nType=Application\n"
            "Name=CapsASR 语音输入\nComment=长按 CapsLock 说话，松手粘贴\n"
            f"Exec={bin_dir}/capsasr-daemon\n",
            encoding="utf8",
        )
        print(f"  已写 {d / 'capsasr.desktop'}")
    elif kind == "systemd":
        d = xdg / "systemd/user"
        d.mkdir(parents=True, exist_ok=True)
        (d / "capsasr.service").write_text(
            "[Unit]\nDescription=CapsASR 语音输入守护进程\n"
            "PartOf=graphical-session.target\n\n[Service]\n"
            f"ExecStart={bin_dir}/capsasr daemon\nRestart=on-failure\nRestartSec=3\n\n"
            "[Install]\nWantedBy=default.target\n",
            encoding="utf8",
        )
        print(f"  已写 {d / 'capsasr.service'}，启用:")
        print("    systemctl --user enable --now capsasr.service")
    else:
        print("  跳过自启")


# ---------- capsasr config ----------

def cmd_config(args) -> int:
    if args.action == "path":
        print(config_path())
        return 0
    if args.action == "edit":
        editor = os.environ.get("EDITOR", "nano")
        p = config_path()
        if not p.exists():
            write_config({})
        return subprocess.call([editor, str(p)])
    if args.action == "set":
        set_config_value(args.key, args.value)
        print(f"{config_path()}: {args.key} = {args.value}")
        if args.key == "hotkey.hold_ms":
            print("执行 sudo capsasr setup-keyd 使按住阈值生效")
        elif args.key == "notify.enabled":
            print('重启守护进程生效（pkill -f "[c]apsasr.daemon"，壳会自动拉起）')
        return 0
    # get / list
    cfg = Config()
    if args.action == "get" and args.key:
        if args.key not in KEYS:
            raise SystemExit(f"未知配置键 {args.key}（可用: {' '.join(sorted(KEYS))}）")
        attr = _cfg_attr(args.key)
        print(getattr(cfg, attr))
        return 0
    raw = {}
    if config_path().exists():
        import tomllib

        with config_path().open("rb") as f:
            raw = tomllib.load(f)
    for dotted in sorted(KEYS):
        sec, key, _ = KEYS[dotted]
        mark = "*" if key in raw.get(sec, {}) else " "
        attr = _cfg_attr(dotted)
        print(f" {mark} {dotted:28} = {getattr(cfg, attr)!r}")
    print(f"\n(* = 配置文件显式设置；文件: {config_path()})")
    return 0


def _cfg_attr(dotted: str) -> str:
    """dotted key → Config 属性名。"""
    special = {
        "backend.mode": "backend_mode",
        "backend.device": "backend_device",
        "backend.ngl": "backend_ngl",
        "backend.enc_threads": "backend_enc_threads",
        "models.enc": "model_enc",
        "models.llm": "model_llm",
        "models.vad": "model_vad",
        "engine.hotwords": "hotwords_file",
        "server.socket": "socket_path",
        "hotkey.layer": "trigger_layer",
        "notify.enabled": "notify_enabled",
    }
    if dotted in special:
        return special[dotted]
    sec, key, _ = KEYS[dotted]
    return key


# ---------- capsasr doctor ----------

def _ok(flag: bool) -> str:
    return "✓" if flag else "✗"


def _daemon_pid() -> int | None:
    """运行中的守护进程 pid（cmdline 含 python3 -m capsasr.daemon）。"""
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            cmd = (p / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        except OSError:
            continue
        if "python3 -m capsasr.daemon" in cmd:
            return int(p.name)
    return None


def _daemon_env(name: str) -> str | None:
    """优先取守护进程环境（自愈后的真实值），否则退回当前 shell。"""
    pid = _daemon_pid()
    if pid:
        try:
            for kv in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
                if kv.startswith(name.encode() + b"="):
                    return kv.split(b"=", 1)[1].decode(errors="replace")
        except OSError:
            pass
    return os.environ.get(name)


def _ydotoold_running() -> bool:
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            if (p / "comm").read_text().strip() == "ydotoold":
                return True
        except OSError:
            continue
    return False


def cmd_doctor(args) -> int:
    fails = 0

    def check(name: str, flag: bool, detail: str = ""):
        nonlocal fails
        if not flag:
            fails += 1
        print(f" {_ok(flag)} {name}" + (f" — {detail}" if detail else ""))

    v = sys.version_info
    check(f"python ≥ 3.11", v >= (3, 11), f"{v.major}.{v.minor}.{v.micro}")

    try:
        cfg = Config()
        check("配置文件可解析", True, str(cfg.path) + ("（不存在，全部默认）" if not cfg.path.exists() else ""))
    except Exception as e:
        check("配置文件可解析", False, str(e))
        return 1

    for name, p in (("encoder", cfg.model_enc), ("llm", cfg.model_llm), ("vad", cfg.model_vad)):
        exists = p.exists()
        if exists and args.verify:
            exists = downloader.verify(p, p.name)
        check(f"模型 {name}", exists, str(p) + ("（sha256 校验通过）" if args.verify and exists else ""))

    print()
    for line in _probe_summary():
        print("  " + line)
    try:
        cands = backend.select(cfg)
        names = [c.name for c in cands]
        check("后端候选链", bool(names), " → ".join(names) or "engines/ 下无可用二进制")
    except RuntimeError as e:
        check("后端候选链", False, str(e))

    print()
    sock = Path(cfg.socket_path)
    alive = False
    if sock.exists():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(str(sock))
            alive = True
        except OSError:
            pass
        finally:
            s.close()
    check("守护进程运行中", alive, str(sock) if not alive else f"{sock} 可连通")
    try:
        conf = Path("/etc/keyd/default.conf").read_text()
        check("keyd 热键配置", "overloadt(" in conf and "[voice]" in conf)
    except OSError:
        check("keyd 热键配置", False, "未配置（sudo bash tools/setup-keyd.sh）")
    import grp

    try:
        in_group = "keyd" in {grp.getgrgid(g).gr_name for g in os.getgroups()}
    except KeyError:
        in_group = False
    check("keyd 组成员", in_group, "读取 /run/keyd.socket 需要（新会话生效）")
    check("WAYLAND 会话", bool(_daemon_env("WAYLAND_DISPLAY") or _daemon_env("WAYLAND_SOCKET")))
    check("DBUS 会话", bool(_daemon_env("DBUS_SESSION_BUS_ADDRESS")))
    check("ydotoold", _ydotoold_running(), "Ctrl+V 模拟需要 ydotoold 运行")
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    check("XDG 自启条目", (xdg / "autostart/capsasr.desktop").exists())
    if (xdg / "autostart/capsasr.desktop").exists() and not alive:
        print("  ! 自启条目存在但守护进程未运行：刚开机请稍候；否则运行 capsasr-daemon 看报错")
    check("systemd user unit", (xdg / "systemd/user/capsasr.service").exists())

    print(f"\n{'体检通过' if fails == 0 else f'{fails} 项异常'}")
    return 0 if fails == 0 else 1


# ---------- capsasr cli（socket 转写任意 wav） ----------

def cmd_cli(args) -> int:
    cfg = Config()
    wav = str(Path(args.wav).resolve())
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(cfg.socket_path)
        s.sendall((json.dumps({"wav": wav}) + "\n").encode())
        out = b""
        while True:
            b = s.recv(65536)
            if not b:
                break
            out += b
        reply = json.loads(out.decode())
    except (OSError, json.JSONDecodeError) as e:
        print(f"守护进程不可达（{cfg.socket_path}）: {e}", file=sys.stderr)
        return 2
    if reply.get("ok"):
        print(reply["text"])
        return 0
    print("错误:", reply.get("error"), file=sys.stderr)
    return 1


# ---------- capsasr uninstall（卸载前执行；pacman 不会动用户主目录） ----------

def cmd_uninstall(args) -> int:
    print("停止守护进程…")
    for pat in (r"[c]apsasr\.daemon", r"[c]apsasr-daemon", r"[c]apsasr-engine"):
        subprocess.run(["pkill", "-f", pat], check=False)

    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    desktop = xdg / "autostart/capsasr.desktop"
    if desktop.exists():
        desktop.unlink()
        print(f"已移除 {desktop}")
    unit = xdg / "systemd/user/capsasr.service"
    if unit.exists():
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", "capsasr.service"],
            check=False, capture_output=True,
        )
        unit.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        print(f"已移除 {unit}")
    for name in ("capsasr.sock", "capsasr.lock"):
        (RUNTIME_DIR / name).unlink(missing_ok=True)

    if args.purge:
        shutil.rmtree(CAPSASR_HOME, ignore_errors=True)
        print(f"已删除用户数据 {CAPSASR_HOME}（含模型）")
    else:
        print(f"用户数据保留在 {CAPSASR_HOME}（彻底清除加 --purge）")

    print("卸载系统包: sudo pacman -R capsasr")
    print("恢复 CapsLock 原功能（可选）: sudo capsasr setup-keyd --remove（在卸载系统包前执行）")
    return 0


# ---------- 入口 ----------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="capsasr", description="CapsASR 语音输入控制命令")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="初始化 ~/.capsasr：模型下载、后端/选卡、激活时长、通知、热词、自启")
    sp.add_argument("--force", action="store_true", help="重新配置（覆盖 config.toml）")
    sp.add_argument("--yes", action="store_true", help="全默认非交互（auto 后端、325ms、无通知、无热词、autostart）")
    sp.set_defaults(fn=cmd_init)

    sp = sub.add_parser("uninstall", help="清理用户级痕迹（自启/服务/进程/数据）；应在 pacman -R 之前执行")
    sp.add_argument("--purge", action="store_true", help="连 ~/.capsasr（含模型）一起删除")
    sp.set_defaults(fn=cmd_uninstall)

    sp = sub.add_parser("config", help="查看/修改配置")
    sp.add_argument("action", choices=["get", "set", "edit", "path", "list"], nargs="?", default="list")
    sp.add_argument("key", nargs="?", help="dotted key，如 backend.mode")
    sp.add_argument("value", nargs="?", help="set 时的值")
    sp.set_defaults(fn=cmd_config)

    sp = sub.add_parser("doctor", help="环境体检")
    sp.add_argument("--verify", action="store_true", help="对模型做完整 sha256 校验（较慢）")
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("cli", help="经守护进程转写 wav 文件")
    sp.add_argument("wav")
    sp.set_defaults(fn=cmd_cli)

    sp = sub.add_parser("setup-keyd", help="配置 keyd 热键（sudo）：按住阈值生效 / 回滚")
    sp.add_argument("--hold-ms", type=int, help="按住时长阈值毫秒（默认取配置 hotkey.hold_ms）")
    sp.add_argument("--remove", action="store_true", help="删除 keyd 配置，CapsLock 恢复原状")
    sp.set_defaults(fn=cmd_setup_keyd)

    sp = sub.add_parser("daemon", help="前台运行守护进程（systemd/调试用；桌面自启用 capsasr-daemon）")
    sp.add_argument("--config", default=None)
    sp.set_defaults(fn=lambda a: _run_daemon(a))

    args = p.parse_args(argv)
    return args.fn(args) or 0


def cmd_setup_keyd(args) -> int:
    """把配置里的按住阈值写入 keyd（需 sudo）；--remove 回滚。"""
    script = APP_ROOT / "tools/setup-keyd.sh"
    if not script.exists():
        print(f"缺少 {script}", file=sys.stderr)
        return 1
    if args.remove:
        return subprocess.call(["sudo", "bash", str(script), "--remove"])
    hold = args.hold_ms if args.hold_ms is not None else Config().hold_ms
    return subprocess.call(["sudo", "bash", str(script), str(hold)])


def _run_daemon(args) -> int:
    from .daemon import main as daemon_main

    sys.argv = ["capsasr"] + (["--config", args.config] if args.config else [])
    daemon_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
