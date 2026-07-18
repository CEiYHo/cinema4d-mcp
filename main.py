#!/usr/bin/env python3
"""Fail-closed entry point for the Cinema 4D Phase 1 MCP server."""

import logging
import os
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

    if not os.environ.get("C4D_MCP_TOKEN"):
        logger.error(
            "C4D_MCP_TOKEN is required. Configure it for both Codex and Cinema 4D, "
            "then restart both applications."
        )
        return 2

    try:
        from cinema4d_mcp import main as package_main

        package_main()
        return 0
    except Exception as exc:
        logger.error("MCP server startup failed: %s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
