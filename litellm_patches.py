#!/usr/bin/env python3
"""Parches puntuales sobre LiteLLM para bugs que pegaban en este router.

Carga
-----
LiteLLM importa este modulo via `get_instance_fn` al registrar
`litellm_patches_handler` en `litellm_settings.callbacks` (config.yaml). Los
parches se aplican al importarse (top-level). No upgrades de LiteLLM: son fixes
quirurgicos, reversibles y atados al repo, para no arriesgar la config que ya
funciona (incluido el fix de thinking de DeepSeek).

Bug arreglado
-------------
AttributeError: 'dict' object has no attribute 'web_search_requests'
  en litellm_core_utils/llm_cost_calc/tool_call_cost_tracking.py,
  funcion `StandardBuiltInToolCostTracking.response_object_includes_web_search_call`.

Causa: algunos backends (DeepSeek, Z.AI) devuelven `usage.server_tool_use` como
un *dict* sin parsear a `ServerToolUse` (pydantic). El cost-tracker hace acceso
por atributo `usage.server_tool_use.web_search_requests` y levanta AttributeError.
Es no-fatal (LiteLLM lo atrapa y loguea, el request devuelve 200), pero rompe el
tracking de costos y ensucia `router.log` en CADA respuesta antropica.

Fix: envolver la funcion para coercer el dict a `ServerToolUse` antes de delegar
al original. Como red de seguridad, si algo igual falla, devuelve False en vez de
levantantar (el cost-tracking nunca debe tirar el logging).

Reroute de vision
----------------
Si models.yaml define seccion 'vision', generate-configs.py setea
LLMROUTER_VISION_MODEL con el alias del deployment de vision (ej. GLM-5V-Turbo
via el endpoint OpenAI-compatible de z.ai). El pre-call hook detecta bloques
de imagen en el historial y reescribe el modelo a ese alias ANTES del routing:
Claude Code sigue mandando su tier (opus/sonnet/haiku) y el request sale por
el modelo multimodal sin tocar el cliente. Sin la env var, el hook no toca nada.

Sonda de imagenes (diagnostico, opcional)
-----------------------------------------
Activada con LLMROUTER_PROBE_IMAGES=1. Solo loguea (read-only, nunca modifica
el request) cualquier URL firmada / imageSource que cruce el router hacia el
backend, con repr() exacto para detectar mangling de '+' -> espacio en la
Signature del CDN de z.ai (sospecha del error 1210 图片输入格式/解析错误).

Dos resultados posibles, ambos diagnosticos:
  - loguea una URL con '+'  -> el router la reenvia intacta; el break esta
    en el fetch que hace z.ai de su propio CDN.
  - no loguea ninguna URL    -> la imagen viaja como base64 (o z.ai genera la
    URL firmada server-side); el router nunca la toca.
En ambos casos exonera al router. Salida: logs/image_probe.log.
"""
from __future__ import annotations

import logging
import os
import uuid

from litellm.integrations.custom_logger import CustomLogger
import litellm

# El endpoint /v1/messages de LiteLLM despacha los backends openai/* por el
# Responses API; z.ai solo expone /chat/completions y responde 404 en
# /v4/responses. Este flag global vuelve al camino chat/completions para los
# deployments openai/azure (los anthropic/* no se ven afectados).
litellm.use_chat_completions_url_for_anthropic_messages = True

logger = logging.getLogger("litellm_patches")
try:
    _log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(_log_dir, exist_ok=True)
    _fh = logging.FileHandler(os.path.join(_log_dir, "litellm_patches.log"))
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_fh)
    logger.setLevel(logging.INFO)
    logger.propagate = False
except Exception:
    pass


# --- Sonda de imagenes (diagnostico, gated por LLMROUTER_PROBE_IMAGES) -----
_PROBE_LOGGER = logging.getLogger("litellm_patches.image_probe")
try:
    _probe_fh = logging.FileHandler(os.path.join(_log_dir, "image_probe.log"))
    _probe_fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _PROBE_LOGGER.addHandler(_probe_fh)
    _PROBE_LOGGER.setLevel(logging.INFO)
    _PROBE_LOGGER.propagate = False
except Exception:
    pass


def _walk_strings(obj: Any, path: str = "$") -> Iterable[tuple]:
    """Itera (path, str) por cada string dentro de una estructura dict/list."""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_strings(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, f"{path}[{i}]")


