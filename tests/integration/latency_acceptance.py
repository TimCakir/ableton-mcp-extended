"""Opt-in latency-tool acceptance in the exact disposable September 8 Live Set.

Workflow temporarily changes only BECKY VOX/GTR monitoring Off -> Auto -> Off.
It never arms, routes, plays, records or saves Live. A generated local PCM WAV
checks detector arithmetic; it is not a physical latency calibration. Persistence
requires successful workflow evidence and a new Set instance after save/reopen.
Every MCP request is journaled before dispatch. Never retry an ambiguous mutation
with a new plan; inspect the retained plan/request evidence instead.
"""

import argparse
import asyncio
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import struct
import sys
import time
import wave

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from MCP_Server import __version__


ROOT = Path(__file__).resolve().parents[2]
DISPOSABLE_SET = ROOT / (
    "dist/live-acceptance-2026-09-08/MCP Acceptance 2026-09-08 Project/"
    "MCP Acceptance 2026-09-08.als")
EXPECTED_VERSION = "1.6.0"
EXPECTED_BUILD = "2026-09-08.8"
BECKY_TRACKS = ("BECKY VOX", "BECKY GTR")
MIDI_TRACK = "MCP QA CHORDS"
TRANSPORT_FIELDS = ("is_playing", "record_mode", "session_record", "tempo")
MONITOR_FIELDS = (
    "name", "current_monitoring_state", "arm", "input_routing_type",
    "input_routing_channel", "output_routing_type", "output_routing_channel",
)
REFERENCES = [0.5, 1.0, 1.5, 2.0, 2.5]
SYNTHETIC_FRAMES = [4560, 8400, 12056, 16064, 20072]  # 70, 50, 7, 8, 9 ms late at 8 kHz.


def unique_named(rows, name):
    matches = [row for row in rows if row["name"] == name]
    assert len(matches) == 1, {"name": name, "matches": matches}
    return matches[0]


def assert_close(actual, expected):
    assert actual is not None and abs(actual - expected) <= 0.000001, (actual, expected)


def canonical(value, targets):
    """Preserve content/order while replacing process-local handles in fields/paths."""
    labels = {row["track_handle"]: "%s[%s]:%s" % (row["kind"], row["track_index"], row["name"])
              for row in targets["tracks"]}

    def walk(item):
        if isinstance(item, dict):
            return {key: walk(val) for key, val in item.items()
                    if key not in ("session_id", "revision", "captured_at")}
        if isinstance(item, list):
            return [walk(val) for val in item]
        if isinstance(item, str):
            if item in labels:
                return labels[item]
            for handle, label in labels.items():
                if item.startswith(handle + "/"):
                    return label + item[len(handle):]
        return item

    return walk(value)


def assert_monitor_change_only(actual, baseline, handles, state):
    """Report all tracks so unrelated monitor/routing/device changes are visible."""
    actual, expected = copy.deepcopy(actual), copy.deepcopy(baseline)
    for report in (actual, expected):
        report.pop("captured_at", None)
        report["findings"] = [row for row in report["findings"]
                              if not (row.get("track_handle") in handles
                                      and row["code"] == "live_monitoring_enabled")]
    for row in expected["tracks"]:
        if row["track_handle"] in handles:
            row["monitoring_value"] = state
            row["monitoring_state"] = ("in", "auto", "off")[state]
    assert actual == expected, "Unexpected monitoring, routing, device or report change"


def synthetic_fixture(path):
    """Create a new local arithmetic fixture only; never touch a Live clip/file."""
    data = bytearray(8000 * 4 * 2)
    for frame in SYNTHETIC_FRAMES:
        struct.pack_into("<h", data, frame * 2, 16384)
    with path.open("xb") as handle:
        with wave.open(handle, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(8000)
            writer.writeframes(data)
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source": "Generated synthetic single-sample impulses; not a hardware recording",
            "sample_rate": 8000, "sample_frames": 32000, "channels": 1,
            "pulse_frames": SYNTHETIC_FRAMES, "expected_onsets_seconds": REFERENCES,
            "expected_offsets_ms": [70, 50, 7, 8, 9]}


