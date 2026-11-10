# Fun-ASR-Nano 语音输入

长按 **大写锁定键** 说话，松手自动识别并粘贴到当前输入框。识别即用，不落任何数据。

## 原理

```
CapsLock ──keyd(/etc/keyd, overloadt 325ms)──> voice层 ──keyd listen──> stt daemon
                                              ├─ parec 常驻采集(带300ms预卷, s16le bytes 直传)
                                              ├─ 松手→能量裁剪首尾静音
                                              ├─ ASR: llama-funasr-cli --stream 常驻子进程
                                              │    (GGUF Q8, 纯C++/ggml直驱GPU, 无PyTorch)
                                              │    后端自动探测: cuda→vulkan→cpu (voiced/backend.py)
                                              │    松手时 SIGUSR1 触发尾段提交(见 tools/funasr-cli.patch)
                                              └─ wl-copy + ydotool Ctrl+V 粘贴
                                                   (0.5s 后按原MIME恢复剪贴板，含图片等二进制)
```

- **长按**（>325ms）：激活 voice 层 → 录音；松手识别粘贴
- **短敲**：正常 CapsLock 功能（keyd 原生发出，守护进程不在线也有效）

实测（5s 音频）：常驻显存 ~2.7GB（9070），松手到出字 0.4-0.7s。可与本地 LLM 异卡共存。

## 安装（Arch / AUR 路径）

```bash
# 1. 装包（控制命令进 PATH，定制引擎在 /usr/lib/funasr-nano 不进 PATH，依赖由包条目声明）
makepkg -si                      # 本地打包；AUR 上架后直接 yay -S funasr-nano

# 2. 初始化（模型下载/本地导入、后端与选卡、热词开关、自启方式）
stt init

# 3. keyd 热键（唯一需要 sudo 的环节，stt init 会提示路径）
sudo bash /usr/lib/funasr-nano/tools/setup-keyd.sh 325

# 4. 启动（init 选了自启则下次登录生效；现在立即用）
stt-daemon &
```

包内**不含模型**：`stt init` 从 Hugging Face / hf-mirror / ModelScope 三源回退下载三个 gguf 并做 sha256 校验，约 1.3GB；中断后重跑可续传（已就绪文件自动跳过）。

## 控制命令

| 命令 | 说明 |
|---|---|
| `stt init` | 向导：模型下载、后端/选卡、热词、自启（`--force` 重配，`--yes` 全默认） |
| `stt doctor` | 环境体检：python/模型/后端探测/设备/daemon/keyd/ydotoold（`--verify` 做完整 sha256） |
| `stt config` | 列出全部配置（`*`=配置文件显式设置） |
| `stt config get backend.mode` | 查单键 |
| `stt config set backend.mode vulkan` | **终端改写配置**：行级编辑，保留注释与结构 |
| `stt config edit` | 用 $EDITOR 打开配置 |
| `stt config path` | 配置文件路径 |
| `stt cli a.wav` | 经守护进程转写任意音频（16kHz 16bit PCM wav） |
| `stt daemon` | 前台运行守护进程（systemd/调试用） |
| `stt-daemon` | 带环境自愈+崩溃重拉的包装壳（桌面自启条目指向它） |

## 目录布局

```
/usr/bin/{stt,stt-daemon}                   控制命令（PATH）
/usr/lib/funasr-nano/{voiced,engines,tools} 核心运行体（不进 PATH，防与系统 llama.cpp 冲突）
~/.stt/                                     全部用户数据（卸载 = rm -rf）
  config.toml    配置（全部字段可省略，默认见 voiced/config.py DEFAULTS）
  hotwords.txt   热词（每行一个，# 注释）
  models/        三个 gguf
  logs/          daemon.log / engine.log
```

热词：编辑 `~/.stt/hotwords.txt` 后 `pkill -HUP -f "[v]oiced.daemon"` 热重载（重启常驻引擎进程）。

## 后端与选卡

