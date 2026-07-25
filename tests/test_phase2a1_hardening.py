"""Hardening contracts for Phase 2A pagination and scoped identities."""

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
    FakeVector,
    SCOPE_A,
    SCOPE_B,
    object_id,
    object_scope,
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
        object_scope_tokens = iter(object_scope(index) for index in range(1, 4096))
        self.plugin._OBJECT_SCOPES = self.plugin._ObjectScopeRegistry(
            token_factory=lambda: next(object_scope_tokens)
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

    def test_same_wrapper_reuses_document_scope(self):
        self.document = FakeDocument()

        first_scope = self.plugin._DOCUMENT_SCOPES.scope_for(self.document)
        second_scope = self.plugin._DOCUMENT_SCOPES.scope_for(self.document)

        self.assertEqual(first_scope, SCOPE_A)
        self.assertEqual(second_scope, first_scope)

    def test_equivalent_c4d_atom_wrappers_reuse_document_scope(self):
        atom_key = object()
        first_wrapper = FakeDocument(atom_key=atom_key)
        second_wrapper = FakeDocument(atom_key=atom_key)

        first_scope = self.plugin._DOCUMENT_SCOPES.scope_for(first_wrapper)
        second_scope = self.plugin._DOCUMENT_SCOPES.scope_for(second_wrapper)

        self.assertIsNot(first_wrapper, second_wrapper)
        self.assertTrue(first_wrapper == second_wrapper)
        self.assertEqual(second_scope, first_scope)

    def test_equivalent_wrapper_can_immediately_get_issued_object_id(self):
        atom_key = object()
        shared_object = FakeObject(77, "Document A", 830001, "Fake A")
        first_wrapper = FakeDocument([shared_object], atom_key=atom_key)
        second_wrapper = FakeDocument([shared_object], atom_key=atom_key)
        self.document = first_wrapper
        issued_id = self.dispatch("list_objects")["objects"][0]["object_id"]

        self.document = second_wrapper
        response = self.execute_task("get_object", {"object_id": issued_id})

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["name"], "Document A")

    def test_repeated_a_to_a_wrapper_sequence_keeps_object_id_valid(self):
        atom_key = object()
        shared_object = FakeObject(77, "Document A", 830011, "Fake A")
        wrappers = [
            FakeDocument([shared_object], atom_key=atom_key)
            for _ in range(5)
        ]
        self.document = wrappers[0]
        issued_id = self.dispatch("list_objects")["objects"][0]["object_id"]

        self.document = wrappers[1]
        first_get = self.execute_task("get_object", {"object_id": issued_id})
        self.document = wrappers[2]
        second_get = self.execute_task("get_object", {"object_id": issued_id})
        self.document = wrappers[3]
        repeated_id = self.dispatch("list_objects")["objects"][0]["object_id"]
        self.document = wrappers[4]
        third_get = self.execute_task("get_object", {"object_id": issued_id})

        self.assertTrue(first_get["ok"])
        self.assertTrue(second_get["ok"])
        self.assertEqual(repeated_id, issued_id)
        self.assertTrue(third_get["ok"])

    def test_cross_document_same_raw_guid_fails_closed_and_switch_back_recovers(self):
        atom_a = object()
        object_a = FakeObject(77, "Document A", 830001, "Fake A")
        object_b = FakeObject(77, "Document B", 830002, "Fake B")
        document_a_first = FakeDocument([object_a], atom_key=atom_a)
        document_b = FakeDocument([object_b], atom_key=object())
        document_a_second = FakeDocument([object_a], atom_key=atom_a)
        self.document = document_a_first
        issued_id = self.dispatch("list_objects")["objects"][0]["object_id"]

        self.document = document_b
        mismatch = self.execute_task("get_object", {"object_id": issued_id})

        self.assertEqual(mismatch["error"]["code"], "DOCUMENT_MISMATCH")
        self.assertNotIn("Document B", str(mismatch))

        self.document = document_a_second
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

    def test_different_live_document_wrappers_do_not_match(self):
        first_wrapper = FakeDocument([FakeObject(77, "First", 850001, "Fake")])
        self.document = first_wrapper
        issued_id = self.dispatch("list_objects")["objects"][0]["object_id"]
        self.document = FakeDocument([FakeObject(77, "First", 850001, "Fake")])

        response = self.execute_task("get_object", {"object_id": issued_id})

        self.assertEqual(response["error"]["code"], "DOCUMENT_MISMATCH")

    def test_dead_registered_document_scope_becomes_stale(self):
        document_a = FakeDocument([FakeObject(77, "Closed", 851001, "Fake")])
        self.document = document_a
        issued_id = self.dispatch("list_objects")["objects"][0]["object_id"]
        document_a.alive = False
        self.document = FakeDocument([FakeObject(77, "Other", 851002, "Fake")])

        response = self.execute_task("get_object", {"object_id": issued_id})

        self.assertEqual(response["error"]["code"], "STALE_OBJECT_ID")
        self.assertNotIn(SCOPE_A, self.plugin._DOCUMENT_SCOPES._documents)
        self.assertNotIn(SCOPE_A, self.plugin._OBJECT_SCOPES._objects)

    def test_liveness_exception_prunes_scope_fail_closed(self):
        document_a = FakeDocument([FakeObject(77, "Closed", 851011, "Fake")])
        self.document = document_a
        issued_id = self.dispatch("list_objects")["objects"][0]["object_id"]
        document_a.liveness_error = True
        self.document = FakeDocument([FakeObject(77, "Other", 851012, "Fake")])

        response = self.execute_task("get_object", {"object_id": issued_id})

        self.assertEqual(response["error"]["code"], "STALE_OBJECT_ID")
        self.assertNotIn(SCOPE_A, self.plugin._DOCUMENT_SCOPES._documents)
        self.assertNotIn(SCOPE_A, self.plugin._OBJECT_SCOPES._objects)

    def test_no_active_document_prunes_closed_document_object_bucket(self):
        document = FakeDocument([FakeObject(0, "Closed", 851021, "Cube")])
        self.document = document
        self.dispatch("list_objects")
        self.assertIn(SCOPE_A, self.plugin._OBJECT_SCOPES._objects)
        document.alive = False
        self.document = None

        response = self.execute_task("get_scene_info")

        self.assertEqual(response["error"]["code"], "NO_ACTIVE_DOCUMENT")
        self.assertNotIn(SCOPE_A, self.plugin._OBJECT_SCOPES._objects)

    def test_equality_exception_never_matches_document_scope(self):
        first_wrapper = FakeDocument(equality_error=True)
        second_wrapper = FakeDocument(atom_key=first_wrapper.atom_key)

        first_scope = self.plugin._DOCUMENT_SCOPES.scope_for(first_wrapper)
        second_scope = self.plugin._DOCUMENT_SCOPES.scope_for(second_wrapper)

        self.assertNotEqual(second_scope, first_scope)

    def test_equality_exception_fails_lookup_closed(self):
        atom_key = object()
        shared_object = FakeObject(77, "Document A", 852001, "Fake")
        first_wrapper = FakeDocument(
            [shared_object],
            atom_key=atom_key,
            equality_error=True,
        )
        second_wrapper = FakeDocument([shared_object], atom_key=atom_key)
        self.document = first_wrapper
        issued_id = self.dispatch("list_objects")["objects"][0]["object_id"]

        self.document = second_wrapper
        response = self.execute_task("get_object", {"object_id": issued_id})

        self.assertEqual(
            response["error"]["code"],
            "DOCUMENT_ID_UNVERIFIED",
        )

    def test_document_and_object_scopes_are_strictly_canonical(self):
        valid = (
            object_id(1),
            object_id(123),
            object_id("f" * 32),
        )
        invalid = (
            "c4d:",
            "c4d:abc:def",
            "c4d:{}".format(SCOPE_A),
            "c4d:{}:123".format(SCOPE_A),
            "c4d:{}:{}".format(SCOPE_A.upper(), object_scope(1)),
            "c4d:{}:{}".format(SCOPE_A, "A" * 32),
            "c4d:{}:{}:extra".format(SCOPE_A, object_scope(1)),
            " c4d:{}:{}".format(SCOPE_A, object_scope(1)),
            "c4d:{}:{} ".format(SCOPE_A, object_scope(1)),
            "c4d:{}:{}".format(SCOPE_A, "g" * 32),
        )
        for value in valid:
            self.assertTrue(self.plugin._is_valid_object_id(value), value)
        for value in invalid:
            self.assertFalse(self.plugin._is_valid_object_id(value), value)

        for scope in (object_scope(1), object_scope(123), "f" * 32):
            serialized = self.plugin._serialize_object_id(SCOPE_A, scope)
            self.assertEqual(
                self.plugin._parse_object_id(serialized),
                (SCOPE_A, scope),
            )

    def test_normal_objects_do_not_require_a_guid_for_addressing(self):
        for guid in (0, None, RuntimeError("GetGUID unavailable")):
            with self.subTest(guid=guid):
                self.setUp()
                obj = FakeObject(guid, "Cube", 860001, "Cube")
                self.document = FakeDocument([obj])

                summary = self.dispatch("list_objects")["objects"][0]

                self.assertTrue(summary["addressable"])
                self.assertIsNotNone(summary["object_id"])
                self.assertEqual(obj.guid_reads, 0)

    def test_object_registry_reuses_scope_for_same_and_equivalent_wrappers(self):
        atom_key = object()
        first_wrapper = FakeObject(
            None, "Cube", 860101, "Cube", atom_key=atom_key
        )
        second_wrapper = FakeObject(
            None, "Cube", 860101, "Cube", atom_key=atom_key
        )

        first_scope, first_error = self.plugin._OBJECT_SCOPES.scope_for(
            SCOPE_A, first_wrapper
        )
        repeated_scope, repeated_error = self.plugin._OBJECT_SCOPES.scope_for(
            SCOPE_A, first_wrapper
        )
        equivalent_scope, equivalent_error = self.plugin._OBJECT_SCOPES.scope_for(
            SCOPE_A, second_wrapper
        )

        self.assertIsNot(first_wrapper, second_wrapper)
        self.assertTrue(first_wrapper == second_wrapper)
        self.assertIsNone(first_error)
        self.assertIsNone(repeated_error)
        self.assertIsNone(equivalent_error)
        self.assertEqual(repeated_scope, first_scope)
        self.assertEqual(equivalent_scope, first_scope)

    def test_different_object_wrappers_preserve_list_get_identity(self):
        document_atom = object()
        object_atom = object()
        first_object = FakeObject(
            0, "Cube", 860201, "Cube", atom_key=object_atom
        )
        second_object = FakeObject(
            None, "Cube", 860201, "Cube", atom_key=object_atom
        )
        first_document = FakeDocument(
            [first_object], atom_key=document_atom
        )
        second_document = FakeDocument(
            [second_object], atom_key=document_atom
        )

        self.document = first_document
        issued_id = self.dispatch("list_objects")["objects"][0]["object_id"]
        self.document = second_document
        fetched = self.execute_task("get_object", {"object_id": issued_id})
        repeated_id = self.dispatch("list_objects")["objects"][0]["object_id"]
        fetched_again = self.execute_task("get_object", {"object_id": issued_id})

        self.assertTrue(fetched["ok"])
        self.assertTrue(fetched_again["ok"])
        self.assertEqual(repeated_id, issued_id)
        self.assertEqual(first_object.guid_reads, 0)
        self.assertEqual(second_object.guid_reads, 0)

    def test_rename_and_transform_change_preserve_object_id(self):
        obj = FakeObject(0, "Cube", 860301, "Cube")
        self.document = FakeDocument([obj])
        issued_id = self.dispatch("list_objects")["objects"][0]["object_id"]

        obj.name = "Cube Renamed"
        obj.position = FakeVector(10.0, 20.0, 30.0)
        obj.rotation = FakeVector(math.pi / 2.0, 0.0, -math.pi / 4.0)
        obj.scale = FakeVector(2.0, 3.0, 4.0)
        repeated_id = self.dispatch("list_objects")["objects"][0]["object_id"]
        result = self.dispatch("get_object", {"object_id": issued_id})

        self.assertEqual(repeated_id, issued_id)
        self.assertEqual(result["object_id"], issued_id)
        self.assertEqual(result["name"], "Cube Renamed")
        self.assertEqual(result["transform"]["position"], [10.0, 20.0, 30.0])
        self.assertEqual(result["transform"]["rotation_deg"], [90.0, 0.0, -45.0])
        self.assertEqual(result["transform"]["scale"], [2.0, 3.0, 4.0])

    def test_same_name_type_and_guid_do_not_merge_distinct_atoms(self):
        first = FakeObject(0, "Cube", 860401, "Cube")
        second = FakeObject(0, "Cube", 860401, "Cube")
        self.document = FakeDocument([first, second])

        objects = self.dispatch("list_objects")["objects"]

        self.assertTrue(all(item["addressable"] for item in objects))
        self.assertNotEqual(objects[0]["object_id"], objects[1]["object_id"])
        self.assertEqual(first.guid_reads, 0)
        self.assertEqual(second.guid_reads, 0)

    def test_dead_object_scope_is_stale_removed_and_never_reused(self):
        obj = FakeObject(0, "Deleted", 860501, "Cube")
        self.document = FakeDocument([obj])
        issued_id = self.dispatch("list_objects")["objects"][0]["object_id"]
        issued_scope = self.plugin._parse_object_id(issued_id)[1]

        obj.alive = False
        self.document.roots = []
        stale = self.execute_task("get_object", {"object_id": issued_id})

        self.assertEqual(stale["error"]["code"], "STALE_OBJECT_ID")
        self.assertNotIn(
            issued_scope,
            self.plugin._OBJECT_SCOPES._objects.get(SCOPE_A, {}),
        )

        replacement = FakeObject(0, "Replacement", 860502, "Cube")
        self.document.roots = [replacement]
        replacement_id = self.dispatch("list_objects")["objects"][0]["object_id"]
        self.assertNotEqual(replacement_id, issued_id)
        self.assertIn(issued_scope, self.plugin._OBJECT_SCOPES._issued_scopes)

    def test_live_registered_object_outside_hierarchy_fails_closed(self):
        obj = FakeObject(0, "Detached", 860601, "Cube")
        self.document = FakeDocument([obj])
        issued_id = self.dispatch("list_objects")["objects"][0]["object_id"]
        self.document.roots = []

        response = self.execute_task("get_object", {"object_id": issued_id})

        self.assertEqual(response["error"]["code"], "OBJECT_NOT_IN_DOCUMENT")

    def test_object_equality_exception_and_non_bool_fail_closed(self):
        for equality_result in (RuntimeError("equality failed"), 1):
            with self.subTest(equality_result=equality_result):
                self.setUp()
                document_atom = object()
                object_atom = object()
                registered = FakeObject(
                    0, "Registered", 860701, "Cube", atom_key=object_atom
                )
                self.document = FakeDocument(
                    [registered], atom_key=document_atom
                )
                issued_id = self.dispatch("list_objects")["objects"][0]["object_id"]

                current = FakeObject(
                    0, "Current", 860701, "Cube", atom_key=object_atom
                )
                self.document = FakeDocument([current], atom_key=document_atom)
                if isinstance(equality_result, Exception):
                    registered.equality_error = True
                else:
                    registered.equality_result = equality_result

                response = self.execute_task(
                    "get_object", {"object_id": issued_id}
                )

                self.assertEqual(
                    response["error"]["code"], "OBJECT_ID_UNVERIFIED"
                )
                self.assertNotIn("Current", str(response.get("result")))

    def test_object_liveness_exception_is_unverified_without_scope_growth(self):
        document_atom = object()
        object_atom = object()
        registered = FakeObject(
            0, "Registered", 860751, "Cube", atom_key=object_atom
        )
        self.document = FakeDocument([registered], atom_key=document_atom)
        issued_id = self.dispatch("list_objects")["objects"][0]["object_id"]
        issued_count = len(self.plugin._OBJECT_SCOPES._issued_scopes)
        registered.liveness_error = True

        lookup = self.execute_task("get_object", {"object_id": issued_id})
        current = FakeObject(
            0, "Current", 860751, "Cube", atom_key=object_atom
        )
        self.document = FakeDocument([current], atom_key=document_atom)
        summary = self.dispatch("list_objects")["objects"][0]

        self.assertEqual(lookup["error"]["code"], "OBJECT_ID_UNVERIFIED")
        self.assertFalse(summary["addressable"])
        self.assertEqual(summary["id_error"], "OBJECT_ID_UNVERIFIED")
        self.assertEqual(
            len(self.plugin._OBJECT_SCOPES._issued_scopes), issued_count
        )

    def test_uncertain_registry_comparison_does_not_mint_new_scope(self):
        registered = FakeObject(0, "Registered", 860801, "Cube")
        self.document = FakeDocument([registered])
        self.dispatch("list_objects")
        issued_count = len(self.plugin._OBJECT_SCOPES._issued_scopes)
        registered.equality_error = True
        newcomer = FakeObject(0, "Newcomer", 860802, "Cube")
        self.document.roots = [newcomer]

        summary = self.dispatch("list_objects")["objects"][0]

        self.assertFalse(summary["addressable"])
        self.assertEqual(summary["id_error"], "OBJECT_ID_UNVERIFIED")
        self.assertEqual(
            len(self.plugin._OBJECT_SCOPES._issued_scopes), issued_count
        )

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

        self.assertEqual(result["children"], [object_id(2), object_id(4)])
        self.assertNotIn(object_id(3), result["children"])
        self.assertEqual(result["child_count"], 2)
        self.assertEqual(result["addressable_child_count"], 2)
        self.assertEqual(result["unaddressable_child_count"], 0)
        self.assertTrue(result["children_complete"])

    def test_children_completeness_reports_unaddressable_only_child(self):
        child = FakeObject(None, "Child", 872002, "Fake")
        parent = FakeObject(1, "Parent", 872001, "Fake")
        self.document = FakeDocument([parent])
        parent_id = self.dispatch("list_objects")["objects"][0]["object_id"]
        self.plugin._OBJECT_SCOPES._token_factory = lambda: "not-a-scope"
        parent.children = [child]
        parent._down = child
        listed = self.dispatch("list_objects")["objects"]
        self.assertEqual(listed[0]["object_id"], parent_id)
        self.assertFalse(listed[1]["addressable"])
        self.assertEqual(listed[1]["id_error"], "OBJECT_ID_UNAVAILABLE")

        result = self.dispatch("get_object", {"object_id": parent_id})

        self.assertEqual(result["children"], [])
        self.assertEqual(result["child_count"], 1)
        self.assertEqual(result["addressable_child_count"], 0)
        self.assertEqual(result["unaddressable_child_count"], 1)
        self.assertFalse(result["children_complete"])

    def test_children_completeness_reports_mixed_and_unverified_children(self):
        addressable = FakeObject(2, "Addressable", 873002, "Fake")
        duplicate_atom = object()
        duplicate_a = FakeObject(
            3, "Duplicate A", 873003, "Fake", atom_key=duplicate_atom
        )
        duplicate_b = FakeObject(
            3, "Duplicate B", 873004, "Fake", atom_key=duplicate_atom
        )
        parent = FakeObject(
            1,
            "Parent",
            873001,
            "Fake",
            children=[addressable, duplicate_a, duplicate_b],
        )
        self.document = FakeDocument([parent])
        listed = self.dispatch("list_objects")["objects"]
        parent_id = listed[0]["object_id"]
        addressable_id = listed[1]["object_id"]

        result = self.dispatch("get_object", {"object_id": parent_id})

        self.assertEqual(result["children"], [addressable_id])
        self.assertEqual(result["child_count"], 3)
        self.assertEqual(result["addressable_child_count"], 1)
        self.assertEqual(result["unaddressable_child_count"], 2)
        self.assertFalse(result["children_complete"])


if __name__ == "__main__":
    unittest.main()
