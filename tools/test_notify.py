#!/usr/bin/env python3
"""notify 通道路由回归：desktop / dms / both + 失败回退与缓存边界。

不依赖真实 dms / 通知服务：临时目录里放同名假程序，把 argv 用 \\x1f 分隔
逐参数记录（空串也能断言），再用 CAPSASR_TEST_LOG 取回。
可单独运行：python tools/test_notify.py
"""

import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from capsasr import notify  # noqa: E402
from capsasr.config import Config  # noqa: E402

FAKE_DMS = """#!/bin/sh
printf 'dms' >> "$CAPSASR_TEST_LOG"
for a in "$@"; do printf '\\037%s' "$a" >> "$CAPSASR_TEST_LOG"; done
printf '\\n' >> "$CAPSASR_TEST_LOG"
case "$FAKE_DMS_MODE" in
  down)     exit 1 ;;
  notfound) echo 'Target not found.' ;;
  badfunc)  case "$4" in status) echo hidden ;; *) echo 'Function not found.' ;; esac ;;
  err)      case "$4" in status) echo hidden ;; *) echo 'ERROR: No message specified' ;; esac ;;
  *)
    case "$4" in
      status) echo hidden ;;
      # 与真实 DMS 对齐：成功串形如 TOAST_ERROR_SUCCESS（正文里也含 ERROR）
      *) name=$(printf '%s' "$4" | tr 'a-z' 'A-Z'); echo "TOAST_${name%WITH}_SUCCESS" ;;
    esac ;;
esac
exit 0
"""

FAKE_NOTIFY_SEND = """#!/bin/sh
printf 'notify-send' >> "$CAPSASR_TEST_LOG"
for a in "$@"; do printf '\\037%s' "$a" >> "$CAPSASR_TEST_LOG"; done
printf '\\n' >> "$CAPSASR_TEST_LOG"
exit 0
"""

_failures = 0


def check(name: str, cond: bool, detail: str = ""):
    global _failures
    print(f" {'✓' if cond else '✗'} {name}" + (f" — {detail}" if not cond and detail else ""))
    if not cond:
        _failures += 1


def D(*args) -> list[str]:
    """期望的 dms 调用（函数名后的 -- 与空串槽位都精确匹配）。"""
    return ["dms", "ipc", "call", "toast", *args]


