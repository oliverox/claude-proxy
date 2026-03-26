#!/usr/bin/env python3
"""Tests for claude-proxy."""

import json
import os
import threading
import time
import unittest
from unittest.mock import patch, MagicMock
from urllib.request import urlopen, Request
from urllib.error import HTTPError

# Import from the proxy module
import importlib.util
spec = importlib.util.spec_from_file_location("claude_proxy", os.path.join(os.path.dirname(__file__), "claude-proxy.py"))
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


def start_server(port):
    """Start the proxy server in a background thread."""
    server = proxy.ThreadedHTTPServer(("127.0.0.1", port), proxy.ProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def api_request(port, path="/v1/messages", method="POST", body=None):
    """Make a request to the test server and return (status, parsed_json)."""
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        resp = urlopen(req)
        return resp.status, json.loads(resp.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


# ---------------------------------------------------------------------------
# Unit tests (no server needed)
# ---------------------------------------------------------------------------

class TestStripToolBlocks(unittest.TestCase):
    def test_strips_tool_call(self):
        text = "Hello\n<tool_call>\nfoo\n</tool_call>\nWorld"
        assert proxy.strip_tool_blocks(text) == "Hello\nWorld"

    def test_strips_tool_call_and_result(self):
        text = "Before\n<tool_call>x</tool_call>\n<tool_result>y</tool_result>\nAfter"
        assert proxy.strip_tool_blocks(text) == "Before\nAfter"

    def test_no_tool_blocks(self):
        text = "Just plain text"
        assert proxy.strip_tool_blocks(text) == "Just plain text"

    def test_empty_string(self):
        assert proxy.strip_tool_blocks("") == ""

    def test_collapses_blank_lines(self):
        text = "A\n<tool_call>x</tool_call>\n\n\n\nB"
        result = proxy.strip_tool_blocks(text)
        assert "\n\n\n" not in result
        assert "A" in result and "B" in result


class TestBuildPrompt(unittest.TestCase):
    def test_single_user_message(self):
        msgs = [{"role": "user", "content": "Hello"}]
        assert proxy.build_prompt(msgs) == "Hello"

    def test_multi_turn(self):
        msgs = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "How are you?"},
        ]
        result = proxy.build_prompt(msgs)
        assert "Hi" in result
        assert "[assistant]: Hello!" in result
        assert "How are you?" in result

    def test_content_blocks(self):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "Part 1"},
            {"type": "text", "text": "Part 2"},
            {"type": "image", "source": {}},  # non-text block ignored
        ]}]
        result = proxy.build_prompt(msgs)
        assert "Part 1" in result
        assert "Part 2" in result

    def test_empty_messages(self):
        assert proxy.build_prompt([]) == ""


class TestMakeMessageId(unittest.TestCase):
    def test_format(self):
        msg_id = proxy.make_message_id()
        assert msg_id.startswith("msg_")
        assert len(msg_id) == 28  # "msg_" + 24 hex chars

    def test_unique(self):
        ids = {proxy.make_message_id() for _ in range(100)}
        assert len(ids) == 100


# ---------------------------------------------------------------------------
# Integration tests (server + mocked subprocess)
# ---------------------------------------------------------------------------

TEST_PORT = 18923  # unlikely to collide


class TestEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = start_server(TEST_PORT)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_health(self):
        status, data = api_request(TEST_PORT, "/health", method="GET")
        assert status == 200
        assert data == {"status": "ok"}

    def test_help(self):
        status, data = api_request(TEST_PORT, "/help", method="GET")
        assert status == 200
        assert "endpoints" in data
        assert "capabilities" in data
        assert "examples" in data
        assert data["name"] == "claude-proxy"

    def test_404(self):
        status, data = api_request(TEST_PORT, "/nonexistent", method="GET")
        assert status == 404

    def test_missing_messages(self):
        status, data = api_request(TEST_PORT, body={"model": "sonnet"})
        assert status == 400
        assert "messages is required" in data["error"]["message"]

    def test_invalid_json(self):
        url = f"http://127.0.0.1:{TEST_PORT}/v1/messages"
        req = Request(url, data=b"not json", method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            urlopen(req)
            assert False, "Should have raised"
        except HTTPError as e:
            assert e.code == 400

    def test_invalid_model(self):
        status, data = api_request(TEST_PORT, body={
            "model": "model with spaces!@#",
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert status == 400
        assert "Invalid model" in data["error"]["message"]

    def test_model_validation_accepts_valid(self):
        """Valid model names should pass validation (will fail at subprocess level, not validation)."""
        valid_models = ["sonnet", "opus", "claude-sonnet-4-6", "claude-opus-4-6", "haiku"]
        for model in valid_models:
            # Just check it doesn't return 400 for invalid model
            # It may return 502 if claude isn't available, that's fine
            status, data = api_request(TEST_PORT, body={
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
            })
            assert status != 400 or "Invalid model" not in data.get("error", {}).get("message", ""), \
                f"Model {model} was rejected as invalid"


class TestSyncHandler(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = start_server(TEST_PORT + 1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    @patch("subprocess.run")
    def test_sync_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "result": "Hello from Claude!",
                "input_tokens": 10,
                "output_tokens": 5,
            }),
            stderr="",
        )

        status, data = api_request(TEST_PORT + 1, body={
            "model": "sonnet",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "Hi"}],
        })

        assert status == 200
        assert data["type"] == "message"
        assert data["role"] == "assistant"
        assert data["content"][0]["text"] == "Hello from Claude!"
        assert data["model"] == "sonnet"
        assert data["usage"]["input_tokens"] == 10
        assert data["usage"]["output_tokens"] == 5
        assert data["id"].startswith("msg_")

    @patch("subprocess.run")
    def test_sync_strips_tool_blocks(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "result": "Answer\n<tool_call>foo</tool_call>\n<tool_result>bar</tool_result>\nDone",
            }),
            stderr="",
        )

        status, data = api_request(TEST_PORT + 1, body={
            "model": "sonnet",
            "messages": [{"role": "user", "content": "test"}],
        })

        assert status == 200
        text = data["content"][0]["text"]
        assert "<tool_call>" not in text
        assert "Answer" in text
        assert "Done" in text

    @patch("subprocess.run")
    def test_sync_claude_error(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Something went wrong",
        )

        status, data = api_request(TEST_PORT + 1, body={
            "model": "sonnet",
            "messages": [{"role": "user", "content": "fail"}],
        })

        assert status == 502
        assert "claude exited 1" in data["error"]["message"]

    @patch("subprocess.run")
    def test_sync_timeout(self, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=300)

        status, data = api_request(TEST_PORT + 1, body={
            "model": "sonnet",
            "messages": [{"role": "user", "content": "slow"}],
        })

        assert status == 504

    @patch("subprocess.run")
    def test_system_prompt_append(self, mock_run):
        """Default system prompt should use --append-system-prompt-file."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": "ok"}),
            stderr="",
        )

        api_request(TEST_PORT + 1, body={
            "model": "sonnet",
            "system": "Be concise.",
            "messages": [{"role": "user", "content": "hi"}],
        })

        cmd = mock_run.call_args[0][0]
        assert "--append-system-prompt-file" in cmd
        assert "--system-prompt-file" not in cmd

    @patch("subprocess.run")
    def test_system_prompt_replace(self, mock_run):
        """system_replace=true should use --system-prompt-file."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": "ok"}),
            stderr="",
        )

        api_request(TEST_PORT + 1, body={
            "model": "sonnet",
            "system": "You are a bot.",
            "system_replace": True,
            "messages": [{"role": "user", "content": "hi"}],
        })

        cmd = mock_run.call_args[0][0]
        assert "--system-prompt-file" in cmd
        # Make sure it's not the append variant
        assert "--append-system-prompt-file" not in cmd

    @patch("subprocess.run")
    def test_model_passed_to_cli(self, mock_run):
        """Model from request should be passed to claude via --model."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": "ok"}),
            stderr="",
        )

        api_request(TEST_PORT + 1, body={
            "model": "opus",
            "messages": [{"role": "user", "content": "hi"}],
        })

        cmd = mock_run.call_args[0][0]
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "opus"

    @patch("subprocess.run")
    def test_system_prompt_content_blocks(self, mock_run):
        """System prompt as array of content blocks should use append flag."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": "ok"}),
            stderr="",
        )

        api_request(TEST_PORT + 1, body={
            "model": "sonnet",
            "system": [
                {"type": "text", "text": "Rule 1."},
                {"type": "text", "text": "Rule 2."},
            ],
            "messages": [{"role": "user", "content": "hi"}],
        })

        cmd = mock_run.call_args[0][0]
        assert "--append-system-prompt-file" in cmd


if __name__ == "__main__":
    unittest.main()
