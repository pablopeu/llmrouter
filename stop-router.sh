#!/usr/bin/env bash
set -euo pipefail
if [ "${LLMROUTER_SKIP_SYSTEMD_STOP:-}" = "1" ]; then
    exit 0
fi
SCRIPT_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
exec "$SCRIPT_DIR/llmrouter" stop "$@"
