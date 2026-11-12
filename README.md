# CapsASR 语音输入

长按 **CapsLock** 说话，松手自动识别并把文本粘贴到光标处。全程本地推理——不联网、不落盘、无 PyTorch。

- **交互**：按住 >325ms 进入语音态；短敲仍是原生 CapsLock（keyd 直接发出，守护进程不在线也有效）
- **实测**：松手到出字 0.4–0.7s（5–20s 语句）；常驻显存 ~2.7GB（RX 9070），可与本机 LLM 异卡共存
- **后端**：cuda → vulkan → cpu 自动探测降级；支持按设备名或序号选卡
- **实现**：Python 仅标准库（≥3.11，零 pip 依赖）+ 自建 Fun-ASR-Nano GGUF 引擎（llama.cpp 系，纯 C++ 直驱 GPU）

## 原理

```
CapsLock ──keyd(/etc/keyd, overloadt 325ms)──> voice层 ──keyd listen──> capsasr daemon
                                              ├─ parec 常驻采集（300ms 预卷，s16le bytes 直传）
                                              ├─ 松手 → 能量裁剪首尾静音
                                              ├─ ASR：llama-funasr-cli --stream 常驻子进程
                                              │    （GGUF Q8；后端探测 cuda→vulkan→cpu）
                                              │    松手时 SIGUSR1 触发尾段提交（tools/funasr-cli.patch）
                                              └─ wl-copy + ydotool Ctrl+V 粘贴
                                                   （0.5s 后按原 MIME 恢复剪贴板，含图片等二进制）
```

## 安装（Arch）

包分两个：`capsasr` 主包（Python 运行体 + Vulkan 引擎，独立完整可用）与 `capsasr-cuda`
（CUDA 引擎，仅 NVIDIA 用户加装，连带安装 nvidia-utils/cuda/nccl）。auto 模式下 CUDA 优先于
Vulkan；未安装则探测自动跳过，不影响使用。

```bash
# A. 预构建包（GitHub Release 下载）
sudo pacman -U capsasr-0.4.5-1-x86_64.pkg.tar.zst
#   要用 CUDA 后端再追加：
#   sudo pacman -U capsasr-cuda-0.4.5-1-x86_64.pkg.tar.zst

# B. 从源码构建（自动拉 Release 引擎二进制；需 base-devel 与 patchelf）
git clone https://github.com/Wry-Zen/capsasr.git && cd capsasr/packaging && makepkg -sf
sudo pacman -U capsasr-0.4.5-1-x86_64.pkg.tar.zst   # 需要时再装 capsasr-cuda-*.zst
```

命令补全（bash/zsh/fish）随包安装；仓库直跑可 `source completions/capsasr.bash`（zsh/fish 同理）。

**平台要求**：x86_64 Linux（Arch；CPU 需支持 AVX2，约 2013 年后）；**Wayland 会话**（粘贴链依赖 wl-copy/wl-paste）；GPU 显存 ~2.7GB（Vulkan 或 CUDA，不足则自动降级 CPU，零显存但延迟约翻倍）；CUDA 引擎需 NVIDIA 驱动 + CUDA 13 运行库（`capsasr-cuda` 会自动拉齐）。

## 初始化

```bash
capsasr init            # 向导：模型下载、后端/选卡、激活时长、通知、热词、自启
sudo capsasr setup-keyd # 唯一需要 root 的环节：把 CapsLock 长按阈值写入 keyd
capsasr-daemon &        # init 选了自启则下次登录生效；这是立即启动
```

- 包内**不含模型**：三个 gguf（encoder 469MB + LLM 805MB + VAD 1.7MB，约 1.3GB）由 init 从
  Hugging Face / hf-mirror / ModelScope 依次回退下载，sha256 校验，中断重跑续传（已就绪自动跳过）
- init 选 autostart 会立即后台启动守护进程

## 控制命令

| 命令 | 说明 |
|---|---|
| `capsasr init` | 向导：模型下载、后端/选卡、热词、自启（`--force` 重配，`--yes` 全默认） |
| `capsasr doctor` | 环境体检：python/模型/后端探测/设备/daemon/keyd/ydotoold（`--verify` 做完整 sha256） |
| `capsasr config` | 列出全部配置（`*`=配置文件显式设置） |
| `capsasr config get backend.mode` | 查单键 |
| `capsasr config set backend.mode vulkan` | **终端改写配置**：行级编辑，保留注释与结构 |
| `capsasr config edit` / `config path` | 用 $EDITOR 打开配置 / 显示路径 |
| `capsasr setup-keyd` | 把 `hotkey.hold_ms` 写入 keyd（sudo）；`--hold-ms N` 直接指定，`--remove` 回滚 |
| `capsasr uninstall` | 清理用户级痕迹（自启/服务/进程/数据）；**应在 `pacman -R` 之前执行**，`--purge` 连模型一起删 |
| `capsasr cli a.wav` | 经守护进程转写任意音频（16kHz 16bit PCM wav） |
| `capsasr daemon` | 前台运行守护进程（systemd/调试用） |
| `capsasr-daemon` | 带环境自愈 + 崩溃 3s 重拉的包装壳（桌面自启条目指向它） |

