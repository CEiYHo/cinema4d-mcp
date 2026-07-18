"""Tests for the external Phase 1 MCP server boundary."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import socket
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


if importlib.util.find_spec("mcp") is None:
    mcp_module = types.ModuleType("mcp")
    mcp_server_module = types.ModuleType("mcp.server")
    fastmcp_module = types.ModuleType("mcp.server.fastmcp")

    class FastMCP:
        def __init__(self, name):
            self.name = name
            self.registered_tools = []

        def tool(self):
            def decorator(function):
                self.registered_tools.append(function.__name__)
                return function

            return decorator

        def run(self):
            return None

    fastmcp_module.FastMCP = FastMCP
    sys.modules["mcp"] = mcp_module
    sys.modules["mcp.server"] = mcp_server_module
    sys.modules["mcp.server.fastmcp"] = fastmcp_module


from cinema4d_mcp import server


TOKEN = "phase1-test-token-with-at-least-32-bytes"
SERVER_PATH = Path(__file__).resolve().parents[1] / "src" / "cinema4d_mcp" / "server.py"


def success_response(request_id="req-1"):
    return {
        "protocol_version": 1,
        "request_id": request_id,
        "ok": True,
        "result": {"status": "ok"},
        "error": None,
    }


class ExternalServerTests(unittest.TestCase):
    def test_active_mcp_surface_has_exactly_two_tools(self):
        source = SERVER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        decorated_tools = []
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id == "mcp"
                    and decorator.func.attr == "tool"
                ):
                    decorated_tools.append(node.name)

        self.assertEqual(tuple(decorated_tools), ("ping", "get_capabilities"))
        self.assertEqual(server.ACTIVE_TOOL_NAMES, ("ping", "get_capabilities"))

    @patch("cinema4d_mcp.server.socket.create_connection")
    def test_authenticated_request_uses_versioned_envelope(self, create_connection):
        bridge_socket = MagicMock()
        payload = json.dumps(success_response()).encode("utf-8") + b"\n"
        bridge_socket.recv.side_effect = [payload[:12], payload[12:]]
        create_connection.return_value = bridge_socket

        response = server.send_to_c4d("ping", token=TOKEN, request_id="req-1")

        self.assertTrue(response["ok"])
        sent = json.loads(bridge_socket.sendall.call_args.args[0].decode("utf-8"))
        self.assertEqual(
            set(sent),
            {"protocol_version", "request_id", "command", "token", "params"},
        )
        self.assertEqual(sent["protocol_version"], 1)
        self.assertEqual(sent["request_id"], "req-1")
        self.assertEqual(sent["command"], "ping")
        self.assertEqual(sent["token"], TOKEN)
        self.assertEqual(sent["params"], {})
        create_connection.assert_called_once_with(("127.0.0.1", 5555), timeout=2.0)
        bridge_socket.close.assert_called_once()

    @patch("cinema4d_mcp.server.socket.create_connection")
    def test_connection_refused_is_structured(self, create_connection):
        create_connection.side_effect = ConnectionRefusedError()

        response = server.send_to_c4d("ping", token=TOKEN, request_id="req-1")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "C4D_UNAVAILABLE")
        self.assertTrue(response["error"]["retryable"])

    @patch("cinema4d_mcp.server.socket.create_connection")
    def test_response_timeout_is_distinct(self, create_connection):
        bridge_socket = MagicMock()
        bridge_socket.recv.side_effect = socket.timeout()
        create_connection.return_value = bridge_socket

        response = server.send_to_c4d("ping", token=TOKEN, request_id="req-1")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "C4D_TIMEOUT")

    @patch("cinema4d_mcp.server.socket.create_connection")
    def test_protocol_mismatch_fails_closed(self, create_connection):
        bridge_socket = MagicMock()
        mismatched = success_response()
        mismatched["protocol_version"] = 99
        bridge_socket.recv.return_value = json.dumps(mismatched).encode("utf-8") + b"\n"
        create_connection.return_value = bridge_socket

        response = server.send_to_c4d("ping", token=TOKEN, request_id="req-1")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "PROTOCOL_MISMATCH")

    @patch("cinema4d_mcp.server.socket.create_connection")
    def test_capabilities_include_external_server_version(self, create_connection):
        bridge_socket = MagicMock()
        response_payload = success_response()
        response_payload["result"] = {"bridge_version": "0.2.0-phase1"}
        bridge_socket.recv.return_value = (
            json.dumps(response_payload).encode("utf-8") + b"\n"
        )
        create_connection.return_value = bridge_socket

        response = server.send_to_c4d(
            "get_capabilities",
            token=TOKEN,
            request_id="req-1",
        )

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["bridge_version"], "0.2.0-phase1")
        self.assertEqual(response["result"]["mcp_server_version"], "0.2.0-phase1")

    @patch("cinema4d_mcp.server.socket.create_connection")
    def test_legacy_command_is_rejected_without_connecting(self, create_connection):
        response = server.send_to_c4d(
            "execute_python",
            token=TOKEN,
            request_id="req-1",
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "UNKNOWN_COMMAND")
        create_connection.assert_not_called()

    def test_missing_token_fails_before_connect(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("cinema4d_mcp.server.socket.create_connection") as connect:
                response = server.send_to_c4d("ping", request_id="req-1")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "CONFIG_ERROR")
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
