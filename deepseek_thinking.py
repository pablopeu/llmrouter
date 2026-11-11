"""Cache privado y acotado de thinking, aislado por backend, contexto y respuesta."""

from collections import OrderedDict
import copy
import hashlib
import json
import threading
import time

from litellm.integrations.custom_logger import CustomLogger

from router_logging import get_logger

logger = get_logger("deepseek_thinking")


def as_dict(value):
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    return {}


def canonical(value):
    value = as_dict(value) if hasattr(value, "model_dump") else value
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items()}
    if isinstance(value, list):
        return [canonical(item) for item in value]
    return value


def encoded(value):
    return json.dumps(canonical(value), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode()


def message_identity(message):
    message = as_dict(message)
    content = message.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if isinstance(content, list):
        content = [
            {key: value for key, value in as_dict(block).items() if key != "cache_control"}
            for block in content if as_dict(block).get("type") not in ("thinking", "redacted_thinking")
        ]
    return {"role": message.get("role"), "content": content}


def prefix_fingerprints(messages):
    digest = hashlib.sha256(b"llmrouter-thinking-v2")
    prefixes = []
    for message in messages:
        prefixes.append(digest.copy().hexdigest())
        digest.update(encoded(message_identity(message)))
        digest.update(b"\x00")
    prefixes.append(digest.hexdigest())
    return prefixes


def response_message(response):
    response = as_dict(response)
    if isinstance(response.get("content"), list):
        return {"role": "assistant", "content": response["content"]}
    choices = response.get("choices") or []
    if not choices:
        return None
    message = as_dict(as_dict(choices[0]).get("message"))
    content = message.get("content") or []
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    content = list(content)
    for call in message.get("tool_calls") or []:
        call = as_dict(call)
        function = as_dict(call.get("function"))
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                return None
        content.append({"type": "tool_use", "id": call.get("id"),
                        "name": function.get("name"), "input": arguments})
    return {"role": "assistant", "content": content}


class DeepSeekThinkingCache(CustomLogger):
    def __init__(self, max_entries=512, max_bytes=8 * 1024 * 1024, ttl=3600):
        super().__init__()
        self._cache = OrderedDict()
        self._lock = threading.Lock()
        self._bytes = 0
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.ttl = ttl

    def _remove(self, key):
        entry = self._cache.pop(key)
        self._bytes -= entry[2]

    def _expire(self):
        now = time.monotonic()
        for key in list(self._cache):
            if self._cache[key][1] <= now:
                self._remove(key)

    def _store(self, key, blocks):
        size = len(json.dumps(blocks, ensure_ascii=False).encode())
        if not blocks or size > self.max_bytes:
            return
        with self._lock:
            self._expire()
            if key in self._cache:
                self._remove(key)
            self._cache[key] = (copy.deepcopy(blocks), time.monotonic() + self.ttl, size)
            self._bytes += size
            while len(self._cache) > self.max_entries or self._bytes > self.max_bytes:
                self._remove(next(iter(self._cache)))

    def _lookup(self, key):
        with self._lock:
            self._expire()
            entry = self._cache.get(key)
            if entry is None:
                return None
            self._cache.move_to_end(key)
            return copy.deepcopy(entry[0])

    def _key(self, scope, prefix, message):
        return hashlib.sha256(encoded([scope, prefix, message_identity(message)])).hexdigest()

    def prepare_request(self, model, api_base, api_key, parameters, messages, logging_obj):
        if not model.split("/")[-1].startswith("deepseek"):
            return messages
        scope = hashlib.sha256(encoded({
            "model": model, "api_base": api_base,
            "credential": hashlib.sha256((api_key or "").encode()).hexdigest(),
            "system": parameters.get("system"), "tools": parameters.get("tools"),
        })).hexdigest()
        messages = copy.deepcopy(messages)
        prefixes = prefix_fingerprints(messages)
        for index, message in enumerate(messages):
            content = message.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if message.get("role") != "assistant" or not isinstance(content, list):
                continue
            if any(as_dict(block).get("type") == "thinking" for block in content):
                continue
            cached = self._lookup(self._key(scope, prefixes[index], message))
            if cached:
                message["content"] = cached + [block for block in content
                                              if as_dict(block).get("type") != "redacted_thinking"]
        logging_obj.model_call_details["_llmrouter_thinking_scope"] = scope
        logging_obj.model_call_details["_llmrouter_thinking_prefix"] = prefixes[-1]
        return messages

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            scope = kwargs.get("_llmrouter_thinking_scope")
            prefix = kwargs.get("_llmrouter_thinking_prefix")
            if not scope or not prefix:
                return
            response = as_dict(response_obj)
            message = response_message(response)
            if not message:
                return
            blocks = [as_dict(block) for block in message["content"]
                      if as_dict(block).get("type") == "thinking"]
            if not blocks:
                choices = response.get("choices") or []
                if choices:
                    fields = as_dict(as_dict(as_dict(choices[0]).get("message")).get("provider_specific_fields"))
                    blocks = fields.get("thinking_blocks") or []
            if blocks:
                self._store(self._key(scope, prefix, message), blocks)
                logger.info("Thinking cache: respuesta guardada; entradas=%d bytes=%d",
                            len(self._cache), self._bytes)
        except Exception:
            logger.warning("Thinking cache: respuesta no cacheable; se conserva el retry de compatibilidad.")


deepseek_thinking_handler = DeepSeekThinkingCache()