# Subcadenas que delatan una URL firmada del CDN de z.ai o el arg imageSource.
_IMAGE_TELLTALES = (
    "Signature=", "imageSource", "ufileos.com",
    "screen.png", "UCloudPublicKey",
)


def _patch_thinking_error_detection() -> None:
    """Reconoce el 400 de DeepSeek 'content[].thinking must be passed back'.

    Causa: al caer de GLM/Z.AI (sonnet) a DeepSeek (haiku) en un fallback, la
    conversacion trae bloques thinking ajenos (o ausentes, tras strippearlos
    Claude Code). DeepSeek exige reenviar SUS propios bloques y responde 400 con
    ese mensaje, anulando el fallback (Claude Code queda en retry).

    LiteLLM ya tiene un mecanismo nativo que, cuando detecta un 400 de thinking
    recuperable, strippea los bloques thinking del request y reintenta (bounded
    por max_retry_on_anthropic_messages_http_error). Pero ese detector solo
    reconoce el error "Invalid signature in thinking block". Este patch amplia
    la deteccion para que el 400 de DeepSeek tambien dispare el strip+retry:
    el request se reenvia sin bloques thinking ni modo thinking, DeepSeek lo
    acepta y el fallback funciona (sin razonamiento, pero operativo).
    """
    try:
        from litellm.llms.anthropic import common_utils as cu
    except Exception as e:  # estructura inesperada de LiteLLM: no romper el arranque
        logger.warning("no se pudo importar anthropic.common_utils: %s", e)
        return

    if getattr(cu, "_llmrouter_thinking_error_patched", False):
        return

    orig = cu.is_anthropic_invalid_thinking_signature_error

    def wrapped(error_text):
        try:
            if orig(error_text):
                return True
            if not error_text:
                return False
            t = error_text.lower()
            return "must be passed back" in t and "thinking" in t
        except Exception:
            return False

    cu.is_anthropic_invalid_thinking_signature_error = wrapped
    cu._llmrouter_thinking_error_patched = True
    logger.info(
        "patch aplicado: 400 'content[].thinking must be passed back' "
        "(DeepSeek) se trata como recuperable -> strip thinking + retry."
    )


_patch_thinking_error_detection()


# Texto placeholder para bloques thinking fabricados. DeepSeek no valida el
# contenido de `thinking` ni la firma (firma UUID propia, sin check criptografico),
# solo exige la PRESENCIA del bloque antes de un tool_use en modo razonamiento.
_PLACEHOLDER_THINKING_TEXT = (
    "Razonamiento previo a la llamada de herramienta."
)


def _inject_placeholder_thinking(request_data):
    """Inserta un bloque `thinking` placeholder antes de cada `tool_use`.

    DeepSeek (endpoint /anthropic) responde 400 con "content[].thinking must be
    passed back" si un assistant message lleva un `tool_use` sin su bloque
    `thinking` previo. Esto anulaba el fallback z.ai -> deepseek cuando la
    conversacion tenia llamadas a herramientas (el thinking que Claude Code
    reenvia es de GLM/Claude, no de deepseek, asi que el cache de
    deepseek_thinking esta frio y no hay bloque reinyectable).

    El strip+retry nativo de LiteLLM no cura este caso: quita los bloques
    thinking y el parametro `thinking`, pero deepseek por defecto sigue en modo
    razonamiento y vuelve a exigir el bloque. En vez de strippear, reinyectamos
    un bloque placeholder (firma UUID aleatoria), que deepseek acepta: la
    conversacion queda operativa en el fallback (sin razonamiento real, pero el
    request sale 200).
    """
    msgs = request_data.get("messages") if isinstance(request_data, dict) else None
    if not isinstance(msgs, list):
        return
    for msg in msgs:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        has_tu = any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in content
        )
        if not has_tu:
            continue
        has_th = any(
            isinstance(b, dict) and b.get("type") == "thinking" for b in content
        )
        if has_th:
            continue
        placeholder = {
            "type": "thinking",
            "thinking": _PLACEHOLDER_THINKING_TEXT,
            "signature": str(uuid.uuid4()),
        }
        msg["content"] = [placeholder] + content


