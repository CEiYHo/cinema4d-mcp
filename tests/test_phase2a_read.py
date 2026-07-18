"""Contract tests for Phase 2A read-only scene and object inspection."""

from __future__ import annotations

import math
import queue
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.test_phase1_transport import TOKEN, load_plugin_module


class FakeVector:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x = x
        self.y = y
        self.z = z


class FakeObject:
    """Only APIs verified in the Cinema 4D 2023.2 SDK are faked here."""

    def __init__(
        self,
        guid,
        name,
        type_id,
        type_name,
        *,
        position=None,
        rotation=None,
        scale=None,
        children=None,
    ):
        self.guid = guid
        self.name = name
        self.type_id = type_id
        self.type_name = type_name
        self.position = position or FakeVector()
        self.rotation = rotation or FakeVector()
        self.scale = scale or FakeVector(1.0, 1.0, 1.0)
        self.children = list(children or [])
        self._down = None
        self._next = None
        self.cache_reads = 0

    def GetGUID(self):
        if isinstance(self.guid, Exception):
            raise self.guid
        return self.guid

    def GetName(self):
        return self.name

    def GetType(self):
        return self.type_id

    def GetTypeName(self):
        if isinstance(self.type_name, Exception):
            raise self.type_name
        return self.type_name

    def GetDown(self):
        return self._down

    def GetNext(self):
        return self._next

    def GetRelPos(self):
        return self.position

    def GetRelRot(self):
        return self.rotation

    def GetRelScale(self):
        return self.scale

    def GetCache(self):
        self.cache_reads += 1
        raise AssertionError("generated caches are outside Phase 2A")


def link_hierarchy(roots):
    roots = list(roots)
    for index, obj in enumerate(roots):
        obj._next = roots[index + 1] if index + 1 < len(roots) else None
        children = link_hierarchy(obj.children)
        obj._down = children[0] if children else None
    return roots


class FakeDocument:
    def __init__(self, roots=None, *, name="Untitled 1", path="", active=None):
        self.roots = link_hierarchy(roots or [])
        self.name = name
        self.path = path
        self.active = list(active or [])
        self.read_calls = 0

    def GetFirstObject(self):
        self.read_calls += 1
        return self.roots[0] if self.roots else None

    def GetDocumentName(self):
        self.read_calls += 1
        return self.name

    def GetDocumentPath(self):
        self.read_calls += 1
        return self.path

    def GetActiveObjects(self, flags):
        self.read_calls += 1
        return list(self.active)


