#!/usr/bin/env bash
# service.sh - 跨平台 service 管理（macOS launchd / Linux systemd --user）
#
# 子命令:
#   install     安装并启动
#   uninstall   停用并删除 unit
#   start       启动
#   stop        停止
#   restart     重启
#   status      查看状态
#   logs        跟踪日志
#
# 用法:
#   ./service.sh [--host 127.0.0.1] [--port 8765] <subcommand> [options]
set -euo pipefail

# ========== 常量 ==========
LABEL="com.local.stock-monitor"
SERVICE_NAME="stock-monitor"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
TEMPLATE_DIR="$SCRIPT_DIR/service"
PLIST_TEMPLATE="$TEMPLATE_DIR/${LABEL}.plist.template"
UNIT_TEMPLATE="$TEMPLATE_DIR/${SERVICE_NAME}.service.template"

DEFAULT_HOST="0.0.0.0"
DEFAULT_PORT="8765"

# ========== 工具 ==========
die() { echo "✗ $*" >&2; exit 1; }
info() { echo "✓ $*"; }

usage() {
    cat <<'EOF'
用法: service.sh [--host HOST] [--port PORT] <subcommand>

默认绑定 0.0.0.0 (局域网可访问)。仅本机请用 --host 127.0.0.1。

子命令:
  install                       安装并启动服务
  uninstall                     停用并删除 unit
  start                         启动
  stop                          停止
  restart                       重启
  status                        查看状态
  logs [--stdout|--stderr]      跟踪日志
  -h, --help                    显示本帮助
EOF
}

# ========== 平台 & 路径 ==========
detect_platform() {
    case "$(uname -s)" in
        Darwin) echo "darwin" ;;
        Linux)  echo "linux" ;;
        *)      echo "unsupported" ;;
    esac
}

launchd_plist_path() {
    echo "$HOME/Library/LaunchAgents/${LABEL}.plist"
}

launchd_log_dir() {
    echo "$HOME/Library/Logs/${SERVICE_NAME}"
}

systemd_unit_path() {
    local xdg="${XDG_CONFIG_HOME:-$HOME/.config}"
    echo "$xdg/systemd/user/${SERVICE_NAME}.service"
}

systemd_log_dir() {
    echo "$SCRIPT_DIR/logs/systemd"
}

config_path() {
    local plat; plat="$(detect_platform)"
    if [[ "$plat" == "darwin" ]]; then
        echo "$HOME/Library/Application Support/${SERVICE_NAME}/config.json"
    else
        local xdg="${XDG_CONFIG_HOME:-$HOME/.config}"
        echo "$xdg/${SERVICE_NAME}/config.json"
    fi
}

detect_python() {
    local candidates=(
        "$PROJECT_ROOT/.venv/bin/python"
        "$PROJECT_ROOT/.venv/bin/python3"
    )
    for c in "${candidates[@]}"; do
        if [[ -x "$c" ]]; then
            echo "$c"
            return
        fi
    done
    local sys_py
    sys_py="$(command -v python3 2>/dev/null || true)"
    if [[ -n "$sys_py" ]]; then
        echo "$sys_py"
    else
        echo "python3"
    fi
}

# ========== 模板渲染（envsubst） ==========
render_template() {
    local template="$1" output="$2"
    LABEL="$LABEL" \
    SERVICE_NAME="$SERVICE_NAME" \
    VENV_PYTHON="$(detect_python)" \
    PROJECT_ROOT="$PROJECT_ROOT" \
    CONFIG_PATH="$(config_path)" \
    HOST="$HOST" \
    PORT="$PORT" \
    SYSTEM_PATH="${PATH:-/usr/local/bin:/usr/bin:/bin}" \
    envsubst < "$template" > "$output"
}

_log_dir_for_render() {
    local plat; plat="$(detect_platform)"
    if [[ "$plat" == "darwin" ]]; then
        launchd_log_dir
    else
        systemd_log_dir
    fi
}

# ========== 平台操作 ==========

macos_install() {
    local plist log_dir
    plist="$(launchd_plist_path)"
    log_dir="$(launchd_log_dir)"
    mkdir -p "$(dirname "$plist")" "$log_dir"

    if [[ -f "$plist" ]]; then
        info "已存在 $plist，先停掉再覆盖"
        macos_stop || true
    fi

    render_template "$PLIST_TEMPLATE" "$plist"
    info "写入 $plist"
    macos_start
    info "已启动，UI: http://${HOST}:${PORT}"
    info "日志:   $log_dir/stdout.log"
}

macos_uninstall() {
    macos_stop || true
    local plist; plist="$(launchd_plist_path)"
    if [[ -f "$plist" ]]; then
        rm "$plist"
        info "删除 $plist"
    fi
}

macos_start() {
    local plist; plist="$(launchd_plist_path)"
    [[ -f "$plist" ]] || die "未找到 $plist，请先 install"
    local domain="gui/$(id -u)"
    if ! launchctl bootstrap "$domain" "$plist" 2>/dev/null; then
        launchctl load -w "$plist"
    fi
}

