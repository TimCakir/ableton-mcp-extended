#!/usr/bin/env python3
"""Reproduce review findings without connecting to or changing Ableton Live.

Run with the repository's Python environment:
    .venv/bin/python docs/review-evidence/transport_repros.py

These assertions confirm the reviewed defects are present. After fixing the
defects, invert the relevant assertions to make regression tests.
"""

import asyncio
import importlib.metadata
import importlib.util
import logging
from pathlib import Path
import queue
import sys
import time
import types
from unittest.mock import patch


sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import MCP_Server.server as server
from mcp import types as mcp_types


logging.disable(logging.CRITICAL)


def load_remote_script():
    """Import the real remote source with a non-Live ControlSurface stub."""
    class StubControlSurface:
        pass

    framework = types.ModuleType("_Framework")
    control_surface = types.ModuleType("_Framework.ControlSurface")
    control_surface.ControlSurface = StubControlSurface
    spec = importlib.util.spec_from_file_location(
        "ableton_review_remote", REPO / "AbletonMCP_Remote_Script/__init__.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        "_Framework": framework,
        "_Framework.ControlSurface": control_surface,
        "ableton_review_remote": module,
    }):
        spec.loader.exec_module(module)
    return module


def reproduce_late_write(remote):
    script = remote.AbletonMCP.__new__(remote.AbletonMCP)
    script.log_message = lambda message: None
    scheduled = []
    mutations = []
    script.schedule_message = lambda ticks, callback: scheduled.append(callback)
    script._create_midi_track = (
        lambda index: mutations.append(index) or {"name": "mock track"}
    )

    class ExpiredQueue:
        def get(self, timeout):
            assert timeout == 10.0
            raise queue.Empty()

        def put(self, value):
            pass

    with patch.object(remote.queue, "Queue", ExpiredQueue):
        result = script._process_command({
            "type": "create_midi_track", "params": {"index": -1}
        })

    assert result["status"] == "error", result
    assert result["message"] == "Timeout waiting for operation to complete", result
    assert mutations == [], mutations
    assert len(scheduled) == 1, scheduled
    scheduled[0]()
    assert mutations == [-1], mutations
    print("PASS timeout-late-write: error returned before callback; callback still mutated mock track list")


async def reproduce_mcp_error_and_blocking():
    handler = server.mcp._mcp_server.request_handlers[mcp_types.CallToolRequest]
    request = mcp_types.CallToolRequest(
        params=mcp_types.CallToolRequestParams(name="get_session_info", arguments={})
    )

    def disconnected():
        raise ConnectionError("mock Live disconnected")

    with patch.object(server, "get_ableton_connection", disconnected):
        result = (await handler(request)).model_dump()
    assert result["isError"] is False, result
    assert result["content"][0]["text"] == (
        "Error getting session info: mock Live disconnected"
    ), result
    assert result["structuredContent"] == {
        "result": "Error getting session info: mock Live disconnected"
    }, result
    print("PASS MCP-error-semantics: disconnected tool returned isError=false with error text")

    events = []

    def slow_connection():
        events.append("tool-start")
        time.sleep(0.300)
        events.append("tool-end")
        raise ConnectionError("slow mock connection")

    async def ticker():
        events.append("ticker-start")
        await asyncio.sleep(0.050)
        events.append("ticker-fired")

    started = time.monotonic()
    with patch.object(server, "get_ableton_connection", slow_connection):
        await asyncio.gather(ticker(), handler(request))
    elapsed = time.monotonic() - started
    assert events == ["ticker-start", "tool-start", "tool-end", "ticker-fired"], events
    assert elapsed >= 0.280, elapsed
    print("PASS event-loop-blocking: 50ms ticker fired only after 300ms synchronous tool ended")


def reproduce_shutdown_dispatch(remote):
    script = remote.AbletonMCP.__new__(remote.AbletonMCP)
    script.log_message = lambda message: None
    script.running = True
    dispatched = []

    def process(command):
        assert script.running is False
        dispatched.append(command["type"])
        return {"status": "success", "result": {}}

    script._process_command = process

    class MockSocket:
        def settimeout(self, timeout):
            assert timeout is None

        def recv(self, size):
            # Simulate disconnect() setting running=False while recv blocked.
            script.running = False
            return b'{"type":"create_midi_track","params":{}}'

        def sendall(self, payload):
            pass

        def close(self):
            pass

    script._handle_client(MockSocket())
    assert dispatched == ["create_midi_track"], dispatched
    print("PASS shutdown-dispatch: command dispatched after running=false while recv was pending")


def main():
    versions = ", ".join(
        name + "=" + importlib.metadata.version(name)
        for name in ("mcp", "pydantic", "elevenlabs")
    )
    print("Dependencies: " + versions)
    print("Safety: all connections, Live mutation handlers, queue expiry, and client sockets are mocked")
    remote = load_remote_script()
    reproduce_late_write(remote)
    asyncio.run(reproduce_mcp_error_and_blocking())
    reproduce_shutdown_dispatch(remote)
    print("4 review findings reproduced; no Live connections or source edits")


if __name__ == "__main__":
    main()
