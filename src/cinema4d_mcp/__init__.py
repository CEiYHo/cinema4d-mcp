"""Secure Phase 2C MCP connectivity for Cinema 4D."""

__version__ = "0.4.0-phase2c"

from .startup import run_mcp_runtime


def main():
    """Validated entry point for the package console script."""
    return run_mcp_runtime()


def main_wrapper():
    """Validated entry point for the compatibility wrapper script."""
    return run_mcp_runtime()
