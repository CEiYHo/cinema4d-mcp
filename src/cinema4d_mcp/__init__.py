"""Secure Phase 1 MCP connectivity for Cinema 4D."""

__version__ = "0.2.0-phase1"

from . import server

def main():
    """Main entry point for the package."""
    server.mcp_app.run()

def main_wrapper():
    """Entry point for the wrapper script."""
    main()
