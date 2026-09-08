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


class NativeVector(list):
    """Represent the Live-owned vector required for note modification."""


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
        self.note_remove_failure = self.note_modify_failure = None
        self.note_remove_calls = self.note_modify_calls = 0

    def get_all_notes_extended(self):
        vector = NativeVector(copy.deepcopy(self.notes))
        vector.owner = self
        return vector

    def remove_notes_by_id(self, ids):
        self.note_remove_calls += 1
        if self.note_remove_failure == "before":
            raise RuntimeError("note removal failed before mutation")
        if self.note_remove_failure != "silent":
            selected = ids[:1] if self.note_remove_failure == "partial" else ids
            self.notes = [note for note in self.notes if note.note_id not in selected]
        if self.note_remove_failure == "partial":
            raise RuntimeError("note removal failed after partial mutation")

    def apply_note_modifications(self, vector):
        assert isinstance(vector, NativeVector) and vector.owner is self
        assert {note.note_id for note in vector} == {note.note_id for note in self.notes}
        self.note_modify_calls += 1
        if self.note_modify_failure == "before":
            raise RuntimeError("note modification failed before mutation")
        if self.note_modify_failure != "silent":
            selected = vector[:1] if self.note_modify_failure == "partial" else vector
            updates = {note.note_id: note for note in selected}
            self.notes = [copy.deepcopy(updates.get(note.note_id, note)) for note in self.notes]
        if self.note_modify_failure == "partial":
            raise RuntimeError("note modification failed after partial mutation")


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
        for note in duplicate.notes:
            note.note_id += 100
        self.arrangement_clips.append(duplicate)
        if self.copy_failure == (self.copy_calls, "after"):
            raise RuntimeError("copy failed after insertion")
        if self.copy_failure == (self.copy_calls, "wrong_bounds"):
            duplicate.end_time += 1
        if self.copy_failure == (self.copy_calls, "wrong_notes"):
            duplicate.notes[0].pitch += 1
        for phase in ("remove", "modify"):
            for failure in ("before", "partial", "silent"):
                if self.copy_failure == (self.copy_calls, "note_" + phase + "_" + failure):
                    setattr(duplicate, "note_" + phase + "_failure", failure)

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
        self.record_mode = self.session_record = False

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
    instance._scheduled_callbacks = []
    instance.schedule_message = lambda delay, callback: instance._scheduled_callbacks.append((delay, callback))
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


def settle_audio_bounds(instance):
    """Model Live making arrangement edge updates observable on its next tick."""
    for track in instance._song.tracks:
        for clip in track.arrangement_clips:
            if hasattr(clip, "_pending_end_time"):
                clip.end_time = clip._pending_end_time
                del clip._pending_end_time


def drain_callbacks(instance):
    settle_audio_bounds(instance)
    while instance._scheduled_callbacks:
        delay, callback = instance._scheduled_callbacks.pop(0)
        assert delay == 1
        callback()


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


@pytest.mark.parametrize("action", ["preview", "apply"])
@pytest.mark.parametrize("flag", ["record_mode", "session_record", "auto_recording_job"])
def test_recording_guard_rejects_preview_and_apply_without_stopping_transport(action, flag):
    instance, song = remote()
    plan = preview(instance) if action == "apply" else None
    stop_calls = []
    song.is_playing = True
    song.stop_playing = lambda: stop_calls.append("stopped")
    if flag == "auto_recording_job":
        instance._auto_rec["active"] = True
    else:
        setattr(song, flag, True)
    with pytest.raises(ValueError, match="recording"):
        apply(instance, plan) if plan else preview(instance)
    assert song.tracks[0].copy_calls == song.undo_started == 0
    assert song.is_playing is True and stop_calls == []
    assert instance._auto_rec["active"] if flag == "auto_recording_job" else getattr(song, flag)


@pytest.mark.parametrize("action", ["preview", "apply"])
@pytest.mark.parametrize("flag", ["record_mode", "session_record"])
def test_unreadable_recording_flag_fails_closed_without_copy_or_transport_changes(monkeypatch, action, flag):
    instance, song = remote()
    plan = preview(instance) if action == "apply" else None
    stop_calls = []
    song.is_playing = True
    song.stop_playing = lambda: stop_calls.append("stopped")

    def unavailable(_song):
        raise RuntimeError("recording flag unavailable")

    monkeypatch.setattr(Song, flag, property(unavailable), raising=False)
    with pytest.raises(ValueError, match="recording"):
        apply(instance, plan) if plan else preview(instance)
    assert song.tracks[0].copy_calls == song.undo_started == 0
    assert song.is_playing is True and stop_calls == []
    assert song.__dict__[flag] is False


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


