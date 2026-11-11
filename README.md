# LLMRouter para Claude Code

Un proxy local LiteLLM, supervisado por systemd, permite elegir proveedores para
los tiers `opus`, `sonnet` y `haiku` sin cambiar de cliente.

```text
Claude Code → LiteLLM :4000 → proveedor del tier
                         ↘ fallback si falla el primario
                         ↘ modelo de visión si el historial contiene imágenes
```

`models.yaml` es la única fuente de configuración y contiene secretos. No editar
`config.yaml` ni `router.env`: se regeneran al iniciar. No hay Moon Bridge,
adaptador Codex, base de datos ni segundo puerto. El uso independiente de Codex
y sus archivos en `~/.codex` no se modifica.

## Uso

```bash
clauder                         # permisos normales de Claude Code
clauder --unsafe                # opción explícita: omitir permisos de Claude
llmrouter claude --model opus
llmrouter status                # estado y readiness; no consume cuota LLM
llmrouter check                 # valida configuración sin escribir ni reiniciar
llmrouter test                  # pruebas offline, incluidos proveedores simulados
llmrouter test --live           # inferencias reales: consume cuota
llmrouter restart               # valida antes de reiniciar y espera readiness
llmrouter stop
llmrouter start
```

`claude-mix` y los scripts `start/stop/restart/status/test-router.sh` se conservan
como wrappers del mismo controlador. Los enlaces en `~/.local/bin` funcionan
desde cualquier directorio. No se activan permisos peligrosos por defecto.

## Instalación

Requisitos: Linux con `systemd --user`, Python con soporte de `venv` y Claude
Code instalado. El servicio incluido espera el checkout en `~/llmrouter`.

```bash
cd ~/llmrouter
./install.sh
# Completar las claves en models.yaml; no compartir ese archivo.
./llmrouter check
./llmrouter start
systemctl --user enable llmrouter.service   # opcional: arranque al iniciar sesión
```

El instalador fija LiteLLM y PyYAML en `requirements.txt`; `constraints.txt`
registra las versiones del entorno validado. No descarga ni compila Moon Bridge.
No reinicia un router activo automáticamente. Para actualizar código o
dependencias, probar primero y reiniciar cuando no haya respuestas en curso.

Sin systemd se puede ejecutar en primer plano:

```bash
.venv/bin/python routerctl.py run
```

Ese modo no tiene recuperación automática. No ejecutarlo mientras otro proxy
esté usando el mismo puerto.

## Configuración

Consultar `models.yaml.example`. Ejemplo mínimo ilustrativo, con claves ficticias:

```yaml
providers:
  primary:
    api_base: https://primary.example/anthropic
    api_key: completar-clave-primaria
    max_parallel_requests: 1
  secondary:
    api_base: https://secondary.example/anthropic
    api_key: completar-clave-secundaria
backends:
  modelo-primario:
    provider: primary
  deepseek-flash:
    provider: secondary
tiers:
  opus: modelo-primario
  sonnet: modelo-primario
  haiku: deepseek-flash
router:
  master_key: completar-clave-local
  host: 127.0.0.1
  port: 4000
  num_retries: 0
  fallbacks:
    opus: haiku
    sonnet: haiku
```

- La clave del backend es el identificador exacto del modelo del proveedor.
- `providers.<nombre>.defaults` permite heredar opciones; el backend puede
  sobreescribir `api_base` y `litellm_provider` (`anthropic` u `openai`).
- `vision.backend` selecciona el backend multimodal y `vision.fallback`, si se
  configura, uno distinto como respaldo. La instalación actual usa DeepSeek
  Flash para visión y GLM-5V como respaldo; los consumos se cobran por proveedor.
- No se permiten claves nulas, puertos inválidos, backends inexistentes ni
  ciclos de fallback. Una configuración inválida no reemplaza la anterior.
- Se exponen los tiers y los aliases canónicos `claude-opus-4-8`,
  `claude-sonnet-4-6`, `claude-haiku-4-5`, sin duplicar deployments por alias.
- Los campos heredados de Codex se ignoran; no se genera su catálogo.

Los archivos generados se publican mediante reemplazos atómicos individuales,
con permisos `0600`; un lock serializa generaciones simultáneas. El proceso
carga su configuración al arrancar: editar YAML no altera solicitudes activas.
El servicio usa `UMask=0077` y reinicia ante una caída inesperada. Un stop normal
permite terminar solicitudes durante hasta 120 segundos, antes de forzar salida.

## Compatibilidad y resiliencia

`litellm_patches.py` concentra los hooks específicos de la versión fijada:

- Historial MCP: convierte `mcp_tool_use`/`mcp_tool_result` históricos a bloques
  compatibles, conservando metadatos, resultados e imágenes. No transforma una
  llamada MCP histórica en una nueva herramienta pendiente de ejecución.
- Herramientas huérfanas: conserva el payload como texto histórico en lugar de
  descartarlo; los pares válidos mantienen su estructura.
- Visión: detecta imágenes también dentro de resultados de herramientas. Con
  DeepSeek mueve imágenes históricas del asistente al siguiente turno de usuario,
  sin descartar los datos ni separar llamadas y resultados de herramientas.
- Cuota `429/1308`: intenta el fallback; recuerda la cuenta agotada durante
  300 segundos por endpoint para no insistir en cada tier. Después vuelve a probar.
- Thinking DeepSeek: caché por endpoint, modelo, credencial hasheada, sistema,
  herramientas, prefijo y respuesta real del asistente. Soporta respuestas
  normales y streaming; límites de 512 entradas, 8 MiB y una hora de TTL.
- Se conservan los retries acotados de compatibilidad de thinking y el parche
  dict-safe del registro de costos de búsquedas.

La caché vive solo en memoria, usa SHA-256 incremental y no mezcla respuestas
de distintos backends. Los logs propios registran conteos, no prompts ni claves;
rotan a 2 MiB con tres copias por logger. La sonda antigua de URLs de imágenes
se retiró para no registrar URLs firmadas.

**El router no elimina una cuota del proveedor.** Si tanto el primario como el
fallback están agotados o caídos, devuelve el error. El cooldown es local,
temporal y se borra al reiniciar; no interpreta la zona horaria del reset remoto.

## Verificación y diagnóstico

```bash
./llmrouter check
./llmrouter test
./llmrouter status
journalctl --user -u llmrouter.service -n 80 --no-pager
```

Las pruebas offline cubren configuración y publicación, permisos, wrappers,
aislamiento y límites de caché, MCP, cuota, aliases, herramientas, streaming y
visión. La integración levanta otro proxy en un puerto efímero con proveedores
locales simulados; no usa claves reales ni reinicia el router operativo.
`test --live` devuelve salida no cero si falla un tier.

No usar `/health` como chequeo rutinario: puede invocar proveedores. El
controlador consulta `/health/readiness` y `/v1/models`, con autenticación.
Si Claude ignora el router, revisar variables `ANTHROPIC_*` y configuración del
cliente. Si está en curso una sesión larga, el reinicio puede vaciar su caché de
thinking; permanecen los mecanismos de recuperación compatibles.

Consultar `RUNBOOK.md` para cambios de tiers y recuperación. No publicar
`models.yaml`, `config.yaml`, `router.env`, `.env`, logs ni respaldos de `data/`.
