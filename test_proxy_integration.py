import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request

import yaml

import routerctl
from test_litellm_patches import mcp_history


class ProxyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.requests = []

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *arguments):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                cls.requests.append((self.path, payload))
                if self.path.startswith("/primary"):
                    body = {"type": "error", "error": {"type": "rate_limit_error", "code": "1308", "message": "Usage limit reached for 5 hour"}}
                    self.reply(429, body)
                    return
                if self.path.startswith("/fallback") and "vision-provider-rate-limit-fixture" in json.dumps(payload):
                    self.reply(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "quota"}})
                    return
                if self.path.startswith("/vision"):
                    self.reply(200, {"id": "vision-fixture", "object": "chat.completion", "created": 0,
                                     "model": "vision-fixture", "choices": [{"index": 0, "message": {"role": "assistant", "content": "Vision OK"}, "finish_reason": "stop"}],
                                     "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                    return
                response = {"id": "msg_fixture", "type": "message", "role": "assistant", "model": "deepseek-flash",
                            "content": [{"type": "thinking", "thinking": "fixture reasoning", "signature": "fixture-signature"},
                                        {"type": "text", "text": "Fallback OK"}],
                            "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 20, "output_tokens": 3}}
                if not payload.get("stream"):
                    self.reply(200, response)
                    return
                events = [{"type": "message_start", "message": {**response, "content": [], "stop_reason": None}},
                          {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                          {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "fixture reasoning"}},
                          {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "fixture-signature"}},
                          {"type": "content_block_stop", "index": 0},
                          {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
                          {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Fallback OK"}},
                          {"type": "content_block_stop", "index": 1},
                          {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 3}},
                          {"type": "message_stop"}]
                body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def reply(self, status, body):
                encoded = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        cls.provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        cls.thread = threading.Thread(target=cls.provider.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.provider.server_close)
        cls.addClassCleanup(cls.provider.shutdown)
        cls.directory = tempfile.TemporaryDirectory(prefix="llmrouter-staging-")
        cls.addClassCleanup(cls.directory.cleanup)
        root = Path(cls.directory.name)
        (root / "litellm_patches.py").symlink_to(routerctl.ROOT / "litellm_patches.py")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        provider_url = f"http://127.0.0.1:{cls.provider.server_port}"
        cls.data = {
            "providers": {"primary": {"api_base": provider_url + "/primary", "api_key": "primary-fixture"},
                          "fallback": {"api_base": provider_url + "/fallback", "api_key": "fallback-fixture"},
                          "vision": {"api_base": provider_url + "/vision/v1", "api_key": "primary-fixture"}},
            "backends": {"glm-fixture": {"provider": "primary"}, "deepseek-flash": {"provider": "fallback"},
                         "vision-fixture": {"provider": "vision", "litellm_provider": "openai"}},
            "tiers": {"opus": "glm-fixture", "sonnet": "glm-fixture", "haiku": "deepseek-flash"},
            "vision": {"backend": "deepseek-flash", "fallback": "vision-fixture"},
            "router": {"master_key": "sk-staging-fixture", "host": "127.0.0.1", "port": port,
                       "fallbacks": {"opus": "haiku", "sonnet": "haiku"}},
        }
        module = routerctl.generator()
        module.validate(cls.data)
        config = root / "config.yaml"
        module.atomic_write(config, yaml.safe_dump(module.build_litellm_config(cls.data, module.resolve(cls.data))))
        cls.log = (root / "proxy.log").open("w+")
        cls.addClassCleanup(cls.log.close)
        cls.process = subprocess.Popen([str(routerctl.ROOT / ".venv/bin/litellm"), "--config", str(config),
                                        "--host", "127.0.0.1", "--port", str(port)], cwd=routerctl.ROOT,
                                       env={**os.environ, "PYTHONPATH": str(routerctl.ROOT), "LITELLM_LOCAL_MODEL_COST_MAP": "True", "LLMROUTER_VISION_MODEL": "vision", "LLMROUTER_VISION_USER_IMAGES": "1"},
                                       stdout=cls.log, stderr=subprocess.STDOUT, start_new_session=True)
        cls.addClassCleanup(cls.stop_proxy)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                cls.log.seek(0)
                raise RuntimeError("Staging proxy failed: " + cls.log.read()[-5000:])
            if routerctl.ready(cls.data):
                return
            time.sleep(0.2)
        raise RuntimeError("Staging proxy readiness timed out")

    @classmethod
    def stop_proxy(cls):
        if cls.process.poll() is None:
            os.killpg(cls.process.pid, signal.SIGTERM)
            try:
                cls.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(cls.process.pid, signal.SIGKILL)
                cls.process.wait(timeout=5)

    def send(self, model, messages, stream=False):
        payload = {"model": model, "max_tokens": 32, "messages": messages, "stream": stream}
        if not stream:
            return routerctl.request(self.data, "/v1/messages", payload, timeout=20)
        request = urllib.request.Request(routerctl.base_url(self.data) + "/v1/messages", data=json.dumps(payload).encode(),
                                         headers={"Authorization": "Bearer sk-staging-fixture", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read().decode()

    def test_01_alias_fallback_and_mcp_history(self):
        response = self.send("claude-opus-4-8", mcp_history())
        self.assertEqual(response["content"][-1]["text"], "Fallback OK")
        self.assertEqual(sum(path.startswith("/primary") for path, payload in self.requests), 1)
        blocks = self.requests[-1][1]["messages"][1]["content"]
        self.assertTrue(all(block["type"] == "text" for block in blocks))

    def test_02_streaming_fallback_and_shared_quota(self):
        response = self.send("sonnet", mcp_history(), stream=True)
        self.assertIn("Fallback OK", response)
        self.assertIn("message_stop", response)
        self.assertEqual(sum(path.startswith("/primary") for path, payload in self.requests), 1)

    def test_03_thinking_rehydrated_by_actual_callback(self):
        messages = [{"role": "user", "content": "cache fixture nonstream"}]
        self.send("haiku", messages)
        time.sleep(0.5)
        history = messages + [{"role": "assistant", "content": "Fallback OK"}, {"role": "user", "content": "follow up"}]
        self.send("haiku", history)
        self.assertEqual(self.requests[-1][1]["messages"][1]["content"][0]["type"], "thinking")

    def test_04_streaming_thinking_rehydrated_by_actual_callback(self):
        messages = [{"role": "user", "content": "cache fixture streaming"}]
        self.send("haiku", messages, stream=True)
        time.sleep(0.5)
        history = messages + [{"role": "assistant", "content": [{"type": "text", "text": "Fallback OK"}]},
                              {"role": "user", "content": "follow up"}]
        self.send("haiku", history)
        self.assertEqual(self.requests[-1][1]["messages"][1]["content"][0]["type"], "thinking")

    def test_05_vision_nested_in_tool_result(self):
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                              "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1cAAAAASUVORK5CYII="}}
        messages = [{"role": "assistant", "content": [{"type": "tool_use", "id": "call", "name": "image", "input": {}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call", "content": [image]}]}]
        response = self.send("opus", messages)
        self.assertEqual(response["content"][-1]["text"], "Fallback OK")
        self.assertTrue(self.requests[-1][0].startswith("/fallback"))
        self.assertEqual(self.requests[-1][1]["messages"][-1]["content"][0]["content"], [image])

    def test_06_vision_fallback_and_historical_mcp_image(self):
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "fixture"}}
        messages = mcp_history()
        messages[1]["content"][2]["content"].append(image)
        messages[-1]["content"] = "vision-provider-rate-limit-fixture"
        response = self.send("opus", messages)
        self.assertEqual(response["content"][-1]["text"], "Vision OK")
        self.assertTrue(self.requests[-1][0].startswith("/vision"))
        primary = next(payload for path, payload in reversed(self.requests) if path.startswith("/fallback"))
        self.assertTrue(any(block.get("type") == "image" for block in primary["messages"][-1]["content"]))
        self.assertFalse(any(block.get("type") == "image" for block in primary["messages"][1]["content"]))

    def test_07_large_vision_context_is_preserved(self):
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "fixture"}}
        text = "historial " * 30000
        messages = [{"role": "user", "content": [{"type": "text", "text": text}, image]}]
        self.send("opus", messages)
        sent = self.requests[-1][1]["messages"][-1]["content"]
        self.assertEqual(sent[0]["text"], text)
        self.assertEqual(sent[1], image)


if __name__ == "__main__":
    unittest.main()
