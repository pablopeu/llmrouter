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

El pipeline normaliza historial MCP, conserva bloques huerfanos como texto
historico y aisla thinking por cuenta, modelo, contexto y respuesta. Las cuotas
1308 activan un cooldown local breve sin interpretar fechas del proveedor.
"""
from __future__ import annotations

import json
import copy
import functools
import inspect
import hashlib
import os
import threading
import time
import uuid

from litellm.integrations.custom_logger import CustomLogger
import litellm

from deepseek_thinking import deepseek_thinking_handler
from router_logging import get_logger

# El endpoint /v1/messages de LiteLLM despacha los backends openai/* por el
# Responses API; z.ai solo expone /chat/completions y responde 404 en
# /v4/responses. Este flag global vuelve al camino chat/completions para los
# deployments openai/azure (los anthropic/* no se ven afectados).
litellm.use_chat_completions_url_for_anthropic_messages = True

logger = get_logger("litellm_patches")


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


def _normalize_mcp_history(messages):
    """Representa el historial MCP sin crear llamadas de cliente pendientes.

    Los backends compatibles no aceptan mcp_tool_use/mcp_tool_result. Ambos
    pueden estar en el mismo turno assistant, por lo que renombrarlos a
    tool_use/tool_result viola la adyacencia exigida por DeepSeek. Conservamos
    sus metadatos como texto JSON y los resultados como bloques de contenido,
    incluidas las imagenes, antes de reparar herramientas y elegir vision.
    """
    if not isinstance(messages, list):
        return messages

    converted = 0

    def normalize_content(content):
        nonlocal converted
        if not isinstance(content, list):
            return content
        normalized = []
        for block in content:
            if not isinstance(block, dict):
                normalized.append(block)
                continue
            block_type = block.get("type")
            if block_type in ("mcp_tool_use", "mcp_tool_result"):
                converted += 1
                metadata = dict(block)
                result_content = None
                if block_type == "mcp_tool_result" and isinstance(
                    metadata.get("content"), list
                ):
                    result_content = metadata.pop("content")
                text_block = {
                    "type": "text",
                    "text": (
                        "[Historical MCP block; not a pending client tool call]\n"
                        + json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
                    ),
                }
                if "cache_control" in block:
                    text_block["cache_control"] = block["cache_control"]
                normalized.append(text_block)
                if result_content is not None:
                    normalized.extend(normalize_content(result_content))
            elif block_type == "tool_result" and isinstance(block.get("content"), list):
                normalized.append({**block, "content": normalize_content(block["content"])})
            else:
                normalized.append(block)
        return normalized

    normalized_messages = [
        {**message, "content": normalize_content(message["content"])}
        if isinstance(message, dict) and "content" in message else message
        for message in messages
    ]
    if converted:
        logger.info("MCP history: normalizados %d bloques para backends compatibles.", converted)
    return normalized_messages


def _historical_blocks(block):
    metadata = dict(block)
    content = metadata.pop("content") if isinstance(metadata.get("content"), list) else []
    result = [{"type": "text", "text": "[Historical tool block; not pending]\n" +
               json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))}]
    result.extend(content)
    if "cache_control" in block:
        result[-1] = {**result[-1], "cache_control": block["cache_control"]}
    return result


def _repair_tool_adjacency(messages):
    """Repara la adyacencia tool_use/tool_result que exigen backends estrictos.

    DeepSeek (endpoint /anthropic) responde 400 con "tool_use ids were found
    without tool_result blocks immediately after" si un assistant message lleva
    un `tool_use` cuyo `tool_result` no esta en el mensaje INMEDIATAMENTE
    siguiente. Eso anulaba el fallback z.ai -> deepseek: el request caido caia
    a deepseek y deepseek lo rechazaba, y el usuario veia el error igual.

    Regla (no-op sobre conversaciones validas: Anthropic ya exige la adyacencia
    y Claude Code la cumple):
      - Los bloques sin pareja inmediata pasan a texto historico.
      - Se preservan argumentos, resultados e imagenes sin llamadas pendientes.
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
            repaired = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") in orphan:
                    repaired.extend(_historical_blocks(block))
                else:
                    repaired.append(block)
            msg["content"] = repaired
            if not msg["content"]:  # DeepSeek rechaza mensajes con content vacio
                msg["content"] = [{"type": "text", "text": ""}]
            logger.info(
                "tool adjacency: preservados como historial %d tool_use sin resultado adyacente.",
                len(orphan),
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
            repaired = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") in orphan_tr:
                    repaired.extend(_historical_blocks(block))
                else:
                    repaired.append(block)
            msg["content"] = repaired
            if not msg["content"]:  # DeepSeek rechaza mensajes con content vacio
                msg["content"] = [{"type": "text", "text": ""}]
            logger.info(
                "tool adjacency: preservados como historial %d tool_result huerfanos.",
                len(orphan_tr),
            )

    return messages


_QUOTA_BLOCKED = {}
_QUOTA_LOCK = threading.Lock()


def _patch_request_pipeline():
    from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler

    original = BaseLLMHTTPHandler.async_anthropic_messages_handler
    if getattr(original, "_llmrouter_pipeline", False):
        return
    signature = inspect.signature(original)

    @functools.wraps(original)
    async def wrapped(self, *arguments, **keywords):
        bound = signature.bind(self, *arguments, **keywords)
        values = bound.arguments
        api_base = values.get("api_base") or "https://api.anthropic.com"
        credential = hashlib.sha256((values.get("api_key") or "").encode()).hexdigest()
        account = (api_base.rstrip("/"), credential)
        with _QUOTA_LOCK:
            now = time.monotonic()
            for key in list(_QUOTA_BLOCKED):
                if _QUOTA_BLOCKED[key] <= now:
                    del _QUOTA_BLOCKED[key]
            blocked = account in _QUOTA_BLOCKED
        if blocked:
            raise litellm.RateLimitError(
                message="Proveedor en cooldown temporal por cuota 1308 agotada; se intenta fallback.",
                llm_provider="anthropic", model=values["model"],
            )
        messages = _normalize_mcp_history(copy.deepcopy(values["messages"]))
        _repair_tool_adjacency(messages)
        values["messages"] = deepseek_thinking_handler.prepare_request(
            values["model"], api_base, values.get("api_key"),
            values.get("anthropic_messages_optional_request_params") or {},
            messages, values["logging_obj"],
        )
        try:
            return await original(*bound.args, **bound.kwargs)
        except Exception as error:
            if getattr(error, "status_code", None) == 429 and "1308" in str(error):
                with _QUOTA_LOCK:
                    if len(_QUOTA_BLOCKED) < 64:
                        _QUOTA_BLOCKED[account] = time.monotonic() + 300
                logger.info("Cuota 1308: cuenta en cooldown durante 300 segundos.")
            raise

    wrapped._llmrouter_pipeline = True
    BaseLLMHTTPHandler.async_anthropic_messages_handler = wrapped


_patch_request_pipeline()


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


def _move_assistant_images(messages):
    """Mantiene imagenes historicas y las ubica en el proximo turno user."""
    if not isinstance(messages, list):
        return messages
    result = copy.deepcopy(messages)
    index = 0
    while index < len(result):
        message = result[index]
        if not isinstance(message, dict) or message.get("role") != "assistant" or not isinstance(message.get("content"), list):
            index += 1
            continue
        images = [block for block in message["content"] if isinstance(block, dict) and block.get("type") == "image"]
        if not images:
            index += 1
            continue
        message["content"] = [block if not isinstance(block, dict) or block.get("type") != "image"
                              else {"type": "text", "text": "[Historical image moved to the next user message.]"}
                              for block in message["content"]]
        if index + 1 == len(result) or not isinstance(result[index + 1], dict) or result[index + 1].get("role") != "user":
            result.insert(index + 1, {"role": "user", "content": []})
        following = result[index + 1]
        content = following.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            raise ValueError("Contenido de usuario incompatible con imagen historica")
        position = 0
        while position < len(content) and isinstance(content[position], dict) and content[position].get("type") == "tool_result":
            position += 1
        following["content"] = content[:position] + images + content[position:]
        index += 1
    return result


class _PatchesLogger(CustomLogger):
    """CustomLogger que aplica los parches de modulo, reroutea los requests con
    imagen al modelo de vision y conserva thinking entre turnos.

    Reroute de vision: si LLMROUTER_VISION_MODEL esta seteado (generate-configs
    lo escribe cuando models.yaml define seccion 'vision') y el historial trae
    bloques de imagen, reescribe model al alias de vision ANTES del routing de
    LiteLLM, con lo que el request sale por el deployment de vision (api_base y
    key propios). LiteLLM convierte los bloques Anthropic {"type":"image"} a
    image_url de OpenAI al despachar al deployment openai/*.

    Los parches viven a nivel de modulo y se aplican al importarse.
    """

    @staticmethod
    def _has_image_blocks(messages) -> bool:
        """True si algun mensaje trae bloque de imagen (formato Anthropic
        'image' u OpenAI 'image_url'; Claude Code reenvia el historial
        completo, por eso se escanea todo y no solo el ultimo turno)."""
        if not isinstance(messages, list):
            return False
        def has_image(content):
            if not isinstance(content, list):
                return False
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") in ("image", "image_url"):
                    return True
                if block.get("type") in ("tool_result", "mcp_tool_result") and has_image(block.get("content")):
                    return True
            return False

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            if has_image(content):
                return True
        return False

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        """Repara la adyacencia tool_use/tool_result (400 de DeepSeek en el
        fallback) y reroutea requests con imagen al modelo de vision."""
        if call_type == "anthropic_messages" and isinstance(data, dict) and "messages" in data:
            data["messages"] = _normalize_mcp_history(copy.deepcopy(data["messages"]))

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
                if data.get("model") == vision_model and os.environ.get("LLMROUTER_VISION_USER_IMAGES") == "1":
                    data["messages"] = _move_assistant_images(data.get("messages"))
        except Exception as e:  # nunca romper el request
            logger.warning("vision reroute fallo (no rompe el request): %s", e)

        return data

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        await deepseek_thinking_handler.async_log_success_event(
            kwargs, response_obj, start_time, end_time,
        )


# Instancia a nivel de modulo (LiteLLM la resuelve via get_instance_fn).
litellm_patches_handler = _PatchesLogger()