def check_timing(result, fixture):
    assert result["status"] == "measured" and result["complete"] is True, result
    assert result["file_path"] == fixture["path"], result
    assert result["sample_rate"] == 8000 and result["sample_width_bits"] == 16, result
    assert result["channels"] == 1 and result["channel"] == 1, result
    assert result["expected_hit_count"] == 5 and result["measured_hit_count"] == 3, result
    assert result["skip_initial"] == 2 and result["detected_edge_count"] == 5, result
    for key, value in (("median_offset_ms", 8), ("jitter_stddev_ms", 1),
                       ("jitter_peak_to_peak_ms", 2), ("minimum_offset_ms", 7),
                       ("maximum_offset_ms", 9), ("sample_resolution_ms", 0.125)):
        assert_close(result[key], value)
    assert len(result["hits"]) == 5, result
    for index, (hit, offset) in enumerate(zip(result["hits"], fixture["expected_offsets_ms"])):
        assert hit["status"] == "matched", hit
        assert_close(hit["expected_seconds"], REFERENCES[index])
        assert_close(hit["offset_ms"], offset)
        assert hit["included_in_statistics"] is (index >= 2), hit
        assert hit["excluded_startup"] is (index < 2), hit
    assert hashlib.sha256(Path(fixture["path"]).read_bytes()).hexdigest() == fixture["sha256"], (
        "Synthetic WAV changed during read-only analysis")


def prior_workflow(path, expected_set, output):
    source = Path(path).expanduser().resolve()
    if source == output:
        raise ValueError("Persistence output must not overwrite workflow evidence")
    data = source.read_bytes()
    rows = json.loads(data)
    summaries = [row for row in rows if row.get("phase") == "workflows" and row.get("passed")]
    baselines = [row for row in rows if row.get("record") == "fixture_baseline"]
    timing = [row for row in rows if row.get("record") == "verified_synthetic_timing"]
    if (len(summaries) != 1 or len(baselines) != 1 or len(timing) != 1
            or not rows[-1].get("passed")):
        raise ValueError("Provide one completed successful latency workflow evidence file")
    assert Path(rows[0]["set_path"]).resolve() == expected_set, rows[0]
    assert rows[0]["expected_build"] == EXPECTED_BUILD, rows[0]
    assert rows[0]["expected_version"] == EXPECTED_VERSION, rows[0]
    assert rows[0]["package_version"] == EXPECTED_VERSION, rows[0]
    assert summaries[0]["monitoring_restored"] is True, summaries[0]
    return {"summary": summaries[0], "baseline": baselines[0], "timing": timing[0],
            "sha256": hashlib.sha256(data).hexdigest()}


