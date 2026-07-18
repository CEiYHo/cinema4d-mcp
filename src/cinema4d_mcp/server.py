"""Phase 2A.2 MCP server exposing a hardened read-only Cinema 4D surface."""

from __future__ import annotations

import asyncio
import json
import socket
import uuid
from typing import Annotated, Any, Dict, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .config import (
    C4D_HOST,
    CONNECT_TIMEOUT_SECONDS,
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    RESPONSE_TIMEOUT_SECONDS,
    SERVER_VERSION,
    get_c4d_port,
    get_c4d_token,
    validate_c4d_token,
)
from .utils import logger


ACTIVE_TOOL_NAMES = (
    "ping",
    "get_capabilities",
    "get_scene_info",
    "list_objects",
    "get_object",
)
OBJECT_ID_PREFIX = "c4d:"
UINT64_MAX = (1 << 64) - 1
DOCUMENT_SCOPE_HEX_LENGTH = 32
MAX_OBJECT_ID_LENGTH = (
    len(OBJECT_ID_PREFIX) + DOCUMENT_SCOPE_HEX_LENGTH + 1 + len(str(UINT64_MAX))
)
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 200
MCP_OFFSET = Annotated[int, Field(strict=True, ge=0)]
MCP_LIMIT = Annotated[int, Field(strict=True, ge=1, le=MAX_LIST_LIMIT)]


def _is_valid_object_id(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > MAX_OBJECT_ID_LENGTH:
        return False
    parts = value.split(":")
    if len(parts) != 3 or parts[0] != "c4d":
        return False
    document_scope, guid_text = parts[1], parts[2]
    if (
        len(document_scope) != DOCUMENT_SCOPE_HEX_LENGTH
        or not document_scope.isascii()
        or any(character not in "0123456789abcdef" for character in document_scope)
    ):
        return False
    if (
        not guid_text
        or not guid_text.isascii()
        or not guid_text.isdigit()
        or guid_text[0] == "0"
    ):
        return False
    guid = int(guid_text)
    return 1 <= guid <= UINT64_MAX


def _validated_list_params(params: Dict[str, Any]) -> Dict[str, Any]:
    if set(params) - {"offset", "limit"}:
        raise ValueError("list_objects received unsupported parameters")
    offset = params.get("offset", 0)
    limit = params.get("limit", DEFAULT_LIST_LIMIT)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be an integer greater than or equal to zero")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
    return {"offset": offset, "limit": limit}


def _validated_command_params(
    command: str,
    params: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    if command == "get_object":
        if set(params) != {"object_id"} or not _is_valid_object_id(
            params.get("object_id")
        ):
            raise ValueError("get_object requires one canonical object_id")
        return {"object_id": params["object_id"]}
    if command == "list_objects":
        return _validated_list_params(params)
    if command in ACTIVE_TOOL_NAMES:
        if params:
            raise ValueError("This command does not accept parameters")
        return {}
    return params


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


class Cinema4DFastMCP(FastMCP):
    """Validate raw tool arguments before FastMCP/Pydantic can coerce them."""

    async def call_tool(
        self,
        name: str,
        arguments: Dict[str, Any],
    ) -> Any:
        if name in ACTIVE_TOOL_NAMES:
            request_id = uuid.uuid4().hex
            try:
                _validated_command_params(name, arguments)
            except ValueError as exc:
                return _error_envelope(request_id, "INVALID_PARAMS", str(exc))
        return await super().call_tool(name, arguments)


def send_to_c4d(
    command: str,
    *,
    token: Optional[str] = None,
    request_id: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Send one authenticated command to the local read-only C4D bridge."""
    if command not in ACTIVE_TOOL_NAMES:
        return _error_envelope(
            request_id,
            "UNKNOWN_COMMAND",
            "Command is not available in Phase 2A.2",
        )

    request_id = request_id or uuid.uuid4().hex
    try:
        validated_params = _validated_command_params(command, params)
    except ValueError as exc:
        return _error_envelope(
            request_id,
            "INVALID_PARAMS",
            str(exc),
        )
    try:
        configured_token = (
            validate_c4d_token(token) if token is not None else get_c4d_token()
        )
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
        "params": validated_params,
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
                    user_action="Confirm the Phase 2A.2 bridge is running in Cinema 4D",
                )
            response_data += chunk
            if len(response_data) > MAX_FRAME_BYTES:
                return _error_envelope(
                    request_id,
                    "FRAME_TOO_LARGE",
                    "Cinema 4D response exceeds the bridge size limit",
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
            "Timed out waiting for the Cinema 4D Phase 2A.2 bridge",
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
            "Could not connect to the Cinema 4D Phase 2A.2 bridge",
            retryable=True,
            user_action="Start the authenticated Phase 2A.2 bridge in Cinema 4D",
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


mcp = Cinema4DFastMCP(name="Cinema4D")


@mcp.tool()
async def ping() -> Dict[str, Any]:
    """Check the authenticated end-to-end Cinema 4D bridge; has no side effects."""
    return await asyncio.to_thread(send_to_c4d, "ping")


@mcp.tool()
async def get_capabilities() -> Dict[str, Any]:
    """Report verified Phase 2A.2 capabilities; has no scene side effects."""
    return await asyncio.to_thread(send_to_c4d, "get_capabilities")


@mcp.tool()
async def get_scene_info() -> Dict[str, Any]:
    """Read active-document metadata, object count, and selected object IDs."""
    return await asyncio.to_thread(send_to_c4d, "get_scene_info")


@mcp.tool()
async def list_objects(
    offset: MCP_OFFSET = 0,
    limit: MCP_LIMIT = DEFAULT_LIST_LIMIT,
) -> Dict[str, Any]:
    """Read one bounded DFS hierarchy page; offset >= 0 and 1 <= limit <= 200."""
    return await asyncio.to_thread(
        send_to_c4d,
        "list_objects",
        params={"offset": offset, "limit": limit},
    )


@mcp.tool()
async def get_object(object_id: str) -> Dict[str, Any]:
    """Read one object by opaque ID; rotation_deg is relative H/P/B order."""
    return await asyncio.to_thread(
        send_to_c4d,
        "get_object",
        params={"object_id": object_id},
    )


mcp_app = mcp
