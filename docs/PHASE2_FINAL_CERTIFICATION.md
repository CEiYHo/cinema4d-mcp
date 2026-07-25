# Cinema 4D MCP Phase 2 Final Certification

Certification date: 2026-07-26

## 1. Certification status

| Phase | Status |
| --- | --- |
| Phase 1 | COMPLETE |
| Phase 2A | COMPLETE |
| Phase 2B | COMPLETE |
| Phase 2C | COMPLETE |
| Phase 2 | FINAL COMPLETE |

Certified runtime: `0.4.0-phase2c`

## 2. Certified environment

| Component | Certified value |
| --- | --- |
| Cinema 4D | 2023.2.2 |
| Cinema 4D raw version | `2023202` |
| Cinema 4D Python | 3.10.8 |
| MCP Python package | 1.28.1 |
| Operating system | Windows |
| Client | Codex Desktop App |
| External transport | STDIO |
| Internal bridge | Authenticated localhost TCP |

## 3. Architecture

The certified request path is:

```text
Codex Desktop App
→ STDIO FastMCP server
→ authenticated 127.0.0.1 TCP bridge
→ Cinema 4D plugin socket worker
→ c4d.SpecialEventAdd()
→ GeDialog.CoreMessage()
→ main-thread dispatcher
→ Cinema 4D API
```

The socket worker authenticates, validates, frames, and queues requests. All
Cinema 4D API access, including read operations, mutations, identity checks,
and persistence, runs through the main-thread dispatcher.

## 4. Exact external tool surface

The certified external surface contains exactly these nine tools:

1. `ping`
2. `get_capabilities`
3. `get_scene_info`
4. `list_objects`
5. `get_object`
6. `create_object`
7. `update_object`
8. `delete_object`
9. `save_document`

The FastMCP tool registry and Cinema 4D plugin command allowlist match this
surface exactly.

## 5. Capability matrix

| Capability | Value |
| --- | --- |
| `scene_read` | `true` |
| `object_operations` | `true` |
| `undo` | `false` |
| `save` | `true` |
| `animation` | `false` |
| `camera` | `false` |
| `light` | `false` |
| `mograph` | `false` |
| `octane_commands` | `false` |
| `arbitrary_python` | `false` |
| `remote_transport` | `false` |

`undo=false` means that no remote MCP undo tool is exposed. The supported
mutations still create native Cinema 4D undo transactions for use through the
Cinema 4D UI.

## 6. Phase 2A identity contract

Object identity is an opaque, process-local composition of:

```text
process-local document scope
+
process-local object scope
+
C4DAtom equality
+
IsAlive
+
active hierarchy revalidation
+
fail closed
```

Production identity does not use object-name lookup, fuzzy lookup,
`BaseObject.GetGUID()`, memory addresses, persistent hidden markers, tags, or
user data. Stale IDs are never rebound to unrelated objects.

MCP deletion explicitly retires the deleted object and subtree scopes. The
certified native delete/undo lifecycle is:

```text
create → ID_A
delete → ID_A retired
Cinema 4D native Undo
restored object → fresh ID_B
ID_B != ID_A
ID_B succeeds
ID_A remains STALE_OBJECT_ID
```

Identity is guaranteed only for the current plugin process, live document, and
live underlying `BaseObject`. Continuity is not promised across close/reopen,
plugin restart, Cinema 4D restart, merge, clone, copy/paste, or cross-document
move.

## 7. Phase 2B mutation and native Undo contract

Phase 2B supports typed `create_object`, `update_object`, and `delete_object`.
Creation is restricted to the explicit primitive allowlist, and updates are
restricted to the documented name and relative PSR fields. Arbitrary C4D type
IDs, arbitrary parameters, and arbitrary `DescID` editing are unavailable.

Each mutation uses a native Cinema 4D undo transaction:

```text
create: StartUndo → InsertObject → AddUndo(UNDOTYPE_NEWOBJ) → EndUndo
update: StartUndo → AddUndo(UNDOTYPE_CHANGE) → setters → EndUndo
delete: StartUndo → AddUndo(UNDOTYPE_DELETEOBJ) → Remove → EndUndo
```

Remote `undo_last` is intentionally unavailable. The MCP server never calls
`GetUndoPtr()` or `DoUndo()`. Users may undo supported mutations through
Cinema 4D's native Undo command.

When a delivered write cannot be proven successful or unsuccessful, the
contract is:

```text
OUTCOME_UNKNOWN
retryable=false
```

Mutations are never retried automatically.

## 8. Phase 2C persistence contract

`save_document` takes no arguments and saves only the current active document
to its already existing native `.c4d` path. The path must be absolute, the
reported name must be a basename, and the combined target must already exist
as a regular file.

The exact Cinema 4D call is:

```python
c4d.documents.SaveDocument(
    doc,
    full_path,
    c4d.SAVEDOCUMENTFLAGS_DONTADDTORECENTLIST,
    c4d.FORMAT_C4DEXPORT,
)
```

The save path does not allow dialogs, Save As, Save Project with Assets,
non-`.c4d` overwrite, or document path/name mutation. An untitled document
returns `SAVE_PATH_REQUIRED` and does not open a file dialog. `SaveDocument()`
must return the actual boolean `True`, followed by verification that the
target still exists as a regular file.

Saving does not modify process-local object identities in the current
document session. Fresh process-local scopes are expected after close/reopen.

## 9. Timeout and no-retry contract

| Budget | Value |
| --- | --- |
| Read and ordinary response timeout | 5 seconds |
| Main-thread queue-start timeout | 5 seconds |
| Save execution completion timeout | 120 seconds |
| External save response timeout | 130 seconds |

