# Cinema 4D MCP Phase 3A Hierarchy Contract

## 1. Status

```text
Phase 3A.0 CONTRACT DRAFT
Implementation: NOT STARTED
Certification: NOT STARTED
```

This document is a design and approval artifact only. It does not authorize or
contain production implementation changes.

Phase 3A is divided into three independently implemented and certified
subphases:

1. Phase 3A.1: `set_active_object`
2. Phase 3A.2: `reparent_object`
3. Phase 3A.3: `duplicate_object`

Each subphase requires its own implementation, automated regression suite,
Cinema 4D 2023.2.2 certification, Codex App E2E certification, certification
record, default-branch merge, and certification tag. The tools must not be
implemented or released as one batch.

## 2. Certified Phase 2 baseline

Phase 3A is based on the Phase 2 certification commit and tag:

```text
Baseline commit: 7fac5a2c2169a271facdd3f55a02704f8dcfb7df
Certification tag: phase2-certified
Runtime: 0.4.0-phase2c
External tool count: 9
```

The certified Phase 2 tool surface is:

```text
ping
get_capabilities
get_scene_info
list_objects
get_object
create_object
update_object
delete_object
save_document
```

The following certified architecture and safety boundaries remain normative:

- FastMCP communicates with the C4D plugin through the authenticated
  `127.0.0.1` TCP bridge.
- `C4D_MCP_TOKEN`, `C4D_MCP_PORT`, protocol validation, structured errors, the
  64 KiB frame limit, and fail-closed startup remain unchanged.
- All C4D APIs execute through the existing main-thread path:
  socket worker → queue → `c4d.SpecialEventAdd()` →
  `GeDialog.CoreMessage()` → main-thread dispatcher.
- Object IDs remain process-local opaque values backed by process-local
  document and object scopes, C4DAtom equality, `IsAlive()`, and active
  hierarchy revalidation.
- Names, hierarchy paths, raw `GetGUID()` values, memory addresses, tags, user
  data, and hidden persistent markers are not identity sources.
- Cross-document lookups fail closed. Stale IDs never alias another object.
- Only the actual Object Manager hierarchy traversed with
  `GetFirstObject()`/`GetDown()`/`GetNext()` is addressable. Generated,
  deform, and cache objects remain excluded.
- Arbitrary Python, arbitrary numeric object types, arbitrary `DescID`
  parameter access, renderer commands, and remote transports remain
  unavailable.
- Writes are never retried automatically. A delivered or running write whose
  result cannot be proven returns `OUTCOME_UNKNOWN` with `retryable=false`.
- Native C4D undo transactions remain available for supported scene
  mutations. Remote MCP undo remains unavailable.
- An object ID retired by MCP deletion is never reused or resurrected. If
  native Undo restores the object, the restored hierarchy receives fresh
  process-local IDs.
- The plugin command allowlist and external FastMCP tool registry must remain
  exact mirrors at every released subphase.

## 3. Phase 3A goals

Phase 3A extends safe hierarchy interaction in three deliberately small steps:

- Select exactly one addressable object in the active document.
- Move an addressable object within the active document hierarchy.
- Duplicate an addressable object and its real hierarchy subtree within the
  active document.

The design reuses the certified Phase 2 identity resolver, document scope
registry, object scope registry, main-thread dispatcher, native undo
transactions, transport uncertainty rules, and exact allowlists.

Official Cinema 4D 2023.2 APIs reviewed for this contract:

