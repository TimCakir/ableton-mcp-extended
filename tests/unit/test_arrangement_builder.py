"""Arrangement plans preserve source identity and fail before destructive overlap."""

import copy
import os
import subprocess
import sys
import textwrap
import types

import pytest

framework = types.ModuleType("_Framework")
surface = types.ModuleType("_Framework.ControlSurface")
surface.ControlSurface = type("ControlSurface", (), {})
sys.modules.setdefault("_Framework", framework)
sys.modules.setdefault("_Framework.ControlSurface", surface)

from AbletonMCP_Remote_Script import AbletonMCP
from AbletonMCP_Remote_Script import arrangement_builder as builder


class Clip:
    def __init__(self, name="Pattern", start=0.0, length=4.0):
        self.name, self.length = name, length
        self.is_midi_clip = True
        self.has_envelopes = False
        self.looping, self.loop_start, self.loop_end = True, 0.0, length
        self.start_marker, self.end_marker = 0.0, length
        self.start_time, self.end_time = start, start + length
        self.muted, self.color = False, 32
        self.notes = [types.SimpleNamespace(pitch=60, start_time=0.0, duration=0.25,
            velocity=90.0, mute=False, note_id=1, probability=1.0,
            velocity_deviation=0.0, release_velocity=64.0)]

    def get_all_notes_extended(self):
        return copy.deepcopy(self.notes)


class Track:
    def __init__(self, name="Piano"):
        self.name = name
        self.is_foldable, self.is_frozen = False, False
        self.clip_slots = [types.SimpleNamespace(has_clip=True, clip=Clip())]
        self.arrangement_clips = []
        self.copy_calls, self.delete_calls = 0, []
        self.copy_failure, self.delete_failure = None, False

    def duplicate_clip_to_arrangement(self, clip, start):
        self.copy_calls += 1
        if self.copy_failure == (self.copy_calls, "before"):
            raise RuntimeError("copy failed before insertion")
        if self.copy_failure == (self.copy_calls, "no_output"):
            return
        duplicate = copy.deepcopy(clip)
        duplicate.start_time, duplicate.end_time = start, start + clip.length
        duplicate.notes[0].note_id += 100
        self.arrangement_clips.append(duplicate)
        if self.copy_failure == (self.copy_calls, "after"):
            raise RuntimeError("copy failed after insertion")
        if self.copy_failure == (self.copy_calls, "wrong_bounds"):
            duplicate.end_time += 1
        if self.copy_failure == (self.copy_calls, "wrong_notes"):
            duplicate.notes[0].pitch += 1

    def delete_clip(self, clip):
        self.delete_calls.append(clip)
        if self.delete_failure:
            raise RuntimeError("could not remove created clip")
        self.arrangement_clips.remove(clip)


class Song:
    def __init__(self):
        self.tracks, self.return_tracks = [Track()], []
        self.name, self.file_path = "Test Lab", "/tmp/Test Lab.als"
        self.undo_started, self.undo_finished = 0, 0

    def begin_undo_step(self):
        self.undo_started += 1

    def end_undo_step(self):
        self.undo_finished += 1


def remote():
    instance = AbletonMCP.__new__(AbletonMCP)
    song = Song()
    instance.song = lambda: song
    instance.log_message = lambda message: None
    instance._auto_rec = {"active": False}
    instance.running, instance._closing = True, False
    return instance, song


def request(instance, starts=(0.0,)):
    targets = instance._get_edit_targets()
    session = targets["session_id"]
    handle = targets["tracks"][0]["track_handle"]
    return session, [{"track_handle": handle, "source_slot": 1,
                      "destination_beat": start} for start in starts]


def preview(instance, starts=(0.0,)):
    session, placements = request(instance, starts)
    return builder.build_arrangement(instance, "preview", session, placements)


def apply(instance, plan):
    return builder.build_arrangement(instance, "apply", plan["session_id"], plan_id=plan["plan_id"])


def test_preview_has_no_mutations_and_retains_resolved_ranges():
    instance, song = remote()
    result = preview(instance, (0.0, 4.0))
    assert result["status"] == "preview"
    assert [row["end_beat"] for row in result["placements"]] == [4, 8]
    assert result["expires_in_seconds"] == 300
    assert song.tracks[0].copy_calls == song.undo_started == 0


@pytest.mark.parametrize("bad", [None, [], [None], [{}], [dict(track_handle="x", source_slot=1,
    destination_beat=0)] * 65])
