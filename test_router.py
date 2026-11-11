import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import yaml

from deepseek_thinking import DeepSeekThinkingCache, prefix_fingerprints
from litellm_patches import _repair_tool_adjacency, litellm_patches_handler
import routerctl


def configuration():
    return {
        "providers": {"test": {"api_base": "https://provider.test/anthropic", "api_key": "test-secret"}},
        "backends": {"deepseek-flash": {"provider": "test"}},
        "tiers": dict.fromkeys(("opus", "sonnet", "haiku"), "deepseek-flash"),
        "router": {"master_key": "test-master", "fallbacks": {"opus": "haiku", "sonnet": "haiku"}},
    }


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.module = routerctl.generator()
        self.data = configuration()

    def test_current_shape_and_canonical_aliases(self):
        self.module.validate(self.data)
        generated = self.module.build_litellm_config(self.data, self.module.resolve(self.data))
        self.assertEqual(len(generated["model_list"]), 3)
        self.assertEqual(generated["router_settings"]["model_group_alias"]["claude-opus-4-8"], "opus")
        self.assertEqual(generated["litellm_settings"]["callbacks"], ["litellm_patches.litellm_patches_handler"])
        self.assertNotIn("CODEX", self.module.build_router_env(self.data))

    def test_vision_fallback_configuration(self):
        self.data["backends"]["other-vision"] = {"provider": "test"}
        self.data["vision"] = {"backend": "deepseek-flash", "fallback": "other-vision"}
        self.module.validate(self.data)
        config = self.module.build_litellm_config(self.data, self.module.resolve(self.data))
        self.assertIn({"vision": ["vision_fallback"]}, config["router_settings"]["fallbacks"])
        self.assertIn("vision_fallback", [item["model_name"] for item in config["model_list"]])
        self.data["vision"]["fallback"] = "deepseek-flash"
        with self.assertRaises(ValueError):
            self.module.validate(self.data)

    def test_reject_invalid_configuration(self):
        cases = [
            ("router", "master_key", None), ("router", "port", True),
            ("router", "port", 65536), ("router", "num_retries", -1),
            ("router", "fallbacks", {"opus": "haiku", "haiku": "opus"}),
            ("router", "fallbacks", {"opus": []}),
            ("tiers", "haiku", "missing"), ("vision", "backend", []),
        ]
        for section, key, value in cases:
            with self.subTest(section=section, key=key, value=value):
                data = copy.deepcopy(self.data)
                data.setdefault(section, {})[key] = value
                with self.assertRaises(ValueError):
                    self.module.validate(data)
        for key, value in (("api_key", None), ("api_base", "https://user:secret@provider.test")):
            with self.subTest(key=key):
                data = copy.deepcopy(self.data)
                data["providers"]["test"][key] = value
                with self.assertRaises(ValueError):
                    self.module.validate(data)
        self.data["backends"]["deepseek-flash"]["provider"] = []
        with self.assertRaises(ValueError):
            self.module.validate(self.data)

    def test_generation_private_and_check_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, environment = (root / name for name in ("models.yaml", "config.yaml", "router.env"))
            source.write_text(yaml.safe_dump(self.data))
            self.module.generate(source, output, environment, check=True)
            self.assertFalse(output.exists())
            self.assertFalse((root / ".config-generation.lock").exists())
            self.module.generate(source, output, environment)
            for path in (output, environment):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            original = output.read_bytes()
            self.data["router"]["master_key"] = None
            source.write_text(yaml.safe_dump(self.data))
            with self.assertRaises(ValueError):
                self.module.generate(source, output, environment)
            self.assertEqual(output.read_bytes(), original)

    def test_failed_atomic_replace_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config.yaml"
            target.write_text("original")
            with patch.object(self.module.os, "replace", side_effect=OSError("fixture failure")):
                with self.assertRaises(OSError):
                    self.module.atomic_write(target, "replacement")
            self.assertEqual(target.read_text(), "original")
            self.assertEqual(list(Path(directory).iterdir()), [target])


class ThinkingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cache = DeepSeekThinkingCache()
        self.messages = [{"role": "user", "content": "hola"}]
        self.parameters = {"system": "system fixture", "tools": []}
        self.response = {"content": [
            {"type": "thinking", "thinking": "private reasoning", "signature": "signature"},
            {"type": "text", "text": "respuesta"},
        ]}

    def prepare(self, messages=None, model="deepseek-flash", base="https://provider.test", key="test-key", parameters=None):
        logging = SimpleNamespace(model_call_details={})
        result = self.cache.prepare_request(model, base, key, parameters if parameters is not None else self.parameters,
                                            messages if messages is not None else self.messages, logging)
        return result, logging

    async def seed(self):
        result, logging = self.prepare()
        await self.cache.async_log_success_event(logging.model_call_details, self.response, None, None)
        return [*self.messages, {"role": "assistant", "content": [{"type": "text", "text": "respuesta"}]},
                {"role": "user", "content": "seguir"}]

    async def test_rehydrate_without_mutating_input(self):
        history = await self.seed()
        result, logging = self.prepare(history)
        self.assertEqual(result[1]["content"][0]["type"], "thinking")
        self.assertEqual(history[1]["content"][0]["type"], "text")
        self.assertNotIn("private reasoning", json.dumps(logging.model_call_details))
        history[1]["content"] = "respuesta"
        result, logging = self.prepare(history)
        self.assertEqual(result[1]["content"][0]["type"], "thinking")

    async def test_isolation_by_provider_credential_model_context_and_response(self):
        history = await self.seed()
        alternatives = [
            {"base": "https://other.test"}, {"key": "other-key"}, {"model": "deepseek-other"},
            {"parameters": {"system": "different", "tools": []}},
            {"parameters": {"system": "system fixture", "tools": [{"name": "tool"}]}},
        ]
        for arguments in alternatives:
            with self.subTest(arguments=arguments):
                result, logging = self.prepare(history, **arguments)
                self.assertEqual(result[1]["content"][0]["type"], "text")
        history[1]["content"][0]["text"] = "other response"
        result, logging = self.prepare(history)
        self.assertEqual(result[1]["content"][0]["type"], "text")

    async def test_string_content_and_tool_call_response(self):
        self.response = {"choices": [{"message": {
            "content": "respuesta", "tool_calls": [{"id": "call", "function": {"name": "run", "arguments": '{"key":1}'}}],
            "provider_specific_fields": {"thinking_blocks": [{"type": "thinking", "thinking": "reason"}]},
        }}]}
        result, logging = self.prepare()
        await self.cache.async_log_success_event(logging.model_call_details, self.response, None, None)
        history = [*self.messages, {"role": "assistant", "content": [
            {"type": "text", "text": "respuesta"}, {"type": "tool_use", "id": "call", "name": "run", "input": {"key": 1}},
        ]}]
        restored, logging = self.prepare(history)
        self.assertEqual(restored[1]["content"][0]["type"], "thinking")

    def test_lru_ttl_and_byte_limits(self):
        cache = DeepSeekThinkingCache(max_entries=2, max_bytes=300, ttl=10)
        blocks = [{"type": "thinking", "thinking": "reason"}]
        with patch("deepseek_thinking.time.monotonic", return_value=0):
            cache._store("first", blocks)
            cache._store("second", blocks)
            cache._lookup("first")
            cache._store("third", blocks)
            self.assertIsNone(cache._lookup("second"))
            cache._store("oversize", [{"type": "thinking", "thinking": "x" * 1000}])
            self.assertIsNone(cache._lookup("oversize"))
            self.assertLessEqual(cache._bytes, 300)
        with patch("deepseek_thinking.time.monotonic", return_value=11):
            self.assertIsNone(cache._lookup("first"))
            self.assertEqual(cache._bytes, 0)

    def test_fingerprints_ignore_thinking_and_signature_not_semantics(self):
        plain = [{"role": "assistant", "content": [{"type": "text", "text": "answer"}]}]
        thinking = copy.deepcopy(plain)
        thinking[0]["content"].insert(0, {"type": "thinking", "thinking": "private", "signature": "signature"})
        self.assertEqual(prefix_fingerprints(plain), prefix_fingerprints(thinking))
        self.assertNotEqual(prefix_fingerprints(plain), prefix_fingerprints(self.messages))

    def test_tool_argument_signatures_are_not_discarded(self):
        first = [{"role": "assistant", "content": [{"type": "tool_use", "id": "call", "name": "run", "input": {"signature": "first"}}]}]
        second = copy.deepcopy(first)
        second[0]["content"][0]["input"]["signature"] = "second"
        self.assertNotEqual(prefix_fingerprints(first), prefix_fingerprints(second))


