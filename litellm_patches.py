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
"""
from __future__ import annotations

import logging
import os

from litellm.integrations.custom_logger import CustomLogger

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


class _NoOpLogger(CustomLogger):
    """CustomLogger trivial.

    Solo existe para que LiteLLM cargue este modulo (que aplica los parches al
    importarse). No hace nada en los hooks: los parches viven a nivel de modulo.
    """


# Instancia a nivel de modulo (LiteLLM la resuelve via get_instance_fn).
litellm_patches_handler = _NoOpLogger()
