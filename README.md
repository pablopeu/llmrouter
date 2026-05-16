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
llmrouter-claude
# or: clauder
```

Launch Codex CLI through the router:

```bash
llmrouter-codex
# or: codexr
```

General helper:

```bash
llmrouter status
llmrouter test
llmrouter claude
llmrouter codex
```

Legacy commands `claude-mix` and `codex-mix` are also available for backward compatibility.

## Commands

| Command | Description |
|---------|-------------|
| `llmrouter start` | Start LiteLLM and the Codex adapter |
| `llmrouter stop` | Stop both processes |
| `llmrouter restart` | Restart both processes |
| `llmrouter status` | Show status and health checks |
| `llmrouter test` | Test models and Codex adapter |
| `llmrouter-claude` | Launch Claude Code pointed at the router |
| `llmrouter-codex` | Launch Codex CLI pointed at the router |
| `clauder` | Shortcut for `llmrouter-claude` |
| `codexr` | Shortcut for `llmrouter-codex` |

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
rm -f ~/.local/bin/llmrouter ~/.local/bin/llmrouter-claude ~/.local/bin/llmrouter-codex
rm -f ~/.local/bin/claude-mix ~/.local/bin/codex-mix ~/.local/bin/codexr ~/.local/bin/clauder
rm -rf ~/llmrouter
```
