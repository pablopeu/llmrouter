import copy
import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import httpx
import litellm
from litellm import Router
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

from litellm_patches import (
    _QUOTA_BLOCKED,
    _move_assistant_images,
    _normalize_mcp_history,
    _repair_tool_adjacency,
    litellm_patches_handler,
)


def mcp_history():
    return [
        {"role": "user", "content": "Buscá el estado."},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Consulto la herramienta."},
                {
                    "type": "mcp_tool_use",
                    "id": "mcptoolu_1",
                    "name": "status",
                    "server_name": "test-server",
                    "input": {"query": "mañana", "type": "mcp_tool_use"},
                },
                {
                    "type": "mcp_tool_result",
                    "tool_use_id": "mcptoolu_1",
                    "is_error": False,
                    "content": [{"type": "text", "text": "Estado: listo."}],
                },
            ],
        },
        {"role": "user", "content": "Continuá."},
    ]


def metadata_from(block):
    return json.loads(block["text"].split("\n", 1)[1])


class MCPHistoryTests(unittest.TestCase):
    def test_same_turn_pair_preserves_metadata_and_result(self):
        messages = mcp_history()
        original = copy.deepcopy(messages)
        normalized = _normalize_mcp_history(messages)
        content = normalized[1]["content"]
        self.assertEqual(metadata_from(content[1]), messages[1]["content"][1])
        result = messages[1]["content"][2]
        metadata = {key: value for key, value in result.items() if key != "content"}
        self.assertEqual(metadata_from(content[2]), metadata)
        self.assertEqual(content[3:], result["content"])
        self.assertEqual(messages, original)
        before_repair = copy.deepcopy(normalized)
        self.assertEqual(_repair_tool_adjacency(normalized), before_repair)
        self.assertEqual(_normalize_mcp_history(normalized), normalized)

    def test_normal_tools_thinking_and_images_are_unchanged(self):
        messages = [
            {"role": "user", "content": "hola"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "pensando", "signature": "sig"},
                {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"},
                {"type": "image", "source": {"type": "url", "url": "https://example.test/image.png"}},
            ]},
        ]
        self.assertEqual(_normalize_mcp_history(messages), messages)

    def test_result_variants_and_cache_control(self):
        for result_content in ("Falló la consulta", [], [{"type": "text", "text": "Error"}]):
            with self.subTest(content=result_content):
                block = {
                    "type": "mcp_tool_result", "tool_use_id": "missing-call", "is_error": True,
                    "content": result_content, "cache_control": {"type": "ephemeral"},
                }
                normalized = _normalize_mcp_history([{"role": "assistant", "content": [block]}])
                content = normalized[0]["content"]
                metadata = metadata_from(content[0])
                if isinstance(result_content, list):
                    metadata["content"] = content[1:]
                self.assertEqual(metadata, block)
                self.assertEqual(content[0]["cache_control"], block["cache_control"])

    def test_nested_result_and_unpaired_call_are_preserved(self):
        messages = mcp_history()
        messages[1]["content"] = [{
            "type": "tool_result", "tool_use_id": "outer",
            "content": messages[1]["content"],
        }]
        normalized = _normalize_mcp_history(messages)
        content = normalized[1]["content"][0]["content"]
        self.assertTrue(all(block["type"] == "text" for block in content))
        call_only = [{"role": "assistant", "content": [mcp_history()[1]["content"][1]]}]
        normalized = _normalize_mcp_history(call_only)
        _repair_tool_adjacency(normalized)
        self.assertEqual(metadata_from(normalized[0]["content"][0]), call_only[0]["content"][0])

    def test_missing_or_non_list_messages_are_unchanged(self):
        for messages in (None, "text", [], [None, {"role": "user"}]):
            with self.subTest(messages=messages):
                self.assertEqual(_normalize_mcp_history(messages), messages)


class MCPHookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _QUOTA_BLOCKED.clear()

    def test_assistant_images_preserve_tool_pair_and_input(self):
        image = {"type": "image", "source": {"type": "url", "url": "https://example.test/image.png"}}
        messages = [{"role": "assistant", "content": [image, {"type": "tool_use", "id": "call", "name": "read", "input": {}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call", "content": "ok"},
                                                 {"type": "text", "text": "next"}]}]
        original = copy.deepcopy(messages)
        moved = _move_assistant_images(messages)
        self.assertEqual(messages, original)
        self.assertEqual(moved[1]["content"][0], messages[1]["content"][0])
        self.assertEqual(moved[1]["content"][1], image)
        self.assertEqual(moved[1]["content"][2]["text"], "next")
        self.assertEqual(_move_assistant_images(moved), moved)

    async def apply_hook(self, messages, call_type="anthropic_messages"):
        data = {"model": "opus", "messages": messages}
        with patch.dict(os.environ, {"LLMROUTER_VISION_MODEL": "", "LLMROUTER_PROBE_IMAGES": ""}):
            return await litellm_patches_handler.async_pre_call_hook(None, None, data, call_type)

    async def test_hook_normalizes_before_tool_adjacency(self):
        data = await self.apply_hook(mcp_history())
        self.assertEqual(data["model"], "opus")
        self.assertEqual(len(data["messages"][1]["content"]), 4)
        self.assertEqual(data["messages"][1]["content"][-1]["text"], "Estado: listo.")

    async def test_other_api_formats_are_not_normalized(self):
        messages = mcp_history()
        data = await self.apply_hook(messages, "acompletion")
        self.assertEqual(data["messages"], messages)

    async def test_mcp_image_still_triggers_vision(self):
        messages = mcp_history()
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}
        messages[1]["content"][2]["content"].append(image)
        data = {"model": "opus", "messages": messages}
        with patch.dict(os.environ, {"LLMROUTER_VISION_MODEL": "vision", "LLMROUTER_PROBE_IMAGES": ""}):
            data = await litellm_patches_handler.async_pre_call_hook(None, None, data, "anthropic_messages")
        self.assertEqual(data["model"], "vision")
        self.assertEqual(data["messages"][1]["content"][-1], image)

    async def test_real_router_fallback_after_429(self):
        await self.assert_router_fallback(stream=False)

    async def test_streaming_router_fallback_after_429(self):
        await self.assert_router_fallback(stream=True)

    async def test_canonical_alias_fallback(self):
        await self.assert_router_fallback(stream=False, model="claude-opus-4-8")

    async def test_quota_cooldown_skips_known_exhausted_account(self):
        await self.assert_router_fallback(stream=False)
        await self.assert_router_fallback(stream=False, blocked=True)

    async def assert_router_fallback(self, stream, model="opus", blocked=False):
        requests = []
        litellm.in_memory_llm_clients_cache.flush_cache()

        def respond(request):
            body = json.loads(request.content)
            requests.append((request.url.host, body))
            if request.url.host == "primary.test":
                return httpx.Response(429, json={"type": "error", "error": {
                    "type": "rate_limit_error", "code": "1308", "message": "Usage limit reached for 5 hour",
                }})
            self.assertEqual(request.url.host, "fallback.test")
            for message in body["messages"]:
                if isinstance(message["content"], list):
                    self.assertTrue(all(block["type"] == "text" for block in message["content"]))
            response = {
                "id": "msg_test", "type": "message", "role": "assistant", "model": "deepseek-flash",
                "content": [{"type": "text", "text": "Fallback OK"}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 20, "output_tokens": 3},
            }
            if body.get("stream"):
                events = [
                    {"type": "message_start", "message": {
                        **response, "content": [], "stop_reason": None,
                    }},
                    {"type": "content_block_start", "index": 0,
                     "content_block": {"type": "text", "text": ""}},
                    {"type": "content_block_delta", "index": 0,
                     "delta": {"type": "text_delta", "text": "Fallback OK"}},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                     "usage": {"output_tokens": 3}},
                    {"type": "message_stop"},
                ]
                content = "".join(
                    "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
                    for event in events
                )
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)
            return httpx.Response(200, json=response)

        router = Router(model_list=[
            {"model_name": "opus", "litellm_params": {
                "model": "anthropic/glm-5.3", "api_key": "test-only", "api_base": "https://primary.test",
            }},
            {"model_name": "haiku", "litellm_params": {
                "model": "anthropic/deepseek-flash", "api_key": "test-only", "api_base": "https://fallback.test",
            }},
        ], fallbacks=[{"opus": ["haiku"]}, {"claude-opus-4-8": ["haiku"]}],
            model_group_alias={"claude-opus-4-8": "opus"}, num_retries=0)
        try:
            data = await self.apply_hook(mcp_history())
            data["model"] = model
            with patch.object(AsyncHTTPHandler, "_create_async_transport", return_value=httpx.MockTransport(respond)):
                response = await router.aanthropic_messages(**data, max_tokens=32, stream=stream)
                if stream:
                    chunks = [chunk async for chunk in response]
                    events = [
                        json.loads(line.removeprefix("data: "))
                        for line in b"".join(chunks).decode().splitlines()
                        if line.startswith("data: ")
                    ]
                    rendered = "".join(
                        event.get("delta", {}).get("text", "") for event in events
                    )
                    self.assertEqual(rendered, "Fallback OK")
                    self.assertEqual(events[-1]["type"], "message_stop")
                else:
                    self.assertEqual(response["content"][0]["text"], "Fallback OK")
            self.assertEqual([host for host, body in requests],
                             ["fallback.test"] if blocked else ["primary.test", "fallback.test"])
            if not blocked:
                self.assertEqual(requests[0][1]["messages"], requests[1][1]["messages"])
            self.assertEqual(requests[-1][1]["messages"][1]["content"][-1]["text"], "Estado: listo.")
        finally:
            router.reset()


if __name__ == "__main__":
    unittest.main()
