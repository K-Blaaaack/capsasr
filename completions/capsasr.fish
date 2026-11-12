# fish completion for capsasr
# 安装位置: /usr/share/fish/vendor_completions.d/capsasr.fish

complete -c capsasr -f

# 子命令
complete -c capsasr -n __fish_use_subcommand -a init -d '初始化 ~/.capsasr（模型下载、后端选择、自启）'
complete -c capsasr -n __fish_use_subcommand -a config -d '查看/修改配置'
complete -c capsasr -n __fish_use_subcommand -a doctor -d '环境体检'
complete -c capsasr -n __fish_use_subcommand -a cli -d '经守护进程转写 wav'
complete -c capsasr -n __fish_use_subcommand -a daemon -d '前台运行守护进程'
complete -c capsasr -n __fish_use_subcommand -a setup-keyd -d '配置 keyd 热键（sudo）'
complete -c capsasr -n __fish_use_subcommand -a uninstall -d '清理用户级痕迹（卸载前执行）'

# 参数
complete -c capsasr -n '__fish_seen_subcommand_from init' -l force -d '重新配置'
complete -c capsasr -n '__fish_seen_subcommand_from init' -l yes -d '全默认非交互'
complete -c capsasr -n '__fish_seen_subcommand_from doctor' -l verify -d '完整 sha256 校验'
complete -c capsasr -n '__fish_seen_subcommand_from daemon' -l config -r -d '配置文件'
complete -c capsasr -n '__fish_seen_subcommand_from setup-keyd' -l hold-ms -r -d '按住阈值(ms)'
complete -c capsasr -n '__fish_seen_subcommand_from setup-keyd' -l remove -d '回滚 keyd 配置'
complete -c capsasr -n '__fish_seen_subcommand_from cli' -a '(__fish_complete_suffix .wav)'

# config 子动作
set -l _capsasr_config_actions get set edit path list
complete -c capsasr -n '__fish_seen_subcommand_from config; and not __fish_seen_subcommand_from get set edit path list' -a "$_capsasr_config_actions"

# config 键
set -l _capsasr_config_keys \
    backend.mode backend.device backend.ngl backend.enc_threads \
    models.enc models.llm models.vad \
    engine.language engine.hotwords \
    hotkey.layer hotkey.hold_ms \
    notify.enabled notify.channel \
    capture.sample_rate capture.preroll_ms capture.trim_db capture.head_pad_ms capture.tail_pad_ms \
    capture.min_seconds capture.max_seconds \
    paste.restore_clipboard paste.restore_delay_ms \
    server.socket
complete -c capsasr -n '__fish_seen_subcommand_from config; and __fish_seen_subcommand_from get' -a "$_capsasr_config_keys"
complete -c capsasr -n '__fish_seen_subcommand_from config; and contains -- (commandline -opc)[-1] set' -a "$_capsasr_config_keys"

# config set 的值候选（按上一个 token 判断键名）
complete -c capsasr -n 'contains -- (commandline -opc)[-1] backend.mode' -a 'auto cuda vulkan cpu'
complete -c capsasr -n 'contains -- (commandline -opc)[-1] notify.enabled paste.restore_clipboard' -a 'true false'
complete -c capsasr -n 'contains -- (commandline -opc)[-1] notify.channel' -a 'desktop dms both'
complete -c capsasr -n 'contains -- (commandline -opc)[-1] engine.language' -a '中文 英文 日文'

complete -c capsasr -n '__fish_seen_subcommand_from uninstall' -l purge -d '连 ~/.capsasr（含模型）一起删除'
