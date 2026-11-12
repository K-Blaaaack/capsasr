"""状态提示的两条通道：通用通知（notify-send）与 DMS 悬浮 toast。

- ``desktop``：``notify-send`` → ``org.freedesktop.Notifications``，进通知中心留档。
- ``dms``：``dms ipc call toast <level>[With]`` → DMS 顶部居中悬浮层
  （namespace ``dms:toast``）。不进通知中心、无历史。
- ``both``：两条都发（悬浮 + 留档）。

DMS toast 的限制（实测）：一次只播一条、排队；时长固定，``timeout_ms``
对它无效——info 1.5s / warn 3s / error 5s，error 带明细 8s；同级同文本
自动去重；队列上限 3。``dms`` 通道在 dms 缺失或调用失败时自动退回
``notify-send``，提示不会静默丢失。
"""

import shutil
import subprocess
import time

CHANNELS = ("desktop", "dms", "both")
DEFAULT_CHANNEL = "desktop"

# DMS toast 的 level 与 IPC 函数前缀（info/infoWith、warn/warnWith、error/errorWith）
_LEVELS = ("info", "warn", "error")

# DMS 可用性探测缓存：守护进程可能比桌面先启动，DMS 稍后才可调用；
# 失败也缓存一小段时间，避免每条提示都白起一个探测进程。
_DMS_TTL = 30.0

_enabled = True
_channel = DEFAULT_CHANNEL
_dms_ok = False
_dms_checked = 0.0


def configure(enabled: bool, channel: str = DEFAULT_CHANNEL):
    """由 Config 注入开关与通道；非法通道值静默落回 desktop。"""
    global _enabled, _channel, _dms_checked
    _enabled = bool(enabled)
    ch = str(channel or DEFAULT_CHANNEL).strip().lower()
    _channel = ch if ch in CHANNELS else DEFAULT_CHANNEL
    _dms_checked = 0.0


def dms_available() -> bool:
    """dms 是否可调用（带 TTL 缓存）。init/doctor 也用它。"""
    global _dms_ok, _dms_checked
    now = time.monotonic()
    if now - _dms_checked < _DMS_TTL:
        return _dms_ok
    _dms_ok = _probe_dms()
    _dms_checked = now
    return _dms_ok


def _is_status_output(out: bytes) -> bool:
    """只认 DMS toast status 的合法返回：hidden 或 visible:<level>:<message>。"""
    text = (out or b"").strip()
    return text == b"hidden" or text.startswith(b"visible:")


def _is_toast_success(out: bytes) -> bool:
    """只认 TOAST_*_SUCCESS。

    dms 对未知 target/函数会返回 rc=0 + "Target not found." / "Function not found."，
    只看 rc 或「不含 ERROR」会把 DMS 换版本后的静默失败当成发送成功。
    """
    text = (out or b"").strip()
    return text.startswith(b"TOAST_") and text.endswith(b"_SUCCESS")


def _probe_dms() -> bool:
    exe = shutil.which("dms")
    if not exe:
        return False
    try:
        r = subprocess.run(
            [exe, "ipc", "call", "toast", "status"],
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and _is_status_output(r.stdout)


def _mark_dms_down():
    """刚失败过：本进程内短暂走通用通知，TTL 后再试。"""
    global _dms_ok, _dms_checked
    _dms_ok = False
    _dms_checked = time.monotonic()


def _send_toast(summary: str, body: str, level: str) -> bool:
    """发一条 DMS toast；返回是否真的进了 DMS。

    DMS 的 IPC 在参数错误时返回码仍是 0（正文才是 ERROR: ...），必须看 stdout；
    成功串 TOAST_ERROR_SUCCESS 里也含 "ERROR"，所以只认 TOAST_*_SUCCESS。
    参数前的 ``--`` 防止以 - 开头的识别文本被 dms 的 cobra 当 flag。
    """
    fn = level + ("With" if body else "")
    args = ["dms", "ipc", "call", "toast", fn, "--", summary]
    if body:
        # infoWith(message, details, command, category)
        args += [body, "", "capsasr"]
    try:
        r = subprocess.run(args, capture_output=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and _is_toast_success(r.stdout)


def _send_desktop(summary: str, body: str, timeout_ms: int):
    args = [
        "notify-send",
        "-a", "CapsASR",
        "-t", str(int(timeout_ms)),
        summary,
    ]
    if body:
        args.append(body)
    try:
        subprocess.run(args, check=False, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        # 没装 libnotify / 通知服务不可用：提示丢就丢了，不能拖垮录音链路
        pass


def notify(summary: str, body: str = "", timeout_ms: int = 2000, level: str = "info"):
    """发状态提示。

    level 只影响 toast 的档位（info/warn/error）；``timeout_ms`` 只对
    notify-send 生效，DMS toast 时长固定。
    """
    if not _enabled:
        return
    summary = str(summary or "")
    body = str(body or "")
    if level not in _LEVELS:
        level = "info"

    if _channel == "desktop":
        _send_desktop(summary, body, timeout_ms)
        return

    if dms_available():
        if _send_toast(summary, body, level):
            if _channel == "both":
                _send_desktop(summary, body, timeout_ms)
            return
        # 刚真正发送失败（DMS 退出/换版本）：短暂回退，TTL 后再试
        _mark_dms_down()

    # dms 不可用（探测失败或负缓存命中）：退回通用通知。
    # 这里不能刷新 _dms_checked，否则高频提示会让负缓存永不过期，
    # DMS 稍后启动也永远等不到下一次探测。
    _send_desktop(summary, body, timeout_ms)
