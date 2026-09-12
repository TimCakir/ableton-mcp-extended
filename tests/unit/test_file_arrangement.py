"""File plans stage only owned audio and reconcile every deferred failure."""

import copy
import os
import types
import wave

import pytest

from .test_arrangement_builder import AudioClip, AudioTrack, Clip, Track, apply, remote, settle_audio_bounds
from AbletonMCP_Remote_Script import arrangement_builder as builder
from AbletonMCP_Remote_Script import audio_file_source
from AbletonMCP_Remote_Script import file_arrangement as files


def wav(path, seconds=12):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(100)
        stream.writeframes(b"\0" * (seconds * 100 * 4))
    return path


class ImportedClip(AudioClip):
    def __init__(self, path, length=8):
        super().__init__(path, warped=True)
        metadata = audio_file_source.probe_file(str(path))
        self.sample_rate, self.sample_length = metadata["sample_rate"], metadata["sample_frames"]
        self.length = length
        self.gain, self.muted = 0.25, True
        self.pitch_coarse, self.pitch_fine = 7, 3
        self.defer_warp, self.ignore_warp = True, False
        self.marker_failure = None

    def __setattr__(self, name, value):
        if name == "warping" and getattr(self, "defer_warp", False):
            if not self.ignore_warp:
                self._pending_warp = value
        else:
            super().__setattr__(name, value)


class Slot:
    def __init__(self, track, clip=None):
        self.track, self.clip = track, clip
        self.import_calls = self.delete_calls = 0
        self.import_failure = self.delete_failure = None
        self.imported_length = 8
        self.last_imported = None
        self.after_import = self.after_delete = None

    @property
    def has_clip(self):
        return self.clip is not None

    def create_audio_clip(self, path):
        self.import_calls += 1
        assert not self.has_clip
        if self.import_failure == "before":
            raise RuntimeError("native import failed before insertion")
        if self.import_failure == "no_output":
            return
        self.clip = ImportedClip(path, self.imported_length)
        self.last_imported = self.clip
        if self.import_failure == "unknown_path":
            self.clip.file_path = None
        self.track.name = "Native auto name"
        if self.after_import:
            self.after_import(self)
        if self.import_failure == "after":
            raise RuntimeError("native import failed after insertion")

    def delete_clip(self):
        self.delete_calls += 1
        if self.delete_failure == "before":
            raise RuntimeError("native cleanup failed before deletion")
        if self.delete_failure != "silent":
            self.clip = None
        if self.after_delete:
            self.after_delete(self)
        if self.delete_failure == "after":
            raise RuntimeError("native cleanup failed after deletion")


def setup(tmp_path, slots=3):
    instance, song = remote()
    path = wav(tmp_path / "source.wav")
    track = AudioTrack(path, warped=False)
    track.name = "AUDIO target"
    track.clip_slots = [Slot(track) for _ in range(slots)]
    track.defer_bounds = True
    song.tracks = [track]
    song.tempo = 120.0
    song.tempo_follower_enabled = song.is_ableton_link_enabled = False
    song.master_track = types.SimpleNamespace(mixer_device=types.SimpleNamespace(
        song_tempo=types.SimpleNamespace(automation_state=0)))
    return instance, song, path


def request(instance, path, starts=(0.5,), **options):
    targets = instance._get_edit_targets()
    return targets["session_id"], [dict(file_path=str(path),
        destination_track_handle=targets["tracks"][0]["track_handle"],
        destination_beat=start, **options) for start in starts]


def preview(instance, path, starts=(0.5,), **options):
    session, placements = request(instance, path, starts, **options)
    return builder.build_arrangement(instance, "preview", session, placements)


def tick(instance):
    callbacks = list(instance._scheduled_callbacks)
    instance._scheduled_callbacks.clear()
    for track in instance._song.tracks:
        for slot in track.clip_slots:
            clip = slot.clip if slot.has_clip else None
            if clip is not None and hasattr(clip, "_pending_warp"):
                object.__setattr__(clip, "warping", clip._pending_warp)
                del clip._pending_warp
    settle_audio_bounds(instance)
    for delay, callback in callbacks:
        assert delay == 1
        callback()