def variation_source(song):
    source = song.tracks[0].clip_slots[0].clip
    source.notes = [types.SimpleNamespace(pitch=pitch, start_time=start, duration=0.25,
        velocity=velocity, mute=False, note_id=index + 1, probability=0.8125,
        velocity_deviation=-3.125, release_velocity=61.875)
        for index, (pitch, start, velocity) in enumerate([
            (64, 2.0, 88.125), (36, 0.0, 95.0), (60, 0.0, 80.75),
            (36, 1.0, 96.0), (62, 1.0, 84.25), (67, 3.0, 91.5)])]
    return source


def test_variation_preview_reports_exact_result_and_apply_names_only_the_copy():
    instance, song = remote()
    source = variation_source(song)
    original = copy.deepcopy(vars(source))
    session, placements = request(instance, (0, 4))
    placements[0].update(name="Breakdown", variation={"remove_pitches": [36],
                          "keep_every": 2, "keep_offset": 1, "transpose": 7})
    placements[1].update(name="Original phrase")
    plan = builder.build_arrangement(instance, "preview", session, placements)
    variation = plan["placements"][0]
    expected = [dict(vars(source.notes[index]), pitch=source.notes[index].pitch + 7)
                for index in (4, 5)]
    for note in expected:
        note.pop("note_id")
    assert variation["notes"] == expected
    assert variation["source_note_count"] == 6 and variation["note_count"] == 2
    assert plan["source_note_count"] == 12 and plan["note_count"] == 8
    assert variation["name"] == "Breakdown" and variation["kind"] == "midi"
    assert vars(source) == original
    assert song.tracks[0].copy_calls == song.undo_started == 0

    # Mutating the returned preview cannot alter the retained plan.
    variation["notes"][0]["pitch"] = 1
    result = apply(instance, plan)
    assert result["status"] == "applied"
    copied, unchanged = song.tracks[0].arrangement_clips
    assert copied.name == "Breakdown" and unchanged.name == "Original phrase"
    actual = instance._serialise_notes(copied.get_all_notes_extended(), rounded=False)
    for note in actual:
        note.pop("note_id")
    assert sorted(actual, key=lambda note: note["start_time"]) == expected
    assert builder._notes(instance, unchanged) == builder._notes(instance, source)
    assert vars(source) == original
    assert copied.note_remove_calls == copied.note_modify_calls == 1
    assert unchanged.note_remove_calls == unchanged.note_modify_calls == 0
    assert apply(instance, plan)["replayed"] is True
    assert song.tracks[0].copy_calls == 2
    assert song.undo_started == song.undo_finished == 1


@pytest.mark.parametrize("variation", [
    {"keep_every": 0}, {"keep_every": 2, "keep_offset": 2},
    {"transpose": True}, {"transpose": 100}, {"remove_pitches": [128]},
    {"randomize": True}, "remove kick",
])
def test_invalid_later_variation_rejects_whole_plan_without_edits(variation):
    instance, song = remote()
    source = variation_source(song)
    original = copy.deepcopy(vars(source))
    session, placements = request(instance, (0, 4))
    placements[0]["variation"] = {"transpose": 7}
    placements[1]["variation"] = variation
    with pytest.raises(ValueError):
        builder.build_arrangement(instance, "preview", session, placements)
    assert vars(source) == original
    assert song.tracks[0].copy_calls == song.undo_started == 0


@pytest.mark.parametrize("name", [None, 42, True, "", "   ", "x" * 1025])
def test_invalid_later_clip_name_is_preflighted(name):
    instance, song = remote()
    session, placements = request(instance, (0, 4))
    placements[1]["name"] = name
    with pytest.raises(ValueError, match="name must"):
        builder.build_arrangement(instance, "preview", session, placements)
    assert song.tracks[0].copy_calls == 0


