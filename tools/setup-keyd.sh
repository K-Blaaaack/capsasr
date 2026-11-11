#!/usr/bin/env bash
# keyd 配置：CapsLock = overloadt(voice, capslock, <阈值ms>)
#   合并写入：只改 [main] 里的 capslock 绑定（这是本工具唯一负责的键），
#   其余用户配置原样保留；首次修改前备份为 <conf>.capsasr.bak。
#   --remove 优先恢复备份；无备份则只摘除 capslock 绑定。绝不关闭 keyd 服务。
# 用法: sudo bash tools/setup-keyd.sh [阈值毫秒, 默认 325]
# 回滚: sudo bash tools/setup-keyd.sh --remove
# 测试: KEYD_CONF=/tmp/k.conf bash tools/setup-keyd.sh 500   （沙箱：不碰系统与 systemd）
set -euo pipefail

CONF="${KEYD_CONF:-/etc/keyd/default.conf}"
[[ "$CONF" != /* ]] && CONF="$PWD/$CONF"
BACKUP="${CONF}.capsasr.bak"
SANDBOX=0; [[ "$CONF" != /etc/keyd/* ]] && SANDBOX=1
TARGET_USER="${SUDO_USER:-$(id -un)}"

svc() {  # 重启 keyd（沙箱模式下不碰 systemd）
    [[ "$SANDBOX" == 1 ]] && return 0
    systemctl restart keyd 2>/dev/null || true
}

if [[ "${1:-}" == "--remove" ]]; then
    if [[ -f "$BACKUP" ]]; then
        mv "$BACKUP" "$CONF"
        echo "已恢复修改前的原配置: $CONF"
    elif [[ -f "$CONF" ]]; then
        python3 - "$CONF" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
lines = [l for l in p.read_text().splitlines()
         if not (l.strip().startswith("capslock") and "overloadt(voice" in l)]
p.write_text("\n".join(lines) + "\n")
PY
        echo "已摘除 capsasr 的 capslock 绑定（其余配置保留）"
    else
        echo "没有可回滚的配置"
    fi
    svc
    echo "CapsLock 恢复原功能。彻底卸载（可选）: sudo pacman -Rs keyd ; sudo gpasswd -d $TARGET_USER keyd"
    exit 0
fi

HOLD_MS="${1:-325}"
if [[ "$SANDBOX" == 0 ]]; then
    command -v keyd >/dev/null || pacman -S --needed --noconfirm keyd
    mkdir -p /etc/keyd
else
    mkdir -p "$(dirname "$CONF")"
fi

python3 - "$CONF" "$BACKUP" "$HOLD_MS" <<'PY'
import pathlib, re, shutil, sys
conf, backup, hold = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(conf)
lines = p.read_text().splitlines() if p.exists() else []
binding = f"capslock = overloadt(voice, capslock, {hold})"

# 首次修改前留底（只留最早一份：多轮改动后仍能回到动手前）
if p.exists() and not pathlib.Path(backup).exists():
    shutil.copy2(conf, backup)

main_idx = next((i for i, l in enumerate(lines) if l.strip() == "[main]"), None)
pat = re.compile(r"^\s*capslock\s*=")
if main_idx is None:
    lines += ([""] if lines and lines[-1].strip() else []) + ["[main]", binding]
else:
    end = next((i for i in range(main_idx + 1, len(lines))
                if lines[i].lstrip().startswith("[")), len(lines))
    for i in range(main_idx + 1, end):
        if pat.match(lines[i]):          # 原位更新阈值（容忍缩进/行尾注释行被整体替换）
            lines[i] = binding
            break
    else:
        lines.insert(end, binding)       # [main] 内新增绑定
if not any(l.strip() == "[voice]" for l in lines):
    lines += ["", "[voice]"]             # voice 层声明（空节即可）
p.write_text("\n".join(lines) + "\n")
PY

if [[ "$SANDBOX" == 0 ]]; then
    # keyd listen 需要读取 /run/keyd.socket（属组 keyd）
    usermod -aG keyd "$TARGET_USER"
    systemctl enable --now keyd
    systemctl restart keyd
    echo
    echo "完成。当前长按阈值: ${HOLD_MS}ms（只改动 capslock 绑定，其余 keyd 配置原样保留）"
    echo "守护进程读取 keyd socket 需要 keyd 组权限；重新登录或重启 voice-daemon 服务后生效。"
else
    echo "[sandbox] 完成: $CONF（阈值 ${HOLD_MS}ms，未触碰系统配置与 systemd）"
fi
