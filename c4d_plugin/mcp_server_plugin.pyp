"""Secure Phase 1 Cinema 4D MCP bridge for Cinema 4D 2023.2.2.

The socket thread performs transport validation and authentication only. The
two allowed commands are executed by the dialog timer on Cinema 4D's main
thread. No scene, object, renderer-control, file, or arbitrary-Python command
is present in this Phase 1 runtime.
"""

import hmac
import json
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
PLUGIN_NAME = "Cinema 4D MCP Phase 1 Bridge"

PROTOCOL_VERSION = 1
BRIDGE_VERSION = "0.2.0-phase1"
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 5555
DEFAULT_REQUEST_SIZE_LIMIT = 64 * 1024
DEFAULT_CLIENT_TIMEOUT = 5.0
DEFAULT_MAIN_THREAD_TIMEOUT = 5.0
ALLOWED_COMMANDS = frozenset(("ping", "get_capabilities"))


def _success_envelope(request_id, result):
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": True,
        "result": result,
        "error": None,
    }


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


def _configured_port():
    raw_value = os.environ.get("C4D_MCP_PORT", str(DEFAULT_PORT))
    try:
        port = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError("C4D_MCP_PORT must be an integer")
    if not 1 <= port <= 65535:
        raise ValueError("C4D_MCP_PORT must be between 1 and 65535")
    return port


def _format_c4d_version(raw_version):
    """Format the numeric Cinema 4D 2023-style version without hiding raw data."""
    if not isinstance(raw_version, int) or raw_version < 2000000:
        return str(raw_version)

    year = raw_version // 1000
    revision = raw_version % 1000
    minor = revision // 100
    patch = (revision % 100) // 10
    build = revision % 10
    parts = [str(year), str(minor), str(patch)]
    if build:
        parts.append(str(build))
    return ".".join(parts)


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

    def current_state(self):
        with self.lock:
            return self.state

    def execute(self):
        """Run on the main thread; a timed-out queued task is never executed."""
        with self.lock:
            if self.state == "cancelled":
                return False
            if self.state != "queued":
                return False
            self.state = "running"

        try:
            result = self.server._dispatch_on_main_thread(
                self.command, self.params
            )
            self.result = _success_envelope(self.request_id, result)
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
        self.request_size_limit = int(request_size_limit)
        self.client_timeout = float(client_timeout)
        self.main_thread_timeout = float(main_thread_timeout)
        self.msg_queue = msg_queue
        self.socket = None
        self.running = False
        self.daemon = True

        if not self.token:
            raise ValueError("C4D_MCP_TOKEN is required")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.request_size_limit < 128:
            raise ValueError("request_size_limit must be at least 128 bytes")
        if self.client_timeout <= 0 or self.main_thread_timeout <= 0:
            raise ValueError("timeouts must be positive")

    def log(self, message):
        """Queue a redacted operational message for the main-thread UI."""
        self.msg_queue.put(("LOG", str(message)))

    def update_status(self, status):
        self.msg_queue.put(("STATUS", status))

    def run(self):
        """Accept one request at a time so C4D work cannot race."""
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket = listener
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((LOOPBACK_HOST, self.port))
            listener.listen(4)
            listener.settimeout(0.5)
            self.running = True
            self.update_status("Online")
            self.log("Bridge listening on {}:{}".format(LOOPBACK_HOST, self.port))

            while self.running:
                try:
                    client, address = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.running:
                        raise
                    break

                # Binding to IPv4 loopback already prevents remote peers. Keep
                # the explicit check as a fail-closed defense in depth.
                if not address or address[0] != LOOPBACK_HOST:
                    client.close()
                    continue
                self.handle_client(client)
        except Exception as exc:
            self.log("Bridge stopped: {}".format(type(exc).__name__))
        finally:
            self.running = False
            self.update_status("Offline")
            if self.socket is not None:
                try:
                    self.socket.close()
                except OSError:
                    pass
                self.socket = None

    def stop(self):
        self.running = False
        listener = self.socket
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        self.update_status("Offline")

    def handle_client(self, client):
        """Read and process exactly one bounded newline-delimited request."""
        request_id = None
        try:
            client.settimeout(self.client_timeout)
            buffer = b""

            while b"\n" not in buffer:
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
                            "Request exceeds the Phase 1 size limit",
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
                self._send_response(
                    client,
                    _error_envelope(
                        request_id,
                        "INVALID_REQUEST",
                        validation_error,
                        retryable=False,
                    ),
                )
                return

            request_id = request["request_id"]
            supplied_token = request["token"]
            if not hmac.compare_digest(str(supplied_token), str(self.token)):
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

            command = request["command"]
            if command not in ALLOWED_COMMANDS:
                self._send_response(
                    client,
                    _error_envelope(
                        request_id,
                        "UNKNOWN_COMMAND",
                        "Command is not available in Phase 1",
                        retryable=False,
                    ),
                )
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
            return "Request must be a JSON object"

        expected_keys = {
            "protocol_version",
            "request_id",
            "command",
            "token",
            "params",
        }
        if set(request.keys()) != expected_keys:
            return "Request fields do not match the Phase 1 protocol"
        if request.get("protocol_version") != PROTOCOL_VERSION:
            return "Unsupported protocol_version"

        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            return "request_id must be a non-empty string of at most 128 characters"
        if not isinstance(request.get("command"), str):
            return "command must be a string"
        if not isinstance(request.get("token"), str) or not request.get("token"):
            return "token must be a non-empty string"
        if not isinstance(request.get("params"), dict):
            return "params must be an object"
        if request["params"]:
            return "Phase 1 commands do not accept parameters"
        return None

    def _send_response(self, client, response):
        payload = json.dumps(
            response,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8") + b"\n"
        client.sendall(payload)

    def execute_on_main_thread(self, command, params, request_id):
        task = _MainThreadTask(self, command, params, request_id)
        self.msg_queue.put(("EXEC", task.execute))

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

    def _dispatch_on_main_thread(self, command, params):
        """The only Phase 1 entry point allowed to call Cinema 4D APIs."""
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
                "tools": ["ping", "get_capabilities"],
                "features": {
                    "scene_read": False,
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
        raise ValueError("unsupported Phase 1 command")


class SocketServerDialog(gui.GeDialog):
    """Small status dialog whose timer is the main-thread dispatcher."""

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
        self.SetTimer(50)

    def CreateLayout(self):
        self.SetTitle("Cinema 4D MCP Phase 1 Bridge")
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

    def Timer(self, message):
        self._drain_messages()
        if (
            self.server is not None
            and not self.server.running
            and not self.server.is_alive()
        ):
            self.server = None
            self.UpdateStatusText("Offline")
        return True

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

    def StopServer(self):
        if self.server is not None:
            self.server.stop()
            self.server = None
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