def finish(instance, plan):
    for _ in range(6):
        if not instance._scheduled_callbacks:
            break
        tick(instance)
    assert not instance._scheduled_callbacks
    return apply(instance, plan)


def at_phase(instance, path, phase):
    plan = preview(instance, path, source_range={"start": 2, "end": 5, "units": "seconds"})
    assert apply(instance, plan)["status"] == "applying"
    for _ in range(4):
        result = apply(instance, plan)
        if result.get("phase") == phase:
            return plan
        tick(instance)
    raise AssertionError("phase not reached: " + phase)


def test_file_preview_is_read_only_and_reserves_full_file_and_distinct_empty_slots(tmp_path):
    instance, song, path = setup(tmp_path)
    track = song.tracks[0]
    occupied = Clip("Original Session phrase")
    track.clip_slots[0].clip = occupied
    plan = preview(instance, path, (0.5, 24.5), name="Audio excerpt",
                   source_range={"start": 2, "end": 5, "units": "seconds"})
    assert plan["status"] == "preview" and plan["expires_in_seconds"] == 300
    assert [row["source_slot"] for row in plan["placements"]] == [2, 3]
    for index, row in enumerate(plan["placements"]):
        assert row["kind"] == "audio" and row["file_path"] == str(path)
        assert row["file_duration_seconds"] == 12 and row["sample_frames"] == 1200
        assert row["channels"] == 2 and row["sample_rate"] == 100
        assert row["destination_beat"] == 0.5 + index * 24
        assert row["end_beat"] == 6.5 + index * 24
        assert row["reserved_end_beat"] == 24.5 + index * 24
        assert row["destination_track_name"] == "AUDIO target"
    assert track.name == "AUDIO target" and track.clip_slots[0].clip is occupied
    assert all(slot.import_calls == slot.delete_calls == 0 for slot in track.clip_slots)
    assert track.copy_calls == song.undo_started == 0


@pytest.mark.parametrize("excerpt", [False, True])
def test_file_apply_verifies_audio_cleans_staging_and_replays_without_more_mutation(tmp_path, excerpt):
    instance, song, path = setup(tmp_path)
    track = song.tracks[0]
    original = Clip("Original Session material")
    track.clip_slots[-1].clip = original
    keep = Clip("Existing arrangement", start=80)
    track.arrangement_clips.append(keep)
    options = {"source_range": {"start": 2, "end": 5, "units": "seconds"}} if excerpt else {}
    plan = preview(instance, path, name="Named file copy", **options)
    # Mutating the returned preview must never affect the retained file/range.
    plan["placements"][0]["source_range"]["end"] = 999
    plan["placements"][0]["file_identity"]["path"] = "/tmp/other.wav"
    initial = apply(instance, plan)
    assert initial["status"] == "applying" and track.copy_calls == 0
    assert all(slot.import_calls == 0 for slot in track.clip_slots)
    for _ in range(4):
        assert apply(instance, plan)["status"] == "applying"
    result = finish(instance, plan)
    assert result["status"] == "applied" and result["staging_cleaned"] is True
    row = result["placements"][0]
    assert row["source_slot"] == 1 and row["name"] == row["actual_name"] == "Named file copy"
    assert row["staging_cleaned"] is True and row["file_identity_verified"] is True
    copied = track.arrangement_clips[-1]
    assert (copied.start_time, copied.end_time) == (0.5, 6.5 if excerpt else 24.5)
    assert (copied.loop_start, copied.loop_end) == ((2, 5) if excerpt else (0, 12))
    assert copied.gain == 1 and copied.muted is False
    assert copied.pitch_coarse == copied.pitch_fine == 0 and copied.warping is copied.looping is False
    assert track.name == "AUDIO target" and track.clip_slots[-1].clip is original
    assert track.arrangement_clips[0] is keep and (keep.start_time, keep.end_time) == (80, 84)
    assert not track.clip_slots[0].has_clip and track.clip_slots[0].delete_calls == 1
    assert apply(instance, plan)["replayed"] is True and track.copy_calls == 1
    assert song.undo_started == song.undo_finished


