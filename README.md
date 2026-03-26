# claude-proxy

A lightweight HTTP proxy that wraps the [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI (`claude -p`) and exposes an **Anthropic-compatible `/v1/messages` API**. This lets you use any tool or library that speaks the Anthropic Messages API — but powered by your local Claude Code installation instead of a direct API key.

## Why?

Claude Code comes with a powerful set of built-in tools (Bash, file editing, search, git, etc.) and uses your existing Claude subscription for authentication. This proxy lets you:

- **Use the Anthropic SDK** (Python/TypeScript) without an API key
- **Connect any Anthropic-compatible client** to Claude Code's capabilities
- **Switch models on the fly** — pass `model: "opus"` or `model: "sonnet"` in your requests
- **Get Claude Code's full toolbox** (file I/O, shell, search) behind a standard API

### claude-proxy vs Claude Agent SDK

| | claude-proxy | [Claude Agent SDK](https://docs.anthropic.com/en/docs/agent-sdk/overview) |
|---|---|---|
| **Auth** | Uses your Claude Code subscription — no API key needed | Requires an Anthropic API key |
| **Built-in tools** | Full Claude Code toolbox (Bash, Edit, Read, Grep, etc.) | You define your own tools |
| **Setup** | Single script, zero dependencies | SDK installation + tool definitions + orchestration code |
| **Control** | Black box — Claude Code handles the agent loop | Full control over tools, retries, agent loop |
| **Use case** | Local dev tooling, personal automation | Production apps, custom agents, multi-user services |
| **Scaling** | Single-user, localhost | Designed for deployment and scaling |

**Use claude-proxy** when you want Claude Code's capabilities behind a standard API with zero setup. **Use the Agent SDK** when you need custom tools, production deployment, or fine-grained control.

## Prerequisites

- **Python 3.10+** (standard library only — no dependencies)
- **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** CLI installed and authenticated

Verify Claude Code is working:

```bash
claude -p "hello"
```

## Quick start

```bash
# Clone and run
git clone https://github.com/oliverox/claude-proxy.git
cd claude-proxy
python claude-proxy.py

# Or specify a custom port (default: 8082)
python claude-proxy.py 9000
```

The server starts on `http://127.0.0.1:8082` and prints:

```
Claude proxy server listening on http://127.0.0.1:8082
  POST /v1/messages  — Anthropic-compatible Messages API
  GET  /health       — Health check
```

## Usage

### With the Anthropic Python SDK

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://127.0.0.1:8082",
    api_key="not-needed",  # Any string works — auth is handled by Claude Code
)

# Non-streaming
response = client.messages.create(
    model="sonnet",  # Aliases: "opus", "sonnet", "haiku" or full model IDs
    max_tokens=1024,
    messages=[{"role": "user", "content": "Explain Python decorators in 3 sentences."}],
)
print(response.content[0].text)

# Streaming
with client.messages.stream(
    model="opus",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Write a haiku about coding."}],
) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)
```

### With the Anthropic TypeScript SDK

```typescript
import Anthropic from "@anthropic-ai/sdk";

const client = new Anthropic({
  baseURL: "http://127.0.0.1:8082",
  apiKey: "not-needed",
});

const message = await client.messages.create({
  model: "sonnet",
  max_tokens: 1024,
  messages: [{ role: "user", content: "Hello!" }],
});
console.log(message.content[0].text);
```

### With curl

```bash
# Non-streaming
curl http://127.0.0.1:8082/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: not-needed" \
  -d '{
    "model": "sonnet",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Hello!"}]
  }'

# Streaming
curl http://127.0.0.1:8082/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: not-needed" \
  -d '{
    "model": "opus",
    "max_tokens": 1024,
    "stream": true,
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

## API reference

### `POST /v1/messages`

Anthropic-compatible Messages API endpoint. Accepts the same request format as the [Anthropic Messages API](https://docs.anthropic.com/en/docs/api-reference/messages).

**Supported request fields:**

| Field | Type | Description |
|---|---|---|
| `model` | string | Model alias (`"opus"`, `"sonnet"`, `"haiku"`) or full model ID (e.g. `"claude-sonnet-4-6"`). Default: `claude-sonnet-4-20250514` |
| `messages` | array | Array of message objects with `role` and `content` (required) |
| `system` | string or array | System prompt — string or array of content blocks |
| `stream` | boolean | Enable SSE streaming (default: `false`) |
| `max_tokens` | number | Accepted but not enforced (Claude Code manages this) |

**Response format:**

- **Non-streaming:** Returns a standard Anthropic `message` object with `content`, `usage`, `stop_reason`, etc.
- **Streaming:** Returns Server-Sent Events (SSE) following the Anthropic streaming protocol (`message_start`, `content_block_delta`, `message_stop`, etc.)

### `GET /health`

Returns `{"status": "ok"}` — useful for readiness checks.

## Model selection

Pass model aliases or full IDs in the `model` field:

| Alias | Resolves to |
|---|---|
| `opus` | Latest Claude Opus |
| `sonnet` | Latest Claude Sonnet |
| `haiku` | Latest Claude Haiku |
| `claude-opus-4-6` | Claude Opus 4.6 specifically |
| `claude-sonnet-4-6` | Claude Sonnet 4.6 specifically |

Model resolution is handled by the Claude Code CLI — any value it accepts via `--model` works here.

## How it works

```
Client (SDK/curl) ──HTTP──▶ claude-proxy ──subprocess──▶ claude -p --model <model>
                  ◀─JSON/SSE─           ◀──stdout───────
```

1. Receives an Anthropic Messages API request
2. Converts the `messages` array into a text prompt
3. Spawns `claude -p --model <model> --output-format json|stream-json`
4. Pipes the prompt via stdin
5. Translates Claude Code's output back into Anthropic API response format

The proxy runs with `--dangerously-skip-permissions` and `--no-session-persistence` for non-interactive use. Each request is a fresh, stateless invocation.

## Limitations

- **No multi-turn context** — each request is independent (no session persistence)
- **No tool_use responses** — tool call/result XML blocks are stripped from output; the proxy returns text only
- **Subprocess overhead** — each request spawns a new `claude` process
- **Local only** — binds to `127.0.0.1`, not suitable for network-exposed deployment
- **Single-user** — designed for personal/development use

## License

MIT
