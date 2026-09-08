"""Opt-in MIDI variation and audio arrangement acceptance for a disposable Set.

Run ``workflows`` once, then save and reopen the Set manually before running
``persistence``. This script never starts playback, changes source clips, or
saves the Set. It is deliberately outside automatic pytest collection.
Every MCP request and response is retained in the required JSON evidence file.
"""

import argparse
import asyncio
import copy
import json
from pathlib import Path
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


MIDI_TRACK = "MCP QA MIDI VERIFIED"
AUDIO_TRACKS = ("MCP QA A", "MCP QA B")
MIDI_CASES = (
    ("MCP V1 BREAKDOWN", 16.5, {"remove_pitches": [62], "transpose": -12}, [54]),
    ("MCP V1 THIN", 20.5, {"keep_every": 2, "keep_offset": 1}, [66]),
    ("MCP V1 PHRASE", 24.5, {"transpose": 7}, [69, 73]),
)
AUDIO_CASES = (
    ("MCP V1 AUDIO BEATS", "MCP QA A", 128.25, 134.25,
     {"start": 2, "end": 8, "units": "beats"}),
    ("MCP V1 AUDIO SECONDS", "MCP QA B", 128.5, 134.0,
     {"start": 2, "end": 5, "units": "seconds"}),
)
AUDIO_FIELDS = (
    "name", "file_path", "warping", "warp_mode", "looping", "length",
    "start_marker", "end_marker", "loop_start", "loop_end", "gain",
    "pitch_coarse", "pitch_fine", "muted", "color", "sample_length", "sample_rate",
)


def notes_without_ids(notes):
    # get_clip_notes exposes floats rounded to five decimals. Match that public
    # precision when comparing it with raw previews; the builder's own apply
    # readback compares the original unrounded musical values.
    rows = [{key: round(value, 5) if isinstance(value, float) else value
             for key, value in note.items() if key != "note_id"} for note in notes]
    return sorted(rows, key=lambda row: (
        row["start_time"], row["pitch"], row["duration"], json.dumps(row, sort_keys=True)))


def expected_notes(source, case_name):
    rows = copy.deepcopy(notes_without_ids(source))
    if case_name == "MCP V1 BREAKDOWN":
        rows = [row for row in rows if row["pitch"] != 62]
        for row in rows:
            row["pitch"] -= 12
    elif case_name == "MCP V1 THIN":
        rows = rows[1::2]
    elif case_name == "MCP V1 PHRASE":
        for row in rows:
            row["pitch"] += 7
    else:
        raise AssertionError("Unknown fixture case: " + case_name)
    return notes_without_ids(rows)


def assert_close(actual, expected):
    assert abs(actual - expected) <= 0.00001, (actual, expected)


def unique_named(rows, name):
    matches = [row for row in rows if row["name"] == name]
    assert len(matches) == 1, {"name": name, "matches": matches}
    return matches[0]


