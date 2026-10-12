# LLMRouter

Proxy LLM local con LiteLLM que comparte los mismos modelos entre Claude Code y OpenAI Codex CLI.

## Qué hace

Levanta dos servicios locales:

- `127.0.0.1:4000` — proxy LiteLLM (compatible Anthropic/OpenAI) para **Claude Code**
- `127.0.0.1:4001` — Moon Bridge, adaptador del Responses API para **Codex CLI**

El router expone tres **tiers**: `opus` (el mejor), `sonnet` (intermedio) y `haiku` (el más barato/rápido). Vos asignás cada tier a un modelo de cualquier proveedor en un único archivo (`models.yaml`). Claude y Codex usan esa misma asignación. Codex pasa por Moon Bridge porque manda herramientas nativas del Responses API que algunos backends rechazan; el adaptador las filtra y deriva a LiteLLM.

## Arquitectura

```text
Claude Code                          Codex CLI
  ANTHROPIC_BASE_URL=:4000            OPENAI_BASE_URL=:4001/v1
        |                                  |
        v                                  v
LiteLLM Proxy (:4000)               Moon Bridge (:4001)
        \                             /
         \                           /
          v                         v
         Z.AI / DeepSeek / ... (backends definidos en models.yaml)
```

## Empezando

### 1. Configurar `models.yaml`

```bash
cd ~/llmrouter
cp models.yaml.example models.yaml
chmod 600 models.yaml
nano models.yaml
```

Completá:

- `providers.*.api_key` — tus claves de cada proveedor (Z.AI, DeepSeek, etc.).
- `router.master_key` — clave local del proxy (cualquier valor, ej. `sk-myrouter`).

Y asigná los tiers en la sección `tiers` (ver más abajo).

> `models.yaml` contiene claves y por eso está en `.gitignore`. Es la **única** fuente de configuración.

### 2. Instalar

```bash
~/llmrouter/install.sh
```

Crea el venv, instala LiteLLM, compila Moon Bridge, crea los symlinks en `~/.local/bin` e instala el servicio systemd de usuario si está disponible.

### 3. Arrancar

```bash
~/llmrouter/start-router.sh
```

Al arrancar, el router lee `models.yaml` y **genera** `config.yaml` (LiteLLM), `moonbridge-config.yml` y `router.env` desde ese archivo. Esos tres son derivados y se ignoran en git.

O con systemd:

```bash
systemctl --user enable --now llmrouter
```

### 4. Probar

```bash
~/llmrouter/test-router.sh
```

### 5. Usar

```bash
clauder    # Claude Code contra el router
codexr     # Codex CLI contra el router
```

Helper general:

```bash
llmrouter status
llmrouter test
llmrouter claude
llmrouter codex
```

## Comandos

| Comando | Descripción |
|---------|-------------|
| `llmrouter start` | Arrancar LiteLLM y Moon Bridge |
| `llmrouter stop` | Detener ambos |
| `llmrouter restart` | Reiniciar ambos |
| `llmrouter status` | Estado y health checks |
| `llmrouter test` | Probar modelos y el adaptador Codex |
| `clauder` | Lanzar Claude Code contra el router |
| `codexr` | Lanzar Codex CLI contra el router |

## Configurar modelos (reasignar tiers)

Todo se hace en **`models.yaml`**, sección `tiers`. El valor de cada tier es el nombre de un backend definido en `backends`:

```yaml
tiers:
  opus:   glm-5.3            # el mejor
  sonnet: glm-5.3-flash      # intermedio
  haiku:  deepseek-v4-flash  # barato / rápido
```

> **Convención**: la clave de cada backend en `backends` es el **id exacto del
> modelo en el proveedor**. El campo `model` es opcional (vale la clave) y, si
> se define, debe coincidir — el arranque rechaza cualquier divergencia. Así el
> nombre nunca queda desincronizado del modelo que realmente sirve.

Para cambiar qué modelo es `opus`/`sonnet`/`haiku`, editá esa sección y reiniciá:

```bash
~/llmrouter/restart-router.sh
```

Claude y Codex reflejan el cambio a la vez (ambos leen los mismos tiers).

