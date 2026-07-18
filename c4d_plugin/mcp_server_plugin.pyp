"""Secure Phase 2A Cinema 4D MCP bridge for Cinema 4D 2023.2.2.

The socket thread performs transport validation and authentication only. The
five allowed commands are executed from a custom CoreMessage on Cinema 4D's
main thread. Scene and object inspection is read-only; no mutation, renderer
control, file operation, or arbitrary-Python command is present.
"""

import hmac
import json
import math
import os
import queue
import socket
import threading
import time
import sys

import c4d
from c4d import gui


# Retained from the upstream baseline so the existing plugin registration keeps
# working. Replace this only with an ID whose Plugin Café ownership is verified.
PLUGIN_ID = 1057843
PLUGIN_NAME = "Cinema 4D MCP Phase 2A Bridge"
MAIN_THREAD_EVENT_ID = PLUGIN_ID

PROTOCOL_VERSION = 1
BRIDGE_VERSION = "0.2.0-phase1"
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 5555
DEFAULT_REQUEST_SIZE_LIMIT = 64 * 1024
DEFAULT_CLIENT_TIMEOUT = 5.0
DEFAULT_MAIN_THREAD_TIMEOUT = 5.0
ACTIVE_COMMAND_NAMES = (
    "ping",
    "get_capabilities",
    "get_scene_info",
    "list_objects",
    "get_object",
)
ALLOWED_COMMANDS = frozenset(ACTIVE_COMMAND_NAMES)
OBJECT_ID_PREFIX = "c4d:"
MAX_OBJECT_ID_LENGTH = 128
TOKEN_MIN_LENGTH = 32
TOKEN_MAX_LENGTH = 256
TOKEN_MIN_ESTIMATED_ENTROPY_BITS = 128
TOKEN_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def _success_envelope(request_id, result):
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": True,
        "result": result,
        "error": None,
    }


def _enqueue_main_thread_message(msg_queue, message_type, value):
    """Queue work and explicitly wake Cinema 4D's main thread."""
    msg_queue.put((message_type, value))
    c4d.SpecialEventAdd(MAIN_THREAD_EVENT_ID)


def _error_envelope(
    request_id,
    code,
    message,
    retryable=False,
    user_action=None,
    details=None,
):
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": False,
        "result": None,
        "error": {
            "code": code,
            "message": message,
            "retryable": bool(retryable),
            "user_action": user_action,
            "details": details or {},
        },
    }


class _BridgeCommandError(Exception):
    """Expected structured failure from a validated main-thread command."""

    def __init__(self, code, message, details=None):
        super(_BridgeCommandError, self).__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _configured_port():
    raw_value = os.environ.get("C4D_MCP_PORT", str(DEFAULT_PORT))
    try:
        port = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError("C4D_MCP_PORT must be an integer")
    if not 1 <= port <= 65535:
        raise ValueError("C4D_MCP_PORT must be between 1 and 65535")
    return port


def _validated_configured_token(token):
    """Validate a configured secret without ever returning it in an error."""
    if not isinstance(token, str) or not token:
        raise ValueError("C4D_MCP_TOKEN is required")
    try:
        encoded = token.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        raise ValueError("C4D_MCP_TOKEN must use URL-safe ASCII characters")
    if not TOKEN_MIN_LENGTH <= len(encoded) <= TOKEN_MAX_LENGTH:
        raise ValueError(
            "C4D_MCP_TOKEN must be between {} and {} ASCII characters".format(
                TOKEN_MIN_LENGTH, TOKEN_MAX_LENGTH
            )
        )
    if any(character not in TOKEN_ALPHABET for character in token):
        raise ValueError("C4D_MCP_TOKEN must use URL-safe ASCII characters")
    counts = {}
    for character in token:
        counts[character] = counts.get(character, 0) + 1
    estimated_entropy_bits = 0.0
    for count in counts.values():
        probability = float(count) / len(token)
        estimated_entropy_bits -= count * math.log(probability, 2)
    if estimated_entropy_bits < TOKEN_MIN_ESTIMATED_ENTROPY_BITS:
        raise ValueError(
            "C4D_MCP_TOKEN must have at least {} bits of estimated entropy".format(
                TOKEN_MIN_ESTIMATED_ENTROPY_BITS
            )
        )
    return encoded


def _format_c4d_version(raw_version):
    """Format the numeric Cinema 4D 2023-style version without hiding raw data."""
    if not isinstance(raw_version, int) or raw_version < 2000000:
        return str(raw_version)

    year = raw_version // 1000
    revision = raw_version % 1000
    minor = revision // 100
    patch = revision % 100
    return "{}.{}.{}".format(year, minor, patch)


