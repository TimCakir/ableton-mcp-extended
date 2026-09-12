"""Opt-in chord-aware variation acceptance on an existing disposable Live Set.

The MCP QA CHORDS track must already have the documented 12-note source in slot
1. Run workflows once, save and unload/reopen the Set in Live, then run
persistence with --workflow-evidence pointing to that successful workflow JSON.
This script never creates fixtures, changes source clips, starts playback or
saves the Set. Every MCP request and response is recorded in --output.
"""

import argparse
import asyncio
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from MCP_Server import __version__


EXPECTED_BUILD = "2026-09-08.5"
EXPECTED_VERSION = "1.3.0"
TRACK = "MCP QA CHORDS"
SOURCE_NAME = "MCP CHORD SOURCE"
SOURCE_LENGTH = 8.0
SOURCE_PAIRS = (
    (0, 60), (0, 64), (0, 67),
    (2, 62), (2, 65), (2, 69),
    (4, 64), (4, 67), (4, 71),
    (6, 65), (6, 69), (6, 72),
)
# Explicit expected notes make this an independent acceptance check rather
# than calculating expectations with the implementation being tested.
CASES = (
    ("MCP CHORD EVEN", 32.5, {"thin_by": "onset", "keep_every": 2},
     ((0, 60), (0, 64), (0, 67), (4, 64), (4, 67), (4, 71))),
    ("MCP CHORD ODD", 40.5,
     {"thin_by": "onset", "keep_every": 2, "keep_offset": 1, "transpose": 12},
     ((2, 74), (2, 77), (2, 81), (6, 77), (6, 81), (6, 84))),
    ("MCP CHORD NOTES", 48.5, {"keep_every": 2},
     ((0, 60), (0, 67), (2, 65), (4, 64), (4, 71), (6, 69))),
)
CLIP_FIELDS = (
    "name", "length", "looping", "loop_start", "loop_end",
    "start_marker", "end_marker", "muted", "color",
)


def notes_without_ids(notes):
    # The public note endpoint rounds to five decimals. Internal apply readback
    # checks unrounded values; this cross-endpoint comparison uses public precision.
    rows = [{key: round(value, 5) if isinstance(value, float) else value
             for key, value in note.items() if key != "note_id"} for note in notes]
    return sorted(rows, key=lambda row: (
        row["start_time"], row["pitch"], row["duration"], json.dumps(row, sort_keys=True)))


def expected_notes(source, pairs, transpose=0):
    by_note = {(row["start_time"], row["pitch"]): row for row in notes_without_ids(source)}
    return notes_without_ids([
        dict(by_note[(start, pitch - transpose)], pitch=pitch) for start, pitch in pairs])


def unique_named(rows, name):
    matches = [row for row in rows if row["name"] == name]
    assert len(matches) == 1, {"name": name, "matches": matches}
    return matches[0]


def assert_close(actual, expected):
    assert abs(actual - expected) <= 0.00001, (actual, expected)