def _patch_anthropic_thinking_retry_transform() -> None:
    """Inyecta thinking placeholder en el retry nativo de errores de thinking.

    LiteLLM, ante un 400 de thinking recuperable, strippea los bloques thinking
    del request y reintenta una vez. Para DeepSeek eso no alcanza (sigue en modo
    razonamiento y exige el bloque). Este patch envuelve el transformador del
    retry: mantiene el strip original y, cuando el error es el de DeepSeek
    ("must be passed back"), reinyecta un bloque placeholder antes de cada
    tool_use. Asi el reintento llega con el bloque que deepseek exige.
    """
    try:
        from litellm.llms.base_llm.anthropic_messages import transformation as tmod
    except Exception as e:  # estructura inesperada de LiteLLM: no romper el arranque
        logger.warning("no se pudo importar anthropic_messages.transformation: %s", e)
        return

    cls = tmod.BaseAnthropicMessagesConfig
    if getattr(cls, "_llmrouter_thinking_retry_patched", False):
        return

    orig = cls.transform_anthropic_messages_request_on_http_error

    def wrapped(self, e, request_data):  # type: ignore[no-untyped-def]
        try:
            orig(self, e, request_data)  # strip nativo (quita thinking + param)
            text = ""
            try:
                text = getattr(e, "response", None).text or ""
            except Exception:
                text = ""
            if "must be passed back" in text.lower():
                _inject_placeholder_thinking(request_data)
        except Exception as ex:
            logger.warning(
                "inject de thinking en retry fallo (no rompe el request): %s", ex
            )

    cls.transform_anthropic_messages_request_on_http_error = wrapped  # type: ignore[assignment]
    cls._llmrouter_thinking_retry_patched = True
    logger.info(
        "patch aplicado: retry de thinking de DeepSeek inyecta bloque "
        "placeholder antes de cada tool_use."
    )


_patch_anthropic_thinking_retry_transform()


def _repair_tool_adjacency(messages):
    """Repara la adyacencia tool_use/tool_result que exigen backends estrictos.

    DeepSeek (endpoint /anthropic) responde 400 con "tool_use ids were found
    without tool_result blocks immediately after" si un assistant message lleva
    un `tool_use` cuyo `tool_result` no esta en el mensaje INMEDIATAMENTE
    siguiente. Eso anulaba el fallback z.ai -> deepseek: el request caido caia
    a deepseek y deepseek lo rechazaba, y el usuario veia el error igual.

    Regla (no-op sobre conversaciones validas: Anthropic ya exige la adyacencia
    y Claude Code la cumple):
      - tool_use sin su tool_result en el mensaje siguiente -> se elimina (el
        turno queda coherente: es como si el modelo no hubiera llamado la tool).
      - tool_result sin su tool_use en el mensaje anterior -> se elimina.
    """
    if not isinstance(messages, list):
        return messages

    def _tu_ids(content):
        return {
            b.get("id") for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")
        }

    def _tr_ids(content):
        return {
            b.get("tool_use_id") for b in content
            if isinstance(b, dict) and b.get("type") == "tool_result"
            and b.get("tool_use_id")
        }

    for i, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        tu_ids = _tu_ids(content)
        if not tu_ids:
            continue
        nxt = messages[i + 1] if i + 1 < len(messages) else None
        results = set()
        if isinstance(nxt, dict) and nxt.get("role") == "user":
            ncontent = nxt.get("content")
            if isinstance(ncontent, list):
                results = _tr_ids(ncontent)
        orphan = tu_ids - results
        if orphan:
            msg["content"] = [
                b for b in content
                if not (isinstance(b, dict) and b.get("type") == "tool_use"
                        and b.get("id") in orphan)
            ]
            if not msg["content"]:  # DeepSeek rechaza mensajes con content vacio
                msg["content"] = [{"type": "text", "text": ""}]
            logger.info(
                "tool adjacency: eliminados %d tool_use sin tool_result "
                "adyacente (ids=%s).",
                len(orphan), sorted(orphan),
            )

    for i, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        tr_ids = _tr_ids(content)
        if not tr_ids:
            continue
        prev = messages[i - 1] if i > 0 else None
        tu_ids = set()
        if isinstance(prev, dict) and prev.get("role") == "assistant":
            pcontent = prev.get("content")
            if isinstance(pcontent, list):
                tu_ids = _tu_ids(pcontent)
        orphan_tr = tr_ids - tu_ids
        if orphan_tr:
            msg["content"] = [
                b for b in content
                if not (isinstance(b, dict) and b.get("type") == "tool_result"
                        and b.get("tool_use_id") in orphan_tr)
            ]
            if not msg["content"]:  # DeepSeek rechaza mensajes con content vacio
                msg["content"] = [{"type": "text", "text": ""}]
            logger.info(
                "tool adjacency: eliminados %d tool_result huerfanos (ids=%s).",
                len(orphan_tr), sorted(orphan_tr),
            )

    return messages


