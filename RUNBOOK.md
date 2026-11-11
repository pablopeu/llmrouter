# Operación del router

## Cambiar tiers sin sorpresas

1. Editar únicamente `models.yaml`; conservar una copia privada previa.
2. La clave de cada backend debe ser su ID exacto en el proveedor.
3. Asignar `tiers.opus`, `tiers.sonnet` y `tiers.haiku` a backends existentes.
4. Mantener `vision.backend` salvo que se quiera cambiar visión explícitamente.
5. Conservar fallbacks unidireccionales: `opus → haiku`, `sonnet → haiku`.
6. Validar y probar antes de tocar el servicio:

```bash
cd ~/llmrouter
./llmrouter check
./llmrouter test
```

En una pausa entre solicitudes:

```bash
./llmrouter restart
./llmrouter status
```

La validación del controlador sucede antes del reinicio. El proceso genera
archivos privados al arrancar. systemd da hasta 120 segundos al proceso para
cerrar las solicitudes; no es un hot reload ni garantiza cero interrupciones
para streams más largos. No existe un segundo proceso Moon Bridge que reiniciar.

## Errores frecuentes

| Síntoma | Acción |
|---------|--------|
| `429`, código `1308` | Cuota del proveedor agotada: fallback automático y cooldown local de cinco minutos. Esperar reset remoto si ambos proveedores fallan. |
| `unknown variant mcp_tool_use` | Verificar que el proceso cargó el código actualizado; la prueba de integración reproduce este caso. |
| Thinking multi-turn | Revisar `logs/deepseek_thinking.log`; no borrar historial. Caché acotada y retries compatibles están activos. |
| Imágenes rechazadas | Verificar backend de visión y journal; no activar sondas que registren URLs firmadas. |
| Visión: `1261`, `Prompt exceeds max length` | DeepSeek Flash tiene una ventana mayor y GLM-5V queda como respaldo, pero ambos tienen límites. Si ambos rechazan el contexto, compactar explícitamente la conversación; no se recorta automáticamente. |
| `models.yaml` inválido | Corregir sin editar archivos generados; `llmrouter check` no modifica el servicio. |
| Puerto ocupado | Revisar `ss -ltnp`; no matar procesos ajenos por número de puerto. |
| Proxy caído | `systemctl --user status llmrouter`; systemd reinicia fallos inesperados automáticamente. |

## Logs y pruebas

```bash
journalctl --user -u llmrouter.service -n 80 --no-pager
./llmrouter test                 # offline; no cuota
./llmrouter test --live          # tres inferencias reales; no ejecutar en bucle
```

No enviar logs o YAML sin revisar/redactar secretos. El chequeo de estado no
usa `/health`, porque puede consumir cuota llamando a proveedores.

## Recuperación

Si un cambio de configuración falla, restaurar la copia privada de `models.yaml`,
validar con `llmrouter check` y reiniciar. Si falla un cambio de código, restaurar
la versión de código validada junto con sus archivos de dependencias; no hacer
upgrades de LiteLLM durante una incidencia. Conservar las credenciales existentes.

En la migración a un único proxy se archivan los artefactos locales de Moon Bridge
en `data/retired-moonbridge-*`, con acceso privado, después de detener su proceso.
Se conserva el respaldo para recuperación; no se toca `~/.codex`.

Para diagnosticar un arranque fallido sin systemd, detener primero el servicio y
ejecutar `.venv/bin/python routerctl.py run` en primer plano. Al terminar, cerrar
ese proceso y volver a `llmrouter start`; nunca levantar ambos en el mismo puerto.