macos_stop() {
    local plist; plist="$(launchd_plist_path)"
    [[ -f "$plist" ]] || return 0
    local domain="gui/$(id -u)"
    if ! launchctl bootout "$domain" "$plist" 2>/dev/null; then
        launchctl unload "$plist" 2>/dev/null || true
    fi
}

macos_restart() {
    macos_stop
    macos_start
}

macos_status() {
    local plist; plist="$(launchd_plist_path)"
    info "plist: $plist"
    if [[ ! -f "$plist" ]]; then
        echo "(未安装)"
        return
    fi
    launchctl list | grep -F "$LABEL" || echo "(未在 launchd 列表中)"
}

linux_install() {
    local unit
    unit="$(systemd_unit_path)"
    mkdir -p "$(dirname "$unit")"

    if [[ -f "$unit" ]]; then
        info "已存在 $unit，先停掉再覆盖"
        linux_stop || true
    fi

    render_template "$UNIT_TEMPLATE" "$unit"
    info "写入 $unit"
    systemctl --user daemon-reload
    if ! systemctl --user enable --now "$SERVICE_NAME"; then
        die "systemctl 启动失败"
    fi
    info "已启动，UI: http://${HOST}:${PORT}"
    info "日志:   journalctl --user -u ${SERVICE_NAME} -f"
}

linux_uninstall() {
    if [[ -f "$(systemd_unit_path)" ]]; then
        systemctl --user disable --now "$SERVICE_NAME" 2>/dev/null || true
    fi
    local unit; unit="$(systemd_unit_path)"
    if [[ -f "$unit" ]]; then
        rm "$unit"
        info "删除 $unit"
    fi
    systemctl --user daemon-reload 2>/dev/null || true
}

linux_start()    { systemctl --user start    "$SERVICE_NAME"; }
linux_stop()     { systemctl --user stop     "$SERVICE_NAME"; }
linux_restart()  { systemctl --user restart  "$SERVICE_NAME"; }

linux_status() {
    local unit; unit="$(systemd_unit_path)"
    info "unit: $unit"
    if [[ ! -f "$unit" ]]; then
        echo "(未安装)"
        return
    fi
    systemctl --user status "$SERVICE_NAME" --no-pager || true
}

logs() {
    local plat; plat="$(detect_platform)"
    if [[ "$plat" == "linux" ]]; then
        journalctl --user -u "$SERVICE_NAME" -f -n 100 "$@"
        return
    fi

    local log_dir; log_dir="$(launchd_log_dir)"
    local files=()
    if [[ "$LOGS_STDOUT_ONLY" == "1" ]]; then
        files+=("$log_dir/stdout.log")
    elif [[ "$LOGS_STDERR_ONLY" == "1" ]]; then
        files+=("$log_dir/stderr.log")
    else
        files+=("$log_dir/stdout.log" "$log_dir/stderr.log")
    fi

    local existing=()
    for f in "${files[@]}"; do
        if [[ -f "$f" ]]; then existing+=("$f"); fi
    done
    [[ ${#existing[@]} -gt 0 ]] || die "日志文件不存在: $log_dir"

    tail -f -n 100 "${existing[@]}"
}

# ========== main ==========
main() {
    HOST="$DEFAULT_HOST"
    PORT="$DEFAULT_PORT"
    CMD=""
    LOGS_STDOUT_ONLY=0
    LOGS_STDERR_ONLY=0

    # 解析顶层参数
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --host)  HOST="$2"; shift 2 ;;
            --port)  PORT="$2"; shift 2 ;;
            -h|--help) usage; exit 0 ;;
            install|uninstall|start|stop|restart|status)
                CMD="$1"; shift; break ;;
            logs)
                CMD="logs"; shift; break ;;
            *)
                usage; die "未知参数: $1" ;;
        esac
    done

    [[ -n "$CMD" ]] || { usage; exit 1; }

    # logs 子命令的额外参数
    if [[ "$CMD" == "logs" ]]; then
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --stdout) LOGS_STDOUT_ONLY=1; shift ;;
                --stderr) LOGS_STDERR_ONLY=1; shift ;;
                *) die "logs 未知参数: $1" ;;
            esac
        done
    fi

    local plat; plat="$(detect_platform)"
    case "$plat" in
        darwin)
            case "$CMD" in
                install)   macos_install ;;
                uninstall) macos_uninstall ;;
                start)     macos_start ;;
                stop)      macos_stop ;;
                restart)   macos_restart ;;
                status)    macos_status ;;
                logs)      logs ;;
            esac ;;
        linux)
            case "$CMD" in
                install)   linux_install ;;
                uninstall) linux_uninstall ;;
                start)     linux_start ;;
                stop)      linux_stop ;;
                restart)   linux_restart ;;
                status)    linux_status ;;
                logs)      logs ;;
            esac ;;
        *)
            die "不支持的平台: $(uname -s)" ;;
    esac
}

main "$@"
