#!/usr/bin/env python3
"""Genera config.yaml (LiteLLM), moonbridge-config.yml y router.env desde models.yaml.

Fuente unica de configuracion: models.yaml (proveedores, backends, tiers).
Salidas (todas derivadas):
  - config.yaml         -> LiteLLM proxy (camino Claude)
  - moonbridge-config.yml -> Moon Bridge (camino Codex)
  - router.env          -> variables para los scripts bash

Uso:
  python generate-configs.py --models models.yaml \
      --config config.yaml --moon moonbridge-config.yml \
      --router-env router.env [--data-dir ./data]
"""
import argparse
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("Falta PyYAML. Ejecuta con el venv del proyecto: "
             ".venv/bin/python generate-configs.py ...")

TIERS = ["opus", "sonnet", "haiku"]
# Nombres canonicos que manda Claude Code >=2.1; cada uno mapea a su tier.
CANONICAL = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}
MOON_VERSION = "2023-06-01"
MOON_UA = "moonbridge/1.0"
MAX_TOKENS = 65536
# Alias del deployment de vision en config.yaml; el hook de litellm_patches.py
# reroutea a este nombre cuando un request trae bloques de imagen.
VISION_ALIAS = "vision"


def die(msg):
    print(f"[generate-configs] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def is_placeholder(val):
    s = str(val).strip().lower()
    return (not s) or "change-me" in s or s.startswith("your-") or s.startswith("tu-")


def load_models(path):
    p = Path(path)
    if not p.is_file():
        die(f"No existe {p}. Copia models.yaml.example a models.yaml y completa tus claves.")
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        die(f"{p} no es YAML valido: {e}")
    if not isinstance(data, dict):
        die(f"{p}: se esperaba un mapa en la raiz.")
    return data


def provider_defaults(data, pname):
    """Defaults de metadatos declarados bajo el provider (clave 'defaults')."""
    pv = data.get("providers", {}).get(pname) or {}
    d = pv.get("defaults")
    return d if isinstance(d, dict) else {}


def effective_backend(data, bname):
    """Backend con los defaults del provider fusionados por debajo (gana el
    backend) y 'model' resuelto a la clave cuando no se define.
    Convencion: la clave del backend ES el id exacto del modelo en el proveedor;
    asi el nombre nunca diverge del modelo servido y se valida al arrancar."""
    b = data["backends"][bname]
    merged = dict(provider_defaults(data, b.get("provider")))
    merged.update(b)
    merged["model"] = merged.get("model") or bname
    return merged


def validate(data):
    providers = data.get("providers") or {}
    backends = data.get("backends") or {}
    tiers = data.get("tiers") or {}

    if not isinstance(providers, dict) or not providers:
        die("Falta la seccion 'providers' (con api_base y api_key por proveedor).")
    if not isinstance(backends, dict) or not backends:
        die("Falta la seccion 'backends' (modelos disponibles).")
    if not isinstance(tiers, dict):
        die("Falta la seccion 'tiers'.")

    for t in TIERS:
        if t not in tiers:
            die(f"'tiers' debe definir '{t}' (opus/sonnet/haiku).")
        bname = tiers[t]
        if bname not in backends:
            die(f"tier '{t}' -> '{bname}' pero ese backend no existe en 'backends' "
                f"(disponibles: {', '.join(sorted(backends))}).")

    for bname, b in backends.items():
        if not isinstance(b, dict):
            die(f"backend '{bname}' debe ser un mapa.")
        prov = b.get("provider")
        if not prov:
            die(f"backend '{bname}' no tiene 'provider'.")
        if prov not in providers:
            die(f"backend '{bname}' usa provider '{prov}' que no esta en 'providers' "
                f"(disponibles: {', '.join(sorted(providers))}).")
        if "model" in b and b["model"] != bname:
            die(f"backend '{bname}': model='{b['model']}' no coincide con la clave. "
                f"La clave del backend debe ser el id exacto del modelo en el "
                f"proveedor (o omitir 'model').")

    vision = data.get("vision") or {}
    vname = vision.get("backend")
    if vname:
        if vname not in backends:
            die(f"vision.backend='{vname}' pero ese backend no existe en "
                f"'backends' (disponibles: {', '.join(sorted(backends))}).")
        vprov = backends[vname].get("provider")
        if vprov not in providers:
            die(f"vision backend '{vname}' usa provider '{vprov}' que no esta "
                f"en 'providers'.")

    for pname, pv in providers.items():
        if is_placeholder(pv.get("api_key", "")):
            die(f"provider '{pname}': api_key vacia o placeholder. Edita models.yaml.")
        if not pv.get("api_base"):
            die(f"provider '{pname}': falta api_base.")

    router = data.get("router") or {}
    if is_placeholder(router.get("master_key", "")):
        die("router.master_key vacia o placeholder. Edita models.yaml.")

    codex = data.get("codex") or {}
    default_tier = codex.get("default_tier", "opus")
    if default_tier not in TIERS:
        die(f"codex.default_tier='{default_tier}' no es un tier valido {TIERS}.")
    effort = codex.get("reasoning_effort")
    if effort:
        tname = tiers[default_tier]
        b = effective_backend(data, tname)
        levels = b.get("supported_reasoning_levels") or []
        if effort not in levels:
            die(f"codex.reasoning_effort='{effort}' no esta soportado por el backend "
                f"'{tname}' (niveles: {levels}).")


def resolve(data):
    providers = data["providers"]
    backends = data["backends"]
    tiers = data["tiers"]

    # tier -> (backend_name, backend_efectivo, provider)
    resolved = {}
    for t in TIERS:
        bname = tiers[t]
        b = effective_backend(data, bname)
        pv = providers[b["provider"]]
        # Limite de concurrencia/tasa: el backend puede sobreescribir al provider.
        mpr = b.get("max_parallel_requests", pv.get("max_parallel_requests"))
        rpm = b.get("rpm", pv.get("rpm"))
        resolved[t] = {
            "backend": bname,
            "model": b["model"],
            "litellm_provider": b.get("litellm_provider", "anthropic"),
            "provider": b["provider"],
            "api_key": pv["api_key"],
            "api_base": b.get("api_base", pv["api_base"]),
            "max_parallel_requests": mpr,
            "rpm": rpm,
        }
    return resolved


def resolve_vision(data):
    """Resuelve la seccion opcional 'vision' -> dict de deployment o None."""
    vision = data.get("vision") or {}
    vname = vision.get("backend")
    if not vname:
        return None
    b = data["backends"][vname]
    pv = data["providers"][b["provider"]]
    return {
        "backend": vname,
        "model": b.get("model", vname),
        "litellm_provider": b.get("litellm_provider", "anthropic"),
        "provider": b["provider"],
        "api_key": pv["api_key"],
        "api_base": b.get("api_base", pv["api_base"]),
        "max_parallel_requests": b.get(
            "max_parallel_requests", pv.get("max_parallel_requests")),
        "rpm": b.get("rpm", pv.get("rpm")),
    }


def build_litellm_config(data, resolved):
    router = data.get("router") or {}
    model_list = []
    for t in TIERS:
        r = resolved[t]
        base = {
            "model": f"{r['litellm_provider']}/{r['model']}",
            "api_key": r["api_key"],
            "api_base": r["api_base"],
        }
        # (2) Limitar concurrencia/tasa hacia el provider para no gatillar los
        # rate-limit transitorios (z.ai 1302). Se aplica por deployment.
        if r.get("max_parallel_requests") is not None:
            base["max_parallel_requests"] = int(r["max_parallel_requests"])
        if r.get("rpm") is not None:
            base["rpm"] = int(r["rpm"])
        for alias in (CANONICAL[t], t):
            model_list.append({"model_name": alias, "litellm_params": dict(base)})

    # Deployment de vision (alias "vision"): destino del reroute de imagenes.
    # El hook pre-call de litellm_patches.py reescribe model -> "vision" cuando
    # el historial trae bloques de imagen (config via LLMROUTER_VISION_MODEL).
    vis = resolve_vision(data)
    if vis:
        vbase = {
            "model": f"{vis['litellm_provider']}/{vis['model']}",
            "api_key": vis["api_key"],
            "api_base": vis["api_base"],
        }
        if vis.get("max_parallel_requests") is not None:
            vbase["max_parallel_requests"] = int(vis["max_parallel_requests"])
        if vis.get("rpm") is not None:
            vbase["rpm"] = int(vis["rpm"])
        model_list.append({"model_name": VISION_ALIAS, "litellm_params": vbase})

    # (1) Reintentos con backoff: absorbe los rate-limit transitorios antes de
    # propagarlos a Claude Code. retry_policy reintenta especificamente los 429.
    num_retries = int(router.get("num_retries", 3))
    retry_after = int(router.get("retry_after", 5))
    cooldown_time = int(router.get("cooldown_time", 15))
    router_settings = {
        "num_retries": num_retries,
        "retry_after": retry_after,
        "cooldown_time": cooldown_time,
        "retry_policy": {
            "RateLimitErrorRetries": num_retries,
            "TimeoutErrorRetries": num_retries,
            "InternalServerErrorRetries": num_retries,
        },
    }

    # Fallbacks: cuando un tier da error (rate-limit, timeout), el proxy
    # reintenta automaticamente contra el tier de fallback.
    fallbacks_cfg = router.get("fallbacks") or {}
    if fallbacks_cfg:
        fallback_list = []
        for source_tier, target_tier in fallbacks_cfg.items():
            if source_tier not in TIERS or target_tier not in TIERS:
                print(f"[generate-configs] WARNING: fallback '{source_tier}' -> "
                      f"'{target_tier}' ignora tiers desconocidos.", file=sys.stderr)
                continue
            fallback_list.append({CANONICAL[source_tier]: [CANONICAL[target_tier]]})
            fallback_list.append({source_tier: [target_tier]})
        if fallback_list:
            router_settings["fallbacks"] = fallback_list

    return {
        "model_list": model_list,
        "general_settings": {"master_key": router.get("master_key")},
        # callbacks: fix del error de DeepSeek V4 thinking en multi-turno
        # ("content[].thinking must be passed back to the API") y parches sobre
        # LiteLLM (p.ej. el bug de cost-tracking con server_tool_use como dict).
        # Los modulos viven junto a config.yaml y se cargan via get_instance_fn
        # (resuelve modulo.instancia en el dir del config).
        "litellm_settings": {
            "drop_params": True,
            "set_verbose": False,
            "callbacks": [
                "deepseek_thinking.deepseek_thinking_handler",
                "litellm_patches.litellm_patches_handler",
            ],
        },
        "router_settings": router_settings,
        "server_settings": {
            "host": router.get("host", "127.0.0.1"),
            "port": int(router.get("port", 4000)),
        },
    }


def _reasoning_levels(levels):
    out = []
    for eff in (levels or []):
        label = eff.capitalize()
        out.append({"effort": eff, "description": f"{label} reasoning effort"})
    return out


def build_moonbridge_config(data, resolved, data_dir):
    tiers = data["tiers"]
    backends = data["backends"]

    # catalogo de modelos: dedup por nombre real de modelo entre los tiers
    models_catalog = {}
    used_backends = []  # (tier, backend_name) conservando orden
    seen_models = set()
    for t in TIERS:
        bname = tiers[t]
        if bname not in seen_models:
            seen_models.add(bname)
            used_backends.append(bname)

    for bname in used_backends:
        b = effective_backend(data, bname)
        entry = {
            "context_window": int(b.get("context_window", 200000)),
            "max_output_tokens": int(b.get("max_output_tokens", 128000)),
            "display_name": b.get("display_name", b["model"]),
            "description": b.get("description", b.get("display_name", b["model"])),
            "default_reasoning_level": b.get("default_reasoning_level", "medium"),
            "supported_reasoning_levels": _reasoning_levels(
                b.get("supported_reasoning_levels")),
        }
        if b.get("supports_reasoning_summaries"):
            entry["supports_reasoning_summaries"] = True
            entry["default_reasoning_summary"] = b.get("default_reasoning_summary", "auto")
        if b.get("extension"):
            entry["extensions"] = {b["extension"]: {"enabled": True}}
        models_catalog[b["model"]] = entry

    # providers usados con sus offers
    providers_cfg = {}
    for t in TIERS:
        b = effective_backend(data, tiers[t])
        pname = b["provider"]
        providers_cfg.setdefault(pname, set()).add(b["model"])

    providers_out = {}
    for pname, model_set in providers_cfg.items():
        pv = data["providers"][pname]
        providers_out[pname] = {
            "base_url": pv["api_base"],
            "api_key": pv["api_key"],
            "version": MOON_VERSION,
            "user_agent": MOON_UA,
            "offers": [{"model": m} for m in sorted(model_set)],
        }

    routes_out = {}
    for t in TIERS:
        b = effective_backend(data, tiers[t])
        routes_out[t] = {"model": b["model"], "provider": b["provider"]}

    codex = data.get("codex") or {}
    router = data.get("router") or {}
    moon_host = router.get("host", "127.0.0.1")
    moon_port = int(codex.get("port", 4001))
    any_extension = any(
        effective_backend(data, bn).get("extension") for bn in used_backends)

    extensions_block = {}
    if any_extension:
        extensions_block["deepseek_v4"] = {"config": {"reinforce_instructions": True}}
    extensions_block["db_sqlite"] = {
        "enabled": True,
        "config": {
            "path": f"{data_dir}/moonbridge.db",
            "wal": True,
            "busy_timeout_ms": 5000,
            "max_open_conns": 1,
        },
    }
    extensions_block["metrics"] = {
        "enabled": True,
        "config": {"default_limit": 100, "max_limit": 1000},
    }

    return {
        "mode": "Transform",
        "log": {"level": "info", "format": "text"},
        "server": {
            "addr": f"{moon_host}:{moon_port}",
            "auth_token": router.get("master_key", ""),
        },
        "persistence": {"active_provider": "db_sqlite"},
        "extensions": extensions_block,
        "cache": {
            "mode": "explicit",
            "ttl": "5m",
            "prompt_caching": True,
            "automatic_prompt_cache": False,
            "explicit_cache_breakpoints": True,
            "allow_retention_downgrade": False,
            "max_breakpoints": 4,
            "min_cache_tokens": 1024,
            "expected_reuse": 2,
            "minimum_value_score": 2048,
            "min_breakpoint_tokens": 1024,
        },
        "defaults": {"model": codex.get("default_tier", "opus"), "max_tokens": MAX_TOKENS},
        "models": models_catalog,
        "providers": providers_out,
        "routes": routes_out,
    }


def shell_quote(val):
    s = str(val)
    return "'" + s.replace("'", "'\"'\"'") + "'"


def build_router_env(data):
    router = data.get("router") or {}
    codex = data.get("codex") or {}
    host = router.get("host", "127.0.0.1")
    lines = [
        f"LITELLM_MASTER_KEY={shell_quote(router.get('master_key', ''))}",
        f"ROUTER_HOST={shell_quote(host)}",
        f"ROUTER_PORT={int(router.get('port', 4000))}",
        f"MOONBRIDGE_HOST={shell_quote(host)}",
        f"MOONBRIDGE_PORT={int(codex.get('port', 4001))}",
        f"CODEX_DEFAULT_MODEL={shell_quote(codex.get('default_tier', 'opus'))}",
        f"CODEX_REASONING_EFFORT={shell_quote(codex.get('reasoning_effort', 'xhigh'))}",
    ]
    if resolve_vision(data):
        lines.append(f"LLMROUTER_VISION_MODEL={shell_quote(VISION_ALIAS)}")
    return "\n".join(lines) + "\n"


def write_yaml(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(obj, f, sort_keys=False, default_flow_style=False,
                  allow_unicode=True, width=1000)


def main():
    ap = argparse.ArgumentParser(description="Genera configs de LLMRouter desde models.yaml")
    ap.add_argument("--models", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--moon", required=True)
    ap.add_argument("--router-env", required=True)
    ap.add_argument("--data-dir", default="./data")
    args = ap.parse_args()

    data = load_models(args.models)
    validate(data)
    resolved = resolve(data)

    write_yaml(args.config, build_litellm_config(data, resolved))
    write_yaml(args.moon, build_moonbridge_config(data, resolved, args.data_dir))
    with open(args.router_env, "w", encoding="utf-8") as f:
        f.write(build_router_env(data))

    print("[generate-configs] Configs generadas:")
    for t in TIERS:
        r = resolved[t]
        print(f"  {t:6} -> {r['backend']} "
              f"({r['litellm_provider']}/{r['model']} @ {r['provider']})")
    vis = resolve_vision(data)
    if vis:
        print(f"  vision -> {vis['backend']} "
              f"({vis['litellm_provider']}/{vis['model']} @ {vis['provider']})")
    print(f"  codex default tier: {data.get('codex', {}).get('default_tier', 'opus')}")
    print(f"  -> {args.config}")
    print(f"  -> {args.moon}")
    print(f"  -> {args.router_env}")


if __name__ == "__main__":
    main()
