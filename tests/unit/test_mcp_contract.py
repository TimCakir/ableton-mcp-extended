"""Actual SDK contract tests, isolated from legacy tests' sys.modules mocks."""

import os
import subprocess
import sys
import textwrap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _run(source):
    result = subprocess.run([sys.executable, "-c", textwrap.dedent(source)], cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_sync_handlers_leave_event_loop_responsive_and_direct_callers_sync():
    _run('''
        import asyncio, inspect, time
        from MCP_Server import server
        events = []
        class FakeConnection:
            def send_command(self, *args):
                time.sleep(0.18)
                return {"tempo": 120}
        server.get_ableton_connection = lambda: FakeConnection()
        assert not inspect.iscoroutinefunction(server.get_session_info)
        assert isinstance(server.get_session_info(None), str)
        async def main():
            async def call():
                await server.mcp.call_tool("get_session_info", {})
                events.append("done")
            async def tick():
                await asyncio.sleep(0.03)
                events.append("tick")
                tools = await server.mcp.list_tools()
                assert any(tool.name == "cancel_automation_record" for tool in tools)
            await asyncio.gather(call(), tick())
            assert events == ["tick", "done"], events
        asyncio.run(main())
    ''')


def test_legacy_error_strings_become_real_mcp_errors():
    _run('''
        import asyncio
        import mcp.types as types
        from MCP_Server import server
        server.get_ableton_connection = lambda: (_ for _ in ()).throw(ConnectionError("offline"))
        async def main():
            request = types.CallToolRequest(params=types.CallToolRequestParams(name="get_session_info", arguments={}))
            response = await server.mcp._mcp_server.request_handlers[types.CallToolRequest](request)
            assert response.root.isError is True
            assert "offline" in response.root.content[0].text
        asyncio.run(main())
    ''')


def test_partial_batches_retain_values_and_mark_error():
    _run('''
        import asyncio
        import mcp.types as types
        from MCP_Server import server
        class FakeConnection:
            def send_command(self, *args):
                return {"total":3,"ran":2,"succeeded":1,"failed":1,"stopped_early":True,
                        "results":[{"index":0,"command":"get_session_info","status":"success","result":{"tempo":123}},
                                   {"index":1,"command":"set_tempo","status":"error","message":"invalid tempo"}]}
        server.get_ableton_connection = lambda: FakeConnection()
        async def main():
            request = types.CallToolRequest(params=types.CallToolRequestParams(name="batch", arguments={
                "commands":[{"command":"get_session_info"},{"command":"set_tempo"},{"command":"get_session_info"}]}))
            response = (await server.mcp._mcp_server.request_handlers[types.CallToolRequest](request)).root
            assert response.isError is True, response
            data = response.structuredContent
            assert data["status"] == "partial"
            assert data["results"][0]["result"] == {"tempo":123}
            assert [row["status"] for row in data["results"]] == ["success","error","skipped"]
            assert [row["index"] for row in data["results"]] == [1,2,3]
        asyncio.run(main())
    ''')


def test_discovery_exposes_structured_results_and_conservative_annotations():
    _run('''
        import asyncio
        from MCP_Server import server
        async def main():
            tools = {tool.name:tool for tool in await server.mcp.list_tools()}
            assert all(tool.annotations is not None for tool in tools.values())
            for name in ("get_command_status", "get_edit_targets", "edit_track", "get_session_snapshot",
                         "compare_session_snapshots", "analyze_audio_file", "verify_export_outputs"):
                assert name in tools, name
            for name in ("call_lom","measure_section","batch","modify_clip_notes","freeze_track"):
                assert tools[name].annotations.readOnlyHint is False
                assert tools[name].annotations.idempotentHint is False
                assert tools[name].annotations.destructiveHint is True
            assert tools["get_session_info"].annotations.readOnlyHint is True
            assert "results" in tools["batch"].outputSchema["properties"]
            assert "operation_id" in tools["get_automation_record_status"].outputSchema["properties"]
            assert "outputs" in tools["get_automation_record_status"].outputSchema["properties"]
            assert "remote_script_build" in tools["get_build_info"].outputSchema["properties"]
            for name in ("get_latency_report", "analyze_recording_timing"):
                assert tools[name].annotations.readOnlyHint is True
                assert tools[name].outputSchema["properties"]["complete"]["type"] == "boolean"
            assert tools["configure_monitoring"].annotations.readOnlyHint is False
            assert tools["configure_monitoring"].inputSchema["properties"]["action"]["enum"] == ["preview", "apply", "restore"]
        asyncio.run(main())
    ''')


def test_missing_build_identity_is_unknown_not_matching():
    _run('''
        import asyncio
        from MCP_Server import server
        class FakeConnection:
            def send_command(self, *args):
                return {"live_version":"12.4.5","remote_script_build":None}
        server.get_ableton_connection = lambda: FakeConnection()
        data = server.get_build_info(None)
        assert data["status"] == "unknown", data
        assert data["remote_script_build"] is None
        assert data["protocol_version"] is None
    ''')


def test_incomplete_latency_evidence_and_partial_monitoring_survive_sdk_validation():
    _run('''
        import asyncio
        import mcp.types as types
        from MCP_Server import server
        class FakeConnection:
            def connect(self): return True
            def send_command(self, command, *args):
                if command == "get_latency_report":
                    return {"status":"incomplete","complete":False,"session_id":"set1",
                        "tracks":[],"master":None,"findings":[],
                        "read_errors":[{"field":"master_track","message":"unavailable","unavailable":True}]}
                return {"status":"partial","plan_id":"plan1","session_id":"set1",
                    "monitoring_path":"direct","track_count":1,"tracks":[{"track_handle":"voice"}],
                    "rollback_verified":False,"rollback_errors":["recording started"]}
        server._ableton_connection = FakeConnection()
        async def main():
            for name, args in (("get_latency_report", {"session_id":"set1"}),
                               ("configure_monitoring", {"action":"apply","session_id":"set1","plan_id":"plan1"})):
                request = types.CallToolRequest(params=types.CallToolRequestParams(name=name, arguments=args))
                response = (await server.mcp._mcp_server.request_handlers[types.CallToolRequest](request)).root
                assert response.isError == (name == "configure_monitoring"), response
                data = response.structuredContent
                if name == "get_latency_report":
                    assert data["master"] is None and not data["complete"]
                    assert data["read_errors"][0]["field"] == "master_track"
                else:
                    assert data["rollback_verified"] is False
                    assert data["rollback_errors"] == ["recording started"]
        asyncio.run(main())
    ''')


def test_operation_manifest_survives_sdk_output_validation():
    _run('''
        import asyncio
        import mcp.types as types
        from MCP_Server import server
        class FakeConnection:
            def send_command(self, *args):
                return {"status":"interrupted","operation_id":"job-17","progress":0.5,
                        "outputs":[{"file_path":"/tmp/record.aif","file_exists":True,"status":"partial"}],
                        "captured":{"from_beat":0,"to_beat":4},"requested":{"from_beat":0,"to_beat":8}}
        server.get_ableton_connection = lambda: FakeConnection()
        async def main():
            request = types.CallToolRequest(params=types.CallToolRequestParams(name="get_automation_record_status", arguments={}))
            response = (await server.mcp._mcp_server.request_handlers[types.CallToolRequest](request)).root
            assert response.isError is False, response
            data = response.structuredContent
            assert data["operation_id"] == "job-17"
            assert data["status"] == "interrupted"
            assert data["outputs"][0]["status"] == "partial"
            assert data["captured"]["to_beat"] == 4
        asyncio.run(main())
    ''')


def test_real_stdio_initialize_discover_and_error_round_trip():
    _run('''
        import asyncio, os, sys
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        child = ("from MCP_Server import server\\n"
                 "class FakeConnection:\\n"
                 "    def send_command(self, *args):\\n"
                 "        return {'tempo':120}\\n"
                 "server.get_ableton_connection = lambda: FakeConnection()\\n"
                 "server.main()\\n")
        async def main():
            params = StdioServerParameters(command=sys.executable, args=["-c", child], cwd=os.getcwd())
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == "AbletonMCP"
                    names = {tool.name for tool in (await session.list_tools()).tools}
                    assert "verify_export_outputs" in names
                    ok = await session.call_tool("get_session_info", {})
                    assert not ok.isError
                    failed = await session.call_tool("get_track_info", {"track_index":0})
                    assert failed.isError
                    assert "must be >= 1" in failed.content[0].text
        asyncio.run(main())
    ''')


def test_note_pages_expose_every_note_and_scope_warning_without_truncation():
    _run('''
        import asyncio
        import mcp.types as types
        from MCP_Server import server
        class FakeConnection:
            def send_command(self, *args):
                return {"notes":[{"note_id":i,"pitch":60,"start_time":i} for i in range(250)],
                        "read_scope":"marker_bounds_fallback", "scope_warning":"Limited API",
                        "loop_start":16,"loop_end":32,"note_count":250}
        server.get_ableton_connection = lambda: FakeConnection()
        first = server.get_clip_notes(None, 1, 1)
        assert len(first["notes"]) == 200
        assert first["next_offset"] == 200
        async def main():
            request = types.CallToolRequest(params=types.CallToolRequestParams(name="get_clip_notes", arguments={
                "track_index":1,"clip_index":1,"offset":200,"limit":200}))
            response = (await server.mcp._mcp_server.request_handlers[types.CallToolRequest](request)).root
            assert not response.isError, response
            data = response.structuredContent
            assert len(data["notes"]) == 50
            assert data["notes"][0]["note_id"] == 200
            assert data["next_offset"] is None
            assert data["scope_warning"] == "Limited API"
            assert data["loop_start"] == 16
        asyncio.run(main())
    ''')


def test_build_matching_requires_loaded_source_hashes_and_detects_changed_code():
    _run('''
        from pathlib import Path
        from MCP_Server import server
        remote_path = Path(server.__file__).resolve().parents[1] / "AbletonMCP_Remote_Script" / "__init__.py"
        reply = {"remote_script_build":server.SERVER_BUILD_ID,"protocol_version":server.PROTOCOL_VERSION,
                 "source_sha256":server._source_sha256(remote_path),
                 "package_sha256":server._package_sha256(remote_path.parent),
                 "disk_package_sha256":server._package_sha256(remote_path.parent)}
        class FakeConnection:
            def send_command(self, *args):
                return dict(reply)
        server.get_ableton_connection = lambda: FakeConnection()
        assert server.get_build_info(None)["status"] == "match"
        reply["source_sha256"] = None
        assert server.get_build_info(None)["status"] == "unknown"
        reply["source_sha256"] = "old-loaded-code"
        result = server.get_build_info(None)
        assert result["status"] == "mismatch"
        assert any("Ableton Live" in line for line in result["recovery"])
        reply["source_sha256"] = server._source_sha256(remote_path)
        server.SERVER_SOURCE_SHA256 = "old-loaded-server"
        result = server.get_build_info(None)
        assert result["status"] == "mismatch"
        assert any("MCP host" in line for line in result["recovery"])
    ''')


def test_measure_section_cancellation_wakes_wait_and_never_stops_later_playback():
    _run('''
        import asyncio, threading
        from MCP_Server import server
        calls = []
        waiting = threading.Event()
        wait_finished = threading.Event()
        actual_wait = server.wait_for_tool_cancellation
        def observed_wait(seconds):
            waiting.set()
            try:
                return actual_wait(seconds)
            finally:
                wait_finished.set()
        server.wait_for_tool_cancellation = observed_wait
        class FakeConnection:
            def send_command(self, command, *args):
                calls.append(command)
                return {"tempo":120}
        server.get_ableton_connection = lambda: FakeConnection()
        async def main():
            # A 200-second sample gap proves cancellation wakes the worker,
            # rather than merely cancelling its await and leaving a sleeper.
            task = asyncio.create_task(server.mcp.call_tool("measure_section", {
                "from_bar":1,"to_bar":301,"samples":3}))
            assert await asyncio.to_thread(waiting.wait, 1)
            task.cancel()
            try:
                await task
                raise AssertionError("call was not cancelled")
            except asyncio.CancelledError:
                pass
            assert await asyncio.to_thread(wait_finished.wait, 1), "sampling wait did not wake"
            await asyncio.sleep(0.02)
            assert calls == ["get_session_info","play_section"], calls
        asyncio.run(main())
    ''')


def test_measure_section_cancellation_during_read_prevents_more_reads_and_final_stop():
    _run('''
        import asyncio, threading
        from MCP_Server import server
        calls = []
        reading = threading.Event()
        release = threading.Event()
        read_finished = threading.Event()
        class FakeConnection:
            def send_command(self, command, *args):
                calls.append(command)
                if command == "get_session_info":
                    return {"tempo":12000}
                if command == "get_meters":
                    reading.set()
                    assert release.wait(2)
                    read_finished.set()
                    return {"tracks":[],"master":{"output_meter_level":0.5}}
                return {}
        server.get_ableton_connection = lambda: FakeConnection()
        async def main():
            task = asyncio.create_task(server.mcp.call_tool("measure_section", {
                "from_bar":1,"to_bar":2,"samples":3}))
            assert await asyncio.to_thread(reading.wait, 1)
            task.cancel()
            try:
                await task
                raise AssertionError("call was not cancelled")
            except asyncio.CancelledError:
                pass
            release.set()
            assert await asyncio.to_thread(read_finished.wait, 1)
            await asyncio.sleep(0.06)
            assert calls == ["get_session_info","play_section","get_meters"], calls
        asyncio.run(main())
    ''')


def test_measure_section_cancelled_during_preflight_does_not_start_playback():
    _run('''
        import asyncio, threading
        from MCP_Server import server
        calls = []
        reading = threading.Event()
        release = threading.Event()
        class FakeConnection:
            def send_command(self, command, *args):
                calls.append(command)
                reading.set()
                assert release.wait(2)
                return {"tempo":12000}
        server.get_ableton_connection = lambda: FakeConnection()
        async def main():
            task = asyncio.create_task(server.mcp.call_tool("measure_section", {
                "from_bar":1,"to_bar":2,"samples":3}))
            assert await asyncio.to_thread(reading.wait, 1)
            task.cancel()
            try:
                await task
                raise AssertionError("call was not cancelled")
            except asyncio.CancelledError:
                pass
            release.set()
            await asyncio.sleep(0.06)
            assert calls == ["get_session_info"], calls
        asyncio.run(main())
    ''')