改 `hotkey.hold_ms` 后需 `sudo capsasr setup-keyd` 生效；改 `notify.enabled` 后重启 daemon。

## 目录布局

```
/usr/bin/{capsasr,capsasr-daemon}                    控制命令（PATH）
/usr/lib/capsasr/{capsasr,engines,tools}             核心运行体（不进 PATH，防与系统 llama.cpp 冲突）
~/.capsasr/                                          全部用户数据（卸载 = rm -rf）
  config.toml    配置（全部字段可省略，默认见 capsasr/config.py DEFAULTS）
  hotwords.txt   热词（每行一个，# 注释）
  models/        三个 gguf
  logs/          daemon.log / engine.log
```

热词：编辑 `~/.capsasr/hotwords.txt` 后 `pkill -HUP -f "[c]apsasr.daemon"` 热重载（重启常驻引擎进程）。

## 配置参考

`~/.capsasr/config.toml` 全部可省略；`capsasr config` 以 `*` 标出显式设置项。

| 键 | 默认 | 说明 |
|---|---|---|
| `backend.mode` | auto | auto=cuda→vulkan→cpu 降级；显式 cuda/vulkan/cpu 只加 CPU 兜底 |
| `backend.device` | auto | 设备名子串（如 "RADV"、"RTX"；纯数字按序号）；auto=多卡取 0 号；点名不存在则拒绝上 GPU，宁降 CPU 不占别的卡 |
| `backend.ngl` | 99 | LLM 层 offload 数（0=纯 CPU） |
| `backend.enc_threads` | 16 | 编码器 CPU 回退线程数 |
| `models.enc / llm / vad` | 文件名 | 相对模型目录；绝对路径原样 |
| `engine.language` | 中文 | 中 / 英 / 日 |
| `engine.hotwords` | hotwords.txt | 空串=禁用热词 |
| `hotkey.layer` | voice | keyd 层名，须与 /etc/keyd 配置一致 |
| `hotkey.hold_ms` | 325 | 长按判定阈值（毫秒） |
| `notify.enabled` | false | 右下角状态气泡（含后端降级通知） |
| `capture.*` | 16000 / 300 / -40 / 100 / 200 / 0.35 / 60 | 采样率Hz / 预卷ms / 裁剪阈值dB / 首垫ms / 尾垫ms / 最短s / 最长s |
| `paste.restore_clipboard / restore_delay_ms` | true / 500 | 剪贴板恢复 |
| `server.socket` | capsasr.sock | 位于 `$XDG_RUNTIME_DIR` 下 |

## 后端与选卡

```toml
[backend]
mode = "auto"      # auto: cuda→vulkan→cpu 探测降级；也可显式 cuda/vulkan/cpu
device = "auto"    # 设备名子串（如 "RADV"、"RTX"；纯数字一律按序号解释）或序号；auto=多卡时取 0 号
```

- 探测判据：引擎二进制存在 + 驱动库可 dlopen + ldd 无缺库；spawn 后 60s 内未 READY 自动换下一候选并气泡通知
- 显式指定的后端只降级到 CPU，不会悄悄换卡（点名某张卡通常有理由）
- 设备枚举优先引擎 `--list-devices`（Vulkan 引擎 0.4.1 起原生支持，展示真实设备名）；无此参数的旧二进制回退 `vulkaninfo --summary` / `nvidia-smi -L`
- 选卡结果经 `GGML_VK_VISIBLE_DEVICES` / `CUDA_VISIBLE_DEVICES` 注入子进程

## 引擎与补丁

主包携带 `capsasr-engine-vulkan`（0.4.3 起可移植构建：`GGML_NATIVE=OFF`，构建机指令集不再泄漏进二进制；FULL RELRO + PIE、无 RUNPATH、原生 `--list-devices`）；
`capsasr-cuda` 携带 `capsasr-engine-cuda`（0.4.3 起七档全覆盖：sm_75/80/86/89/90/100/120a SASS + 每档 PTX，比列表更新的卡可 JIT；不再链接 nccl）。CUDA 引擎包内已剥离 RUNPATH，
运行库（cudart/cublas/nccl）由 daemon 在 spawn 时注入 `LD_LIBRARY_PATH` 解析——
`/opt/cuda`、`CUDA_HOME` 等任意安装位置均可。

引擎基于 Fun-ASR 官方 llama.cpp fork，GGUF Q8_0（官方验证 CER 与 PyTorch 参考差 0.02%）。本项目补丁（`tools/funasr-cli.patch`）：

1. **SIGUSR1 尾段提交**：上游只在 stdin EOF 时 flush，且 VAD 尾段确认需 1850ms 静音，不适合"松手即要结果"；补丁改为信号置标志 + 主循环检查，对当前段 is_final 提交并输出 `FLUSHED` 结束标记。stdin 读取改 select+read（信号落在 fread 阻塞窗口会死锁到超时）
2. **`--hotwords`**：按 funasr 官方 prompt 格式注入热词上下文
3. **`--ngl` / `--enc-device`**：LLM 层标准 offload；221M 编码器经 ggml backend registry 上传 GPU 常驻（CUDA/Vulkan 通用，失败自动回退 CPU；日志 `[enc] encoder on GPU` 可确认）
4. **`--enc-threads`**：CPU 回退线程数（默认 16）
5. **`--list-devices`**：打印 backend registry 设备表，供守护进程选卡探测（在参数校验前处理，可裸调）

