# Revisión integral y migración — 11 de noviembre de 2026

## Resultado operativo

- Un único proceso LiteLLM en `127.0.0.1:4000`, supervisado por systemd.
- Migración realizada a las 17:25:25, hora Argentina, esperando dos chequeos
  consecutivos sin conexiones establecidas. Ventana stop → readiness: 3.487 ms.
- Servicio `Type=exec`, `Restart=on-failure`, PID real y cero reinicios
  inesperados al terminar la verificación. Stop con hasta 120 segundos de gracia.
- Moon Bridge detenido; puerto 4001 cerrado. Artefactos y base anterior archivados
  en `data/retired-moonbridge-20261111-172323/`, directorio privado `0700`.
- Se retiraron únicamente los enlaces propios `codex-mix` y `codexr`.
  No se modificaron configuraciones ni catálogos de `~/.codex`.
- No se actualizaron dependencias en el entorno operativo ni se cambiaron
  proveedores, claves, tiers o modelo de visión.

## Hallazgos y correcciones

| Hallazgo reproducido | Corrección |
|---------------------|------------|
| Cuota 429/1308 seguida de historial MCP rechazado por el fallback | Normalización previa al envío, conservando contenido; cobertura de fallback normal y SSE. |
| Requests repetidos contra una cuenta agotada | Cooldown de cinco minutos por endpoint y credencial hasheada, compartido por sus tiers. |
| Aliases canónicos sin fallback efectivo al quitar deployments duplicados | Aliases de grupos y entradas de fallback explícitas para nombres canónicos; integración verificada. |
| Dos caminos operativos, aunque Codex no se usa por el router | Eliminación de Moon Bridge, adaptador, catálogo y launcher Codex del código activo. |
| Servicio oneshot aparecía activo sin supervisar un PID principal | Servicio de ejecución directa, recuperación automática y apagado con gracia. |
| Arranque/parada duplicados con pidfiles y señales por puerto | Controlador único; systemd gestiona solo su cgroup, sin matar procesos ajenos. |
| Wrappers fallaban al invocarlos por symlink desde otro directorio | Resolución del destino real; prueba desde un directorio temporal con espacios. |
| Lanzador omitía permisos de Claude por defecto | Permisos normales por defecto; `--unsafe` como opción explícita. |
| Health operativo podía consumir cuota | Readiness y listado autenticado de modelos, sin inferencias implícitas. |
| Smoke test podía devolver éxito aunque todas las inferencias fallaran | Tests offline por defecto y `--live` explícito con salida no cero ante fallos. |
| Generación aceptaba claves nulas, ciclos y valores inválidos | Validación previa, lock y reemplazos atómicos individuales con permisos 0600. |
| Caché de thinking podía mezclar modelos/respuestas con el mismo prefijo | Aislamiento por endpoint, credencial, modelo, sistema, herramientas y respuesta; SHA-256 incremental. |
| Thinking sin presupuesto de bytes/TTL | 512 entradas, 8 MiB, TTL de una hora y LRU, con payload copiado. |
| Historial MCP normalizado distinto entre guardado y lookup | Preparación al despachar al proveedor efectivo, también durante fallback. |
| Tool blocks huérfanos se descartaban | Conversión a historial textual sin perder argumentos, resultados o imágenes. |
| Imágenes dentro de resultados de herramientas no se detectaban | Detección recursiva y prueba de visión real contra un proveedor simulado. |
| Logs propios sin rotación y sonda de URLs firmadas | Logs privados, rotación acotada y retiro de la sonda. |
| Instalación de versiones flotantes y compilación remota de Moon Bridge | Dependencias directas fijadas y constraints del entorno validado; sin compilación Go. |

## Simplificación medida

El código operativo pasó de 2.575 a 1.287 líneas: reducción del **50%**,
contando scripts Python/shell y launchers, incluyendo el nuevo controlador y
logging, sin contar documentación, configuración ni tests. Se elimina un daemon,
un puerto y su base de datos del camino operativo.

El fingerprint nuevo de un contexto sintético de 1,2 MB tardó aproximadamente
3 ms por pasada en este equipo. Es una microprueba local, no una garantía de
latencia de requests completos. LiteLLM sigue siendo el principal costo de
memoria; comparar RSS antes/después con distinta carga no sería una medición
controlada. Reemplazarlo por un proxy casero implicaría recuperar routing,
streaming y conversiones de protocolo: no se hizo para evitar regresiones.

## Verificación

- 34 pruebas automatizadas pasan antes y después de la migración.
- La integración levanta un proxy separado y proveedores locales simulados:
  aliases, 429/1308, MCP, fallback normal/SSE, caché de thinking normal/SSE y visión.
- Validación de YAML real sin mostrar secretos, `pip check`, compilación Python,
  sintaxis shell, verificación de la unidad systemd y `git diff --check` correctos.
- Readiness y modelos operativos; credencial inválida rechazada. El wrapper
  instalado invoca Claude Code desde `/tmp` correctamente.
- No se ejecutaron inferencias pagas de prueba ni se mató deliberadamente el
  proxy operativo para probar la recuperación; se verificó su política de restart.

## Límites que permanecen

1. El router no puede ampliar cuotas externas: si todos los backends están
   agotados, el error debe llegar al cliente. El cooldown se pierde al reiniciar.
2. Durante la verificación, tráfico operativo recibió `1261: Prompt exceeds max
   length` del backend de visión. No es el error MCP original. Como el historial
   conserva imágenes, se mantiene el routing multimodal y puede superar el
   contexto admitido por ese proveedor. No se truncó ni resumió automáticamente
   contenido del usuario. Posteriormente se configuró DeepSeek Flash, que admite
   visión y una ventana mayor, con GLM-5V como respaldo. Si el contexto supera
   ambos límites, sigue siendo necesaria la compactación explícita.
3. Los límites `max_parallel_requests` siguen siendo por deployment, no una
   garantía global de concurrencia de la cuenta del proveedor. Se eliminaron
   deployments duplicados por alias; una cuota entre modelos puede seguir
   provocando rate limits, atendidos por el fallback.
4. Los parches dependen de interfaces internas de LiteLLM 1.83.14. Actualizarlo
   exige volver a pasar la integración antes de reemplazar el proceso activo.
5. El apagado con gracia no es hot reload: streams que superen el timeout de
   stop pueden cortarse. Por eso se eligió una pausa sin conexiones.

Operación y recuperación: `README.md` y `RUNBOOK.md`.