def _is_valid_object_id(value):
    if not isinstance(value, str) or len(value) > MAX_OBJECT_ID_LENGTH:
        return False
    if not value.startswith(OBJECT_ID_PREFIX):
        return False
    payload = value[len(OBJECT_ID_PREFIX) :]
    digits = payload[1:] if payload.startswith("-") else payload
    return (
        bool(digits)
        and digits.isascii()
        and digits.isdigit()
        and digits[0] != "0"
    )


def _object_id_or_none(obj):
    """Serialize documented integer GetGUID() output as an opaque string."""
    try:
        guid = obj.GetGUID()
    except Exception:
        return None
    if isinstance(guid, bool) or not isinstance(guid, int) or guid == 0:
        return None
    return "{}{}".format(OBJECT_ID_PREFIX, guid)


def _walk_document_hierarchy(doc):
    """Return real document objects in deterministic depth-first pre-order."""
    entries = []
    current = doc.GetFirstObject()
    parent = None
    depth = 0
    pending_siblings = []

    while current is not None:
        entries.append({"object": current, "parent": parent, "depth": depth})
        sibling = current.GetNext()
        child = current.GetDown()
        if sibling is not None:
            pending_siblings.append((sibling, parent, depth))
        if child is not None:
            parent = current
            current = child
            depth += 1
        elif pending_siblings:
            current, parent, depth = pending_siblings.pop()
        else:
            current = None
    return entries


def _build_identity_snapshot(doc):
    entries = _walk_document_hierarchy(doc)
    counts = {}
    for entry in entries:
        candidate = _object_id_or_none(entry["object"])
        entry["candidate_id"] = candidate
        if candidate is not None:
            counts[candidate] = counts.get(candidate, 0) + 1

    identity_by_object = {}
    for entry in entries:
        candidate = entry["candidate_id"]
        identity_by_object[id(entry["object"])] = (
            candidate if candidate is not None and counts[candidate] == 1 else None
        )
    return entries, identity_by_object


def _safe_type_name(obj):
    try:
        type_name = obj.GetTypeName()
    except Exception:
        return None
    return type_name if isinstance(type_name, str) and type_name else None


def _object_name(obj):
    name = obj.GetName()
    if not isinstance(name, str):
        raise RuntimeError("Cinema 4D returned an invalid object name")
    return name


def _runtime_type_id(obj):
    type_id = obj.GetType()
    if isinstance(type_id, bool) or not isinstance(type_id, int):
        raise RuntimeError("Cinema 4D returned an invalid object type")
    return type_id


def _object_summary(entry, identity_by_object):
    obj = entry["object"]
    parent = entry["parent"]
    object_id = identity_by_object[id(obj)]
    parent_id = (
        identity_by_object.get(id(parent)) if parent is not None else None
    )
    result = {
        "object_id": object_id,
        "addressable": object_id is not None,
        "name": _object_name(obj),
        "type_id": _runtime_type_id(obj),
        "type_name": _safe_type_name(obj),
        "parent_id": parent_id,
        "depth": entry["depth"],
    }
    if object_id is None:
        result["id_error"] = "OBJECT_ID_UNAVAILABLE"
    if parent is not None and parent_id is None:
        result["parent_id_error"] = "OBJECT_ID_UNAVAILABLE"
    return result


def _require_active_document():
    doc = c4d.documents.GetActiveDocument()
    if doc is None:
        raise _BridgeCommandError(
            "NO_ACTIVE_DOCUMENT",
            "Cinema 4D has no active document",
        )
    return doc


def _read_scene_info(doc):
    entries, identity_by_object = _build_identity_snapshot(doc)
    name = doc.GetDocumentName()
    path = doc.GetDocumentPath()
    if not isinstance(name, str) or not isinstance(path, str):
        raise RuntimeError("Cinema 4D returned invalid document metadata")

    active_candidates = set()
    active_objects = doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_CHILDREN)
    for obj in active_objects or []:
        candidate = _object_id_or_none(obj)
        if candidate is not None:
            active_candidates.add(candidate)

    active_object_ids = []
    for entry in entries:
        object_id = identity_by_object[id(entry["object"])]
        if object_id is not None and object_id in active_candidates:
            active_object_ids.append(object_id)

    return {
        "document": {
            "name": name,
            "path": path,
            "saved": bool(path),
        },
        "object_count": len(entries),
        "active_object_ids": active_object_ids,
    }