@pytest.mark.parametrize("field,value", [
    ("name", None), ("name", ""), ("name", 3), ("name", "x" * 1025),
    ("destination_track_handle", None), ("destination_track_handle", []),
    ("destination_track_handle", "missing"), ("destination_beat", -1),
    ("destination_beat", float("nan")), ("source_range", {"start": 0, "end": 1, "units": "beats"}),
    ("source_range", {"start": 2, "end": 2, "units": "seconds"}),
    ("source_range", {"start": 0, "end": 13, "units": "seconds"}),
    ("source_range", {"start": True, "end": 1, "units": "seconds"}),
    ("source_slot", 1), ("track_handle", "source"), ("variation", {}),
])
def test_invalid_file_placement_rejects_whole_plan_without_imports(tmp_path, field, value):
    instance, song, path = setup(tmp_path)
    session, placements = request(instance, path, (0, 24))
    placements[1][field] = value
    with pytest.raises(ValueError):
        builder.build_arrangement(instance, "preview", session, placements)
    assert all(slot.import_calls == 0 for slot in song.tracks[0].clip_slots)


def test_mixed_session_and_file_plan_is_explicitly_rejected(tmp_path):
    instance, song, path = setup(tmp_path)
    session, placements = request(instance, path)
    placements.append({"track_handle": placements[0]["destination_track_handle"],
                       "source_slot": 1, "destination_beat": 30})
    with pytest.raises(ValueError, match="mixed Session/file"):
        builder.build_arrangement(instance, "preview", session, placements)
    assert song.tracks[0].copy_calls == 0


@pytest.mark.parametrize("stage", ["preview", "apply"])
@pytest.mark.parametrize("change", ["record", "link", "follower", "automation", "frozen", "group", "midi", "unknown_input"])
def test_unsafe_file_conditions_fail_before_import(tmp_path, stage, change):
    instance, song, path = setup(tmp_path)
    plan = preview(instance, path) if stage == "apply" else None
    if change == "record": song.record_mode = True
    elif change == "link": song.is_ableton_link_enabled = True
    elif change == "follower": song.tempo_follower_enabled = True
    elif change == "automation": song.master_track.mixer_device.song_tempo.automation_state = 1
    elif change == "frozen": song.tracks[0].is_frozen = True
    elif change == "group": song.tracks[0].is_foldable = True
    elif change == "midi": song.tracks[0].has_audio_input = False
    else: del song.tracks[0].has_audio_input
    with pytest.raises(ValueError):
        apply(instance, plan) if plan else preview(instance, path)
    assert all(slot.import_calls == 0 for slot in song.tracks[0].clip_slots)


def test_empty_staging_capacity_and_reserved_arrangement_span_are_preflighted(tmp_path):
    instance, song, path = setup(tmp_path, slots=1)
    with pytest.raises(ValueError, match="separate empty"):
        preview(instance, path, (0, 24))
    song.tracks[0].clip_slots.append(Slot(song.tracks[0]))
    with pytest.raises(ValueError, match="proposed placement"):
        preview(instance, path, (0, 8), source_range={"start": 2, "end": 5, "units": "seconds"})
    song.tracks[0].arrangement_clips.append(Clip("Beyond excerpt but inside full-file reservation", start=8))
    with pytest.raises(ValueError, match="overlaps existing"):
        preview(instance, path, source_range={"start": 2, "end": 5, "units": "seconds"})
    assert all(slot.import_calls == 0 for slot in song.tracks[0].clip_slots)


@pytest.mark.parametrize("failure", ["before", "after", "no_output"])
def test_later_import_failure_removes_only_owned_staging_and_no_arrangement(tmp_path, failure):
    instance, song, path = setup(tmp_path)
    track = song.tracks[0]
    original = Clip("Unrelated Session clip")
    track.clip_slots[2].clip = original
    track.clip_slots[1].import_failure = failure
    plan = preview(instance, path, (0, 24))
    assert apply(instance, plan)["status"] == "applying"
    result = finish(instance, plan)
    assert result["status"] == "error" and result["rollback_verified"] is True
    assert not track.clip_slots[0].has_clip and not track.clip_slots[1].has_clip
    assert track.clip_slots[0].delete_calls == 1
    assert track.clip_slots[1].delete_calls == (1 if failure == "after" else 0)
    assert track.clip_slots[2].clip is original and track.clip_slots[2].delete_calls == 0
    assert track.name == "AUDIO target" and track.copy_calls == 0
    assert apply(instance, plan)["replayed"] is True