```toml
[backend]
mode = "auto"      # auto: cuda→vulkan→cpu 探测降级；也可显式 cuda/vulkan/cpu
device = "auto"    # 设备名子串（如 "RADV"、"9070"）或序号；auto=多卡时取 0 号
```

启动探测（二进制存在 + 驱动库可加载），spawn 后 60s 内未 READY 自动换下一个后端并气泡通知。
显式指定的后端只降级到 CPU，不会悄悄换卡（点名某张卡通常有理由）。
选卡结果经 `GGML_VK_VISIBLE_DEVICES` / `CUDA_VISIBLE_DEVICES` 注入；设备枚举优先引擎
`--list-devices`（新版补丁），旧二进制自动回退 `vulkaninfo --summary` / `nvidia-smi -L`。

## 引擎说明

引擎二进制（包内**携带两个**）：`/usr/lib/funasr-nano/engines/llama-funasr-cli-vulkan`（现役走 AMD Radeon RX 9070）与
`llama-funasr-cli-cuda`（Blackwell/sm_120）。CUDA 版需要 CUDA 运行库（cudart/cublas/nccl，来自 `cuda` 包，
optdepends 声明）；缺失时探测会如实报告"运行库✗缺 …"并自动跳过该后端。

Fun-ASR 官方 llama.cpp fork，GGUF Q8_0（官方验证 CER 与 PyTorch 参考差 0.02%）。本项目补丁（`tools/funasr-cli.patch`）：
SIGUSR1 尾段提交（含 select+read 防死锁）、`--hotwords` 注入、`--ngl`/`--enc-device` GPU 化、
`--enc-threads`、`--list-devices`（选卡探测；现存二进制编译于该参数之前，自动回退 vulkaninfo/nvidia-smi）。

重新编译（Vulkan）：拉 [Fun-ASR](https://github.com/QwenAudio/Fun-ASR) tarball → `patch -p1 < tools/funasr-cli.patch` → 拉固定版本 llama.cpp（`8086439a4cea94c71a5dfb8fe4ad1546aebd640f`）→ 在 `runtime/llama.cpp/` 下：
```
cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_VULKAN=ON -DLLAMA_CURL=OFF \
      -DFETCHCONTENT_SOURCE_DIR_LLAMA=<本地llama.cpp路径>
cmake --build build -j 32 --target llama-funasr-cli
```
依赖：`vulkan-headers spirv-headers shaderc`。CUDA 版把 `-DGGML_VULKAN=ON` 换成 `-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=120`，需 `/opt/cuda`。

## 打包分发（维护者）

`packaging/PKGBUILD` 为 AUR/本地通用骨架：

- `depends`：python keyd wl-clipboard ydotool libnotify pipewire pipewire-pulse vulkan-icd-loader
- `optdepends`：vulkan-tools（设备探测回退）、nvidia-utils（CUDA 后端）
- `source`：源码 tarball + 引擎二进制（GitHub Release 托管），发布时改 `_github` 并 `makepkg -g` 生成 sha256
- 本地验证构建：见 PKGBUILD 头注释（`_code`/`_engine` 环境变量指向本地产物）

## 回滚 / 卸载

```bash
rm -f ~/.config/autostart/funasr-nano.desktop ~/.config/systemd/user/funasr-nano.service
pkill -f "bin/stt-daemon"; pkill -f "[v]oiced.daemon"
sudo bash /usr/lib/funasr-nano/tools/setup-keyd.sh --remove   # CapsLock 恢复原状
sudo pacman -R funasr-nano
rm -rf ~/.stt
```

## 依赖

- Python ≥ 3.11 标准库（tomllib；**无任何 pip 依赖**）
- 系统：见包 `depends`（pipewire/parec、keyd、wl-clipboard、ydotool+ydotoold、libnotify、Vulkan 加载器）
- 模型：`stt init` 下载（encoder-f16 469MB + qwen3-0.6b-q8_0 805MB + fsmn-vad 1.7MB，均 Apache-2.0）
