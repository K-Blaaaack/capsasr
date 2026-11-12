# bash completion for capsasr
# 安装位置: /usr/share/bash-completion/completions/capsasr
_capsasr_subcommands="init config doctor cli daemon setup-keyd uninstall"
_capsasr_config_actions="get set edit path list"
_capsasr_config_keys="backend.mode backend.device backend.ngl backend.enc_threads \
models.enc models.llm models.vad engine.language engine.hotwords \
hotkey.layer hotkey.hold_ms notify.enabled notify.channel \
capture.sample_rate capture.preroll_ms capture.trim_db capture.head_pad_ms capture.tail_pad_ms \
capture.min_seconds capture.max_seconds paste.restore_clipboard paste.restore_delay_ms server.socket"

_capsasr() {
    local cur prev cmd="" action="" i
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    for ((i = 1; i < COMP_CWORD; i++)); do
        case "${COMP_WORDS[i]}" in
            init|config|doctor|cli|daemon|setup-keyd|uninstall) cmd="${COMP_WORDS[i]}"; break ;;
        esac
    done

    if [[ -z "$cmd" ]]; then
        COMPREPLY=($(compgen -W "$_capsasr_subcommands -h --help" -- "$cur"))
        return
    fi

    case "$cmd" in
        init)       COMPREPLY=($(compgen -W "--force --yes -h --help" -- "$cur")) ;;
        doctor)     COMPREPLY=($(compgen -W "--verify -h --help" -- "$cur")) ;;
        cli)        COMPREPLY=($(compgen -f -- "$cur")) ;;
        daemon)     COMPREPLY=($(compgen -W "--config -h --help" -- "$cur")) ;;
        setup-keyd) COMPREPLY=($(compgen -W "--hold-ms --remove -h --help" -- "$cur")) ;;
        uninstall)  COMPREPLY=($(compgen -W "--purge -h --help" -- "$cur")) ;;
        config)
            for ((i = 1; i < COMP_CWORD; i++)); do
                case "${COMP_WORDS[i]}" in
                    get|set|edit|path|list) action="${COMP_WORDS[i]}"; break ;;
                esac
            done
            if [[ -z "$action" ]]; then
                COMPREPLY=($(compgen -W "$_capsasr_config_actions" -- "$cur"))
                return
            fi
            case "$action" in
                get) COMPREPLY=($(compgen -W "$_capsasr_config_keys" -- "$cur")) ;;
                set)
                    if [[ "$prev" == "set" ]]; then
                        COMPREPLY=($(compgen -W "$_capsasr_config_keys" -- "$cur"))
                    else
                        case "$prev" in
                            backend.mode)   COMPREPLY=($(compgen -W "auto cuda vulkan cpu" -- "$cur")) ;;
                            notify.enabled|paste.restore_clipboard)
                                            COMPREPLY=($(compgen -W "true false" -- "$cur")) ;;
                            notify.channel)
                                            COMPREPLY=($(compgen -W "desktop dms both" -- "$cur")) ;;
                            engine.language) COMPREPLY=($(compgen -W "中文 英文 日文" -- "$cur")) ;;
                            *)              COMPREPLY=() ;;
                        esac
                    fi ;;
                *) ;;
            esac ;;
    esac
}
complete -F _capsasr capsasr