def test_import_with_unverifiable_owned_path_is_left_untouched_and_reports_partial(tmp_path):
    instance, song, path = setup(tmp_path)
    slot = song.tracks[0].clip_slots[0]
    slot.import_failure = "unknown_path"
    plan = preview(instance, path)
    apply(instance, plan)
    result = finish(instance, plan)
    assert result["status"] == "partial" and result["rollback_verified"] is False
    assert slot.has_clip and slot.delete_calls == 0
    assert song.tracks[0].arrangement_clips == []


def test_stale_asd_footprint_cannot_expand_preview_reservation(tmp_path):
    instance, song, path = setup(tmp_path)
    slot = song.tracks[0].clip_slots[0]
    slot.imported_length = 32  # Full file was previewed at 24 beats.
    plan = preview(instance, path)
    assert plan["placements"][0]["reserved_end_beat"] == 24.5
    apply(instance, plan)
    result = finish(instance, plan)
    assert result["status"] == "error" and result["rollback_verified"] is True
    assert "previewed reservation" in result["message"]
    assert song.tracks[0].copy_calls == 0 and not slot.has_clip


@pytest.mark.parametrize("property,value", [
    ("gain", 0.75), ("pitch_coarse", 1), ("muted", True),
    ("sample_rate", 99), ("sample_length", 1199), ("name", "Unexpected name"),
    ("end_marker", 11),
])
def test_staging_normalization_requires_exact_source_readback(tmp_path, property, value):
    instance, song, path = setup(tmp_path)
    plan = at_phase(instance, path, "copy_audio")
    setattr(song.tracks[0].clip_slots[0].clip, property, value)
    result = finish(instance, plan)
    assert result["status"] == "error" and result["rollback_verified"] is True
    assert song.tracks[0].copy_calls == 0
    assert not song.tracks[0].clip_slots[0].has_clip


def test_unsettled_warping_aborts_before_marker_writes_and_cleans_staging(tmp_path):
    instance, song, path = setup(tmp_path)
    slot = song.tracks[0].clip_slots[0]
    slot.after_import = lambda imported: setattr(imported.clip, "ignore_warp", True)
    plan = preview(instance, path)
    apply(instance, plan)
    result = finish(instance, plan)
    assert result["status"] == "error" and result["rollback_verified"] is True
    assert "unwarped staging" in result["message"]
    assert song.tracks[0].copy_calls == 0 and not slot.has_clip


def test_unrestorable_native_track_rename_cannot_claim_verified_rollback(tmp_path, monkeypatch):
    instance, song, path = setup(tmp_path)
    track = song.tracks[0]
    original = type(track).__setattr__
    def refuse_restore(target, name, value):
        if name == "name" and value == "AUDIO target" and target.__dict__.get("name") == "Native auto name":
            raise RuntimeError("track name setter unavailable")
        original(target, name, value)
    monkeypatch.setattr(type(track), "__setattr__", refuse_restore)
    plan = preview(instance, path)
    apply(instance, plan)
    result = finish(instance, plan)
    assert result["status"] == "partial" and result["rollback_verified"] is False
    assert any("name restoration" in message for message in result["rollback_errors"])
    assert not track.clip_slots[0].has_clip and track.copy_calls == 0
    assert track.name == "Native auto name"


def test_unreadable_native_track_name_restoration_is_preserved_as_partial(tmp_path, monkeypatch):
    instance, song, path = setup(tmp_path)
    def unavailable(*_args):
        raise RuntimeError("track name read unavailable after native import")
    monkeypatch.setattr(files, "_restore_import_name", unavailable)
    plan = preview(instance, path)
    apply(instance, plan)
    result = finish(instance, plan)
    assert result["status"] == "partial" and result["rollback_verified"] is False
    assert any("name restoration" in message for message in result["rollback_errors"])
    assert not song.tracks[0].clip_slots[0].has_clip
    assert song.tracks[0].name == "Native auto name"


