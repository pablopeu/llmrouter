#!/usr/bin/env bash
set -euo pipefail
umask 077
SCRIPT_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
if ! command -v systemctl >/dev/null || ! systemctl --user show-environment >/dev/null 2>&1; then
    echo "Se requiere systemd --user. Alternativa en primer plano: .venv/bin/python routerctl.py run" >&2
    exit 1
fi
if [ ! -f "$SCRIPT_DIR/models.yaml" ]; then
    cp "$SCRIPT_DIR/models.yaml.example" "$SCRIPT_DIR/models.yaml"
fi
chmod 600 "$SCRIPT_DIR/models.yaml"
if [ ! -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    python3 -m venv "$SCRIPT_DIR/.venv"
fi
"$SCRIPT_DIR/.venv/bin/python" -m pip install -r "$SCRIPT_DIR/requirements.txt" -c "$SCRIPT_DIR/constraints.txt"
mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user"
for command_name in llmrouter claude-mix clauder; do
    destination="$HOME/.local/bin/$command_name"
    if [ -e "$destination" ] && [ ! -L "$destination" ]; then
        echo "No se reemplaza archivo ajeno: $destination" >&2
        exit 1
    fi
    ln -sfn "$SCRIPT_DIR/$command_name" "$destination"
done
for command_name in codex-mix codexr; do
    destination="$HOME/.local/bin/$command_name"
    if [ -L "$destination" ] && [ "$(readlink "$destination")" = "$SCRIPT_DIR/$command_name" ]; then
        rm "$destination"
    fi
done
cp "$SCRIPT_DIR/systemd/llmrouter.service" "$HOME/.config/systemd/user/llmrouter.service"
systemctl --user daemon-reload
echo "Instalado. Editar models.yaml y ejecutar llmrouter check; llmrouter start."
echo "Si ya estaba activo, llmrouter restart aplica los cambios."