The 130-second external budget is greater than the bridge's 5 + 120-second
start/completion budget and leaves response serialization and network
headroom.

A write that times out before main-thread execution starts is cancelled and
returns `MAIN_THREAD_TIMEOUT`. Once a write may have started or request bytes
may have reached the bridge, timeout, reset, EOF, or missing/malformed response
returns `OUTCOME_UNKNOWN` with `retryable=false`. Writes, including
`save_document`, use one request connection and are never retried
automatically.

## 10. Automated test evidence

The final automated regression result verified in this repository was:

```text
Ran 160 tests
OK
```

Certified introspection evidence:

```text
MCP package: 1.28.1
Runtime: 0.4.0-phase2c
Tool count: 9
save_document input properties: {}
Read response timeout: 5 seconds
Save response timeout: 130 seconds
Bridge save timeout budget: 5 + 120 seconds
```

## 11. Real Cinema 4D certification evidence

The following Cinema 4D 2023.2.2 results were reported by the user from the
actual certified environment. They are recorded as user-supplied certification
evidence; this document does not claim that Codex directly observed the Cinema
4D UI.

```text
Connectivity                             PASS
Runtime 0.4.0-phase2c                    PASS
Exact 9-tool surface                     PASS
Capability save=true                     PASS
Untitled SAVE_PATH_REQUIRED              PASS
Untitled no-dialog behavior              PASS
Existing .c4d path recognition           PASS
Create                                   PASS
Update                                   PASS
Save existing document                   PASS
Save no-dialog behavior                  PASS
Current-process object ID preserved      PASS
Arbitrary path rejected                  PASS
Transport-safe INVALID_PARAMS            PASS
Output validation compatibility          PASS
Save As surface unavailable              PASS
Close/reopen persistence                 PASS
Old ID no-alias after reopen             PASS
Bridge restart/recovery                  PASS
Save after bridge restart                PASS
No automatic save retry                  PASS
Unexpected OUTCOME_UNKNOWN               PASS
Cleanup                                  PASS
```

The persisted object observed after close/reopen was:

```text
MCP_P2C_Cube_Saved
position = [100,200,300]
rotation_deg = [10,20,30]
scale = [2,2,2]
```

The prior process-local object ID returned `STALE_OBJECT_ID` and did not alias
another object.

## 12. Codex App E2E evidence

The user-reported Codex Desktop App E2E run covered capability discovery,
exact tool introspection, read operations, typed mutations, existing-path
save, close/reopen persistence, identity behavior, bridge restart, and recovery.

The unsupported-path validation response at STEP 8 was:

```text
isError=true
INVALID_PARAMS: This command does not accept parameters
```

This confirmed that invalid arguments are returned through MCP 1.28.1 STDIO as
a transport-safe error instead of reaching the TCP bridge or becoming an
output-schema validation failure.

## 13. Security boundaries

- The internal bridge binds to `127.0.0.1` only.
- `C4D_MCP_TOKEN` authentication is mandatory and secrets are not logged.
- `C4D_MCP_PORT`, protocol version, request schemas, and the 64 KiB frame limit
  are validated.
- FastMCP and plugin allowlists expose the same exact nine commands.
- Cinema 4D API calls run only through the main-thread dispatcher.
- Write delivery ambiguity is fail-closed and non-retryable.
- Object lookup uses opaque identity only; names never become identifiers.
- Save accepts no caller path and cannot open a dialog or perform Save As.
- Arbitrary Python, remote transport, and renderer control are unavailable.

## 14. Intentional exclusions

The certified Phase 2 surface intentionally excludes:

```text
undo_last
save_as
save_project
save_project_with_assets
load_document
close_document
execute_python
arbitrary Python
arbitrary object type IDs
arbitrary DescID/parameter editing
animation tools
camera/light tools
MoGraph tools
Octane control commands
Redshift commands
remote transport
```

## 15. Relevant commits

| Commit | Subject |
| --- | --- |
| `c9c67e88874a5bd629d9a1de3fae7dc906e3b4af` | `fix: remove unsafe remote undo surface` |
| `bc8c35fca9df77a4d1af3df94ea138b70e79434f` | `docs: record Phase 2B write timeout certification` |
| `4751e0d81d34d3bf301df3d42c141f4c6ea1559b` | `fix: retire object identities after delete` |
| `1eba0ae1e4ce583bc50e74a9ac2697a661a09442` | `feat: add existing-path document save` |
| `8f43b195a7fee5f2407ff2ab694599c5223938c9` | `test: cover Phase 2C persistence safety` |
| `c9e510a7a1eb430eeb9a35128788fc87e1651509` | `fix: add response headroom for document save` |
| `50accd86ba8f53ebe01dd6a11f227869068d6011` | `fix: return transport-safe MCP validation errors` |

## 16. Final certification statement

Phase 1, Phase 2A, Phase 2B, and Phase 2C passed their automated, actual Cinema
4D 2023.2.2, and Codex Desktop App E2E gates. The exact nine-tool surface and
runtime `0.4.0-phase2c` form the certified Phase 2 release baseline.

## 17. Maintenance notes

MCP Python SDK 1.28.1 is the certified version.

The transport-safe invalid-argument implementation relies on MCP 1.28.1
FastMCP internal handler behavior.

Any MCP SDK upgrade requires renewed STDIO integration testing, tool-schema
introspection, invalid-argument testing, and Codex App E2E certification.

Changes to the tool surface, security boundary, identity lifecycle, mutation
semantics, persistence contract, or timeout budgets require renewed applicable
certification.

Phase 2 FINAL COMPLETE: YES
