#!/usr/bin/env python3
"""
Small OpenAI Responses adapter for Codex CLI.

Codex sends Responses API tool definitions that LiteLLM cannot always map to
Anthropic-compatible backends. This adapter keeps normal function tools, drops
unsupported hosted/native tool types, and forwards the request to the local
LiteLLM proxy.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


SUPPORTED_RESPONSE_TOOL_TYPES = {"function"}
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def getenv(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value if value else default


ROUTER_HOST = getenv("ROUTER_HOST", "127.0.0.1")
ROUTER_PORT = getenv("ROUTER_PORT", "4000")
ADAPTER_HOST = getenv("CODEX_ADAPTER_HOST", ROUTER_HOST)
ADAPTER_PORT = int(getenv("CODEX_ADAPTER_PORT", "4001"))
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
UPSTREAM_BASE = f"http://{ROUTER_HOST}:{ROUTER_PORT}"
DISABLE_DEEPSEEK_THINKING = getenv("CODEX_DISABLE_THINKING", "1") not in {
    "0",
    "false",
    "False",
}


def sanitize_responses_payload(payload: dict[str, Any]) -> dict[str, Any]:
    input_items = payload.get("input")
    if isinstance(input_items, list):
        normalized_items = normalize_responses_input(input_items)
        if normalized_items != input_items:
            logging.info("Normalized Codex function_call/function_call_output ordering")
            payload["input"] = normalized_items
            input_items = normalized_items

        counts = Counter(
            str(item.get("type", "unknown")) if isinstance(item, dict) else "unknown"
            for item in input_items
        )
        sequence = [
            str(item.get("type", "unknown")) if isinstance(item, dict) else "unknown"
            for item in input_items
        ]
        roles = [
            str(item.get("role", "")) if isinstance(item, dict) else ""
            for item in input_items
        ]
        logging.info(
            "Responses input item types: %s sequence=%s roles=%s",
            dict(counts),
            sequence,
            roles,
        )

    if DISABLE_DEEPSEEK_THINKING:
        extra_body = payload.get("extra_body")
        if not isinstance(extra_body, dict):
            extra_body = {}
        extra_body.setdefault("thinking", {"type": "disabled"})
        payload["extra_body"] = extra_body

    tools = payload.get("tools")
    if isinstance(tools, list):
        kept: list[Any] = []
        dropped: dict[str, int] = {}
        for tool in tools:
            tool_type = tool.get("type") if isinstance(tool, dict) else None
            if tool_type in SUPPORTED_RESPONSE_TOOL_TYPES:
                kept.append(tool)
            else:
                key = str(tool_type or "unknown")
                dropped[key] = dropped.get(key, 0) + 1

        if dropped:
            logging.info("Dropped unsupported Responses tools: %s", dropped)
        payload["tools"] = kept

        if not kept and payload.get("tool_choice") not in (None, "none"):
            payload["tool_choice"] = "none"

    tool_choice = payload.get("tool_choice")
    if isinstance(tool_choice, dict):
        choice_type = tool_choice.get("type")
        if choice_type not in SUPPORTED_RESPONSE_TOOL_TYPES:
            logging.info("Reset unsupported tool_choice type: %s", choice_type)
            payload["tool_choice"] = "auto" if payload.get("tools") else "none"

    return payload


def should_sanitize(method: str, path: str) -> bool:
    if method != "POST":
        return False
    clean_path = path.split("?", 1)[0]
    return clean_path in {
        "/v1/responses",
        "/responses",
        "/openai/v1/responses",
    }


def message_has_text(item: dict[str, Any]) -> bool:
    content = item.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    return True
    return False


def normalize_responses_input(input_items: list[Any]) -> list[Any]:
    normalized: list[Any] = []
    index = 0
    while index < len(input_items):
        current = input_items[index]
        next_item = input_items[index + 1] if index + 1 < len(input_items) else None
        after_next = input_items[index + 2] if index + 2 < len(input_items) else None

        if (
            isinstance(current, dict)
            and current.get("type") == "function_call"
            and isinstance(next_item, dict)
            and next_item.get("type") == "message"
            and next_item.get("role") == "assistant"
            and isinstance(after_next, dict)
            and after_next.get("type") == "function_call_output"
        ):
            if message_has_text(next_item):
                normalized.append(next_item)
            normalized.append(current)
            normalized.append(after_next)
            index += 3
            continue

        normalized.append(current)
        index += 1

    return normalized


class CodexAdapterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        logging.info("%s - %s", self.client_address[0], fmt % args)

    def do_GET(self) -> None:
        if self.path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.forward()

    def do_POST(self) -> None:
        self.forward()

    def do_DELETE(self) -> None:
        self.forward()

    def forward(self) -> None:
        self.close_connection = True
        body = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))

        if should_sanitize(self.command, self.path):
            try:
                payload = json.loads(body.decode("utf-8"))
                if isinstance(payload, dict):
                    body = json.dumps(
                        sanitize_responses_payload(payload),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
            except json.JSONDecodeError:
                self.send_error(400, "Invalid JSON")
                return

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
        }
        headers["Content-Length"] = str(len(body))
        if MASTER_KEY and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {MASTER_KEY}"

        req = urllib.request.Request(
            f"{UPSTREAM_BASE}{self.path}",
            data=body if self.command in {"POST", "DELETE"} else None,
            headers=headers,
            method=self.command,
        )

        try:
            with urllib.request.urlopen(req, timeout=900) as upstream:
                self.send_response(upstream.status)
                for key, value in upstream.headers.items():
                    if key.lower() in HOP_BY_HOP_HEADERS:
                        continue
                    self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = upstream.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except urllib.error.HTTPError as exc:
            error_body = exc.read()
            self.send_response(exc.code)
            for key, value in exc.headers.items():
                if key.lower() in HOP_BY_HOP_HEADERS:
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(error_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(error_body)
        except Exception as exc:
            logging.exception("Adapter upstream error")
            message = json.dumps({"error": {"message": str(exc)}}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not MASTER_KEY:
        logging.warning("LITELLM_MASTER_KEY is not set; relying on client auth headers")
    server = ThreadingHTTPServer((ADAPTER_HOST, ADAPTER_PORT), CodexAdapterHandler)
    logging.info("Codex adapter listening on http://%s:%s", ADAPTER_HOST, ADAPTER_PORT)
    logging.info("Forwarding to %s", UPSTREAM_BASE)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
