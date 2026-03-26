#!/usr/bin/env python3
"""
Claude CLI Proxy Server

A local HTTP server that wraps `claude -p` and exposes an
Anthropic-compatible /v1/messages endpoint (both streaming and non-streaming).
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

DEFAULT_PORT = 8082

# Strip <tool_call>...</tool_call> and <tool_result>...</tool_result> blocks
_TOOL_BLOCK_RE = re.compile(
    r'<tool_call>\s*.*?\s*</tool_call>\s*'
    r'(?:<tool_result>\s*.*?\s*</tool_result>\s*)?',
    re.DOTALL,
)


def strip_tool_blocks(text: str) -> str:
    """Remove tool_call/tool_result XML blocks from claude -p output."""
    cleaned = _TOOL_BLOCK_RE.sub('', text)
    # Collapse multiple blank lines left behind
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


def build_prompt(messages: list[dict]) -> str:
    """Convert Anthropic messages array into a single text prompt for claude -p."""
    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            # Handle content blocks: [{"type": "text", "text": "..."}]
            text_parts = [
                block["text"] for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            content = "\n".join(text_parts)
        if role == "assistant":
            parts.append(f"[assistant]: {content}")
        else:
            parts.append(content)
    return "\n\n".join(parts)


def make_message_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        sys.stderr.write(f"[claude-proxy] {args[0]}\n")

    def _send_json(self, status: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, error_type: str, message: str):
        self._send_json(status, {
            "type": "error",
            "error": {"type": error_type, "message": message},
        })

    def do_POST(self):
        if self.path != "/v1/messages":
            self._send_error(404, "not_found_error", f"Unknown endpoint: {self.path}")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            self._send_error(400, "invalid_request_error", f"Invalid JSON: {e}")
            return

        sys.stderr.write(f"[claude-proxy] stream={body.get('stream')}, model={body.get('model')}, system_len={len(str(body.get('system', '')))}, msgs={len(body.get('messages', []))}\n")

        messages = body.get("messages", [])
        if not messages:
            self._send_error(400, "invalid_request_error", "messages is required")
            return

        stream = body.get("stream", False)
        system_prompt = body.get("system")
        if isinstance(system_prompt, list):
            # Handle system as content blocks
            system_prompt = "\n".join(
                block["text"] for block in system_prompt
                if isinstance(block, dict) and block.get("type") == "text"
            )

        prompt = build_prompt(messages)
        model_requested = body.get("model", "claude-sonnet-4-20250514")
        # Validate model: only allow alphanumeric, hyphens, dots, underscores
        if not re.fullmatch(r'[a-zA-Z0-9._-]+', model_requested):
            self._send_error(400, "invalid_request_error", f"Invalid model: {model_requested}")
            return
        msg_id = make_message_id()

        # Build the claude command — optimized for speed
        cmd = ["claude", "-p",
               "--dangerously-skip-permissions",
               "--no-session-persistence",
               "--model", model_requested,
               "--output-format"]
        if stream:
            cmd.extend(["stream-json", "--verbose"])
        else:
            cmd.append("json")
        # Write system prompt to a temp file to avoid ARG_MAX limits
        sp_file = None
        if system_prompt:
            sp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
            sp_file.write(system_prompt)
            sp_file.close()
            cmd.extend(["--system-prompt-file", sp_file.name])

        env = {**os.environ}
        # Allow running inside a Claude Code session
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)

        try:
            if stream:
                self._handle_stream(cmd, env, prompt, msg_id, model_requested)
            else:
                self._handle_sync(cmd, env, prompt, msg_id, model_requested)
        finally:
            if sp_file:
                os.unlink(sp_file.name)

    def _handle_sync(self, cmd, env, prompt, msg_id, model):
        try:
            result = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True, env=env,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            self._send_error(504, "timeout_error", "claude -p timed out")
            return

        if result.returncode != 0:
            self._send_error(502, "api_error", f"claude exited {result.returncode}: {result.stderr.strip()}")
            return

        # Parse the JSON output from claude
        try:
            claude_resp = json.loads(result.stdout)
        except json.JSONDecodeError:
            # Fallback: treat raw stdout as text
            claude_resp = {"result": result.stdout.strip()}

        # Extract text from claude's JSON output and strip tool blocks
        text = strip_tool_blocks(claude_resp.get("result", result.stdout.strip()))

        self._send_json(200, {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": model,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": claude_resp.get("input_tokens", 0),
                "output_tokens": claude_resp.get("output_tokens", 0),
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        })

    def _handle_stream(self, cmd, env, prompt, msg_id, model):
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env,
        )

        try:
            # Send prompt and close stdin
            proc.stdin.write(prompt)
            proc.stdin.close()
        except BrokenPipeError:
            proc.kill()
            self._send_error(502, "api_error", "claude process exited before accepting input")
            return

        # Set up SSE streaming response
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def send_sse(event: str, data: dict):
            payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
            self.wfile.write(payload.encode())
            self.wfile.flush()

        try:
            # message_start
            send_sse("message_start", {
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            })

            # content_block_start
            send_sse("content_block_start", {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            })

            send_sse("ping", {"type": "ping"})

            input_tokens = 0
            output_tokens = 0
            buffered_text = ""

            # Read stream-json lines from claude
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    sys.stderr.write(f"[claude-proxy] non-json line: {line}\n")
                    continue

                sys.stderr.write(f"[claude-proxy] event: {event}\n")
                evt_type = event.get("type", "")

                if evt_type == "assistant":
                    # Text from claude — buffer it (each event has cumulative text)
                    msg = event.get("message", "")
                    if isinstance(msg, dict):
                        content = msg.get("content", [])
                        text = "".join(
                            block.get("text", "")
                            for block in content
                            if isinstance(block, dict) and block.get("type") == "text"
                        )
                    else:
                        text = str(msg) if msg else ""
                    if text:
                        buffered_text = text
                elif evt_type == "result":
                    # Final result with usage info
                    usage = event.get("usage", {})
                    input_tokens = usage.get("input_tokens", 0) if isinstance(usage, dict) else 0
                    output_tokens = usage.get("output_tokens", 0) if isinstance(usage, dict) else 0
                    result_text = event.get("result", "")
                    # Use result text (most complete), fall back to buffered
                    final_text = strip_tool_blocks(result_text or buffered_text or "")
                    if final_text:
                        send_sse("content_block_delta", {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": final_text},
                        })

            proc.wait()
            stderr_out = proc.stderr.read()
            if stderr_out:
                sys.stderr.write(f"[claude-proxy] claude stderr: {stderr_out}\n")

            # content_block_stop
            send_sse("content_block_stop", {
                "type": "content_block_stop",
                "index": 0,
            })

            # message_delta
            send_sse("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": output_tokens},
            })

            # message_stop
            send_sse("message_stop", {"type": "message_stop"})
        except (BrokenPipeError, ConnectionResetError):
            sys.stderr.write("[claude-proxy] client disconnected during stream\n")
            proc.kill()
        finally:
            self.close_connection = True

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_error(404, "not_found_error", f"Unknown endpoint: {self.path}")


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    server = ThreadedHTTPServer(("127.0.0.1", port), ProxyHandler)
    print(f"Claude proxy server listening on http://127.0.0.1:{port}")
    print(f"  POST /v1/messages  — Anthropic-compatible Messages API")
    print(f"  GET  /health       — Health check")
    print()
    print("Usage with Anthropic SDK:")
    print(f'  client = Anthropic(base_url="http://127.0.0.1:{port}", api_key="not-needed")')
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
