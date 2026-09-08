"""Opt-in PCM WAV placement acceptance on an existing disposable Live Set.

MCP QA CROSS AUDIO must retain its earlier cross-track copies and have empty
Session slots. The supplied files must be existing 12-second stereo 44.1 kHz
PCM WAV fixtures. Run workflows, save and unload/reopen the Set, then run
persistence with --workflow-evidence. This runner never creates or edits source
files, creates fixtures, starts playback, records or saves the Set.
"""

import argparse
import asyncio
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import wave

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from MCP_Server import __version__


EXPECTED_VERSION = "1.5.0"
EXPECTED_BUILD = "2026-09-08.7"
DESTINATION = "MCP QA CROSS AUDIO"
MIDI_DESTINATION = "MCP QA CROSS MIDI"
MIDI_SOURCE = "MCP QA CHORDS"
CASES = (
    {"name": "MCP FILE EXCERPT", "file": "excerpt", "start": 256.5, "end": 262.0,
     "reserved_end": 278.5, "source_range": {"start": 2, "end": 5, "units": "seconds"}},
    {"name": "MCP FILE FULL", "file": "full", "start": 288.25, "end": 310.25,
     "reserved_end": 310.25, "source_range": {"start": 0, "end": 12, "units": "seconds"}},
)
COPY_NAMES = {case["name"] for case in CASES}
AUDIO_FIELDS = (
    "name", "file_path", "warping", "warp_mode", "looping", "length",
    "start_marker", "end_marker", "loop_start", "loop_end", "gain",
    "pitch_coarse", "pitch_fine", "muted", "color", "sample_length", "sample_rate",
)
TRANSPORT_FIELDS = ("is_playing", "record_mode", "session_record", "tempo")


def assert_close(actual, expected):
    assert abs(actual - expected) <= 0.00001, (actual, expected)


def unique_named(rows, name):
    matches = [row for row in rows if row["name"] == name]
    assert len(matches) == 1, {"name": name, "matches": matches}
    return matches[0]


def file_facts(value):
    """Independently inspect the local fixture without writing it or its ASD."""
    path = Path(value).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() != ".wav":
        raise ValueError("Provide an existing PCM WAV fixture: " + str(path))
    stat = path.stat()
    with wave.open(str(path), "rb") as reader:
        frames, rate = reader.getnframes(), reader.getframerate()
        channels, width, compression = reader.getnchannels(), reader.getsampwidth(), reader.getcomptype()
    assert channels == 2 and rate == 44100 and compression == "NONE", (channels, rate, compression)
    assert_close(frames / rate, 12)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    current = path.stat()
    identity_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    assert all(getattr(current, key) == getattr(stat, key) for key in identity_fields), (
        "Fixture changed while being inspected")
    return {"file_path": str(path), "size_bytes": stat.st_size, "duration_seconds": frames / rate,
            "sample_frames": frames, "sample_rate": rate, "channels": channels,
            "sample_width_bytes": width, "sha256": digest.hexdigest(),
            "device": stat.st_dev, "inode": stat.st_ino,
            "mtime_ns": stat.st_mtime_ns, "ctime_ns": stat.st_ctime_ns}


def snapshot_tracks(snapshot, without_copies=False):
    rows = copy.deepcopy(snapshot["tracks"])
    for row in rows:
        row.pop("track_handle", None)  # A fresh Set gives tracks new handles.
        if without_copies and row["name"] == DESTINATION:
            row["clips"] = [clip for clip in row["clips"]
                            if not (clip["view"] == "arrangement" and clip["name"] in COPY_NAMES)]
            row["clip_count"] = len(row["clips"])
    return rows


def assert_only_file_additions(current, baseline_tracks):
    assert snapshot_tracks(current, without_copies=True) == baseline_tracks, (
        "Original tracks, routing, Session slots or arrangement material changed")
    destination = unique_named(current["tracks"], DESTINATION)
    baseline = unique_named(baseline_tracks, DESTINATION)
    assert len(destination["clips"]) == len(baseline["clips"]) + 2, destination
    assert not any(clip["view"] == "session" for clip in destination["clips"]), (
        "Imported staging clip remains in a Session slot")
    for name in COPY_NAMES:
        assert unique_named(destination["clips"], name)["view"] == "arrangement"


def check_copy_properties(case, actual, fixture):
    assert actual["name"] == case["name"] and actual["file_path"] == fixture["file_path"], actual
    assert actual["warping"] is False and actual["looping"] is False and actual["muted"] is False, actual
    assert_close(actual["length"], case["end"] - case["start"])
    for field in ("pitch_coarse", "pitch_fine"):
        assert_close(actual[field], 0)
    assert_close(actual["gain"], 1)
    assert actual["sample_rate"] == fixture["sample_rate"], actual
    assert actual["sample_length"] == fixture["sample_frames"], actual
    for field in ("start_marker", "loop_start"):
        assert_close(actual[field], case["source_range"]["start"])
    for field in ("end_marker", "loop_end"):
        assert_close(actual[field], case["source_range"]["end"])