重编译（Vulkan）：拉 [Fun-ASR](https://github.com/QwenAudio/Fun-ASR) tarball → `patch -p1 < tools/funasr-cli.patch` → 拉固定版本 llama.cpp（`8086439a4cea94c71a5dfb8fe4ad1546aebd640f`，勿随意升级，补丁与该版 ggml API 对齐）→ 在 `runtime/llama.cpp/` 下：

```
cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_VULKAN=ON -DLLAMA_CURL=OFF \
      -DGGML_NATIVE=OFF \
      -DCMAKE_SKIP_RPATH=ON -DCMAKE_EXE_LINKER_FLAGS="-Wl,-z,relro,-z,now" \
      -DFETCHCONTENT_SOURCE_DIR_LLAMA=<本地llama.cpp路径>
cmake --build build -j 32 --target llama-funasr-cli
```

依赖 `vulkan-headers spirv-headers shaderc`。CUDA 版把 `-DGGML_VULKAN=ON` 换成 `-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="75;80;86;89;90;100;120-virtual"`，需 `/opt/cuda`（编译机装 `cuda` 包即可，**不需要 NVIDIA 显卡在场**；列表含分号，shell 里必须带引号；nccl 可选——不装则不链，仅影响多卡并行性能，本工具选卡固定单卡）。

可移植性要点（在别人的设备上踩过 SIGILL 后的结论）：
- `GGML_NATIVE=OFF`：默认 `native` 会把构建机指令集（如 AVX-512）烤进代码，在旧 CPU 上直接非法指令（i5-13500H 实测）。关掉后编译到 **AVX2 档**（与 llama.cpp 官方发布二进制一致，覆盖约 2013 年后的 x86-64）；热点在 GPU，CPU 档位只影响 VAD（毫秒级）与 CPU 兜底模式（略慢）。不启用 `GGML_CPU_ALL_VARIANTS`：它要求动态后端，产物变成多个 .so，破坏单文件布局
- CUDA 架构列表覆盖 Turing(75)/Ampere(80,86)/Ada(89)/Hopper(90)/Blackwell(100,120)；末位 `120-virtual` 嵌 PTX，更新的卡可 JIT
- `-z now` 补全 FULL RELRO；`CMAKE_SKIP_RPATH` 去掉 `/opt/cuda` RUNPATH，运行库改由 daemon 注入 `LD_LIBRARY_PATH`（`capsasr/backend.py`）
- **先停 daemon 再 cp 二进制**（Text file busy），验证后删构建树

## 打包分发（维护者）

分发走 **GitHub Release**（AUR 新用户注册暂关；`PKGBUILD`/`.SRCINFO` 保留，注册恢复后可直接提交）。

- **拆双包**：`capsasr`（Vulkan 引擎）+ `capsasr-cuda`（CUDA 引擎，depends: capsasr glibc libstdc++ libgcc libgomp nvidia-utils cuda）
- **主包 depends**：bash glibc libstdc++ libgcc libgomp python keyd wl-clipboard ydotool libnotify pipewire pipewire-pulse vulkan-icd-loader；optdepends: vulkan-tools（设备探测回退）、bash-completion
- **Release 资产四件**：`capsasr-engine-vulkan`、`capsasr-engine-cuda`、预构建包 `capsasr-<ver>-x86_64.pkg.tar.zst` 与 `capsasr-cuda-<ver>-x86_64.pkg.tar.zst`
- **发布顺序**：打 tag → 建 Release 传引擎二进制 → `sha256sum` 填进 PKGBUILD → `makepkg -sf`（需 `pacman -S patchelf`，用于剥离 CUDA 引擎 RUNPATH）→ 预构建包传同一 Release
- **本地验证**：见 PKGBUILD 头注释（makepkg 的 source 不收绝对路径，产物以基名放入 PKGBUILD 目录）
- **namcap 基线**：0 错误；主包仅剩 keyd 等子进程运行依赖误报（静态分析盲区）；capsasr-cuda 仅剩 cublasLt unused（链接器 as-needed 现象）与跨包依赖不可见两条

## 回滚 / 卸载

```bash
capsasr uninstall --purge        # 清自启/服务/进程/数据（--purge 连模型；不加则保留 ~/.capsasr）
sudo capsasr setup-keyd --remove  # CapsLock 恢复原状（可选）
sudo pacman -R capsasr            # 卸载系统包（装了 capsasr-cuda 则 `pacman -R capsasr capsasr-cuda`）
```

## 依赖与许可

- Python ≥ 3.11 标准库（tomllib；**零 pip 依赖**）
- 系统依赖见包 `depends`（pipewire/parec、keyd、wl-clipboard、ydotool+ydotoold、libnotify、Vulkan 加载器）
- 模型权重 Apache-2.0（FunAudioLLM）；本仓库代码 MIT（见 LICENSE）
