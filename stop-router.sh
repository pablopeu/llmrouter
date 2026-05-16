#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOTENV="$SCRIPT_DIR/.env"
PID_FILE="$SCRIPT_DIR/router.pid"
ADAPTER_PID_FILE="$SCRIPT_DIR/codex-adapter.pid"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

if [ -f "$DOTENV" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$DOTENV"
    set +a
fi
: "${ROUTER_PORT:=4000}"
: "${CODEX_ADAPTER_PORT:=4001}"

stop_pid_file() {
    local pid_file="$1"
    local label="$2"
    if [ ! -f "$pid_file" ]; then
        return 1
    fi

    local pid
    pid="$(cat "$pid_file")"
    if kill -0 "$pid" 2>/dev/null; then
        info "Deteniendo $label (PID $pid) ..."
        kill "$pid"
        for _ in $(seq 1 10); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$pid" 2>/dev/null; then
            warn "$label no respondio, forzando kill ..."
            kill -9 "$pid" 2>/dev/null || true
        fi
        info "$label detenido."
    else
        warn "PID $pid de $label no esta corriendo."
    fi
    rm -f "$pid_file"
    return 0
}

stop_port() {
    local port="$1"
    local label="$2"
    local pid
    pid=$(ss -tlnp 2>/dev/null | grep ":${port} " | grep -oP 'pid=\K[0-9]+' | head -1 || true)
    if [ -n "$pid" ]; then
        warn "Encontrado $label en puerto $port (PID $pid), deteniendo ..."
        kill "$pid"
        info "$label detenido."
        return 0
    fi
    return 1
}

# --- intentar systemd primero ---
if [ -z "${LLMROUTER_SKIP_SYSTEMD_STOP:-}" ] && command -v systemctl &>/dev/null && systemctl --user status &>/dev/null 2>&1; then
    if systemctl --user is-active llmrouter &>/dev/null 2>&1; then
        info "Deteniendo via systemd --user ..."
        systemctl --user stop llmrouter
        info "Servicio detenido."
    elif systemctl --user is-active claude-router &>/dev/null 2>&1; then
        info "Deteniendo servicio legacy claude-router via systemd --user ..."
        systemctl --user stop claude-router
        info "Servicio legacy detenido."
    fi
fi

# --- matar procesos manuales ---
STOPPED=0
stop_pid_file "$ADAPTER_PID_FILE" "adaptador Codex" && STOPPED=1 || true
stop_pid_file "$PID_FILE" "router" && STOPPED=1 || true
stop_port "$CODEX_ADAPTER_PORT" "adaptador Codex" && STOPPED=1 || true
stop_port "$ROUTER_PORT" "router" && STOPPED=1 || true

if [ "$STOPPED" = "0" ]; then
    info "Router no esta corriendo."
fi