def test_set_switch_preserves_new_set_and_does_not_open_undo_group_there(tmp_path):
    instance, old_song, path = setup(tmp_path)
    plan = at_phase(instance, path, "normalize_source")
    _other, new_song = remote()
    original = new_song.tracks[0].clip_slots[0].clip
    instance.song = lambda: new_song
    tick(instance)
    result = instance._arrangement_plans[plan["plan_id"]]["result"]
    assert result["status"] == "partial" and result["rollback_verified"] is False
    assert old_song.tracks[0].clip_slots[0].has_clip
    assert new_song.tracks[0].clip_slots[0].clip is original
    assert new_song.undo_started == new_song.undo_finished == 0
    with pytest.raises(ValueError, match="Set"):
        apply(instance, plan)


@pytest.mark.parametrize("phase", ["import_audio", "normalize_source", "copy_audio", "verify_and_cleanup"])
@pytest.mark.parametrize("change", ["file", "tempo", "recording"])
def test_every_deferred_phase_rechecks_file_tempo_and_recording(tmp_path, phase, change):
    instance, song, path = setup(tmp_path)
    plan = at_phase(instance, path, phase)
    if change == "file":
        with path.open("ab") as stream:
            stream.write(b"changed")
    elif change == "tempo":
        song.tempo = 121
    else:
        song.session_record = True
    result = finish(instance, plan)
    assert result["status"] == "error" and result["rollback_verified"] is True
    assert song.tracks[0].arrangement_clips == []
    assert all(not slot.has_clip for slot in song.tracks[0].clip_slots)
    assert song.session_record is (change == "recording")


@pytest.mark.parametrize("phase", ["normalize_source", "copy_audio", "verify_and_cleanup"])
@pytest.mark.parametrize("change", ["slot", "clip", "track", "file_path"])
def test_deferred_identity_substitution_never_deletes_replacement_content(tmp_path, phase, change):
    instance, song, path = setup(tmp_path)
    track = song.tracks[0]
    old_slot = track.clip_slots[0]
    plan = at_phase(instance, path, phase)
    replacement_clip = ImportedClip(path)
    if change == "slot":
        track.clip_slots[0] = Slot(track, replacement_clip)
    elif change == "clip":
        old_slot.clip = replacement_clip
    elif change == "track":
        song.tracks[0] = Track("Replacement")
    else:
        old_slot.clip.file_path = str(wav(tmp_path / "replacement.wav"))
    result = finish(instance, plan)
    assert result["status"] == "partial" and result["rollback_verified"] is False
    assert old_slot.delete_calls == 0
    if change == "slot":
        assert track.clip_slots[0].clip is replacement_clip and track.clip_slots[0].delete_calls == 0
    elif change == "clip":
        assert old_slot.clip is replacement_clip
    elif change == "track":
        assert song.tracks[0].name == "Replacement" and song.tracks[0].delete_calls == []
    else:
        assert old_slot.has_clip


def test_track_rename_and_reorder_while_pending_follow_original_handle(tmp_path):
    instance, song, path = setup(tmp_path)
    track = song.tracks[0]
    plan = at_phase(instance, path, "normalize_source")
    track.name = "User renamed target"
    song.tracks.insert(0, Track("Inserted"))
    result = finish(instance, plan)
    assert result["status"] == "applied"
    assert result["placements"][0]["destination_track_name"] == "User renamed target"
    assert track.name == "User renamed target" and song.tracks[0].copy_calls == 0


@pytest.mark.parametrize("failure", ["before", "silent", "after"])
def test_cleanup_failures_roll_back_arrangement_and_report_verified_state(tmp_path, failure):
    instance, song, path = setup(tmp_path)
    slot = song.tracks[0].clip_slots[0]
    slot.delete_failure = failure
    plan = preview(instance, path)
    apply(instance, plan)
    result = finish(instance, plan)
    assert result["status"] == ("error" if failure == "after" else "partial")
    assert result["rollback_verified"] is (failure == "after")
    assert song.tracks[0].arrangement_clips == []
    assert slot.has_clip is (failure != "after")


