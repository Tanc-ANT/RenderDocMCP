"""Isolated file IPC for RenderDoc's socket-less embedded Python (3.6+)."""

import json
import os
import tempfile
import time
import traceback
import uuid

from PySide2.QtCore import QObject, QTimer


IPC_DIR = os.environ.get(
    "RENDERDOC_MCP_IPC_DIR", os.path.join(tempfile.gettempdir(), "renderdoc_mcp")
)
PROTOCOL_VERSION = 2


def _write_json(path, value):
    """Publish a closed, complete file; readers never see partial JSON."""
    temporary = path + "." + uuid.uuid4().hex + ".tmp"
    try:
        with open(temporary, "x", encoding="utf-8") as stream:
            json.dump(value, stream, allow_nan=False)
        os.replace(temporary, path)
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass


class MCPBridgeServer(QObject):
    def __init__(self, host, port, handler, parent=None, metadata_provider=None):
        super(MCPBridgeServer, self).__init__(parent)
        self.handler = handler
        self.metadata_provider = metadata_provider
        self.instance_id = "%s-%s" % (os.getpid(), uuid.uuid4().hex)
        self.ipc_dir = os.path.join(IPC_DIR, "instances", self.instance_id)
        self.requests_dir = os.path.join(self.ipc_dir, "requests")
        self.responses_dir = os.path.join(self.ipc_dir, "responses")
        self.manifest = os.path.join(self.ipc_dir, "instance.json")
        self._timer = None
        self._running = False
        self._polling = False
        self._started_at = time.time()
        self._last_heartbeat = 0

    def start(self):
        if self._running:
            return True
        os.makedirs(self.requests_dir, exist_ok=True)
        os.makedirs(self.responses_dir, exist_ok=True)
        self._publish_instance()
        self._running = True
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_request)
        self._timer.start(100)
        print("[MCP Bridge] Instance: %s" % self.instance_id)
        print("[MCP Bridge] IPC directory: %s" % self.ipc_dir)
        return True

    def stop(self):
        self._running = False
        if self._timer:
            self._timer.stop()
            self._timer = None
        # Withdraw only this instance. Other windows and in-flight clients are untouched.
        try:
            os.remove(self.manifest)
        except OSError:
            pass
        print("[MCP Bridge] Instance stopped: %s" % self.instance_id)

    def is_running(self):
        return self._running

    def _publish_instance(self):
        info = {}
        if self.metadata_provider:
            info.update(self.metadata_provider())
        info.update({
            "instance_id": self.instance_id,
            "pid": os.getpid(),
            "protocol_version": PROTOCOL_VERSION,
            "started_at": self._started_at,
            "heartbeat": time.time(),
        })
        _write_json(self.manifest, info)
        self._last_heartbeat = time.time()

    def _cleanup_expired_files(self):
        # Late responses and abandoned requests must not grow without bound.
        cutoff = time.time() - 300
        for directory in (self.requests_dir, self.responses_dir):
            for name in os.listdir(directory):
                path = os.path.join(directory, name)
                try:
                    if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except OSError:
                    pass

    def _poll_request(self):
        # BlockInvoke can pump UI events; do not re-enter the request handler.
        if not self._running or self._polling:
            return
        self._polling = True
        try:
            if time.time() - self._last_heartbeat >= 2:
                self._publish_instance()
                self._cleanup_expired_files()
            for name in sorted(os.listdir(self.requests_dir)):
                if not name.endswith(".json"):
                    continue
                request_id = name[:-5]
                if len(request_id) != 32 or any(c not in "0123456789abcdef" for c in request_id):
                    continue
                path = os.path.join(self.requests_dir, name)
                claimed = path + ".processing"
                try:
                    # Claim before reading. A client may withdraw an expired request.
                    os.replace(path, claimed)
                except FileNotFoundError:
                    continue
                try:
                    with open(claimed, "r", encoding="utf-8") as stream:
                        request = json.load(stream)
                    if request.get("id") != request_id:
                        raise ValueError("Request ID does not match filename")
                    expires_at = float(request["expires_at"])
                    if expires_at <= time.time():
                        continue
                    try:
                        response = self.handler.handle(request)
                    except Exception as exc:
                        traceback.print_exc()
                        response = {"id": request_id, "error": {
                            "code": -32603, "message": str(exc),
                        }}
                    if self._running and time.time() < expires_at:
                        _write_json(os.path.join(self.responses_dir, name), response)
                except Exception as exc:
                    traceback.print_exc()
                    _write_json(os.path.join(self.responses_dir, name), {
                        "id": request_id,
                        "error": {"code": -32603, "message": str(exc)},
                    })
                finally:
                    try:
                        os.remove(claimed)
                    except OSError:
                        pass
                break  # One request per tick keeps the UI responsive between calls.
        except Exception:
            traceback.print_exc()
        finally:
            self._polling = False