def _patch_web_search_cost_tracker() -> None:
    """Hace dict-safe `response_object_includes_web_search_call`."""
    try:
        from litellm.litellm_core_utils.llm_cost_calc import (
            tool_call_cost_tracking as mod,
        )
        from litellm.types.utils import ServerToolUse
    except Exception as e:  # estructura inesperada de LiteLLM: no romper el arranque
        logger.warning("no se pudo importar tool_call_cost_tracking: %s", e)
        return

    cls = mod.StandardBuiltInToolCostTracking
    if getattr(cls, "_llmrouter_ws_patched", False):
        return

    # Al acceder en la clase, el staticmethod devuelve la funcion plana.
    orig = cls.response_object_includes_web_search_call

    def safe(response_object, usage=None):  # type: ignore[no-untyped-def]
        try:
            stu = (
                getattr(usage, "server_tool_use", None)
                if usage is not None
                else None
            )
            if isinstance(stu, dict):
                # Coercer dict -> ServerToolUse (solo nos interesa web_search_requests,
                # que es lo unico que lee la funcion original en esta rama).
                try:
                    usage.server_tool_use = ServerToolUse(
                        web_search_requests=stu.get("web_search_requests")
                    )
                except Exception:
                    try:
                        usage.server_tool_use = None
                    except Exception:
                        pass
            return orig(response_object, usage)
        except Exception:
            # Ultima red de seguridad: el cost-tracking nunca debe romper el logging.
            return False

    cls.response_object_includes_web_search_call = staticmethod(safe)  # type: ignore[assignment]
    cls._llmrouter_ws_patched = True
    logger.info(
        "patch aplicado: response_object_includes_web_search_call es dict-safe."
    )


_patch_web_search_cost_tracker()


class _PatchesLogger(CustomLogger):
    """CustomLogger que aplica los parches de modulo, reroutea los requests con
    imagen al modelo de vision y, opcionalmente, sonda el body del request.

    Reroute de vision: si LLMROUTER_VISION_MODEL esta seteado (generate-configs
    lo escribe cuando models.yaml define seccion 'vision') y el historial trae
    bloques de imagen, reescribe model al alias de vision ANTES del routing de
    LiteLLM, con lo que el request sale por el deployment de vision (api_base y
    key propios). LiteLLM convierte los bloques Anthropic {"type":"image"} a
    image_url de OpenAI al despachar al deployment openai/*.

    Sonda de imagenes: solo se activa con LLMROUTER_PROBE_IMAGES=1 y es
    estrictamente read-only. Los parches viven a nivel de modulo y se aplican
    al importarse.
    """

    @staticmethod
    def _has_image_blocks(messages) -> bool:
        """True si algun mensaje trae bloque de imagen (formato Anthropic
        'image' u OpenAI 'image_url'; Claude Code reenvia el historial
        completo, por eso se escanea todo y no solo el ultimo turno)."""
        if not isinstance(messages, list):
            return False
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") in (
                    "image", "image_url",
                ):
                    return True
        return False

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        """Repara la adyacencia tool_use/tool_result (400 de DeepSeek en el
        fallback); reroutea requests con imagen al modelo de vision; luego la
        sonda read-only de URLs firmadas / imageSource."""
        try:
            if isinstance(data, dict):
                _repair_tool_adjacency(data.get("messages"))
        except Exception as e:  # nunca romper el request
            logger.warning("tool adjacency repair fallo (no rompe el request): %s", e)

        try:
            vision_model = os.environ.get("LLMROUTER_VISION_MODEL", "").strip()
            if vision_model and isinstance(data, dict):
                if data.get("model") != vision_model and self._has_image_blocks(
                    data.get("messages")
                ):
                    orig = data.get("model", "?")
                    data["model"] = vision_model
                    logger.info(
                        "vision reroute: %s -> %s (request con bloques de imagen)",
                        orig, vision_model,
                    )
        except Exception as e:  # nunca romper el request
            logger.warning("vision reroute fallo (no rompe el request): %s", e)

        if os.environ.get("LLMROUTER_PROBE_IMAGES", "").lower() not in (
            "1", "true", "yes", "on"
        ):
            return data


# Instancia a nivel de modulo (LiteLLM la resuelve via get_instance_fn).
litellm_patches_handler = _PatchesLogger()
