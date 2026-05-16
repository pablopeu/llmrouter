#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOTENV="$SCRIPT_DIR/.env"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }

# cargar puerto
if [ -f "$DOTENV" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$DOTENV"
    set +a
fi
: "${ROUTER_HOST:=127.0.0.1}"
: "${ROUTER_PORT:=4000}"
: "${CODEX_ADAPTER_HOST:=$ROUTER_HOST}"
: "${CODEX_ADAPTER_PORT:=4001}"

echo "=== LLMRouter Status ==="
echo ""

# --- systemd ---
if command -v systemctl &>/dev/null && systemctl --user status &>/dev/null 2>&1; then
    if systemctl --user is-active llmrouter &>/dev/null 2>&1; then
        info "systemd: activo"
    else
        warn "systemd: inactivo (no instalado o detenido)"
    fi
else
    warn "systemd --user: no disponible"
fi

# --- puertos ---
if ss -tlnp 2>/dev/null | grep -q ":${ROUTER_PORT} "; then
    info "Puerto ${ROUTER_PORT}: escuchando"
else
    echo -e "${RED}[INFO]${NC} Puerto ${ROUTER_PORT}: NO escuchando"
fi

# --- PID files ---
PID_FILE="$SCRIPT_DIR/router.pid"
if [ -f "$PID_FILE" ]; then
    PID="$(cat "$PID_FILE")"
    if kill -0 "$PID" 2>/dev/null; then
        info "PID: $PID (corriendo)"
    else
        warn "PID: $PID (muerto, stale PID file)"
    fi
else
    warn "PID file: no existe"
fi

ADAPTER_PID_FILE="$SCRIPT_DIR/codex-adapter.pid"
if ss -tlnp 2>/dev/null | grep -q ":${CODEX_ADAPTER_PORT} "; then
    info "Puerto ${CODEX_ADAPTER_PORT} (Codex): escuchando"
else
    echo -e "${RED}[INFO]${NC} Puerto ${CODEX_ADAPTER_PORT} (Codex): NO escuchando"
fi

if [ -f "$ADAPTER_PID_FILE" ]; then
    PID="$(cat "$ADAPTER_PID_FILE")"
    if kill -0 "$PID" 2>/dev/null; then
        info "Codex PID: $PID (corriendo)"
    else
        warn "Codex PID: $PID (muerto, stale PID file)"
    fi
else
    warn "Codex PID file: no existe"
fi

# --- health endpoint ---
AUTH_ARGS=()
if [ -n "${LITELLM_MASTER_KEY:-}" ]; then
    AUTH_ARGS=(-H "Authorization: Bearer ${LITELLM_MASTER_KEY}")
fi
RESP=$(curl -s --max-time 5 -o /dev/null -w "%{http_code}" "http://${ROUTER_HOST}:${ROUTER_PORT}/health" "${AUTH_ARGS[@]}" 2>/dev/null || true)
: "${RESP:=000}"
if [ "$RESP" = "200" ]; then
    info "LiteLLM health: OK (HTTP 200)"
elif [ "$RESP" = "000" ]; then
    echo -e "${RED}[INFO]${NC} LiteLLM health: sin conexion"
else
    warn "LiteLLM health: HTTP $RESP"
fi

RESP=$(curl -s --max-time 5 -o /dev/null -w "%{http_code}" "http://${CODEX_ADAPTER_HOST}:${CODEX_ADAPTER_PORT}/health" 2>/dev/null || true)
: "${RESP:=000}"
if [ "$RESP" = "200" ]; then
    info "Codex adapter health: OK (HTTP 200)"
elif [ "$RESP" = "000" ]; then
    echo -e "${RED}[INFO]${NC} Codex adapter health: sin conexion"
else
    warn "Codex adapter health: HTTP $RESP"
fi

echo ""
echo "============================"
