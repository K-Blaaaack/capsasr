import subprocess

_enabled = True


def set_enabled(enabled: bool):
    global _enabled
    _enabled = enabled


def notify(summary: str, body: str = "", timeout_ms: int = 2000):
    if not _enabled:
        return
    args = [
        "notify-send",
        "-a", "CapsASR",
        "-t", str(timeout_ms),
        summary,
    ]
    if body:
        args.append(body)
    subprocess.run(args, check=False, capture_output=True)
