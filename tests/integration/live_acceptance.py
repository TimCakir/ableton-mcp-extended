"""Explicit, opt-in acceptance against the named disposable Live Set.

Run manually; this is deliberately not an automatically collected pytest test.
Fixture tracks must already exist. Every MCP reply is retained as evidence.
"""

import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(args):
    evidence = []
    output = Path(args.output).resolve()
    expected_set = Path(args.set_path).resolve()
    if not expected_set.is_file() or "live-acceptance" not in str(expected_set):
        raise ValueError("Provide an existing disposable live-acceptance Set")

    def persist():
        output.write_text(json.dumps(evidence, indent=2) + "\n")

    async with stdio_client(StdioServerParameters(
            command=sys.executable, args=["-m", "MCP_Server.server"])) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            async def call(name, params=None, expect_error=False):
                result = await session.call_tool(name, params or {})
                evidence.append({"tool": name, "params": params or {},
                                 "result": result.model_dump(mode="json")})
                persist()
                assert bool(result.isError) == expect_error, result
                return result.structuredContent

            info = await call("get_build_info")
            assert Path(info["set_file_path"]).resolve() == expected_set, info
            assert info["status"] == "match", info
            tools = await session.list_tools()
            evidence.append({"discovered_tools": len(tools.tools)})
            persist()

            async def finished():
                until = time.monotonic() + 40
                while time.monotonic() < until:
                    status = await call("get_automation_record_status")
                    if not status["active"]:
                        assert status["status"] == "done", status
                        return status
                    await asyncio.sleep(.5)
                raise AssertionError("Recording did not finish within 40 seconds")

            async def verify(count):
                result = await call("verify_export_outputs")
                assert result["all_files_measured"] and len(result["outputs"]) == count, result
                for row in result["outputs"]:
                    audio = row["audio_analysis"]
                    assert row["status"] == "complete" and audio["analysis_complete"], row
                    assert not audio["below_silence_threshold"] and not audio["possible_clipping"], row
                    assert audio["duration_seconds"] > 4, row
                return result

            if args.phase == "recordings":
                await call("bounce_to_audio", {"from_bar": 1, "to_bar": 3,
                           "source": "MCP QA A", "name": "MCP QA BOUNCE A FIXED"})
                await finished()
                await verify(1)
                await call("export_stems", {"from_bar": 1, "to_bar": 3,
                           "track_names": ["MCP QA A", "MCP QA B"]})
                await finished()
                stems = await verify(2)
                levels = [row["audio_analysis"]["rms_dbfs"] for row in stems["outputs"]]
                assert abs(levels[0] - levels[1]) > 3, levels
                await call("bounce_to_audio", {"from_bar": 1, "to_bar": 9,
                           "source": "MCP QA A", "name": "MCP QA CANCELLED"})
                await asyncio.sleep(1.5)
                cancel = await call("cancel_automation_record")
                assert cancel["status"] == "cancelled" and not cancel["active"], cancel
                await call("bounce_to_audio", {"from_bar": 1, "to_bar": 3,
                           "source": "MCP QA B", "name": "MCP QA AFTER CANCEL"})
                await finished()
                await verify(1)
            elif args.phase == "arrangement":
                targets = await call("get_edit_targets")
                track = next(t for t in targets["tracks"] if t["name"] == "MCP QA MIDI VERIFIED")
                common = {"session_id": targets["session_id"]}
                placements = [{"track_handle": track["track_handle"], "source_slot": 1,
                               "destination_beat": start} for start in (0, 4)]
                await call("get_session_overview")
                source = await call("get_clip_notes", {"track_index": track["track_index"], "clip_index": 1})
                plan = await call("build_arrangement", dict(common, action="preview", placements=placements))
                applied = await call("build_arrangement", dict(common, action="apply", plan_id=plan["plan_id"]))
                assert applied["status"] == "applied" and applied["placement_count"] == 2, applied
                replay = await call("build_arrangement", dict(common, action="apply", plan_id=plan["plan_id"]))
                assert replay["replayed"] and replay["status"] == "applied", replay
                after = await call("get_clip_notes", {"track_index": track["track_index"], "clip_index": 1})
                assert source["notes"] == after["notes"], (source, after)
                await call("build_arrangement", dict(common, action="preview", placements=placements), expect_error=True)
                stale = await call("build_arrangement", dict(common, action="preview", placements=[dict(placements[0], destination_beat=16)]))
                await call("get_session_overview")
                await call("modify_clip_notes", {"track_index": track["track_index"], "clip_index": 1, "velocity_set": 82})
                await call("build_arrangement", dict(common, action="apply", plan_id=stale["plan_id"]), expect_error=True)
            snapshot = await call("get_session_snapshot")
            assert not snapshot["tracks_truncated"], snapshot
            assert all(not t["read_errors"] and not t["clips_truncated"] for t in snapshot["tracks"]), snapshot
            if args.phase == "persistence":
                rows = {t["name"]: t for t in snapshot["tracks"]}
                midi = rows["MCP QA MIDI VERIFIED"]
                placed = [c for c in midi["clips"] if c["view"] == "arrangement"]
                assert [(c["start_time"], c["end_time"]) for c in placed] == [(0, 4), (4, 8)], placed
                for name in ("MCP QA BOUNCE A FIXED", "STEM MCP QA A", "STEM MCP QA B", "MCP QA AFTER CANCEL"):
                    clips = rows[name]["clips"]
                    assert len(clips) == 1 and Path(clips[0]["file_path"]).is_file(), clips
                    assert rows[name]["arm"] is False
                for index in (1, 2):
                    notes = await call("get_clip_notes", {"track_index": midi["track_index"],
                                       "clip_index": index, "arrangement": True})
                    assert [n["pitch"] for n in notes["notes"]] == [62, 66], notes
                    assert [n["velocity"] for n in notes["notes"]] == [85, 95], notes
                source = await call("get_clip_notes", {"track_index": midi["track_index"], "clip_index": 1})
                assert [n["velocity"] for n in source["notes"]] == [82, 82], source
            evidence.append({"phase": args.phase, "passed": True})
            persist()
            print(json.dumps({"phase": args.phase, "passed": True, "evidence": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set-path", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--phase", choices=("recordings", "arrangement", "persistence"), required=True)
    asyncio.run(run(parser.parse_args()))