- [`BaseDocument`](https://developers.maxon.net/docs/py/2023_2/modules/c4d.documents/BaseDocument/index.html):
  `SetActiveObject()`, `GetActiveObject()`, `InsertObject()`, `StartUndo()`,
  `AddUndo()`, and `EndUndo()`.
- [`C4DAtom`](https://developers.maxon.net/docs/py/2023_2/modules/c4d/C4DAtom/index.html):
  `GetClone()`, C4DAtom equality, and `IsAlive()`.
- [`AliasTrans`](https://developers.maxon.net/docs/py/2023_2/modules/c4d/AliasTrans/index.html):
  `Init()` and `Translate()`.
- [`GeListNode`](https://developers.maxon.net/docs/py/2023_2/modules/c4d/C4DAtom/GeListNode/index.html):
  `GetUp()`, `GetDown()`, `GetNext()`, `GetDownLast()`, `InsertAfter()`,
  `InsertUnderLast()`, `Remove()`, and `GetDocument()`.
- [`BaseObject`](https://developers.maxon.net/docs/py/2023_2/modules/c4d/C4DAtom/GeListNode/BaseList2D/BaseObject/index.html):
  `GetMg()` and `SetMg()`.
- [`c4d.threading`](https://developers.maxon.net/docs/py/2023_2/modules/c4d.threading/):
  `GeIsMainThread()` and `GeIsMainThreadAndNoDrawThread()`.
- [`c4d`](https://developers.maxon.net/docs/py/2023_2/modules/c4d/index.html):
  `StopAllThreads()`, `EventAdd()`, and `SpecialEventAdd()`.
- [Cinema 4D 2023.2 threading manual](https://developers.maxon.net/docs/cpp/2023_2/page_manual_cinemathreads.html):
  active-document edits from asynchronous UI code must be performed on the
  main thread after stopping running scene threads.

The Python SDK documentation is versioned as 2023.2. API presence in those
documents is not by itself a claim of production compatibility. Each API
sequence below must still pass real Cinema 4D 2023.2.2 certification.

Official API decision matrix:

| Candidate | Official methods and call targets | Main-thread and event contract | Native undo contract | Hierarchy/clone meaning | Document-membership contract | 2023.2.2 status |
| --- | --- | --- | --- | --- | --- | --- |
| `set_active_object` | `BaseDocument.SetActiveObject(obj, SELECTION_NEW)` and `BaseDocument.GetActiveObject()` on the active `BaseDocument` | Existing main-thread dispatcher; `StopAllThreads()` before modifying user state; `EventAdd()` after completion | `StartUndo()`/`EndUndo()`; activation/deactivation records are automatically managed, so no manual activate/deactivate `AddUndo()` | Replaces object selection only; no hierarchy or clone operation | Existing document scope and current hierarchy resolver; the resolved atom must already be in the active document | APIs documented in 2023.2; exact native selection-undo behavior awaits 2023.2.2 certification |
| `reparent_object` | `BaseObject.GetMg()`/`SetMg()`; inherited `GetUp()`, `Remove()`, `InsertUnderLast()`; `BaseDocument.InsertObject()` | Existing main-thread dispatcher; `StopAllThreads()`; one `EventAdd()` after verified completion | `StartUndo()` → `AddUndo(UNDOTYPE_HIERARCHY_PSR, obj)` before mutation → `EndUndo()` | Non-null parent means last child; null means top of root; world matrix is preserved | Both IDs resolved in one current active-document DFS snapshot; optional `GetDocument()` result is compared by C4DAtom equality only | APIs documented in 2023.2; transform and sibling restoration await 2023.2.2 certification |
| `duplicate_object` | `AliasTrans.Init(doc)`; `C4DAtom.GetClone(COPYFLAGS_NONE, trans)`; `AliasTrans.Translate(True)`; `GeListNode.InsertAfter(source)` | Clone, traversal, insertion, registry binding, and `EventAdd()` all on the existing main-thread dispatcher after `StopAllThreads()` | `StartUndo()` → insert → `AddUndo(UNDOTYPE_NEWOBJ, clone)` after insertion → `EndUndo()` | Clone the real source hierarchy and branches; insert root directly after source; no generated/cache clone surface | Source resolves in current active document; `AliasTrans` is initialized for that document; no cross-document destination | APIs documented in 2023.2; link, branch, plugin-data, and native Undo behavior await 2023.2.2 certification |

## 4. Intentional exclusions

Phase 3A does not add:

```text
multi-selection
selection add/toggle/remove
selection by name
selection by hierarchy path
selection of generated/cache objects
selection of materials or tags
sibling index control
insert-before or insert-after input
cross-document reparent
cross-document duplicate
destination parent input for duplicate
instance or reference creation
material deep copy
animation-specific clone options
save_as
save_project
load/close document
remote MCP undo
arbitrary object type IDs
arbitrary DescID or parameter editing
arbitrary Python
Octane or Redshift control
remote transport
```

The design also does not introduce persistent IDs into `.c4d` files. It does
not add tags, user data, hidden markers, or name-based fingerprints.

## 5. Proposed subphases

### Phase 3A.1: `set_active_object`

Add one replace-only selection tool. Complete all automated, real C4D, and
Codex App gates before Phase 3A.2 begins.

### Phase 3A.2: `reparent_object`

Add one same-document hierarchy-move tool. Preserve the Phase 3A.1 surface and
certification. Complete all gates before Phase 3A.3 begins.

### Phase 3A.3: `duplicate_object`

Add one same-document hierarchy-clone tool. Preserve all earlier surfaces and
certifications.

Candidate runtime labels, following the repository's Phase 2 minor-version
progression, are:

```text
Phase 3A.1: 0.5.0-phase3a1
Phase 3A.2: 0.5.0-phase3a2
Phase 3A.3: 0.5.0-phase3a3
```

These are proposals only. Phase 3A.0 does not change any runtime version.

## 6. `set_active_object` contract

### 6.1 Request

```json
{
  "object_id": "c4d:<32 lowercase hex document scope>:<32 lowercase hex object scope>"
}
```

`object_id` is required. Extra fields are rejected as `INVALID_PARAMS`.
`null`, an empty string, a name, and a hierarchy path are not valid object
identifiers.

There is no selection-clear operation in Phase 3A.1. The request always
replaces the current object selection with exactly one object.

### 6.2 Resolution and document ownership

The tool must reuse the exact Phase 2A.3 resolver:

1. Parse the canonical opaque object ID.
2. Validate the document scope against the active document registry.
3. Reject stale, cross-document, dead, detached, generated, cache, and
   unverified objects.
4. Walk the active document's real hierarchy.
5. Require exactly one C4DAtom-equal current hierarchy wrapper.
6. Operate on that current hierarchy wrapper, not a stored registry wrapper.

`BaseList2D.GetDocument()` may be used only as an additional documented
diagnostic. If used, its result must be compared with the active document by
C4DAtom equality. Python `is`, `id()`, document names, and document paths are
not identity checks.

### 6.3 Selection semantics

The normative API is:

```python
doc.SetActiveObject(obj, c4d.SELECTION_NEW)
```

This is a replace-only selection. It is a UI selection side effect, not a
content or hierarchy mutation. It is nevertheless a write command because the
request changes C4D state and its delivery can be ambiguous.

The official SDK states that activation/deactivation undo records are
automatically managed by `SetActiveObject()` and related APIs. The
implementation must not manually call `AddUndo()` with
`UNDOTYPE_ACTIVATE` or `UNDOTYPE_DEACTIVATE`.

Proposed main-thread sequence:

```text
resolve and validate
StopAllThreads
StartUndo
SetActiveObject(target, SELECTION_NEW)
GetActiveObject readback
EndUndo
EventAdd
```

`GetActiveObject()` must be called after `SetActiveObject()` as required by the
2023.2 SDK note. The returned atom must compare as actual `bool True` to the
resolved target. An exception, non-bool equality result, `False`, or a
different active object after selection began makes the result
`OUTCOME_UNKNOWN`, `retryable=false`.

`StartUndo()` and `EndUndo()` must return actual `bool True`. Real C4D
certification must prove that one native UI Undo restores the prior object
selection. If Cinema 4D 2023.2.2 does not produce a safe single native undo
step with this documented sequence, Phase 3A.1 must stop for contract review;
private undo types or remote `DoUndo()` are not alternatives.

`EventAdd()` is called once after the completed selection transaction so the
Object Manager and dependent UI update. A later `get_scene_info` call is the
external readback source of truth and must report exactly the selected
addressable object ID in `active_object_ids`.

### 6.4 Response

```json
{
  "object_id": "c4d:<document scope>:<object scope>",
  "active": true,
  "selection_mode": "replace"
}
```

No new object or document scope is issued. The target ID remains unchanged.

### 6.5 Timeout contract

`set_active_object` is added to both write-name sets. It uses the certified
ordinary write timeout:

- Still queued at the main-thread start deadline: cancel the task and return
  `MAIN_THREAD_TIMEOUT`, `retryable=false`; selection did not start.
- Started or possibly delivered, but response is lost or late: return
  `OUTCOME_UNKNOWN`, `retryable=false`.
- Never retry automatically and never open a second connection.

## 7. `reparent_object` contract

### 7.1 Request

```json
{
  "object_id": "c4d:<document scope>:<object scope>",
  "parent_id": "c4d:<document scope>:<object scope>"
}
```

To move the object to the document root:

```json
{
  "object_id": "c4d:<document scope>:<object scope>",
  "parent_id": null
}
```

Both fields are required. `parent_id=null` is the only top-level sentinel.
Missing fields, extra fields, names, paths, empty strings, and malformed IDs
are `INVALID_PARAMS`.

Phase 3A.2 has no sibling index and no before/after parameter.

### 7.2 Resolution and structural validation

Before starting an undo transaction, the dispatcher must:

1. Resolve `object_id` through the certified Phase 2A.3 path.
2. Resolve non-null `parent_id` through the same path and the same active
   document hierarchy snapshot.
3. Require both objects to belong to the same current active document scope.
4. Reject target/parent C4DAtom equality with `SAME_OBJECT`.
5. Reject a requested parent found anywhere in the target's current DFS
   subtree with `CYCLE_DETECTED`.
6. Fail closed on any equality, liveness, membership, or hierarchy ambiguity.
7. Capture the current parent and the target world matrix with `GetMg()`.

Cycle detection uses the already verified deterministic DFS hierarchy and
C4DAtom equality. It does not rely on names, paths, raw GUIDs, or an ambiguous
interpretation of `SearchHierarchy()`.

If the requested parent is already the current parent, including
`parent_id=null` for an existing root object, the operation is a verified
no-op. It returns `changed=false` without opening a native undo transaction,
calling `EventAdd()`, or changing sibling order.

### 7.3 Hierarchy and transform semantics

For a non-null parent, the moved target becomes the last direct child:

```python
obj.InsertUnderLast(parent)
```

For `parent_id=null`, the object is inserted at the top of the root hierarchy:

```python
doc.InsertObject(obj)
```

This top-root/last-child placement is deterministic. Phase 3A.2 does not
preserve the previous sibling index and does not expose sibling positioning.

The operation preserves the target's world transform:

```text
world_matrix = obj.GetMg()
remove and insert
obj.SetMg(world_matrix)
```

Local/relative PSR may change because the parent coordinate system changes.
The target's descendants move with the target and remain in the same relative
subtree.

All current object scopes in the moved subtree remain bound to the same live
underlying C4DAtoms. Reparenting does not retire or replace them. The target,
descendant, and unrelated object IDs therefore remain unchanged within the
same live document session.

### 7.4 Native undo sequence

The documented hierarchy-aware undo type is
`c4d.UNDOTYPE_HIERARCHY_PSR`. The proposed main-thread sequence is:

```text
resolve and validate complete hierarchy snapshot
capture world matrix
StopAllThreads
StartUndo
AddUndo(UNDOTYPE_HIERARCHY_PSR, target) before mutation
Remove
InsertUnderLast(parent) or InsertObject(target)
SetMg(captured world matrix)
EndUndo
re-resolve and verify parent, identity, and world transform
EventAdd
```

`StartUndo()`, `AddUndo()`, and `EndUndo()` must return actual `bool True`.
All validation possible without mutation occurs before `StartUndo()`.

The postcondition requires:

- Exactly one current hierarchy wrapper matches the original target ID.
- `GetUp()` matches the requested parent by actual C4DAtom equality, or is
  `None` for a root move.
- The world matrix matches the captured matrix within an explicitly approved
  component tolerance.
- The target subtree remains acyclic and all existing subtree IDs resolve to
  their original atoms.

The implementation plan must define and test the numeric matrix tolerance
before coding. Exact Python object or floating-point equality is not an
acceptable unstated assumption.

If failure occurs before `Remove()`, a proven no-change failure can use
`MUTATION_FAILED` or a more specific pre-write validation error. From the
first successful hierarchy mutation onward, any unproven result is
`OUTCOME_UNKNOWN`, `retryable=false`. The bridge must not perform an automatic
retry or an unverified rollback.

One native UI Undo must restore the original parent, sibling position, and
world appearance. This behavior and ID continuity must be certified in real
Cinema 4D 2023.2.2.

### 7.5 Response

Changed operation:

```json
{
  "object_id": "c4d:<document scope>:<object scope>",
  "parent_id": "c4d:<document scope>:<object scope>",
  "changed": true,
  "placement": "last_child",
  "world_transform_preserved": true
}
```

Top-level operation returns `parent_id=null` and
`placement="top_level_first"`. A verified no-op returns `changed=false` and
describes the existing placement without mutating the scene.

### 7.6 Timeout contract

`reparent_object` is a write command. It uses the certified ordinary mutation
timeout:

- Queued and cancelled before start: `MAIN_THREAD_TIMEOUT`,
  `retryable=false`.
- Running or possibly delivered without a verified response:
  `OUTCOME_UNKNOWN`, `retryable=false`.
- No automatic retry and one connection/request only.

## 8. `duplicate_object` contract

### 8.1 Request

```json
{
  "object_id": "c4d:<document scope>:<object scope>"
}
```

`object_id` is required and is the only field. Cross-document destinations,
parent choices, sibling positions, instance modes, names, and clone flags are
not accepted.

### 8.2 Source resolution and clone scope

The source is resolved through the complete Phase 2A.3 path. It must be one
real, live, addressable object in the current active document hierarchy.
Generated, cache, deform-cache, detached, stale, cross-document, and
unverified atoms are rejected before cloning.

The entire real source subtree is duplicated. The clone root is inserted
immediately after the source under the same parent. The source and all source
subtree IDs remain unchanged.

The documented clone path under review is:

```python
trans = c4d.AliasTrans()
trans.Init(doc)
clone = source.GetClone(c4d.COPYFLAGS_NONE, trans)
trans.Translate(True)
clone.InsertAfter(source)
```

`AliasTrans.Init()` must return actual `bool True`. The returned clone must be
a live `BaseObject` hierarchy and must not compare C4DAtom-equal to any source
subtree object.

`COPYFLAGS_NONE` is the initial normative flag because the 2023.2 SDK defines
`COPYFLAGS_NO_HIERARCHY` as excluding children and
`COPYFLAGS_NO_BRANCHES` as excluding branches such as tags and implying no
animation. Phase 3A.3 supplies neither exclusion, so the normal hierarchy and
branches are cloned.

`AliasTrans.Translate(True)` reconnects links within the cloned hierarchy to
cloned targets where possible and permits links to existing old targets when
no cloned target exists. Tags and animation branches handled by normal
`GetClone()` are included. Existing document materials are not deep-copied;
cloned tags and links continue to reference existing document resources
according to `AliasTrans` translation.

No guarantee is made for private plugin data, renderer-specific objects,
external asset files, or undocumented clone behavior. Instance recursion and
plugin-specific link behavior are explicit real-runtime certification items.

### 8.3 Fresh identity allocation

Before document insertion, the detached clone hierarchy is walked in DFS
pre-order on the main thread. A fresh, never-issued object scope is reserved
for every cloned hierarchy object.

After successful insertion, each clone object is registered through the
trusted new-object registry path. Generic equality matching is not weakened,
and `GetGUID()` is not consulted.

Required identity properties:

- Every cloned object ID is canonical and fresh.
- No clone ID equals a source, descendant, retired, or previously issued ID.
- Every source ID continues to resolve to its original source atom.
- The clone root and all clone descendants resolve through current active
  hierarchy revalidation.
- Scope reservations are never returned to the allocation pool, including on
  a failed clone attempt.

The success response is deliberately bounded:

```json
{
  "source_object_id": "c4d:<document scope>:<source scope>",
  "object": {
    "object_id": "c4d:<document scope>:<fresh clone root scope>",
    "name": "Source Name",
    "type_id": 0,
    "type_name": "Cube",
    "parent_id": null
  },
  "duplicated_count": 1
}
```

It does not return an unbounded list of all descendant IDs. Clients enumerate
the cloned subtree through the existing paginated `list_objects` and
`get_object` tools. This preserves the 64 KiB response-frame contract.

### 8.4 Native undo sequence

The proposed main-thread sequence is:

```text
resolve source and source subtree
StopAllThreads
initialize AliasTrans
GetClone detached hierarchy
Translate aliases
validate clone hierarchy and reserve fresh scopes
StartUndo
InsertAfter(source)
AddUndo(UNDOTYPE_NEWOBJ, clone) after insertion
register clone subtree with reserved scopes
EndUndo
re-resolve and verify source/clone no-alias and fresh IDs
EventAdd
```

`StartUndo()`, `AddUndo()`, and `EndUndo()` must return actual `bool True`.
`UNDOTYPE_NEWOBJ` is added only after the clone has been inserted, as required
by the official undo contract.

A failure before insertion, with the source scene proven unchanged, returns
`CLONE_FAILED`, `retryable=false`. From successful insertion onward, any
failure whose final state cannot be proven returns `OUTCOME_UNKNOWN`,
`retryable=false`. Automatic retry and a second connection are forbidden.

One native UI Undo must remove the complete duplicate subtree without changing
the source. On the next completed hierarchy reconciliation, every ID issued to
that duplicate subtree must become stale and be retired without poisoning
unrelated registry entries. Those scopes remain in the never-reuse set.

If native Redo or another UI operation later restores a clone hierarchy, it
must receive fresh process-local IDs. Phase 3A.3 does not resurrect the
pre-Undo duplicate IDs.

### 8.5 Cleanup and ownership

Before insertion, the clone is detached and must not be exposed through the
registry. The implementation must audit Python SDK ownership and cleanup for
failed detached clones without inventing an undocumented free API.

After insertion, the document owns the hierarchy. The bridge must not manually
free an inserted clone. A post-insertion failure is handled by native undo and
the fail-closed result contract, not by an unverified automatic cleanup.

Duplicate-scope reconciliation must be limited to bridge-issued duplicate
scopes known to have left the active hierarchy. It must not globally retire a
live object merely because one equality comparison was unverified. Retired or
unverified history must not prevent unrelated current hierarchy objects from
receiving or retaining valid IDs.

## 9. Identity implications

Phase 3A does not redesign Phase 2A.3 identity.

| Operation | Existing object IDs | New object IDs | Retirement |
| --- | --- | --- | --- |
| `set_active_object` | Unchanged | None | None |
| `reparent_object` | Target and subtree unchanged | None | None |
| `duplicate_object` | Source and subtree unchanged | Fresh ID for every cloned atom | Duplicate subtree IDs retire after native Undo removes it |

All object ID resolution remains:

```text
canonical ID parse
document scope validation
active document validation
registry lookup
IsAlive
active hierarchy DFS revalidation
exactly one C4DAtom equality match
```

The following identity outcomes remain fail closed:

- C4DAtom equality raises or returns a non-bool.
- `IsAlive()` raises, returns a non-bool, or returns `False`.
- The registry atom is live but absent from the active hierarchy.
- Zero or multiple current hierarchy atoms match.
- The active document differs from the encoded document scope.
- The document or object scope has been retired.

Bridge restart, plugin restart, Cinema 4D process restart, close/reopen, merge,
cross-document transfer, clone, and copy/paste do not guarantee ID continuity.

## 10. Undo and timeout contract

### 10.1 Native undo

Phase 3A never exposes `undo_last` and never calls `GetUndoPtr()` or
`DoUndo()`.

| Tool | Native undo plan |
| --- | --- |
| `set_active_object` | `StartUndo` → `SetActiveObject(SELECTION_NEW)` with automatic activation records → `GetActiveObject` → `EndUndo` |
| `reparent_object` | `StartUndo` → `AddUndo(UNDOTYPE_HIERARCHY_PSR)` before hierarchy change → move and restore world matrix → `EndUndo` |
| `duplicate_object` | `StartUndo` → insert clone → `AddUndo(UNDOTYPE_NEWOBJ)` after insertion → `EndUndo` |

All scene edits call `StopAllThreads()` first and run only on the existing main
thread dispatcher. Every completed state change calls `EventAdd()` once.

### 10.2 Write delivery and completion

All three Phase 3A tools are writes in the plugin and external server
write-name sets.

```text
not connected / not sent
→ C4D_UNAVAILABLE according to the existing pre-delivery contract

queued and cancelled before main-thread start
→ MAIN_THREAD_TIMEOUT
→ retryable=false
→ no state change

running, sent, response lost, connection reset, EOF, or late result
→ OUTCOME_UNKNOWN
→ retryable=false
→ no automatic retry
```

The client uses one request and one TCP connection. The server does not retry
any Phase 3A write.

Phase 3A.0 proposes retaining the certified ordinary mutation start/response
budget. A subphase may request a distinct completion budget only through a
separate reviewed contract change backed by real timing evidence. It must not
silently increase all read or mutation timeouts.

## 11. Error model

Existing errors are reused whenever their meanings already cover the new
condition.

| Error | Condition | Retryable | Stage | Scene may have changed | User action |
| --- | --- | ---: | --- | ---: | --- |
| `INVALID_PARAMS` | Malformed ID, missing/extra field, invalid `null`, name/path input, or unsupported option | false | Before write | No | Correct the exact tool input |
| `DOCUMENT_MISMATCH` | ID belongs to a different live document scope | false | Before write | No | Activate the owning document or list the current document |
| `STALE_OBJECT_ID` | Retired/dead process-local object or document scope | false | Before write | No | Call `list_objects` and use a current ID |
| `DOCUMENT_ID_UNVERIFIED` | Document equality/liveness cannot be proven | false | Before write | No | Stabilize the document state and list again |
| `OBJECT_ID_UNVERIFIED` | Object equality/liveness cannot be proven | false | Before write | No | List again; do not retry the write blindly |
| `OBJECT_NOT_IN_DOCUMENT` | Live registered object is not in the current addressable hierarchy | false | Before write | No | Use a current Object Manager object |
| `SAME_OBJECT` | `reparent_object` target and proposed parent are the same C4DAtom | false | Before write | No | Choose a different parent or `null` |
| `CYCLE_DETECTED` | Proposed parent is inside the target subtree | false | Before write | No | Choose a parent outside the subtree |
| `INVALID_HIERARCHY` | A verified structural precondition is invalid and no more specific existing error applies | false | Before write | No | Repair the hierarchy or choose another target |
| `CLONE_FAILED` | Clone/AliasTrans/reservation failed before insertion and no scene change occurred | false | Before insertion | No | Inspect unsupported source/plugin data; do not retry automatically |
| `MUTATION_FAILED` | A deterministic C4D or undo failure occurred before a hierarchy/selection change began | false | Before write | No | Resolve the reported condition and issue a new request |
| `MAIN_THREAD_TIMEOUT` | Queued write was cancelled before execution started | false | Before write | No | Restore main-thread responsiveness before a new explicit request |
| `OUTCOME_UNKNOWN` | Write started or may have been delivered and the final result cannot be proven | false | After start/delivery | Yes | Inspect with read tools/UI; never auto-retry |

`OBJECT_NOT_IN_ACTIVE_DOCUMENT` is not added because the existing
`OBJECT_NOT_IN_DOCUMENT` contract already represents this condition.
`OBJECT_HAS_CHILDREN` remains the existing delete guard and is not reused for
reparent or duplicate.

No error includes tokens, stack traces, Python object representations, memory
addresses, document contents, or unrelated object metadata.

## 12. Exact tool-surface evolution

Phase 3A.0 changes no production surface:

```text
Phase 3A.0: 9 tools
```

Proposed certified progression:

```text
Phase 3A.1: 10 tools
  Phase 2 exact 9
  + set_active_object

Phase 3A.2: 11 tools
  Phase 3A.1 exact 10
  + reparent_object

Phase 3A.3: 12 tools
  Phase 3A.2 exact 11
  + duplicate_object
```

At each stage:

- `ACTIVE_TOOL_NAMES`, plugin `ACTIVE_COMMAND_NAMES`, and plugin
  `ALLOWED_COMMANDS` are exactly equal.
- Only the new approved tool is added.
- All previous forbidden tools remain unregistered and return
  `UNKNOWN_COMMAND` at the raw bridge.
- Existing capability booleans retain their Phase 2 meanings.
  `object_operations=true` already covers typed object mutation; the exact
  `tools` list is the source of truth for hierarchy support.
- Runtime labels change only in the subphase implementation commit after
  approval.

## 13. Automated test plan

Every subphase runs all existing Phase 1/2 regression tests plus its new tests.
The current Phase 2 regression count is a baseline observation, not a frozen
future count.

### 13.1 Common test levels

Each tool is tested at six levels:

1. Pure validation: exact schema, canonical IDs, extra fields, and invalid
   values fail before a socket opens.
2. Fake C4D contract: documented call order, actual-bool checks, C4DAtom
   equality/liveness, hierarchy semantics, native undo, `EventAdd()`, and
   failure atomicity.
3. Raw bridge transport: authentication, protocol, allowlist, structured
   errors, main-thread dispatch, timeout state, response frame safety, and
   recovery.
4. Real MCP STDIO integration: exact tool schema, transport-safe
   `INVALID_PARAMS`, no output-schema conflict, one connection for writes, and
   post-delivery uncertainty.
5. Real Cinema 4D 2023.2.2 manual certification.
6. Codex App E2E certification.

### 13.2 Phase 3A.1 automated tests

- Exact 10-tool FastMCP and plugin surfaces.
- `object_id` required; all extras and null rejected before socket use.
- Same-wrapper and different-wrapper/same-atom selection succeeds.
- Stale, detached, unverified, generated/cache, and cross-document IDs fail
  closed.
- `SELECTION_NEW` is used; add/toggle/subtract modes are unreachable.
- `GetActiveObject()` is called after `SetActiveObject()`.
- Readback must be actual C4DAtom equality `True`.
- No manual `UNDOTYPE_ACTIVATE`/`UNDOTYPE_DEACTIVATE`.
- One `StartUndo`/`EndUndo` pair and one `EventAdd()`.
- Queued timeout and running/post-send uncertainty contracts.
- No object or document ID changes.

### 13.3 Phase 3A.2 automated tests

- Exact 11-tool surfaces.
- Required `object_id`/`parent_id`, nullable parent only, and extra-field
  rejection.
- Same object → `SAME_OBJECT`; descendant parent → `CYCLE_DETECTED`.
- Parent and target must resolve in one active-document hierarchy snapshot.
- Cross-document, stale, detached, and unverified IDs fail closed.
- Current-parent request returns `changed=false` with no undo or `EventAdd()`.
- Non-null parent places target last; null parent places target first at root.
- `GetMg()` occurs before mutation and `SetMg()` after insertion.
- `UNDOTYPE_HIERARCHY_PSR` occurs before `Remove()`.
- One native undo action restores parent, sibling placement, and PSR in the
  fake contract.
- Target and entire subtree retain IDs across move and fake native Undo.
- Failures before removal are definite; failures after removal normalize to
  `OUTCOME_UNKNOWN`, `retryable=false`.
- No automatic retry.

### 13.4 Phase 3A.3 automated tests

- Exact 12-tool surfaces.
- Only canonical source `object_id`; every extra option rejected before socket
  use.
- `AliasTrans.Init(doc)`, `GetClone(COPYFLAGS_NONE, trans)`, and
  `Translate(True)` order.
- Generated/cache/detached/cross-document sources rejected.
- Clone root inserted immediately after source under the same parent.
- `UNDOTYPE_NEWOBJ` added after insertion.
- Original subtree IDs unchanged.
- Every clone subtree atom receives a fresh canonical ID.
- Source and clone C4DAtom comparisons are actual `False`; no ID alias.
- Reserved or retired clone scopes are never reused.
- Native Undo simulation removes the duplicate, retires all duplicate IDs,
  preserves source IDs, and does not poison unrelated objects.
- Native Redo simulation, when modeled, assigns fresh IDs rather than
  resurrecting retired IDs.
- Clone failure before insertion → `CLONE_FAILED`; failure after insertion →
  `OUTCOME_UNKNOWN`, `retryable=false`.
- Response remains below the 64 KiB frame limit for a large subtree because
  it returns only root metadata and count.
- No automatic retry.

### 13.5 Mandatory Phase 2 regressions

Every subphase retains:

```text
Phase 2 exact identity lifecycle
native delete Undo restored object gets a fresh ID
retired old ID remains stale and never aliases
document isolation and close/reopen stale handling
pagination and 64 KiB request/response guards
existing-path-only native .c4d save
current-process identity preserved by save
transport-safe INVALID_PARAMS over real STDIO
read/save timeout budgets
write OUTCOME_UNKNOWN retryable=false
no automatic write retry
authentication and loopback-only transport
SpecialEventAdd/CoreMessage main-thread dispatch
shutdown, cancellation, restart, and port-collision handling
arbitrary Python/DescID/type IDs/renderer commands unavailable
```

## 14. Real C4D certification plan

All tests target Cinema 4D 2023.2.2 on the certified Windows/C4D Python
environment. Results must be recorded as observed by the human tester; the
certification must not imply that Codex directly observed the Cinema 4D UI.

### 14.1 Phase 3A.1

1. Start from the preceding certified runtime and deploy only the Phase 3A.1
   plugin/server.
2. Restart Cinema 4D and the bridge; verify runtime and exact 10 tools.
3. In Document A create two ordinary UI objects and acquire current IDs.
4. Select object A in the UI, call `set_active_object(ID_B)`, and verify only B
   is selected.
5. Verify `get_scene_info.active_object_ids == [ID_B]`.
6. Verify A and B IDs remain unchanged across repeated list/get calls.
7. Use native UI Undo once and verify the prior A selection is restored; Redo
   restores B.
8. Rename and transform B; verify the same ID can still be selected.
9. Delete B in the UI and verify the stale ID fails closed.
10. In Document B call with an A ID and verify `DOCUMENT_MISMATCH`.
11. Restart the bridge, reacquire process-local IDs as required, and verify
    normal selection recovery.
12. Exercise queued timeout in a controlled certification environment and
    verify no selection change and no retry.
13. Exercise running/post-delivery uncertainty where practical; inspect the
    final selection manually and verify no automatic retry.
14. Clean up test documents and confirm no tags, user data, or hidden markers
    were added.

### 14.2 Phase 3A.2

1. Deploy the certified Phase 3A.1 baseline plus only Phase 3A.2; verify exact
   11 tools.
2. In Document A create two root Nulls and a nested Cube subtree.
3. Record all object IDs and global transforms.
4. Reparent the Cube to the other Null and verify last-child placement,
   unchanged world transform, and unchanged subtree IDs.
5. Move the Cube to `parent_id=null` and verify top-root placement.
6. Request the current parent and verify a no-op with no extra undo step.
7. Attempt self-parent and descendant-parent moves; verify `SAME_OBJECT` and
   `CYCLE_DETECTED` with no hierarchy change.
8. Try stale and Document B IDs; verify fail-closed errors and no movement.
9. Native UI Undo each successful move and verify original parent, sibling
   placement, world appearance, and IDs.
10. Use generators and non-default transforms to test world-matrix
    preservation without addressing generated cache objects.
11. Restart the bridge, reacquire IDs as required, and verify a new move.
12. Certify queued timeout as no-start/no-change and running uncertainty as
    `OUTCOME_UNKNOWN`, `retryable=false`, with no automatic retry.
13. Clean up and verify no temporary scene data remains.

### 14.3 Phase 3A.3

1. Deploy the certified Phase 3A.2 baseline plus only Phase 3A.3; verify exact
   12 tools.
2. In Document A build a source Null with nested objects, duplicate names,
   transforms, a standard material tag, animation data, internal links where
   available, and an external link where safely testable.
3. Record the complete source subtree IDs.
4. Call `duplicate_object(source_id)`.
5. Verify the clone root is immediately after the source under the same
   parent.
6. Verify every source ID is unchanged and every clone subtree ID is fresh,
   canonical, and different from every source ID.
7. Verify repeated list/get calls preserve all live IDs and never alias source
   and clone.
8. Verify cloned names, transforms, hierarchy, tags, animation branches, and
   documented `AliasTrans` link behavior.
9. Verify existing materials are linked as documented and are not silently
   deep-copied.
10. Native UI Undo once; verify the whole duplicate disappears, the source is
    unchanged, and all former duplicate IDs fail closed as stale.
11. If native Redo is tested, verify the restored duplicate receives fresh IDs
    and old duplicate IDs remain stale.
12. In Document B call with an A source ID and verify
    `DOCUMENT_MISMATCH`; cross-document copy is unavailable.
13. Delete or detach the source and verify stale/membership errors.
14. Restart the bridge, reacquire IDs, and verify a new duplication.
15. Certify queued timeout as no insertion and running/post-delivery
    uncertainty as `OUTCOME_UNKNOWN`, `retryable=false`, with one request and
    no automatic retry.
16. Verify large-subtree response framing and enumerate descendants only
    through paginated `list_objects`.
17. Clean up cloned objects/material references and verify no hidden identity
    data was inserted.

## 15. Codex App E2E plan

Each subphase begins with:

- `ping`
- `get_capabilities`
- exact runtime label
- exact tool count and names
- forbidden tools absent
- bridge stop/start recovery

### Phase 3A.1 E2E

- List two ordinary UI-created objects.
- Set one active by opaque ID and verify replace-only selection in C4D and
  `get_scene_info`.
- Verify stale and cross-document IDs fail closed.
- Verify native UI Undo/Redo selection behavior.
- Verify invalid names, paths, extra arguments, and null fail through a
  transport-safe `INVALID_PARAMS` result without a bridge request.
- Verify timeout/no-retry behavior from the recorded real-C4D procedure.

### Phase 3A.2 E2E

- Reparent a nested object between two parents and then to root.
- Verify last-child/top-root placement, world transform, and stable subtree
  IDs.
- Verify self-parent, cycle, stale, and cross-document calls fail without
  mutation.
- Verify native UI Undo restores the exact hierarchy.
- Verify bridge restart recovery and write timeout/no retry.
- Clean up without leaving temporary identity data.

### Phase 3A.3 E2E

- Duplicate a multi-level ordinary hierarchy.
- Verify the original IDs remain stable.
- Verify the clone root and every cloned descendant have fresh IDs.
- Verify source and clone never alias.
- Verify tags, standard materials, animation branches, and links match the
  approved clone contract.
- Native UI Undo the duplicate; verify every duplicate ID is stale and every
  source ID still succeeds.
- Verify stale/cross-document calls, restart recovery, large-subtree
  pagination, timeout/no retry, and cleanup.

No subphase is complete until automated tests, real Cinema 4D certification,
and Codex App E2E all pass.

## 16. Cleanup plan

Phase 3A cleanup is fail closed and never identity-based scene mutation:

- No temporary tags, user data, hidden objects, custom markers, or document
  metadata are created.
- `set_active_object` creates no registry entries and leaves only the explicit
  UI selection side effect.
- `reparent_object` creates or retires no object scopes. A completed native
  Undo must restore hierarchy without ID replacement.
- `duplicate_object` reserves fresh scopes before insertion and never reuses
  them. Pre-insertion failures expose no registry binding. Post-insertion
  ambiguity returns `OUTCOME_UNKNOWN` and relies on inspection/native UI Undo,
  not automatic retry or unverified rollback.
- When native Undo removes a duplicate subtree, a later main-thread hierarchy
  reconciliation retires every bridge-issued clone scope that is no longer in
  the active hierarchy. The retired scopes stay in the never-reuse set.
- A native Redo or later restoration receives fresh IDs. Old IDs are not
  resurrected.
- Retired/unverified history must not poison identity allocation for unrelated
  current hierarchy objects.
- Bridge/plugin/Cinema 4D restart invalidates process-local IDs according to
  the existing contract; it does not write persistent replacement IDs.

## 17. Open questions

The following questions require approval or real Cinema 4D 2023.2.2 evidence
before the affected implementation can be certified:

1. Does the documented automatic selection undo behavior of
   `SetActiveObject(SELECTION_NEW)` form exactly one safe native UI undo step
   when enclosed by `StartUndo()`/`EndUndo()`?
2. What component tolerance should be normative for `GetMg()`/`SetMg()`
   world-matrix postverification, including frozen transforms, scaled parents,
   generators, and floating-point round trips?
3. Does root insertion through `BaseDocument.InsertObject(obj)` consistently
   produce the documented top-root placement in Cinema 4D 2023.2.2, and does
   `UNDOTYPE_HIERARCHY_PSR` restore the exact prior sibling position?
4. Should `COPYFLAGS_RECURSIONCHECK` be combined with `COPYFLAGS_NONE` for the
   approved duplicate path? Its behavior with instances and recursive links
   must be measured before changing the normative clone flags.
5. Does `AliasTrans.Translate(True)` preserve all approved internal/external
   links, animation branches, standard tags, and material references exactly
   as specified for ordinary 2023.2.2 objects?
6. What is the safest implementation trigger for retiring an entire
   bridge-issued duplicate subtree after native UI Undo without mistaking a
   transient detach during an in-progress write for permanent removal?
7. Is root-only metadata plus `duplicated_count` the approved bounded
   duplicate response, with paginated `list_objects` as the sole descendant
   enumeration mechanism?
8. Does large-subtree cloning require a separately reviewed completion
   timeout, or can it retain the certified ordinary mutation timeout without
   unacceptable ambiguity?
9. What certification tag naming convention should be used for the three
   independent subphases?

Open questions are not permission to make implementation assumptions. An
unresolved safety question blocks the affected subphase.

## 18. Approval gate

Phase 3A implementation must not begin until this contract is reviewed and the
target subphase is explicitly approved.

Approval for one subphase does not approve later subphases. Each implementation
must:

1. Start from the preceding certified baseline.
2. Add exactly one approved tool.
3. Preserve all Phase 2 identity, security, lifecycle, save, and timeout
   contracts.
4. Use only documented Cinema 4D 2023.2 APIs named in the approved contract.
5. Pass all automated regressions and real MCP 1.28.1 STDIO introspection.
6. Pass real Cinema 4D 2023.2.2 certification.
7. Pass Codex App E2E certification.
8. Produce a certification document, merge commit, and non-forced annotated
   tag before the next subphase begins.

Current gate:

```text
Phase 3A.0 CONTRACT DRAFT
Implementation: NOT STARTED
Certification: NOT STARTED
Approval: PENDING REVIEW
```