- Claude Code: `claude-mix` expone los tiers como aliases `opus`/`sonnet`/`haiku` (más los nombres canónicos `claude-opus-4-8`, `claude-sonnet-4-6`, `claude-haiku-4-5`).
- Codex: `codex-mix` usa por defecto `codex.default_tier` (ej. `sonnet`). Cambialo en `models.yaml` si querés otro.

## Agregar un proveedor o modelo

Todo en `models.yaml`:

1. Agregá el proveedor en `providers` (con `api_base` y `api_key`). Si el modelo
   necesita endpoint distinto al del formato Anthropic, o tiene metadatos que se
   repiten entre sus modelos, usá `defaults`:

```yaml
providers:
  qwen:
    api_base: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key: your-qwen-api-key
    defaults:
      context_window: 131072
      max_output_tokens: 8192
      supported_reasoning_levels: [low, medium, high]
```

2. Agregá el backend en `backends`: la clave es el **id exacto del modelo** en
   el proveedor (el campo `model` es opcional y debe coincidir si lo definís).

```yaml
backends:
  qwen-coder-plus-latest:
    provider: qwen
    litellm_provider: openai   # openai para APIs OpenAI-compatible; anthropic para formato Anthropic
    display_name: "Qwen Coder"
```

3. Asignalo a un tier en `tiers` (ej. `sonnet: qwen-coder-plus-latest`) y reiniciá.

### Formato del nombre de modelo

LiteLLM usa un prefijo `litellm_provider/model` para saber cómo hablar con el backend:

| Tipo de API | `api_base` | `litellm_provider` |
|-------------|-----------|-------------------|
| OpenAI-compatible | endpoint `/v1` del proveedor | `openai` |
| Anthropic-compatible | endpoint Anthropic del proveedor | `anthropic` |