@pytest.mark.parametrize("phase", ["remove", "modify"])
@pytest.mark.parametrize("failure", ["before", "partial", "silent"])
def test_later_native_variation_failure_removes_all_owned_copies_only(phase, failure):
    instance, song = remote()
    source = variation_source(song)
    original = copy.deepcopy(vars(source))
    track = song.tracks[0]
    keep = Clip("Unrelated arrangement", start=20)
    track.arrangement_clips.append(keep)
    session, placements = request(instance, (0, 4))
    for placement in placements:
        placement["variation"] = {"remove_pitches": [36], "transpose": 12}
    plan = builder.build_arrangement(instance, "preview", session, placements)
    track.copy_failure = (2, "note_" + phase + "_" + failure)
    result = apply(instance, plan)
    assert result["status"] == "error" and result["rollback_verified"]
    assert result["completed_before_failure"] == 1
    assert track.arrangement_clips == [keep]
    assert keep not in track.delete_calls
    assert len(track.delete_calls) == 2
    assert vars(source) == original
    assert song.undo_started == song.undo_finished == 1
    assert apply(instance, plan)["replayed"] is True
    assert track.copy_calls == 2


def test_midi_variation_can_intentionally_place_an_empty_breakdown():
    instance, song = remote()
    session, placements = request(instance)
    placements[0].update(name="Silent breakdown", variation={"remove_pitches": [60]})
    result = apply(instance, builder.build_arrangement(instance, "preview", session, placements))
    assert result["status"] == "applied"
    assert song.tracks[0].arrangement_clips[0].notes == []
    assert len(song.tracks[0].clip_slots[0].clip.notes) == 1


def chord_source(song):
    source = song.tracks[0].clip_slots[0].clip = Clip("Four chords", length=8)
    source.notes = [types.SimpleNamespace(
        pitch=pitch, start_time=start, duration=1.0, velocity=90.125 + index,
        mute=False, note_id=index + 1, probability=0.8125,
        velocity_deviation=-3.125, release_velocity=61.875)
        for index, (start, pitch) in enumerate([
            (0.0, 60), (0.0, 64), (0.0, 67),
            (2.0, 62), (2.0, 65), (2.0, 69),
            (4.0, 64), (4.0, 67), (4.0, 71),
            (6.0, 65), (6.0, 69), (6.0, 72)])]
    source.notes.reverse()  # Live enumeration must not define group order.
    return source


def musical_rows(notes):
    rows = [dict(vars(note)) for note in notes]
    for row in rows:
        row.pop("note_id")
    return sorted(rows, key=lambda row: (row["start_time"], row["pitch"]))


def test_onset_variations_keep_whole_chords_and_preserve_retained_plan_and_source():
    instance, song = remote()
    source = chord_source(song)
    original = copy.deepcopy(vars(source))
    source_rows = musical_rows(source.notes)
    session, placements = request(instance, (32.5, 40.5, 48.5))
    variations = [
        {"thin_by": "onset", "keep_every": 2},
        {"thin_by": "onset", "keep_every": 2, "keep_offset": 1, "transpose": 12},
        {"keep_every": 2},
    ]
    expected = [
        [row for row in source_rows if row["start_time"] in (0, 4)],
        [dict(row, pitch=row["pitch"] + 12) for row in source_rows if row["start_time"] in (2, 6)],
        source_rows[::2],
    ]
    for index, (placement, variation) in enumerate(zip(placements, variations)):
        placement.update(name="Chord copy " + str(index + 1), variation=variation)
    plan = builder.build_arrangement(instance, "preview", session, placements)
    assert plan["note_count"] == 18 and plan["source_note_count"] == 36
    for index, row in enumerate(plan["placements"]):
        assert row["notes"] == expected[index]
        assert row["note_count"] == 6 and row["source_note_count"] == 12
        assert row["end_beat"] == row["destination_beat"] + 8
    assert [row["variation"]["thin_by"] for row in plan["placements"]] == ["onset", "onset", "note"]
    assert vars(source) == original and song.tracks[0].copy_calls == song.undo_started == 0

    # Client-side edits must not alter the retained transform or preview notes.
    plan["placements"][0]["variation"]["thin_by"] = "note"
    plan["placements"][0]["notes"][0]["pitch"] = 1
    placements[1]["variation"]["transpose"] = -12
    result = apply(instance, plan)
    assert result["status"] == "applied"
    for index, (row, clip) in enumerate(zip(result["placements"], song.tracks[0].arrangement_clips)):
        assert row["status"] == "verified" and row["notes"] == expected[index]
        assert musical_rows(clip.notes) == expected[index]
        assert (clip.start_time, clip.end_time) == (32.5 + index * 8, 40.5 + index * 8)
        assert clip.name == "Chord copy " + str(index + 1)
    replay = apply(instance, plan)
    assert replay["status"] == "applied" and replay["replayed"] is True
    assert vars(source) == original
    assert song.tracks[0].copy_calls == 3 and song.undo_started == song.undo_finished == 1


