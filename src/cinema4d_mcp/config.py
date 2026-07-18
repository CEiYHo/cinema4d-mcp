"""Fail-closed configuration for the Phase 1 Cinema 4D bridge."""

import os


C4D_HOST = "127.0.0.1"
DEFAULT_C4D_PORT = 5555
PROTOCOL_VERSION = 1
SERVER_VERSION = "0.2.0-phase1"
MAX_FRAME_BYTES = 64 * 1024
CONNECT_TIMEOUT_SECONDS = 2.0
RESPONSE_TIMEOUT_SECONDS = 5.0


def get_c4d_port():
    raw_value = os.environ.get("C4D_MCP_PORT", str(DEFAULT_C4D_PORT))
    try:
        port = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError("C4D_MCP_PORT must be an integer")
    if not 1 <= port <= 65535:
        raise ValueError("C4D_MCP_PORT must be between 1 and 65535")
    return port


def get_c4d_token():
    token = os.environ.get("C4D_MCP_TOKEN")
    if not token:
        raise ValueError("C4D_MCP_TOKEN is required")
    return token
