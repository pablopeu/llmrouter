#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOTENV="$SCRIPT_DIR/.env"
VENV_DIR="$SCRIPT_DIR/.venv"
CONFIG="$SCRIPT_DIR/config.yaml"
LOG_DIR="$SCRIPT_DIR/logs"
PID_FILE="$SCRIPT_DIR/router.pid"
ADAPTER_PID_FILE="$SCRIPT_DIR/codex-adapter.pid"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# --- cargar .env ---
if [ ! -f "$DOTENV" ]; then
    error "No existe $DOTENV"
    error "Copia .env.example a .env y completa tus API keys."
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "$DOTENV"
set +a

# --- validar claves ---
MISSING=0
for var in ZAI_API_KEY DEEPSEEK_API_KEY LITELLM_MASTER_KEY; do
    VAL="${!var:-}"
    if [ -z "$VAL" ] || [[ "$VAL" == tu-api-key-* ]] || [[ "$VAL" == *change-me* ]]; then
        error "$var no esta configurada. Edita $DOTENV"
        MISSING=1
    fi
done

if [ "$MISSING" = "1" ]; then
    error "Faltan claves. No se arranca el router."
    exit 1
fi

# --- defaults ---
: "${ROUTER_HOST:=127.0.0.1}"
: "${ROUTER_PORT:=4000}"
: "${CODEX_ADAPTER_HOST:=$ROUTER_HOST}"
: "${CODEX_ADAPTER_PORT:=4001}"

# --- verificar venv ---
if [ ! -d "$VENV_DIR" ]; then
    error "No existe el virtualenv en $VENV_DIR"
    error "Ejecuta install.sh primero."
    exit 1
fi

LITELLM_BIN="$VENV_DIR/bin/litellm"
if [ ! -x "$LITELLM_BIN" ]; then
    error "litellm no encontrado en $LITELLM_BIN"
    error "Ejecuta install.sh primero."
    exit 1
fi

# --- log dir ---
mkdir -p "$LOG_DIR"

is_listening() {
    local port="$1"
    ss -tlnp 2>/dev/null | grep -q ":${port} "
}

wait_for_port() {
    local port="$1"
    local name="$2"
    local log_file="$3"
    local timeout=30
    for _ in $(seq 1 "$timeout"); do
        if is_listening "$port"; then
            return 0
        fi
        sleep 1
    done
    error "$name no respondio en ${timeout}s. Revisa $log_file"
    return 1
}

# --- arrancar LiteLLM ---
if is_listening "$ROUTER_PORT"; then
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        warn "Router ya corriendo (PID $(cat "$PID_FILE")) en puerto $ROUTER_PORT."
    else
        warn "Puerto $ROUTER_PORT ya esta escuchando; asumo que LiteLLM esta activo."
    fi
else
    info "Arrancando LiteLLM proxy en http://${ROUTER_HOST}:${ROUTER_PORT} ..."
    nohup setsid "$LITELLM_BIN" \
        --config "$CONFIG" \
        --host "$ROUTER_HOST" \
        --port "$ROUTER_PORT" \
        > "$LOG_DIR/router.log" 2>&1 &
    echo $! > "$PID_FILE"
    wait_for_port "$ROUTER_PORT" "LiteLLM proxy" "$LOG_DIR/router.log"
    info "Router arrancado (PID $(cat "$PID_FILE")) en http://${ROUTER_HOST}:${ROUTER_PORT}"
fi

# --- arrancar adaptador para Codex ---
if is_listening "$CODEX_ADAPTER_PORT"; then
    if [ -f "$ADAPTER_PID_FILE" ] && kill -0 "$(cat "$ADAPTER_PID_FILE")" 2>/dev/null; then
        warn "Adaptador Codex ya corriendo (PID $(cat "$ADAPTER_PID_FILE")) en puerto $CODEX_ADAPTER_PORT."
    else
        warn "Puerto $CODEX_ADAPTER_PORT ya esta escuchando; asumo que el adaptador Codex esta activo."
    fi
else
    info "Arrancando adaptador Codex en http://${CODEX_ADAPTER_HOST}:${CODEX_ADAPTER_PORT} ..."
    nohup setsid "$VENV_DIR/bin/python" "$SCRIPT_DIR/codex-adapter.py" \
        > "$LOG_DIR/codex-adapter.log" 2>&1 &
    echo $! > "$ADAPTER_PID_FILE"
    wait_for_port "$CODEX_ADAPTER_PORT" "Adaptador Codex" "$LOG_DIR/codex-adapter.log"
    info "Adaptador Codex arrancado (PID $(cat "$ADAPTER_PID_FILE")) en http://${CODEX_ADAPTER_HOST}:${CODEX_ADAPTER_PORT}"
fi
