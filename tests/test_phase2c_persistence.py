"""Phase 2C existing-path document persistence contracts."""

from __future__ import annotations

import asyncio
import json
import math
import os
import queue
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cinema4d_mcp import server as external_server
from cinema4d_mcp.config import (
    RESPONSE_TIMEOUT_SECONDS,
    SAVE_RESPONSE_TIMEOUT_SECONDS,
)

from tests.test_phase1_transport import TOKEN, load_plugin_module
from tests.test_phase2a_read import (
    FakeObject,
    SCOPE_A,
    SCOPE_B,
    object_scope,
)
from tests.test_phase2b_mutation import MutationFakeDocument


class Phase2CPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = load_plugin_module()

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.document_directory = Path(self.temporary_directory.name)
        self.document_name = "scene.c4d"
        self.document_target = self.document_directory / self.document_name
        self.document_target.write_bytes(b"existing-c4d-test-file")
        self.document = MutationFakeDocument(
            name=self.document_name,
            path=str(self.document_directory),
        )

        document_tokens = iter((SCOPE_A, SCOPE_B, "c" * 32, "d" * 32))
        self.plugin._DOCUMENT_SCOPES = self.plugin._DocumentScopeRegistry(
            token_factory=lambda: next(document_tokens)
        )
        object_tokens = iter(object_scope(index) for index in range(1, 4096))
        self.plugin._OBJECT_SCOPES = self.plugin._ObjectScopeRegistry(
            token_factory=lambda: next(object_tokens)
        )

        self.save_document = MagicMock(return_value=True)
        self.save_project = MagicMock()
        self.plugin.c4d.documents = SimpleNamespace(
            GetActiveDocument=lambda: self.document,
            SaveDocument=self.save_document,
            SaveProject=self.save_project,
        )
        self.plugin.c4d.SAVEDOCUMENTFLAGS_DONTADDTORECENTLIST = object()
        self.plugin.c4d.SAVEDOCUMENTFLAGS_DIALOGSALLOWED = object()
        self.plugin.c4d.SAVEDOCUMENTFLAGS_SAVEAS = object()
        self.plugin.c4d.SAVEDOCUMENTFLAGS_EXPORTDIALOG = object()
        self.plugin.c4d.SAVEDOCUMENTFLAGS_AUTOSAVE = object()
        self.plugin.c4d.FORMAT_C4DEXPORT = object()
        self.plugin.c4d.threading.GeIsMainThread = lambda: True
        self.plugin.c4d.utils = SimpleNamespace(
            RadToDeg=math.degrees,
            DegToRad=math.radians,
        )
        self.plugin.c4d.GETACTIVEOBJECTFLAGS_CHILDREN = object()
        self.plugin.c4d.StopAllThreads = MagicMock()
        self.plugin.c4d.EventAdd = MagicMock()
        self.plugin.c4d.CallCommand = MagicMock()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def make_server(self, **overrides):
        options = {
            "msg_queue": queue.Queue(),
            "token": TOKEN,
            "request_size_limit": 1024,
            "client_timeout": 0.25,
            "main_thread_timeout": 0.5,
            "save_completion_timeout": 0.5,
        }
        options.update(overrides)
        server = self.plugin.C4DSocketServer(**options)
        server.running = True
        return server

    def dispatch(self, command, params=None, request_id="phase2c-dispatch"):
        return self.make_server()._dispatch_on_main_thread(
            command,
            {} if params is None else params,
            request_id,
        )

    def execute_task(self, command, params=None):
        server = self.make_server()
        task = self.plugin._MainThreadTask(
            server,
            command,
            {} if params is None else params,
            "phase2c-request",
        )
        self.assertTrue(task.execute())
        return task.result

    def assert_error(self, response, code):
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], code)
        self.assertFalse(response["error"]["retryable"])

    def test_plugin_surface_capabilities_and_write_classification_are_exact(self):
        expected = (
            "ping",
            "get_capabilities",
            "get_scene_info",
            "list_objects",
            "get_object",
            "create_object",
            "update_object",
            "delete_object",
            "save_document",
        )
        self.assertEqual(self.plugin.ACTIVE_COMMAND_NAMES, expected)
        self.assertEqual(self.plugin.ALLOWED_COMMANDS, frozenset(expected))
        self.assertEqual(
            self.plugin.WRITE_COMMAND_NAMES,
            frozenset(
                ("create_object", "update_object", "delete_object", "save_document")
            ),
        )

        self.plugin.c4d.GetC4DVersion = lambda: 2023202
        self.plugin.c4d.plugins = SimpleNamespace(FindPlugin=lambda *args: None)
        capabilities = self.dispatch("get_capabilities")
        self.assertEqual(capabilities["tools"], list(expected))
        self.assertTrue(capabilities["features"]["save"])
        self.assertFalse(capabilities["features"]["undo"])
        self.assertEqual(capabilities["bridge_version"], "0.4.0-phase2c")

        for command in (
            "undo_last",
            "save_as",
            "save_project",
            "save_project_with_assets",
            "load_document",
            "execute_python",
            "octane_command",
            "redshift_command",
        ):
            with self.subTest(command=command):
                self.assertNotIn(command, self.plugin.ALLOWED_COMMANDS)
                request = {
                    "protocol_version": 1,
                    "request_id": "forbidden",
                    "command": command,
                    "token": TOKEN,
                    "params": {},
                }
                self.assertIsNone(self.make_server()._validate_request(request))

    def test_raw_save_params_are_exactly_an_empty_object(self):
        server = self.make_server()
        valid = {
            "protocol_version": 1,
            "request_id": "save-valid",
            "command": "save_document",
            "token": TOKEN,
            "params": {},
        }
        self.assertIsNone(server._validate_request(valid))

        for params, expected_code in (
            (None, "INVALID_REQUEST"),
            ({"path": r"C:\temp\scene.c4d"}, "INVALID_PARAMS"),
            ({"filename": "scene.c4d"}, "INVALID_PARAMS"),
            ({"save_as": True}, "INVALID_PARAMS"),
            ({"overwrite": True}, "INVALID_PARAMS"),
            ({"format": "c4d"}, "INVALID_PARAMS"),
        ):
            with self.subTest(params=params):
                request = dict(valid)
                request["params"] = params
                validation = server._validate_request(request)
                self.assertEqual(validation[0], expected_code)

    def test_untitled_document_requires_manual_initial_save(self):
        for path, name in (("", "Untitled 1"), (str(self.document_directory), "")):
            with self.subTest(path=path, name=name):
                self.document.path = path
                self.document.name = name
                response = self.execute_task("save_document")
                self.assert_error(response, "SAVE_PATH_REQUIRED")
        self.save_document.assert_not_called()

    def test_invalid_document_metadata_is_rejected_before_save(self):
        invalid_cases = (
            ("relative", "scene.c4d"),
            (str(self.document_directory), "../scene.c4d"),
            (str(self.document_directory), r"subdir\scene.c4d"),
            (str(self.document_directory), "subdir/scene.c4d"),
            (str(self.document_directory) + "\x00", "scene.c4d"),
            (str(self.document_directory), "scene.c4d\x00"),
            (123, "scene.c4d"),
            (str(self.document_directory), 123),
        )
        for path, name in invalid_cases:
            with self.subTest(path=path, name=name):
                self.document.path = path
                self.document.name = name
                response = self.execute_task("save_document")
                self.assert_error(response, "SAVE_TARGET_INVALID")
        self.save_document.assert_not_called()

    def test_only_native_c4d_extensions_are_supported_case_insensitively(self):
        for name in ("scene.fbx", "scene.obj", "scene.abc", "scene"):
            with self.subTest(name=name):
                self.document.name = name
                response = self.execute_task("save_document")
                self.assert_error(response, "SAVE_FORMAT_UNSUPPORTED")

        uppercase_target = self.document_directory / "SCENE.C4D"
        uppercase_target.write_bytes(b"existing-uppercase-c4d")
        self.document.name = uppercase_target.name
        response = self.execute_task("save_document")
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["document"]["format"], "c4d")

    def test_missing_or_non_file_target_is_rejected(self):
        self.document.name = "missing.c4d"
        response = self.execute_task("save_document")
        self.assert_error(response, "SAVE_TARGET_MISSING")

        directory_target = self.document_directory / "directory.c4d"
        directory_target.mkdir()
        self.document.name = directory_target.name
        response = self.execute_task("save_document")
        self.assert_error(response, "SAVE_TARGET_INVALID")
        self.save_document.assert_not_called()

    def test_save_document_uses_only_exact_native_no_dialog_call(self):
        self.document.SetDocumentPath = MagicMock()
        self.document.SetDocumentName = MagicMock()

        response = self.execute_task("save_document")

        self.assertTrue(response["ok"])
        self.assertEqual(
            response["result"],
            {
                "saved": True,
                "document": {
                    "name": self.document_name,
                    "path": os.path.abspath(str(self.document_directory)),
                    "format": "c4d",
                },
            },
        )
        self.save_document.assert_called_once_with(
            self.document,
            os.path.abspath(str(self.document_target)),
            self.plugin.c4d.SAVEDOCUMENTFLAGS_DONTADDTORECENTLIST,
            self.plugin.c4d.FORMAT_C4DEXPORT,
        )
        used_flags = self.save_document.call_args.args[2]
        for forbidden_flag in (
            self.plugin.c4d.SAVEDOCUMENTFLAGS_DIALOGSALLOWED,
            self.plugin.c4d.SAVEDOCUMENTFLAGS_SAVEAS,
            self.plugin.c4d.SAVEDOCUMENTFLAGS_EXPORTDIALOG,
            self.plugin.c4d.SAVEDOCUMENTFLAGS_AUTOSAVE,
        ):
            self.assertIsNot(used_flags, forbidden_flag)
        self.document.SetDocumentPath.assert_not_called()
        self.document.SetDocumentName.assert_not_called()
        self.save_project.assert_not_called()
        self.plugin.c4d.CallCommand.assert_not_called()
        self.plugin.c4d.StopAllThreads.assert_not_called()
        self.plugin.c4d.EventAdd.assert_not_called()
        self.assertNotIn("StartUndo", self.document.log)
        self.assertNotIn("EndUndo", self.document.log)

    def test_save_result_requires_actual_bool_true(self):
        for result in (False, 1, "true", None):
            with self.subTest(result=result):
                self.save_document.reset_mock()
                self.save_document.return_value = result
                response = self.execute_task("save_document")
                self.assert_error(response, "SAVE_FAILED")

    def test_save_exception_and_failed_postflight_are_outcome_unknown(self):
        self.save_document.side_effect = RuntimeError("save failed")
        response = self.execute_task("save_document")
        self.assert_error(response, "OUTCOME_UNKNOWN")
        self.assertEqual(
            response["error"]["message"],
            "Cinema 4D may have modified the document file before save failed",
        )
        self.assertEqual(response["error"]["details"], {})

        self.save_document.reset_mock()

        def save_then_remove(*args):
            self.document_target.unlink()
            return True

        self.save_document.side_effect = save_then_remove
        response = self.execute_task("save_document")
        self.assert_error(response, "OUTCOME_UNKNOWN")

    def test_save_requires_the_main_thread_before_document_or_file_access(self):
        get_active_document = MagicMock(return_value=self.document)
        self.plugin.c4d.documents.GetActiveDocument = get_active_document
        self.plugin.c4d.threading.GeIsMainThread = lambda: False

        with self.assertRaisesRegex(RuntimeError, "main thread"):
            self.make_server()._dispatch_on_main_thread(
                "save_document", {}, "off-main-thread"
            )

        get_active_document.assert_not_called()
        self.save_document.assert_not_called()

    def test_save_preserves_document_and_object_scopes(self):
        cube = FakeObject(0, "Persistent Cube", 5159, "Cube")
        self.document.roots = [cube]
        self.document._attach_all()

        before = self.dispatch(
            "list_objects", {"offset": 0, "limit": 100}, "before-save"
        )
        object_id = before["objects"][0]["object_id"]
        document_scope = object_id.split(":")[1]

        saved = self.dispatch("save_document")
        after = self.dispatch(
            "list_objects", {"offset": 0, "limit": 100}, "after-save"
        )
        inspected = self.dispatch("get_object", {"object_id": object_id})

        self.assertTrue(saved["saved"])
        self.assertEqual(after["objects"][0]["object_id"], object_id)
        self.assertEqual(object_id.split(":")[1], document_scope)
        self.assertEqual(inspected["object_id"], object_id)
        self.assertEqual(inspected["name"], "Persistent Cube")

    def test_queued_save_timeout_cancels_before_dispatch(self):
        server = self.make_server(
            main_thread_timeout=0.02,
            save_completion_timeout=0.2,
        )
        server._dispatch_on_main_thread = MagicMock()

        response = server.execute_on_main_thread(
            "save_document", {}, "queued-save"
        )

        self.assertEqual(response["error"]["code"], "MAIN_THREAD_TIMEOUT")
        self.assertFalse(response["error"]["retryable"])
        _, late_callback = server.msg_queue.get_nowait()
        self.assertFalse(late_callback())
        server._dispatch_on_main_thread.assert_not_called()

    def test_started_save_has_a_separate_completion_timeout(self):
        server = self.make_server(
            main_thread_timeout=0.5,
            save_completion_timeout=0.02,
        )
        release = threading.Event()
        server._dispatch_on_main_thread = lambda *args: (
            release.wait(1.0) or {"saved": True}
        )
        response = {}
        worker = threading.Thread(
            target=lambda: response.setdefault(
                "value",
                server.execute_on_main_thread(
                    "save_document", {}, "running-save"
                ),
            )
        )
        worker.start()
        _, callback = server.msg_queue.get(timeout=0.5)
        callback_worker = threading.Thread(target=callback)
        callback_worker.start()
        worker.join(timeout=0.5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(response["value"]["error"]["code"], "OUTCOME_UNKNOWN")
        self.assertFalse(response["value"]["error"]["retryable"])

        release.set()
        callback_worker.join(timeout=0.5)


class Phase2CExternalTransportTests(unittest.TestCase):
    def _response(self, request_id="phase2c-external", result=None):
        return {
            "protocol_version": 1,
            "request_id": request_id,
            "ok": True,
            "result": result or {"saved": True},
            "error": None,
        }

    def _send_save_with_socket(self, bridge_socket, request_id="phase2c-external"):
        with patch(
            "cinema4d_mcp.server.socket.create_connection",
            return_value=bridge_socket,
        ) as connect:
            response = external_server.send_to_c4d(
                "save_document",
                token=TOKEN,
                request_id=request_id,
                params={},
            )
        connect.assert_called_once()
        return response

    def test_mcp_1281_surface_and_save_schema_are_exact(self):
        expected = (
            "ping",
            "get_capabilities",
            "get_scene_info",
            "list_objects",
            "get_object",
            "create_object",
            "update_object",
            "delete_object",
            "save_document",
        )
        self.assertEqual(external_server.ACTIVE_TOOL_NAMES, expected)
        self.assertEqual(
            set(external_server.mcp._tool_manager._tools),
            set(expected),
        )
        schema = external_server.mcp._tool_manager.get_tool(
            "save_document"
        ).parameters
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema.get("properties", {}), {})
        self.assertFalse(schema.get("required"))
        self.assertEqual(
            external_server.WRITE_TOOL_NAMES,
            frozenset(
                ("create_object", "update_object", "delete_object", "save_document")
            ),
        )

    def test_external_save_rejects_all_arguments_before_connect(self):
        invalid_arguments = (
            {"path": r"C:\temp\scene.c4d"},
            {"filename": "scene.c4d"},
            {"save_as": True},
            {"overwrite": True},
            {"format": "c4d"},
        )
        with patch("cinema4d_mcp.server.socket.create_connection") as connect:
            null_response = asyncio.run(
                external_server.mcp.call_tool("save_document", None)
            )
            self.assertEqual(
                null_response["error"]["code"],
                "INVALID_PARAMS",
            )
            for arguments in invalid_arguments:
                with self.subTest(arguments=arguments):
                    response = external_server.send_to_c4d(
                        "save_document",
                        token=TOKEN,
                        request_id="invalid-save",
                        params=arguments,
                    )
                    self.assertEqual(
                        response["error"]["code"],
                        "INVALID_PARAMS",
                    )
                    tool_response = asyncio.run(
                        external_server.mcp.call_tool(
                            "save_document",
                            arguments,
                        )
                    )
                    self.assertEqual(
                        tool_response["error"]["code"],
                        "INVALID_PARAMS",
                    )
        connect.assert_not_called()

    def test_external_save_uses_one_request_and_long_response_timeout(self):
        bridge_socket = MagicMock()
        bridge_socket.recv.return_value = (
            json.dumps(self._response()).encode("utf-8") + b"\n"
        )

        response = self._send_save_with_socket(bridge_socket)

        self.assertTrue(response["ok"])
        bridge_socket.settimeout.assert_called_once_with(
            SAVE_RESPONSE_TIMEOUT_SECONDS
        )
        self.assertEqual(SAVE_RESPONSE_TIMEOUT_SECONDS, 120.0)
        bridge_socket.sendall.assert_called_once()
        sent = json.loads(
            bridge_socket.sendall.call_args.args[0].decode("utf-8")
        )
        self.assertEqual(sent["command"], "save_document")
        self.assertEqual(sent["params"], {})

    def test_post_delivery_save_failures_are_outcome_unknown_without_retry(self):
        failure_cases = (
            socket.timeout(),
            ConnectionResetError(),
            b"",
            b"{malformed-json}\n",
            json.dumps({"not": "an envelope"}).encode("utf-8") + b"\n",
        )
        for failure in failure_cases:
            with self.subTest(failure=repr(failure)):
                bridge_socket = MagicMock()
                if isinstance(failure, bytes):
                    bridge_socket.recv.return_value = failure
                else:
                    bridge_socket.recv.side_effect = failure
                response = self._send_save_with_socket(bridge_socket)
                self.assertEqual(
                    response["error"]["code"],
                    "OUTCOME_UNKNOWN",
                )
                self.assertFalse(response["error"]["retryable"])
                bridge_socket.sendall.assert_called_once()

    def test_save_send_failure_is_outcome_unknown_and_not_retried(self):
        bridge_socket = MagicMock()
        bridge_socket.sendall.side_effect = socket.timeout()

        response = self._send_save_with_socket(bridge_socket)

        self.assertEqual(response["error"]["code"], "OUTCOME_UNKNOWN")
        self.assertFalse(response["error"]["retryable"])
        bridge_socket.sendall.assert_called_once()
        bridge_socket.recv.assert_not_called()

    def test_save_preconnect_failure_is_retryable_unavailable(self):
        with patch(
            "cinema4d_mcp.server.socket.create_connection",
            side_effect=ConnectionRefusedError(),
        ) as connect:
            response = external_server.send_to_c4d(
                "save_document",
                token=TOKEN,
                request_id="save-preconnect",
                params={},
            )

        connect.assert_called_once()
        self.assertEqual(response["error"]["code"], "C4D_UNAVAILABLE")
        self.assertTrue(response["error"]["retryable"])

    def test_read_timeout_remains_short_and_retryable(self):
        bridge_socket = MagicMock()
        bridge_socket.recv.side_effect = socket.timeout()
        with patch(
            "cinema4d_mcp.server.socket.create_connection",
            return_value=bridge_socket,
        ):
            response = external_server.send_to_c4d(
                "ping",
                token=TOKEN,
                request_id="read-timeout",
            )

        bridge_socket.settimeout.assert_called_once_with(
            RESPONSE_TIMEOUT_SECONDS
        )
        self.assertEqual(response["error"]["code"], "C4D_TIMEOUT")
        self.assertTrue(response["error"]["retryable"])


if __name__ == "__main__":
    unittest.main()