def test_onset_filtering_removes_empty_groups_before_counting_and_transposing():
    instance, song = remote()
    source = chord_source(song)
    source.notes = [types.SimpleNamespace(**dict(vars(source.notes[0]),
                    note_id=index + 1, start_time=start, pitch=pitch))
                    for index, (start, pitch) in enumerate([
                        (0.0, 36), (1.0, 60), (1.0, 64),
                        (2.0, 62), (2.0, 65), (3.0, 67), (3.0, 71)])]
    original = copy.deepcopy(vars(source))
    session, placements = request(instance)
    placements[0]["variation"] = {"thin_by": "onset", "remove_pitches": [36],
                                   "keep_every": 2, "keep_offset": 1, "transpose": 7}
    plan = builder.build_arrangement(instance, "preview", session, placements)
    expected = [dict(row, pitch=row["pitch"] + 7) for row in musical_rows(source.notes)
                if row["start_time"] == 2]
    assert plan["placements"][0]["notes"] == expected
    assert apply(instance, plan)["status"] == "applied"
    assert musical_rows(song.tracks[0].arrangement_clips[0].notes) == expected
    assert vars(source) == original


@pytest.mark.parametrize("thin_by", [None, True, 0, [], {}, "chord", "Onset", " onset"])
def test_invalid_later_thin_by_rejects_entire_plan_before_writes(thin_by):
    instance, song = remote()
    source = chord_source(song)
    original = copy.deepcopy(vars(source))
    session, placements = request(instance, (0, 8))
    placements[0]["variation"] = {"thin_by": "onset", "keep_every": 2}
    placements[1]["variation"] = {"thin_by": thin_by}
    with pytest.raises(ValueError, match="thin_by"):
        builder.build_arrangement(instance, "preview", session, placements)
    assert vars(source) == original
    assert song.tracks[0].copy_calls == song.undo_started == 0


def test_onset_change_smaller_than_public_rounding_invalidates_retained_plan():
    instance, song = remote()
    source = chord_source(song)
    session, placements = request(instance)
    placements[0]["variation"] = {"thin_by": "onset", "keep_every": 2}
    plan = builder.build_arrangement(instance, "preview", session, placements)
    source.notes[0].start_time += 0.000000001
    changed = copy.deepcopy(vars(source))
    with pytest.raises(ValueError, match="Source clip changed"):
        apply(instance, plan)
    assert song.tracks[0].copy_calls == song.undo_started == 0
    assert vars(source) == changed


class AudioClip:
    """Simulate arrangement edge updates from marker edits without toggling warp."""

    def __init__(self, file_path, warped=True):
        self.name, self.file_path = "Recorded stem", str(file_path)
        self.is_midi_clip, self.is_audio_clip = False, True
        self.is_session_clip, self.is_arrangement_clip = True, False
        self.has_envelopes = self.has_groove = self.is_recording = False
        self.color, self.muted, self.gain = 32, False, 0.85
        self.warping, self.warp_mode, self.looping = warped, 0, warped
        self.pitch_coarse = self.pitch_fine = 0
        self.sample_rate, self.sample_length = 44100, 441000
        self.warp_markers = [types.SimpleNamespace(beat_time=0.0, sample_time=0.0),
                             types.SimpleNamespace(beat_time=20.0, sample_time=10.0)]
        self.length = 8.0
        self.start_marker, self.end_marker = 2.0, 10.0
        self._loop_start, self._loop_end = 2.0, 10.0
        self.start_time, self.end_time = 0.0, 8.0
        self.marker_failure = None

    def _edge(self):
        factor = 1 if self.warping else 2  # Fake Song's fixed 120 BPM.
        end = self.start_time + (self._loop_end - self._loop_start) * factor
        if getattr(self, "defer_bounds", False):
            self._pending_end_time = end
        else:
            self.end_time = end

    @property
    def loop_start(self):
        return self._loop_start

    @loop_start.setter
    def loop_start(self, value):
        self._loop_start = self.start_marker = value
        self._edge()
        if self.marker_failure == "after_loop_start":
            raise RuntimeError("audio trim failed after marker mutation")

    @property
    def loop_end(self):
        return self._loop_end

    @loop_end.setter
    def loop_end(self, value):
        self._loop_end = value
        self._edge()


