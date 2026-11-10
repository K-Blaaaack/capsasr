import subprocess


def notify(summary: str, body: str = "", timeout_ms: int = 2000):
    args = [
        "notify-send",
        "-a", "CapsASR",
        "-t", str(timeout_ms),
        summary,
    ]
    if body:
        args.append(body)
    subprocess.run(args, check=False, capture_output=True)