`litellm_provider` por defecto es `anthropic` (así viene Z.AI y DeepSeek). Usá `openai` para proveedores OpenAI-compatible. Ver los [docs de providers de LiteLLM](https://docs.litellm.ai/docs/providers).

### Validación al arrancar

`start-router.sh` valida `models.yaml` antes de levantar nada: que los tres tiers existan y apunten a backends válidos, que cada backend cite un proveedor con clave, que la clave de cada backend coincida con su campo `model` si está definido, y que el `reasoning_effort` de Codex esté soportado por su tier. Si algo falla, te dice exactamente qué corregir.

## Visión (imágenes)

El router deriva automáticamente los requests con imagen al modelo multimodal
**GLM-5V-Turbo** de z.ai, sin tocar el cliente. Claude Code sigue mandando su
tier (`opus`/`sonnet`/`haiku`) por `/v1/messages`; el hook pre-call de LiteLLM
(`litellm_patches.py`) detecta bloques `type: "image"` en el historial y
reescribe el modelo al alias `vision` **antes** del routing, con lo que el
request sale por el deployment de visión (con su `api_base` y clave propios).
LiteLLM convierte los bloques Anthropic a `image_url` de OpenAI al despachar.

Configuración en `models.yaml` (sección opcional — quitá `vision:` para
desactivar el reroute y volver a texto puro):

```yaml
backends:
  glm-5v-turbo:                              # clave = id del modelo en z.ai
    provider: zai
    api_base: https://api.z.ai/api/paas/v4   # override per-backend del provider
    litellm_provider: openai                 # GLM-5V solo tiene endpoint OpenAI
    display_name: "GLM 5V Turbo (Z.AI)"
    description: "Vision multimodal para los turns con imagen"

vision:
  backend: glm-5v-turbo
```

Notas:

- El escaneo cubre **todo el historial**, no solo el último turno: Claude Code
  reenvía la conversación completa, y una imagen vieja haría 400 en DeepSeek
  (texto puro) si el turno volviera al tier de texto. GLM-5V-Turbo tiene el
  mismo contexto que GLM 5.3 (200K), así que no hay pérdida.
- Los límites de imagen los impone z.ai (~5 MB, JPEG/PNG/GIF/WebP); si falla,
  el error 1210 (图片输入格式) llega directo del backend.
- Codex/Moon Bridge no manda imágenes, así que el camino Codex queda como está.
- Detalle de LiteLLM: el endpoint `/v1/messages` despacha los backends
  `openai/*` por el Responses API, que z.ai no tiene (404 en `/v4/responses`).
  `litellm_patches.py` setea `use_chat_completions_url_for_anthropic_messages`
  para forzar `/chat/completions` en esos deployments.

## Troubleshooting

### Puertos en uso

Por defecto `ROUTER_PORT=4000` y `MOONBRIDGE_PORT=4001` (en `router` y `codex` dentro de `models.yaml`). Cambialos ahí o detené con:

```bash
~/llmrouter/stop-router.sh
```

### Error de DeepSeek: "`content[].thinking` ... must be passed back to the API"

Los modelos de razonamiento de DeepSeek V4 (los backends `deepseek-v4-pro` / `deepseek-v4-flash`, típicamente el tier `haiku`) firman **sus propios** bloques `thinking`. En una conversación multi-turno DeepSeek exige que esos bloques se **reenvíen intactos** en el historial. Claude Code los stripia/redienta (asume que solo los firmados por Anthropic son verificables), así que el turno siguiente llega sin thinking y DeepSeek responde 400:

```
400 The `content[].thinking` in the thinking mode must be passed back to the API.
```

(o la variante `Expected 'thinking' or 'redacted_thinking', but found 'tool_use'`).

El router lo resuelve automáticamente con un callback de LiteLLM (`deepseek_thinking.py`, cargado vía `litellm_settings.callbacks`): cachea los bloques `thinking` de cada respuesta (indexados por un fingerprint estable del prefijo de la conversación) y los **re-inyecta** en los mensajes asistente del historial que los perdieron antes de reenviar a DeepSeek. Es el mismo enfoque que usan Hermes Agent, opencode y claude-code-router.

- No requiere configuración: se activa solo al arrancar.
- El cache es en memoria (se pierde al reiniciar; las nuevas continuaciones dentro de la sesión se reconstruyen solas).
- Actividad visible en `logs/deepseek_thinking.log` (líneas `cacheados ...` / `reinyectados ...`).

### Ruido en `router.log`: `'dict' object has no attribute 'web_search_requests'`

Bug no-fatal de LiteLLM 1.83.x en el cost-tracker: algunos backends (DeepSeek, Z.AI) devuelven `usage.server_tool_use` como dict sin parsear, y el cálculo de costo hace acceso por atributo `.web_search_requests` → `AttributeError` logueada en cada respuesta (rompe el tracking de costo y ensucia el log, pero el request devuelve 200). El router lo cura con un monkeypatch quirúrgico en `litellm_patches.py` (cargado como callback), que coerciona el dict al modelo esperado. Carga confirmable en `logs/litellm_patches.log`.

### Codex falla con herramientas no soportadas

Usá `codexr` (o `llmrouter codex`), no apuntes Codex directo a `:4000`. El `:4001` existe para filtrar herramientas del Responses API que LiteLLM no convierte para estos backends.

### Claude Code ignora el router

Verificá con `clauder`; debería mostrar `ANTHROPIC_BASE_URL=http://127.0.0.1:4000`. Si `~/.claude/settings.json` tiene `ANTHROPIC_BASE_URL` hardcodeado, ese ajuste puede ganar.

### Ver logs

```bash
tail -f ~/llmrouter/logs/router.log
tail -f ~/llmrouter/logs/moonbridge.log
tail -f ~/llmrouter/logs/deepseek_thinking.log   # reinyección de thinking (fix DeepSeek)
```

### Desinstalar

```bash
~/llmrouter/stop-router.sh
systemctl --user disable llmrouter 2>/dev/null || true
rm -f ~/.config/systemd/user/llmrouter.service
systemctl --user daemon-reload 2>/dev/null || true
rm -f ~/.local/bin/llmrouter ~/.local/bin/claude-mix ~/.local/bin/codex-mix
rm -f ~/.local/bin/codexr ~/.local/bin/clauder
rm -rf ~/llmrouter
```