def read_workflow_evidence(path, expected_set, output):
    prior_path = Path(path).expanduser().resolve()
    if prior_path == output:
        raise ValueError("Persistence output must not overwrite workflow evidence")
    contents = prior_path.read_bytes()
    rows = json.loads(contents)
    summaries = [row for row in rows if row.get("phase") == "workflows" and row.get("passed")]
    baselines = [row for row in rows if row.get("record") == "fixture_baseline"]
    copies = [row for row in rows if row.get("record") == "verified_copies"]
    if len(summaries) != 1 or len(baselines) != 1 or len(copies) != 1 or not rows[-1].get("passed"):
        raise ValueError("Provide evidence from one completed successful audio-file workflow")
    assert Path(rows[0]["set_path"]).resolve() == expected_set, rows[0]
    assert rows[0]["expected_build"] == EXPECTED_BUILD, rows[0]
    assert rows[0]["expected_version"] == EXPECTED_VERSION, rows[0]
    return {"summary": summaries[0], "baseline": baselines[0], "copies": copies[0]["copies"],
            "sha256": hashlib.sha256(contents).hexdigest()}


async def run(args):
    if not __debug__:
        raise RuntimeError("Acceptance guards require assertions; do not run Python with -O")
    expected_set = Path(args.set_path).expanduser().resolve()
    if (not expected_set.is_file() or expected_set.suffix.lower() != ".als"
            or "live-acceptance" not in str(expected_set)):
        raise ValueError("Provide an existing disposable live-acceptance .als Set")
    output = Path(args.output).expanduser().resolve()
    if output.suffix.lower() != ".json" or output == expected_set:
        raise ValueError("Evidence output must be a separate .json file")
    fixtures = {"excerpt": file_facts(args.excerpt_file), "full": file_facts(args.full_file)}
    previous = None
    if args.phase == "persistence":
        if not args.workflow_evidence:
            raise ValueError("Persistence requires --workflow-evidence from the successful workflow")
        previous = read_workflow_evidence(args.workflow_evidence, expected_set, output)
    elif args.workflow_evidence:
        raise ValueError("--workflow-evidence is used only for persistence")
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = [{"phase": args.phase, "set_path": str(expected_set), "passed": False,
                 "started_at": datetime.now(timezone.utc).isoformat(),
                 "expected_build": EXPECTED_BUILD, "expected_version": EXPECTED_VERSION,
                 "package_version": __version__}]
    if previous:
        evidence[0].update(workflow_evidence_sha256=previous["sha256"],
                           prior_session_id=previous["summary"]["session_id"])

    def persist():
        output.write_text(json.dumps(evidence, indent=2) + "\n")

    persist()
    try:
        assert __version__ == EXPECTED_VERSION, __version__
        async with stdio_client(StdioServerParameters(
                command=sys.executable, args=["-m", "MCP_Server.server"])) as streams:
            async with ClientSession(*streams) as session:
                initialized = await session.initialize()
                evidence.append({"initialize": initialized.model_dump(mode="json")})

                async def call(name, params=None, expect_error=False):
                    arguments = params if params is not None else {}
                    entry = {"tool": name, "params": copy.deepcopy(arguments)}
                    evidence.append(entry)
                    persist()
                    result = await session.call_tool(name, arguments)
                    entry["result"] = result.model_dump(mode="json")
                    persist()
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
                tools = await session.list_tools()
                tool = unique_named([item.model_dump(mode="json") for item in tools.tools], "build_arrangement")
                assert "file_path" in tool["description"] and "seconds" in tool["description"], tool
                assert len(tools.tools) == 152, len(tools.tools)
                evidence.append({"discovered_tools": len(tools.tools), "build_arrangement": tool})
                persist()

                async def properties(path, fields):
                    result = await call("batch", {
                        "commands": [{"command": "call_lom", "params": {"path": path, "member": field}}
                                     for field in fields], "indices_are_one_based": False})
                    assert result["status"] == "success" and result["succeeded"] == len(fields), result
                    return {field: row["result"]["value"]
                            for field, row in zip(fields, result["results"])}

                transport = await properties("song", TRANSPORT_FIELDS)
                assert not any(transport[field] for field in TRANSPORT_FIELDS[:3]), transport
                assert_close(transport["tempo"], 110)
                assert not (await call("get_automation_record_status")).get("active")
                targets = await call("get_edit_targets")
                tracks = {name: unique_named(targets["tracks"], name)
                          for name in (DESTINATION, MIDI_DESTINATION, MIDI_SOURCE)}
                assert all(row["kind"] == "track" for row in tracks.values()), tracks
                destination = tracks[DESTINATION]
                common = {"session_id": targets["session_id"]}
                if previous:
                    assert common["session_id"] != previous["summary"]["session_id"], (
                        "Save, unload and reopen the Set before persistence acceptance")

                async def snapshot():
                    result = await call("get_session_snapshot")
                    assert not result["tracks_truncated"], result
                    assert all(not row["read_errors"] and not row["clips_truncated"]
                               for row in result["tracks"]), result
                    assert result["session_id"] == common["session_id"], result
                    return result

                async def empty_slots():
                    result = await call("batch", {"commands": [{"command": "get_track_info", "params": {
                        "track_index": destination["track_index"] - 1}}], "indices_are_one_based": False})
                    assert result["status"] == "success" and result["succeeded"] == 1, result
                    slots = result["results"][0]["result"]["clip_slots"]
                    assert len(slots) >= 2 and all(slot["has_clip"] is False and slot["clip"] is None for slot in slots), slots
                    return slots

                async def clip_properties(index):
                    path = "tracks." + str(destination["track_index"] - 1) + ".arrangement_clips." + str(index - 1)
                    return await properties(path, AUDIO_FIELDS)

                async def original_clip_properties(current):
                    clips = unique_named(current["tracks"], DESTINATION)["clips"]
                    originals = [clip for clip in clips if clip["name"] not in COPY_NAMES]
                    assert all(clip["view"] == "arrangement" for clip in originals), originals
                    return [{"index": clip["index"], "properties": await clip_properties(clip["index"])}
                            for clip in originals]

                before_snapshot = await snapshot()
                slots = await empty_slots()
                originals = await original_clip_properties(before_snapshot)
                baseline_tracks = previous["baseline"]["tracks"] if previous else snapshot_tracks(before_snapshot)
                if previous:
                    assert fixtures == previous["baseline"]["files"], "Source WAV files changed"
                    assert slots == previous["baseline"]["empty_slots"], "Saved staging slots changed"
                    assert originals == previous["baseline"]["original_clip_properties"], "Saved original clips changed"
                    assert transport == previous["baseline"]["transport"], "Saved transport changed"
                    assert_only_file_additions(before_snapshot, baseline_tracks)
                else:
                    clips = unique_named(before_snapshot["tracks"], DESTINATION)["clips"]
                    for name, start, end in (("MCP CROSS BEATS", 192.25, 198.25), ("MCP CROSS SECONDS", 224.5, 230)):
                        clip = unique_named(clips, name)
                        assert_close(clip["start_time"], start)
                        assert_close(clip["end_time"], end)
                    assert not any(clip["name"] in COPY_NAMES for row in before_snapshot["tracks"] for clip in row["clips"]), (
                        "Acceptance names already exist; inspect the Set before rerunning")
                evidence.append({"record": "fixture_baseline", "session_id": common["session_id"],
                                 "files": fixtures, "tracks": baseline_tracks, "empty_slots": slots,
                                 "original_clip_properties": originals, "transport": transport})
                persist()
                placements = [{"file_path": fixtures[case["file"]]["file_path"],
                               "destination_track_handle": destination["track_handle"],
                               "destination_beat": case["start"], "name": case["name"],
                               **({"source_range": copy.deepcopy(case["source_range"])} if case["file"] == "excerpt" else {})}
                              for case in CASES]

                def check_plan(plan):
                    assert plan["placement_count"] == 2, plan
                    for case in CASES:
                        row = unique_named(plan["placements"], case["name"])
                        fixture = fixtures[case["file"]]
                        assert row["kind"] == "audio" and row["file_path"] == fixture["file_path"], row
                        assert row["destination_track_handle"] == destination["track_handle"], row
                        assert row["destination_track_name"] == DESTINATION, row
                        assert 1 <= row["source_slot"] <= len(slots), row
                        assert row["source_range"] == case["source_range"], row
                        assert row["file_identity"]["size_bytes"] == fixture["size_bytes"], row
                        assert_close(row["file_duration_seconds"], fixture["duration_seconds"])
                        for field in ("sample_rate", "sample_frames", "channels"):
                            assert row[field] == fixture[field], row
                        assert_close(row["destination_beat"], case["start"])
                        assert_close(row["end_beat"], case["end"])
                        assert_close(row["reserved_end_beat"], case["reserved_end"])

                if args.phase == "workflows":
                    wrong_kind = copy.deepcopy(placements[0])
                    wrong_kind["destination_track_handle"] = tracks[MIDI_DESTINATION]["track_handle"]
                    await call("build_arrangement", dict(common, action="preview", placements=[wrong_kind]), True)
                    session_source = {"track_handle": tracks[MIDI_SOURCE]["track_handle"], "source_slot": 1,
                                      "destination_track_handle": tracks[MIDI_DESTINATION]["track_handle"],
                                      "destination_beat": 400}
                    await call("build_arrangement", dict(common, action="preview", placements=[placements[0], session_source]), True)
                    overlap = [copy.deepcopy(placements[0]), copy.deepcopy(placements[1])]
                    overlap[1]["destination_beat"] = 264.5  # Beyond excerpt end, inside its reserved full-file span.
                    await call("build_arrangement", dict(common, action="preview", placements=overlap), True)
                    invalid_range = copy.deepcopy(placements[0])
                    invalid_range["source_range"]["end"] = 13
                    await call("build_arrangement", dict(common, action="preview", placements=[invalid_range]), True)
                    wrong_units = copy.deepcopy(placements[0])
                    wrong_units["source_range"]["units"] = "beats"
                    await call("build_arrangement", dict(common, action="preview", placements=[wrong_units]), True)
                    missing = copy.deepcopy(placements[0])
                    missing["file_path"] += ".acceptance-missing.wav"
                    assert not Path(missing["file_path"]).exists()
                    await call("build_arrangement", dict(common, action="preview", placements=[missing]), True)
                    existing_overlap = copy.deepcopy(placements[0])
                    existing_overlap["destination_beat"] = 192.25
                    await call("build_arrangement", dict(common, action="preview", placements=[existing_overlap]), True)
                    assert (await snapshot())["tracks"] == before_snapshot["tracks"]
                    assert await empty_slots() == slots
                    plan = await call("build_arrangement", dict(common, action="preview", placements=placements))
                    assert plan["status"] == "preview", plan
                    check_plan(plan)
                    assert (await snapshot())["tracks"] == before_snapshot["tracks"]
                    assert await empty_slots() == slots
                    applied = await call("build_arrangement", dict(common, action="apply", plan_id=plan["plan_id"]))
                    assert applied["status"] == "applying", applied
                    deadline = time.monotonic() + 30
                    while applied["status"] == "applying":
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise AssertionError("Plan did not finish in 30 seconds; reconcile this retained plan")
                        await asyncio.sleep(min(0.2, remaining))
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise AssertionError("Plan did not finish in 30 seconds; reconcile this retained plan")
                        applied = await asyncio.wait_for(call("build_arrangement", dict(
                            common, action="apply", plan_id=plan["plan_id"])), timeout=remaining)
                    assert applied["status"] == "applied", applied
                    check_plan(applied)
                    assert all(row["status"] == "verified" for row in applied["placements"]), applied
                    assert all(row["staging_cleaned"] is True for row in applied["placements"]), applied
                    assert await empty_slots() == slots
                    after_apply = await snapshot()
                    assert_only_file_additions(after_apply, baseline_tracks)
                    replay = await call("build_arrangement", dict(common, action="apply", plan_id=plan["plan_id"]))
                    assert replay["status"] == "applied" and replay["replayed"], replay
                    assert (await snapshot())["tracks"] == after_apply["tracks"]
                    assert await empty_slots() == slots
                    await call("build_arrangement", dict(common, action="preview", placements=placements), True)

                current = await snapshot()
                assert_only_file_additions(current, baseline_tracks)
                copies = {}
                for case in CASES:
                    clip = unique_named(unique_named(current["tracks"], DESTINATION)["clips"], case["name"])
                    assert_close(clip["start_time"], case["start"])
                    assert_close(clip["end_time"], case["end"])
                    actual = await clip_properties(clip["index"])
                    check_copy_properties(case, actual, fixtures[case["file"]])
                    copies[case["name"]] = {"start": clip["start_time"], "end": clip["end_time"], "properties": actual}
                if previous:
                    assert copies == previous["copies"], "Saved copies differ from workflow readback"
                evidence.append({"record": "verified_copies", "copies": copies})
                persist()
                assert await original_clip_properties(current) == originals, "Original audio clip metadata changed"
                assert await empty_slots() == slots, "Staging slots were not restored"
                for fixture in fixtures.values():
                    assert file_facts(fixture["file_path"]) == fixture, "Source WAV changed"
                assert await properties("song", TRANSPORT_FIELDS) == transport, "Transport changed"
                assert not (await call("get_automation_record_status")).get("active")
                assert_only_file_additions(await snapshot(), baseline_tracks)
                evidence.append({"phase": args.phase, "passed": True, "session_id": common["session_id"],
                                 "finished_at": datetime.now(timezone.utc).isoformat(), "verified_placements": 2,
                                 "source_files_unchanged": True, "original_arrangement_unchanged": True,
                                 "staging_slots_restored": True, "only_expected_destination_additions": True,
                                 "saved_by_runner": False, "fresh_set_instance": previous is not None})
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
    parser.add_argument("--excerpt-file", required=True)
    parser.add_argument("--full-file", required=True)
    asyncio.run(run(parser.parse_args()))
