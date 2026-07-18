"""Phase 2B MCP server exposing typed Cinema 4D object mutations."""

from __future__ import annotations

import asyncio
import json
import math
import socket
import unicodedata
import uuid
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from mcp.server.fastmcp import FastMCP
from pydantic import Field, StrictBool, StrictFloat, StrictInt

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
    "create_object",
    "update_object",
    "delete_object",
    "undo_last",
)
WRITE_TOOL_NAMES = frozenset(
    ("create_object", "update_object", "delete_object", "undo_last")
)
CREATE_OBJECT_TYPES = frozenset(
    ("null", "cube", "sphere", "plane", "cylinder", "cone")
)
OBJECT_ID_PREFIX = "c4d:"
DOCUMENT_SCOPE_HEX_LENGTH = 32
OBJECT_SCOPE_HEX_LENGTH = 32
MAX_OBJECT_ID_LENGTH = (
    len(OBJECT_ID_PREFIX)
    + DOCUMENT_SCOPE_HEX_LENGTH
    + 1
    + OBJECT_SCOPE_HEX_LENGTH
)
MUTATION_ID_PREFIX = "mut:"
MUTATION_SCOPE_HEX_LENGTH = 32
MAX_MUTATION_ID_LENGTH = len(MUTATION_ID_PREFIX) + MUTATION_SCOPE_HEX_LENGTH
MAX_OBJECT_NAME_LENGTH = 255
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 200
MCP_OFFSET = Annotated[int, Field(strict=True, ge=0)]
MCP_LIMIT = Annotated[int, Field(strict=True, ge=1, le=MAX_LIST_LIMIT)]
MCP_VECTOR = Annotated[
    List[Union[StrictInt, StrictFloat]],
    Field(min_length=3, max_length=3),
]
MCP_CREATE_TYPE = Literal["null", "cube", "sphere", "plane", "cylinder", "cone"]
MCP_NAME = Annotated[str, Field(max_length=MAX_OBJECT_NAME_LENGTH)]
MCP_OBJECT_ID = Annotated[
    str,
    Field(pattern=r"^c4d:[0-9a-f]{32}:[0-9a-f]{32}$"),
]
MCP_MUTATION_ID = Annotated[str, Field(pattern=r"^mut:[0-9a-f]{32}$")]


