#!/usr/bin/env python3
"""Manual Phase 2A transport certification harness for the C4D bridge.

Run this only while the Phase 2A plugin is loaded and its server is Online. Each
case opens a new TCP connection. The token is read from ``C4D_MCP_TOKEN`` and is
never printed.
"""

from __future__ import annotations

import json
import math
import os
import socket
import sys
import uuid


HOST = "127.0.0.1"
DEFAULT_PORT = 5555
PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 64 * 1024
TOKEN_MIN_LENGTH = 32
TOKEN_MAX_LENGTH = 256
TOKEN_MIN_ESTIMATED_ENTROPY_BITS = 128
TOKEN_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


class SmokeFailure(RuntimeError):
    pass


def configured_port():
    try:
        port = int(os.environ.get("C4D_MCP_PORT", str(DEFAULT_PORT)))
    except ValueError:
        raise SmokeFailure("C4D_MCP_PORT must be an integer")
    if not 1 <= port <= 65535:
        raise SmokeFailure("C4D_MCP_PORT must be between 1 and 65535")
    return port


def configured_token():
    token = os.environ.get("C4D_MCP_TOKEN")
    if not token:
        raise SmokeFailure("C4D_MCP_TOKEN is required")
    try:
        encoded = token.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        raise SmokeFailure("C4D_MCP_TOKEN must use URL-safe ASCII characters")
    if not TOKEN_MIN_LENGTH <= len(encoded) <= TOKEN_MAX_LENGTH:
        raise SmokeFailure(
            "C4D_MCP_TOKEN must be between 32 and 256 ASCII characters"
        )
    if any(character not in TOKEN_ALPHABET for character in token):
        raise SmokeFailure("C4D_MCP_TOKEN must use URL-safe ASCII characters")
    counts = {}
    for character in token:
        counts[character] = counts.get(character, 0) + 1
    estimated_entropy_bits = 0.0
    for count in counts.values():
        probability = float(count) / len(token)
        estimated_entropy_bits -= count * math.log(probability, 2)
    if estimated_entropy_bits < TOKEN_MIN_ESTIMATED_ENTROPY_BITS:
        raise SmokeFailure(
            "C4D_MCP_TOKEN must have at least 128 bits of estimated entropy"
        )
    return token


def request_frame(command, token, protocol_version=PROTOCOL_VERSION, include_token=True):
    request = {
        "protocol_version": protocol_version,
        "request_id": uuid.uuid4().hex,
        "command": command,
        "params": {},
    }
    if include_token:
        request["token"] = token
    return json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"


def exchange(frame, port):
    """Send one raw frame over a fresh connection and parse one response."""
    client = socket.create_connection((HOST, port), timeout=2.0)
    try:
        client.settimeout(5.0)
        client.sendall(frame)
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError:
            pass

        response_data = b""
        while b"\n" not in response_data:
            chunk = client.recv(4096)
            if not chunk:
                break
            response_data += chunk
            if len(response_data) > MAX_FRAME_BYTES:
                raise SmokeFailure("response exceeded the bridge size limit")
    finally:
        client.close()

    if b"\n" not in response_data:
        raise SmokeFailure("bridge closed without a complete response")
    frame_bytes, trailing = response_data.split(b"\n", 1)
    if trailing.strip():
        raise SmokeFailure("bridge returned more than one response")
    try:
        return json.loads(frame_bytes.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SmokeFailure("bridge returned an invalid response: {}".format(type(exc).__name__))


def expect_success(name, response):
    if response.get("ok") is not True:
        raise SmokeFailure("{} expected success, got {}".format(name, response.get("error")))
    print("PASS {}".format(name))


def expect_error(name, response, code):
    actual = (response.get("error") or {}).get("code")
    if response.get("ok") is not False or actual != code:
        raise SmokeFailure("{} expected {}, got {}".format(name, code, actual))
    print("PASS {} ({})".format(name, code))


def verify_capabilities(response):
    result = response.get("result") or {}
    if result.get("protocol_version") != PROTOCOL_VERSION:
        raise SmokeFailure("capabilities reported an unexpected protocol version")
    if not isinstance(result.get("bridge_version"), str):
        raise SmokeFailure("capabilities omitted bridge_version")
    if result.get("tools") != [
        "ping",
        "get_capabilities",
        "get_scene_info",
        "list_objects",
        "get_object",
    ]:
        raise SmokeFailure("capability tool list is not the Phase 2A surface")

    cinema4d = result.get("cinema4d") or {}
    if cinema4d.get("version") != "2023.2.2":
        raise SmokeFailure(
            "expected Cinema 4D 2023.2.2, got {}".format(
                cinema4d.get("version")
            )
        )
    if not isinstance(cinema4d.get("version_raw"), int):
        raise SmokeFailure("capabilities omitted the raw Cinema 4D version")
    if not isinstance(cinema4d.get("python_version"), str):
        raise SmokeFailure("capabilities omitted the Cinema 4D Python version")

    octane = ((result.get("renderers") or {}).get("octane") or {})
    if octane.get("installed") not in (True, False, None):
        raise SmokeFailure("Octane installed state is invalid")
    if not isinstance(octane.get("detection"), str):
        raise SmokeFailure("Octane detection state is missing")
    if octane.get("installed") is None and octane.get("detection") != "unverified":
        raise SmokeFailure("unverified Octane detection must not guess installed=false")
    if octane.get("version") is not None and not isinstance(
        octane.get("version"), str
    ):
        raise SmokeFailure("verified Octane version must be a string")

    print(
        "INFO Cinema 4D {} (raw {}), Python {}, bridge {}, Octane {}".format(
            cinema4d["version"],
            cinema4d["version_raw"],
            cinema4d["python_version"],
            result["bridge_version"],
            octane["detection"],
        )
    )


def main():
    try:
        port = configured_port()
        token = configured_token()

        expect_success("ping", exchange(request_frame("ping", token), port))

        capabilities = exchange(request_frame("get_capabilities", token), port)
        expect_success("get_capabilities", capabilities)
        verify_capabilities(capabilities)

        expect_error(
            "missing token",
            exchange(request_frame("ping", token, include_token=False), port),
            "AUTH_REQUIRED",
        )

        wrong_token = "A" * max(TOKEN_MIN_LENGTH, len(token))
        if wrong_token == token:
            wrong_token = "B" * len(wrong_token)
        expect_error(
            "wrong token",
            exchange(request_frame("ping", wrong_token), port),
            "AUTH_FAILED",
        )

        expect_error(
            "protocol mismatch",
            exchange(request_frame("ping", token, protocol_version=99), port),
            "PROTOCOL_MISMATCH",
        )
        expect_error(
            "malformed JSON",
            exchange(b'{"protocol_version":1,\n', port),
            "MALFORMED_JSON",
        )
        expect_error(
            "invalid UTF-8",
            exchange(b"\xff\xfe\n", port),
            "INVALID_UTF8",
        )
        expect_error(
            "oversized frame",
            exchange((b"x" * (MAX_FRAME_BYTES + 1)) + b"\n", port),
            "FRAME_TOO_LARGE",
        )

        expect_success(
            "recovery ping after errors",
            exchange(request_frame("ping", token), port),
        )
        print("Phase 2A transport smoke certification passed")
        return 0
    except (OSError, SmokeFailure, KeyError) as exc:
        print("FAIL {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