def test_invalid_plan_shapes_never_copy(bad):
    instance, song = remote()
    session, _ = request(instance)
    with pytest.raises(ValueError):
        builder.build_arrangement(instance, "preview", session, bad)
    assert song.tracks[0].copy_calls == 0


@pytest.mark.parametrize("field,value", [
    ("source_slot", 0), ("source_slot", True), ("source_slot", 2),
    ("source_slot", 1.2), ("destination_beat", -1), ("destination_beat", True),
    ("destination_beat", float("nan")), ("destination_beat", float("inf")),
    ("track_handle", "missing"), ("unexpected", "ignored"),
])
def test_invalid_later_placement_cannot_apply_anything(field, value):
    instance, song = remote()
    session, rows = request(instance, (0, 4))
    rows[1][field] = value
    with pytest.raises(ValueError):
        builder.build_arrangement(instance, "preview", session, rows)
    assert song.tracks[0].copy_calls == 0


@pytest.mark.parametrize("attribute,value,error", [
    ("has_envelopes", True, "envelopes"), ("has_envelopes", None, "envelopes"),
    ("is_midi_clip", False, "Only session MIDI"),
    ("get_all_notes_extended", None, "get_all_notes_extended"),
    ("length", 0, "source length"), ("length", float("nan"), "source length"),
])
def test_unsupported_sources_fail_closed(attribute, value, error):
    instance, song = remote()
    setattr(song.tracks[0].clip_slots[0].clip, attribute, value)
    with pytest.raises(ValueError, match=error):
        preview(instance)
    assert song.tracks[0].copy_calls == 0


@pytest.mark.parametrize("attribute", ["is_foldable", "is_frozen"])
def test_group_and_frozen_tracks_rejected(attribute):
    instance, song = remote()
    setattr(song.tracks[0], attribute, True)
    with pytest.raises(ValueError):
        preview(instance)


def test_return_tracks_rejected():
    instance, song = remote()
    song.return_tracks.append(Track("Return"))
    targets = instance._get_edit_targets()
    with pytest.raises(ValueError, match="regular track"):
        builder.build_arrangement(instance, "preview", targets["session_id"], [{
            "track_handle": targets["tracks"][1]["track_handle"], "source_slot": 1,
            "destination_beat": 0}])


def test_note_limit_bounds_whole_plan_and_each_source():
    instance, song = remote()
    source = song.tracks[0].clip_slots[0].clip
    source.notes *= 6000
    with pytest.raises(ValueError, match="Plan exceeds"):
        preview(instance, (0, 4))
    source.notes *= 2
    with pytest.raises(ValueError, match="Source exceeds"):
        preview(instance)


@pytest.mark.parametrize("start", [0.0, 1.0, 3.9999])
def test_existing_overlap_rejected(start):
    instance, song = remote()
    song.tracks[0].arrangement_clips = [Clip("Keep", start=start)]
    with pytest.raises(ValueError, match="overlaps existing"):
        preview(instance)
    assert song.tracks[0].copy_calls == 0


def test_proposed_overlap_rejected_but_adjacent_ranges_work():
    instance, song = remote()
    with pytest.raises(ValueError, match="proposed placement"):
        preview(instance, (0, 2))
    song.tracks[0].arrangement_clips.append(Clip("Keep", start=8))
    plan = preview(instance, (0, 4))
    result = apply(instance, plan)
    assert result["status"] == "applied"
    assert len(song.tracks[0].arrangement_clips) == 3


def test_track_insertion_and_rename_follow_original_object():
    instance, song = remote()
    plan = preview(instance)
    original = song.tracks[0]
    song.tracks.insert(0, Track("New"))
    original.name = "Renamed Piano"
    assert apply(instance, plan)["status"] == "applied"
    assert original.copy_calls == 1
    assert song.tracks[0].copy_calls == 0