class _CommandValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _is_valid_object_id(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > MAX_OBJECT_ID_LENGTH:
        return False
    parts = value.split(":")
    if len(parts) != 3 or parts[0] != "c4d":
        return False
    document_scope, object_scope = parts[1], parts[2]
    if (
        len(document_scope) != DOCUMENT_SCOPE_HEX_LENGTH
        or not document_scope.isascii()
        or any(character not in "0123456789abcdef" for character in document_scope)
    ):
        return False
    if (
        len(object_scope) != OBJECT_SCOPE_HEX_LENGTH
        or not object_scope.isascii()
        or any(character not in "0123456789abcdef" for character in object_scope)
    ):
        return False
    return True


def _is_valid_mutation_id(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != MAX_MUTATION_ID_LENGTH:
        return False
    if not value.startswith(MUTATION_ID_PREFIX):
        return False
    mutation_scope = value[len(MUTATION_ID_PREFIX):]
    return (
        len(mutation_scope) == MUTATION_SCOPE_HEX_LENGTH
        and mutation_scope.isascii()
        and all(character in "0123456789abcdef" for character in mutation_scope)
    )


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


def _validated_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("name must be a string")
    if len(value) > MAX_OBJECT_NAME_LENGTH:
        raise ValueError(
            f"name must contain at most {MAX_OBJECT_NAME_LENGTH} characters"
        )
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("name must not contain control characters")
    return value


def _validated_vector(value: Any, field_name: str) -> List[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{field_name} must be an array of exactly 3 numbers")
    values = []
    for component in value:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise ValueError(f"{field_name} must contain finite numbers")
        try:
            normalized = float(component)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{field_name} must contain finite numbers")
        if not math.isfinite(normalized):
            raise ValueError(f"{field_name} must contain finite numbers")
        values.append(normalized)
    return values


def _validated_create_params(params: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {"type", "name", "position", "rotation_deg", "scale"}
    if set(params) - allowed or "type" not in params:
        raise ValueError("create_object requires type and only typed creation fields")
    object_type = params.get("type")
    if not isinstance(object_type, str) or object_type not in CREATE_OBJECT_TYPES:
        raise _CommandValidationError(
            "UNSUPPORTED_OBJECT_TYPE",
            f"type must be one of: {', '.join(sorted(CREATE_OBJECT_TYPES))}",
        )
    validated: Dict[str, Any] = {"type": object_type}
    if "name" in params:
        validated["name"] = _validated_name(params["name"])
    for field_name in ("position", "rotation_deg", "scale"):
        if field_name in params:
            validated[field_name] = _validated_vector(
                params[field_name], field_name
            )
    return validated


def _validated_update_params(params: Dict[str, Any]) -> Dict[str, Any]:
    mutable_fields = {"name", "position", "rotation_deg", "scale"}
    allowed = mutable_fields | {"object_id"}
    if set(params) - allowed or "object_id" not in params:
        raise ValueError("update_object requires object_id and only typed fields")
    if not _is_valid_object_id(params.get("object_id")):
        raise ValueError("update_object requires one canonical object_id")
    if not set(params).intersection(mutable_fields):
        raise ValueError("update_object requires at least one mutable field")
    validated: Dict[str, Any] = {"object_id": params["object_id"]}
    if "name" in params:
        validated["name"] = _validated_name(params["name"])
    for field_name in ("position", "rotation_deg", "scale"):
        if field_name in params:
            validated[field_name] = _validated_vector(
                params[field_name], field_name
            )
    return validated


def _validated_delete_params(params: Dict[str, Any]) -> Dict[str, Any]:
    if set(params) - {"object_id", "recursive"} or "object_id" not in params:
        raise ValueError("delete_object requires object_id and optional recursive")
    if not _is_valid_object_id(params.get("object_id")):
        raise ValueError("delete_object requires one canonical object_id")
    recursive = params.get("recursive", False)
    if not isinstance(recursive, bool):
        raise ValueError("recursive must be a boolean")
    return {"object_id": params["object_id"], "recursive": recursive}


def _validated_undo_params(params: Dict[str, Any]) -> Dict[str, Any]:
    if set(params) != {"mutation_id"} or not _is_valid_mutation_id(
        params.get("mutation_id")
    ):
        raise ValueError("undo_last requires one canonical mutation_id")
    return {"mutation_id": params["mutation_id"]}


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
    if command == "create_object":
        return _validated_create_params(params)
    if command == "update_object":
        return _validated_update_params(params)
    if command == "delete_object":
        return _validated_delete_params(params)
    if command == "undo_last":
        return _validated_undo_params(params)
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
            except _CommandValidationError as exc:
                return _error_envelope(request_id, exc.code, str(exc))
            except ValueError as exc:
                return _error_envelope(request_id, "INVALID_PARAMS", str(exc))
        return await super().call_tool(name, arguments)


def _post_delivery_failure(
    command: str,
    request_id: str,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    user_action: Optional[str] = None,
) -> Dict[str, Any]:
    if command in WRITE_TOOL_NAMES:
        return _error_envelope(
            request_id,
            "OUTCOME_UNKNOWN",
            "The mutation may have reached Cinema 4D but no verified response was received",
            retryable=False,
            user_action="Inspect the scene before issuing another mutation",
        )
    return _error_envelope(
        request_id,
        code,
        message,
        retryable=retryable,
        user_action=user_action,
    )


def send_to_c4d(
    command: str,
    *,
    token: Optional[str] = None,
    request_id: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Send one authenticated command to the local C4D bridge without retries."""
    if command not in ACTIVE_TOOL_NAMES:
        return _error_envelope(
            request_id,
            "UNKNOWN_COMMAND",
            "Command is not available in Phase 2B",
        )

    request_id = request_id or uuid.uuid4().hex
    try:
        validated_params = _validated_command_params(command, params)
    except _CommandValidationError as exc:
        return _error_envelope(request_id, exc.code, str(exc))
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
    delivery_started = False
    try:
        bridge_socket = socket.create_connection(
            (C4D_HOST, port),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
        bridge_socket.settimeout(RESPONSE_TIMEOUT_SECONDS)
        delivery_started = True
        bridge_socket.sendall(payload)

        response_data = b""
        while b"\n" not in response_data:
            chunk = bridge_socket.recv(4096)
            if not chunk:
                return _post_delivery_failure(
                    command,
                    request_id,
                    "C4D_UNAVAILABLE",
                    "Cinema 4D closed the connection without a response",
                    retryable=True,
                    user_action="Confirm the Phase 2B bridge is running in Cinema 4D",
                )
            response_data += chunk
            if len(response_data) > MAX_FRAME_BYTES:
                return _post_delivery_failure(
                    command,
                    request_id,
                    "FRAME_TOO_LARGE",
                    "Cinema 4D response exceeds the bridge size limit",
                )

        frame, trailing = response_data.split(b"\n", 1)
        if trailing.strip():
            return _post_delivery_failure(
                command,
                request_id,
                "INVALID_RESPONSE",
                "Cinema 4D returned more than one response frame",
            )
        try:
            response_text = frame.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return _post_delivery_failure(
                command,
                request_id,
                "INVALID_UTF8",
                "Cinema 4D response is not valid UTF-8",
            )
        try:
            response = json.loads(response_text)
        except (TypeError, ValueError):
            return _post_delivery_failure(
                command,
                request_id,
                "MALFORMED_JSON",
                "Cinema 4D response is not valid JSON",
            )

        validation_error = _validate_response(response, request_id)
        if validation_error is not None:
            if command in WRITE_TOOL_NAMES:
                return _post_delivery_failure(
                    command,
                    request_id,
                    "INVALID_RESPONSE",
                    "Cinema 4D returned an invalid response",
                )
            return validation_error
        if (
            not response["ok"]
            and response["error"].get("code") == "OUTCOME_UNKNOWN"
        ):
            response["error"] = dict(response["error"])
            response["error"]["retryable"] = False
        if command == "get_capabilities" and response["ok"]:
            response["result"] = dict(response["result"])
            response["result"]["mcp_server_version"] = SERVER_VERSION
        return response
    except socket.timeout:
        if delivery_started:
            return _post_delivery_failure(
                command,
                request_id,
                "C4D_TIMEOUT",
                "Timed out waiting for the Cinema 4D Phase 2B bridge",
                retryable=True,
                user_action="Confirm Cinema 4D is responsive and retry the read",
            )
        return _error_envelope(
            request_id,
            "C4D_UNAVAILABLE",
            "Timed out before the request was sent to Cinema 4D",
            retryable=True,
        )
    except (ConnectionRefusedError, ConnectionAbortedError, ConnectionResetError, OSError) as exc:
        logger.warning(
            "Cinema 4D bridge connection failed for request_id=%s error=%s",
            request_id,
            type(exc).__name__,
        )
        if delivery_started:
            return _post_delivery_failure(
                command,
                request_id,
                "C4D_UNAVAILABLE",
                "The Cinema 4D connection failed after request delivery began",
                retryable=True,
            )
        return _error_envelope(
            request_id,
            "C4D_UNAVAILABLE",
            "Could not connect to the Cinema 4D Phase 2B bridge",
            retryable=True,
            user_action="Start the authenticated Phase 2B bridge in Cinema 4D",
        )
    except Exception as exc:
        logger.error(
            "Unexpected bridge failure for request_id=%s error=%s",
            request_id,
            type(exc).__name__,
        )
        if delivery_started:
            return _post_delivery_failure(
                command,
                request_id,
                "INTERNAL_ERROR",
                "The MCP server failed after request delivery began",
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
    """Report the verified Phase 2B read, typed mutation, and undo surface."""
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


@mcp.tool()
async def create_object(
    type: MCP_CREATE_TYPE,
    name: MCP_NAME = None,
    position: MCP_VECTOR = None,
    rotation_deg: MCP_VECTOR = None,
    scale: MCP_VECTOR = None,
) -> Dict[str, Any]:
    """Create one top-level allowlisted object; rotation_deg uses relative H/P/B."""
    params: Dict[str, Any] = {"type": type}
    for field_name, value in (
        ("name", name),
        ("position", position),
        ("rotation_deg", rotation_deg),
        ("scale", scale),
    ):
        if value is not None:
            params[field_name] = value
    return await asyncio.to_thread(send_to_c4d, "create_object", params=params)


@mcp.tool()
async def update_object(
    object_id: MCP_OBJECT_ID,
    name: MCP_NAME = None,
    position: MCP_VECTOR = None,
    rotation_deg: MCP_VECTOR = None,
    scale: MCP_VECTOR = None,
) -> Dict[str, Any]:
    """Update only name or relative H/P/B PSR fields of one resolved object."""
    params: Dict[str, Any] = {"object_id": object_id}
    for field_name, value in (
        ("name", name),
        ("position", position),
        ("rotation_deg", rotation_deg),
        ("scale", scale),
    ):
        if value is not None:
            params[field_name] = value
    return await asyncio.to_thread(send_to_c4d, "update_object", params=params)


@mcp.tool()
async def delete_object(
    object_id: MCP_OBJECT_ID,
    recursive: StrictBool = False,
) -> Dict[str, Any]:
    """Delete one resolved object; child subtrees require recursive=true."""
    return await asyncio.to_thread(
        send_to_c4d,
        "delete_object",
        params={"object_id": object_id, "recursive": recursive},
    )


@mcp.tool()
async def undo_last(mutation_id: MCP_MUTATION_ID) -> Dict[str, Any]:
    """Undo only the verified top MCP mutation for the active document."""
    return await asyncio.to_thread(
        send_to_c4d,
        "undo_last",
        params={"mutation_id": mutation_id},
    )


mcp_app = mcp
