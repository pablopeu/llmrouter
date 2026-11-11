#!/usr/bin/env python3
"""Control de un unico proxy supervisado por systemd, sin consultas LLM implicitas."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv/bin/python"
SERVICE = "llmrouter.service"


def generator():
    specification = importlib.util.spec_from_file_location("router_config", ROOT / "generate-configs.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def configuration():
    module = generator()
    data = module.load_models(ROOT / "models.yaml")
    module.validate(data)
    return data


def service(*arguments, check=True):
    return subprocess.run(["systemctl", "--user", *arguments, SERVICE], check=check)


def base_url(data):
    router = data["router"]
    host = router.get("host", "127.0.0.1")
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{router.get('port', 4000)}"


def request(data, path, payload=None, timeout=3):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {data['router']['master_key']}"}
    if body:
        headers["Content-Type"] = "application/json"
    probe = urllib.request.Request(base_url(data) + path, data=body, headers=headers)
    with urllib.request.urlopen(probe, timeout=timeout) as response:
        return json.load(response)


def ready(data):
    try:
        response = request(data, "/health/readiness")
        return response.get("status") == "healthy"
    except (OSError, ValueError):
        return False


def wait_ready(data):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if ready(data):
            print(f"Router listo en {base_url(data)}")
            return
        time.sleep(0.25)
    raise RuntimeError("El router no quedo listo. Revisar journalctl --user -u llmrouter.")


def run_proxy():
    os.umask(0o077)
    module = generator()
    data = module.generate(ROOT / "models.yaml", ROOT / "config.yaml", ROOT / "router.env")
    environment = os.environ.copy()
    environment["LITELLM_MASTER_KEY"] = data["router"]["master_key"]
    environment["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    environment.pop("LLMROUTER_VISION_MODEL", None)
    environment.pop("LLMROUTER_VISION_USER_IMAGES", None)
    if data.get("vision", {}).get("backend"):
        environment["LLMROUTER_VISION_MODEL"] = "vision"
        backend = module.resolve_vision(data)
        if backend["litellm_provider"] == "anthropic" and backend["model"].startswith("deepseek"):
            environment["LLMROUTER_VISION_USER_IMAGES"] = "1"
    host = data["router"].get("host", "127.0.0.1")
    port = str(data["router"].get("port", 4000))
    executable = str(ROOT / ".venv/bin/litellm")
    os.chdir(ROOT)
    os.execvpe(executable, [executable, "--config", str(ROOT / "config.yaml"),
                          "--host", host, "--port", port], environment)


def offline_tests():
    configuration()
    environment = {**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
    subprocess.run([str(PYTHON), "-m", "unittest", "discover", "-v", "-p", "test_*.py"],
                   cwd=ROOT, env=environment, check=True)
    for script in (*ROOT.glob("*.sh"), ROOT / "llmrouter", ROOT / "claude-mix", ROOT / "clauder"):
        subprocess.run(["bash", "-n", str(script)], check=True)


def live_tests(data):
    failed = []
    for tier in ("opus", "sonnet", "haiku"):
        try:
            response = request(data, "/v1/messages", {
                "model": tier, "max_tokens": 32,
                "messages": [{"role": "user", "content": "Responde solo OK"}],
            }, timeout=90)
            if response.get("type") != "message":
                raise ValueError("Respuesta inesperada")
            print(f"{tier}: OK")
        except (OSError, ValueError):
            print(f"{tier}: fallo (revisar logs privados)", file=sys.stderr)
            failed.append(tier)
    if failed:
        raise RuntimeError("Fallaron pruebas live: " + ", ".join(failed))


def launch_claude(data, arguments):
    executable = shutil.which("claude")
    if not executable:
        raise RuntimeError("claude no esta en PATH.")
    if not ready(data):
        service("start")
        wait_ready(data)
    unsafe = bool(arguments and arguments[0] == "--unsafe")
    arguments = arguments[1:] if unsafe else list(arguments)
    if unsafe:
        arguments.insert(0, "--dangerously-skip-permissions")
    environment = {
        **os.environ,
        "ANTHROPIC_BASE_URL": base_url(data),
        "ANTHROPIC_AUTH_TOKEN": data["router"]["master_key"],
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "opus",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "sonnet",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "haiku",
        "CLAUDE_CODE_SUBAGENT_MODEL": "haiku",
    }
    os.execvpe(executable, [executable, *arguments], environment)


def main():
    arguments = sys.argv[1:] or ["status"]
    if arguments[0] == "claude":
        launch_claude(configuration(), arguments[1:])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "stop", "restart", "status", "check", "test", "run"))
    parser.add_argument("--live", action="store_true", help="prueba inferencias reales y consume cuota")
    options = parser.parse_args(arguments)
    if options.command == "run":
        run_proxy()
        return
    if options.command == "stop":
        service("stop")
        return
    data = configuration()
    if options.command in ("start", "restart"):
        service(options.command)
        wait_ready(data)
    elif options.command == "status":
        service("show", "--property=ActiveState,SubState,MainPID,Restart", check=False)
        if not ready(data):
            raise RuntimeError("El proxy no esta listo.")
        models = request(data, "/v1/models")
        print("Proxy listo; modelos: " + ", ".join(item["id"] for item in models.get("data", [])))
    elif options.command == "check":
        print("models.yaml: valido (sin cambios ni llamadas a proveedores)")
    elif options.command == "test":
        if options.live:
            live_tests(data)
        else:
            offline_tests()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"[llmrouter] {error}", file=sys.stderr)
        sys.exit(1)
