"""Phase 1 MCP server exposing only connectivity and capability tools."""

from __future__ import annotations

import asyncio
import json
import socket
import uuid
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from .config import (
    C4D_HOST,
    CONNECT_TIMEOUT_SECONDS,
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    RESPONSE_TIMEOUT_SECONDS,
    SERVER_VERSION,
    get_c4d_port,
    get_c4d_token,
)
from .utils import logger


ACTIVE_TOOL_NAMES = ("ping", "get_capabilities")


def _error_envelope(
    request_id: Optional[str],
    code: str,
    message: str,
    *,
    retryable: bool = False,
    user_action: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": False,
        "result": None,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "user_action": user_action,
            "details": details or {},
        },
    }


def _validate_response(response: Any, request_id: str) -> Optional[Dict[str, Any]]:
    if not isinstance(response, dict):
        return _error_envelope(
            request_id,
            "INVALID_RESPONSE",
            "Cinema 4D returned a non-object response",
        )
    if response.get("protocol_version") != PROTOCOL_VERSION:
        return _error_envelope(
            request_id,
            "PROTOCOL_MISMATCH",
            "Cinema 4D bridge protocol does not match the MCP server",
            user_action="Install matching MCP server and Cinema 4D plugin versions",
            details={
                "expected": PROTOCOL_VERSION,
                "received": response.get("protocol_version"),
            },
        )
    if response.get("request_id") != request_id:
        return _error_envelope(
            request_id,
            "INVALID_RESPONSE",
            "Cinema 4D returned a mismatched request_id",
        )
    if response.get("ok") not in (True, False):
        return _error_envelope(
            request_id,
            "INVALID_RESPONSE",
            "Cinema 4D response is missing an ok flag",
        )
    if response["ok"]:
        if not isinstance(response.get("result"), dict) or response.get("error") is not None:
            return _error_envelope(
                request_id,
                "INVALID_RESPONSE",
                "Cinema 4D returned an invalid success envelope",
            )
    else:
        error = response.get("error")
        if not isinstance(error, dict) or not isinstance(error.get("code"), str):
            return _error_envelope(
                request_id,
                "INVALID_RESPONSE",
                "Cinema 4D returned an invalid error envelope",
            )
    return None


def send_to_c4d(
    command: str,
    *,
    token: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Send one authenticated Phase 1 command to the local C4D bridge."""
    if command not in ACTIVE_TOOL_NAMES:
        return _error_envelope(
            request_id,
            "UNKNOWN_COMMAND",
            "Command is not available in Phase 1",
        )

    request_id = request_id or uuid.uuid4().hex
    try:
        configured_token = token if token is not None else get_c4d_token()
        port = get_c4d_port()
    except ValueError as exc:
        return _error_envelope(
            request_id,
            "CONFIG_ERROR",
            str(exc),
            user_action="Configure C4D_MCP_TOKEN and a valid C4D_MCP_PORT, then restart both applications",
        )

    request = {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "command": command,
        "token": configured_token,
        "params": {},
    }
    payload = json.dumps(
        request,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8") + b"\n"

    bridge_socket = None
    try:
        bridge_socket = socket.create_connection(
            (C4D_HOST, port),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
        bridge_socket.settimeout(RESPONSE_TIMEOUT_SECONDS)
        bridge_socket.sendall(payload)

        response_data = b""
        while b"\n" not in response_data:
            chunk = bridge_socket.recv(4096)
            if not chunk:
                return _error_envelope(
                    request_id,
                    "C4D_UNAVAILABLE",
                    "Cinema 4D closed the connection without a response",
                    retryable=True,
                    user_action="Confirm the Phase 1 bridge is running in Cinema 4D",
                )
            response_data += chunk
            if len(response_data) > MAX_FRAME_BYTES:
                return _error_envelope(
                    request_id,
                    "FRAME_TOO_LARGE",
                    "Cinema 4D response exceeds the Phase 1 size limit",
                )

        frame, trailing = response_data.split(b"\n", 1)
        if trailing.strip():
            return _error_envelope(
                request_id,
                "INVALID_RESPONSE",
                "Cinema 4D returned more than one response frame",
            )
        try:
            response_text = frame.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return _error_envelope(
                request_id,
                "INVALID_UTF8",
                "Cinema 4D response is not valid UTF-8",
            )
        try:
            response = json.loads(response_text)
        except (TypeError, ValueError):
            return _error_envelope(
                request_id,
                "MALFORMED_JSON",
                "Cinema 4D response is not valid JSON",
            )

        validation_error = _validate_response(response, request_id)
        if validation_error is not None:
            return validation_error
        if command == "get_capabilities" and response["ok"]:
            response["result"] = dict(response["result"])
            response["result"]["mcp_server_version"] = SERVER_VERSION
        return response
    except socket.timeout:
        return _error_envelope(
            request_id,
            "C4D_TIMEOUT",
            "Timed out waiting for the Cinema 4D Phase 1 bridge",
            retryable=True,
            user_action="Confirm Cinema 4D is responsive and retry ping",
        )
    except (ConnectionRefusedError, ConnectionAbortedError, ConnectionResetError, OSError) as exc:
        logger.warning(
            "Cinema 4D bridge connection failed for request_id=%s error=%s",
            request_id,
            type(exc).__name__,
        )
        return _error_envelope(
            request_id,
            "C4D_UNAVAILABLE",
            "Could not connect to the Cinema 4D Phase 1 bridge",
            retryable=True,
            user_action="Start the authenticated Phase 1 bridge in Cinema 4D",
        )
    except Exception as exc:
        logger.error(
            "Unexpected bridge failure for request_id=%s error=%s",
            request_id,
            type(exc).__name__,
        )
        return _error_envelope(
            request_id,
            "INTERNAL_ERROR",
            "The MCP server could not complete the bridge request",
        )
    finally:
        if bridge_socket is not None:
            try:
                bridge_socket.close()
            except OSError:
                pass


mcp = FastMCP(name="Cinema4D")


@mcp.tool()
async def ping() -> Dict[str, Any]:
    """Check the authenticated end-to-end Cinema 4D bridge; has no side effects."""
    return await asyncio.to_thread(send_to_c4d, "ping")


@mcp.tool()
async def get_capabilities() -> Dict[str, Any]:
    """Report verified Phase 1 runtime capabilities; has no scene side effects."""
    return await asyncio.to_thread(send_to_c4d, "get_capabilities")


mcp_app = mcp
