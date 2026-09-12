"""Scheduled-tick recorder regressions; never connect to or operate Ableton Live."""

import json
from types import SimpleNamespace as NS

import pytest

from tests.unit.test_remote_script_helpers import AbletonMCP


class Track:
    def __init__(self, name, arm=False):
        self.name = name
        self.arm = arm
        self.can_be_armed = True
        self.arrangement_clips = []
        self.clip_slots = []
        self.available_input_routing_types = []
        self.current_monitoring_state = 0
        self.mixer_device = NS(track_activator=NS(value=1))


class Song:
    def __init__(self, tracks):
        self.tracks = tracks
        self.return_tracks = []
        self.scenes = []
        self.start_time = 16.0
        self.current_song_time = 0.0
        self.loop = True
        self.record_mode = False
        self.tempo = 120.0
        self.is_counting_in = False
        self.starts = 0
        self.stops = 0
        self.creates = 0
        self.fail_create = None
        self.fail_start = False

    def start_playing(self):
        if self.fail_start:
            raise RuntimeError("start failed")
        self.starts += 1

    def stop_playing(self):
        self.stops += 1

    def create_audio_track(self, _index):
        self.creates += 1
        # Live's observed exclusive-arm side effect is on track creation.
        for track in self.tracks:
            track.arm = False
        if self.creates == self.fail_create:
            raise RuntimeError("create failed")
        track = Track("New Audio", arm=True)
        track.available_input_routing_types = [NS(display_name=t.name) for t in self.tracks]
        track.available_input_routing_types.append(NS(display_name="Resampling"))
        self.tracks.append(track)

    def delete_track(self, index):
        self.tracks.pop(index)


class Rig:
    def __init__(self, tracks=None):
        self.song = Song(tracks if tracks is not None else [Track("Bass", True), Track("Lead")])
        self.current_song = self.song
        self.script = AbletonMCP.__new__(AbletonMCP)
        self.script.song = lambda: self.current_song
        self.script.log_message = lambda _message: None
        self.queue = []
        self.script.schedule_message = lambda _delay, callback: self.queue.append(callback)
        self.parameter = NS(min=0, max=1, value=0, name="Cutoff")
        self.script._resolve_parameter = lambda *_args: (self.parameter, "Synth")
        self.scene_fires = 0
        self.song.scenes = [NS(name="Verse", is_empty=False, fire=self.fire)]
        self.song.tracks[0].clip_slots = [NS(has_clip=True)]

    def fire(self):
        self.scene_fires += 1

    def tick(self, position=None):
        if position is not None:
            self.song.current_song_time = position
        assert self.queue, "Expected a queued Live tick"
        self.queue.pop(0)()

    def drain(self, limit=100):
        for _ in range(limit):
            if not self.queue:
                return
            self.tick()
        raise AssertionError("Recorder did not stop scheduling callbacks")

    def start(self, kind, start=0, end=8):
        if kind == "bounce":
            return self.script._bounce_to_audio(start, end, "Bass")
        if kind == "freeze":
            return self.script._freeze_track(0, start, end)
        if kind == "stems":
            return self.script._export_stems(start, end, ["Bass", "Lead"])
        if kind == "take":
            return self.script._record_over_range(0, start, end)
        if kind == "automation":
            return self.script._record_arrangement_automation(
                0, "Cutoff", [{"time": start, "value": 0}, {"time": end, "value": 1}])
        if kind == "capture":
            return self.script._capture_session_to_arrangement(0, start, end)
        raise AssertionError(kind)

    def configure(self):
        if self.status["status"] == "preparing":
            self.tick()

    @property
    def status(self):
        return self.script._get_automation_record_status()


class FreshLiveProxy:
    """Live-style wrapper: each read is fresh but equality uses entity identity."""

    def __init__(self, entity):
        object.__setattr__(self, "_entity", entity)

    def __getattr__(self, name):
        value = getattr(self._entity, name)
        if name in ("tracks", "return_tracks", "arrangement_clips", "scenes"):
            return tuple(FreshLiveProxy(item) for item in value)
        return value

    def __setattr__(self, name, value):
        setattr(self._entity, name, value)

    def __eq__(self, other):
        return isinstance(other, FreshLiveProxy) and self._entity is other._entity


@pytest.fixture(params=["ordinary_objects", "fresh_live_proxies"])
def rig(request):
    rig = Rig()
    if request.param == "fresh_live_proxies":
        rig.script.song = lambda: FreshLiveProxy(rig.current_song)
    return rig


