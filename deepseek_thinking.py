#!/usr/bin/env python3
"""Callback de LiteLLM que arregla el error de DeepSeek V4 thinking en multi-turno.

PROBLEMA
--------
DeepSeek V4 (`deepseek-v4-pro`, `deepseek-v4-flash`) expone un endpoint
compatible con Anthropic (`api.deepseek.com/anthropic`) que firma SUS PROPIOS
bloques `thinking` (firma UUID-v4 propia, no la criptografica de Anthropic).
En una conversacion multi-turno, DeepSeek exige que esos bloques `thinking`
se **reenvien intactos** en los mensajes asistente del historial. El error es:

    400 {"error":{"message":"The `content[].thinking` in the thinking mode
        must be passed back to the API.","type":"invalid_request_error", ...}}

Claude Code, al recibirlos, los stripa/redienta (asume que solo los bloques
firmados por Anthropic son verificables). En el turno siguiente el mensaje
asistente llega sin thinking -> DeepSeek rechaza con 400. LiteLLM no lo
resuelve: su unica logica de thinking dropea el PARAMETRO `thinking` solo si
`modify_params: true` (aqui apagado), y de todos modos DeepSeek quiere los
BLOQUES, no el parametro dropeado.

SOLUCION (la misma que adoptaron Hermes Agent, opencode y claude-code-router)
------------------------------------------------------------------------
Un CustomLogger de LiteLLM que:
  1. Al volver una respuesta con bloques `thinking`, los cachea indexados por
     un *fingerprint* estable del prefijo de conversacion que los produjo.
  2. Antes de reenviar el siguiente request, reinyecta los bloques `thinking`
     cacheados en los mensajes asistente del historial que los perdieron
     (y limpia cualquier `redacted_thinking` que Claude Code haya dejado).

FINGERPRINT
-----------
Hash FNV-1a-32 del prefijo de mensajes (todo lo anterior al turno asistente)
normalizado: excluye bloques `thinking`/`redacted_thinking`, firmas y
espacios del texto. El prefijo es estable entre turnos (Claude Code apendea
turnos nuevos pero no modifica los anteriores), asi que el fingerprint del
prefijo que produjo un turno coincide con el del mismo turno cuando se reenvia
en el historial. La normalizacion hace que la reinyeccion (que reagrega
thinking a turnos previos) NO cambie el fingerprint => coincide determinista.

REGISTRO
--------
El modulo define una INSTANCIA a nivel de modulo (`deepseek_thinking_handler`)
porque LiteLLM resuelve el callback via `get_instance_fn("mod.name")` que hace
`getattr(module, name)` y luego el loop de hooks filtra con
`isinstance(callback, CustomLogger)` (necesita la instancia, no la clase).
"""
from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional

from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("deepseek_thinking")
logger.setLevel(logging.INFO)
# Handler propio: evidencia independiente del nivel del root logger de LiteLLM.
try:
    _log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(_log_dir, exist_ok=True)
    _fh = logging.FileHandler(os.path.join(_log_dir, "deepseek_thinking.log"))
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_fh)
    logger.propagate = False
except Exception:  # nunca fallar la carga del modulo por logging
    pass

# Solo actuar en estos call_types. Claude Code usa /v1/messages (anthropic_messages).
_SUPPORTED_CALL_TYPES = {"anthropic_messages"}
# Techo del cache en memoria (LRU). Sobrada para uso local mono-usuario.
_MAX_ENTRIES = 512

_FNV_OFFSET = 0x811C9DC5
_FNV_PRIME = 0x01000193
_MASK = 0xFFFFFFFF


def _fnv1a_update(h: int, data: str) -> int:
    """Actualiza un hash FNV-1a-32 con un string (UTF-8)."""
    h &= _MASK
    for b in data.encode("utf-8", errors="replace"):
        h ^= b
        h = (h * _FNV_PRIME) & _MASK
    return h


def _block_to_dict(block: Any) -> Optional[Dict[str, Any]]:
    """Normaliza un content block (dict, pydantic u otro) a dict o None."""
    if block is None:
        return None
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):  # pydantic v2
        try:
            return block.model_dump()
        except Exception:
            pass
    if hasattr(block, "dict"):  # pydantic v1
        try:
            return block.dict()
        except Exception:
            pass
    if isinstance(block, object):
        try:
            return dict(block)  # type: ignore[arg-type]
        except Exception:
            return None
    return None


def _is_thinking_like(t: Optional[str]) -> bool:
    return t in ("thinking", "redacted_thinking")