async def run(args):
    expected_set = Path(args.set_path).expanduser().resolve()
    if (not expected_set.is_file() or expected_set.suffix.lower() != ".als"
            or "live-acceptance" not in str(expected_set)):
        raise ValueError("Provide an existing disposable live-acceptance .als Set")
    output = Path(args.output).expanduser().resolve()
    if output.suffix.lower() != ".json" or output == expected_set:
        raise ValueError("Evidence output must be a separate .json file")
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = [{"phase": args.phase, "set_path": str(expected_set), "passed": False,
                 "public_note_comparison_decimal_places": 5,
                 "builder_internal_note_readback": "exact unrounded values"}]

    def persist():
        output.write_text(json.dumps(evidence, indent=2) + "\n")

    persist()
    try:
        async with stdio_client(StdioServerParameters(
                command=sys.executable, args=["-m", "MCP_Server.server"])) as streams:
            async with ClientSession(*streams) as session:
                initialized = await session.initialize()
                evidence.append({"initialize": initialized.model_dump(mode="json")})

                async def call(name, params=None, expect_error=False):
                    arguments = params if params is not None else {}
                    result = await session.call_tool(name, arguments)
                    evidence.append({"tool": name, "params": arguments,
                                     "result": result.model_dump(mode="json")})
                    persist()
                    assert bool(result.isError) == expect_error, result
                    if expect_error:
                        return result
                    assert isinstance(result.structuredContent, dict), result
                    return result.structuredContent

                info = await call("get_build_info")
                assert Path(info["set_file_path"]).resolve() == expected_set, info
                assert info["status"] == "match", info
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert "build_arrangement" in names, names
                evidence.append({"discovered_tools": len(tools.tools), "tool_names": sorted(names)})
                persist()

                async def properties(path, fields):
                    result = await call("batch", {
                        "commands": [{"command": "call_lom", "params": {"path": path, "member": field}}
                                     for field in fields],
                        "indices_are_one_based": False,
                    })
                    assert result["status"] == "success" and result["succeeded"] == len(fields), result
                    return {field: row["result"]["value"]
                            for field, row in zip(fields, result["results"])}

                transport = await properties("song", ("is_playing", "record_mode", "session_record", "tempo"))
                assert not any(transport[field] for field in ("is_playing", "record_mode", "session_record")), transport
                assert_close(transport["tempo"], 110)
                recording = await call("get_automation_record_status")
                assert not recording.get("active"), recording
                targets = await call("get_edit_targets")
                tracks = {name: unique_named(targets["tracks"], name)
                          for name in (MIDI_TRACK,) + AUDIO_TRACKS}
                assert all(track["kind"] == "track" for track in tracks.values()), tracks
                common = {"session_id": targets["session_id"]}

                async def source_notes():
                    result = await call("get_clip_notes", {
                        "track_index": tracks[MIDI_TRACK]["track_index"], "clip_index": 1})
                    assert result["next_offset"] is None, result
                    assert result["read_scope"] == "all_stored", result
                    return result["notes"]

                async def audio_properties(track_name, clip_index=None):
                    path = "tracks." + str(tracks[track_name]["track_index"] - 1)
                    if clip_index is None:
                        path += ".clip_slots.0.clip"
                    else:
                        path += ".arrangement_clips." + str(clip_index - 1)
                    return await properties(path, AUDIO_FIELDS)

                async def snapshot():
                    result = await call("get_session_snapshot")
                    assert not result["tracks_truncated"], result
                    assert all(not row["read_errors"] and not row["clips_truncated"]
                               for row in result["tracks"]), result
                    assert result["session_id"] == common["session_id"], result
                    return result

                before_notes = await source_notes()
                assert [row["pitch"] for row in notes_without_ids(before_notes)] == [62, 66], before_notes
                assert [row["velocity"] for row in notes_without_ids(before_notes)] == [82, 82], before_notes
                before_audio = {name: await audio_properties(name) for name in AUDIO_TRACKS}
                for name, expected_name, warped, end in (
                        ("MCP QA A", "MCP QA AUDIO SOURCE", True, 16),
                        ("MCP QA B", "MCP QA AUDIO SECONDS", False, 12)):
                    row = before_audio[name]
                    assert row["name"] == expected_name and bool(row["warping"]) == warped, row
                    assert Path(row["file_path"]).is_file(), row
                    for field in ("start_marker", "loop_start", "pitch_coarse", "pitch_fine"):
                        assert_close(row[field], 0)
                    for field in ("end_marker", "loop_end"):
                        assert_close(row[field], end)
                before_snapshot = await snapshot()

                placements = [{"track_handle": tracks[MIDI_TRACK]["track_handle"], "source_slot": 1,
                               "destination_beat": start, "name": name, "variation": variation}
                              for name, start, variation, _pitches in MIDI_CASES]
                placements += [{"track_handle": tracks[track_name]["track_handle"], "source_slot": 1,
                                "destination_beat": start, "name": name, "source_range": source_range}
                               for name, track_name, start, _end, source_range in AUDIO_CASES]

                if args.phase == "workflows":
                    case_names = {row["name"] for row in placements}
                    assert not any(clip["name"] in case_names for track in before_snapshot["tracks"]
                                   for clip in track["clips"]), "Acceptance names already exist; inspect the Set before rerunning"
                    # The second invalid entry must be rejected without placing the first.
                    invalid = copy.deepcopy(placements[:2])
                    invalid[1]["variation"] = {"transpose": 127}
                    await call("build_arrangement", dict(common, action="preview", placements=invalid), True)
                    invalid_audio = copy.deepcopy(placements[-2])
                    invalid_audio["source_range"]["units"] = "seconds"
                    await call("build_arrangement", dict(common, action="preview", placements=[invalid_audio]), True)
                    unchanged = await snapshot()
                    assert unchanged["tracks"] == before_snapshot["tracks"], unchanged

                    plan = await call("build_arrangement", dict(common, action="preview", placements=placements))
                    assert plan["status"] == "preview" and plan["placement_count"] == 5, plan
                    for name, start, _variation, pitches in MIDI_CASES:
                        row = unique_named(plan["placements"], name)
                        assert notes_without_ids(row["notes"]) == expected_notes(before_notes, name), row
                        assert [note["pitch"] for note in notes_without_ids(row["notes"])] == pitches, row
                        assert_close(row["destination_beat"], start)
                        assert_close(row["end_beat"], start + 4)
                    for name, _track, start, end, source_range in AUDIO_CASES:
                        row = unique_named(plan["placements"], name)
                        assert row["source_range"] == source_range and row["kind"] == "audio", row
                        assert_close(row["destination_beat"], start)
                        assert_close(row["end_beat"], end)
                    assert (await snapshot())["tracks"] == before_snapshot["tracks"]
                    applied = await call("build_arrangement", dict(common, action="apply", plan_id=plan["plan_id"]))
                    # Unwarped audio settles on a later Live tick. Poll only
                    # this retained plan; never create or replay a new edit.
                    deadline = time.monotonic() + 10
                    while applied["status"] == "applying":
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise AssertionError("Arrangement plan did not finish within 10 seconds")
                        await asyncio.sleep(min(0.2, remaining))
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise AssertionError("Arrangement plan did not finish within 10 seconds")
                        applied = await asyncio.wait_for(
                            call("build_arrangement", dict(common, action="apply", plan_id=plan["plan_id"])),
                            timeout=remaining,
                        )
                    assert applied["status"] == "applied" and applied["placement_count"] == 5, applied
                    assert all(row["status"] == "verified" for row in applied["placements"]), applied
                    after_apply = await snapshot()
                    replay = await call("build_arrangement", dict(common, action="apply", plan_id=plan["plan_id"]))
                    assert replay["status"] == "applied" and replay["replayed"], replay
                    assert (await snapshot())["tracks"] == after_apply["tracks"]
                    await call("build_arrangement", dict(common, action="preview", placements=placements), True)

                current = await snapshot()
                for name, start, _variation, pitches in MIDI_CASES:
                    track = unique_named(current["tracks"], MIDI_TRACK)
                    clip = unique_named([clip for clip in track["clips"] if clip["view"] == "arrangement"], name)
                    assert_close(clip["start_time"], start)
                    assert_close(clip["end_time"], start + 4)
                    result = await call("get_clip_notes", {"track_index": track["track_index"],
                        "clip_index": clip["index"], "arrangement": True, "clip_name": name})
                    assert result["next_offset"] is None, result
                    assert notes_without_ids(result["notes"]) == expected_notes(before_notes, name), result
                    assert [note["pitch"] for note in notes_without_ids(result["notes"])] == pitches, result
                for name, track_name, start, end, source_range in AUDIO_CASES:
                    track = unique_named(current["tracks"], track_name)
                    clip = unique_named([clip for clip in track["clips"] if clip["view"] == "arrangement"], name)
                    assert_close(clip["start_time"], start)
                    assert_close(clip["end_time"], end)
                    actual = await audio_properties(track_name, clip["index"])
                    assert not actual["looping"] and actual["name"] == name, actual
                    assert Path(actual["file_path"]).is_file(), actual
                    for field in AUDIO_FIELDS:
                        if field not in {"name", "looping", "length", "start_marker", "end_marker", "loop_start", "loop_end"}:
                            assert actual[field] == before_audio[track_name][field], (field, actual)
                    for field in ("start_marker", "loop_start"):
                        assert_close(actual[field], source_range["start"])
                    for field in ("end_marker", "loop_end"):
                        assert_close(actual[field], source_range["end"])

                assert await source_notes() == before_notes, "Source MIDI notes changed"
                for name in AUDIO_TRACKS:
                    assert await audio_properties(name) == before_audio[name], "Source audio changed: " + name
                final_transport = await properties("song", ("is_playing", "record_mode", "session_record", "tempo"))
                assert final_transport == transport, (transport, final_transport)
                evidence.append({"phase": args.phase, "passed": True,
                                 "verified_placements": len(placements), "source_clips_unchanged": True,
                                 "saved_by_runner": False})
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
    asyncio.run(run(parser.parse_args()))
