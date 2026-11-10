#!/usr/bin/env bash
# keyd 安装与配置：CapsLock = overloadt(voice, capslock, <阈值ms>)
#   按住超过阈值: 激活 voice 层（voice-daemon 经 keyd listen 感知）
#   短敲:         keyd 原生发出 CapsLock，不依赖任何用户态进程
# 用法: sudo bash tools/setup-keyd.sh [阈值毫秒, 默认 325]
# 回滚: sudo bash tools/setup-keyd.sh --remove
set -euo pipefail

TARGET_USER="${SUDO_USER:-$(id -un)}"
HOLD_MS="${1:-325}"

if [[ "${1:-}" == "--remove" ]]; then
    systemctl disable --now keyd 2>/dev/null || true
    rm -f /etc/keyd/default.conf
    echo "keyd 已停止，配置已删除。CapsLock 恢复原功能。"
    echo "如需彻底卸载: sudo pacman -Rs keyd ; sudo gpasswd -d $TARGET_USER keyd"
    exit 0
fi

if ! command -v keyd >/dev/null; then
    pacman -S --needed --noconfirm keyd
fi

mkdir -p /etc/keyd
cat > /etc/keyd/default.conf <<EOF
# 长按大写锁定(>${HOLD_MS}ms) = 语音输入（激活 voice 层，不产生任何按键事件）
# 短敲 = 原生 CapsLock（由 keyd 直接发出，voice-daemon 不在线也有效）
[ids]
*

[main]
capslock = overloadt(voice, capslock, ${HOLD_MS})

[voice]
EOF

# keyd listen 需要读取 /run/keyd.socket（属组 keyd）
usermod -aG keyd "$TARGET_USER"

systemctl enable --now keyd
systemctl restart keyd

echo
echo "完成。当前长按阈值: ${HOLD_MS}ms"
echo "守护进程读取 keyd socket 需要 keyd 组权限；新建/重启 voice-daemon 服务即可继承。"
