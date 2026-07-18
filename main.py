#!/usr/bin/env python3
"""Fail-closed entry point for the Cinema 4D Phase 1 MCP server."""

import logging
import sys
from pathlib import Path


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("cinema4d-mcp")


def main():
    project_src = Path(__file__).resolve().parent / "src"
    if str(project_src) not in sys.path:
        sys.path.insert(0, str(project_src))

    try:
        from cinema4d_mcp import main as package_main

        return package_main()
    except Exception as exc:
        logger.error("MCP server startup failed: %s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
