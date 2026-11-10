#!/usr/bin/env bash
# keyd listen 输出格式校准：请按住 CapsLock 一秒后松开，重复两次
# 用法: bash tools/calibrate_keyd.sh
set -euo pipefail

echo "接下来 10 秒，请【按住 CapsLock 一秒 → 松开】重复两次，观察输出："
if groups | grep -qw keyd; then
    timeout 10 keyd listen || true
else
    echo "(当前会话尚无 keyd 组权限，用 newgrp 临时获取)"
    echo 'timeout 10 keyd listen' | newgrp keyd || true
fi
echo
echo "若上面没有任何输出：确认 1) setup-keyd.sh 已跑过 2) 已重新登录或当前 shell 在 keyd 组"