def _read_object_list(doc):
    entries, identity_by_object = _build_identity_snapshot(doc)
    return {
        "objects": [
            _object_summary(entry, identity_by_object) for entry in entries
        ]
    }


def _vector_values(vector):
    values = [float(vector.x), float(vector.y), float(vector.z)]
    if not all(math.isfinite(value) for value in values):
        raise RuntimeError("Cinema 4D returned a non-finite transform")
    return values


def _rotation_degree_values(rotation):
    # GetRelRot() returns HPB in radians. Vector x/y/z are H/P/B.
    values = [
        float(c4d.utils.RadToDeg(rotation.x)),
        float(c4d.utils.RadToDeg(rotation.y)),
        float(c4d.utils.RadToDeg(rotation.z)),
    ]
    if not all(math.isfinite(value) for value in values):
        raise RuntimeError("Cinema 4D returned a non-finite rotation")
    return values


def _read_object(doc, object_id):
    entries, identity_by_object = _build_identity_snapshot(doc)
    matches = [
        entry for entry in entries if entry["candidate_id"] == object_id
    ]
    if not matches:
        raise _BridgeCommandError(
            "OBJECT_NOT_FOUND",
            "No object with that object_id exists in the active document",
        )
    if len(matches) != 1 or identity_by_object[id(matches[0]["object"])] is None:
        raise _BridgeCommandError(
            "OBJECT_ID_UNAVAILABLE",
            "The object_id is not uniquely addressable in the active document",
        )

    entry = matches[0]
    obj = entry["object"]
    parent = entry["parent"]
    parent_id = (
        identity_by_object.get(id(parent)) if parent is not None else None
    )
    # Reuse the per-request traversal entries so child wrappers do not have to
    # retain Python object identity across separate Cinema 4D API calls.
    children = [
        identity_by_object[id(child_entry["object"])]
        for child_entry in entries
        if child_entry["parent"] is obj
        and identity_by_object[id(child_entry["object"])] is not None
    ]

    position = obj.GetRelPos()
    rotation = obj.GetRelRot()
    scale = obj.GetRelScale()
    result = {
        "object_id": object_id,
        "name": _object_name(obj),
        "type_id": _runtime_type_id(obj),
        "type_name": _safe_type_name(obj),
        "parent_id": parent_id,
        "transform": {
            "position": _vector_values(position),
            "rotation_deg": _rotation_degree_values(rotation),
            "scale": _vector_values(scale),
            "space": "relative",
        },
        "children": children,
    }
    if parent is not None and parent_id is None:
        result["parent_id_error"] = "OBJECT_ID_UNAVAILABLE"
    return result


def _dispatch_read_command(command, params):
    try:
        doc = _require_active_document()
        if command == "get_scene_info":
            return _read_scene_info(doc)
        if command == "list_objects":
            return _read_object_list(doc)
        if command == "get_object":
            object_id = params.get("object_id")
            if not _is_valid_object_id(object_id):
                raise _BridgeCommandError(
                    "INVALID_PARAMS",
                    "get_object requires one canonical object_id",
                )
            return _read_object(doc, object_id)
    except _BridgeCommandError:
        raise
    except Exception:
        raise _BridgeCommandError(
            "C4D_API_ERROR",
            "Cinema 4D could not complete the read-only inspection",
        )
    raise _BridgeCommandError("UNKNOWN_COMMAND", "Unsupported read command")


def _plugin_label(plugin):
    """Return non-sensitive plugin registry labels, tolerating incomplete entries."""
    labels = []
    try:
        name = plugin.GetName()
        if name:
            labels.append(str(name))
    except Exception:
        pass
    try:
        filename = plugin.GetFilename()
        if filename:
            labels.append(os.path.basename(str(filename)))
    except Exception:
        pass
    return " | ".join(labels)


def _detect_octane_on_main_thread():
    """Detect Octane by the documented C4D plugin registry, never guessed IDs."""
    try:
        plugins = c4d.plugins.FilterPluginList(c4d.PLUGINTYPE_ANY, True)
    except Exception:
        return {
            "installed": None,
            "version": None,
            "detection": "unverified",
            "evidence": [],
        }

    matches = []
    for plugin in plugins or []:
        label = _plugin_label(plugin)
        normalized = label.lower()
        if "octane" in normalized or "c4doctane" in normalized:
            matches.append(label)

    if not matches:
        return {
            "installed": False,
            "version": None,
            "detection": "plugin_registry_not_found",
            "evidence": [],
        }

    # C4D's registry proves that an Octane-named plugin is loaded, but it does
    # not expose a documented Octane release version. Do not parse the C4D
    # compatibility number from the binary filename as an Octane version.
    return {
        "installed": True,
        "version": None,
        "detection": "installed_version_unverified",
        "evidence": sorted(set(matches))[:10],
    }


