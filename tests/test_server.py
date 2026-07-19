"""Tests for the external Phase 2A MCP server boundary."""

from __future__ import annotations

import asyncio
import ast
import importlib.util
import inspect
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


from cinema4d_mcp import __version__, server
from cinema4d_mcp.config import SERVER_VERSION


TOKEN = "phase1-test-token-with-at-least-32-bytes"
OBJECT_ID = "c4d:{}:{}".format("a" * 32, "b" * 32)
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
    def test_runtime_version_labels_are_phase2b(self):
        self.assertEqual(__version__, "0.3.0-phase2b")
        self.assertEqual(SERVER_VERSION, "0.3.0-phase2b")

    def test_active_mcp_surface_has_exactly_nine_tools(self):
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

        expected = (
            "ping",
            "get_capabilities",
            "get_scene_info",
            "list_objects",
            "get_object",
            "create_object",
            "update_object",
            "delete_object",
        )
        self.assertEqual(tuple(decorated_tools), expected)
        self.assertEqual(server.ACTIVE_TOOL_NAMES, expected)
        signature = inspect.signature(server.list_objects)
        self.assertEqual(signature.parameters["offset"].default, 0)
        self.assertEqual(signature.parameters["limit"].default, 100)

    def test_mcp_1281_schema_and_raw_argument_validation_are_strict(self):
        tool = server.mcp._tool_manager.get_tool("list_objects")
        offset_schema = tool.parameters["properties"]["offset"]
        limit_schema = tool.parameters["properties"]["limit"]
        self.assertEqual(offset_schema["type"], "integer")
        self.assertEqual(offset_schema["minimum"], 0)
        self.assertEqual(limit_schema["type"], "integer")
        self.assertEqual(limit_schema["minimum"], 1)
        self.assertEqual(limit_schema["maximum"], 200)

        invalid_calls = (
            ("list_objects", {"offset": True, "limit": 100}),
            ("list_objects", {"offset": 0, "limit": 100, "extra": 1}),
            ("get_object", {"object_id": "c4d:invalid"}),
            ("get_object", {"object_id": OBJECT_ID, "extra": 1}),
            ("get_scene_info", {"extra": 1}),
        )
        with patch("cinema4d_mcp.server.socket.create_connection") as connect:
            for name, arguments in invalid_calls:
                with self.subTest(name=name, arguments=arguments):
                    response = asyncio.run(server.mcp.call_tool(name, arguments))
                    self.assertEqual(response["error"]["code"], "INVALID_PARAMS")

        connect.assert_not_called()

    def test_external_object_id_validator_matches_scope_canonical_contract(self):
        scope = "a" * 32
        for value in (
            "c4d:{}:{}".format(scope, "0" * 31 + "1"),
            "c4d:{}:{}".format(scope, "f" * 32),
        ):
            self.assertTrue(server._is_valid_object_id(value))
        for value in (
            "c4d:",
            "c4d:abc:def",
            "c4d:{}".format(scope),
            "c4d:{}:123".format(scope),
            "c4d:{}:{}".format(scope.upper(), "b" * 32),
            "c4d:{}:{}".format(scope, "B" * 32),
            "c4d:{}:{}:extra".format(scope, "b" * 32),
            " c4d:{}:{}".format(scope, "b" * 32),
            "c4d:{}:{} ".format(scope, "b" * 32),
            "c4d:{}:{}".format(scope, "g" * 32),
        ):
            self.assertFalse(server._is_valid_object_id(value))

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
    def test_get_object_sends_only_validated_object_id(self, create_connection):
        bridge_socket = MagicMock()
        bridge_socket.recv.return_value = (
            json.dumps(success_response()).encode("utf-8") + b"\n"
        )
        create_connection.return_value = bridge_socket

        response = server.send_to_c4d(
            "get_object",
            token=TOKEN,
            request_id="req-1",
            params={"object_id": OBJECT_ID},
        )

        self.assertTrue(response["ok"])
        sent = json.loads(bridge_socket.sendall.call_args.args[0].decode("utf-8"))
        self.assertEqual(sent["params"], {"object_id": OBJECT_ID})

    @patch("cinema4d_mcp.server.socket.create_connection")
    def test_invalid_object_params_fail_before_connect(self, create_connection):
        for params in (
            {},
            {"object_id": "Cube"},
            {"object_id": "c4d:0"},
            {"object_id": OBJECT_ID, "name": "Cube"},
        ):
            with self.subTest(params=params):
                response = server.send_to_c4d(
                    "get_object",
                    token=TOKEN,
                    request_id="req-1",
                    params=params,
                )
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        create_connection.assert_not_called()

    @patch("cinema4d_mcp.server.socket.create_connection")
    def test_list_objects_sends_validated_pagination(self, create_connection):
        bridge_socket = MagicMock()
        bridge_socket.recv.return_value = (
            json.dumps(success_response()).encode("utf-8") + b"\n"
        )
        create_connection.return_value = bridge_socket

        response = server.send_to_c4d(
            "list_objects",
            token=TOKEN,
            request_id="req-1",
            params={"offset": 12, "limit": 200},
        )

        self.assertTrue(response["ok"])
        sent = json.loads(bridge_socket.sendall.call_args.args[0].decode("utf-8"))
        self.assertEqual(sent["params"], {"offset": 12, "limit": 200})

    @patch("cinema4d_mcp.server.socket.create_connection")
    def test_command_specific_params_are_consistently_invalid(self, create_connection):
        invalid_cases = (
            ("get_object", {"object_id": "malformed"}),
            ("list_objects", {"offset": -1}),
            ("list_objects", {"offset": True}),
            ("list_objects", {"limit": 0}),
            ("list_objects", {"limit": False}),
            ("list_objects", {"limit": 201}),
            ("list_objects", {"offset": 0, "extra": 1}),
            ("get_scene_info", {"extra": 1}),
        )
        for command, params in invalid_cases:
            with self.subTest(command=command, params=params):
                response = server.send_to_c4d(
                    command,
                    token=TOKEN,
                    request_id="req-1",
                    params=params,
                )
                self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        create_connection.assert_not_called()

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
        response_payload["result"] = {"bridge_version": "0.3.0-phase2b"}
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
        self.assertEqual(response["result"]["bridge_version"], "0.3.0-phase2b")
        self.assertEqual(
            response["result"]["mcp_server_version"],
            "0.3.0-phase2b",
        )

    @patch("cinema4d_mcp.server.socket.create_connection")
    def test_legacy_command_is_rejected_without_connecting(self, create_connection):
        for command in (
            "save_document",
            "execute_python",
            "octane_command",
        ):
            with self.subTest(command=command):
                response = server.send_to_c4d(
                    command,
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

    def test_unsafe_configured_token_fails_before_connect(self):
        for invalid_token in ("short-token", "가" * 32, "!" * 32, "A" * 64):
            with self.subTest(token_kind=type(invalid_token).__name__):
                with patch("cinema4d_mcp.server.socket.create_connection") as connect:
                    response = server.send_to_c4d(
                        "ping",
                        token=invalid_token,
                        request_id="req-1",
                    )

                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], "CONFIG_ERROR")
                connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
