# Runbook — Reasignar tiers del router

Procedimiento probado para cambiar qué modelo responde como `opus` / `sonnet` /
`haiku` (ej. hecho el 2026-08-26: opus → `glm-5.3`, sonnet → `glm-5.3-flash`,
haiku → `deepseek-v4-flash`). Los detalles completos están en `README.md`; este
documento es el flujo corto y las trampas.

## Regla de oro

**Todo vive en `models.yaml`** (única fuente, contiene claves, está en .gitignore).
De ahí se generan solos `config.yaml`, `moonbridge-config.yml` y `router.env`
al arrancar, y sirven para ambos caminos: Claude Code (`:4000`) y Codex /
Moon Bridge (`:4001`). Nunca editar los archivos generados a mano.

## Convención inviolable: la clave del backend ES el id del modelo

La clave de cada entrada de `backends` es el **id exacto que usa el proveedor**
(`glm-5.3`, `deepseek-v4-flash`), sin apodos ni traducciones. Así el nombre no
puede desincronizarse del modelo servido, y el generador (`generate-configs.py`)
valida al arrancar que si alguien define `model:` coincida con la clave.
(Historia: antes existía el backend `glm-52` sirviendo `glm-5.3`; esa trampa se
eliminó estructuralmente el 2026-08-26 con esta convención.)

Los metadatos repetibles de una familia (`context_window`,
`supported_reasoning_levels`, etc.) van como `defaults:` bajo el proveedor;
un backend nuevo típico son ~3 líneas porque hereda todo lo demás.

## Flujo

### 1. Ver el estado actual

```bash
grep -A4 '^tiers:' ~/llmrouter/models.yaml
```

La clave de cada tier es ya el id del modelo; solo hace falta verificar que el
backend exista en `backends` y su proveedor tenga clave válida.

### 2. Si el modelo deseado aún no existe como backend: agregarlo

Copiar el bloque de **otro backend del mismo proveedor** y cambiar solamente:

- `model:` — nombre real del modelo en el proveedor
- `display_name:` / `description:`

```yaml
backends:
  glm-5.3-flash:            # ← clave = id EXACTO del modelo en el proveedor
    provider: zai           # los metadatos vienen de providers.zai.defaults
    display_name: "GLM 5.3 Flash (Z.AI)"
    description: "GLM 5.3 Flash via Z.AI"
```

No definir `context_window` / `max_output_tokens` / reasoning levels salvo
diferencias reales respecto a `providers.<p>.defaults`. La mayoría de los
backends van por el endpoint
Anthropic del proveedor (por defecto); si el modelo solo existe en endpoint
OpenAI-compatible, ver `glm-5v-turbo` para el override con `api_base` +
`litellm_provider: openai`.

### 3. Asignar los tiers

```yaml
tiers:
  opus: glm-5.3              # el mejor
  sonnet: glm-5.3-flash      # intermedio
  haiku: deepseek-v4-flash   # barato / rapido
```

### 4. Chequear invariantes ANTES de reiniciar

- **Vision no se toca**: la sección `vision:` y el backend `glm-5v-turbo`
  reenrutan imágenes independientemente de los tiers. Solo se modifican si el
  pedido explícito incluye visión.
- **`codex.reasoning_effort` debe existir en `supported_reasoning_levels` del
  backend asignado a `codex.default_tier`** (hoy: sonnet). Niveles por familia:
  GLM `[low, medium, high]`, DeepSeek V4 `[high, xhigh]`. Cambiar `effort`
  acorde si cambia la familia (hoy `high` — sirve para ambas).
- **Fallbacks unidireccionales**: mantener `opus → haiku` y `sonnet → haiku`.
  NUNCA agregar `haiku: sonnet` (loop sonnet↔haiku cuando el primario cae).
- `start-router.sh` valida todo esto al arrancar y dice qué corregir si falla,
  pero es más barato verificarlo uno mismo primero.

### 5. Reiniciar y verificar el mapeo impreso

```bash
~/llmrouter/restart-router.sh
```

La salida imprime el mapeo efectivo — confirmarlo ahí:

```
opus   -> glm-5.3 (anthropic/glm-5.3 @ zai)
sonnet -> glm-53-flash (anthropic/glm-5.3-flash @ zai)
haiku  -> deepseek-v4-flash (...)
vision -> glm-5v-turbo (...)     ← verificar que siga intacto
codex default tier: sonnet
```

### 6. Smoke test

```bash
~/llmrouter/test-router.sh
```

Esperado: health HTTP 200, los tres tiers HTTP 200, "Vision reroute HTTP 200",
"Codex adapter HTTP 200".

## Contexto no-obvio (para no redescubrirlo)

| Decisión | Por qué |
|----------|---------|
| `num_retries: 0` | Intencional: failover inmediato al fallback, no esperar retries de un provider caído/rate-limited. |
| `max_parallel_requests: 1` en z.ai | Serializa contra Z.AI para no gatillar su rate-limit transitorio 1302. No subirlo sin probar. |
| Nombres `claude-*` que expone el router | Son aliases fijos de LiteLLM (`claude-opus-4-8`, etc.) para que Claude Code los acepte; tras ellos está el tier real según `models.yaml`. |
| Cliente usa el router | Usar `clauder` / `codexr` (o `llmrouter claude/codex`). Si Claude Code ignora el router, hay `ANTHROPIC_BASE_URL` hardcodeado en `~/.claude/settings.json`. |
| Errores DeepSeek de thinking multi-turno | Resueltos automáticamente (`deepseek_thinking.py`); diagnosticables en `logs/deepseek_thinking.log`. No configuran. |