class AudioTrack(Track):
    def __init__(self, file_path, warped=True):
        super().__init__("Recorded audio")
        self.clip_slots[0].clip = AudioClip(file_path, warped)
        self.defer_bounds = False

    def duplicate_clip_to_arrangement(self, clip, start):
        self.copy_calls += 1
        duplicate = copy.deepcopy(clip)
        duplicate.is_session_clip, duplicate.is_arrangement_clip = False, True
        duplicate.defer_bounds = self.defer_bounds
        duplicate.start_time, duplicate.end_time = start, start + clip.length
        self.arrangement_clips.append(duplicate)
        if self.copy_failure == (self.copy_calls, "trim"):
            duplicate.marker_failure = "after_loop_start"
        elif self.copy_failure == (self.copy_calls, "oversized"):
            duplicate.end_time = start + 100


def audio_remote(tmp_path, warped=True):
    instance, song = remote()
    audio_file = tmp_path / "stem.wav"
    audio_file.write_bytes(b"nonempty audio file identity fixture")
    song.tracks = [AudioTrack(audio_file, warped)]
    song.tempo = 120.0
    song.tempo_follower_enabled = song.is_ableton_link_enabled = False
    song.master_track = types.SimpleNamespace(mixer_device=types.SimpleNamespace(
        song_tempo=types.SimpleNamespace(automation_state=0)))
    return instance, song


def audio_plan(instance, starts=(0,), units="beats"):
    session, placements = request(instance, starts)
    for index, placement in enumerate(placements):
        placement.update(name="Stem section " + str(index + 1),
                         source_range={"start": 4.0, "end": 6.0, "units": units})
    return builder.build_arrangement(instance, "preview", session, placements)


@pytest.mark.parametrize("warped,units,length,reserved", [
    (True, "beats", 2.0, 10.0), (False, "seconds", 4.0, 20.0),
])
def test_audio_plan_places_fractional_start_trims_names_and_preserves_source(
        tmp_path, warped, units, length, reserved):
    instance, song = audio_remote(tmp_path, warped)
    source = song.tracks[0].clip_slots[0].clip
    original = copy.deepcopy(vars(source))
    plan = audio_plan(instance, (1.5,), units)
    row = plan["placements"][0]
    assert (row["destination_beat"], row["end_beat"], row["reserved_end_beat"]) == (
        1.5, 1.5 + length, 1.5 + reserved)
    assert row["kind"] == "audio" and row["note_count"] == 0
    assert song.tracks[0].copy_calls == 0
    result = apply(instance, plan)
    if not warped:
        assert result["status"] == "applying"
        drain_callbacks(instance)
        result = apply(instance, plan)
    assert result["status"] == "applied"
    placed = song.tracks[0].arrangement_clips[0]
    assert (placed.start_time, placed.end_time) == (1.5, 1.5 + length)
    assert (placed.loop_start, placed.loop_end, placed.start_marker, placed.end_marker) == (4, 6, 4, 6)
    assert placed.name == "Stem section 1" and placed.looping is False
    assert placed.warping is warped
    assert vars(source) == original
    assert apply(instance, plan)["replayed"] is True
    assert song.tracks[0].copy_calls == 1


@pytest.mark.parametrize("starts", [(0, 4), (4, 0)])
def test_audio_reserved_span_blocks_proposed_overlap_outside_final_trim(tmp_path, starts):
    instance, song = audio_remote(tmp_path)
    with pytest.raises(ValueError, match="proposed placement"):
        audio_plan(instance, starts)
    assert song.tracks[0].copy_calls == 0


def test_audio_reserved_span_blocks_current_clip_then_accepts_adjacent_footprint(tmp_path):
    instance, song = audio_remote(tmp_path)
    track = song.tracks[0]
    keep = Clip("Keep beyond trimmed section", start=4)
    track.arrangement_clips.append(keep)
    with pytest.raises(ValueError, match="existing arrangement clip"):
        audio_plan(instance)
    assert track.copy_calls == 0
    keep.start_time, keep.end_time = 20, 24
    result = apply(instance, audio_plan(instance, (0, 10)))
    assert result["status"] == "applied"
    assert track.arrangement_clips[0] is keep
    assert [(clip.start_time, clip.end_time) for clip in track.arrangement_clips[1:]] == [(0, 2), (10, 12)]


