"""Instrumented real-STDIO FastMCP fixture for Phase 2C transport tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

from cinema4d_mcp import server


def _append_record(environment_name, record):
    target = os.environ.get(environment_name)
    if not target:
        return
    with Path(target).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()


def _fake_send_to_c4d(command, **kwargs):
    _append_record(
        "PHASE2C_STDIO_SEND_LOG",
        {
            "command": command,
            "params": kwargs.get("params"),
        },
    )
    return {
        "protocol_version": 1,
        "request_id": "phase2c-stdio-fixture",
        "ok": True,
        "result": {
            "saved": True,
            "document": {
                "name": "fixture.c4d",
                "path": r"C:\fixture",
                "format": "c4d",
            },
        },
        "error": None,
    }


def _forbidden_socket_connection(*args, **kwargs):
    _append_record(
        "PHASE2C_STDIO_SOCKET_LOG",
        {"socket_create_connection": True},
    )
    raise AssertionError("STDIO validation reached socket.create_connection")


server.send_to_c4d = _fake_send_to_c4d
server.socket.create_connection = _forbidden_socket_connection


if __name__ == "__main__":
    server.mcp.run()