def test_fresh_live_proxy_wrappers_preserve_plan_copy_and_rollback_identity():
    instance, song = remote()

    class Proxy:
        def __init__(self, underlying):
            self.underlying = underlying

        def __eq__(self, other):
            return self.underlying is getattr(other, "underlying", other)

        def __getattr__(self, name):
            value = getattr(self.underlying, name)
            if callable(value):
                def call(*args, **kwargs):
                    return wrap(value(*(getattr(arg, "underlying", arg) for arg in args), **kwargs))
                return call
            return wrap(value)

    def wrap(value):
        if isinstance(value, list):
            return [wrap(item) for item in value]
        if isinstance(value, (Song, Track, Clip, types.SimpleNamespace)):
            return Proxy(value)
        return value

    instance.song = lambda: Proxy(song)
    assert instance._song is not instance._song
    keep = Clip("Keep", start=20)
    track = song.tracks[0]
    track.arrangement_clips.append(keep)
    plan = preview(instance, (0, 4))
    track.copy_failure = (2, "after")
    result = apply(instance, plan)
    assert result["status"] == "error"
    assert result["rollback_verified"] is True
    assert track.arrangement_clips == [keep]
    track.copy_failure = None
    plan = preview(instance, (0, 4))
    assert apply(instance, plan)["status"] == "applied"
    assert track.arrangement_clips[0] is keep


@pytest.mark.parametrize("change", ["track_deleted", "clip_replaced", "slot_empty", "notes", "markers", "name", "envelopes", "frozen"])
def test_stale_sources_rejected_before_copy(change):
    instance, song = remote()
    track = song.tracks[0]
    plan = preview(instance)
    if change == "track_deleted": song.tracks = []
    elif change == "clip_replaced": track.clip_slots[0].clip = Clip()
    elif change == "slot_empty": track.clip_slots[0].has_clip = False
    elif change == "notes": track.clip_slots[0].clip.notes[0].velocity += 0.000000001
    elif change == "markers": track.clip_slots[0].clip.loop_start = 1
    elif change == "name": track.clip_slots[0].clip.name = "Changed"
    elif change == "envelopes": track.clip_slots[0].clip.has_envelopes = True
    elif change == "frozen": track.is_frozen = True
    with pytest.raises(ValueError):
        apply(instance, plan)
    assert track.copy_calls == 0


def test_changed_set_and_expired_plan_rejected(monkeypatch):
    instance, song = remote()
    plan = preview(instance)
    original_clock = builder.time.monotonic
    monkeypatch.setattr(builder.time, "monotonic", lambda: original_clock() + 301)
    with pytest.raises(ValueError, match="expired"):
        apply(instance, plan)
    instance.song = lambda: Song()
    with pytest.raises(ValueError, match="Set"):
        apply(instance, plan)
    assert song.tracks[0].copy_calls == 0


def test_new_destination_conflict_and_recording_rejected_before_any_copy():
    instance, song = remote()
    plan = preview(instance, (0, 4))
    song.tracks[0].arrangement_clips.append(Clip("New destination", start=4))
    with pytest.raises(ValueError, match="overlaps existing"):
        apply(instance, plan)
    song.tracks[0].arrangement_clips.clear()
    instance._auto_rec["active"] = True
    with pytest.raises(ValueError, match="recording job"):
        apply(instance, plan)
    assert song.tracks[0].copy_calls == 0


def test_success_preserves_source_and_completed_plan_replays_without_copying():
    instance, song = remote()
    source = song.tracks[0].clip_slots[0].clip
    original = copy.deepcopy(vars(source))
    plan = preview(instance, (0, 4))
    result = apply(instance, plan)
    assert result["status"] == "applied" and result["saved"] is False
    assert all(row["status"] == "verified" for row in result["placements"])
    assert vars(source) == original
    assert apply(instance, plan)["replayed"] is True
    assert song.tracks[0].copy_calls == 2
    assert song.undo_started == song.undo_finished == 1


@pytest.mark.parametrize("failure", ["before", "after", "wrong_bounds", "wrong_notes", "no_output"])
def test_second_copy_failure_rolls_back_only_operation_owned_clips(failure):
    instance, song = remote()
    track = song.tracks[0]
    keep = Clip("Existing", start=20)
    track.arrangement_clips.append(keep)
    plan = preview(instance, (0, 4))
    track.copy_failure = (2, failure)
    result = apply(instance, plan)
    assert result["status"] == "error" and result["rollback_verified"]
    assert track.arrangement_clips == [keep]
    assert keep not in track.delete_calls
    assert song.undo_started == song.undo_finished == 1
    assert apply(instance, plan)["replayed"] is True
    assert track.copy_calls == 2


def test_rollback_failure_is_explicit_partial():
    instance, song = remote()
    plan = preview(instance, (0, 4))
    track = song.tracks[0]
    track.copy_failure, track.delete_failure = (2, "after"), True
    result = apply(instance, plan)
    assert result["status"] == "partial"
    assert result["rollback_verified"] is False
    assert len(result["rollback_errors"]) >= 2
    assert len(track.arrangement_clips) == 2