def test_audio_destination_conflict_inserted_after_preview_rejects_before_copy(tmp_path):
    instance, song = audio_remote(tmp_path)
    plan = audio_plan(instance)
    song.tracks[0].arrangement_clips.append(Clip("New material in reserved space", start=4))
    with pytest.raises(ValueError, match="existing arrangement clip"):
        apply(instance, plan)
    assert song.tracks[0].copy_calls == 0


@pytest.mark.parametrize("failure", ["trim", "oversized"])
def test_later_audio_trim_failure_rolls_back_all_owned_copies_only(tmp_path, failure):
    instance, song = audio_remote(tmp_path)
    track = song.tracks[0]
    source = track.clip_slots[0].clip
    original = copy.deepcopy(vars(source))
    keep = Clip("Unrelated audio", start=40)
    track.arrangement_clips.append(keep)
    plan = audio_plan(instance, (0, 10))
    track.copy_failure = (2, failure)
    result = apply(instance, plan)
    assert result["status"] == "error" and result["rollback_verified"]
    assert result["completed_before_failure"] == 1
    assert track.arrangement_clips == [keep]
    assert keep not in track.delete_calls and len(track.delete_calls) == 2
    assert vars(source) == original
    assert song.undo_started == song.undo_finished == 1


@pytest.mark.parametrize("change", ["file", "tempo", "source_markers"])
def test_unwarped_source_changes_invalidate_audio_preview_before_copy(tmp_path, change):
    instance, song = audio_remote(tmp_path, warped=False)
    plan = audio_plan(instance, units="seconds")
    source = song.tracks[0].clip_slots[0].clip
    if change == "file":
        with open(source.file_path, "ab") as handle:
            handle.write(b"changed")
    elif change == "tempo":
        song.tempo = 121.0
    else:
        source.loop_start = 3.0
    with pytest.raises(ValueError, match="Source clip changed"):
        apply(instance, plan)
    assert song.tracks[0].copy_calls == 0


@pytest.mark.parametrize("kind,field,value,error", [
    ("midi", "source_range", {"start": 0, "end": 1, "units": "beats"}, "only to audio"),
    ("audio", "variation", {"transpose": 12}, "only to MIDI"),
    ("audio", "source_range", {"start": 4, "end": 6, "units": "seconds"}, "units must"),
])
def test_later_incompatible_options_reject_before_any_copy(tmp_path, kind, field, value, error):
    instance, song = audio_remote(tmp_path) if kind == "audio" else remote()
    session, placements = request(instance, (0, 20))
    placements[1][field] = value
    with pytest.raises(ValueError, match=error):
        builder.build_arrangement(instance, "preview", session, placements)
    assert song.tracks[0].copy_calls == song.undo_started == 0


def test_copy_that_secretly_trims_existing_clip_cannot_claim_verified_rollback():
    instance, song = remote()
    track = song.tracks[0]
    keep = Clip("Keep entire original", start=20)
    track.arrangement_clips.append(keep)
    plan = preview(instance)
    original_copy = track.duplicate_clip_to_arrangement

    def copy_with_unexpected_side_effect(source, start):
        original_copy(source, start)
        keep.end_time = 23.0  # The object survives, but one beat of its region is lost.

    track.duplicate_clip_to_arrangement = copy_with_unexpected_side_effect
    result = apply(instance, plan)
    assert result["status"] == "partial"
    assert result["rollback_verified"] is False
    assert "bounds changed" in result["message"]
    assert any("bounds changed" in message for message in result["rollback_errors"])
    assert track.arrangement_clips == [keep]
    assert keep.end_time == 23.0
    assert keep not in track.delete_calls and len(track.delete_calls) == 1


def pending_audio(instance, starts=(0,)):
    track = instance._song.tracks[0]
    track.defer_bounds = True
    plan = audio_plan(instance, starts, units="seconds")
    result = apply(instance, plan)
    assert result["status"] == "applying"
    assert len(instance._scheduled_callbacks) == 1
    return plan


def test_unwarped_deferred_edges_verify_after_undo_closes_and_replay_never_copies(tmp_path):
    instance, song = audio_remote(tmp_path, warped=False)
    source = song.tracks[0].clip_slots[0].clip
    original = copy.deepcopy(vars(source))
    plan = pending_audio(instance)
    track = song.tracks[0]
    copied = track.arrangement_clips[0]
    assert copied.end_time == 8  # Stale while Live has not settled the trim.
    assert song.undo_started == song.undo_finished == 1
    assert track.delete_calls == []
    for _ in range(3):
        status = apply(instance, plan)
        assert status["status"] == "applying" and status["replayed"] is True
    assert track.copy_calls == 1 and len(instance._scheduled_callbacks) == 1
    drain_callbacks(instance)
    result = apply(instance, plan)
    assert result["status"] == "applied" and result["replayed"] is True
    assert (copied.start_time, copied.end_time) == (0, 4)
    assert vars(source) == original
    assert apply(instance, plan)["status"] == "applied"
    assert track.copy_calls == 1


