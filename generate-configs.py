#!/usr/bin/env python3
"""Valida models.yaml y publica configuracion privada de LiteLLM."""

import argparse
import fcntl
import os
from pathlib import Path
import tempfile
from urllib.parse import urlparse

import yaml

TIERS = ("opus", "sonnet", "haiku")
CANONICAL = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}
VISION_ALIAS = "vision"


def die(message):
    raise ValueError(message)


def is_placeholder(value):
    if not isinstance(value, str):
        return True
    value = value.strip().lower()
    return not value or "change-me" in value or value.startswith(("your-", "tu-"))


def load_models(path):
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError:
        die("models.yaml no es YAML valido; no se muestra el contenido por seguridad.")
    if not isinstance(data, dict):
        die("models.yaml debe contener un mapa.")
    return data


def mapping(data, key):
    value = data.get(key, {})
    if not isinstance(value, dict):
        die(f"'{key}' debe ser un mapa.")
    return value


def positive_integer(value, label, minimum=1, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        die(f"{label} debe ser un entero >= {minimum}.")
    if maximum is not None and value > maximum:
        die(f"{label} excede {maximum}.")


def validate_url(value, label):
    if not isinstance(value, str):
        die(f"{label} debe ser una URL HTTP(S).")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        die(f"{label} debe ser una URL HTTP(S) sin credenciales embebidas.")


def effective_backend(data, name):
    backend = data["backends"][name]
    defaults = data["providers"][backend["provider"]].get("defaults", {})
    return {**defaults, **backend, "model": backend.get("model") or name}


def validate(data):
    providers = mapping(data, "providers")
    backends = mapping(data, "backends")
    tiers = mapping(data, "tiers")
    router = mapping(data, "router")
    vision = mapping(data, "vision")
    if not providers or not backends:
        die("Se requieren providers y backends.")
    for name, provider in providers.items():
        if not isinstance(provider, dict) or is_placeholder(provider.get("api_key")):
            die(f"Provider '{name}': api_key vacia, nula o placeholder.")
        validate_url(provider.get("api_base"), f"Provider '{name}': api_base")
        if not isinstance(provider.get("defaults", {}), dict):
            die(f"Provider '{name}': defaults debe ser un mapa.")
    for name, backend in backends.items():
        if not isinstance(name, str) or not isinstance(backend, dict):
            die("Cada backend debe tener nombre y un mapa de opciones.")
        if not isinstance(backend.get("provider"), str) or backend["provider"] not in providers:
            die(f"Backend '{name}': provider desconocido.")
        if "model" in backend and backend["model"] != name:
            die(f"Backend '{name}': model debe coincidir con la clave.")
        effective = effective_backend(data, name)
        if effective.get("litellm_provider", "anthropic") not in ("anthropic", "openai"):
            die(f"Backend '{name}': litellm_provider no soportado.")
        if "api_base" in effective:
            validate_url(effective["api_base"], f"Backend '{name}': api_base")
        provider = providers[backend["provider"]]
        for option in ("max_parallel_requests", "rpm"):
            value = effective.get(option, provider.get(option))
            if value is not None:
                positive_integer(value, f"Backend '{name}': {option}")
    for tier in TIERS:
        if not isinstance(tiers.get(tier), str) or tiers[tier] not in backends:
            die(f"Tier '{tier}': backend inexistente.")
    if vision.get("backend") is not None and (
        not isinstance(vision["backend"], str) or vision["backend"] not in backends
    ):
        die("vision.backend no existe en backends.")
    if vision.get("fallback") is not None and (
        not vision.get("backend") or not isinstance(vision["fallback"], str)
        or vision["fallback"] not in backends or vision["fallback"] == vision["backend"]
    ):
        die("vision.fallback debe ser un backend existente distinto de vision.backend.")
    if is_placeholder(router.get("master_key")):
        die("router.master_key vacia, nula o placeholder.")
    host = router.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host or any(character.isspace() for character in host):
        die("router.host invalido.")
    positive_integer(router.get("port", 4000), "router.port", maximum=65535)
    for option, default in (("num_retries", 0), ("retry_after", 1), ("cooldown_time", 30)):
        positive_integer(router.get(option, default), f"router.{option}", minimum=0)
    fallbacks = mapping(router, "fallbacks")
    for source, target in fallbacks.items():
        if not isinstance(source, str) or not isinstance(target, str) or source not in TIERS or target not in TIERS:
            die("Los fallbacks deben conectar tiers conocidos.")
        seen = set()
        current = source
        while current in fallbacks:
            if current in seen:
                die("Se detecto un ciclo en router.fallbacks.")
            seen.add(current)
            current = fallbacks[current]


def resolve_backend(data, name):
    backend = effective_backend(data, name)
    provider = data["providers"][backend["provider"]]
    return {
        "backend": name,
        "provider": backend["provider"],
        "model": backend["model"],
        "litellm_provider": backend.get("litellm_provider", "anthropic"),
        "api_base": backend.get("api_base", provider["api_base"]),
        "api_key": provider["api_key"],
        **{option: backend.get(option, provider.get(option))
           for option in ("max_parallel_requests", "rpm")},
    }


def resolve(data):
    return {tier: resolve_backend(data, data["tiers"][tier]) for tier in TIERS}


def resolve_vision(data):
    name = data.get("vision", {}).get("backend")
    return resolve_backend(data, name) if name else None


def build_litellm_config(data, resolved):
    deployments = dict(resolved)
    vision = resolve_vision(data)
    if vision:
        deployments[VISION_ALIAS] = vision
    fallback_vision = data.get("vision", {}).get("fallback")
    if fallback_vision:
        deployments["vision_fallback"] = resolve_backend(data, fallback_vision)
    model_list = []
    for alias, backend in deployments.items():
        params = {
            "model": f"{backend['litellm_provider']}/{backend['model']}",
            "api_key": backend["api_key"],
            "api_base": backend["api_base"],
        }
        params.update({option: backend[option] for option in ("max_parallel_requests", "rpm")
                       if backend.get(option) is not None})
        model_list.append({"model_name": alias, "litellm_params": params})
    router = data.get("router", {})
    retries = router.get("num_retries", 0)
    return {
        "model_list": model_list,
        "general_settings": {"master_key": router["master_key"]},
        "litellm_settings": {
            "drop_params": True,
            "set_verbose": False,
            "callbacks": ["litellm_patches.litellm_patches_handler"],
        },
        "router_settings": {
            "model_group_alias": {canonical: tier for tier, canonical in CANONICAL.items()},
            "num_retries": retries,
            "retry_after": router.get("retry_after", 1),
            "cooldown_time": router.get("cooldown_time", 30),
            "retry_policy": {name: retries for name in (
                "RateLimitErrorRetries", "TimeoutErrorRetries", "InternalServerErrorRetries",
            )},
            "fallbacks": [
                {alias: [target]}
                for source, target in router.get("fallbacks", {}).items()
                for alias in (source, CANONICAL[source])
            ] + ([{"vision": ["vision_fallback"]}] if fallback_vision else []),
        },
    }


def shell_quote(value):
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def build_router_env(data):
    router = data["router"]
    values = {
        "LITELLM_MASTER_KEY": router["master_key"],
        "ROUTER_HOST": router.get("host", "127.0.0.1"),
        "ROUTER_PORT": router.get("port", 4000),
    }
    if resolve_vision(data):
        values["LLMROUTER_VISION_MODEL"] = VISION_ALIAS
    return "".join(f"{name}={shell_quote(value)}\n" for name, value in values.items())


def atomic_write(path, content):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def generate(models, config, router_env, check=False):
    data = load_models(models)
    validate(data)
    config_text = yaml.safe_dump(build_litellm_config(data, resolve(data)), sort_keys=False)
    env_text = build_router_env(data)
    if not check:
        lock_path = Path(models).parent / ".config-generation.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            atomic_write(config, config_text)
            atomic_write(router_env, env_text)
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default="models.yaml")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--router-env", default="router.env")
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    try:
        generate(arguments.models, arguments.config, arguments.router_env, arguments.check)
    except (ValueError, OSError) as error:
        parser.exit(1, f"[generate-configs] {error}\n")
    print("[generate-configs] Configuracion validada" if arguments.check else
          "[generate-configs] Configuracion privada publicada")


if __name__ == "__main__":
    main()