async def run(args):
    if not __debug__:
        raise RuntimeError("Acceptance guards require Python assertions; do not run with -O")
    expected_set = Path(args.set_path).expanduser().resolve()
    if (not expected_set.is_file() or expected_set.suffix.lower() != ".als"
            or "live-acceptance" not in str(expected_set)):
        raise ValueError("Provide an existing disposable live-acceptance .als Set")
    output = Path(args.output).expanduser().resolve()
    if output.suffix.lower() != ".json" or output == expected_set:
        raise ValueError("Evidence output must be a separate .json file")
    previous = None
    prior_bytes = None
    if args.phase == "persistence":
        if not args.workflow_evidence:
            raise ValueError("Persistence requires --workflow-evidence from a successful workflow run")
        prior_path = Path(args.workflow_evidence).expanduser().resolve()
        if prior_path == output:
            raise ValueError("Persistence evidence must not overwrite the workflow evidence")
        prior_bytes = prior_path.read_bytes()
        prior = json.loads(prior_bytes)
        summaries = [row for row in prior if row.get("phase") == "workflows" and row.get("passed")]
        baselines = [row for row in prior if row.get("record") == "fixture_baseline"]
        if len(summaries) != 1 or len(baselines) != 1 or not prior[-1].get("passed"):
            raise ValueError("Workflow evidence must contain one completed successful run and fixture baseline")
        previous = {"summary": summaries[0], "baseline": baselines[0]}
        assert Path(prior[0]["set_path"]).resolve() == expected_set, prior[0]
        assert prior[0]["expected_build"] == EXPECTED_BUILD, prior[0]
        assert prior[0]["expected_version"] == EXPECTED_VERSION, prior[0]
    elif args.workflow_evidence:
        raise ValueError("--workflow-evidence is used only for the persistence phase")

    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = [{"phase": args.phase, "set_path": str(expected_set), "passed": False,
                 "started_at": datetime.now(timezone.utc).isoformat(),
                 "expected_build": EXPECTED_BUILD, "expected_version": EXPECTED_VERSION,
                 "package_version": __version__, "public_note_comparison_decimal_places": 5,
                 "builder_internal_note_readback": "exact unrounded values"}]
    if previous:
        evidence[0]["workflow_evidence_sha256"] = hashlib.sha256(prior_bytes).hexdigest()
        evidence[0]["prior_session_id"] = previous["summary"]["session_id"]

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
                for key in ("server_build", "repo_build", "remote_script_build"):
                    assert info[key] == EXPECTED_BUILD, info
                tools = await session.list_tools()
                tool = unique_named([item.model_dump(mode="json") for item in tools.tools], "build_arrangement")
                assert "thin_by" in tool["description"] and "onset" in tool["description"], tool
                evidence.append({"discovered_tools": len(tools.tools), "build_arrangement": tool})
                persist()

                async def properties(path, fields):
                    result = await call("batch", {
                        "commands": [{"command": "call_lom", "params": {"path": path, "member": field}}
                                     for field in fields], "indices_are_one_based": False})
                    assert result["status"] == "success" and result["succeeded"] == len(fields), result
                    return {field: row["result"]["value"]
                            for field, row in zip(fields, result["results"])}

                transport = await properties("song", ("is_playing", "record_mode", "session_record", "tempo"))
                assert not any(transport[field] for field in ("is_playing", "record_mode", "session_record")), transport
                recording = await call("get_automation_record_status")
                assert not recording.get("active"), recording
                targets = await call("get_edit_targets")
                track = unique_named(targets["tracks"], TRACK)
                assert track["kind"] == "track", track
                common = {"session_id": targets["session_id"]}
                if previous:
                    assert common["session_id"] != previous["summary"]["session_id"], (
                        "Set instance unchanged; save, unload and reopen it before persistence acceptance")

                async def source_notes():
                    result = await call("get_clip_notes", {"track_index": track["track_index"], "clip_index": 1})
                    assert result["next_offset"] is None and result["read_scope"] == "all_stored", result
                    return result["notes"]

                async def source_properties():
                    return await properties("tracks." + str(track["track_index"] - 1) + ".clip_slots.0.clip", CLIP_FIELDS)

                async def snapshot():
                    result = await call("get_session_snapshot")
                    assert not result["tracks_truncated"], result
                    assert all(not row["read_errors"] and not row["clips_truncated"]
                               for row in result["tracks"]), result
                    assert result["session_id"] == common["session_id"], result
                    return result

                before_notes = await source_notes()
                normalized = notes_without_ids(before_notes)
                assert [(row["start_time"], row["pitch"]) for row in normalized] == list(SOURCE_PAIRS), normalized
                for row in normalized:
                    assert_close(row["duration"], 1)
                    assert_close(row["velocity"], 90)
                    assert_close(row["probability"], 0.8)
                    assert row["mute"] is False, row
                before_properties = await source_properties()
                assert before_properties["name"] == SOURCE_NAME, before_properties
                for field in ("length", "loop_end", "end_marker"):
                    assert_close(before_properties[field], SOURCE_LENGTH)
                for field in ("loop_start", "start_marker"):
                    assert_close(before_properties[field], 0)
                assert before_properties["looping"] and not before_properties["muted"], before_properties
                if previous:
                    assert normalized == previous["baseline"]["notes"], "Saved source notes changed"
                    assert before_properties == previous["baseline"]["properties"], "Saved source metadata changed"
                evidence.append({"record": "fixture_baseline", "session_id": common["session_id"],
                                 "notes": normalized, "properties": before_properties})
                persist()
                before_snapshot = await snapshot()
                placements = [{"track_handle": track["track_handle"], "source_slot": 1,
                               "destination_beat": start, "name": name, "variation": variation}
                              for name, start, variation, _pairs in CASES]

                if args.phase == "workflows":
                    names = {row["name"] for row in placements}
                    assert not any(clip["name"] in names for row in before_snapshot["tracks"] for clip in row["clips"]), (
                        "Acceptance names already exist; inspect the Set before rerunning")
                    invalid = copy.deepcopy(placements[:2])
                    invalid[1]["variation"]["thin_by"] = "chord"
                    await call("build_arrangement", dict(common, action="preview", placements=invalid), True)
                    assert (await snapshot())["tracks"] == before_snapshot["tracks"]
                    plan = await call("build_arrangement", dict(common, action="preview", placements=placements))
                    assert plan["status"] == "preview" and plan["placement_count"] == 3, plan
                    assert plan["note_count"] == 18 and plan["source_note_count"] == 36, plan
                    for name, start, variation, pairs in CASES:
                        row = unique_named(plan["placements"], name)
                        assert row["kind"] == "midi" and row["note_count"] == 6, row
                        assert row["source_note_count"] == 12, row
                        assert row["variation"]["thin_by"] == variation.get("thin_by", "note"), row
                        assert notes_without_ids(row["notes"]) == expected_notes(before_notes, pairs, variation.get("transpose", 0)), row
                        assert_close(row["destination_beat"], start)
                        assert_close(row["end_beat"], start + SOURCE_LENGTH)
                    assert (await snapshot())["tracks"] == before_snapshot["tracks"]
                    applied = await call("build_arrangement", dict(common, action="apply", plan_id=plan["plan_id"]))
                    assert applied["status"] == "applied" and applied["placement_count"] == 3, applied
                    assert all(row["status"] == "verified" for row in applied["placements"]), applied
                    after_apply = await snapshot()
                    replay = await call("build_arrangement", dict(common, action="apply", plan_id=plan["plan_id"]))
                    assert replay["status"] == "applied" and replay["replayed"], replay
                    assert (await snapshot())["tracks"] == after_apply["tracks"]
                    await call("build_arrangement", dict(common, action="preview", placements=placements), True)

                current = await snapshot()
                current_track = unique_named(current["tracks"], TRACK)
                for name, start, variation, pairs in CASES:
                    clip = unique_named([clip for clip in current_track["clips"] if clip["view"] == "arrangement"], name)
                    assert_close(clip["start_time"], start)
                    assert_close(clip["end_time"], start + SOURCE_LENGTH)
                    result = await call("get_clip_notes", {"track_index": current_track["track_index"],
                        "clip_index": clip["index"], "arrangement": True, "clip_name": name})
                    assert result["next_offset"] is None and result["read_scope"] == "all_stored", result
                    actual = notes_without_ids(result["notes"])
                    assert [(row["start_time"], row["pitch"]) for row in actual] == list(pairs), result
                    assert actual == expected_notes(before_notes, pairs, variation.get("transpose", 0)), result
                    actual_properties = await properties(
                        "tracks." + str(current_track["track_index"] - 1)
                        + ".arrangement_clips." + str(clip["index"] - 1), CLIP_FIELDS)
                    assert actual_properties == dict(before_properties, name=name), actual_properties

                assert await source_notes() == before_notes, "Source MIDI notes changed"
                assert await source_properties() == before_properties, "Source metadata changed"
                final_transport = await properties("song", ("is_playing", "record_mode", "session_record", "tempo"))
                assert final_transport == transport, (transport, final_transport)
                evidence.append({"phase": args.phase, "passed": True, "session_id": common["session_id"],
                                 "finished_at": datetime.now(timezone.utc).isoformat(),
                                 "verified_placements": 3, "verified_notes": 18, "source_clips_unchanged": True,
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
    asyncio.run(run(parser.parse_args()))
