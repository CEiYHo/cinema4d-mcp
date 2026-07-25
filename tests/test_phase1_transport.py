"""Contract tests for the Cinema 4D Phase 1 socket boundary.

These tests deliberately load the ``.pyp`` plugin with a minimal fake ``c4d``
module. They exercise only the transport boundary; no Cinema 4D scene API is
available to the test process.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import queue
import socket
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = PROJECT_ROOT / "c4d_plugin" / "mcp_server_plugin.pyp"
TOKEN = "phase1-test-token-with-at-least-32-bytes"
OMIT_TOKEN = object()


def load_plugin_module():
    """Load the C4D plugin with only the host symbols needed at import time."""

    c4d = types.ModuleType("c4d")
    gui = types.ModuleType("c4d.gui")

    class GeDialog:
        def __init__(self):
            self.strings = {}
            self.enabled = {}
            self.timer_values = []
            self.base_core_messages = []

        def SetTimer(self, value):
            self.timer_values.append(value)

        def SetString(self, gadget_id, value):
            self.strings[gadget_id] = value

        def GetString(self, gadget_id):
            return self.strings.get(gadget_id, "")

        def Enable(self, gadget_id, value):
            self.enabled[gadget_id] = value

        def CoreMessage(self, message_id, message):
            self.base_core_messages.append((message_id, message))
            return False

    class CommandData:
        pass

    gui.GeDialog = GeDialog
    c4d.gui = gui
    c4d.plugins = types.SimpleNamespace(
        CommandData=CommandData,
        RegisterCommandPlugin=lambda *args, **kwargs: True,
        FilterPluginList=lambda *args, **kwargs: [],
    )
    c4d.PLUGINTYPE_ANY = 0
    c4d.threading = types.SimpleNamespace(GeIsMainThread=lambda: True)
    c4d.GetC4DVersion = lambda: 2023202
    c4d.SpecialEventAdd = lambda *args, **kwargs: None
    c4d.BFH_SCALEFIT = 0
    c4d.BFH_SCALE = 0
    c4d.DR_MULTILINE_READONLY = 0
    c4d.DLG_TYPE_ASYNC = 0
    c4d.CMD_ENABLED = 1

    module_name = "cinema4d_mcp_phase1_plugin_test"
    loader = importlib.machinery.SourceFileLoader(module_name, str(PLUGIN_PATH))
    spec = importlib.util.spec_from_loader(module_name, loader)
    module = importlib.util.module_from_spec(spec)

    with patch.dict(sys.modules, {"c4d": c4d, "c4d.gui": gui}):
        loader.exec_module(module)
    return module


class Phase1TransportContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = load_plugin_module()

    def make_server(self, **overrides):
        options = {
            "msg_queue": queue.Queue(),
            "token": TOKEN,
            "request_size_limit": 1024,
            "client_timeout": 0.25,
            "main_thread_timeout": 0.1,
        }
        options.update(overrides)
        server = self.plugin.C4DSocketServer(**options)
        server.running = True
        return server

    def exchange(self, server, fragments, execute_main_thread=True):
        client, plugin_side = socket.socketpair()
        worker = threading.Thread(
            target=server.handle_client,
            args=(plugin_side,),
            daemon=True,
        )
        worker.start()

        for fragment in fragments:
            client.sendall(fragment)

        if execute_main_thread:
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                try:
                    message_type, value = server.msg_queue.get(timeout=0.02)
                except queue.Empty:
                    continue
                if message_type == "EXEC":
                    value()
                    break

        client.settimeout(1.0)
        response_bytes = b""
        while b"\n" not in response_bytes:
            chunk = client.recv(4096)
            if not chunk:
                break
            response_bytes += chunk

        client.close()
        worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive(), "client worker did not terminate")
        return json.loads(response_bytes.split(b"\n", 1)[0].decode("utf-8"))

    @staticmethod
    def request(
        command="ping",
        token=TOKEN,
        request_id="req-1",
        protocol_version=1,
        params=None,
    ):
        request = {
            "protocol_version": protocol_version,
            "request_id": request_id,
            "command": command,
            "params": {} if params is None else params,
        }
        if token is not OMIT_TOKEN:
            request["token"] = token
        return request

    @staticmethod
    def free_port():
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]
        finally:
            probe.close()

    def start_network_server(self, port=None):
        server = self.make_server(port=port or self.free_port())
        server.running = False
        server.start()
        self.assertTrue(server.wait_until_ready(timeout=1.0), server.startup_error)
        return server

    def test_fragmented_frame_round_trips_through_main_thread(self):
        server = self.make_server()
        payload = json.dumps(self.request(), separators=(",", ":")).encode("utf-8")

        response = self.exchange(
            server,
            [payload[:7], payload[7:23], payload[23:] + b"\n"],
        )

        self.assertTrue(response["ok"])
        self.assertEqual(response["protocol_version"], 1)
        self.assertEqual(response["request_id"], "req-1")
        self.assertEqual(response["result"]["status"], "ok")
        self.assertIsNone(response["error"])

    def test_malformed_json_returns_structured_error(self):
        response = self.exchange(self.make_server(), [b'{"command":\n'])

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "MALFORMED_JSON")
        self.assertFalse(response["error"]["retryable"])

    def test_oversized_frame_is_rejected_before_dispatch(self):
        server = self.make_server(request_size_limit=128)
        response = self.exchange(server, [b"{" + (b"x" * 256) + b"}\n"])

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "FRAME_TOO_LARGE")

    def test_main_thread_timeout_is_structured_and_not_retryable(self):
        server = self.make_server(main_thread_timeout=0.05)
        server._dispatch_on_main_thread = MagicMock()
        payload = json.dumps(self.request()).encode("utf-8") + b"\n"

        response = self.exchange(server, [payload], execute_main_thread=False)

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "MAIN_THREAD_TIMEOUT")
        self.assertFalse(response["error"]["retryable"])

        message_type, delayed_callback = server.msg_queue.get_nowait()
        self.assertEqual(message_type, "EXEC")
        self.assertFalse(delayed_callback())
        server._dispatch_on_main_thread.assert_not_called()

    def test_forbidden_commands_are_not_dispatchable(self):
        for command in (
            "undo_last",
            "save_as",
            "save_project",
            "execute_python",
            "octane_command",
            "redshift_command",
        ):
            with self.subTest(command=command):
                payload = json.dumps(self.request(command=command)).encode("utf-8") + b"\n"
                response = self.exchange(self.make_server(), [payload])

                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], "UNKNOWN_COMMAND")

    def test_wrong_token_is_rejected(self):
        payload = json.dumps(self.request(token="wrong-token")).encode("utf-8") + b"\n"
        response = self.exchange(self.make_server(), [payload])

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "AUTH_FAILED")

    def test_missing_token_is_auth_required(self):
        payload = json.dumps(self.request(token=OMIT_TOKEN)).encode("utf-8") + b"\n"
        response = self.exchange(self.make_server(), [payload])

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "AUTH_REQUIRED")

    def test_empty_token_is_auth_required(self):
        payload = json.dumps(self.request(token="")).encode("utf-8") + b"\n"
        response = self.exchange(self.make_server(), [payload])

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "AUTH_REQUIRED")

    def test_protocol_mismatch_has_distinct_error(self):
        payload = json.dumps(self.request(protocol_version=99)).encode("utf-8") + b"\n"
        response = self.exchange(self.make_server(), [payload])

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "PROTOCOL_MISMATCH")

    def test_other_schema_error_is_invalid_request(self):
        request = self.request()
        request["unexpected"] = True
        payload = json.dumps(request).encode("utf-8") + b"\n"
        response = self.exchange(self.make_server(), [payload])

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "INVALID_REQUEST")

    def test_non_ascii_request_token_is_invalid_request_not_internal_error(self):
        payload = json.dumps(
            self.request(token="가" * 32), ensure_ascii=False
        ).encode("utf-8") + b"\n"
        response = self.exchange(self.make_server(), [payload])

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "INVALID_REQUEST")

    def test_configured_token_requires_safe_ascii_and_minimum_length(self):
        with self.assertRaisesRegex(ValueError, "URL-safe ASCII"):
            self.make_server(token="가" * 32)
        with self.assertRaisesRegex(ValueError, "between 32 and 256"):
            self.make_server(token="short-token")
        with self.assertRaisesRegex(ValueError, "estimated entropy"):
            self.make_server(token="A" * 64)

    def test_format_c4d_version_uses_revision_as_minor_and_patch(self):
        self.assertEqual(self.plugin._format_c4d_version(2023202), "2023.2.2")
        self.assertEqual(self.plugin._format_c4d_version(2023212), "2023.2.12")

    def test_capabilities_report_verified_runtime_and_safe_defaults(self):
        payload = json.dumps(self.request(command="get_capabilities")).encode("utf-8") + b"\n"
        response = self.exchange(self.make_server(), [payload])

        self.assertTrue(response["ok"])
        result = response["result"]
        self.assertEqual(result["cinema4d"]["version"], "2023.2.2")
        self.assertEqual(result["cinema4d"]["version_raw"], 2023202)
        self.assertEqual(result["cinema4d"]["compatibility"], "target")
        self.assertEqual(result["bridge_version"], "0.4.0-phase2c")
        self.assertEqual(
            result["tools"],
            [
                "ping",
                "get_capabilities",
                "get_scene_info",
                "list_objects",
                "get_object",
                "create_object",
                "update_object",
                "delete_object",
                "save_document",
            ],
        )
        self.assertTrue(result["features"]["scene_read"])
        self.assertTrue(result["features"]["object_operations"])
        self.assertFalse(result["features"]["undo"])
        self.assertTrue(result["features"]["save"])
        self.assertTrue(
            all(
                value is False
                for name, value in result["features"].items()
                if name not in ("scene_read", "object_operations", "save")
            )
        )
        self.assertFalse(result["renderers"]["octane"]["installed"])
        self.assertIsNone(result["renderers"]["octane"]["version"])

    def test_octane_install_does_not_guess_a_version(self):
        class OctanePlugin:
            def GetName(self):
                return "OctaneRender"

            def GetFilename(self):
                return r"C:\\plugins\\c4dOctane-R2023.xdl64"

        payload = json.dumps(self.request(command="get_capabilities")).encode("utf-8") + b"\n"
        with patch.object(
            self.plugin.c4d.plugins,
            "FilterPluginList",
            return_value=[OctanePlugin()],
        ):
            response = self.exchange(self.make_server(), [payload])

        octane = response["result"]["renderers"]["octane"]
        self.assertTrue(octane["installed"])
        self.assertIsNone(octane["version"])
        self.assertEqual(octane["detection"], "installed_version_unverified")

    def test_octane_registry_failure_is_unverified_not_false(self):
        payload = json.dumps(self.request(command="get_capabilities")).encode("utf-8") + b"\n"
        with patch.object(
            self.plugin.c4d.plugins,
            "FilterPluginList",
            side_effect=RuntimeError("registry unavailable"),
        ):
            response = self.exchange(self.make_server(), [payload])

        octane = response["result"]["renderers"]["octane"]
        self.assertIsNone(octane["installed"])
        self.assertIsNone(octane["version"])
        self.assertEqual(octane["detection"], "unverified")

    def test_connected_client_cannot_execute_after_stop(self):
        server = self.start_network_server()
        server._dispatch_on_main_thread = MagicMock()
        client = socket.create_connection(("127.0.0.1", server.port), timeout=1.0)
        try:
            deadline = time.monotonic() + 1.0
            while server.active_client is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(server.active_client)

            self.assertTrue(server.stop(wait=True, timeout=1.0))
            payload = json.dumps(self.request()).encode("utf-8") + b"\n"
            try:
                client.sendall(payload)
                client.settimeout(0.2)
                self.assertEqual(client.recv(4096), b"")
            except OSError:
                pass
            server._dispatch_on_main_thread.assert_not_called()
        finally:
            client.close()
            server.stop(wait=True, timeout=1.0)

    def test_stop_terminates_server_thread(self):
        server = self.start_network_server()
        self.assertTrue(server.is_alive())

        self.assertTrue(server.stop(wait=True, timeout=1.0))

        self.assertFalse(server.is_alive())
        self.assertTrue(server.wait_until_stopped(timeout=0.1))

    def test_immediate_restart_on_same_port_succeeds(self):
        port = self.free_port()
        first = self.start_network_server(port=port)
        self.assertTrue(first.stop(wait=True, timeout=1.0))

        second = self.start_network_server(port=port)
        try:
            self.assertTrue(second.running)
            self.assertIsNone(second.startup_error)
        finally:
            second.stop(wait=True, timeout=1.0)

    def test_port_collision_is_a_controlled_startup_failure(self):
        port = self.free_port()
        first = self.start_network_server(port=port)
        second = self.make_server(port=port)
        second.running = False
        try:
            second.start()
            self.assertTrue(second._ready_event.wait(1.0))
            second.join(1.0)

            self.assertFalse(second.is_alive())
            self.assertIsNotNone(second.startup_error)
            self.assertEqual(second.startup_error["error"]["code"], "PORT_IN_USE")
        finally:
            second.stop(wait=True, timeout=1.0)
            first.stop(wait=True, timeout=1.0)

    def test_stop_cancels_queued_task_before_late_queue_consumption(self):
        server = self.start_network_server()
        server._dispatch_on_main_thread = MagicMock()
        client = socket.create_connection(("127.0.0.1", server.port), timeout=1.0)
        payload = json.dumps(self.request()).encode("utf-8") + b"\n"
        client.sendall(payload)

        delayed_callback = None
        deadline = time.monotonic() + 1.0
        while delayed_callback is None and time.monotonic() < deadline:
            try:
                message_type, value = server.msg_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if message_type == "EXEC":
                delayed_callback = value

        self.assertIsNotNone(delayed_callback)
        self.assertTrue(server.stop(wait=True, timeout=1.0))

        self.assertFalse(delayed_callback())
        server._dispatch_on_main_thread.assert_not_called()
        client.close()

    def test_exec_enqueue_requests_special_main_thread_event(self):
        server = self.make_server(main_thread_timeout=1.0)
        response = {}
        with patch.object(self.plugin.c4d, "SpecialEventAdd") as wake_main:
            worker = threading.Thread(
                target=lambda: response.setdefault(
                    "value",
                    server.execute_on_main_thread("ping", {}, "req-wake"),
                ),
                daemon=True,
            )
            worker.start()

            message_type, callback = server.msg_queue.get(timeout=0.5)
            self.assertEqual(message_type, "EXEC")
            wake_main.assert_called_once_with(self.plugin.MAIN_THREAD_EVENT_ID)

            server.stop(wait=True, timeout=1.0)
            worker.join(timeout=1.0)
            self.assertFalse(callback())

        self.assertFalse(worker.is_alive())
        self.assertEqual(response["value"]["error"]["code"], "SERVER_STOPPING")

    def test_custom_core_message_dispatches_ping_without_timer(self):
        dialog = self.plugin.SocketServerDialog()
        dialog.Timer = MagicMock()
        server = self.make_server(
            msg_queue=dialog.msg_queue,
            main_thread_timeout=1.0,
        )
        response = {}

        with patch.object(self.plugin.c4d, "SpecialEventAdd") as wake_main:
            worker = threading.Thread(
                target=lambda: response.setdefault(
                    "value",
                    server.execute_on_main_thread("ping", {}, "req-core"),
                ),
                daemon=True,
            )
            worker.start()

            deadline = time.monotonic() + 0.5
            while not wake_main.called and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(wake_main.called)
            self.assertTrue(
                dialog.CoreMessage(self.plugin.MAIN_THREAD_EVENT_ID, None)
            )

        worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive())
        self.assertTrue(response["value"]["ok"])
        self.assertEqual(response["value"]["result"]["status"], "ok")
        dialog.Timer.assert_not_called()

    def test_status_and_log_are_consumed_by_custom_core_message(self):
        dialog = self.plugin.SocketServerDialog()
        server = self.make_server(msg_queue=dialog.msg_queue)

        with patch.object(self.plugin.c4d, "SpecialEventAdd") as wake_main:
            server.update_status("Online")
            server.log("Bridge ready")

            self.assertNotIn(dialog.STATUS_TEXT_ID, dialog.strings)
            self.assertNotIn(dialog.LOG_BOX_ID, dialog.strings)
            self.assertEqual(wake_main.call_count, 2)
            for call in wake_main.call_args_list:
                self.assertEqual(
                    call.args,
                    (self.plugin.MAIN_THREAD_EVENT_ID,),
                )

            dialog.CoreMessage(self.plugin.MAIN_THREAD_EVENT_ID, None)

        self.assertEqual(dialog.strings[dialog.STATUS_TEXT_ID], "Server: Online")
        self.assertEqual(dialog.strings[dialog.LOG_BOX_ID], "Bridge ready")

    def test_unrelated_core_message_delegates_to_base_implementation(self):
        dialog = self.plugin.SocketServerDialog()
        message = object()

        with patch.object(
            self.plugin.gui.GeDialog,
            "CoreMessage",
            return_value=False,
        ) as base_core_message:
            result = dialog.CoreMessage(987654, message)

        self.assertFalse(result)
        base_core_message.assert_called_once_with(dialog, 987654, message)

    def test_timer_is_initialized_after_dialog_values(self):
        dialog = self.plugin.SocketServerDialog()

        self.assertEqual(dialog.timer_values, [])
        self.assertTrue(dialog.InitValues())
        self.assertEqual(dialog.timer_values, [500])

    def test_stopped_task_is_not_dispatched_by_late_core_message(self):
        dialog = self.plugin.SocketServerDialog()
        server = self.make_server(
            msg_queue=dialog.msg_queue,
            main_thread_timeout=1.0,
        )
        server._dispatch_on_main_thread = MagicMock()
        response = {}

        with patch.object(self.plugin.c4d, "SpecialEventAdd") as wake_main:
            worker = threading.Thread(
                target=lambda: response.setdefault(
                    "value",
                    server.execute_on_main_thread("ping", {}, "req-late-core"),
                ),
                daemon=True,
            )
            worker.start()

            deadline = time.monotonic() + 0.5
            while dialog.msg_queue.empty() and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertFalse(dialog.msg_queue.empty())

            server.stop(wait=True, timeout=1.0)
            worker.join(timeout=1.0)
            dialog.CoreMessage(self.plugin.MAIN_THREAD_EVENT_ID, None)

        self.assertFalse(worker.is_alive())
        self.assertGreaterEqual(wake_main.call_count, 2)
        self.assertEqual(response["value"]["error"]["code"], "SERVER_STOPPING")
        server._dispatch_on_main_thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