class Phase2AReadContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = load_plugin_module()

    def setUp(self):
        self.document = FakeDocument()
        self.plugin.c4d.documents = SimpleNamespace(
            GetActiveDocument=lambda: self.document
        )
        self.plugin.c4d.utils = SimpleNamespace(RadToDeg=math.degrees)
        # The test value is a fake flag, not a guessed Cinema 4D numeric ID.
        self.plugin.c4d.GETACTIVEOBJECTFLAGS_CHILDREN = object()
        self.plugin.c4d.threading.GeIsMainThread = lambda: True

    def make_server(self, msg_queue=None, main_thread_timeout=0.5):
        server = self.plugin.C4DSocketServer(
            msg_queue=msg_queue or queue.Queue(),
            token=TOKEN,
            request_size_limit=1024,
            client_timeout=0.25,
            main_thread_timeout=main_thread_timeout,
        )
        server.running = True
        return server

    def dispatch(self, command, params=None):
        return self.make_server()._dispatch_on_main_thread(command, params or {})

    def execute_task(self, command, params=None):
        server = self.make_server()
        task = self.plugin._MainThreadTask(
            server,
            command,
            params or {},
            "phase2a-request",
        )
        self.assertTrue(task.execute())
        return task.result

    def test_plugin_allowlist_is_exactly_the_five_read_only_tools(self):
        expected = (
            "ping",
            "get_capabilities",
            "get_scene_info",
            "list_objects",
            "get_object",
        )
        self.assertEqual(self.plugin.ACTIVE_COMMAND_NAMES, expected)
        self.assertEqual(self.plugin.ALLOWED_COMMANDS, frozenset(expected))
        for forbidden in (
            "create_object",
            "update_object",
            "delete_object",
            "save_scene",
            "execute_python",
            "octane_command",
        ):
            self.assertNotIn(forbidden, self.plugin.ALLOWED_COMMANDS)

    def test_scene_info_empty_unsaved_document_is_read_only(self):
        before = (self.document.name, self.document.path, list(self.document.roots))

        result = self.dispatch("get_scene_info")

        self.assertEqual(
            result,
            {
                "document": {
                    "name": "Untitled 1",
                    "path": "",
                    "saved": False,
                },
                "object_count": 0,
                "active_object_ids": [],
            },
        )
        self.assertEqual(
            (self.document.name, self.document.path, list(self.document.roots)),
            before,
        )

    def test_scene_info_counts_hierarchy_and_sorts_active_ids_by_hierarchy(self):
        child = FakeObject(102, "Child", 700002, "Fake Child")
        root = FakeObject(101, "Root", 700001, "Fake Root", children=[child])
        other = FakeObject(103, "Other", 700003, "Fake Other")
        self.document = FakeDocument(
            [root, other],
            name="scene.c4d",
            path=r"C:\scenes",
            active=[other, root],
        )

        result = self.dispatch("get_scene_info")

        self.assertEqual(
            result["document"],
            {"name": "scene.c4d", "path": r"C:\scenes", "saved": True},
        )
        self.assertEqual(result["object_count"], 3)
        self.assertEqual(result["active_object_ids"], ["c4d:101", "c4d:103"])

    def test_no_active_document_is_structured(self):
        self.document = None

        response = self.execute_task("get_scene_info")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "NO_ACTIVE_DOCUMENT")

    def test_list_objects_empty_and_flat_hierarchies(self):
        self.assertEqual(self.dispatch("list_objects"), {"objects": []})

        first = FakeObject(101, "First", 705001, "Fake")
        second = FakeObject(102, "Second", 705002, "Fake")
        self.document = FakeDocument([first, second])
        objects = self.dispatch("list_objects")["objects"]

        self.assertEqual([item["depth"] for item in objects], [0, 0])
        self.assertEqual([item["parent_id"] for item in objects], [None, None])

    def test_list_objects_is_depth_first_preorder_with_parent_and_depth(self):
        grandchild = FakeObject(103, "Grandchild", 700003, "Fake Grandchild")
        child_a = FakeObject(
            102,
            "Duplicate",
            700002,
            "Fake Child",
            children=[grandchild],
        )
        child_b = FakeObject(104, "Duplicate", 700004, "Fake Child")
        root = FakeObject(
            101,
            "Root",
            700001,
            "Fake Root",
            children=[child_a, child_b],
        )
        root_b = FakeObject(105, "Root B", 700005, "Fake Root")
        self.document = FakeDocument([root, root_b])

        objects = self.dispatch("list_objects")["objects"]

        self.assertEqual(
            [item["object_id"] for item in objects],
            ["c4d:101", "c4d:102", "c4d:103", "c4d:104", "c4d:105"],
        )
        self.assertEqual([item["depth"] for item in objects], [0, 1, 2, 1, 0])
        self.assertEqual(
            [item["parent_id"] for item in objects],
            [None, "c4d:101", "c4d:102", "c4d:101", None],
        )
        duplicates = [item for item in objects if item["name"] == "Duplicate"]
        self.assertEqual(len(duplicates), 2)
        self.assertNotEqual(duplicates[0]["object_id"], duplicates[1]["object_id"])

    def test_unavailable_and_duplicate_guids_are_not_addressable(self):
        missing = FakeObject(None, "Missing", 710001, "Fake")
        broken = FakeObject(RuntimeError("no marker"), "Broken", 710002, "Fake")
        duplicate_a = FakeObject(500, "A", 710003, "Fake")
        duplicate_b = FakeObject(500, "B", 710004, "Fake")
        child = FakeObject(501, "Child", 710005, "Fake")
        missing.children = [child]
        self.document = FakeDocument([missing, broken, duplicate_a, duplicate_b])

        objects = self.dispatch("list_objects")["objects"]

        by_name = {item["name"]: item for item in objects}
        for item in (
            by_name["Missing"],
            by_name["Broken"],
            by_name["A"],
            by_name["B"],
        ):
            self.assertIsNone(item["object_id"])
            self.assertFalse(item["addressable"])
            self.assertEqual(item["id_error"], "OBJECT_ID_UNAVAILABLE")
        self.assertEqual(by_name["Child"]["object_id"], "c4d:501")
        self.assertIsNone(by_name["Child"]["parent_id"])
        self.assertEqual(
            by_name["Child"]["parent_id_error"],
            "OBJECT_ID_UNAVAILABLE",
        )

    def test_type_name_failure_is_null_not_a_guess(self):
        obj = FakeObject(101, "Object", 720001, RuntimeError("unavailable"))
        self.document = FakeDocument([obj])

        result = self.dispatch("list_objects")["objects"][0]

        self.assertEqual(result["type_id"], 720001)
        self.assertIsNone(result["type_name"])

    def test_generated_cache_is_not_traversed(self):
        root = FakeObject(101, "Root", 730001, "Fake Root")
        generated = FakeObject(999, "Generated Cache", 730099, "Fake Cache")
        root.generated_cache = generated
        self.document = FakeDocument([root])

        objects = self.dispatch("list_objects")["objects"]

        self.assertEqual([item["object_id"] for item in objects], ["c4d:101"])
        self.assertEqual(root.cache_reads, 0)

    def test_get_object_returns_runtime_type_relative_hpb_and_direct_children(self):
        grandchild = FakeObject(104, "Grandchild", 740004, "Fake")
        child_a = FakeObject(102, "Child A", 740002, "Fake", children=[grandchild])
        child_b = FakeObject(103, "Child B", 740003, "Fake")
        target = FakeObject(
            101,
            "Target",
            740001,
            "Runtime Type Name",
            position=FakeVector(1.25, 100.0, -3.5),
            rotation=FakeVector(math.pi / 2, -math.pi / 4, math.pi),
            scale=FakeVector(1.0, 2.0, 0.5),
            children=[child_a, child_b],
        )
        parent = FakeObject(100, "Parent", 740000, "Fake", children=[target])
        self.document = FakeDocument([parent])

        result = self.dispatch("get_object", {"object_id": "c4d:101"})

        self.assertEqual(result["object_id"], "c4d:101")
        self.assertEqual(result["type_id"], 740001)
        self.assertEqual(result["type_name"], "Runtime Type Name")
        self.assertEqual(result["parent_id"], "c4d:100")
        self.assertEqual(result["children"], ["c4d:102", "c4d:103"])
        self.assertEqual(result["transform"]["position"], [1.25, 100.0, -3.5])
        self.assertEqual(result["transform"]["rotation_deg"], [90.0, -45.0, 180.0])
        self.assertEqual(result["transform"]["scale"], [1.0, 2.0, 0.5])
        self.assertEqual(result["transform"]["space"], "relative")

    def test_duplicate_names_are_resolved_only_by_object_id(self):
        first = FakeObject(101, "Cube", 750001, "Fake Cube")
        second = FakeObject(102, "Cube", 750002, "Fake Cube")
        self.document = FakeDocument([first, second])

        first_result = self.dispatch("get_object", {"object_id": "c4d:101"})
        second_result = self.dispatch("get_object", {"object_id": "c4d:102"})

        self.assertEqual(first_result["type_id"], 750001)
        self.assertEqual(second_result["type_id"], 750002)
        malformed = self.execute_task("get_object", {"object_id": "Cube"})
        self.assertEqual(malformed["error"]["code"], "INVALID_PARAMS")

    def test_object_id_is_deterministic_across_repeat_list_get_and_rename(self):
        obj = FakeObject(123456789, "Before", 755001, "Fake")
        self.document = FakeDocument([obj])

        first_id = self.dispatch("list_objects")["objects"][0]["object_id"]
        first_get = self.dispatch("get_object", {"object_id": first_id})
        obj.name = "After"
        second_id = self.dispatch("list_objects")["objects"][0]["object_id"]
        second_get = self.dispatch("get_object", {"object_id": second_id})

        self.assertEqual(first_id, "c4d:123456789")
        self.assertEqual(second_id, first_id)
        self.assertEqual(first_get["name"], "Before")
        self.assertEqual(second_get["name"], "After")

    def test_unknown_and_non_unique_object_ids_have_distinct_errors(self):
        duplicate_a = FakeObject(500, "A", 760001, "Fake")
        duplicate_b = FakeObject(500, "B", 760002, "Fake")
        self.document = FakeDocument([duplicate_a, duplicate_b])

        missing = self.execute_task("get_object", {"object_id": "c4d:999"})
        duplicate = self.execute_task("get_object", {"object_id": "c4d:500"})

        self.assertEqual(missing["error"]["code"], "OBJECT_NOT_FOUND")
        self.assertEqual(duplicate["error"]["code"], "OBJECT_ID_UNAVAILABLE")

    def test_request_validation_accepts_only_command_specific_params(self):
        server = self.make_server()
        base = {
            "protocol_version": 1,
            "request_id": "request-1",
            "token": TOKEN,
        }
        for command in ("get_scene_info", "list_objects"):
            request = dict(base, command=command, params={})
            self.assertIsNone(server._validate_request(request))
        request = dict(
            base,
            command="get_object",
            params={"object_id": "c4d:123"},
        )
        self.assertIsNone(server._validate_request(request))

        for params in (
            {},
            {"object_id": "Cube"},
            {"object_id": "c4d:01"},
            {"object_id": "c4d:123", "name": "Cube"},
        ):
            request = dict(base, command="get_object", params=params)
            error = server._validate_request(request)
            self.assertEqual(error[0], "INVALID_PARAMS")

    def test_unexpected_c4d_api_failure_is_redacted(self):
        class BrokenDocument(FakeDocument):
            def GetFirstObject(self):
                raise RuntimeError("sensitive internal detail")

        self.document = BrokenDocument()

        response = self.execute_task("list_objects")

        self.assertEqual(response["error"]["code"], "C4D_API_ERROR")
        self.assertNotIn("sensitive", str(response))

    def test_new_commands_refuse_to_touch_c4d_off_main_thread(self):
        obj = FakeObject(101, "Object", 770001, "Fake")
        self.document = FakeDocument([obj])
        self.plugin.c4d.threading.GeIsMainThread = lambda: False

        for command, params in (
            ("get_scene_info", {}),
            ("list_objects", {}),
            ("get_object", {"object_id": "c4d:101"}),
        ):
            with self.subTest(command=command):
                with self.assertRaisesRegex(RuntimeError, "main thread"):
                    self.make_server()._dispatch_on_main_thread(command, params)
        self.assertEqual(self.document.read_calls, 0)

    def test_core_message_completes_read_dispatch_without_timer(self):
        obj = FakeObject(101, "Object", 780001, "Fake")
        self.document = FakeDocument([obj])
        dialog = self.plugin.SocketServerDialog()
        dialog.Timer = MagicMock()
        server = self.make_server(msg_queue=dialog.msg_queue)
        response = {}

        with patch.object(self.plugin.c4d, "SpecialEventAdd") as wake_main:
            worker = threading.Thread(
                target=lambda: response.setdefault(
                    "value",
                    server.execute_on_main_thread(
                        "get_scene_info",
                        {},
                        "phase2a-core-message",
                    ),
                ),
                daemon=True,
            )
            worker.start()
            deadline = time.monotonic() + 0.5
            while not wake_main.called and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(wake_main.called)
            dialog.CoreMessage(self.plugin.MAIN_THREAD_EVENT_ID, None)

        worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive())
        self.assertTrue(response["value"]["ok"])
        self.assertEqual(response["value"]["result"]["object_count"], 1)
        dialog.Timer.assert_not_called()

    def test_stop_cancels_queued_read_before_late_core_message(self):
        obj = FakeObject(101, "Object", 790001, "Fake")
        self.document = FakeDocument([obj])
        dialog = self.plugin.SocketServerDialog()
        server = self.make_server(
            msg_queue=dialog.msg_queue,
            main_thread_timeout=1.0,
        )
        response = {}

        with patch.object(self.plugin.c4d, "SpecialEventAdd"):
            worker = threading.Thread(
                target=lambda: response.setdefault(
                    "value",
                    server.execute_on_main_thread(
                        "list_objects",
                        {},
                        "phase2a-late-core-message",
                    ),
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
        self.assertEqual(response["value"]["error"]["code"], "SERVER_STOPPING")
        self.assertEqual(self.document.read_calls, 0)


if __name__ == "__main__":
    unittest.main()
