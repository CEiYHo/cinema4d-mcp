"""Secure Phase 2A.1 MCP connectivity for Cinema 4D."""

__version__ = "0.2.0-phase2a1"

from .startup import run_mcp_runtime


def main():
    """Validated entry point for the package console script."""
    return run_mcp_runtime()


def main_wrapper():
    """Validated entry point for the compatibility wrapper script."""
    return run_mcp_runtime()