def test_unwarped_final_bounds_failure_rolls_back_all_owned_copies(tmp_path):
    instance, song = audio_remote(tmp_path, warped=False)
    track = song.tracks[0]
    keep = Clip("Unrelated region", start=80)
    track.arrangement_clips.append(keep)
    plan = pending_audio(instance, (0, 20))
    settle_audio_bounds(instance)
    track.arrangement_clips[-1].end_time += 1
    drain_callbacks(instance)
    result = apply(instance, plan)
    assert result["status"] == "error" and result["rollback_verified"] is True
    assert "bounds" in result["message"]
    assert track.arrangement_clips == [keep]
    assert keep not in track.delete_calls and len(track.delete_calls) == 2
    assert song.undo_started == song.undo_finished == 2


@pytest.mark.parametrize("changed", ["source", "tempo", "copy_gain", "copy_name"])
def test_unwarped_finalizer_rechecks_source_and_copied_audio_values(tmp_path, changed):
    instance, song = audio_remote(tmp_path, warped=False)
    source = song.tracks[0].clip_slots[0].clip
    plan = pending_audio(instance)
    track = song.tracks[0]
    if changed == "source":
        source.gain = 0.75
    elif changed == "tempo":
        song.tempo = 121
    elif changed == "copy_gain":
        track.arrangement_clips[0].gain = 0.25
    else:
        track.arrangement_clips[0].name = "Changed during pending finalization"
    drain_callbacks(instance)
    result = apply(instance, plan)
    assert result["status"] == "error" and result["rollback_verified"] is True
    assert track.arrangement_clips == []
    assert source.gain == (0.75 if changed == "source" else 0.85)
    assert song.tempo == (121 if changed == "tempo" else 120)


def test_unwarped_finalizer_preserves_new_set_and_reports_unverifiable_old_copy(tmp_path):
    instance, old_song = audio_remote(tmp_path, warped=False)
    plan = pending_audio(instance)
    old_copy = old_song.tracks[0].arrangement_clips[0]
    new_song = Song()
    new_keep = Clip("New Set original region", start=0)
    new_song.tracks[0].arrangement_clips.append(new_keep)
    instance.song = lambda: new_song
    drain_callbacks(instance)
    terminal = instance._arrangement_plans[plan["plan_id"]]["result"]
    assert terminal["status"] == "partial" and terminal["rollback_verified"] is False
    assert old_song.tracks[0].arrangement_clips == [old_copy]
    assert new_song.tracks[0].arrangement_clips == [new_keep]
    assert new_song.tracks[0].delete_calls == old_song.tracks[0].delete_calls == []
    with pytest.raises(ValueError, match="Set"):
        apply(instance, plan)


def test_unwarped_finalizer_detects_deleted_owned_copy_and_removes_remaining_owned_only(tmp_path):
    instance, song = audio_remote(tmp_path, warped=False)
    track = song.tracks[0]
    plan = pending_audio(instance, (0, 20))
    track.arrangement_clips.pop(0)
    drain_callbacks(instance)
    result = apply(instance, plan)
    assert result["status"] == "error" and result["rollback_verified"] is True
    assert track.arrangement_clips == []
    assert len(track.delete_calls) == 1


@pytest.mark.parametrize("flag", ["record_mode", "session_record"])
def test_unwarped_finalizer_rejects_recording_that_starts_while_pending_without_stopping_it(tmp_path, flag):
    instance, song = audio_remote(tmp_path, warped=False)
    plan = pending_audio(instance)
    stop_calls = []
    song.stop_playing = lambda: stop_calls.append("stopped")
    song.is_playing = True
    setattr(song, flag, True)
    drain_callbacks(instance)
    result = apply(instance, plan)
    assert result["status"] == "error" and result["rollback_verified"] is True
    assert "recording" in result["message"]
    assert song.tracks[0].arrangement_clips == []
    assert song.is_playing is True and getattr(song, flag) is True and stop_calls == []