def test_file_change_during_success_cleanup_rolls_back_owned_arrangement(tmp_path):
    instance, song, path = setup(tmp_path)
    slot = song.tracks[0].clip_slots[0]
    def mutate_file(_slot):
        with path.open("ab") as stream:
            stream.write(b"changed during cleanup")
    slot.after_delete = mutate_file
    plan = preview(instance, path)
    apply(instance, plan)
    result = finish(instance, plan)
    assert result["status"] == "error" and result["rollback_verified"] is True
    assert not slot.has_clip and song.tracks[0].arrangement_clips == []


def test_native_arrangement_failure_after_insertion_cleans_both_owned_surfaces(tmp_path):
    instance, song, path = setup(tmp_path)
    track = song.tracks[0]
    native = track.duplicate_clip_to_arrangement
    def fail_after(clip, start):
        native(clip, start)
        raise RuntimeError("native arrangement failure after insertion")
    track.duplicate_clip_to_arrangement = fail_after
    plan = preview(instance, path)
    apply(instance, plan)
    result = finish(instance, plan)
    assert result["status"] == "error" and result["rollback_verified"] is True
    assert all(not slot.has_clip for slot in track.clip_slots) and track.arrangement_clips == []
    assert len(track.delete_calls) == 1


def test_changed_other_session_material_is_preserved_and_reported_partial(tmp_path):
    instance, song, path = setup(tmp_path)
    plan = at_phase(instance, path, "normalize_source")
    inserted = Clip("User added Session content")
    song.tracks[0].clip_slots[-1].clip = inserted
    result = finish(instance, plan)
    assert result["status"] == "partial" and result["rollback_verified"] is False
    assert song.tracks[0].clip_slots[-1].clip is inserted
    assert song.tracks[0].clip_slots[-1].delete_calls == 0
    assert not song.tracks[0].clip_slots[0].has_clip


def test_global_pending_guard_blocks_session_and_file_plans_until_staging_cleanup(tmp_path):
    instance, song, path = setup(tmp_path)
    other_file = preview(instance, path, (48,))
    session_track = Track("Session MIDI")
    song.tracks.append(session_track)
    targets = instance._get_edit_targets()
    session_plan = builder.build_arrangement(instance, "preview", targets["session_id"], [{
        "track_handle": targets["tracks"][1]["track_handle"], "source_slot": 1, "destination_beat": 0}])
    pending = at_phase(instance, path, "verify_and_cleanup")
    for action in (lambda: apply(instance, other_file), lambda: apply(instance, session_plan),
                   lambda: preview(instance, path, (72,))):
        with pytest.raises(ValueError, match="applying"):
            action()
    assert finish(instance, pending)["status"] == "applied"
    assert apply(instance, session_plan)["status"] == "applied"


def test_pending_file_plan_ignores_preview_expiry_and_cannot_be_evicted(tmp_path):
    instance, song, path = setup(tmp_path)
    plan = at_phase(instance, path, "normalize_source")
    instance._arrangement_plans[plan["plan_id"]]["created"] -= 301
    with pytest.raises(ValueError, match="applying"):
        preview(instance, path, (48,))
    assert apply(instance, plan)["status"] == "applying"
    assert finish(instance, plan)["status"] == "applied"


@pytest.mark.parametrize("trigger", ["poll", "callback"])
def test_pending_deadline_cleans_staging_even_after_dropped_scheduler_callback(tmp_path, monkeypatch, trigger):
    instance, song, path = setup(tmp_path)
    plan = at_phase(instance, path, "normalize_source")
    slot = song.tracks[0].clip_slots[0]
    assert slot.has_clip
    clock = files.time.monotonic
    monkeypatch.setattr(files.time, "monotonic", lambda: clock() + 31)
    if trigger == "poll":
        instance._scheduled_callbacks.clear()
        result = apply(instance, plan)
    else:
        tick(instance)
        result = apply(instance, plan)
    assert result["status"] == "error" and result["rollback_verified"] is True
    assert "pending deadline" in result["message"]
    assert result["placements"][0]["file_path"] == str(path)
    assert result["placements"][0]["status"] == "rolled_back"
    assert not slot.has_clip and song.tracks[0].copy_calls == 0
    assert apply(instance, plan)["replayed"] is True