def _normalize_content(content: Any) -> Iterable[str]:
    """Itera representaciones estables de un `content` (string o lista de blocks).

    Excluye thinking/redacted_thinking y firmas; colapsa espacios del texto.
    """
    if content is None:
        return
    if isinstance(content, str):
        yield " ".join(content.split())
        return
    if isinstance(content, list):
        for block in content:
            d = _block_to_dict(block)
            if not isinstance(d, dict):
                continue
            t = d.get("type")
            if _is_thinking_like(t):
                continue  # excluir thinking del fingerprint
            if t == "text":
                txt = d.get("text")
                if isinstance(txt, str):
                    yield " ".join(txt.split())
                continue
            # tool_use / tool_result / image / etc.: identidad estable.
            # Para tool_result, normalizar su content recursivamente (sin thinking).
            if t == "tool_result":
                tid = d.get("tool_use_id")
                if isinstance(tid, str):
                    yield f"tr:{tid}"
                for piece in _normalize_content(d.get("content")):
                    yield f"trc:{piece}"
                continue
            if t == "tool_use":
                tid = d.get("id")
                name = d.get("name")
                inp = d.get("input")
                yield f"tu:{tid}:{name}:{_canon_json(inp)}"
                continue
            # Fallback determinista para tipos desconocidos.
            yield f"{t}:{_canon_json(d)}"


def _canon_json(obj: Any) -> str:
    """JSON estable (sorted keys) para meter en el fingerprint."""
    import json

    try:
        return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), default=str)
    except Exception:
        return repr(obj)


def _message_pieces(msg: Any) -> Iterable[str]:
    """Partes estables de UN mensaje para el fingerprint (sin thinking)."""
    if isinstance(msg, dict):
        role = msg.get("role")
        yield f"role:{role}"
        for piece in _normalize_content(msg.get("content")):
            yield piece
    else:
        # Mensaje pydantic u objeto raro: best-effort.
        d = _block_to_dict(msg)
        if isinstance(d, dict):
            yield from _message_pieces(d)


def _fingerprint_prefix(messages: List[Any], upto: int) -> int:
    """Fingerprint FNV-1a-32 de messages[0:upto] (sin thinking)."""
    h = _fnv1a_update(_FNV_OFFSET, "anthropic|v1")
    for msg in messages[:upto]:
        h = _fnv1a_update(h, "\x1e")  # separador entre mensajes
        for piece in _message_pieces(msg):
            h = _fnv1a_update(h, piece)
            h = _fnv1a_update(h, "\x1f")  # separador entre piezas
    return h


def _prefix_fingerprints(messages: List[Any]) -> List[int]:
    """Fingerprints de TODOS los prefijos en una sola pasada O(n).

    Devuelve fps donde fps[i] == _fingerprint_prefix(messages, i), es decir el
    fingerprint del prefijo ANTES del mensaje i. Usar esto en vez de llamar
    _fingerprint_prefix por cada mensaje (que seria O(n^2) en sesiones largas).
    """
    h = _fnv1a_update(_FNV_OFFSET, "anthropic|v1")
    fps: List[int] = []
    for msg in messages:
        fps.append(h)  # fingerprint de messages[0:i]
        h = _fnv1a_update(h, "\x1e")
        for piece in _message_pieces(msg):
            h = _fnv1a_update(h, piece)
            h = _fnv1a_update(h, "\x1f")
    return fps