class ControllerTests(unittest.TestCase):
    def test_stop_remains_available_with_invalid_configuration(self):
        with patch.object(sys, "argv", ["routerctl.py", "stop"]), \
                patch.object(routerctl, "configuration", side_effect=ValueError("invalid")) as configuration_mock, \
                patch.object(routerctl, "service") as service:
            routerctl.main()
            configuration_mock.assert_not_called()
            service.assert_called_once_with("stop")

    def test_shell_entrypoint_follows_symlink_from_other_directory(self):
        with tempfile.TemporaryDirectory(prefix="router symlink ") as directory:
            link = Path(directory) / "router-link"
            link.symlink_to(routerctl.ROOT / "llmrouter")
            result = subprocess.run([str(link), "--help"], cwd="/tmp", capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("restart", result.stdout)

    def test_failed_live_tests_raise(self):
        with patch.object(routerctl, "request", side_effect=OSError("fixture")):
            with self.assertRaises(RuntimeError):
                routerctl.live_tests(configuration())

    def test_readiness_does_not_call_provider_health(self):
        with patch.object(routerctl, "request", return_value={"status": "healthy"}) as request:
            self.assertTrue(routerctl.ready(configuration()))
            request.assert_called_once_with(configuration(), "/health/readiness")

    def test_claude_permissions_are_explicit_and_prompt_unchanged(self):
        for arguments, expected in ((["hello", "--unsafe"], ["hello", "--unsafe"]),
                                    (["--unsafe", "hello"], ["--dangerously-skip-permissions", "hello"])):
            with self.subTest(arguments=arguments), patch.object(routerctl.shutil, "which", return_value="/fixture/claude"), \
                    patch.object(routerctl, "ready", return_value=True), patch.object(routerctl.os, "execvpe") as execute:
                routerctl.launch_claude(configuration(), arguments)
                self.assertEqual(execute.call_args.args[1], ["/fixture/claude", *expected])
                self.assertEqual(execute.call_args.args[2]["ANTHROPIC_DEFAULT_OPUS_MODEL"], "opus")

    def test_nested_tool_images_route_to_vision(self):
        image = {"type": "image", "source": {"type": "base64", "data": "fixture"}}
        messages = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "orphan", "content": [image]}]}]
        repaired = _repair_tool_adjacency(copy.deepcopy(messages))
        self.assertIn(image, repaired[0]["content"])
        self.assertTrue(litellm_patches_handler._has_image_blocks(messages))
        self.assertTrue(litellm_patches_handler._has_image_blocks(repaired))

    def test_orphan_tool_payload_preserved_as_history(self):
        messages = [{"role": "assistant", "content": [{"type": "tool_use", "id": "call", "name": "run", "input": {"preserve": 42}}]}]
        result = _repair_tool_adjacency(messages)
        metadata = json.loads(result[0]["content"][0]["text"].split("\n", 1)[1])
        self.assertEqual(metadata["input"], {"preserve": 42})


if __name__ == "__main__":
    unittest.main()
