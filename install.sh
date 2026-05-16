#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
DOTENV="$SCRIPT_DIR/.env"
DOTENV_EXAMPLE="$SCRIPT_DIR/.env.example"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# --- .env ---
if [ ! -f "$DOTENV" ]; then
    cp "$DOTENV_EXAMPLE" "$DOTENV"
    chmod 600 "$DOTENV"
    info "Creado .env desde .env.example con permisos 600."
    warn "DEBES editar $DOTENV con tus API keys antes de arrancar el router."
    MUST_EDIT_ENV=1
else
    chmod 600 "$DOTENV"
    info ".env ya existe, permisos 600 asegurados."
    MUST_EDIT_ENV=0
fi

# --- venv ---
if [ ! -d "$VENV_DIR" ]; then
    info "Creando virtualenv en $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
else
    info "Virtualenv ya existe en $VENV_DIR."
fi

# --- instalar litellm ---
info "Instalando litellm[proxy] en el venv ..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet 'litellm[proxy]!=1.82.7,!=1.82.8'
info "litellm instalado: $("$VENV_DIR/bin/python" -c 'import litellm; print(litellm.__version__)' 2>/dev/null || echo 'version desconocida')"

# --- comandos en PATH ---
TARGET_BIN=""
for candidate in "$HOME/.local/bin" "$HOME/bin"; do
    if echo ":$PATH:" | grep -q ":$candidate:"; then
        TARGET_BIN="$candidate"
        break
    fi
done

if [ -z "$TARGET_BIN" ]; then
    TARGET_BIN="$HOME/.local/bin"
    mkdir -p "$TARGET_BIN"
    warn "$TARGET_BIN no estaba en PATH. Agregalo a tu ~/.bashrc:"
    warn "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

for command_name in llmrouter llmrouter-claude llmrouter-codex claude-mix codex-mix codexr clauder; do
    if [ -L "$TARGET_BIN/$command_name" ] || [ -f "$TARGET_BIN/$command_name" ]; then
        info "$command_name ya existe en $TARGET_BIN, actualizando symlink."
        rm -f "$TARGET_BIN/$command_name"
    fi
    ln -s "$SCRIPT_DIR/$command_name" "$TARGET_BIN/$command_name"
    info "Symlink $command_name -> $TARGET_BIN/$command_name"
done

# --- systemd user service ---
SYSTEMD_DIR="$HOME/.config/systemd/user"
if command -v systemctl &>/dev/null && systemctl --user status &>/dev/null 2>&1; then
    mkdir -p "$SYSTEMD_DIR"
    SERVICE_SRC="$SCRIPT_DIR/systemd/llmrouter.service"
    SERVICE_DST="$SYSTEMD_DIR/llmrouter.service"
    if [ -f "$SERVICE_SRC" ]; then
        cp "$SERVICE_SRC" "$SERVICE_DST"
        systemctl --user disable claude-router 2>/dev/null || true
        rm -f "$SYSTEMD_DIR/claude-router.service"
        systemctl --user daemon-reload
        info "Servicio systemd user instalado."
    else
        warn "No se encontro $SERVICE_SRC, saltando instalacion de servicio."
    fi
else
    warn "systemctl --user no disponible. El router se manejara con scripts manuales."
fi

# --- resumen ---
echo ""
echo "========================================="
echo "  LLMRouter - Instalacion completa"
echo "========================================="
echo ""
echo "Archivos en: $SCRIPT_DIR"
echo ""
if [ "$MUST_EDIT_ENV" = "1" ]; then
    echo -e "${RED}PROXIMO PASO OBLIGATORIO:${NC}"
    echo "  1) Edita tus API keys:"
    echo "     nano $DOTENV"
    echo ""
fi
echo "  2) Arranca el router:"
echo "     $SCRIPT_DIR/start-router.sh"
echo ""
echo "  3) Prueba que funciona:"
echo "     $SCRIPT_DIR/test-router.sh"
echo ""
echo "  4) Usa Claude Code con el router:"
echo "     llmrouter-claude"
echo ""
echo "  5) Usa Codex CLI con el mismo router:"
echo "     llmrouter-codex"
echo ""
echo "Comandos utiles:"
echo "  start-router.sh    - arrancar"
echo "  stop-router.sh     - detener"
echo "  restart-router.sh  - reiniciar"
echo "  status-router.sh   - ver estado"
echo "  test-router.sh     - probar endpoints"
echo "  llmrouter          - helper start/stop/status/test/claude/codex"
echo "  llmrouter-claude   - lanzar Claude Code"
echo "  llmrouter-codex    - lanzar Codex CLI"
echo ""
echo "Para cambiar modelos, edita $DOTENV"
echo "y reinicia con restart-router.sh"
echo "========================================="