@pytest.mark.parametrize("action", ["preview", "apply"])
def test_pending_audio_blocks_other_plans_until_finalized(tmp_path, action):
    instance, song = audio_remote(tmp_path, warped=False)
    other = audio_plan(instance, (40,), units="seconds")
    pending = pending_audio(instance)
    with pytest.raises(ValueError, match="pending|applying|progress|settling|Finish"):
        apply(instance, other) if action == "apply" else audio_plan(instance, (40,), units="seconds")
    assert song.tracks[0].copy_calls == 1
    drain_callbacks(instance)
    assert apply(instance, pending)["status"] == "applied"
    assert apply(instance, other)["status"] == "applying"
    drain_callbacks(instance)
    assert apply(instance, other)["status"] == "applied"


def test_terminal_plan_replay_remains_available_while_another_audio_plan_is_pending(tmp_path):
    instance, song = audio_remote(tmp_path, warped=False)
    complete = pending_audio(instance)
    drain_callbacks(instance)
    assert apply(instance, complete)["status"] == "applied"
    pending = pending_audio(instance, (20,))
    replay = apply(instance, complete)
    assert replay["status"] == "applied" and replay["replayed"] is True
    assert song.tracks[0].copy_calls == 2 and len(instance._scheduled_callbacks) == 1
    drain_callbacks(instance)
    assert apply(instance, pending)["status"] == "applied"


def test_unwarped_schedule_failure_rolls_back_and_releases_pending_guard(tmp_path):
    instance, song = audio_remote(tmp_path, warped=False)
    plan = audio_plan(instance, units="seconds")
    original_schedule = instance.schedule_message

    def failed_schedule(_delay, _callback):
        raise RuntimeError("cannot schedule Live finalizer")

    instance.schedule_message = failed_schedule
    result = apply(instance, plan)
    assert result["status"] == "error" and result["rollback_verified"] is True
    assert "schedule" in result["message"]
    assert song.tracks[0].arrangement_clips == []
    assert apply(instance, plan)["replayed"] is True
    instance.schedule_message = original_schedule
    next_plan = pending_audio(instance)
    drain_callbacks(instance)
    assert apply(instance, next_plan)["status"] == "applied"


def test_pending_plan_is_not_expired_or_evicted_while_waiting_for_live_tick(tmp_path, monkeypatch):
    instance, song = audio_remote(tmp_path, warped=False)
    oldest = audio_plan(instance, units="seconds")
    for _ in range(builder.MAX_PLANS - 1):
        audio_plan(instance, (40,), units="seconds")
    song.tracks[0].defer_bounds = True
    assert apply(instance, oldest)["status"] == "applying"
    original_clock = builder.time.monotonic
    monkeypatch.setattr(builder.time, "monotonic", lambda: original_clock() + 301)
    with pytest.raises(ValueError, match="pending|applying|progress|settling|Finish"):
        audio_plan(instance, (60,), units="seconds")
    assert oldest["plan_id"] in instance._arrangement_plans
    assert apply(instance, oldest)["status"] == "applying"
    assert song.tracks[0].copy_calls == 1
    drain_callbacks(instance)
    assert apply(instance, oldest)["status"] == "applied"


def test_deferred_mixed_plan_rechecks_midi_copy_before_claiming_success(tmp_path):
    instance, song = audio_remote(tmp_path, warped=False)
    audio_track = song.tracks[0]
    midi_track = Track("MIDI alongside audio")
    song.tracks.append(midi_track)
    audio_track.defer_bounds = True
    targets = instance._get_edit_targets()
    handles = {row["name"]: row["track_handle"] for row in targets["tracks"]}
    placements = [
        {"track_handle": handles[midi_track.name], "source_slot": 1, "destination_beat": 0,
         "variation": {"transpose": 7}},
        {"track_handle": handles[audio_track.name], "source_slot": 1, "destination_beat": 0,
         "source_range": {"start": 4.0, "end": 6.0, "units": "seconds"}},
    ]
    plan = builder.build_arrangement(instance, "preview", targets["session_id"], placements)
    assert apply(instance, plan)["status"] == "applying"
    midi_track.arrangement_clips[0].notes[0].velocity += 1
    drain_callbacks(instance)
    result = apply(instance, plan)
    assert result["status"] == "error" and result["rollback_verified"] is True
    assert "MIDI" in result["message"]
    assert midi_track.arrangement_clips == audio_track.arrangement_clips == []
    assert midi_track.clip_slots[0].clip.notes[0].pitch == 60
    assert midi_track.clip_slots[0].clip.notes[0].velocity == 90
