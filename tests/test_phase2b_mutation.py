"""Phase 2B typed mutation, guarded undo, and write-timeout contracts."""

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
    DEFAULT_EQUALITY,
    FakeDocument,
    FakeObject,
    FakeVector,
    SCOPE_A,
    SCOPE_B,
    link_hierarchy,
    object_id,
    object_scope,
)


def mutation_id(value):
    return "mut:{:032x}".format(value)


class FakeUndoAnchor:
    def __init__(self, atom_key=None):
        self.atom_key = atom_key if atom_key is not None else object()
        self.alive = True
        self.equality_error = False
        self.equality_result = DEFAULT_EQUALITY

    def IsAlive(self):
        return self.alive

    def __eq__(self, other):
        if self.equality_error:
            raise RuntimeError("undo anchor equality failed")
        if self.equality_result is not DEFAULT_EQUALITY:
            return self.equality_result
        return (
            isinstance(other, FakeUndoAnchor)
            and self.atom_key == other.atom_key
        )

    def __ne__(self, other):
        return not self.__eq__(other)


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
    NO_MANUAL_ANCHOR = object()

    def __init__(self, roots=None, **kwargs):
        super().__init__(roots, **kwargs)
        self.log = []
        self.start_undo_result = True
        self.add_undo_result = True
        self.end_undo_result = True
        self.do_undo_result = True
        self.pending_undo = None
        self.undo_stack = []
        self.manual_undo_anchor = self.NO_MANUAL_ANCHOR
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
            "anchor": FakeUndoAnchor(),
        }
        return True

    def EndUndo(self):
        self.log.append("EndUndo")
        if self.end_undo_result and self.pending_undo is not None:
            self.undo_stack.append(self.pending_undo)
            self.pending_undo = None
        return self.end_undo_result

    def GetUndoPtr(self):
        self.log.append("GetUndoPtr")
        if self.manual_undo_anchor is not self.NO_MANUAL_ANCHOR:
            return self.manual_undo_anchor
        return self.undo_stack[-1]["anchor"] if self.undo_stack else None

    def DoUndo(self):
        self.log.append("DoUndo")
        if not self.do_undo_result:
            return False
        if not self.undo_stack:
            return False
        record = self.undo_stack.pop()
        obj = record["object"]
        if record["type"] == "NEW":
            self.remove_object(obj)
        elif record["type"] == "CHANGE":
            snapshot = record["snapshot"]
            obj.name = snapshot["name"]
            obj.position = snapshot["position"]
            obj.rotation = snapshot["rotation"]
            obj.scale = snapshot["scale"]
        elif record["type"] == "DELETE":
            snapshot = record["snapshot"]
            parent = snapshot["parent"]
            siblings = self.roots if parent is None else parent.children
            siblings.insert(snapshot["index"], obj)
            self._attach_all()
        return True


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
        mutation_tokens = iter("{:032x}".format(index) for index in range(1001, 4096))
        self.plugin._MUTATION_LEDGER = self.plugin._MutationLedger(
            token_factory=lambda: next(mutation_tokens)
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

    def test_plugin_surface_is_exactly_nine_tools(self):
        expected = (
            "ping",
            "get_capabilities",
            "get_scene_info",
            "list_objects",
            "get_object",
            "create_object",
            "update_object",
            "delete_object",
            "undo_last",
        )
        self.assertEqual(self.plugin.ACTIVE_COMMAND_NAMES, expected)
        self.assertEqual(self.plugin.ALLOWED_COMMANDS, frozenset(expected))
        self.assertEqual(
            self.plugin.WRITE_COMMAND_NAMES,
            frozenset(("create_object", "update_object", "delete_object", "undo_last")),
        )
        for forbidden in (
            "save_document",
            "execute_python",
            "octane_command",
            "redshift_command",
        ):
            self.assertNotIn(forbidden, self.plugin.ALLOWED_COMMANDS)

    def test_each_allowlisted_creation_type_is_top_level_and_addressable(self):
        expected_types = ("null", "cube", "sphere", "plane", "cylinder", "cone")
        for object_type in expected_types:
            with self.subTest(object_type=object_type):
                self.setUp()
                result = self.dispatch("create_object", {"type": object_type})
                created = self.document.roots[0]

                self.assertIsNone(created.parent)
                self.assertTrue(self.plugin._is_valid_object_id(result["object"]["object_id"]))
                self.assertTrue(self.plugin._is_valid_mutation_id(result["mutation_id"]))
                self.assertEqual(created.guid_reads, 0)
                self.assertEqual(
                    self.document.log.index("InsertObject")
                    < self.document.log.index(("AddUndo", "NEW")),
                    True,
                )
                self.assertEqual(self.document.log[-1], "GetUndoPtr")

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
            ("undo_last", {"mutation_id": mutation_id(1)}),
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
            ("undo_last", {"mutation_id": "mut:bad"}),
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
        self.assertEqual(self.document.log.count(("AddUndo", "CHANGE")), 1)
        add_index = self.document.log.index(("AddUndo", "CHANGE"))
        for setter in ("SetName", "SetRelPos", "SetRelRot", "SetRelScale"):
            self.assertGreater(self.document.log.index(setter), add_index)

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
        deleted = self.dispatch(
            "delete_object", {"object_id": deleted_id, "recursive": False}
        )

        self.assertEqual(deleted["deleted_object_id"], deleted_id)
        self.assertEqual(self.document.roots, [])
        stale = self.execute_task("get_object", {"object_id": deleted_id})
        self.assertEqual(stale["error"]["code"], "OBJECT_NOT_IN_DOCUMENT")
        replacement = self.create()
        self.assertNotEqual(replacement["object"]["object_id"], deleted_id)

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
        self.assertEqual(self.document.roots, [])
        self.assertLess(
            self.document.log.index(("AddUndo", "DELETE")),
            self.document.log.index("Remove"),
        )

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

    def test_create_update_delete_each_undo_through_verified_anchor(self):
        created = self.create(name="Created")
        create_mutation = created["mutation_id"]
        create_undo = self.dispatch("undo_last", {"mutation_id": create_mutation})
        self.assertEqual(create_undo["operation"], "create_object")
        self.assertEqual(self.document.roots, [])

        created = self.create(name="Before")
        object_id_value = created["object"]["object_id"]
        updated = self.dispatch(
            "update_object", {"object_id": object_id_value, "name": "After"}
        )
        update_undo = self.dispatch(
            "undo_last", {"mutation_id": updated["mutation_id"]}
        )
        self.assertEqual(update_undo["operation"], "update_object")
        self.assertEqual(self.document.roots[0].name, "Before")

        deleted = self.dispatch(
            "delete_object", {"object_id": object_id_value, "recursive": False}
        )
        delete_undo = self.dispatch(
            "undo_last", {"mutation_id": deleted["mutation_id"]}
        )
        self.assertEqual(delete_undo["operation"], "delete_object")
        self.assertEqual(self.document.roots[0].name, "Before")

    def test_undo_rejects_wrong_non_top_and_cross_document_mutation_ids(self):
        first = self.create()
        second = self.create()
        non_top = self.execute_task(
            "undo_last", {"mutation_id": first["mutation_id"]}
        )
        self.assertEqual(non_top["error"]["code"], "UNDO_STATE_MISMATCH")

        unknown = self.execute_task(
            "undo_last", {"mutation_id": mutation_id(9999)}
        )
        self.assertEqual(unknown["error"]["code"], "UNDO_NOT_AVAILABLE")

        self.document = MutationFakeDocument()
        mismatch = self.execute_task(
            "undo_last", {"mutation_id": second["mutation_id"]}
        )
        self.assertEqual(mismatch["error"]["code"], "DOCUMENT_MISMATCH")

    def test_multiple_sequential_mcp_undos_verify_each_exposed_anchor(self):
        first = self.create(name="First")
        second = self.create(name="Second")

        second_undo = self.dispatch(
            "undo_last", {"mutation_id": second["mutation_id"]}
        )
        first_undo = self.dispatch(
            "undo_last", {"mutation_id": first["mutation_id"]}
        )

        self.assertEqual(second_undo["operation"], "create_object")
        self.assertTrue(second_undo["undo_available"])
        self.assertEqual(first_undo["operation"], "create_object")
        self.assertFalse(first_undo["undo_available"])
        self.assertEqual(self.document.roots, [])

    def test_manual_or_unverifiable_undo_anchor_never_calls_do_undo(self):
        for mode in ("different", "raises", "non_bool"):
            with self.subTest(mode=mode):
                self.setUp()
                created = self.create()
                stored = self.document.undo_stack[-1]["anchor"]
                if mode == "different":
                    self.document.manual_undo_anchor = FakeUndoAnchor()
                elif mode == "raises":
                    stored.equality_error = True
                else:
                    stored.equality_result = 1

                response = self.execute_task(
                    "undo_last", {"mutation_id": created["mutation_id"]}
                )

                self.assertEqual(response["error"]["code"], "UNDO_STATE_MISMATCH")
                self.assertNotIn("DoUndo", self.document.log)

    def test_do_undo_false_is_structured_and_ledger_is_not_popped(self):
        created = self.create()
        self.document.do_undo_result = False

        response = self.execute_task(
            "undo_last", {"mutation_id": created["mutation_id"]}
        )

        self.assertEqual(response["error"]["code"], "UNDO_FAILED")
        entry, status = self.plugin._MUTATION_LEDGER.requested_top(
            SCOPE_A, created["mutation_id"]
        )
        self.assertEqual(status, "top")
        self.assertIsNotNone(entry)

    def test_mutations_refuse_all_c4d_access_off_main_thread(self):
        self.plugin.c4d.threading.GeIsMainThread = lambda: False
        for command, params in (
            ("create_object", {"type": "cube"}),
            ("update_object", {"object_id": object_id(1), "name": "x"}),
            ("delete_object", {"object_id": object_id(1)}),
            ("undo_last", {"mutation_id": mutation_id(1)}),
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
            "undo_last",
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
        undo_schema = external_server.mcp._tool_manager.get_tool(
            "undo_last"
        ).parameters
        self.assertEqual(
            undo_schema["properties"]["mutation_id"]["pattern"],
            r"^mut:[0-9a-f]{32}$",
        )

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
            ("undo_last", {"mutation_id": "mut:bad"}, "INVALID_PARAMS"),
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

    def test_forbidden_phase2c_and_renderer_commands_remain_unknown(self):
        with patch("cinema4d_mcp.server.socket.create_connection") as connect:
            for command in (
                "save_document",
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
