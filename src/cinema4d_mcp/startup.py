"""Shared fail-closed launcher for every MCP package entry point."""

import logging

from .config import validate_startup_configuration


logger = logging.getLogger("cinema4d-mcp")


def _load_mcp_runtime():
    """Import FastMCP only after startup configuration has been validated."""
    from .server import mcp_app

    return mcp_app


def run_mcp_runtime():
    """Validate configuration, run FastMCP, and return a process exit status."""
    try:
        validate_startup_configuration()
    except ValueError as exc:
        logger.error("MCP server startup configuration invalid: %s", exc)
        return 2

    try:
        _load_mcp_runtime().run()
    except Exception as exc:
        logger.error("MCP server startup failed: %s", type(exc).__name__)
        return 1
    return 0