def add_clip(track, tmp_path, name="recorded.wav", start=0, end=8, midi=False, exists=True):
    path = tmp_path / name
    if exists and not midi:
        path.write_bytes(b"a nonempty audio fixture")
    clip = NS(name=name, start_time=start, end_time=end, is_midi_clip=midi,
              file_path="" if midi else str(path))
    track.arrangement_clips.append(clip)
    return clip


@pytest.mark.parametrize("kind", ["bounce", "freeze", "stems", "capture"])
def test_cancel_before_first_tick_never_starts_or_deactivates(rig, kind):
    original = list(rig.song.tracks)
    started = rig.start(kind)
    assert started["status"] == "preparing"
    cancelled = rig.script._cancel_automation_record()
    rig.drain()
    assert cancelled["cancelled"]
    assert rig.status["status"] == "cancelled"
    assert rig.song.starts == rig.scene_fires == 0
    assert rig.song.tracks == original
    assert original[0].arm
    assert original[0].mixer_device.track_activator.value == 1


@pytest.mark.parametrize("kind", ["bounce", "freeze", "stems", "take", "automation", "capture"])
def test_old_callback_cannot_terminate_replacement_operation(rig, kind):
    first = rig.start(kind, 0, 4)
    rig.configure()
    rig.script._cancel_automation_record()
    second = rig.start("take", 100, 200)
    assert first["operation_id"] != second["operation_id"]
    rig.tick(100.1)  # Old callback captured 0..4, not the new operation.
    assert rig.status["operation_id"] == second["operation_id"]
    assert rig.status["status"] == "recording"
    assert rig.status["position"] == 100
    assert rig.song.record_mode


@pytest.mark.parametrize("kind", ["bounce", "stems"])
def test_success_disarms_owned_outputs_and_restores_precreation_arms(rig, kind, tmp_path):
    original = list(rig.song.tracks)
    rig.start(kind)
    outputs = list(rig.song.tracks[2:])
    rig.configure()
    assert all(track.arm for track in outputs)
    assert all(not track.arm for track in original)
    for index, target in enumerate(outputs):
        add_clip(target, tmp_path, "stem-{0}.wav".format(index))
    rig.tick(8)
    assert rig.status["status"] == "finalizing"
    assert original[0].arm and not original[1].arm
    assert all(not track.arm for track in outputs)
    rig.tick()
    assert rig.status["status"] == "done"
    assert all(row["status"] == "complete" for row in rig.status["outputs"])


@pytest.mark.parametrize("kind", ["freeze", "bounce", "stems", "capture", "take", "automation"])
def test_active_job_rejects_other_pass_without_structural_or_arm_changes(rig, kind):
    first = rig.start("take")
    before = [(track, track.arm) for track in rig.song.tracks]
    with pytest.raises(RuntimeError, match="already running"):
        rig.start(kind)
    assert [(track, track.arm) for track in rig.song.tracks] == before
    assert rig.song.creates == 0
    assert rig.status["operation_id"] == first["operation_id"]


@pytest.mark.parametrize("kind", ["freeze", "bounce", "stems", "capture", "take", "automation"])
@pytest.mark.parametrize("start,end", [(8, 8), (-1, 8), (0, float("inf")), (float("nan"), 8)])
def test_invalid_bounds_leave_the_set_untouched(rig, kind, start, end):
    with pytest.raises(ValueError):
        rig.start(kind, start, end)
    assert rig.song.creates == rig.song.starts == 0
    assert rig.song.tracks[0].arm
    assert rig.song.start_time == 16
    assert rig.song.loop and not rig.song.record_mode


def test_duplicate_and_missing_sources_fail_before_creation(rig):
    for names in (["Missing"], ["Bass", "Bass"]):
        with pytest.raises(ValueError):
            rig.script._export_stems(0, 8, names)
    rig.song.tracks.append(Track("Bass"))
    with pytest.raises(ValueError, match="Ambiguous"):
        rig.start("bounce")
    assert rig.song.creates == 0


def test_backward_bounce_range_is_rejected_before_creation(rig):
    with pytest.raises(ValueError):
        rig.start("bounce", 8, 0)
    assert rig.song.creates == 0 and rig.song.tracks[0].arm


