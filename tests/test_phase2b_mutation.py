"""Phase 2B typed mutation, native undo, and write-timeout contracts."""

from __future__ import annotations

import asyncio
import math
import queue
import socket
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cinema4d_mcp import server as external_server

from tests.test_phase1_transport import TOKEN, load_plugin_module
from tests.test_phase2a_read import (
    FakeDocument,
    FakeObject,
    FakeVector,
    SCOPE_A,
    SCOPE_B,
    link_hierarchy,
    object_id,
    object_scope,
)


class MutationFakeObject(FakeObject):
    def __init__(self, guid, name, type_id, type_name, **kwargs):
        super().__init__(guid, name, type_id, type_name, **kwargs)
        self.document = None
        self.parent = None

    def _log(self, action):
        if self.document is not None:
            self.document.log.append(action)

    def SetName(self, value):
        self._log("SetName")
        self.name = value

    def SetRelPos(self, value):
        self._log("SetRelPos")
        self.position = value

    def SetRelRot(self, value):
        self._log("SetRelRot")
        self.rotation = value

    def SetRelScale(self, value):
        self._log("SetRelScale")
        self.scale = value

    def Remove(self):
        if self.document is None:
            raise RuntimeError("object is not attached to a document")
        self.document.log.append("Remove")
        self.document.remove_object(self)


class MutationFakeDocument(FakeDocument):
    def __init__(self, roots=None, **kwargs):
        super().__init__(roots, **kwargs)
        self.log = []
        self.start_undo_result = True
        self.add_undo_result = True
        self.end_undo_result = True
        self.pending_undo = None
        self.undo_stack = []
        self._attach_all()

    def _attach(self, obj, parent=None):
        obj.document = self
        obj.parent = parent
        for child in obj.children:
            self._attach(child, obj)

    def _attach_all(self):
        self.roots = link_hierarchy(self.roots)
        for root in self.roots:
            self._attach(root)

    def InsertObject(self, obj):
        self.log.append("InsertObject")
        self.roots.append(obj)
        self._attach_all()

    def remove_object(self, obj):
        if obj.parent is None:
            self.roots.remove(obj)
        else:
            obj.parent.children.remove(obj)
        obj.parent = None
        self._attach_all()

    def StartUndo(self):
        self.log.append("StartUndo")
        self.pending_undo = None
        return self.start_undo_result

    def AddUndo(self, undo_type, obj):
        self.log.append(("AddUndo", undo_type))
        if not self.add_undo_result:
            return False
        snapshot = None
        if undo_type == "CHANGE":
            snapshot = {
                "name": obj.name,
                "position": obj.position,
                "rotation": obj.rotation,
                "scale": obj.scale,
            }
        elif undo_type == "DELETE":
            parent = obj.parent
            siblings = self.roots if parent is None else parent.children
            snapshot = {"parent": parent, "index": siblings.index(obj)}
        self.pending_undo = {
            "type": undo_type,
            "object": obj,
            "snapshot": snapshot,
        }
        return True

    def EndUndo(self):
        self.log.append("EndUndo")
        if self.end_undo_result and self.pending_undo is not None:
            self.undo_stack.append(self.pending_undo)
            self.pending_undo = None
        return self.end_undo_result

    def simulate_native_delete_undo(self, replacement=None):
        if not self.undo_stack or self.undo_stack[-1]["type"] != "DELETE":
            raise AssertionError("no native delete undo is available")
        undo_entry = self.undo_stack.pop()
        snapshot = undo_entry["snapshot"]
        restored = replacement or undo_entry["object"]
        parent = snapshot["parent"]
        siblings = self.roots if parent is None else parent.children
        siblings.insert(snapshot["index"], restored)
        self._attach_all()
        self.log.append("NativeUndoDelete")
        return restored

    def GetUndoPtr(self):
        raise AssertionError("MCP mutation paths must not call GetUndoPtr")

    def DoUndo(self):
        raise AssertionError("MCP mutation paths must not call DoUndo")


