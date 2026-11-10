# CapsASR 语音输入

长按 **大写锁定键** 说话，松手自动识别并粘贴到当前输入框。识别即用，不落任何数据。

## 原理

```
CapsLock ──keyd(/etc/keyd, overloadt 325ms)──> voice层 ──keyd listen──> capsasr daemon
                                              ├─ parec 常驻采集(带300ms预卷, s16le bytes 直传)
                                              ├─ 松手→能量裁剪首尾静音
                                              ├─ ASR: llama-funasr-cli --stream 常驻子进程
                                              │    (GGUF Q8, 纯C++/ggml直驱GPU, 无PyTorch)
                                              │    后端自动探测: cuda→vulkan→cpu (capsasr/backend.py)
                                              │    松手时 SIGUSR1 触发尾段提交(见 tools/funasr-cli.patch)
                                              └─ wl-copy + ydotool Ctrl+V 粘贴
                                                   (0.5s 后按原MIME恢复剪贴板，含图片等二进制)
```

- **长按**（>325ms）：激活 voice 层 → 录音；松手识别粘贴
- **短敲**：正常 CapsLock 功能（keyd 原生发出，守护进程不在线也有效）

实测（5s 音频）：常驻显存 ~2.7GB（9070），松手到出字 0.4-0.7s。可与本地 LLM 异卡共存。

## 安装（Arch）

两种方式二选一，之后初始化步骤相同（CUDA 后端是独立子包 `capsasr-cuda`，
仅 NVIDIA 卡用户需要，会连带安装 cuda 工具链与 nccl）：

```bash
# A. 预构建包（GitHub Release 下载）
sudo pacman -U capsasr-0.4.1-1-x86_64.pkg.tar.zst
#   想用 CUDA 后端（auto 模式优先于 Vulkan）再追加：
#   sudo pacman -U capsasr-cuda-0.4.1-1-x86_64.pkg.tar.zst

# B. 从源码构建（自动拉 Release 里的引擎二进制；需 base-devel 与 patchelf）
git clone https://github.com/Wry-Zen/capsasr.git && cd capsasr/packaging && makepkg -sf
sudo pacman -U capsasr-0.4.1-1-x86_64.pkg.tar.zst   # 需要时再装 capsasr-cuda-*.zst

# 初始化（模型下载、后端与选卡、激活时长、通知开关、热词、自启；选 autostart 会立即后台启动守护进程）
capsasr init

# keyd 热键（唯一需要 sudo 的环节，使用 hotkey.hold_ms 配置的阈值）
sudo capsasr setup-keyd

# 启动（init 选了自启则下次登录生效；现在立即用）
capsasr-daemon &
```

命令补全（bash/zsh/fish）随包安装；仓库直跑时可 `source completions/capsasr.bash`（zsh/fish 对应文件同理）。

包内**不含模型**：`capsasr init` 从 Hugging Face / hf-mirror / ModelScope 三源回退下载三个 gguf 并做 sha256 校验，约 1.3GB；中断后重跑可续传（已就绪文件自动跳过）。

## 控制命令

| 命令 | 说明 |
|---|---|
| `capsasr init` | 向导：模型下载、后端/选卡、热词、自启（`--force` 重配，`--yes` 全默认） |
| `capsasr doctor` | 环境体检：python/模型/后端探测/设备/daemon/keyd/ydotoold（`--verify` 做完整 sha256） |
| `capsasr config` | 列出全部配置（`*`=配置文件显式设置） |
| `capsasr config get backend.mode` | 查单键 |
| `capsasr config set backend.mode vulkan` | **终端改写配置**：行级编辑，保留注释与结构（改 `hotkey.hold_ms` 后需 `sudo capsasr setup-keyd` 生效；改 `notify.enabled` 后重启 daemon） |
| `capsasr config edit` | 用 $EDITOR 打开配置 |
| `capsasr config path` | 配置文件路径 |
| `capsasr setup-keyd` | 把 `hotkey.hold_ms` 写入 keyd（sudo）；`--hold-ms N` 直接指定，`--remove` 回滚 |
| `capsasr uninstall` | 清理用户级痕迹（自启/服务/进程/数据）；**应在 `pacman -R` 之前执行**，`--purge` 连模型一起删 |
| `capsasr cli a.wav` | 经守护进程转写任意音频（16kHz 16bit PCM wav） |
| `capsasr daemon` | 前台运行守护进程（systemd/调试用） |
| `capsasr-daemon` | 带环境自愈+崩溃重拉的包装壳（桌面自启条目指向它） |

## 目录布局

```
/usr/bin/{capsasr,capsasr-daemon}                   控制命令（PATH）
/usr/lib/capsasr/{capsasr,engines,tools} 核心运行体（不进 PATH，防与系统 llama.cpp 冲突）
~/.capsasr/                                     全部用户数据（卸载 = rm -rf）
  config.toml    配置（全部字段可省略，默认见 capsasr/config.py DEFAULTS）
  hotwords.txt   热词（每行一个，# 注释）
  models/        三个 gguf
  logs/          daemon.log / engine.log
```

热词：编辑 `~/.capsasr/hotwords.txt` 后 `pkill -HUP -f "[v]oiced.daemon"` 热重载（重启常驻引擎进程）。

## 后端与选卡