def test_post_copy_enumeration_failure_cannot_claim_verified_rollback(monkeypatch):
    instance, song = remote()
    plan = preview(instance)
    track = song.tracks[0]
    original = builder._arrangement
    failed = []
    def arrangement(target):
        if track.copy_calls and not failed:
            failed.append(True)
            raise RuntimeError("post-copy enumeration unavailable")
        return original(target)
    monkeypatch.setattr(builder, "_arrangement", arrangement)
    result = apply(instance, plan)
    assert result["status"] == "partial"
    assert not result["rollback_verified"]
    assert "New arrangement material remains" in " ".join(result["rollback_errors"])
    assert len(track.arrangement_clips) == 1


def test_nonfinite_copy_bounds_fail_readback_and_are_removed():
    instance, song = remote()
    track = song.tracks[0]
    original = track.duplicate_clip_to_arrangement
    def copy_invalid(clip, start):
        original(clip, start)
        track.arrangement_clips[-1].end_time = float("nan")
    track.duplicate_clip_to_arrangement = copy_invalid
    result = apply(instance, preview(instance))
    assert result["status"] == "error" and result["rollback_verified"]
    assert track.arrangement_clips == []


def test_overall_arrangement_limit_is_checked_before_copy(monkeypatch):
    instance, song = remote()
    monkeypatch.setattr(builder, "MAX_EXISTING_CLIPS", 2)
    song.tracks[0].arrangement_clips.append(Clip("Keep", start=20))
    with pytest.raises(ValueError, match="Plan would exceed"):
        preview(instance, (0, 4))
    assert song.tracks[0].copy_calls == 0


def test_plan_capacity_is_bounded_and_unknown_is_not_claimed_unapplied():
    instance, _song = remote()
    old = preview(instance)
    for _index in range(builder.MAX_PLANS):
        preview(instance)
    assert len(instance._arrangement_plans) == builder.MAX_PLANS
    with pytest.raises(ValueError, match="Unknown does not prove unapplied"):
        apply(instance, old)


def test_apply_cannot_change_preview_placements():
    instance, _song = remote()
    plan = preview(instance)
    session, rows = request(instance)
    with pytest.raises(ValueError, match="not replacement placements"):
        builder.build_arrangement(instance, "apply", session, rows, plan["plan_id"])


def test_dispatch_schedules_preview_and_apply_and_wire_results_match():
    instance, song = remote()
    ticks = []
    def schedule(delay, callback):
        ticks.append(delay)
        callback()
    instance.schedule_message = schedule
    session, placements = request(instance)
    preview_response = instance._process_command({"id": "preview", "type": "build_arrangement",
        "params": {"action": "preview", "session_id": session, "placements": placements}})
    assert preview_response["status"] == "success"
    plan_id = preview_response["result"]["plan_id"]
    applied = instance._process_command({"id": "apply", "type": "build_arrangement",
        "params": {"action": "apply", "session_id": session, "plan_id": plan_id}})
    assert applied["result"]["status"] == "applied"
    assert ticks == [1, 1]
    assert song.tracks[0].copy_calls == 1


def test_actual_mcp_discovery_and_partial_error_schema():
    source = '''
        import asyncio
        import mcp.types as types
        from MCP_Server import server
        class Connection:
            def connect(self):
                return True
            def send_command(self, command, params):
                assert command == "build_arrangement"
                return {"status":"partial", "plan_id":"plan", "session_id":"set", "placements":[],
                        "placement_count":0, "rollback_verified":False, "rollback_errors":["failed"]}
        server._ableton_connection = Connection()
        async def main():
            tools = {tool.name: tool for tool in await server.mcp.list_tools()}
            tool = tools["build_arrangement"]
            assert tool.annotations.readOnlyHint is False
            assert "plan_id" in tool.outputSchema["properties"]
            req = types.CallToolRequest(params=types.CallToolRequestParams(name="build_arrangement",
                arguments={"action":"apply", "session_id":"set", "plan_id":"plan"}))
            result = (await server.mcp._mcp_server.request_handlers[types.CallToolRequest](req)).root
            assert result.isError is True
            assert result.structuredContent["rollback_errors"] == ["failed"]
        asyncio.run(main())
    '''
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    result = subprocess.run([sys.executable, "-c", textwrap.dedent(source)], cwd=root,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
