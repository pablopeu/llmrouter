#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS_YAML="$SCRIPT_DIR/models.yaml"
VENV_DIR="$SCRIPT_DIR/.venv"
GENERATOR="$SCRIPT_DIR/generate-configs.py"
CONFIG="$SCRIPT_DIR/config.yaml"
ROUTER_ENV="$SCRIPT_DIR/router.env"
LOG_DIR="$SCRIPT_DIR/logs"
DATA_DIR="$SCRIPT_DIR/data"
PID_FILE="$SCRIPT_DIR/router.pid"
MOON_PID_FILE="$SCRIPT_DIR/moonbridge.pid"
MOON_CONFIG="$SCRIPT_DIR/moonbridge-config.yml"
MOON_BIN="$SCRIPT_DIR/moonbridge"
PY="$VENV_DIR/bin/python"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# --- models.yaml ---
if [ ! -f "$MODELS_YAML" ]; then
    error "No existe $MODELS_YAML"
    error "Copia models.yaml.example a models.yaml y completa tus claves."
    exit 1
fi

# --- venv + generador ---
if [ ! -x "$PY" ]; then
    error "No se encontro python del venv en $PY"
    error "Ejecuta install.sh primero."
    exit 1
fi
if [ ! -f "$GENERATOR" ]; then
    error "No se encontro el generador $GENERATOR"
    exit 1
fi

# --- Moon Bridge ---
if [ ! -x "$MOON_BIN" ]; then
    error "Moon Bridge no encontrado en $MOON_BIN"
    error "Ejecuta install.sh primero."
    exit 1
fi

LITELLM_BIN="$VENV_DIR/bin/litellm"
if [ ! -x "$LITELLM_BIN" ]; then
    error "litellm no encontrado en $LITELLM_BIN"
    error "Ejecuta install.sh primero."
    exit 1
fi

# --- log y data dirs ---
mkdir -p "$LOG_DIR" "$DATA_DIR"

# --- generar config.yaml, moonbridge-config.yml y router.env desde models.yaml ---
info "Generando configs desde $MODELS_YAML ..."
"$PY" "$GENERATOR" \
    --models "$MODELS_YAML" \
    --config "$CONFIG" \
    --moon "$MOON_CONFIG" \
    --router-env "$ROUTER_ENV" \
    --data-dir "$DATA_DIR"

# --- cargar router.env ---
set -a
# shellcheck disable=SC1090
source "$ROUTER_ENV"
set +a

: "${ROUTER_HOST:=127.0.0.1}"
: "${ROUTER_PORT:=4000}"
: "${MOONBRIDGE_HOST:=$ROUTER_HOST}"
: "${MOONBRIDGE_PORT:=4001}"
: "${LITELLM_MASTER_KEY:=}"
: "${CODEX_DEFAULT_MODEL:=sonnet}"

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

# --- generar models_catalog.json para Codex ---
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
mkdir -p "$CODEX_HOME"
info "Generando models_catalog.json en $CODEX_HOME ..."
"$MOON_BIN" \
    --config "$MOON_CONFIG" \
    --codex-home "$CODEX_HOME" \
    --codex-base-url "http://${MOONBRIDGE_HOST}:${MOONBRIDGE_PORT}/v1" \
    --print-codex-config "$CODEX_DEFAULT_MODEL" \
    > /dev/null 2>&1
info "models_catalog.json generado."

# --- arrancar Moon Bridge para Codex ---
if is_listening "$MOONBRIDGE_PORT"; then
    if [ -f "$MOON_PID_FILE" ] && kill -0 "$(cat "$MOON_PID_FILE")" 2>/dev/null; then
        warn "Moon Bridge ya corriendo (PID $(cat "$MOON_PID_FILE")) en puerto $MOONBRIDGE_PORT."
    else
        warn "Puerto $MOONBRIDGE_PORT ya esta escuchando; asumo que Moon Bridge esta activo."
    fi
else
    info "Arrancando Moon Bridge en http://${MOONBRIDGE_HOST}:${MOONBRIDGE_PORT} ..."
    nohup setsid "$MOON_BIN" \
        --config "$MOON_CONFIG" \
        > "$LOG_DIR/moonbridge.log" 2>&1 &
    echo $! > "$MOON_PID_FILE"
    wait_for_port "$MOONBRIDGE_PORT" "Moon Bridge" "$LOG_DIR/moonbridge.log"
    info "Moon Bridge arrancado (PID $(cat "$MOON_PID_FILE")) en http://${MOONBRIDGE_HOST}:${MOONBRIDGE_PORT}"
fi
