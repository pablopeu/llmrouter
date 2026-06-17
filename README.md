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
  opus:   deepseek-v4-pro   # el mejor
  sonnet: glm-52            # intermedio
  haiku:  deepseek-v4-flash # barato / rápido
```

Para cambiar qué modelo es `opus`/`sonnet`/`haiku`, editá esa sección y reiniciá:

```bash
~/llmrouter/restart-router.sh
```

Claude y Codex reflejan el cambio a la vez (ambos leen los mismos tiers).

- Claude Code: `claude-mix` expone los tiers como aliases `opus`/`sonnet`/`haiku` (más los nombres canónicos `claude-opus-4-8`, `claude-sonnet-4-6`, `claude-haiku-4-5`).
- Codex: `codex-mix` usa por defecto `codex.default_tier` (ej. `sonnet`). Cambialo en `models.yaml` si querés otro.

## Agregar un proveedor o modelo

Todo en `models.yaml`:

1. Agregá el proveedor en `providers` (con `api_base` y `api_key`):

```yaml
providers:
  qwen:
    api_base: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key: your-qwen-api-key
```

2. Agregá el backend en `backends` (proveedor + nombre real del modelo + metadatos que usa Moon Bridge):

```yaml
backends:
  qwen-coder:
    provider: qwen
    model: qwen-coder-plus-latest
    litellm_provider: openai        # openai para APIs OpenAI-compatible; anthropic para formato Anthropic
    display_name: "Qwen Coder"
    context_window: 131072
    max_output_tokens: 8192
    default_reasoning_level: medium
    supported_reasoning_levels: [low, medium, high]
```

3. Asignalo a un tier en `tiers` (ej. `sonnet: qwen-coder`) y reiniciá.

### Formato del nombre de modelo

LiteLLM usa un prefijo `litellm_provider/model` para saber cómo hablar con el backend:

| Tipo de API | `api_base` | `litellm_provider` |
|-------------|-----------|-------------------|
| OpenAI-compatible | endpoint `/v1` del proveedor | `openai` |
| Anthropic-compatible | endpoint Anthropic del proveedor | `anthropic` |

`litellm_provider` por defecto es `anthropic` (así viene Z.AI y DeepSeek). Usá `openai` para proveedores OpenAI-compatible. Ver los [docs de providers de LiteLLM](https://docs.litellm.ai/docs/providers).

### Validación al arrancar

`start-router.sh` valida `models.yaml` antes de levantar nada: que los tres tiers existan y apunten a backends válidos, que cada backend cite un proveedor con clave, y que el `reasoning_effort` de Codex esté soportado por su tier. Si algo falla, te dice exactamente qué corregir.

## Troubleshooting

### Puertos en uso

Por defecto `ROUTER_PORT=4000` y `MOONBRIDGE_PORT=4001` (en `router` y `codex` dentro de `models.yaml`). Cambialos ahí o detené con:

```bash
~/llmrouter/stop-router.sh
```

### Codex falla con herramientas no soportadas

Usá `codexr` (o `llmrouter codex`), no apuntes Codex directo a `:4000`. El `:4001` existe para filtrar herramientas del Responses API que LiteLLM no convierte para estos backends.

### Claude Code ignora el router

Verificá con `clauder`; debería mostrar `ANTHROPIC_BASE_URL=http://127.0.0.1:4000`. Si `~/.claude/settings.json` tiene `ANTHROPIC_BASE_URL` hardcodeado, ese ajuste puede ganar.

### Ver logs

```bash
tail -f ~/llmrouter/logs/router.log
tail -f ~/llmrouter/logs/moonbridge.log
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
