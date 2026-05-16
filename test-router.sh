#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOTENV="$SCRIPT_DIR/.env"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[--]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*"; }

# cargar config
if [ ! -f "$DOTENV" ]; then
    fail "No existe $DOTENV"
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "$DOTENV"
set +a

: "${ROUTER_HOST:=127.0.0.1}"
: "${ROUTER_PORT:=4000}"
: "${CODEX_ADAPTER_HOST:=$ROUTER_HOST}"
: "${CODEX_ADAPTER_PORT:=4001}"
BASE="http://${ROUTER_HOST}:${ROUTER_PORT}"
CODEX_BASE="http://${CODEX_ADAPTER_HOST}:${CODEX_ADAPTER_PORT}"

echo "=== LLMRouter - Test ==="
echo ""

# 1) Health
echo "--- Health check ---"
RESP=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/health" \
    -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" 2>/dev/null || echo "000")
if [ "$RESP" = "200" ]; then
    info "Health endpoint: HTTP 200"
else
    fail "Health endpoint: HTTP $RESP (esperado 200)"
    fail "El router no esta respondiendo. Arrancalo con start-router.sh"
    exit 1
fi

# 2) Modelos disponibles
echo ""
echo "--- Modelos registrados ---"
MODELS_RESP=$(curl -s "$BASE/v1/models" \
    -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" 2>/dev/null || echo "{}")
echo "$MODELS_RESP" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    models = [m.get('id','?') for m in data.get('data', [])]
    if models:
        for m in models:
            print(f'  - {m}')
    else:
        print('  (no se encontraron modelos)')
except Exception:
    print('  (respuesta no valida)')
" 2>/dev/null

# 3) Test por modelo - Anthropic-compatible endpoint
echo ""
echo "--- Test de modelos (Anthropic /v1/messages) ---"

test_model() {
    local model="$1"
    local label="$2"
    echo -n "  $label ($model): "
    RESP=$(curl -s -w "\n%{http_code}" "$BASE/v1/messages" \
        -H "Content-Type: application/json" \
        -H "x-api-key: ${LITELLM_MASTER_KEY}" \
        -H "anthropic-version: 2023-06-01" \
        -d "{\"model\":\"$model\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"Responde solo: OK\"}]}" \
        --max-time 30 2>/dev/null || echo -e "\n000")

    HTTP_CODE=$(echo "$RESP" | tail -1)
    BODY=$(echo "$RESP" | sed '$d')

    if [ "$HTTP_CODE" = "200" ]; then
        # extraer texto de la respuesta Anthropic
        CONTENT=$(echo "$BODY" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    blocks = d.get('content', [])
    texts = [b.get('text','') for b in blocks if b.get('type') == 'text']
    print(texts[0][:60] if texts else '(sin texto)')
except Exception:
    print('(respuesta no parseable)')
" 2>/dev/null)
        info "HTTP 200 - $CONTENT"
    else
        # mostrar error sin claves
        ERR=$(echo "$BODY" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    e = d.get('error', {})
    print(e.get('message', str(d))[:120])
except Exception:
    print(sys.stdin.read()[:120] if hasattr(sys.stdin,'read') else 'error desconocido')
" 2>/dev/null || echo "HTTP $HTTP_CODE")
        fail "HTTP $HTTP_CODE - $ERR"
    fi
}

test_model "zai-sonnet" "Z.AI Sonnet"
test_model "deepseek-opus" "DeepSeek Opus"
test_model "deepseek-haiku" "DeepSeek Haiku"

echo ""
echo "--- Test Codex adapter (OpenAI /v1/responses) ---"
RESP=$(curl -s -w "\n%{http_code}" "$CODEX_BASE/v1/responses" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
    -d '{"model":"deepseek-haiku","input":"Responde solo: OK","max_output_tokens":64,"stream":false,"tools":[{"type":"namespace","name":"unsupported_probe"}]}' \
    --max-time 45 2>/dev/null || echo -e "\n000")

HTTP_CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')

if [ "$HTTP_CODE" = "200" ]; then
    CONTENT=$(echo "$BODY" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    texts = []
    for item in d.get('output', []):
        if item.get('type') == 'message':
            for block in item.get('content', []):
                if block.get('type') == 'output_text':
                    texts.append(block.get('text') or '')
    print((texts[0] if texts else '(sin texto)')[:80])
except Exception:
    print('(respuesta no parseable)')
" 2>/dev/null)
    info "Codex adapter HTTP 200 - $CONTENT"
else
    ERR=$(echo "$BODY" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    e = d.get('error', {})
    print(e.get('message', str(d))[:160])
except Exception:
    print(sys.stdin.read()[:160] if hasattr(sys.stdin,'read') else 'error desconocido')
" 2>/dev/null || echo "HTTP $HTTP_CODE")
    fail "Codex adapter HTTP $HTTP_CODE - $ERR"
fi

echo ""
echo "=== Test completo ==="