class _MainThreadTask:
    """A cancellable unit of work consumed by the Cinema 4D main thread."""

    def __init__(self, server, command, params, request_id):
        self.server = server
        self.command = command
        self.params = params
        self.request_id = request_id
        self.state = "queued"
        self.result = None
        self.event = threading.Event()
        self.lock = threading.Lock()

    def cancel_if_queued(self):
        with self.lock:
            if self.state != "queued":
                return False
            self.state = "cancelled"
            return True

    def cancel_for_shutdown(self):
        """Cancel queued work and release its waiting socket thread."""
        with self.lock:
            if self.state != "queued":
                return False
            self.state = "cancelled"
            self.result = _error_envelope(
                self.request_id,
                "SERVER_STOPPING",
                "Cinema 4D bridge is stopping",
                retryable=True,
            )
            self.event.set()
            return True

    def current_state(self):
        with self.lock:
            return self.state

    def execute(self):
        """Run on the main thread; a timed-out queued task is never executed."""
        # The server lock makes the queued -> running transition atomic with
        # stop(). Once stopping owns this lock, no queued task can begin.
        with self.server._state_lock:
            with self.lock:
                if self.state == "cancelled":
                    return False
                if self.state != "queued":
                    return False
                if self.server.is_stopping():
                    self.state = "cancelled"
                    self.result = _error_envelope(
                        self.request_id,
                        "SERVER_STOPPING",
                        "Cinema 4D bridge is stopping",
                        retryable=True,
                    )
                    self.event.set()
                    return False
                self.state = "running"

        try:
            result = self.server._dispatch_on_main_thread(
                self.command, self.params
            )
            self.result = _success_envelope(self.request_id, result)
        except _BridgeCommandError as exc:
            self.result = _error_envelope(
                self.request_id,
                exc.code,
                exc.message,
                retryable=False,
                details=exc.details,
            )
        except Exception:
            self.server.log(
                "Request {} failed inside the main-thread dispatcher".format(
                    self.request_id
                )
            )
            self.result = _error_envelope(
                self.request_id,
                "INTERNAL_ERROR",
                "Cinema 4D could not complete the request",
                retryable=False,
            )
        finally:
            with self.lock:
                self.state = "completed"
            self.event.set()
        return True


