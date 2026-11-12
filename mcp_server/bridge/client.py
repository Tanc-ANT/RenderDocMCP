"""Per-instance, per-request file IPC client for the RenderDoc extension."""

import json
import os
from pathlib import Path
import tempfile
import time
import uuid
from typing import Any

IPC_DIR = os.environ.get(
    "RENDERDOC_MCP_IPC_DIR", os.path.join(tempfile.gettempdir(), "renderdoc_mcp")
)
PROTOCOL_VERSION = 2
INSTANCE_MAX_AGE = 120.0


class RenderDocBridgeError(Exception):
    """Error communicating with the RenderDoc bridge."""


class RenderDocBridge:
    def __init__(self, host: str = "127.0.0.1", port: int = 19876):
        # Retained for configuration compatibility; transport is file-based.
        self.host = host
        self.port = port
        self.timeout = 30.0
        self.ipc_dir = Path(IPC_DIR)
        self.instance_id = os.environ.get("RENDERDOC_MCP_INSTANCE_ID") or None

    def list_instances(self) -> dict:
        """Discover live protocol-v2 bridges without changing the target."""
        instances = []
        for manifest in sorted((self.ipc_dir / "instances").glob("*/instance.json")):
            try:
                with manifest.open(encoding="utf-8") as stream:
                    info = json.load(stream)
                if (
                    info.get("protocol_version") == PROTOCOL_VERSION
                    and info.get("instance_id") == manifest.parent.name
                    and 0 <= time.time() - float(info["heartbeat"]) <= INSTANCE_MAX_AGE
                ):
                    instances.append(info)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
        return {"count": len(instances), "instances": instances}

    def _resolve_instance(self, instance_id: str | None) -> str:
        target = instance_id if instance_id is not None else self.instance_id
        instances = self.list_instances()["instances"]
        if target is not None:
            if any(info["instance_id"] == target for info in instances):
                return target
            raise RenderDocBridgeError(
                f"RenderDoc instance {target!r} is unavailable. "
                "Call list_renderdoc_instances to find current instance IDs."
            )
        if len(instances) == 1:
            return instances[0]["instance_id"]
        if not instances:
            raise RenderDocBridgeError(
                "No active RenderDoc MCP Bridge v2 instance found. Reload the updated "
                "extension in each RenderDoc window and call list_renderdoc_instances."
            )
        choices = ", ".join(
            f"{info['instance_id']} ({info.get('filename') or 'no capture'})"
            for info in instances
        )
        raise RenderDocBridgeError(
            "Multiple RenderDoc windows are available. Pass instance_id explicitly "
            f"(or configure RENDERDOC_MCP_INSTANCE_ID). Available: {choices}"
        )

    def call(
        self, method: str, params: dict[str, Any] | None = None,
        instance_id: str | None = None,
    ) -> Any:
        """Send one isolated request; never fall back to a different window."""
        target = self._resolve_instance(instance_id)
        instance_dir = self.ipc_dir / "instances" / target
        request_id = uuid.uuid4().hex
        request_file = instance_dir / "requests" / (request_id + ".json")
        temporary_file = request_file.with_suffix(".tmp")
        response_file = instance_dir / "responses" / (request_id + ".json")
        deadline = time.monotonic() + self.timeout
        request = {
            "id": request_id, "method": method, "params": params or {},
            "expires_at": time.time() + self.timeout,
        }
        try:
            # Only completed files have a .json suffix visible to the reader.
            with temporary_file.open("x", encoding="utf-8") as stream:
                json.dump(request, stream, allow_nan=False)
            os.replace(temporary_file, request_file)
            while time.monotonic() < deadline:
                try:
                    with response_file.open(encoding="utf-8") as stream:
                        response = json.load(stream)
                except FileNotFoundError:
                    time.sleep(0.02)
                    continue
                if not isinstance(response, dict) or response.get("id") != request_id:
                    raise RenderDocBridgeError("Response ID does not match the request")
                if "error" in response:
                    error = response["error"]
                    raise RenderDocBridgeError(f"[{error['code']}] {error['message']}")
                if "result" not in response:
                    raise RenderDocBridgeError("Response has no result or error")
                return response["result"]
            raise RenderDocBridgeError(f"Request timed out for RenderDoc instance {target}")
        except RenderDocBridgeError:
            raise
        except Exception as exc:
            raise RenderDocBridgeError(f"Communication error: {exc}") from exc
        finally:
            # Only this call's files; never delete another client's response.
            for path in (temporary_file, request_file, response_file):
                try:
                    path.unlink()
                except OSError:
                    pass