@pytest.mark.parametrize("failure", ["create", "route", "transport"])
def test_setup_failure_restores_original_arms_and_removes_only_owned_tracks(rig, failure):
    original = list(rig.song.tracks)
    if failure == "create":
        rig.song.fail_create = 2
        with pytest.raises(RuntimeError, match="create failed"):
            rig.start("stems")
    else:
        rig.start("stems")
        if failure == "route":
            rig.song.tracks[-1].available_input_routing_types = []
        else:
            rig.song.fail_start = True
        inserted = Track("Inserted")
        rig.song.tracks.insert(0, inserted)
        rig.tick()
        assert inserted in rig.song.tracks
        original.insert(0, inserted)
    assert rig.song.tracks == original
    assert next(track for track in original if track.name == "Bass").arm
    assert not rig.song.record_mode
    assert rig.status["status"] == "failed"
    assert len(rig.status["outputs"]) == 2
    assert all(row["status"] == "missing" for row in rig.status["outputs"])


def test_insertion_before_configuration_keeps_correct_routes_and_arm_restore(rig, tmp_path):
    bass, lead = rig.song.tracks
    rig.start("stems")
    targets = list(rig.song.tracks[2:])
    inserted = Track("Inserted", True)
    rig.song.tracks.insert(0, inserted)
    rig.configure()
    assert [target.input_routing_type.display_name for target in targets] == ["Bass", "Lead"]
    assert not inserted.arm
    for index, target in enumerate(targets):
        add_clip(target, tmp_path, str(index) + ".wav")
    rig.tick(8)
    rig.tick()
    assert bass.arm and inserted.arm and not lead.arm
    assert all(not target.arm for target in targets)


@pytest.mark.parametrize("phase", ["preparing", "recording", "finalizing"])
def test_set_change_invalidates_callbacks_without_touching_either_set(rig, phase, tmp_path):
    rig.start("freeze")
    if phase != "preparing":
        rig.configure()
    if phase == "finalizing":
        add_clip(rig.song.tracks[-1], tmp_path)
        rig.tick(8)
    original_counts = (rig.song.starts, rig.song.stops, rig.song.record_mode)
    new_song = Song([Track("Different", True)])
    rig.current_song = new_song
    rig.drain()
    assert rig.status["status"] == "set_changed"
    assert (rig.song.starts, rig.song.stops, rig.song.record_mode) == original_counts
    assert new_song.starts == new_song.stops == 0
    assert new_song.tracks[0].arm and not new_song.record_mode
    assert rig.song.tracks[0].mixer_device.track_activator.value == 1


def test_freeze_deactivates_captured_source_only_after_later_tick_verifies_file(rig, tmp_path):
    source = rig.song.tracks[0]
    rig.start("freeze")
    target = rig.song.tracks[-1]
    rig.configure()
    rig.tick(8)
    assert rig.status["status"] == "finalizing"
    assert source.mixer_device.track_activator.value == 1
    rig.tick()
    assert source.mixer_device.track_activator.value == 1
    inserted = Track("Inserted")
    rig.song.tracks.insert(0, inserted)
    add_clip(target, tmp_path)
    rig.tick()
    assert rig.status["status"] == "done"
    assert source.mixer_device.track_activator.value == 0
    assert inserted.mixer_device.track_activator.value == 1


@pytest.mark.parametrize("output", ["none", "partial", "missing_file", "empty_file"])
def test_freeze_without_usable_full_capture_keeps_original_enabled(rig, tmp_path, output):
    rig.start("freeze")
    rig.configure()
    target = rig.song.tracks[-1]
    if output != "none":
        clip = add_clip(target, tmp_path, end=4 if output == "partial" else 8,
                        exists=output != "missing_file")
        if output == "empty_file":
            (tmp_path / "recorded.wav").write_bytes(b"")
    rig.tick(8)
    rig.drain()
    assert rig.status["status"] == "failed"
    assert rig.song.tracks[0].mixer_device.track_activator.value == 1
    assert rig.status["outputs"][0]["status"] in ("missing", "partial")


@pytest.mark.parametrize("kind", ["bounce", "freeze", "stems", "take", "automation", "capture"])
def test_manual_stop_is_interrupted_never_completed(rig, kind):
    rig.start(kind)
    rig.configure()
    rig.tick(2)
    rig.tick(2)
    rig.tick(2)
    rig.tick(2)
    assert rig.status["status"] == "interrupted"
    assert rig.status["progress"] == .25
    assert rig.status["captured"] == {"from_beat": 0, "to_beat": 2}
    assert rig.song.tracks[0].mixer_device.track_activator.value == 1
    assert not rig.song.record_mode


def test_never_started_and_countin_timeout_release_transport(rig):
    rig.start("freeze")
    rig.configure()
    rig.drain()
    assert rig.status["status"] == "never_started"
    assert not rig.song.record_mode and rig.song.tracks[0].arm
    rig.song.is_counting_in = True
    rig.start("automation", 0, 1)
    rig.drain(200)
    assert rig.status["status"] == "timeout"