```toml
[backend]
mode = "auto"      # auto: cuda→vulkan→cpu 探测降级；也可显式 cuda/vulkan/cpu
device = "auto"    # 设备名子串（如 "RADV"、"9070"）或序号；auto=多卡时取 0 号
```

启动探测（二进制存在 + 驱动库可加载），spawn 后 60s 内未 READY 自动换下一个后端并气泡通知。
显式指定的后端只降级到 CPU，不会悄悄换卡（点名某张卡通常有理由）。
选卡结果经 `GGML_VK_VISIBLE_DEVICES` / `CUDA_VISIBLE_DEVICES` 注入；设备枚举优先引擎
`--list-devices`（Vulkan 引擎 0.4.1 起已编译入），CUDA 旧二进制自动回退
`vulkaninfo --summary` / `nvidia-smi -L`。

## 引擎说明

引擎二进制（**拆两个包**）：主包带 `/usr/lib/capsasr/engines/capsasr-engine-vulkan`（现役走 AMD Radeon RX 9070）；
`capsasr-cuda` 子包带 `capsasr-engine-cuda`（Blackwell/sm_120，depends 声明 nvidia-utils/cuda/nccl），
auto 模式下优先于 Vulkan，未安装则探测自动跳过。CUDA 引擎包内已剥离 RUNPATH，
运行库（cudart/cublas/nccl）由 daemon 在 spawn 时注入 `LD_LIBRARY_PATH` 解析
（`/opt/cuda`、`CUDA_HOME` 等位置均可）。

Fun-ASR 官方 llama.cpp fork，GGUF Q8_0（官方验证 CER 与 PyTorch 参考差 0.02%）。本项目补丁（`tools/funasr-cli.patch`）：
SIGUSR1 尾段提交（含 select+read 防死锁）、`--hotwords` 注入、`--ngl`/`--enc-device` GPU 化、
`--enc-threads`、`--list-devices`（选卡探测；Vulkan 引擎 0.4.1 起已编译入，CUDA 旧二进制自动回退 vulkaninfo/nvidia-smi）。

重新编译（Vulkan）：拉 [Fun-ASR](https://github.com/QwenAudio/Fun-ASR) tarball → `patch -p1 < tools/funasr-cli.patch` → 拉固定版本 llama.cpp（`8086439a4cea94c71a5dfb8fe4ad1546aebd640f`）→ 在 `runtime/llama.cpp/` 下：
```
cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_VULKAN=ON -DLLAMA_CURL=OFF \
      -DCMAKE_SKIP_RPATH=ON -DCMAKE_EXE_LINKER_FLAGS="-Wl,-z,relro,-z,now" \
      -DFETCHCONTENT_SOURCE_DIR_LLAMA=<本地llama.cpp路径>
cmake --build build -j 32 --target llama-funasr-cli
```
依赖：`vulkan-headers spirv-headers shaderc`。CUDA 版把 `-DGGML_VULKAN=ON` 换成 `-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=120`，需 `/opt/cuda`。
链接参数说明：`-z now` 补全 FULL RELRO（namcap 要求）；`CMAKE_SKIP_RPATH` 使二进制不带
`/opt/cuda` RUNPATH（namcap 报 Insecure RUNPATH）——CUDA 运行库改由 daemon 经
`LD_LIBRARY_PATH` 注入（`capsasr/backend.py`），兼容任意 CUDA 安装位置。

## 打包分发（维护者）

分发走 **GitHub Release**（AUR 新用户注册暂关；`PKGBUILD`/`.SRCINFO` 保留，注册恢复后可直接提交）：

- Release 资产四件：`capsasr-engine-vulkan`、`capsasr-engine-cuda`、预构建包 `capsasr-0.4.1-1-x86_64.pkg.tar.zst` 与 `capsasr-cuda-0.4.1-1-x86_64.pkg.tar.zst`
- `depends`（主包）：bash glibc libstdc++ libgcc libgomp python keyd wl-clipboard ydotool libnotify pipewire pipewire-pulse vulkan-icd-loader
- `depends`（capsasr-cuda）：capsasr glibc libstdc++ libgcc libgomp nvidia-utils cuda nccl
- 构建包：`cd packaging && makepkg -sf`（需 `pacman -S patchelf`，用于剥离 CUDA 引擎 RUNPATH）
- 本地验证：见 PKGBUILD 头注释（source 不收绝对路径，产物以基名放入 PKGBUILD 目录）
- namcap 残留告警均有解释：FULL RELRO 需按上面参数重编译引擎；keyd 等子进程运行依赖 namcap 静态分析看不见；cuda/nccl 库告警仅在未装它们的构建机上出现

## 回滚 / 卸载

```bash
capsasr uninstall --purge        # 清自启/服务/进程/数据（--purge 连模型；不加则保留 ~/.capsasr）
sudo capsasr setup-keyd --remove  # CapsLock 恢复原状（可选）
sudo pacman -R capsasr            # 卸载系统包
```

## 依赖

- Python ≥ 3.11 标准库（tomllib；**无任何 pip 依赖**）
- 系统：见包 `depends`（pipewire/parec、keyd、wl-clipboard、ydotool+ydotoold、libnotify、Vulkan 加载器）
- 模型：`capsasr init` 下载（encoder-f16 469MB + qwen3-0.6b-q8_0 805MB + fsmn-vad 1.7MB，均 Apache-2.0）