def test_scheduling_failure_after_copy_cancels_queued_inner_finalizer(tmp_path):
    instance, song, path = setup(tmp_path)
    plan = at_phase(instance, path, "copy_audio")
    original_schedule = instance.schedule_message
    calls = []
    def fail_outer_schedule(delay, callback):
        calls.append(callback)
        if len(calls) == 2:
            raise RuntimeError("cannot schedule outer cleanup")
        original_schedule(delay, callback)
    instance.schedule_message = fail_outer_schedule
    tick(instance)
    result = apply(instance, plan)
    assert result["status"] == "error" and result["rollback_verified"] is True
    assert song.tracks[0].arrangement_clips == []
    deletions, undo_count = len(song.tracks[0].delete_calls), song.undo_started
    tick(instance)  # Previously queued inner finalizer must be inert.
    assert len(song.tracks[0].delete_calls) == deletions and song.undo_started == undo_count
    assert apply(instance, plan)["status"] == "error"


def test_multi_destination_and_reverse_time_order_resolve_owned_copies_by_placement(tmp_path):
    instance, song, path = setup(tmp_path)
    second = AudioTrack(path, warped=False)
    second.name = "Second audio target"
    second.clip_slots = [Slot(second)]
    second.defer_bounds = True
    song.tracks.append(second)
    session, placements = request(instance, path, (48, 0))
    targets = instance._get_edit_targets()
    placements.insert(1, dict(placements[0], destination_track_handle=targets["tracks"][1]["track_handle"],
                              destination_beat=0))
    plan = builder.build_arrangement(instance, "preview", session, placements)
    apply(instance, plan)
    result = finish(instance, plan)
    assert result["status"] == "applied"
    assert [row["destination_beat"] for row in result["placements"]] == [48, 0, 0]
    assert len(song.tracks[0].arrangement_clips) == 2 and len(second.arrangement_clips) == 1


@pytest.mark.parametrize("side_effect", ["settle", "damage_original", "owned_outside_reservation", "owned_wrong_final", "remove_owned"])
def test_later_native_copy_can_settle_prior_owned_edges_without_weakening_original_guards(tmp_path, side_effect):
    instance, song, path = setup(tmp_path)
    track = song.tracks[0]
    keep = Clip("Original arrangement must stay exact", start=80)
    track.arrangement_clips.append(keep)
    original_copy = track.duplicate_clip_to_arrangement
    observations = []
    def copy_after_flushing_pending_edges(clip, start):
        if track.copy_calls:
            prior = track.arrangement_clips[1]
            observations.append((prior.end_time, prior._pending_end_time))
            prior.end_time = prior._pending_end_time
            del prior._pending_end_time
            if side_effect == "damage_original":
                keep.end_time -= 1
            elif side_effect == "owned_outside_reservation":
                prior.end_time = 25
            elif side_effect == "owned_wrong_final":
                prior.end_time += 1  # Inside reservation, but final readback must still reject it.
            elif side_effect == "remove_owned":
                track.arrangement_clips.remove(prior)
        original_copy(clip, start)
    track.duplicate_clip_to_arrangement = copy_after_flushing_pending_edges
    plan = preview(instance, path, (0.5, 32.5),
                   source_range={"start": 2, "end": 5, "units": "seconds"})
    apply(instance, plan)
    result = finish(instance, plan)
    assert observations == [(8.5, 6.5)]
    assert all(not slot.has_clip for slot in track.clip_slots)
    assert keep not in track.delete_calls
    if side_effect == "settle":
        assert result["status"] == "applied"
        assert [(clip.start_time, clip.end_time) for clip in track.arrangement_clips[1:]] == [(0.5, 6.5), (32.5, 38.5)]
        assert (keep.start_time, keep.end_time) == (80, 84)
    elif side_effect == "damage_original":
        assert result["status"] == "partial" and result["rollback_verified"] is False
        assert "bounds changed" in result["message"]
        assert track.arrangement_clips == [keep] and keep.end_time == 83
    else:
        assert result["status"] == "error" and result["rollback_verified"] is True
        assert track.arrangement_clips == [keep] and keep.end_time == 84
