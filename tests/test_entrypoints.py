"""Fail-closed contract tests for every MCP process entry point."""

from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import cinema4d_mcp
from cinema4d_mcp import startup


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT_MAIN_PATH = PROJECT_ROOT / "main.py"
TOKEN = "phase1-test-token-with-at-least-32-bytes"


def load_root_entrypoint():
    spec = importlib.util.spec_from_file_location(
        "cinema4d_mcp_root_entrypoint_test",
        ROOT_MAIN_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EntrypointStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root_entrypoint = load_root_entrypoint()

    def test_missing_token_does_not_start_mcp_runtime(self):
        with patch.dict(os.environ, {"C4D_MCP_PORT": "5555"}, clear=True):
            with patch.object(startup, "_load_mcp_runtime") as load_runtime:
                with self.assertLogs("cinema4d-mcp", level="ERROR") as logs:
                    status = cinema4d_mcp.main()

        self.assertNotEqual(status, 0)
        load_runtime.assert_not_called()
        self.assertIn("C4D_MCP_TOKEN is required", "\n".join(logs.output))

    def test_invalid_token_does_not_start_or_log_secret(self):
        invalid_token = TOKEN + "!"
        environment = {
            "C4D_MCP_TOKEN": invalid_token,
            "C4D_MCP_PORT": "5555",
        }
        with patch.dict(os.environ, environment, clear=True):
            with patch.object(startup, "_load_mcp_runtime") as load_runtime:
                with self.assertLogs("cinema4d-mcp", level="ERROR") as logs:
                    status = cinema4d_mcp.main()

        rendered_logs = "\n".join(logs.output)
        self.assertNotEqual(status, 0)
        load_runtime.assert_not_called()
        self.assertIn("URL-safe ASCII", rendered_logs)
        self.assertNotIn(invalid_token, rendered_logs)

    def test_invalid_port_does_not_start_mcp_runtime(self):
        environment = {
            "C4D_MCP_TOKEN": TOKEN,
            "C4D_MCP_PORT": "not-a-port",
        }
        with patch.dict(os.environ, environment, clear=True):
            with patch.object(startup, "_load_mcp_runtime") as load_runtime:
                with self.assertLogs("cinema4d-mcp", level="ERROR") as logs:
                    status = cinema4d_mcp.main()

        self.assertNotEqual(status, 0)
        load_runtime.assert_not_called()
        self.assertIn("C4D_MCP_PORT must be an integer", "\n".join(logs.output))

    def test_valid_configuration_runs_mcp_runtime(self):
        environment = {
            "C4D_MCP_TOKEN": TOKEN,
            "C4D_MCP_PORT": "5555",
        }
        entrypoints = (
            cinema4d_mcp.main,
            cinema4d_mcp.main_wrapper,
            self.root_entrypoint.main,
        )
        for entrypoint in entrypoints:
            with self.subTest(entrypoint=entrypoint.__name__):
                runtime = MagicMock()
                with patch.dict(os.environ, environment, clear=True):
                    with patch.object(
                        startup,
                        "_load_mcp_runtime",
                        return_value=runtime,
                    ):
                        status = entrypoint()

                self.assertEqual(status, 0)
                runtime.run.assert_called_once_with()

    def test_main_and_main_wrapper_share_validation_policy(self):
        entrypoints = (
            cinema4d_mcp.main,
            cinema4d_mcp.main_wrapper,
            self.root_entrypoint.main,
        )
        invalid_environments = (
            {"C4D_MCP_PORT": "5555"},
            {"C4D_MCP_TOKEN": "short-token", "C4D_MCP_PORT": "5555"},
            {"C4D_MCP_TOKEN": TOKEN, "C4D_MCP_PORT": "70000"},
        )
        for entrypoint in entrypoints:
            for environment in invalid_environments:
                with self.subTest(
                    entrypoint=entrypoint.__name__,
                    environment_keys=tuple(sorted(environment)),
                ):
                    with patch.dict(os.environ, environment, clear=True):
                        with patch.object(
                            startup, "_load_mcp_runtime"
                        ) as load_runtime:
                            with self.assertLogs("cinema4d-mcp", level="ERROR"):
                                status = entrypoint()

                    self.assertEqual(status, 2)
                    load_runtime.assert_not_called()


if __name__ == "__main__":
    unittest.main()
