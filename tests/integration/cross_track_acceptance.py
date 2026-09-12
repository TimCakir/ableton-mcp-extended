"""Opt-in cross-track MIDI/audio acceptance on a disposable saved Live Set.

Existing sources: MCP QA CHORDS, MCP QA A and MCP QA B, each in Session slot 1.
Empty destinations: MCP QA CROSS MIDI and MCP QA CROSS AUDIO. Run workflows,
save and unload/reopen the Set, then run persistence with --workflow-evidence.
This runner never creates fixtures, changes sources, starts transport or saves.
It records requests before dispatch and responses in the required JSON output.
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

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from MCP_Server import __version__


EXPECTED_VERSION = "1.4.0"
EXPECTED_BUILD = "2026-09-08.6"
MIDI_SOURCE = "MCP QA CHORDS"
MIDI_DESTINATION = "MCP QA CROSS MIDI"
AUDIO_DESTINATION = "MCP QA CROSS AUDIO"
AUDIO_SOURCES = ("MCP QA A", "MCP QA B")
SOURCE_TRACKS = (MIDI_SOURCE,) + AUDIO_SOURCES
DESTINATION_TRACKS = (MIDI_DESTINATION, AUDIO_DESTINATION)
SOURCE_PAIRS = (
    (0, 60), (0, 64), (0, 67), (2, 62), (2, 65), (2, 69),
    (4, 64), (4, 67), (4, 71), (6, 65), (6, 69), (6, 72),
)
EXPECTED_MIDI_PAIRS = ((0, 72), (0, 76), (0, 79), (4, 76), (4, 79), (4, 83))
CASES = (
    {"name": "MCP CROSS CHORDS", "source": MIDI_SOURCE, "destination": MIDI_DESTINATION,
     "kind": "midi", "start": 64.5, "end": 72.5,
     "variation": {"thin_by": "onset", "keep_every": 2, "transpose": 12}},
    {"name": "MCP CROSS BEATS", "source": "MCP QA A", "destination": AUDIO_DESTINATION,
     "kind": "audio", "start": 192.25, "end": 198.25,
     "source_range": {"start": 2, "end": 8, "units": "beats"}},
    {"name": "MCP CROSS SECONDS", "source": "MCP QA B", "destination": AUDIO_DESTINATION,
     "kind": "audio", "start": 224.5, "end": 230.0,
     "source_range": {"start": 2, "end": 5, "units": "seconds"}},
)
MIDI_FIELDS = (
    "name", "length", "looping", "loop_start", "loop_end",
    "start_marker", "end_marker", "muted", "color",
)
AUDIO_FIELDS = (
    "name", "file_path", "warping", "warp_mode", "looping", "length",
    "start_marker", "end_marker", "loop_start", "loop_end", "gain",
    "pitch_coarse", "pitch_fine", "muted", "color", "sample_length", "sample_rate",
)
TRANSPORT_FIELDS = ("is_playing", "record_mode", "session_record", "tempo")


def notes_without_ids(notes):
    """Use public endpoint precision; internal builder readback stays exact."""
    rows = [{key: round(value, 5) if isinstance(value, float) else value
             for key, value in note.items() if key != "note_id"} for note in notes]
    return sorted(rows, key=lambda row: (
        row["start_time"], row["pitch"], row["duration"], json.dumps(row, sort_keys=True)))


def expected_midi_notes(source):
    # Explicit pitch/onset pairs are independent of the implementation under test.
    by_note = {(row["start_time"], row["pitch"]): row for row in notes_without_ids(source)}
    return notes_without_ids([dict(by_note[(start, pitch - 12)], pitch=pitch)
                              for start, pitch in EXPECTED_MIDI_PAIRS])


def unique_named(rows, name):
    matches = [row for row in rows if row["name"] == name]
    assert len(matches) == 1, {"name": name, "matches": matches}
    return matches[0]


def assert_close(actual, expected):
    assert abs(actual - expected) <= 0.00001, (actual, expected)


def snapshot_tracks(snapshot, without_copies=False):
    """Ignore only transient track handles when comparing a reloaded Set."""
    rows = copy.deepcopy(snapshot["tracks"])
    for row in rows:
        row.pop("track_handle", None)
        if without_copies:
            names = {case["name"] for case in CASES if case["destination"] == row["name"]}
            row["clips"] = [clip for clip in row["clips"]
                            if not (clip["view"] == "arrangement" and clip["name"] in names)]
            row["clip_count"] = len(row["clips"])
    return rows


def assert_only_expected_additions(current, baseline_tracks):
    assert snapshot_tracks(current, without_copies=True) == baseline_tracks, (
        "Source tracks, original clips or other Set content changed")
    for destination in DESTINATION_TRACKS:
        clips = unique_named(current["tracks"], destination)["clips"]
        names = {case["name"] for case in CASES if case["destination"] == destination}
        assert len(clips) == len(names), (destination, clips)
        assert {clip["name"] for clip in clips if clip["view"] == "arrangement"} == names, clips


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
        raise ValueError("Provide evidence from one completed successful cross-track workflow run")
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
    previous = None
    if args.phase == "persistence":
        if not args.workflow_evidence:
            raise ValueError("Persistence requires --workflow-evidence from the successful workflow run")
        previous = read_workflow_evidence(args.workflow_evidence, expected_set, output)
    elif args.workflow_evidence:
        raise ValueError("--workflow-evidence is used only for persistence")
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = [{"phase": args.phase, "set_path": str(expected_set), "passed": False,
                 "started_at": datetime.now(timezone.utc).isoformat(),
                 "expected_build": EXPECTED_BUILD, "expected_version": EXPECTED_VERSION,
                 "package_version": __version__, "public_note_comparison_decimal_places": 5,
                 "builder_internal_note_readback": "exact unrounded values"}]
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
                assert "destination_track_handle" in tool["description"], tool
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
                          for name in SOURCE_TRACKS + DESTINATION_TRACKS}
                assert all(row["kind"] == "track" for row in tracks.values()), tracks
                common = {"session_id": targets["session_id"]}
                if previous:
                    assert common["session_id"] != previous["summary"]["session_id"], (
                        "Save, unload and reopen the Set before persistence acceptance")

                async def notes(track_name, clip_index=1, arrangement=False, clip_name=None):
                    params = {"track_index": tracks[track_name]["track_index"],
                              "clip_index": clip_index, "arrangement": arrangement}
                    if clip_name is not None:
                        params["clip_name"] = clip_name
                    result = await call("get_clip_notes", params)
                    assert result["next_offset"] is None and result["read_scope"] == "all_stored", result
                    return result["notes"]

                async def clip_properties(track_name, fields, arrangement_index=None):
                    path = "tracks." + str(tracks[track_name]["track_index"] - 1)
                    path += (".clip_slots.0.clip" if arrangement_index is None else
                             ".arrangement_clips." + str(arrangement_index - 1))
                    return await properties(path, fields)

                async def snapshot():
                    result = await call("get_session_snapshot")
                    assert not result["tracks_truncated"], result
                    assert all(not row["read_errors"] and not row["clips_truncated"]
                               for row in result["tracks"]), result
                    assert result["session_id"] == common["session_id"], result
                    return result

                before_notes = await notes(MIDI_SOURCE)
                normalized = notes_without_ids(before_notes)
                assert [(row["start_time"], row["pitch"]) for row in normalized] == list(SOURCE_PAIRS), normalized
                for row in normalized:
                    for field, value in (("duration", 1), ("velocity", 90), ("probability", 0.8)):
                        assert_close(row[field], value)
                    assert row["mute"] is False, row
                source_properties = {MIDI_SOURCE: await clip_properties(MIDI_SOURCE, MIDI_FIELDS)}
                midi = source_properties[MIDI_SOURCE]
                assert midi["name"] == "MCP CHORD SOURCE" and midi["looping"] and not midi["muted"], midi
                for field in ("length", "loop_end", "end_marker"):
                    assert_close(midi[field], 8)
                for field in ("loop_start", "start_marker"):
                    assert_close(midi[field], 0)
                for name, source_name, warped, end in (
                        ("MCP QA A", "MCP QA AUDIO SOURCE", True, 16),
                        ("MCP QA B", "MCP QA AUDIO SECONDS", False, 12)):
                    row = source_properties[name] = await clip_properties(name, AUDIO_FIELDS)
                    assert row["name"] == source_name and bool(row["warping"]) == warped, row
                    assert Path(row["file_path"]).is_file(), row
                    for field in ("loop_start", "start_marker", "pitch_coarse", "pitch_fine"):
                        assert_close(row[field], 0)
                    for field in ("loop_end", "end_marker"):
                        assert_close(row[field], end)
                before_snapshot = await snapshot()
                baseline_tracks = (previous["baseline"]["tracks"] if previous else snapshot_tracks(before_snapshot))
                if previous:
                    assert normalized == previous["baseline"]["notes"], "Saved source notes changed"
                    assert source_properties == previous["baseline"]["source_properties"], "Saved source metadata changed"
                    assert transport == previous["baseline"]["transport"], "Saved transport state changed"
                    assert_only_expected_additions(before_snapshot, baseline_tracks)
                else:
                    for name in DESTINATION_TRACKS:
                        assert unique_named(before_snapshot["tracks"], name)["clips"] == [], (
                            "Acceptance destination must be empty: " + name)
                evidence.append({"record": "fixture_baseline", "session_id": common["session_id"],
                                 "notes": normalized, "source_properties": source_properties,
                                 "tracks": baseline_tracks, "transport": transport})
                persist()

                placements = []
                for case in CASES:
                    placement = {"track_handle": tracks[case["source"]]["track_handle"],
                                 "destination_track_handle": tracks[case["destination"]]["track_handle"],
                                 "source_slot": 1, "destination_beat": case["start"], "name": case["name"]}
                    for option in ("variation", "source_range"):
                        if option in case:
                            placement[option] = copy.deepcopy(case[option])
                    placements.append(placement)

                def check_plan(plan):
                    assert plan["placement_count"] == 3, plan
                    for case in CASES:
                        row = unique_named(plan["placements"], case["name"])
                        assert row["track_handle"] == tracks[case["source"]]["track_handle"], row
                        assert row["track_name"] == case["source"], row
                        assert row["source_slot"] == 1 and row["source_name"] == source_properties[case["source"]]["name"], row
                        assert row["destination_track_handle"] == tracks[case["destination"]]["track_handle"], row
                        assert row["destination_track_name"] == case["destination"], row
                        assert row["kind"] == case["kind"], row
                        assert_close(row["destination_beat"], case["start"])
                        assert_close(row["end_beat"], case["end"])
                        if case["kind"] == "midi":
                            assert row["note_count"] == 6 and row["source_note_count"] == 12, row
                            assert notes_without_ids(row["notes"]) == expected_midi_notes(before_notes), row
                            assert row["variation"]["thin_by"] == "onset", row
                        else:
                            assert row["source_range"] == case["source_range"], row

                if args.phase == "workflows":
                    names = {case["name"] for case in CASES}
                    assert not any(clip["name"] in names for row in before_snapshot["tracks"] for clip in row["clips"]), (
                        "Acceptance copy names already exist; inspect the Set before rerunning")
                    invalid = copy.deepcopy(placements[:2])
                    invalid[1]["destination_track_handle"] = tracks[MIDI_DESTINATION]["track_handle"]
                    await call("build_arrangement", dict(common, action="preview", placements=invalid), True)
                    invalid_midi = copy.deepcopy(placements[0])
                    invalid_midi["destination_track_handle"] = tracks[AUDIO_DESTINATION]["track_handle"]
                    await call("build_arrangement", dict(common, action="preview", placements=[invalid_midi]), True)
                    overlap = [copy.deepcopy(placements[1]), copy.deepcopy(placements[1])]
                    overlap[1]["destination_beat"] += 1
                    await call("build_arrangement", dict(common, action="preview", placements=overlap), True)
                    assert (await snapshot())["tracks"] == before_snapshot["tracks"]
                    plan = await call("build_arrangement", dict(common, action="preview", placements=placements))
                    assert plan["status"] == "preview", plan
                    check_plan(plan)
                    assert (await snapshot())["tracks"] == before_snapshot["tracks"]
                    applied = await call("build_arrangement", dict(common, action="apply", plan_id=plan["plan_id"]))
                    deadline = time.monotonic() + 10
                    while applied["status"] == "applying":
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise AssertionError("Retained plan did not finish within 10 seconds; reconcile this plan")
                        await asyncio.sleep(min(0.2, remaining))
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise AssertionError("Retained plan did not finish within 10 seconds; reconcile this plan")
                        applied = await asyncio.wait_for(call("build_arrangement", dict(
                            common, action="apply", plan_id=plan["plan_id"])), timeout=remaining)
                    assert applied["status"] == "applied", applied
                    check_plan(applied)
                    assert all(row["status"] == "verified" for row in applied["placements"]), applied
                    after_apply = await snapshot()
                    assert_only_expected_additions(after_apply, baseline_tracks)
                    replay = await call("build_arrangement", dict(common, action="apply", plan_id=plan["plan_id"]))
                    assert replay["status"] == "applied" and replay["replayed"], replay
                    assert (await snapshot())["tracks"] == after_apply["tracks"]
                    await call("build_arrangement", dict(common, action="preview", placements=placements), True)

                current = await snapshot()
                assert_only_expected_additions(current, baseline_tracks)
                verified_copies = {}
                for case in CASES:
                    track = unique_named(current["tracks"], case["destination"])
                    clip = unique_named(track["clips"], case["name"])
                    assert clip["view"] == "arrangement", clip
                    assert_close(clip["start_time"], case["start"])
                    assert_close(clip["end_time"], case["end"])
                    fields = MIDI_FIELDS if case["kind"] == "midi" else AUDIO_FIELDS
                    actual = await clip_properties(case["destination"], fields, clip["index"])
                    expected = dict(source_properties[case["source"]], name=case["name"])
                    copy_notes = None
                    if case["kind"] == "midi":
                        copy_notes = notes_without_ids(await notes(case["destination"], clip["index"], True, case["name"]))
                        assert [(row["start_time"], row["pitch"]) for row in copy_notes] == list(EXPECTED_MIDI_PAIRS), copy_notes
                        assert copy_notes == expected_midi_notes(before_notes), copy_notes
                    else:
                        source_range = case["source_range"]
                        expected.update(looping=False, length=case["end"] - case["start"],
                                        start_marker=source_range["start"], loop_start=source_range["start"],
                                        end_marker=source_range["end"], loop_end=source_range["end"])
                        assert Path(actual["file_path"]).is_file(), actual
                    assert actual == expected, {"actual": actual, "expected": expected}
                    verified_copies[case["name"]] = {"destination": case["destination"],
                        "start": clip["start_time"], "end": clip["end_time"], "properties": actual, "notes": copy_notes}
                if previous:
                    assert verified_copies == previous["copies"], "Saved copies differ from workflow readbacks"
                evidence.append({"record": "verified_copies", "copies": verified_copies})
                persist()
                assert await notes(MIDI_SOURCE) == before_notes, "Source MIDI notes changed"
                for name in SOURCE_TRACKS:
                    fields = MIDI_FIELDS if name == MIDI_SOURCE else AUDIO_FIELDS
                    assert await clip_properties(name, fields) == source_properties[name], "Source metadata changed: " + name
                assert await properties("song", TRANSPORT_FIELDS) == transport, "Transport changed"
                assert not (await call("get_automation_record_status")).get("active")
                assert_only_expected_additions(await snapshot(), baseline_tracks)
                evidence.append({"phase": args.phase, "passed": True, "session_id": common["session_id"],
                                 "finished_at": datetime.now(timezone.utc).isoformat(),
                                 "verified_placements": 3, "verified_midi_notes": 6,
                                 "source_clips_unchanged": True, "source_arrangements_unchanged": True,
                                 "only_expected_destination_additions": True, "saved_by_runner": False,
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
    asyncio.run(run(parser.parse_args()))
