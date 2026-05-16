# LLMRouter

Local LLM proxy using LiteLLM to share the same models between Claude Code and OpenAI Codex CLI.

## What it does

Starts two local services:

- `127.0.0.1:4000` — LiteLLM proxy (Anthropic/OpenAI-compatible)
- `127.0.0.1:4001` — Codex CLI adapter for the Responses API

The router maps these model aliases:

| Alias | Backend | Real model |
|-------|---------|------------|
| `zai-sonnet` | Z.AI (Anthropic-compatible) | `glm-5.1` |
| `deepseek-opus` | DeepSeek (OpenAI-compatible) | `deepseek-v4-pro` |
| `deepseek-haiku` | DeepSeek (OpenAI-compatible) | `deepseek-v4-flash` |

Claude Code talks directly to the LiteLLM proxy on `:4000`. Codex CLI goes through the local adapter on `:4001` because Codex sends native Responses API tools that some Anthropic-compatible backends reject — the adapter strips unsupported tools and forwards to LiteLLM.

## Architecture

```text
Claude Code
  ANTHROPIC_BASE_URL=http://127.0.0.1:4000
        |
        v
LiteLLM Proxy (:4000) ----> Z.AI / DeepSeek
        ^
        |
Codex CLI
  OPENAI_BASE_URL=http://127.0.0.1:4001/v1
        |
        v
Codex adapter (:4001)
```

## Getting started

### 1. Set up API keys

```bash
cd ~/llmrouter
cp .env.example .env
nano .env
chmod 600 .env
```

Fill in:

- `ZAI_API_KEY`
- `DEEPSEEK_API_KEY`
- `LITELLM_MASTER_KEY`

Optional overrides:

- `ZAI_MODEL_FOR_SONNET` — GLM model via Z.AI
- `DEEPSEEK_MODEL_FOR_OPUS` — most powerful DeepSeek model
- `DEEPSEEK_MODEL_FOR_HAIKU` — fastest/cheapest DeepSeek model
- `CODEX_DEFAULT_MODEL` — model alias Codex uses by default
- `CODEX_REASONING_EFFORT` — reasoning effort for Codex (default: `xhigh`)
- `CODEX_DISABLE_THINKING` — set `1` to disable DeepSeek thinking via `extra_body.thinking=disabled`

### 2. Install

```bash
~/llmrouter/install.sh
```

This sets up the venv, installs LiteLLM, creates command symlinks in `~/.local/bin`, and installs a systemd user service if available.

### 3. Start

```bash
~/llmrouter/start-router.sh
```

Or with systemd:

```bash
systemctl --user enable --now llmrouter
```

### 4. Test

```bash
~/llmrouter/test-router.sh
```

### 5. Use

Launch Claude Code through the router:

```bash
clauder
```

Launch Codex CLI through the router:

```bash
codexr
```

General helper:

```bash
llmrouter status
llmrouter test
llmrouter claude
llmrouter codex
```

## Commands

| Command | Description |
|---------|-------------|
| `llmrouter start` | Start LiteLLM and the Codex adapter |
| `llmrouter stop` | Stop both processes |
| `llmrouter restart` | Restart both processes |
| `llmrouter status` | Show status and health checks |
| `llmrouter test` | Test models and Codex adapter |
| `clauder` | Launch Claude Code pointed at the router |
| `codexr` | Launch Codex CLI pointed at the router |

## Changing models

Edit `~/llmrouter/.env`:

```bash
DEEPSEEK_MODEL_FOR_OPUS=deepseek-reasoner
ZAI_MODEL_FOR_SONNET=glm-4-plus
CODEX_DEFAULT_MODEL=deepseek-opus
```

Then restart:

```bash
~/llmrouter/restart-router.sh
```

## Adding other providers

LLMRouter uses LiteLLM under the hood, which supports 100+ providers. To add a new one (e.g. Claude, Kimi, Qwen, Grok), you need to edit two files:

### 1. Add the model to `config.yaml`

Each entry maps an alias to a real model via environment variables:

```yaml
model_list:
  # ... existing entries ...

  # Example: Claude via Anthropic (Anthropic-compatible)
  - model_name: claude-sonnet
    litellm_params:
      model: os.environ/CLAUDE_MODEL
      api_key: os.environ/CLAUDE_API_KEY

  # Example: Qwen via Alibaba Cloud (OpenAI-compatible)
  - model_name: qwen-coder
    litellm_params:
      model: os.environ/QWEN_MODEL
      api_key: os.environ/QWEN_API_KEY
      api_base: os.environ/QWEN_API_BASE

  # Example: Grok via xAI (OpenAI-compatible)
  - model_name: grok
    litellm_params:
      model: os.environ/GROK_MODEL
      api_key: os.environ/GROK_API_KEY
      api_base: os.environ/GROK_API_BASE

  # Example: Kimi via Moonshot (OpenAI-compatible)
  - model_name: kimi
    litellm_params:
      model: os.environ/KIMI_MODEL
      api_key: os.environ/KIMI_API_KEY
      api_base: os.environ/KIMI_API_BASE
```

### 2. Add the variables to `.env`

```bash
# --- Claude (Anthropic) ---
# No api_base needed — LiteLLM uses the official Anthropic endpoint.
CLAUDE_API_KEY=your-anthropic-api-key
CLAUDE_MODEL=anthropic/claude-sonnet-4-6

# --- Qwen (Alibaba Cloud) ---
QWEN_API_KEY=your-qwen-api-key
QWEN_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
QWEN_MODEL=openai/qwen-coder-plus-latest

# --- Grok (xAI) ---
GROK_API_KEY=your-grok-api-key
GROK_API_BASE=https://api.x.ai/v1
GROK_MODEL=openai/grok-4

# --- Kimi (Moonshot) ---
KIMI_API_KEY=your-kimi-api-key
KIMI_API_BASE=https://api.moonshot.cn/v1
KIMI_MODEL=openai/moonshot-v1-auto
```

### 3. Restart and use

```bash
~/llmrouter/restart-router.sh
```

Then use the alias as your default model.

For **Codex CLI**, set in `.env`:

```bash
CODEX_DEFAULT_MODEL=qwen-coder
```

For **Claude Code**, set in `.env`:

```bash
CLAUDE_SONNET_MODEL=qwen-coder
CLAUDE_OPUS_MODEL=qwen-coder
CLAUDE_HAIKU_MODEL=deepseek-haiku
CLAUDE_SUBAGENT_MODEL=deepseek-haiku
```

These map to `model_name` entries in `config.yaml` and override the defaults.

### Model name format

LiteLLM uses a `provider/model` prefix to route to the right API. Common patterns:

| Provider type | `api_base` | `model` value |
|--------------|-----------|---------------|
| OpenAI-compatible | provider's `/v1` endpoint | `openai/model-name` |
| Anthropic-compatible | provider's API endpoint | `anthropic/model-name` |
| Anthropic (official) | not needed | `anthropic/model-name` |

Key difference: Anthropic-native providers don't need `api_base` — LiteLLM sends requests directly to `api.anthropic.com`. OpenAI-compatible providers need `api_base` pointing to their `/v1` endpoint.

Check [LiteLLM's provider docs](https://docs.litellm.ai/docs/providers) for the correct prefix and endpoint for each provider.

## Troubleshooting

### Ports in use

Default ports:

- `ROUTER_PORT=4000`
- `CODEX_ADAPTER_PORT=4001`

Change them in `.env` or stop with:

```bash
~/llmrouter/stop-router.sh
```

### Codex fails with unsupported tools

Use `llmrouter-codex` (or `codexr`), don't point Codex directly at `:4000`. Port `:4001` exists to filter Responses API tools that LiteLLM can't convert for these backends.

### Claude Code ignores the router

Verify with:

```bash
llmrouter-claude
```

It should show:

```text
ANTHROPIC_BASE_URL=http://127.0.0.1:4000
```

If `~/.claude/settings.json` has `ANTHROPIC_BASE_URL` hardcoded, that setting may take precedence.

### View logs

```bash
tail -f ~/llmrouter/logs/router.log
tail -f ~/llmrouter/logs/codex-adapter.log
```

### Uninstall

```bash
~/llmrouter/stop-router.sh
systemctl --user disable llmrouter 2>/dev/null || true
rm -f ~/.config/systemd/user/llmrouter.service
systemctl --user daemon-reload 2>/dev/null || true
rm -f ~/.local/bin/llmrouter ~/.local/bin/claude-mix ~/.local/bin/codex-mix
rm -f ~/.local/bin/codexr ~/.local/bin/clauder
rm -rf ~/llmrouter
```