class Phase2BMutationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = load_plugin_module()

    def setUp(self):
        self.document = MutationFakeDocument()
        scope_tokens = iter((SCOPE_A, SCOPE_B, "c" * 32, "d" * 32))
        self.plugin._DOCUMENT_SCOPES = self.plugin._DocumentScopeRegistry(
            token_factory=lambda: next(scope_tokens)
        )
        object_tokens = iter(object_scope(index) for index in range(1, 4096))
        self.plugin._OBJECT_SCOPES = self.plugin._ObjectScopeRegistry(
            token_factory=lambda: next(object_tokens)
        )
        self.plugin.c4d.documents = SimpleNamespace(
            GetActiveDocument=lambda: self.document
        )
        self.plugin.c4d.utils = SimpleNamespace(
            RadToDeg=math.degrees,
            DegToRad=math.radians,
        )
        self.plugin.c4d.GETACTIVEOBJECTFLAGS_CHILDREN = object()
        self.plugin.c4d.threading.GeIsMainThread = lambda: True
        # Arbitrary fake values model documented symbols without guessing IDs.
        type_symbols = {
            "Onull": 10001,
            "Ocube": 10002,
            "Osphere": 10003,
            "Oplane": 10004,
            "Ocylinder": 10005,
            "Ocone": 10006,
        }
        for symbol, value in type_symbols.items():
            setattr(self.plugin.c4d, symbol, value)
        self.type_names = {
            10001: "Null",
            10002: "Cube",
            10003: "Sphere",
            10004: "Plane",
            10005: "Cylinder",
            10006: "Cone",
        }
        self.plugin.c4d.BaseObject = lambda type_id: MutationFakeObject(
            0,
            self.type_names[type_id],
            type_id,
            self.type_names[type_id],
        )
        self.plugin.c4d.Vector = FakeVector
        self.plugin.c4d.UNDOTYPE_NEWOBJ = "NEW"
        self.plugin.c4d.UNDOTYPE_CHANGE = "CHANGE"
        self.plugin.c4d.UNDOTYPE_DELETEOBJ = "DELETE"
        self.plugin.c4d.StopAllThreads = MagicMock()
        self.plugin.c4d.EventAdd = MagicMock()

    def make_server(self, **overrides):
        options = {
            "msg_queue": queue.Queue(),
            "token": TOKEN,
            "request_size_limit": 1024,
            "client_timeout": 0.25,
            "main_thread_timeout": 0.5,
        }
        options.update(overrides)
        server = self.plugin.C4DSocketServer(**options)
        server.running = True
        return server

    def dispatch(self, command, params=None):
        return self.make_server()._dispatch_on_main_thread(command, params or {})

    def execute_task(self, command, params=None):
        server = self.make_server()
        task = self.plugin._MainThreadTask(
            server, command, params or {}, "phase2b-request"
        )
        self.assertTrue(task.execute())
        return task.result

    def create(self, **params):
        request = {"type": "cube"}
        request.update(params)
        return self.dispatch("create_object", request)

    def test_plugin_surface_includes_phase2b_tools_and_phase2c_save(self):
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
        for forbidden in (
            "undo_last",
            "save_as",
            "save_project",
            "execute_python",
            "octane_command",
            "redshift_command",
        ):
            self.assertNotIn(forbidden, self.plugin.ALLOWED_COMMANDS)
        self.assertFalse(hasattr(self.plugin, "_undo_last"))
        self.assertFalse(hasattr(self.plugin, "_MUTATION_LEDGER"))

    def test_each_allowlisted_creation_type_is_top_level_and_addressable(self):
        expected_types = ("null", "cube", "sphere", "plane", "cylinder", "cone")
        for object_type in expected_types:
            with self.subTest(object_type=object_type):
                self.setUp()
                result = self.dispatch("create_object", {"type": object_type})
                created = self.document.roots[0]

                self.assertIsNone(created.parent)
                self.assertEqual(set(result), {"object"})
                self.assertTrue(self.plugin._is_valid_object_id(result["object"]["object_id"]))
                self.assertNotIn("mutation_id", result)
                self.assertNotIn("undo_available", result)
                self.assertEqual(created.guid_reads, 0)
                create_order = (
                    "StartUndo",
                    "InsertObject",
                    ("AddUndo", "NEW"),
                    "EndUndo",
                )
                indexes = [self.document.log.index(item) for item in create_order]
                self.assertEqual(indexes, sorted(indexes))

    def test_create_applies_name_and_relative_hpb_psr(self):
        result = self.create(
            name="예제 Cube",
            position=[1, 2.5, -3],
            rotation_deg=[90, -45, 180],
            scale=[1, 2, 0.5],
        )
        obj = self.document.roots[0]

        self.assertEqual(obj.name, "예제 Cube")
        self.assertEqual([obj.position.x, obj.position.y, obj.position.z], [1.0, 2.5, -3.0])
        self.assertEqual(
            [obj.rotation.x, obj.rotation.y, obj.rotation.z],
            [math.pi / 2, -math.pi / 4, math.pi],
        )
        self.assertEqual([obj.scale.x, obj.scale.y, obj.scale.z], [1.0, 2.0, 0.5])
        self.assertEqual(result["object"]["rotation_deg"], [90.0, -45.0, 180.0])
        self.plugin.c4d.StopAllThreads.assert_called_once_with()
        self.plugin.c4d.EventAdd.assert_called_once_with()

    def test_create_rejects_raw_or_unknown_type_before_mutation(self):
        for object_type in (5159, "torus", None, True):
            with self.subTest(object_type=object_type):
                response = self.execute_task("create_object", {"type": object_type})
                self.assertEqual(response["error"]["code"], "UNSUPPORTED_OBJECT_TYPE")
        self.assertEqual(self.document.roots, [])
        self.plugin.c4d.StopAllThreads.assert_not_called()

    def test_strict_mutation_validation_rejects_bad_shapes_and_fields(self):
        invalid = (
            ("create_object", {"type": "cube", "position": [1, 2]}),
            ("create_object", {"type": "cube", "position": [True, 2, 3]}),
            ("create_object", {"type": "cube", "position": [math.nan, 2, 3]}),
            ("create_object", {"type": "cube", "position": [10 ** 1000, 2, 3]}),
            ("create_object", {"type": "cube", "scale": [1, math.inf, 1]}),
            ("create_object", {"type": "cube", "name": "bad\nname"}),
            ("create_object", {"type": "cube", "parent_id": object_id(1)}),
            ("update_object", {"object_id": object_id(1)}),
            ("delete_object", {"object_id": object_id(1), "recursive": 1}),
        )
        for command, params in invalid:
            with self.subTest(command=command, params=params):
                response = self.execute_task(command, params)
                self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.plugin.c4d.StopAllThreads.assert_not_called()

    def test_raw_bridge_uses_the_same_phase2b_validation_codes(self):
        server = self.make_server()
        base = {
            "protocol_version": 1,
            "request_id": "phase2b-validation",
            "token": TOKEN,
        }
        valid = (
            ("create_object", {"type": "cube"}),
            ("update_object", {"object_id": object_id(1), "name": "x"}),
            ("delete_object", {"object_id": object_id(1), "recursive": False}),
        )
        for command, params in valid:
            self.assertIsNone(
                server._validate_request(dict(base, command=command, params=params))
            )

        unsupported = server._validate_request(
            dict(base, command="create_object", params={"type": 5159})
        )
        self.assertEqual(unsupported[0], "UNSUPPORTED_OBJECT_TYPE")
        invalid = (
            ("create_object", {"type": "cube", "position": [True, 2, 3]}),
            ("update_object", {"object_id": object_id(1)}),
            ("delete_object", {"object_id": object_id(1), "recursive": 1}),
        )
        for command, params in invalid:
            with self.subTest(command=command):
                error = server._validate_request(
                    dict(base, command=command, params=params)
                )
                self.assertEqual(error[0], "INVALID_PARAMS")

    def test_update_keeps_id_and_uses_one_change_undo_before_setters(self):
        created = self.create()
        object_id_value = created["object"]["object_id"]
        self.document.log.clear()

        updated = self.dispatch(
            "update_object",
            {
                "object_id": object_id_value,
                "name": "Updated",
                "position": [10, 20, 30],
                "rotation_deg": [10, 20, 30],
                "scale": [2, 3, 4],
            },
        )

        self.assertEqual(updated["object"]["object_id"], object_id_value)
        self.assertEqual(updated["object"]["name"], "Updated")
        self.assertEqual(set(updated), {"object"})
        self.assertNotIn("mutation_id", updated)
        self.assertNotIn("undo_available", updated)
        self.assertEqual(self.document.log.count(("AddUndo", "CHANGE")), 1)
        add_index = self.document.log.index(("AddUndo", "CHANGE"))
        end_index = self.document.log.index("EndUndo")
        for setter in ("SetName", "SetRelPos", "SetRelRot", "SetRelScale"):
            setter_index = self.document.log.index(setter)
            self.assertGreater(setter_index, add_index)
            self.assertLess(setter_index, end_index)

    def test_update_cross_document_and_detached_objects_fail_closed(self):
        created = self.create()
        object_id_value = created["object"]["object_id"]
        document_a = self.document
        self.document = MutationFakeDocument()
        mismatch = self.execute_task(
            "update_object", {"object_id": object_id_value, "name": "Wrong"}
        )
        self.assertEqual(mismatch["error"]["code"], "DOCUMENT_MISMATCH")

        self.document = document_a
        obj = self.document.roots[0]
        self.document.remove_object(obj)
        detached = self.execute_task(
            "update_object", {"object_id": object_id_value, "name": "Wrong"}
        )
        self.assertEqual(detached["error"]["code"], "OBJECT_NOT_IN_DOCUMENT")

    def test_update_stale_and_unverified_objects_fail_closed(self):
        created = self.create()
        object_id_value = created["object"]["object_id"]
        obj = self.document.roots[0]
        obj.alive = False

        stale = self.execute_task(
            "update_object", {"object_id": object_id_value, "name": "Wrong"}
        )

        self.assertEqual(stale["error"]["code"], "STALE_OBJECT_ID")
        self.plugin.c4d.StopAllThreads.assert_called_once_with()

        self.setUp()
        created = self.create()
        object_id_value = created["object"]["object_id"]
        self.plugin.c4d.StopAllThreads.reset_mock()
        self.document.roots[0].equality_error = True

        unverified = self.execute_task(
            "update_object", {"object_id": object_id_value, "name": "Wrong"}
        )

        self.assertEqual(unverified["error"]["code"], "OBJECT_ID_UNVERIFIED")
        self.plugin.c4d.StopAllThreads.assert_not_called()

    def test_update_add_undo_failure_does_not_mutate(self):
        created = self.create(name="Before")
        object_id_value = created["object"]["object_id"]
        self.document.add_undo_result = False

        response = self.execute_task(
            "update_object", {"object_id": object_id_value, "name": "After"}
        )

        self.assertEqual(response["error"]["code"], "MUTATION_FAILED")
        self.assertEqual(self.document.roots[0].name, "Before")

    def test_start_undo_failure_is_known_and_does_not_mutate(self):
        self.document.start_undo_result = False

        response = self.execute_task("create_object", {"type": "cube"})

        self.assertEqual(response["error"]["code"], "MUTATION_FAILED")
        self.assertEqual(self.document.roots, [])
        self.assertFalse(response["error"]["retryable"])

    def test_create_add_undo_failure_is_outcome_unknown_after_insertion(self):
        self.document.add_undo_result = False

        response = self.execute_task("create_object", {"type": "cube"})

        self.assertEqual(response["error"]["code"], "OUTCOME_UNKNOWN")
        self.assertFalse(response["error"]["retryable"])
        self.assertEqual(len(self.document.roots), 1)

    def test_post_create_identity_failure_is_outcome_unknown(self):
        identity_error = self.plugin._BridgeCommandError(
            "OBJECT_ID_UNVERIFIED",
            "forced post-create identity failure",
        )

        with patch.object(
            self.plugin,
            "_resolve_object_entry",
            side_effect=identity_error,
        ):
            response = self.execute_task("create_object", {"type": "cube"})

        self.assertEqual(response["error"]["code"], "OUTCOME_UNKNOWN")
        self.assertFalse(response["error"]["retryable"])
        self.assertEqual(len(self.document.roots), 1)
        self.assertIn("EndUndo", self.document.log)
        self.assertEqual(len(self.document.undo_stack), 1)
        self.assertNotEqual(response["error"]["code"], "OBJECT_ID_UNVERIFIED")

    def test_post_create_metadata_failure_is_outcome_unknown(self):
        with patch.object(
            self.plugin,
            "_mutation_object_payload",
            side_effect=RuntimeError("forced metadata failure"),
        ):
            response = self.execute_task("create_object", {"type": "cube"})

        self.assertEqual(response["error"]["code"], "OUTCOME_UNKNOWN")
        self.assertFalse(response["error"]["retryable"])
        self.assertEqual(len(self.document.roots), 1)
        self.assertIn("EndUndo", self.document.log)
        self.assertEqual(len(self.document.undo_stack), 1)

    def test_pre_insertion_allocation_failure_remains_mutation_failed(self):
        self.plugin.c4d.BaseObject = MagicMock(return_value=None)

        response = self.execute_task("create_object", {"type": "cube"})

        self.assertEqual(response["error"]["code"], "MUTATION_FAILED")
        self.assertFalse(response["error"]["retryable"])
        self.assertEqual(self.document.roots, [])
        self.assertNotIn("InsertObject", self.document.log)
        self.assertFalse(any(entry == ("AddUndo", "NEW") for entry in self.document.log))

    def test_partial_update_and_end_undo_failure_are_outcome_unknown(self):
        created = self.create(name="Before")
        object_id_value = created["object"]["object_id"]
        obj = self.document.roots[0]
        obj.SetRelPos = MagicMock(side_effect=RuntimeError("setter failed"))

        partial = self.execute_task(
            "update_object",
            {
                "object_id": object_id_value,
                "name": "Partially Changed",
                "position": [1, 2, 3],
            },
        )

        self.assertEqual(partial["error"]["code"], "OUTCOME_UNKNOWN")
        self.assertEqual(obj.name, "Partially Changed")
        self.assertFalse(partial["error"]["retryable"])

        self.setUp()
        created = self.create(name="Before")
        self.document.end_undo_result = False
        failed_end = self.execute_task(
            "update_object",
            {"object_id": created["object"]["object_id"], "name": "After"},
        )
        self.assertEqual(failed_end["error"]["code"], "OUTCOME_UNKNOWN")
        self.assertEqual(self.document.roots[0].name, "After")

    def test_delete_leaf_and_scope_non_reuse(self):
        created = self.create()
        deleted_id = created["object"]["object_id"]
        document_scope, deleted_scope = self.plugin._parse_object_id(deleted_id)
        deleted = self.dispatch(
            "delete_object", {"object_id": deleted_id, "recursive": False}
        )

        self.assertEqual(deleted["deleted_object_id"], deleted_id)
        self.assertEqual(self.document.roots, [])
        self.assertNotIn(
            deleted_scope,
            self.plugin._OBJECT_SCOPES._objects.get(document_scope, {}),
        )
        self.assertIn(deleted_scope, self.plugin._OBJECT_SCOPES._issued_scopes)
        self.assertTrue(
            self.plugin._OBJECT_SCOPES.retire_scopes(
                document_scope,
                (deleted_scope,),
            )
        )
        stale = self.execute_task("get_object", {"object_id": deleted_id})
        self.assertEqual(stale["error"]["code"], "STALE_OBJECT_ID")
        replacement = self.create()
        self.assertNotEqual(replacement["object"]["object_id"], deleted_id)

    def test_native_delete_undo_assigns_a_fresh_id_to_a_different_wrapper(self):
        created = self.create(name="MCP_E2E_Cube")
        deleted_id = created["object"]["object_id"]
        deleted_wrapper = self.document.roots[0]

        self.dispatch(
            "delete_object", {"object_id": deleted_id, "recursive": False}
        )
        deleted_wrapper.liveness_error = True
        restored_wrapper = MutationFakeObject(
            0,
            "MCP_E2E_Cube",
            10002,
            "Cube",
            atom_key=object(),
        )
        self.assertIsNot(restored_wrapper, deleted_wrapper)
        self.assertFalse(restored_wrapper == deleted_wrapper)
        self.document.simulate_native_delete_undo(restored_wrapper)

        listed = self.dispatch("list_objects")
        self.assertEqual(listed["returned_count"], 1)
        restored_summary = listed["objects"][0]
        restored_id = restored_summary["object_id"]
        self.assertTrue(restored_summary["addressable"])
        self.assertIsNotNone(restored_id)
        self.assertNotEqual(restored_id, deleted_id)
        self.assertEqual(
            self.dispatch("get_object", {"object_id": restored_id})["name"],
            "MCP_E2E_Cube",
        )
        stale = self.execute_task("get_object", {"object_id": deleted_id})
        self.assertEqual(stale["error"]["code"], "STALE_OBJECT_ID")

    def test_native_delete_undo_never_revives_old_id_for_the_same_atom(self):
        created = self.create(name="RestoredSameAtom")
        deleted_id = created["object"]["object_id"]
        deleted_wrapper = self.document.roots[0]

        self.dispatch(
            "delete_object", {"object_id": deleted_id, "recursive": False}
        )
        restored_wrapper = self.document.simulate_native_delete_undo()
        self.assertIs(restored_wrapper, deleted_wrapper)

        restored_summary = self.dispatch("list_objects")["objects"][0]
        restored_id = restored_summary["object_id"]
        self.assertTrue(restored_summary["addressable"])
        self.assertNotEqual(restored_id, deleted_id)
        self.assertEqual(
            self.dispatch("get_object", {"object_id": restored_id})["name"],
            "RestoredSameAtom",
        )
        stale = self.execute_task("get_object", {"object_id": deleted_id})
        self.assertEqual(stale["error"]["code"], "STALE_OBJECT_ID")

    def test_recursive_delete_retires_subtree_and_restore_gets_fresh_ids(self):
        cube = MutationFakeObject(0, "Cube", 10002, "Cube")
        sphere = MutationFakeObject(0, "Sphere", 10003, "Sphere")
        parent = MutationFakeObject(
            0,
            "Null",
            10001,
            "Null",
            children=[cube, sphere],
        )
        self.document = MutationFakeDocument([parent])
        before = self.dispatch("list_objects")["objects"]
        old_ids = {item["name"]: item["object_id"] for item in before}
        old_scopes = {
            self.plugin._parse_object_id(value)[1] for value in old_ids.values()
        }

        self.dispatch(
            "delete_object",
            {"object_id": old_ids["Null"], "recursive": True},
        )

        bucket = self.plugin._OBJECT_SCOPES._objects.get(SCOPE_A, {})
        self.assertTrue(old_scopes.isdisjoint(bucket))
        self.assertTrue(
            old_scopes.issubset(self.plugin._OBJECT_SCOPES._issued_scopes)
        )
        parent.liveness_error = True
        cube.liveness_error = True
        sphere.liveness_error = True
        restored_cube = MutationFakeObject(0, "Cube", 10002, "Cube")
        restored_sphere = MutationFakeObject(0, "Sphere", 10003, "Sphere")
        restored_parent = MutationFakeObject(
            0,
            "Null",
            10001,
            "Null",
            children=[restored_cube, restored_sphere],
        )
        self.document.simulate_native_delete_undo(restored_parent)

        after = self.dispatch("list_objects")["objects"]
        fresh_ids = {item["name"]: item["object_id"] for item in after}
        self.assertEqual([item["name"] for item in after], ["Null", "Cube", "Sphere"])
        for name, old_id in old_ids.items():
            with self.subTest(name=name):
                summary = next(item for item in after if item["name"] == name)
                self.assertTrue(summary["addressable"])
                self.assertNotEqual(fresh_ids[name], old_id)
                self.assertEqual(
                    self.dispatch(
                        "get_object",
                        {"object_id": fresh_ids[name]},
                    )["name"],
                    name,
                )
                stale = self.execute_task("get_object", {"object_id": old_id})
                self.assertEqual(stale["error"]["code"], "STALE_OBJECT_ID")

    def test_retired_history_does_not_poison_unrelated_object_identity(self):
        deleted_wrapper = MutationFakeObject(0, "Deleted", 10002, "Cube")
        unrelated = MutationFakeObject(0, "Unrelated", 10003, "Sphere")
        self.document = MutationFakeDocument([deleted_wrapper, unrelated])
        before = self.dispatch("list_objects")["objects"]
        ids = {item["name"]: item["object_id"] for item in before}

        self.dispatch(
            "delete_object",
            {"object_id": ids["Deleted"], "recursive": False},
        )
        deleted_wrapper.liveness_error = True
        restored = MutationFakeObject(0, "Deleted", 10002, "Cube")
        self.document.simulate_native_delete_undo(restored)

        after = self.dispatch("list_objects")["objects"]
        summaries = {item["name"]: item for item in after}
        self.assertTrue(summaries["Deleted"]["addressable"])
        self.assertTrue(summaries["Unrelated"]["addressable"])
        self.assertNotEqual(summaries["Deleted"]["object_id"], ids["Deleted"])
        self.assertEqual(summaries["Unrelated"]["object_id"], ids["Unrelated"])

    def test_delete_retires_before_end_undo_and_failure_clears_bindings(self):
        created = self.create()
        deleted_id = created["object"]["object_id"]
        original_retire = self.plugin._OBJECT_SCOPES.retire_scopes

        def retire_with_log(*args):
            self.document.log.append("RetireScopes")
            return original_retire(*args)

        self.document.log.clear()
        with patch.object(
            self.plugin._OBJECT_SCOPES,
            "retire_scopes",
            side_effect=retire_with_log,
        ):
            self.dispatch(
                "delete_object",
                {"object_id": deleted_id, "recursive": False},
            )
        order = ["Remove", "RetireScopes", "EndUndo"]
        self.assertEqual(
            [self.document.log.index(item) for item in order],
            sorted(self.document.log.index(item) for item in order),
        )

        self.setUp()
        created = self.create()
        deleted_id = created["object"]["object_id"]
        with patch.object(
            self.plugin._OBJECT_SCOPES,
            "retire_scopes",
            return_value=False,
        ):
            response = self.execute_task(
                "delete_object",
                {"object_id": deleted_id, "recursive": False},
            )
        self.assertEqual(response["error"]["code"], "OUTCOME_UNKNOWN")
        self.assertFalse(response["error"]["retryable"])
        self.assertEqual(self.document.roots, [])
        self.assertNotIn(SCOPE_A, self.plugin._OBJECT_SCOPES._objects)
        stale = self.execute_task("get_object", {"object_id": deleted_id})
        self.assertEqual(stale["error"]["code"], "STALE_OBJECT_ID")

    def test_delete_end_undo_failure_still_retires_old_scope(self):
        created = self.create()
        deleted_id = created["object"]["object_id"]
        self.document.end_undo_result = False

        response = self.execute_task(
            "delete_object",
            {"object_id": deleted_id, "recursive": False},
        )

        self.assertEqual(response["error"]["code"], "OUTCOME_UNKNOWN")
        self.assertFalse(response["error"]["retryable"])
        self.assertEqual(self.document.roots, [])
        stale = self.execute_task("get_object", {"object_id": deleted_id})
        self.assertEqual(stale["error"]["code"], "STALE_OBJECT_ID")

    def test_delete_parent_requires_recursive_and_preserves_scene_on_guard(self):
        child = MutationFakeObject(0, "Child", 20002, "Cube")
        parent = MutationFakeObject(0, "Parent", 20001, "Null", children=[child])
        self.document = MutationFakeDocument([parent])
        parent_id = self.dispatch("list_objects")["objects"][0]["object_id"]

        guarded = self.execute_task(
            "delete_object", {"object_id": parent_id, "recursive": False}
        )

        self.assertEqual(guarded["error"]["code"], "OBJECT_HAS_CHILDREN")
        self.assertEqual(self.document.roots, [parent])
        self.plugin.c4d.StopAllThreads.assert_not_called()

        deleted = self.dispatch(
            "delete_object", {"object_id": parent_id, "recursive": True}
        )
        self.assertTrue(deleted["recursive"])
        self.assertEqual(
            set(deleted),
            {"deleted_object_id", "deleted_object", "recursive"},
        )
        self.assertNotIn("mutation_id", deleted)
        self.assertNotIn("undo_available", deleted)
        self.assertEqual(self.document.roots, [])
        delete_order = (
            "StartUndo",
            ("AddUndo", "DELETE"),
            "Remove",
            "EndUndo",
        )
        indexes = [self.document.log.index(item) for item in delete_order]
        self.assertEqual(indexes, sorted(indexes))

    def test_delete_add_undo_failure_does_not_remove_object(self):
        created = self.create()
        self.document.add_undo_result = False

        response = self.execute_task(
            "delete_object",
            {"object_id": created["object"]["object_id"], "recursive": False},
        )

        self.assertEqual(response["error"]["code"], "MUTATION_FAILED")
        self.assertEqual(len(self.document.roots), 1)
        self.assertNotIn("Remove", self.document.log)

    def test_mutations_refuse_all_c4d_access_off_main_thread(self):
        self.plugin.c4d.threading.GeIsMainThread = lambda: False
        for command, params in (
            ("create_object", {"type": "cube"}),
            ("update_object", {"object_id": object_id(1), "name": "x"}),
            ("delete_object", {"object_id": object_id(1)}),
        ):
            with self.subTest(command=command):
                with self.assertRaisesRegex(RuntimeError, "main thread"):
                    self.make_server()._dispatch_on_main_thread(command, params)
        self.plugin.c4d.StopAllThreads.assert_not_called()

    def test_queued_and_running_write_timeouts_have_distinct_outcomes(self):
        queued_server = self.make_server(main_thread_timeout=0.02)
        queued_server._dispatch_on_main_thread = MagicMock()
        queued = queued_server.execute_on_main_thread(
            "create_object", {"type": "cube"}, "queued-write"
        )
        self.assertEqual(queued["error"]["code"], "MAIN_THREAD_TIMEOUT")
        _, late = queued_server.msg_queue.get_nowait()
        self.assertFalse(late())
        queued_server._dispatch_on_main_thread.assert_not_called()

        running_server = self.make_server(main_thread_timeout=0.02)
        release = threading.Event()
        running_server._dispatch_on_main_thread = lambda *args: release.wait(1.0) or {}
        response = {}
        worker = threading.Thread(
            target=lambda: response.setdefault(
                "value",
                running_server.execute_on_main_thread(
                    "create_object", {"type": "cube"}, "running-write"
                ),
            )
        )
        worker.start()
        _, callback = running_server.msg_queue.get(timeout=0.5)
        callback_worker = threading.Thread(target=callback)
        callback_worker.start()
        worker.join(timeout=0.5)
        self.assertEqual(response["value"]["error"]["code"], "OUTCOME_UNKNOWN")
        self.assertFalse(response["value"]["error"]["retryable"])
        release.set()
        callback_worker.join(timeout=0.5)