def test_missing_one_stem_returns_all_manifest_rows_and_truthful_status(rig, tmp_path):
    rig.start("stems")
    rig.configure()
    add_clip(rig.song.tracks[-2], tmp_path)
    rig.tick(8)
    rig.drain()
    status = rig.status
    assert status["status"] == "failed"
    assert [row["source"] for row in status["outputs"]] == ["Bass", "Lead"]
    assert [row["status"] for row in status["outputs"]] == ["complete", "missing"]
    assert status["outputs"][0]["file_exists"]
    assert status["outputs"][0]["source_identity"]["identity"]
    assert status["outputs"][1]["file_path"] is None


def test_status_is_detached_json_and_never_serializes_live_references(rig):
    initial = rig.start("stems")
    json.dumps(initial)
    assert "restore" not in initial and "_song" not in initial
    initial["outputs"][0]["source_identity"]["name"] = "Changed by client"
    assert rig.status["outputs"][0]["source_identity"]["name"] == "Bass"


def test_old_finalizer_cannot_deactivate_source_or_modify_new_job(rig, tmp_path):
    rig.start("freeze")
    rig.configure()
    add_clip(rig.song.tracks[-1], tmp_path)
    rig.tick(8)
    rig.script._cancel_automation_record()
    new_job = rig.start("take", 100, 200)
    rig.tick(101)
    assert rig.status["operation_id"] == new_job["operation_id"]
    assert rig.status["status"] == "recording"
    assert rig.song.tracks[0].mixer_device.track_activator.value == 1


def test_capture_reports_overlap_before_recording_and_disarms_unrelated_inputs(rig, tmp_path):
    unrelated = rig.song.tracks[1]
    unrelated.arm = True
    old = add_clip(rig.song.tracks[0], tmp_path, name="old.wav", start=2, end=6)
    result = rig.start("capture")
    assert rig.song.starts == 0
    assert result["overlap_plan"]["overlaps"][0]["clip"] == old.name
    rig.configure()
    assert all(not track.arm for track in rig.song.tracks)
    assert rig.scene_fires == 1
    rig.script._cancel_automation_record()
    assert unrelated.arm and rig.song.tracks[0].arm


def test_existing_clip_is_not_evidence_of_new_capture(rig, tmp_path):
    add_clip(rig.song.tracks[0], tmp_path)
    rig.start("take")
    rig.tick(8)
    rig.drain()
    assert rig.status["status"] == "failed"
    assert rig.status["outputs"][0]["status"] == "missing"


def test_midi_recording_completes_without_an_audio_file(rig, tmp_path):
    rig.start("take")
    add_clip(rig.song.tracks[0], tmp_path, midi=True)
    rig.tick(8)
    rig.tick()
    assert rig.status["status"] == "done"
    assert rig.status["outputs"][0]["media_type"] == "midi"


def test_removed_source_fails_configuration_without_retargeting(rig):
    rig.start("freeze")
    source = rig.song.tracks.pop(0)
    survivor = rig.song.tracks[0]
    rig.tick()
    assert rig.status["status"] == "failed"
    assert rig.song.tracks == [survivor]
    assert source.mixer_device.track_activator.value == 1


def test_rejected_existing_arrangement_recording_is_untouched(rig):
    rig.song.record_mode = True
    with pytest.raises(RuntimeError, match="already enabled"):
        rig.start("bounce")
    assert rig.song.creates == rig.song.stops == 0
    assert rig.song.record_mode


def test_scheduling_failure_rolls_back_preparation(rig):
    def fail(*_args):
        raise RuntimeError("schedule unavailable")
    rig.script.schedule_message = fail
    with pytest.raises(RuntimeError, match="schedule unavailable"):
        rig.start("stems")
    assert len(rig.song.tracks) == 2
    assert rig.song.tracks[0].arm
    assert rig.status["status"] == "failed"


@pytest.mark.parametrize("phase", ["recording", "finalizing"])
def test_later_scheduling_failure_releases_job_and_preserves_source(rig, phase):
    rig.start("freeze")
    rig.configure()
    if phase == "finalizing":
        rig.tick(8)
    def fail(*_args):
        raise RuntimeError("later schedule failed")
    rig.script.schedule_message = fail
    rig.tick(2 if phase == "recording" else None)
    assert rig.status["status"] == "failed"
    assert not rig.status["active"]
    assert not rig.song.record_mode
    assert rig.song.tracks[0].mixer_device.track_activator.value == 1