def N(*args) -> list[str]:
    return ["notify-send", "-a", "CapsASR", *args]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="capsasr-notify-"))
    try:
        bindir = tmp / "bin"
        bindir.mkdir()
        (bindir / "dms").write_text(FAKE_DMS, encoding="utf8")
        (bindir / "notify-send").write_text(FAKE_NOTIFY_SEND, encoding="utf8")
        for f in bindir.iterdir():
            f.chmod(f.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        log = tmp / "calls.log"
        os.environ["CAPSASR_TEST_LOG"] = str(log)
        os.environ["PATH"] = f"{bindir}:{os.environ.get('PATH', '')}"

        def take() -> list[list[str]]:
            if not log.exists():
                return []
            lines = [l.split("\x1f") for l in log.read_text(encoding="utf8").splitlines() if l]
            log.unlink()
            return lines

        def sent() -> list[list[str]]:
            """只取真正发出的提示（过滤 dms 可用性探测）。"""
            return [l for l in take() if l != D("status")]

        # 1) 总开关关：一条都不发
        notify.configure(False, "dms")
        notify.notify("关掉了", "details")
        check("enabled=false 不发任何东西", sent() == [])

        # 2) desktop：只有 notify-send，参数与 timeout 保持旧行为
        notify.configure(True, "desktop")
        notify.notify("纯文本提示")
        check("desktop 仅 notify-send", sent() == [N("-t", "2000", "纯文本提示")])
        notify.notify("带明细", "第二行", timeout_ms=3000)
        check("desktop 明细与 -t",
              sent() == [N("-t", "3000", "带明细", "第二行")])

        # 3) dms：无明细走 info，有明细走 infoWith（空 command 槽位必须原样传）
        os.environ["FAKE_DMS_MODE"] = "ok"
        notify.configure(True, "dms")
        notify.notify("录音中")
        check("dms 无明细走 info", sent() == [D("info", "--", "录音中")])
        notify.notify("已粘贴", "识别文本")
        check("dms 有明细走 infoWith + category",
              sent() == [D("infoWith", "--", "已粘贴", "识别文本", "", "capsasr")])

        # 4) level → toast 档位；error 成功串 TOAST_ERROR_SUCCESS 不能被误判
        notify.notify("忙", level="warn")
        check("warn 走 warn", sent() == [D("warn", "--", "忙")])
        notify.notify("坏了", "堆栈", level="error")
        check("error+明细走 errorWith",
              sent() == [D("errorWith", "--", "坏了", "堆栈", "", "capsasr")])
        notify.notify("未知档位", level="fatal")
        check("非法 level 落回 info", sent() == [D("info", "--", "未知档位")])

        # 5) dms 调不通（rc≠0）：退回 notify-send
        os.environ["FAKE_DMS_MODE"] = "down"
        notify.configure(True, "dms")
        notify.notify("回退", "dms 没起来")
        check("dms 不可用退回 notify-send",
              sent() == [N("-t", "2000", "回退", "dms 没起来")])
        check("dms_available 反映不可用", notify.dms_available() is False)

        # 6) 探测正常、发送正文 ERROR（rc=0）：同样退回
        os.environ["FAKE_DMS_MODE"] = "err"
        notify.configure(True, "dms")
        notify.notify("正文报错")
        check("dms 返回 ERROR 也退回 notify-send",
              sent() == [D("info", "--", "正文报错"), N("-t", "2000", "正文报错")])

        # 7) 未知函数：rc=0 + "Function not found." 不能被当成发送成功
        os.environ["FAKE_DMS_MODE"] = "badfunc"
        notify.configure(True, "dms")
        notify.notify("函数没了")
        check("dms 未知函数也退回 notify-send",
              sent() == [D("info", "--", "函数没了"), N("-t", "2000", "函数没了")])

        # 8) 未知 target：探测输出不是 status 合法值 → 直接走通用通知
        os.environ["FAKE_DMS_MODE"] = "notfound"
        notify.configure(True, "dms")
        notify.notify("目标没了")
        check("dms 未知 target 探测失败并退回",
              sent() == [N("-t", "2000", "目标没了")])
        check("dms_available 对未知 target 为 False", notify.dms_available() is False)

        # 9) 以 - 开头的明细：-- 必须挡住 cobra 的 flag 解析
        os.environ["FAKE_DMS_MODE"] = "ok"
        notify.configure(True, "dms")
        notify.notify("横线", "-abc")
        check("明细以 - 开头时参数前有 --",
              sent() == [D("infoWith", "--", "横线", "-abc", "", "capsasr")])

        # 10) both：悬浮 + 留档
        notify.configure(True, "both")
        notify.notify("双通道", "两路都发")
        check("both 发 toast + notify-send",
              sent() == [D("infoWith", "--", "双通道", "两路都发", "", "capsasr"),
                         N("-t", "2000", "双通道", "两路都发")])

        # 11) 负缓存命中时不能刷新探测时间（否则 DMS 起来后永远不再探测）
        notify.configure(True, "dms")
        notify._dms_ok = False
        notify._dms_checked = time.monotonic() - 5.0
        before = notify._dms_checked
        notify.notify("负缓存")
        lines = take()
        check("负缓存命中不重新探测",
              all(l != D("status") for l in lines))
        check("负缓存命中不刷新 _dms_checked", notify._dms_checked == before)
        check("负缓存命中照常退回 notify-send",
              lines == [N("-t", "2000", "负缓存")])

        # 12) 配置层：notify.channel 解析/归一
        toml = tmp / "config.toml"
        toml.write_text('[notify]\nenabled = true\nchannel = "dms"\n', encoding="utf8")
        check("Config 读 channel", Config(toml).notify_channel == "dms")
        toml.write_text('[notify]\nchannel = "DMS"\n', encoding="utf8")
        check("Config 大小写归一", Config(toml).notify_channel == "dms")
        toml.write_text('[notify]\nchannel = "telepathy"\n', encoding="utf8")
        check("Config 非法值落回 desktop", Config(toml).notify_channel == "desktop")

        print(f"\n{'全部通过' if _failures == 0 else f'{_failures} 项失败'}")
        return 0 if _failures == 0 else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