async def run(args):
    if not __debug__:
        raise RuntimeError("Acceptance guards require assertions; do not run with Python -O")
    expected_set = Path(args.set_path).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    if expected_set != DISPOSABLE_SET.resolve() or not expected_set.is_file():
        raise ValueError("Only the exact existing disposable September 8 acceptance Set is allowed")
    if output.suffix.lower() != ".json" or output.exists():
        raise ValueError("Choose a new .json evidence output; existing evidence is never overwritten")
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = [{"phase": args.phase, "set_path": str(expected_set), "passed": False,
                 "started_at": datetime.now(timezone.utc).isoformat(),
                 "expected_build": EXPECTED_BUILD, "expected_version": EXPECTED_VERSION,
                 "package_version": __version__, "tr_track_name": args.tr_track_name}]

    def persist():
        output.write_text(json.dumps(evidence, indent=2) + "\n")

    persist()
    try:
        assert __version__ == EXPECTED_VERSION, __version__
        previous = None
        if args.phase == "persistence":
            if not args.workflow_evidence:
                raise ValueError("Persistence requires --workflow-evidence from a successful workflow")
            previous = prior_workflow(args.workflow_evidence, expected_set, output)
            assert previous["baseline"]["tr_track_name"] == args.tr_track_name
            evidence[0].update(workflow_evidence_sha256=previous["sha256"],
                               prior_session_id=previous["summary"]["session_id"])
            persist()
        elif args.workflow_evidence:
            raise ValueError("--workflow-evidence applies only to the persistence phase")
        async with stdio_client(StdioServerParameters(
                command=sys.executable, args=["-m", "MCP_Server.server"], cwd=str(ROOT))) as streams:
            async with ClientSession(*streams) as session:
                async def rpc(method, params, callback):
                    entry = {"method": method, "params": copy.deepcopy(params)}
                    evidence.append(entry)
                    persist()
                    try:
                        response = await callback()
                    except BaseException as exc:
                        entry.update(error_type=type(exc).__name__, error=str(exc))
                        persist()
                        raise
                    entry["result"] = response.model_dump(mode="json")
                    persist()
                    return response

                await rpc("initialize", {}, session.initialize)

                async def call(name, params=None, expect_error=False):
                    arguments = params if params is not None else {}
                    result = await rpc("tools/call", {"name": name, "arguments": arguments},
                                       lambda: session.call_tool(name, arguments))
                    assert bool(result.isError) == expect_error, result
                    if expect_error:
                        return result
                    assert isinstance(result.structuredContent, dict), result
                    return result.structuredContent

                info = await call("get_build_info")
                assert Path(info["set_file_path"]).resolve() == expected_set, info
                assert info["status"] == "match", info
                for field in ("server_build", "repo_build", "remote_script_build"):
                    assert info[field] == EXPECTED_BUILD, info
                for fields in (("remote_source_sha256", "repo_source_sha256"),
                               ("server_loaded_source_sha256", "server_disk_source_sha256"),
                               ("remote_package_sha256", "remote_disk_package_sha256", "repo_package_sha256"),
                               ("server_loaded_package_sha256", "server_disk_package_sha256")):
                    values = [info[field] for field in fields]
                    assert all(isinstance(value, str) and len(value) == 64 for value in values), info
                    assert len(set(values)) == 1, (fields, values)
                discovered = await rpc("tools/list", {}, session.list_tools)
                assert len(discovered.tools) == 155, len(discovered.tools)
                tool_rows = [tool.model_dump(mode="json") for tool in discovered.tools]
                for name, read_only in (("get_latency_report", True), ("configure_monitoring", False),
                                        ("analyze_recording_timing", True)):
                    tool = unique_named(tool_rows, name)
                    assert tool["annotations"]["readOnlyHint"] is read_only, tool
                    assert tool["annotations"]["destructiveHint"] is (not read_only), tool
                    assert tool["annotations"]["idempotentHint"] is read_only, tool

                # The public overview is formatted text; batch preserves its raw
                # Remote Script result for assertions without parsing that text.
                human_overview = await call("get_session_overview")  # Before any indexed Live read.
                assert isinstance(human_overview.get("result"), str) and human_overview["result"], human_overview
                raw_overview = await call("batch", {
                    "commands": [{"command": "get_session_overview", "params": {}}],
                    "indices_are_one_based": False})
                assert raw_overview["status"] == "success" and raw_overview["succeeded"] == 1, raw_overview
                assert len(raw_overview["results"]) == 1, raw_overview
                overview = raw_overview["results"][0]["result"]
                assert isinstance(overview, dict), overview
                assert overview["session_track_count"] == 38 and overview["return_track_count"] == 3, overview
                targets = await call("get_edit_targets")
                assert sum(row["kind"] == "track" for row in targets["tracks"]) == 38, targets
                assert sum(row["kind"] == "return" for row in targets["tracks"]) == 3, targets
                names = BECKY_TRACKS + (args.tr_track_name, MIDI_TRACK)
                tracks = {name: unique_named(targets["tracks"], name) for name in names}
                assert unique_named(overview["tracks"], MIDI_TRACK)["kind"] == "midi", overview
                for name in BECKY_TRACKS:
                    assert unique_named(overview["tracks"], name)["kind"] == "audio", overview
                common = {"session_id": targets["session_id"]}
                if previous:
                    assert common["session_id"] != previous["summary"]["session_id"], (
                        "Save, unload and reopen the Set before persistence acceptance")
                becky_handles = [tracks[name]["track_handle"] for name in BECKY_TRACKS]
                selected_handles = becky_handles + [tracks[args.tr_track_name]["track_handle"]]

                async def properties(path, fields):
                    result = await call("batch", {"commands": [
                        {"command": "call_lom", "params": {"path": path, "member": field}}
                        for field in fields], "indices_are_one_based": False})
                    assert result["status"] == "success" and result["succeeded"] == len(fields), result
                    assert len(result["results"]) == len(fields), result
                    return {field: row["result"]["value"] for field, row in zip(fields, result["results"])}

                async def monitor_reads():
                    current = await call("get_edit_targets")
                    assert current["session_id"] == common["session_id"], current
                    result = {}
                    for name in BECKY_TRACKS:
                        track = unique_named(current["tracks"], name)
                        assert track["track_handle"] == tracks[name]["track_handle"], track
                        result[name] = await properties("tracks." + str(track["track_index"] - 1), MONITOR_FIELDS)
                    return result

                async def snapshot():
                    result = await call("get_session_snapshot")
                    assert result["session_id"] == common["session_id"], result
                    assert not result["tracks_truncated"], result
                    assert len(result["tracks"]) == 41, result
                    assert all(not row["read_errors"] and not row["clips_truncated"]
                               for row in result["tracks"]), result
                    return result

                async def report(handles=None):
                    result = await call("get_latency_report", dict(common, track_handles=handles))
                    assert result["session_id"] == common["session_id"] and not result["truncated"], result
                    assert result["complete"] is (result["status"] == "complete"), result
                    assert result["status"] in ("complete", "incomplete"), result
                    assert result["scope"]["includes_master"] is True, result
                    assert result["scope"]["includes_all_returns"] is True, result
                    assert result["scope"]["includes_parent_group_context"] is True, result
                    if handles is None:
                        assert sum(row["kind"] == "track" for row in result["tracks"]) == 38, result
                        assert sum(row["kind"] == "return" for row in result["tracks"]) == 3, result
                    else:
                        assert set(result["scope"]["requested_track_handles"]) == set(handles), result
                        assert {row["track_handle"] for row in result["tracks"] if row["focused"]} == set(handles), result
                    assert all(row["value"] is None and row["status"] == "not_observed"
                               for row in result["manual_settings"]), result
                    return result

                transport = await properties("song", TRANSPORT_FIELDS)
                assert all(transport[field] is False for field in TRANSPORT_FIELDS[:3]), transport
                assert_close(transport["tempo"], 110)
                assert not (await call("get_automation_record_status")).get("active")
                before = await snapshot()
                all_before = await report()
                subset_before = await report(selected_handles)
                monitoring_before = await monitor_reads()
                for name in BECKY_TRACKS:
                    assert monitoring_before[name]["current_monitoring_state"] == 2, monitoring_before
                    assert unique_named(all_before["tracks"], name)["monitoring_value"] == 2, all_before
                    assert unique_named(subset_before["tracks"], name)["monitoring_value"] == 2, subset_before
                limiter = unique_named(all_before["master"]["devices"], "Limiter")
                limiter_index = all_before["master"]["devices"].index(limiter)
                native_limiter = await properties("master_track.devices." + str(limiter_index),
                                                  ("name", "latency_in_samples", "latency_in_ms"))
                assert native_limiter["name"] == "Limiter", native_limiter
                assert native_limiter["latency_in_samples"] == limiter["reported_latency_samples"] == 128, limiter
                assert_close(limiter["reported_latency_ms"], native_limiter["latency_in_ms"])
                baseline = {"record": "fixture_baseline", "session_id": common["session_id"],
                            "tr_track_name": args.tr_track_name, "snapshot": canonical(before, targets),
                            "all_report": canonical(all_before, targets),
                            "subset_report": canonical(subset_before, targets),
                            "monitoring": monitoring_before, "transport": transport,
                            "limiter": native_limiter}
                evidence.append(baseline)
                persist()
                if previous:
                    for field in ("snapshot", "all_report", "subset_report", "monitoring", "transport", "limiter"):
                        assert baseline[field] == previous["baseline"][field], "Saved baseline differs: " + field
                else:
                    for bad in (
                            dict(common, track_handles=[tracks[MIDI_TRACK]["track_handle"]]),
                            dict(common, track_handles=[becky_handles[0], becky_handles[0]]),
                            {"session_id": "acceptance-stale-set", "track_handles": becky_handles},
                            dict(common, track_handles=["acceptance-missing-handle"])):
                        await call("configure_monitoring", dict(bad, action="preview", monitoring_path="live"), True)
                    await call("get_latency_report", {"session_id": "acceptance-stale-set"}, True)
                    await call("get_latency_report", dict(common, track_handles=["acceptance-missing-handle"]), True)
                    assert canonical(await snapshot(), targets) == baseline["snapshot"]
                    assert canonical(await report(), targets) == baseline["all_report"]
                    assert await monitor_reads() == monitoring_before
                    plan = await call("configure_monitoring", dict(common, action="preview",
                                      track_handles=becky_handles, monitoring_path="live"))
                    assert plan["status"] == "preview" and plan["track_count"] == 2, plan
                    assert plan["session_id"] == common["session_id"] and plan["monitoring_path"] == "live", plan
                    assert {row["track_handle"] for row in plan["tracks"]} == set(becky_handles), plan
                    assert all(row["original_monitoring_state"] == 2 and row["target_monitoring_state"] == 1
                               for row in plan["tracks"]), plan
                    evidence.append({"record": "monitoring_plan", "plan_id": plan["plan_id"],
                                     "session_id": common["session_id"]})
                    persist()
                    assert canonical(await snapshot(), targets) == baseline["snapshot"]
                    assert canonical(await report(), targets) == baseline["all_report"]
                    assert await monitor_reads() == monitoring_before

                    async def transition(action, pending, terminal):
                        params = dict(common, action=action, plan_id=plan["plan_id"])
                        result = await call("configure_monitoring", params)
                        assert result["status"] == pending, result
                        deadline = time.monotonic() + 35
                        while result["status"] == pending:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise AssertionError("Monitoring plan did not settle; reconcile its retained action/plan_id")
                            await asyncio.sleep(min(0.2, remaining))
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise AssertionError("Monitoring plan did not settle; reconcile its retained action/plan_id")
                            result = await asyncio.wait_for(call("configure_monitoring", params), timeout=remaining)
                        assert result["status"] == terminal and result["verified"] is True, result
                        assert result["plan_id"] == plan["plan_id"] and result["saved"] is False, result
                        return result

                    applied = await transition("apply", "applying", "applied")
                    assert all(row["actual_monitoring_state"] == 1 for row in applied["tracks"]), applied
                    assert_monitor_change_only(await report(), all_before, becky_handles, 1)
                    current = await monitor_reads()
                    for name in BECKY_TRACKS:
                        assert current[name] == dict(monitoring_before[name], current_monitoring_state=1), current
                    assert canonical(await snapshot(), targets) == baseline["snapshot"]
                    replay = await call("configure_monitoring", dict(common, action="apply", plan_id=plan["plan_id"]))
                    assert replay["status"] == "applied" and replay["replayed"] is True, replay
                    assert_monitor_change_only(await report(), all_before, becky_handles, 1)
                    restored = await transition("restore", "restoring", "restored")
                    assert all(row["actual_monitoring_state"] == 2 for row in restored["tracks"]), restored
                    for action in ("restore", "apply"):
                        replay = await call("configure_monitoring", dict(common, action=action, plan_id=plan["plan_id"]))
                        assert replay["status"] == "restored" and replay["replayed"] is True, replay

                fixture = (previous["timing"]["fixture"] if previous else
                           synthetic_fixture(output.with_suffix(".synthetic.wav")))
                assert hashlib.sha256(Path(fixture["path"]).read_bytes()).hexdigest() == fixture["sha256"], fixture
                timing = await call("analyze_recording_timing", {
                    "path": fixture["path"], "expected_onsets_seconds": REFERENCES})
                check_timing(timing, fixture)
                if previous:
                    assert timing == previous["timing"]["result"], "Synthetic detector output changed after reopening"
                evidence.append({"record": "verified_synthetic_timing", "fixture": fixture, "result": timing,
                                 "physical_calibration": False})
                persist()
                assert canonical(await snapshot(), targets) == baseline["snapshot"], "Set metadata changed"
                assert canonical(await report(), targets) == baseline["all_report"], "Full latency/monitor report changed"
                assert canonical(await report(selected_handles), targets) == baseline["subset_report"]
                assert await monitor_reads() == monitoring_before, "Monitoring or routing was not restored"
                assert await properties("song", TRANSPORT_FIELDS) == transport, "Transport changed"
                assert not (await call("get_automation_record_status")).get("active")
                evidence.append({"phase": args.phase, "passed": True, "session_id": common["session_id"],
                                 "finished_at": datetime.now(timezone.utc).isoformat(),
                                 "monitoring_restored": True, "set_snapshot_unchanged": True,
                                 "latency_report_unchanged": True, "verified_synthetic_timing": True,
                                 "physical_calibration": False, "saved_by_runner": False,
                                 "fresh_set_instance": previous is not None})
                persist()
                print(json.dumps({"phase": args.phase, "passed": True, "evidence": str(output)}))
    except BaseException as exc:
        evidence.append({"phase": args.phase, "passed": False,
                         "error_type": type(exc).__name__, "error": str(exc)})
        persist()
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set-path", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--phase", choices=("workflows", "persistence"), required=True)
    parser.add_argument("--workflow-evidence")
    parser.add_argument("--tr-track-name", default="TR BD - Kick")
    asyncio.run(run(parser.parse_args()))