class C4DSocketServer(threading.Thread):
    """Single-flight, loopback-only socket bridge."""

    def __init__(
        self,
        msg_queue,
        port=DEFAULT_PORT,
        token=None,
        request_size_limit=DEFAULT_REQUEST_SIZE_LIMIT,
        client_timeout=DEFAULT_CLIENT_TIMEOUT,
        main_thread_timeout=DEFAULT_MAIN_THREAD_TIMEOUT,
    ):
        super(C4DSocketServer, self).__init__()
        self.host = LOOPBACK_HOST
        self.port = int(port)
        self.token = token if token is not None else os.environ.get("C4D_MCP_TOKEN")
        self._token_bytes = _validated_configured_token(self.token)
        self.request_size_limit = int(request_size_limit)
        self.client_timeout = float(client_timeout)
        self.main_thread_timeout = float(main_thread_timeout)
        self.msg_queue = msg_queue
        self.socket = None
        self.active_client = None
        self.running = False
        self.daemon = True
        self.startup_error = None
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._stopped_event = threading.Event()
        self._active_tasks = set()

        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.request_size_limit < 128:
            raise ValueError("request_size_limit must be at least 128 bytes")
        if self.client_timeout <= 0 or self.main_thread_timeout <= 0:
            raise ValueError("timeouts must be positive")

    def log(self, message):
        """Queue a redacted operational message for the main-thread UI."""
        _enqueue_main_thread_message(self.msg_queue, "LOG", str(message))

    def update_status(self, status):
        _enqueue_main_thread_message(self.msg_queue, "STATUS", status)

    def is_stopping(self):
        return self._stop_event.is_set()

    def wait_until_ready(self, timeout=2.0):
        """Wait for bind success or a controlled startup failure."""
        if not self._ready_event.wait(timeout):
            return False
        return self.running and self.startup_error is None

    def wait_until_stopped(self, timeout=2.0):
        return self._stopped_event.wait(timeout)

    def _register_active_client(self, client):
        with self._state_lock:
            if self._stop_event.is_set():
                return False
            self.active_client = client
            return True

    def _clear_active_client(self, client):
        with self._state_lock:
            if self.active_client is client:
                self.active_client = None

    def _close_socket(self, target):
        if target is None:
            return
        try:
            target.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            target.close()
        except OSError:
            pass

    def run(self):
        """Accept one request at a time so C4D work cannot race."""
        listener = None
        try:
            if self._stop_event.is_set():
                return

            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                # On Windows SO_REUSEADDR can let a second listener bind the
                # same address. Exclusive ownership makes collision handling
                # deterministic while still releasing the port on close.
                listener.setsockopt(
                    socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
                )
            else:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((LOOPBACK_HOST, self.port))
            listener.listen(4)
            listener.settimeout(0.5)
            with self._state_lock:
                if self._stop_event.is_set():
                    return
                self.socket = listener
                self.running = True
            self.update_status("Online")
            self.log("Bridge listening on {}:{}".format(LOOPBACK_HOST, self.port))
            self._ready_event.set()

            while not self._stop_event.is_set():
                try:
                    client, address = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if not self._stop_event.is_set():
                        raise
                    break

                # Binding to IPv4 loopback already prevents remote peers. Keep
                # the explicit check as a fail-closed defense in depth.
                if not address or address[0] != LOOPBACK_HOST:
                    client.close()
                    continue
                if not self._register_active_client(client):
                    self._close_socket(client)
                    break
                try:
                    self.handle_client(client)
                finally:
                    self._clear_active_client(client)
        except OSError as exc:
            code = "PORT_IN_USE" if getattr(exc, "errno", None) in (98, 48, 10048) else "STARTUP_FAILED"
            self.startup_error = _error_envelope(
                None,
                code,
                (
                    "C4D_MCP_PORT is already in use"
                    if code == "PORT_IN_USE"
                    else "Cinema 4D bridge could not start"
                ),
                retryable=False,
            )
            self.log("Bridge startup failed: {}".format(code))
        except Exception as exc:
            self.startup_error = _error_envelope(
                None,
                "STARTUP_FAILED",
                "Cinema 4D bridge could not start",
                retryable=False,
            )
            self.log("Bridge startup failed: {}".format(type(exc).__name__))
        finally:
            self._ready_event.set()
            with self._state_lock:
                self._stop_event.set()
                self.running = False
                self.socket = None
                active_client = self.active_client
                self.active_client = None
                active_tasks = list(self._active_tasks)
            for task in active_tasks:
                task.cancel_for_shutdown()
            self._close_socket(active_client)
            self._close_socket(listener)
            self.update_status("Offline")
            self._stopped_event.set()

    def stop(self, wait=True, timeout=2.0):
        """Stop listener/client/tasks and optionally wait for thread termination."""
        with self._state_lock:
            self._stop_event.set()
            self.running = False
            listener = self.socket
            active_client = self.active_client
            active_tasks = list(self._active_tasks)

        for task in active_tasks:
            task.cancel_for_shutdown()
        self._close_socket(active_client)
        self._close_socket(listener)
        self.update_status("Offline")

        if wait and self.is_alive() and threading.current_thread() is not self:
            self.join(timeout)
        return not self.is_alive()

    def handle_client(self, client):
        """Read and process exactly one bounded newline-delimited request."""
        request_id = None
        try:
            if self._stop_event.is_set():
                return
            client.settimeout(self.client_timeout)
            buffer = b""

            while b"\n" not in buffer:
                if self._stop_event.is_set():
                    return
                try:
                    chunk = client.recv(4096)
                except socket.timeout:
                    self._send_response(
                        client,
                        _error_envelope(
                            None,
                            "C4D_TIMEOUT",
                            "Timed out while reading the request",
                            retryable=True,
                        ),
                    )
                    return

                if not chunk:
                    return
                buffer += chunk
                if len(buffer) > self.request_size_limit:
                    self._send_response(
                        client,
                        _error_envelope(
                            None,
                            "FRAME_TOO_LARGE",
                            "Request exceeds the bridge size limit",
                            retryable=False,
                            details={"max_bytes": self.request_size_limit},
                        ),
                    )
                    return

            frame, trailing = buffer.split(b"\n", 1)
            if trailing.strip():
                self._send_response(
                    client,
                    _error_envelope(
                        None,
                        "INVALID_REQUEST",
                        "Only one request is allowed per connection",
                        retryable=False,
                    ),
                )
                return

            try:
                text = frame.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                self._send_response(
                    client,
                    _error_envelope(
                        None,
                        "INVALID_UTF8",
                        "Request must be valid UTF-8",
                        retryable=False,
                    ),
                )
                return

            try:
                request = json.loads(text)
            except (TypeError, ValueError):
                self._send_response(
                    client,
                    _error_envelope(
                        None,
                        "MALFORMED_JSON",
                        "Request is not valid JSON",
                        retryable=False,
                    ),
                )
                return

            validation_error = self._validate_request(request)
            if validation_error is not None:
                request_id = request.get("request_id") if isinstance(request, dict) else None
                error_code, error_message = validation_error
                self._send_response(
                    client,
                    _error_envelope(
                        request_id,
                        error_code,
                        error_message,
                        retryable=False,
                    ),
                )
                return

            request_id = request["request_id"]
            supplied_token = request["token"]
            try:
                supplied_token_bytes = supplied_token.encode("ascii", errors="strict")
            except UnicodeEncodeError:
                self._send_response(
                    client,
                    _error_envelope(
                        request_id,
                        "INVALID_REQUEST",
                        "token must use URL-safe ASCII characters",
                        retryable=False,
                    ),
                )
                return
            if not hmac.compare_digest(supplied_token_bytes, self._token_bytes):
                self._send_response(
                    client,
                    _error_envelope(
                        request_id,
                        "AUTH_FAILED",
                        "Authentication failed",
                        retryable=False,
                    ),
                )
                return

            if self._stop_event.is_set():
                return
            command = request["command"]
            if command not in ALLOWED_COMMANDS:
                self._send_response(
                    client,
                    _error_envelope(
                        request_id,
                        "UNKNOWN_COMMAND",
                        "Command is not available in Phase 2A",
                        retryable=False,
                    ),
                )
                return

            if self._stop_event.is_set():
                return
            response = self.execute_on_main_thread(
                command,
                request["params"],
                request_id,
            )
            self._send_response(client, response)
        except (ConnectionError, OSError):
            # Client disconnects are isolated to this connection.
            return
        except Exception:
            try:
                self._send_response(
                    client,
                    _error_envelope(
                        request_id,
                        "INTERNAL_ERROR",
                        "Bridge could not process the request",
                        retryable=False,
                    ),
                )
            except Exception:
                pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    def _validate_request(self, request):
        if not isinstance(request, dict):
            return ("INVALID_REQUEST", "Request must be a JSON object")

        if (
            "protocol_version" in request
            and request.get("protocol_version") != PROTOCOL_VERSION
        ):
            return ("PROTOCOL_MISMATCH", "Unsupported protocol_version")
        if "token" not in request or request.get("token") == "":
            return ("AUTH_REQUIRED", "A non-empty token is required")

        expected_keys = {
            "protocol_version",
            "request_id",
            "command",
            "token",
            "params",
        }
        if set(request.keys()) != expected_keys:
            return (
                "INVALID_REQUEST",
                "Request fields do not match the bridge protocol",
            )

        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            return (
                "INVALID_REQUEST",
                "request_id must be a non-empty string of at most 128 characters",
            )
        if not isinstance(request.get("command"), str):
            return ("INVALID_REQUEST", "command must be a string")
        if not isinstance(request.get("token"), str):
            return ("INVALID_REQUEST", "token must be a string")
        try:
            request["token"].encode("ascii", errors="strict")
        except UnicodeEncodeError:
            return (
                "INVALID_REQUEST",
                "token must use URL-safe ASCII characters",
            )
        if any(character not in TOKEN_ALPHABET for character in request["token"]):
            return (
                "INVALID_REQUEST",
                "token must use URL-safe ASCII characters",
            )
        if not isinstance(request.get("params"), dict):
            return ("INVALID_REQUEST", "params must be an object")
        if request.get("command") == "get_object":
            params = request["params"]
            if set(params.keys()) != {"object_id"} or not _is_valid_object_id(
                params.get("object_id")
            ):
                return (
                    "INVALID_PARAMS",
                    "get_object requires one canonical object_id",
                )
        elif request["params"]:
            return (
                "INVALID_REQUEST",
                "This command does not accept parameters",
            )
        return None

    def _send_response(self, client, response):
        payload = json.dumps(
            response,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8") + b"\n"
        client.sendall(payload)

    def execute_on_main_thread(self, command, params, request_id):
        if self._stop_event.is_set():
            return _error_envelope(
                request_id,
                "SERVER_STOPPING",
                "Cinema 4D bridge is stopping",
                retryable=True,
            )

        task = _MainThreadTask(self, command, params, request_id)
        with self._state_lock:
            if self._stop_event.is_set():
                task.cancel_for_shutdown()
                return task.result
            self._active_tasks.add(task)
        _enqueue_main_thread_message(self.msg_queue, "EXEC", task.execute)

        try:
            if not task.event.wait(self.main_thread_timeout):
                if task.cancel_if_queued():
                    return _error_envelope(
                        request_id,
                        "MAIN_THREAD_TIMEOUT",
                        "Cinema 4D main thread did not start the request in time",
                        retryable=False,
                    )
                return _error_envelope(
                    request_id,
                    "OUTCOME_UNKNOWN",
                    "Cinema 4D began the request but did not finish before the timeout",
                    retryable=False,
                )
            return task.result
        finally:
            with self._state_lock:
                self._active_tasks.discard(task)

    def _dispatch_on_main_thread(self, command, params):
        """The only entry point allowed to call Cinema 4D APIs."""
        if hasattr(c4d, "threading") and not c4d.threading.GeIsMainThread():
            raise RuntimeError("dispatcher is not running on the main thread")

        if command == "ping":
            return {
                "status": "ok",
                "protocol_version": PROTOCOL_VERSION,
                "bridge_version": BRIDGE_VERSION,
                "cinema4d": {"responsive": True},
            }
        if command == "get_capabilities":
            raw_c4d_version = c4d.GetC4DVersion()
            c4d_version = _format_c4d_version(raw_c4d_version)
            python_version = "{}.{}.{}".format(
                sys.version_info[0],
                sys.version_info[1],
                sys.version_info[2],
            )
            return {
                "protocol_version": PROTOCOL_VERSION,
                "bridge_version": BRIDGE_VERSION,
                "cinema4d": {
                    "version": c4d_version,
                    "version_raw": raw_c4d_version,
                    "python_version": python_version,
                    "compatibility": (
                        "target" if c4d_version == "2023.2.2" else "unverified"
                    ),
                },
                "tools": list(ACTIVE_COMMAND_NAMES),
                "features": {
                    "scene_read": True,
                    "object_operations": False,
                    "undo": False,
                    "save": False,
                    "animation": False,
                    "camera": False,
                    "light": False,
                    "mograph": False,
                    "octane_commands": False,
                    "arbitrary_python": False,
                    "remote_transport": False,
                },
                "security": {
                    "authenticated": True,
                    "loopback_only": True,
                    "request_size_limit": self.request_size_limit,
                },
                "renderers": {
                    "octane": _detect_octane_on_main_thread(),
                },
            }
        if command in ("get_scene_info", "list_objects", "get_object"):
            return _dispatch_read_command(command, params)
        raise ValueError("unsupported Phase 2A command")


class SocketServerDialog(gui.GeDialog):
    """Status dialog and main-thread receiver for explicit bridge wake-ups."""

    STATUS_TEXT_ID = 1002
    ENDPOINT_TEXT_ID = 1003
    AUTH_TEXT_ID = 1005
    LOG_BOX_ID = 1004
    START_BUTTON_ID = 1011
    STOP_BUTTON_ID = 1012

    def __init__(self):
        super(SocketServerDialog, self).__init__()
        self.server = None
        self.msg_queue = queue.Queue()

    def CreateLayout(self):
        self.SetTitle("Cinema 4D MCP Phase 2A Bridge")
        self.AddStaticText(
            self.STATUS_TEXT_ID,
            c4d.BFH_SCALEFIT,
            name="Server: Offline",
        )
        self.AddStaticText(
            self.ENDPOINT_TEXT_ID,
            c4d.BFH_SCALEFIT,
            name="Endpoint: {}:{}".format(LOOPBACK_HOST, self._display_port()),
        )
        self.AddStaticText(
            self.AUTH_TEXT_ID,
            c4d.BFH_SCALEFIT,
            name="Token configured: {}".format(
                "Yes" if os.environ.get("C4D_MCP_TOKEN") else "No"
            ),
        )

        self.GroupBegin(1010, c4d.BFH_SCALEFIT, 2, 1)
        self.AddButton(self.START_BUTTON_ID, c4d.BFH_SCALE, name="Start Server")
        self.AddButton(self.STOP_BUTTON_ID, c4d.BFH_SCALE, name="Stop Server")
        self.GroupEnd()

        self.AddMultiLineEditText(
            self.LOG_BOX_ID,
            c4d.BFH_SCALEFIT,
            initw=440,
            inith=220,
            style=c4d.DR_MULTILINE_READONLY,
        )
        self.Enable(self.STOP_BUTTON_ID, False)
        return True

    def InitValues(self):
        """Start only the low-frequency lifecycle fallback after GUI init."""
        self.SetTimer(500)
        return True

    def _display_port(self):
        try:
            return _configured_port()
        except ValueError:
            return "invalid"

    def Command(self, message_id, message):
        if message_id == self.START_BUTTON_ID:
            self.StartServer()
            return True
        if message_id == self.STOP_BUTTON_ID:
            self.StopServer()
            return True
        return False

    def CoreMessage(self, message_id, message):
        if message_id == MAIN_THREAD_EVENT_ID:
            self._drain_messages()
            self._release_finished_server_reference()
            return True
        return c4d.gui.GeDialog.CoreMessage(self, message_id, message)

    def Timer(self, message):
        """Lifecycle fallback only; request dispatch uses CoreMessage()."""
        self._release_finished_server_reference()
        return True

    def _release_finished_server_reference(self):
        if (
            self.server is not None
            and not self.server.running
            and not self.server.is_alive()
        ):
            self.server = None
            self.UpdateStatusText("Offline")

    def _drain_messages(self):
        while True:
            try:
                message_type, value = self.msg_queue.get_nowait()
            except queue.Empty:
                break

            if message_type == "EXEC":
                if callable(value):
                    value()
            elif message_type == "STATUS":
                self.UpdateStatusText(value)
            elif message_type == "LOG":
                self.AppendLog(value)

    def UpdateStatusText(self, status):
        self.SetString(self.STATUS_TEXT_ID, "Server: {}".format(status))
        online = status == "Online"
        self.Enable(self.START_BUTTON_ID, not online)
        self.Enable(self.STOP_BUTTON_ID, online)

    def AppendLog(self, message):
        existing = self.GetString(self.LOG_BOX_ID)
        combined = (existing + "\n" + str(message)).strip()
        self.SetString(self.LOG_BOX_ID, combined)

    def StartServer(self):
        if self.server is not None:
            return

        token = os.environ.get("C4D_MCP_TOKEN")
        if not token:
            self.AppendLog("C4D_MCP_TOKEN is required; server was not started")
            self.UpdateStatusText("Offline")
            self.SetString(self.AUTH_TEXT_ID, "Token configured: No")
            return

        try:
            port = _configured_port()
            self.server = C4DSocketServer(
                msg_queue=self.msg_queue,
                port=port,
                token=token,
            )
        except ValueError as exc:
            self.AppendLog(str(exc))
            self.UpdateStatusText("Offline")
            return

        self.SetString(self.AUTH_TEXT_ID, "Token configured: Yes")
        self.server.start()
        if not self.server.wait_until_ready(timeout=2.0):
            startup_error = self.server.startup_error
            if startup_error is not None:
                error = startup_error["error"]
                self.AppendLog(
                    "Bridge start failed: {} - {}".format(
                        error["code"], error["message"]
                    )
                )
            else:
                self.AppendLog("Bridge start timed out")
            self.server.stop(wait=True, timeout=2.0)
            self.server = None
            self.UpdateStatusText("Offline")

    def StopServer(self):
        if self.server is not None:
            server = self.server
            terminated = server.stop(wait=True, timeout=2.0)
            if terminated:
                self.server = None
            else:
                self.AppendLog("Bridge thread did not terminate within 2 seconds")
        self.UpdateStatusText("Offline")


class SocketServerPlugin(c4d.plugins.CommandData):
    def __init__(self):
        self.dialog = None

    def Execute(self, document):
        if self.dialog is None:
            self.dialog = SocketServerDialog()
        return self.dialog.Open(
            dlgtype=c4d.DLG_TYPE_ASYNC,
            pluginid=PLUGIN_ID,
            defaultw=440,
            defaulth=300,
        )

    def GetState(self, document):
        return c4d.CMD_ENABLED


if __name__ == "__main__":
    c4d.plugins.RegisterCommandPlugin(
        PLUGIN_ID,
        PLUGIN_NAME,
        0,
        None,
        "Secure localhost-only MCP Phase 1 bridge",
        SocketServerPlugin(),
    )