def test_source_without_audio_is_rejected_before_creation(rig):
    rig.song.tracks[0].has_audio_output = False
    with pytest.raises(ValueError, match="no audio output"):
        rig.start("bounce")
    assert rig.song.creates == 0


def test_idle_cancel_returns_consistent_structured_status(rig):
    result = rig.script._cancel_automation_record()
    assert not result["cancelled"]
    assert result["status"] == "idle"
    assert result["operation_id"] is None
    assert result["progress"] == 0


def test_removed_early_output_cannot_remain_complete_while_other_file_finalizes(rig, tmp_path):
    rig.start("stems")
    rig.configure()
    first, second = rig.song.tracks[-2:]
    add_clip(first, tmp_path, "first.wav")
    rig.tick(8)
    rig.tick()
    assert rig.status["outputs"][0]["status"] == "complete"
    rig.song.tracks.remove(first)
    add_clip(second, tmp_path, "second.wav")
    rig.drain()
    assert rig.status["status"] == "failed"
    assert [row["status"] for row in rig.status["outputs"]] == ["missing", "complete"]


def test_failed_deactivation_restores_original_activator(rig, tmp_path):
    class FailingActivator:
        def __init__(self):
            self._value = 1

        @property
        def value(self):
            return self._value

        @value.setter
        def value(self, value):
            self._value = value
            if value == 0:
                raise RuntimeError("changed but reported failure")

    source = rig.song.tracks[0]
    source.mixer_device.track_activator = FailingActivator()
    rig.start("freeze")
    rig.configure()
    add_clip(rig.song.tracks[-1], tmp_path)
    rig.tick(8)
    rig.tick()
    assert rig.status["status"] == "failed"
    assert source.mixer_device.track_activator.value == 1


@pytest.mark.parametrize("kind", ["bounce", "freeze", "stems", "take", "automation", "capture"])
def test_fresh_proxies_complete_every_recording_workflow(kind, tmp_path):
    rig = Rig()
    rig.script.song = lambda: FreshLiveProxy(rig.current_song)
    first, second = rig.script._song, rig.script._song
    assert first is not second and first == second
    assert first.tracks[0] is not first.tracks[0]
    assert first.tracks[0] == first.tracks[0]
    started = rig.start(kind)
    assert started["active"]
    rig.configure()
    assert rig.status["status"] == "recording"
    outputs = (rig.song.tracks[2:] if kind in ("bounce", "freeze", "stems")
               else rig.song.tracks[:1])
    if kind != "automation":
        for index, output in enumerate(outputs):
            add_clip(output, tmp_path, "proxy-{0}.wav".format(index))
    rig.tick(8)
    rig.drain()
    assert rig.status["status"] == "done"
    assert rig.song.tracks[0].arm and not rig.song.tracks[1].arm
    assert all(not track.arm for track in rig.song.tracks[2:])
    assert len(rig.script._auto_rec["restore"]["arm_map"]) == 2
    if kind in ("bounce", "freeze"):
        assert started["bounce_track_index"] == 2
        assert started["outputs"][0]["source_identity"]["index_at_start"] == 0
        assert started["outputs"][0]["track"]["index_at_start"] == 2


def test_equal_name_replacement_source_is_not_original_entity(rig):
    rig.start("freeze")
    original = rig.song.tracks.pop(0)
    replacement = Track(original.name, True)
    rig.song.tracks.insert(0, replacement)
    rig.tick()
    assert rig.status["status"] == "failed"
    assert rig.song.starts == 0
    assert replacement.mixer_device.track_activator.value == 1
    assert original.mixer_device.track_activator.value == 1
    assert len(rig.song.tracks) == 2


def test_distinct_set_with_same_metadata_invalidates_proxy_owner_without_cleanup():
    rig = Rig()
    rig.script.song = lambda: FreshLiveProxy(rig.current_song)
    rig.song.name = "Same Set Name"
    rig.song.file_path = "/same/path.als"
    rig.start("take")
    replacement = Song([Track("Bass", True), Track("Lead")])
    replacement.name, replacement.file_path = rig.song.name, rig.song.file_path
    rig.current_song = replacement
    rig.drain()
    assert rig.status["status"] == "set_changed"
    assert rig.song.stops == replacement.stops == 0
    assert rig.song.record_mode and not replacement.record_mode
    assert replacement.tracks[0].arm


def test_invalid_live_proxy_comparison_fails_closed():
    class InvalidProxy:
        def __eq__(self, _other):
            raise RuntimeError("Live entity was deleted")
    assert not AbletonMCP._recording_same_object(InvalidProxy(), InvalidProxy())
    assert not AbletonMCP._recording_same_object(None, InvalidProxy())
