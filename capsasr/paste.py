import subprocess
import threading
import time

_TEXT_TYPES = [
    "text/plain;charset=utf-8",
    "UTF8_STRING",
    "text/plain",
    "STRING",
    "TEXT",
]
_IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/bmp", "image/gif"]


def _run(args, **kw):
    return subprocess.run(args, capture_output=True, check=False, **kw)


def _save_clipboard():
    """暂存剪贴板内容（文本或图片等二进制 MIME），返回 (bytes, mime) 或 None。

    wl-copy 一次只能提供一种 MIME，因此富文本会降级为纯文本；
    图片按无损格式优先原样保留。
    """
    r = _run(["wl-paste", "--list-types"])
    if r.returncode != 0:
        return None
    types = r.stdout.decode("utf8", "replace").split()
    if not types:
        return None

    pick = next((t for t in _TEXT_TYPES if t in types), None)
    if pick is None:
        pick = next((t for t in _IMAGE_TYPES if t in types), None)
    if pick is None:
        pick = next((t for t in types if t.startswith("image/")), None)
    if pick is None:
        pick = types[0]

    data = _run(["wl-paste", "-t", pick])
    if data.returncode != 0 or not data.stdout:
        return None
    return data.stdout, pick


def _restore_clipboard(data: bytes, mime: str):
    p = subprocess.Popen(["wl-copy", "-t", mime], stdin=subprocess.PIPE)
    p.communicate(data)


def paste_text(text: str, restore: bool = True, restore_delay_ms: int = 500) -> bool:
    """写入剪贴板并模拟 Ctrl+V，之后按原 MIME 恢复剪贴板内容。

    返回自动粘贴是否成功；失败时**不恢复**原剪贴板——识别文本留在里面供手动 Ctrl+V。
    """
    old = _save_clipboard() if restore else None

    p = subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE)
    p.communicate(text.encode("utf8"))
    if p.returncode != 0:
        print("[paste] wl-copy 失败（剪贴板不可用？WAYLAND_DISPLAY 缺失）", flush=True)
        from .notify import notify

        notify("剪贴板写入失败", "文本未进入剪贴板", level="error")
        return False

    time.sleep(0.05)
    r = _run(["ydotool", "key", "29:1", "47:1", "47:0", "29:0"])
    if r.returncode != 0:
        # 粘贴失败：识别文本已在剪贴板，跳过恢复，留给用户手动粘贴
        print(f"[paste] ydotool 失败(rc={r.returncode})，识别文本留在剪贴板", flush=True)
        from .notify import notify

        notify("自动粘贴失败", "识别文本已留在剪贴板，可手动 Ctrl+V", level="error")
        return False

    if old:
        def _later():
            time.sleep(restore_delay_ms / 1000.0)
            _restore_clipboard(*old)

        threading.Thread(target=_later, daemon=True).start()
    return True