class Phase2BExternalTransportTests(unittest.TestCase):
    def test_mcp_1281_surface_and_mutation_schemas_are_exact(self):
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
            set(external_server.mcp._tool_manager._tools), set(expected)
        )
        create_schema = external_server.mcp._tool_manager.get_tool(
            "create_object"
        ).parameters
        self.assertEqual(create_schema["required"], ["type"])
        self.assertEqual(
            create_schema["properties"]["type"]["enum"],
            ["null", "cube", "sphere", "plane", "cylinder", "cone"],
        )
        position = create_schema["properties"]["position"]
        self.assertEqual(position["type"], "array")
        self.assertEqual(position["minItems"], 3)
        self.assertEqual(position["maxItems"], 3)
        self.assertEqual(create_schema["properties"]["name"]["type"], "string")
        self.assertEqual(
            create_schema["properties"]["name"]["maxLength"], 255
        )
        delete_schema = external_server.mcp._tool_manager.get_tool(
            "delete_object"
        ).parameters
        self.assertEqual(
            delete_schema["properties"]["recursive"]["type"], "boolean"
        )
        self.assertEqual(
            delete_schema["properties"]["object_id"]["pattern"],
            r"^c4d:[0-9a-f]{32}:[0-9a-f]{32}$",
        )
        self.assertNotIn("undo_last", external_server.mcp._tool_manager._tools)

    def test_external_validation_rejects_mutation_params_before_connect(self):
        invalid = (
            ("create_object", {"type": 5159}, "UNSUPPORTED_OBJECT_TYPE"),
            ("create_object", {"type": "torus"}, "UNSUPPORTED_OBJECT_TYPE"),
            ("create_object", {"type": "cube", "position": [True, 2, 3]}, "INVALID_PARAMS"),
            ("create_object", {"type": "cube", "rotation_deg": [1, 2]}, "INVALID_PARAMS"),
            ("create_object", {"type": "cube", "scale": [1, math.inf, 1]}, "INVALID_PARAMS"),
            ("create_object", {"type": "cube", "scale": [1, 10 ** 1000, 1]}, "INVALID_PARAMS"),
            ("create_object", {"type": "cube", "name": None}, "INVALID_PARAMS"),
            ("create_object", {"type": "cube", "raw_type_id": 5159}, "INVALID_PARAMS"),
            ("update_object", {"object_id": object_id(1)}, "INVALID_PARAMS"),
            ("update_object", {"object_id": object_id(1), "name": None}, "INVALID_PARAMS"),
            ("update_object", {"object_id": object_id(1), "parameter": 1}, "INVALID_PARAMS"),
            ("delete_object", {"object_id": object_id(1), "recursive": 1}, "INVALID_PARAMS"),
        )
        with patch("cinema4d_mcp.server.socket.create_connection") as connect:
            for command, params, code in invalid:
                with self.subTest(command=command, params=params):
                    response = external_server.send_to_c4d(
                        command,
                        token=TOKEN,
                        request_id="phase2b-external",
                        params=params,
                    )
                    self.assertEqual(response["error"]["code"], code)
        connect.assert_not_called()

    def test_forbidden_future_persistence_and_renderer_commands_remain_unknown(self):
        with patch("cinema4d_mcp.server.socket.create_connection") as connect:
            for command in (
                "undo_last",
                "save_as",
                "save_project",
                "execute_python",
                "octane_command",
                "redshift_command",
            ):
                with self.subTest(command=command):
                    response = external_server.send_to_c4d(
                        command,
                        token=TOKEN,
                        request_id="forbidden-command",
                    )
                    self.assertEqual(response["error"]["code"], "UNKNOWN_COMMAND")
        connect.assert_not_called()

    def test_fastmcp_raw_arguments_use_the_same_strict_validation(self):
        invalid = (
            ("create_object", {"type": 5159}, "UNSUPPORTED_OBJECT_TYPE"),
            ("create_object", {"type": "cube", "position": [True, 2, 3]}, "INVALID_PARAMS"),
            ("update_object", {"object_id": object_id(1)}, "INVALID_PARAMS"),
            ("delete_object", {"object_id": object_id(1), "recursive": 1}, "INVALID_PARAMS"),
        )
        with patch("cinema4d_mcp.server.socket.create_connection") as connect:
            for name, arguments, code in invalid:
                with self.subTest(name=name):
                    response = asyncio.run(
                        external_server.mcp.call_tool(name, arguments)
                    )
                    self.assertEqual(response["error"]["code"], code)
        connect.assert_not_called()

    def _write_transport_failure(self, recv_effect):
        bridge_socket = MagicMock()
        if isinstance(recv_effect, bytes):
            bridge_socket.recv.return_value = recv_effect
        else:
            bridge_socket.recv.side_effect = recv_effect
        with patch(
            "cinema4d_mcp.server.socket.create_connection",
            return_value=bridge_socket,
        ) as connect:
            response = external_server.send_to_c4d(
                "create_object",
                token=TOKEN,
                request_id="write-failure",
                params={"type": "cube"},
            )
        connect.assert_called_once()
        bridge_socket.sendall.assert_called_once()
        return response

    def test_post_send_write_timeout_reset_and_eof_are_outcome_unknown(self):
        for failure in (socket.timeout(), ConnectionResetError(), b""):
            with self.subTest(failure=type(failure).__name__):
                response = self._write_transport_failure(failure)
                self.assertEqual(response["error"]["code"], "OUTCOME_UNKNOWN")
                self.assertFalse(response["error"]["retryable"])

    def test_sendall_timeout_is_outcome_unknown_and_never_retried(self):
        bridge_socket = MagicMock()
        bridge_socket.sendall.side_effect = socket.timeout()
        with patch(
            "cinema4d_mcp.server.socket.create_connection",
            return_value=bridge_socket,
        ) as connect:
            response = external_server.send_to_c4d(
                "create_object",
                token=TOKEN,
                request_id="send-timeout",
                params={"type": "cube"},
            )

        connect.assert_called_once()
        bridge_socket.sendall.assert_called_once()
        bridge_socket.recv.assert_not_called()
        self.assertEqual(response["error"]["code"], "OUTCOME_UNKNOWN")
        self.assertFalse(response["error"]["retryable"])

    def test_write_connect_failure_is_retryable_before_delivery(self):
        with patch(
            "cinema4d_mcp.server.socket.create_connection",
            side_effect=ConnectionRefusedError(),
        ) as connect:
            response = external_server.send_to_c4d(
                "create_object",
                token=TOKEN,
                request_id="connect-failure",
                params={"type": "cube"},
            )

        connect.assert_called_once()
        self.assertEqual(response["error"]["code"], "C4D_UNAVAILABLE")
        self.assertTrue(response["error"]["retryable"])

    def test_read_timeout_behavior_remains_retryable(self):
        bridge_socket = MagicMock()
        bridge_socket.recv.side_effect = socket.timeout()
        with patch(
            "cinema4d_mcp.server.socket.create_connection",
            return_value=bridge_socket,
        ):
            response = external_server.send_to_c4d(
                "ping", token=TOKEN, request_id="read-timeout"
            )

        self.assertEqual(response["error"]["code"], "C4D_TIMEOUT")
        self.assertTrue(response["error"]["retryable"])


if __name__ == "__main__":
    unittest.main()