class DeepSeekThinkingCache(CustomLogger):
    """Cachea bloques `thinking` y los reinyecta donde DeepSeek los exige."""

    def __init__(self) -> None:
        # fingerprint(int) -> List[dict] de bloques thinking.
        self._cache: "OrderedDict[int, List[Dict[str, Any]]]" = OrderedDict()
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- cache
    def _store(self, fp: int, blocks: List[Dict[str, Any]]) -> None:
        if not blocks:
            return
        with self._lock:
            self._cache[fp] = blocks
            self._cache.move_to_end(fp)
            while len(self._cache) > _MAX_ENTRIES:
                self._cache.popitem(last=False)

    def _lookup(self, fp: int) -> Optional[List[Dict[str, Any]]]:
        with self._lock:
            blocks = self._cache.get(fp)
            if blocks is not None:
                self._cache.move_to_end(fp)
            return blocks

    # ------------------------------------------------------------ extraccion
    def _iter_content_lists(self, obj: Any) -> Iterable[List[Any]]:
        """Candidatos de `content` en distintas formas del response_obj."""
        if obj is None:
            return
        # Forma Anthropic directa.
        content = getattr(obj, "content", None)
        if content is None and isinstance(obj, dict):
            content = obj.get("content")
        if isinstance(content, list):
            yield content
        # Forma OpenAI ModelResponse: choices[0].message.content.
        choices = getattr(obj, "choices", None)
        if choices is None and isinstance(obj, dict):
            choices = obj.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0]
            if hasattr(msg, "message"):
                msg = msg.message
            elif isinstance(msg, dict):
                msg = msg.get("message")
            mc = getattr(msg, "content", None)
            if mc is None and isinstance(msg, dict):
                mc = msg.get("content")
            if isinstance(mc, list):
                yield mc

    def _iter_psf(self, obj: Any, kwargs: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
        """Candidatos de provider_specific_fields (donde LiteLLM ensambla thinking streamed)."""
        seen = []
        # Desde el response_obj.
        choices = getattr(obj, "choices", None)
        if choices is None and isinstance(obj, dict):
            choices = obj.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0]
            if hasattr(msg, "message"):
                msg = msg.message
            elif isinstance(msg, dict):
                msg = msg.get("message")
            psf = getattr(msg, "provider_specific_fields", None)
            if psf is None and isinstance(msg, dict):
                psf = msg.get("provider_specific_fields")
            if isinstance(psf, dict):
                seen.append(psf)
        # Desde kwargs (logging payload).
        for key in ("provider_specific_fields", "litellm_provider_specific_fields"):
            psf = kwargs.get(key)
            if isinstance(psf, dict):
                seen.append(psf)
        slp = kwargs.get("standard_logging_object")
        if isinstance(slp, dict):
            resp = slp.get("response")
            if isinstance(resp, dict):
                psf = resp.get("provider_specific_fields")
                if isinstance(psf, dict):
                    seen.append(psf)
        for s in seen:
            yield s

    def _extract_thinking(self, response_obj: Any,
                          kwargs: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen_text: set = set()

        def add(text: Any, signature: Any) -> None:
            if not text or not isinstance(text, str):
                return
            if text in seen_text:
                return
            seen_text.add(text)
            out.append({
                "type": "thinking",
                "thinking": text,
                "signature": signature if isinstance(signature, str) else None,
            })

        # 1) Bloques thinking en cualquier `content`.
        for content in self._iter_content_lists(response_obj):
            for block in content:
                d = _block_to_dict(block)
                if isinstance(d, dict) and d.get("type") == "thinking":
                    add(d.get("thinking"), d.get("signature"))
        # 2) thinking_blocks ensamblados por el adapter (streaming).
        for psf in self._iter_psf(response_obj, kwargs):
            tbs = psf.get("thinking_blocks")
            if isinstance(tbs, list):
                for b in tbs:
                    d = _block_to_dict(b)
                    if isinstance(d, dict):
                        add(d.get("thinking"), d.get("signature"))
        return out

    # --------------------------------------------------------------- hooks
    async def async_log_success_event(self, kwargs, response_obj, start_time,
                                      end_time) -> None:
        """Cachea thinking de la respuesta indexado por el prefijo del request."""
        try:
            if kwargs.get("call_type") not in _SUPPORTED_CALL_TYPES:
                return
            blocks = self._extract_thinking(response_obj, kwargs or {})
            if not blocks:
                return
            messages = kwargs.get("messages") or []
            if not isinstance(messages, list) or not messages:
                return
            fp = _fingerprint_prefix(messages, len(messages))
            self._store(fp, blocks)
            logger.info(
                "deepseek_thinking: cacheados %d bloque(s) thinking (fp=%d) "
                "para %d mensaje(s) previos.",
                len(blocks), fp, len(messages),
            )
        except Exception as e:  # nunca romper el flujo de logging
            logger.warning("deepseek_thinking: fallo cacheando thinking: %s", e)

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        """Reinyecta thinking cacheado en mensajes asistente que lo perdieron."""
        try:
            if call_type not in _SUPPORTED_CALL_TYPES:
                return data
            if not isinstance(data, dict):
                return data
            messages = data.get("messages")
            if not isinstance(messages, list) or not messages:
                return data

            injected = 0
            fingerprints = _prefix_fingerprints(messages)  # O(n) una sola pasada
            for i, msg in enumerate(messages):
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                content = msg.get("content")
                # Content puede ser string o lista. Solo tocamos listas.
                if not isinstance(content, list):
                    continue
                types = {
                    (_block_to_dict(b) or {}).get("type") for b in content
                }
                # ¿Ya tiene un bloque thinking propio? -> Claude Code lo pasó bien.
                if "thinking" in types:
                    continue
                cached = self._lookup(fingerprints[i])
                if not cached:
                    continue
                # Limpiar redacted_thinking ajeno (DeepSeek no lo acepta) y reinyectar.
                new_content = [
                    b for b in content
                    if (_block_to_dict(b) or {}).get("type") != "redacted_thinking"
                ]
                msg["content"] = list(cached) + new_content
                injected += 1

            if injected:
                logger.info(
                    "deepseek_thinking: reinyectados thinking en %d mensaje(s) "
                    "asistente antes de reenviar.", injected,
                )
            return data
        except Exception as e:
            # NUNCA propagar: devolver data intacto para no romper el request.
            logger.warning("deepseek_thinking: fallo reinyectando thinking: %s", e)
            return data


# Instancia a nivel de modulo (LiteLLM la resuelve via get_instance_fn).
deepseek_thinking_handler = DeepSeekThinkingCache()
