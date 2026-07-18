"""Hardening contracts for Phase 2A.1 pagination and scoped identities."""

from __future__ import annotations

import json
import math
import queue
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from tests.test_phase1_transport import TOKEN, load_plugin_module
from tests.test_phase2a_read import (
    FakeDocument,
    FakeObject,
    SCOPE_A,
    SCOPE_B,
    object_id,
)


class Phase2A1HardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = load_plugin_module()

    def setUp(self):
        self.document = FakeDocument()
        scope_tokens = iter((SCOPE_A, SCOPE_B, "c" * 32, "d" * 32))
        self.plugin._DOCUMENT_SCOPES = self.plugin._DocumentScopeRegistry(
            token_factory=lambda: next(scope_tokens)
        )
        self.plugin.c4d.documents = SimpleNamespace(
            GetActiveDocument=lambda: self.document
        )
        self.plugin.c4d.utils = SimpleNamespace(RadToDeg=math.degrees)
        self.plugin.c4d.GETACTIVEOBJECTFLAGS_CHILDREN = object()
        self.plugin.c4d.threading.GeIsMainThread = lambda: True

    def make_server(self):
        server = self.plugin.C4DSocketServer(
            msg_queue=queue.Queue(),
            token=TOKEN,
            request_size_limit=1024,
            client_timeout=0.25,
            main_thread_timeout=0.5,
        )
        server.running = True
        return server

    def dispatch(self, command, params=None, request_id="hardened-request"):
        return self.make_server()._dispatch_on_main_thread(
            command,
            params or {},
            request_id,
        )

    def execute_task(self, command, params=None, request_id="hardened-request"):
        server = self.make_server()
        task = self.plugin._MainThreadTask(
            server,
            command,
            params or {},
            request_id,
        )
        self.assertTrue(task.execute())
        return task.result

    @staticmethod
    def flat_objects(count, *, name_size=0, type_name_size=0):
        return [
            FakeObject(
                index + 1,
                "Object-{}{}".format(index, "N" * name_size),
                800000 + index,
                "Type-{}{}".format(index, "T" * type_name_size),
            )
            for index in range(count)
        ]

    def response_frame(self, request_id, result):
        return self.plugin._serialize_response_frame(
            self.plugin._success_envelope(request_id, result)
        )

    def test_pagination_defaults_empty_and_offset_beyond_total(self):
        empty = self.dispatch("list_objects")
        self.assertEqual(
            empty,
            {
                "objects": [],
                "total_count": 0,
                "offset": 0,
                "limit": 100,
                "returned_count": 0,
                "next_offset": None,
            },
        )

        self.document = FakeDocument(self.flat_objects(3))
        beyond = self.dispatch("list_objects", {"offset": 10})
        self.assertEqual(beyond["objects"], [])
        self.assertEqual(beyond["total_count"], 3)
        self.assertEqual(beyond["offset"], 10)
        self.assertEqual(beyond["limit"], 100)
        self.assertEqual(beyond["returned_count"], 0)
        self.assertIsNone(beyond["next_offset"])

    def test_multiple_pages_preserve_dfs_order_without_skip_or_duplicate(self):
        grandchild = FakeObject(3, "Grandchild", 810003, "Fake")
        child_a = FakeObject(2, "Child A", 810002, "Fake", children=[grandchild])
        child_b = FakeObject(4, "Child B", 810004, "Fake")
        root = FakeObject(1, "Root", 810001, "Fake", children=[child_a, child_b])
        tail = self.flat_objects(6)
        for index, obj in enumerate(tail):
            obj.guid = index + 5
        self.document = FakeDocument([root] + tail)

        offset = 0
        collected = []
        seen_offsets = []
        while True:
            page = self.dispatch(
                "list_objects",
                {"offset": offset, "limit": 3},
            )
            seen_offsets.append(offset)
            collected.extend(item["object_id"] for item in page["objects"])
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]

        self.assertEqual(seen_offsets, [0, 3, 6, 9])
        self.assertEqual(collected, [object_id(guid) for guid in range(1, 11)])
        self.assertEqual(len(collected), len(set(collected)))

    def test_last_page_uses_null_next_offset(self):
        self.document = FakeDocument(self.flat_objects(5))

        first = self.dispatch("list_objects", {"offset": 0, "limit": 3})
        last = self.dispatch("list_objects", {"offset": 3, "limit": 3})

        self.assertEqual(first["returned_count"], 3)
        self.assertEqual(first["next_offset"], 3)
        self.assertEqual(last["returned_count"], 2)
        self.assertIsNone(last["next_offset"])

    def test_raw_bridge_rejects_invalid_list_and_command_specific_params(self):
        server = self.make_server()
        base = {
            "protocol_version": 1,
            "request_id": "request-1",
            "token": TOKEN,
        }
        invalid_cases = (
            ("list_objects", {"offset": -1}),
            ("list_objects", {"offset": True}),
            ("list_objects", {"limit": 0}),
            ("list_objects", {"limit": False}),
            ("list_objects", {"limit": 201}),
            ("list_objects", {"offset": 0, "extra": 1}),
            ("get_scene_info", {"extra": 1}),
            ("ping", {"extra": 1}),
        )
        for command, params in invalid_cases:
            with self.subTest(command=command, params=params):
                request = dict(base, command=command, params=params)
                error = server._validate_request(request)
                self.assertEqual(error[0], "INVALID_PARAMS")

        malformed_envelope = dict(base, command="list_objects", params=[])
        self.assertEqual(
            server._validate_request(malformed_envelope)[0],
            "INVALID_REQUEST",
        )
        maximum = dict(base, command="list_objects", params={"limit": 200})
        self.assertIsNone(server._validate_request(maximum))

    def test_frame_budget_returns_short_page_and_advances_by_returned_count(self):
        self.document = FakeDocument(
            self.flat_objects(40, name_size=1200, type_name_size=1200)
        )
        request_id = "r" * 128

        page = self.dispatch(
            "list_objects",
            {"offset": 0, "limit": 40},
            request_id,
        )
        frame = self.response_frame(request_id, page)

        self.assertGreater(page["returned_count"], 0)
        self.assertLess(page["returned_count"], 40)
        self.assertEqual(page["next_offset"], page["returned_count"])
        self.assertLessEqual(len(frame), self.plugin.MAX_RESPONSE_FRAME_BYTES)
        for item in page["objects"]:
            self.assertTrue(item["name"].startswith("Object-"))
            self.assertTrue(item["name"].endswith("N" * 1200))

    def test_frame_limited_pages_cover_all_objects_without_looping(self):
        self.document = FakeDocument(
            self.flat_objects(35, name_size=1500, type_name_size=1500)
        )
        offset = 0
        collected = []
        page_count = 0
        while True:
            page_count += 1
            self.assertLess(page_count, 20, "pagination did not make progress")
            page = self.dispatch(
                "list_objects",
                {"offset": offset, "limit": 35},
                "r" * 128,
            )
            self.assertLessEqual(
                len(self.response_frame("r" * 128, page)),
                self.plugin.MAX_RESPONSE_FRAME_BYTES,
            )
            collected.extend(item["object_id"] for item in page["objects"])
            if page["next_offset"] is None:
                break
            self.assertEqual(
                page["next_offset"],
                offset + page["returned_count"],
            )
            offset = page["next_offset"]

        self.assertEqual(collected, [object_id(guid) for guid in range(1, 36)])

    def test_unicode_names_use_exact_serialized_frame_size(self):
        objects = self.flat_objects(20)
        for obj in objects:
            obj.name = "界" * 600
            obj.type_name = "型" * 600
        self.document = FakeDocument(objects)

        page = self.dispatch(
            "list_objects",
            {"offset": 0, "limit": 20},
            "unicode-frame",
        )

        self.assertLess(page["returned_count"], 20)
        self.assertLessEqual(
            len(self.response_frame("unicode-frame", page)),
            self.plugin.MAX_RESPONSE_FRAME_BYTES,
        )
        self.assertEqual(page["objects"][0]["name"], "界" * 600)

    def test_single_oversized_item_fails_without_truncation_or_empty_loop(self):
        huge_name = "X" * self.plugin.MAX_RESPONSE_FRAME_BYTES
        self.document = FakeDocument(
            [FakeObject(1, huge_name, 820001, "Huge Type")]
        )

        response = self.execute_task(
            "list_objects",
            {"offset": 0, "limit": 1},
            "oversized-item",
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "LIST_ITEM_TOO_LARGE")
        self.assertEqual(response["error"]["details"]["item_offset"], 0)
        self.assertEqual(self.document.roots[0].name, huge_name)

    def test_socket_response_guard_replaces_any_oversized_success(self):
        server = self.make_server()
        client = MagicMock()
        oversized = self.plugin._success_envelope(
            "request-1",
            {"value": "X" * self.plugin.MAX_RESPONSE_FRAME_BYTES},
        )

        server._send_response(client, oversized)

        payload = client.sendall.call_args.args[0]
        self.assertLessEqual(len(payload), self.plugin.MAX_RESPONSE_FRAME_BYTES)
        response = json.loads(payload.decode("utf-8"))
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "RESPONSE_TOO_LARGE")

    def test_response_guard_drops_oversized_invalid_request_id(self):
        server = self.make_server()
        client = MagicMock()
        oversized = self.plugin._error_envelope(
            "r" * self.plugin.MAX_RESPONSE_FRAME_BYTES,
            "INVALID_REQUEST",
            "Invalid request",
        )

        server._send_response(client, oversized)

        payload = client.sendall.call_args.args[0]
        self.assertLessEqual(len(payload), self.plugin.MAX_RESPONSE_FRAME_BYTES)
        response = json.loads(payload.decode("utf-8"))
        self.assertIsNone(response["request_id"])
        self.assertEqual(response["error"]["code"], "RESPONSE_TOO_LARGE")

    def test_cross_document_same_raw_guid_fails_closed_and_switch_back_recovers(self):
        document_a = FakeDocument(
            [FakeObject(77, "Document A", 830001, "Fake A")]
        )
        document_b = FakeDocument(
            [FakeObject(77, "Document B", 830002, "Fake B")]
        )
        self.document = document_a
        issued_id = self.dispatch("list_objects")["objects"][0]["object_id"]

        self.document = document_b
        mismatch = self.execute_task("get_object", {"object_id": issued_id})

        self.assertEqual(mismatch["error"]["code"], "DOCUMENT_MISMATCH")
        self.assertNotIn("Document B", str(mismatch))

        self.document = document_a
        recovered = self.execute_task("get_object", {"object_id": issued_id})
        self.assertTrue(recovered["ok"])
        self.assertEqual(recovered["result"]["name"], "Document A")

    def test_unknown_process_scope_is_stale_not_object_not_found(self):
        self.document = FakeDocument([FakeObject(77, "Current", 840001, "Fake")])

        response = self.execute_task(
            "get_object",
            {"object_id": object_id(77, "f" * 32)},
        )

        self.assertEqual(response["error"]["code"], "STALE_OBJECT_ID")

    def test_different_wrapper_is_not_guessed_to_be_same_document(self):
        first_wrapper = FakeDocument([FakeObject(77, "First", 850001, "Fake")])
        self.document = first_wrapper
        issued_id = self.dispatch("list_objects")["objects"][0]["object_id"]
        self.document = FakeDocument([FakeObject(77, "First", 850001, "Fake")])

        response = self.execute_task("get_object", {"object_id": issued_id})

        self.assertEqual(response["error"]["code"], "DOCUMENT_MISMATCH")

    def test_document_scoped_uint64_object_id_is_strictly_canonical(self):
        valid = (
            object_id(1),
            object_id(123),
            object_id(self.plugin.UINT64_MAX),
        )
        invalid = (
            "c4d:{}:0".format(SCOPE_A),
            "c4d:{}:-1".format(SCOPE_A),
            "c4d:{}:+1".format(SCOPE_A),
            "c4d:{}:01".format(SCOPE_A),
            "c4d:{}:{}".format(SCOPE_A, self.plugin.UINT64_MAX + 1),
            "c4d:{}:1".format(SCOPE_A.upper()),
            "c4d:1",
        )
        for value in valid:
            self.assertTrue(self.plugin._is_valid_object_id(value), value)
        for value in invalid:
            self.assertFalse(self.plugin._is_valid_object_id(value), value)

        for guid in (1, 123, self.plugin.UINT64_MAX):
            serialized = self.plugin._serialize_object_id(SCOPE_A, guid)
            self.assertEqual(
                self.plugin._parse_object_id(serialized),
                (SCOPE_A, guid),
            )

    def test_invalid_getguid_values_are_unaddressable(self):
        for guid in (
            False,
            "1",
            0,
            -1,
            self.plugin.UINT64_MAX + 1,
        ):
            with self.subTest(guid=guid):
                obj = FakeObject(guid, "Invalid", 860001, "Fake")
                self.assertIsNone(self.plugin._object_guid_or_none(obj))

    def test_children_completeness_with_no_children(self):
        parent = FakeObject(1, "Parent", 870001, "Fake")
        self.document = FakeDocument([parent])
        self.dispatch("list_objects")

        result = self.dispatch("get_object", {"object_id": object_id(1)})

        self.assertEqual(result["children"], [])
        self.assertEqual(result["child_count"], 0)
        self.assertEqual(result["addressable_child_count"], 0)
        self.assertEqual(result["unaddressable_child_count"], 0)
        self.assertTrue(result["children_complete"])

    def test_children_completeness_counts_direct_addressable_children_only(self):
        grandchild = FakeObject(4, "Grandchild", 871004, "Fake")
        first = FakeObject(2, "First", 871002, "Fake", children=[grandchild])
        second = FakeObject(3, "Second", 871003, "Fake")
        parent = FakeObject(1, "Parent", 871001, "Fake", children=[first, second])
        self.document = FakeDocument([parent])
        self.dispatch("list_objects")

        result = self.dispatch("get_object", {"object_id": object_id(1)})

        self.assertEqual(result["children"], [object_id(2), object_id(3)])
        self.assertNotIn(object_id(4), result["children"])
        self.assertEqual(result["child_count"], 2)
        self.assertEqual(result["addressable_child_count"], 2)
        self.assertEqual(result["unaddressable_child_count"], 0)
        self.assertTrue(result["children_complete"])

    def test_children_completeness_reports_unaddressable_only_child(self):
        child = FakeObject(None, "Child", 872002, "Fake")
        parent = FakeObject(1, "Parent", 872001, "Fake", children=[child])
        self.document = FakeDocument([parent])
        self.dispatch("list_objects")

        result = self.dispatch("get_object", {"object_id": object_id(1)})

        self.assertEqual(result["children"], [])
        self.assertEqual(result["child_count"], 1)
        self.assertEqual(result["addressable_child_count"], 0)
        self.assertEqual(result["unaddressable_child_count"], 1)
        self.assertFalse(result["children_complete"])

    def test_children_completeness_reports_mixed_and_duplicate_guid_children(self):
        addressable = FakeObject(2, "Addressable", 873002, "Fake")
        duplicate_a = FakeObject(3, "Duplicate A", 873003, "Fake")
        duplicate_b = FakeObject(3, "Duplicate B", 873004, "Fake")
        parent = FakeObject(
            1,
            "Parent",
            873001,
            "Fake",
            children=[addressable, duplicate_a, duplicate_b],
        )
        self.document = FakeDocument([parent])
        self.dispatch("list_objects")

        result = self.dispatch("get_object", {"object_id": object_id(1)})

        self.assertEqual(result["children"], [object_id(2)])
        self.assertEqual(result["child_count"], 3)
        self.assertEqual(result["addressable_child_count"], 1)
        self.assertEqual(result["unaddressable_child_count"], 2)
        self.assertFalse(result["children_complete"])


if __name__ == "__main__":
    unittest.main()
