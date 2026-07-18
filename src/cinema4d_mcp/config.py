"""Fail-closed configuration for the Phase 2A.3 Cinema 4D bridge."""

import math
import os


C4D_HOST = "127.0.0.1"
DEFAULT_C4D_PORT = 5555
PROTOCOL_VERSION = 1
SERVER_VERSION = "0.2.0-phase2a3"
MAX_FRAME_BYTES = 64 * 1024
CONNECT_TIMEOUT_SECONDS = 2.0
RESPONSE_TIMEOUT_SECONDS = 5.0
TOKEN_MIN_LENGTH = 32
TOKEN_MAX_LENGTH = 256
TOKEN_MIN_ESTIMATED_ENTROPY_BITS = 128
TOKEN_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def get_c4d_port():
    raw_value = os.environ.get("C4D_MCP_PORT", str(DEFAULT_C4D_PORT))
    try:
        port = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError("C4D_MCP_PORT must be an integer")
    if not 1 <= port <= 65535:
        raise ValueError("C4D_MCP_PORT must be between 1 and 65535")
    return port


def validate_c4d_token(token):
    if not isinstance(token, str) or not token:
        raise ValueError("C4D_MCP_TOKEN is required")
    try:
        encoded = token.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        raise ValueError("C4D_MCP_TOKEN must use URL-safe ASCII characters")
    if not TOKEN_MIN_LENGTH <= len(encoded) <= TOKEN_MAX_LENGTH:
        raise ValueError(
            "C4D_MCP_TOKEN must be between {} and {} ASCII characters".format(
                TOKEN_MIN_LENGTH, TOKEN_MAX_LENGTH
            )
        )
    if any(character not in TOKEN_ALPHABET for character in token):
        raise ValueError("C4D_MCP_TOKEN must use URL-safe ASCII characters")
    counts = {}
    for character in token:
        counts[character] = counts.get(character, 0) + 1
    estimated_entropy_bits = 0.0
    for count in counts.values():
        probability = float(count) / len(token)
        estimated_entropy_bits -= count * math.log(probability, 2)
    if estimated_entropy_bits < TOKEN_MIN_ESTIMATED_ENTROPY_BITS:
        raise ValueError(
            "C4D_MCP_TOKEN must have at least {} bits of estimated entropy".format(
                TOKEN_MIN_ESTIMATED_ENTROPY_BITS
            )
        )
    return token


def get_c4d_token():
    return validate_c4d_token(os.environ.get("C4D_MCP_TOKEN"))


def validate_startup_configuration():
    """Validate all configuration required before FastMCP is imported."""
    return {
        "token": get_c4d_token(),
        "port": get_c4d_port(),
    }
