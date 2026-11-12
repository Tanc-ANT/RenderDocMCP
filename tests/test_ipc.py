"""Transport regression tests; no RenderDoc, GPU, Qt or build tools required."""

import concurrent.futures
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from mcp_server.bridge.client import RenderDocBridge, RenderDocBridgeError


class FakeTimer:
    def __init__(self, parent):
        self.timeout = types.SimpleNamespace(connect=lambda callback: None)

    def start(self, interval):
        pass

    def stop(self):
        pass


qt = types.ModuleType("PySide2.QtCore")
qt.QObject = type("QObject", (), {"__init__": lambda self, parent=None: None})
qt.QTimer = FakeTimer
spec = importlib.util.spec_from_file_location(
    "ipc_server_under_test",
    Path(__file__).resolve().parents[1] / "renderdoc_extension" / "socket_server.py",
)
server_module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"PySide2": types.ModuleType("PySide2"), "PySide2.QtCore": qt}):
    spec.loader.exec_module(server_module)


class Handler:
    def __init__(self, filename):
        self.filename = filename

    def handle(self, request):
        return {"id": request["id"], "result": {
            "filename": self.filename, "params": request["params"],
        }}


class IPCTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.server_root = patch.object(server_module, "IPC_DIR", self.directory.name)
        self.server_root.start()
        self.servers = []
        self.stop_polling = threading.Event()
        self.poll_thread = None

    def tearDown(self):
        self.stop_polling.set()
        if self.poll_thread:
            self.poll_thread.join(5)
        for server in self.servers:
            server.stop()
        self.server_root.stop()
        self.directory.cleanup()

    def server(self, filename):
        server = server_module.MCPBridgeServer(
            "", 0, Handler(filename), metadata_provider=lambda: {"filename": filename},
        )
        server.start()
        self.servers.append(server)
        return server

    def bridge(self):
        bridge = RenderDocBridge()
        bridge.ipc_dir = self.root
        bridge.instance_id = None
        bridge.timeout = 3
        return bridge

    def poll(self):
        def run():
            while not self.stop_polling.wait(0.001):
                for server in self.servers:
                    server._poll_request()
        self.poll_thread = threading.Thread(target=run)
        self.poll_thread.start()

    def test_concurrent_clients_and_windows_large_unicode_payloads(self):
        first = self.server("Mobile.rdc")
        second = self.server("PC.rdc")
        self.poll()

        def query(index):
            server = first if index % 2 == 0 else second
            payload = {"index": index, "text": "截帧测试" * 20000}
            result = self.bridge().call("echo", payload, server.instance_id)
            self.assertEqual(result, {"filename": server.handler.filename, "params": payload})
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(query, range(48)))
        for server in self.servers:
            self.assertEqual(list(Path(server.requests_dir).iterdir()), [])
            self.assertEqual(list(Path(server.responses_dir).iterdir()), [])

    def test_multiple_windows_require_target_and_never_fallback(self):
        self.server("Mobile.rdc")
        self.server("PC.rdc")
        with self.assertRaisesRegex(RenderDocBridgeError, "Multiple RenderDoc"):
            self.bridge().call("echo")
        with self.assertRaisesRegex(RenderDocBridgeError, "unavailable"):
            self.bridge().call("echo", instance_id="../other")

    def test_single_window_automatic_and_configured_target(self):
        server = self.server("one.rdc")
        self.poll()
        bridge = self.bridge()
        self.assertEqual(bridge.call("echo")["filename"], "one.rdc")
        self.server("two.rdc")
        bridge.instance_id = server.instance_id
        self.assertEqual(bridge.call("echo")["filename"], "one.rdc")

    def test_mismatched_response_id_is_rejected(self):
        server = self.server("one.rdc")
        server.handler.handle = lambda request: {"id": "wrong", "result": {}}
        self.poll()
        with self.assertRaisesRegex(RenderDocBridgeError, "Response ID"):
            self.bridge().call("echo")

    def test_stop_does_not_remove_other_instance(self):
        first = self.server("one.rdc")
        second = self.server("two.rdc")
        first.stop()
        instances = self.bridge().list_instances()["instances"]
        self.assertEqual([info["instance_id"] for info in instances], [second.instance_id])

    def test_stale_and_invalid_manifests_ignored(self):
        server = self.server("old.rdc")
        info = json.loads(Path(server.manifest).read_text(encoding="utf-8"))
        info["heartbeat"] = time.time() - 3600
        server_module._write_json(server.manifest, info)
        self.assertEqual(self.bridge().list_instances()["count"], 0)
        Path(server.manifest).write_text('{"partial":', encoding="utf-8")
        self.assertEqual(self.bridge().list_instances()["count"], 0)

    def test_timeout_cleans_only_own_files_and_ignores_unpublished_requests(self):
        server = self.server("one.rdc")
        unfinished = Path(server.requests_dir) / ("a" * 32 + ".tmp")
        unfinished.write_text('{"partial":', encoding="utf-8")
        bridge = self.bridge()
        bridge.timeout = 0.05
        with self.assertRaisesRegex(RenderDocBridgeError, "timed out"):
            bridge.call("echo")
        server._poll_request()
        self.assertEqual(list(Path(server.requests_dir).iterdir()), [unfinished])

    def test_expired_requests_not_executed_and_old_orphans_removed(self):
        server = self.server("one.rdc")
        request_id = "b" * 32
        path = Path(server.requests_dir) / (request_id + ".json")
        server_module._write_json(str(path), {
            "id": request_id, "expires_at": time.time() - 1,
            "method": "echo", "params": {},
        })
        server.handler.handle = lambda request: self.fail("Expired request executed")
        server._poll_request()
        self.assertFalse(path.exists())
        self.assertEqual(list(Path(server.responses_dir).iterdir()), [])
        orphan = Path(server.responses_dir) / "orphan.json"
        orphan.write_text("{}")
        import os
        os.utime(orphan, (0, 0))
        server._cleanup_expired_files()
        self.assertFalse(orphan.exists())


if __name__ == "__main__":
    unittest.main()
