"""Monitoring profiles reconcile only their own monitor-state changes."""

import copy
import types

import pytest

from .test_arrangement_builder import Track, remote
from AbletonMCP_Remote_Script import monitoring_setup as monitoring


def route(label):
    return types.SimpleNamespace(identifier=label, display_name=label)


class AudioTrack(Track):
    def __init__(self, name="Vocal", state=0):
        super().__init__(name)
        self.has_audio_input, self.has_midi_input = True, False
        self.arm = False
        self.devices = [types.SimpleNamespace(name="Original effect", is_active=True)]
        self.input_routing_type, self.input_routing_channel = route("Ext. In"), route("1")
        self.output_routing_type, self.output_routing_channel = route("Main"), route("1/2")
        self._state = state
        self.writes = []
        self.failure = None
        self.after_write = None
        self.defer = False
        self.read_failure = False

    @property
    def current_monitoring_state(self):
        if self.read_failure:
            raise RuntimeError("native monitor read failed")
        return self._state

    @current_monitoring_state.setter
    def current_monitoring_state(self, value):
        self.writes.append(value)
        failure = self.failure
        if callable(failure):
            failure = failure(value, len(self.writes))
        if failure == "before":
            raise RuntimeError("monitor write failed before mutation")
        if failure != "silent":
            if self.defer:
                self._pending_monitor = value
            else:
                self._state = value
        if self.after_write:
            self.after_write(self, value)
        if failure == "after":
            raise RuntimeError("monitor write failed after mutation")


def setup(states=(0, 2)):
    instance, song = remote()
    song.tracks = [AudioTrack("Audio " + str(index), state) for index, state in enumerate(states)]
    return instance, song


def preview(instance, path="live", handles=None):
    targets = instance._get_edit_targets()
    if handles is None:
        handles = [row["track_handle"] for row in targets["tracks"] if row["kind"] == "track"]
    return monitoring.configure(instance, "preview", targets["session_id"], handles, path)


def call(instance, plan, action="apply"):
    return monitoring.configure(instance, action, plan["session_id"], plan_id=plan["plan_id"])


def tick(instance):
    callbacks = list(instance._scheduled_callbacks)
    instance._scheduled_callbacks.clear()
    for track in instance._song.tracks:
        if hasattr(track, "_pending_monitor"):
            track._state = track._pending_monitor
            del track._pending_monitor
    for delay, callback in callbacks:
        assert delay == 1
        callback()


def finish(instance, plan, action="apply"):
    for _ in range(4):
        if not instance._scheduled_callbacks:
            break
        tick(instance)
    assert not instance._scheduled_callbacks
    return call(instance, plan, action)


@pytest.mark.parametrize("path,target", [("live", 1), ("direct", 2)])
@pytest.mark.parametrize("deferred", [False, True])
def test_profile_read_only_preview_deferred_apply_restore_and_replay(path, target, deferred):
    instance, song = setup()
    for track in song.tracks:
        track.defer = deferred
    original_context = [copy.deepcopy(monitoring._context(track)) for track in song.tracks]
    devices = [track.devices for track in song.tracks]
    plan = preview(instance, path)
    assert plan["status"] == "preview" and plan["expires_in_seconds"] == 300
    assert plan["track_count"] == 2 and plan["monitoring_path"] == path
    assert all(track.writes == [] for track in song.tracks)
    assert [row["original_monitoring_state"] for row in plan["tracks"]] == [0, 2]
    plan["tracks"][0]["target_monitoring_state"] = 0  # Caller cannot alter retained intent.
    plan["tracks"][0]["routing"]["input_routing_type"]["identifier"] = "fake"
    assert call(instance, plan)["status"] == "applying"
    writes = [list(track.writes) for track in song.tracks]
    for _ in range(3):
        assert call(instance, plan)["replayed"] is True
    assert [track.writes for track in song.tracks] == writes
    result = finish(instance, plan)
    assert result["status"] == "applied" and result["verified"] is True
    assert [row["actual_monitoring_state"] for row in result["tracks"]] == [target, target]
    assert [track._state for track in song.tracks] == [target, target]
    assert call(instance, plan, "restore")["status"] == "restoring"
    assert call(instance, plan, "restore")["replayed"] is True
    result = finish(instance, plan, "restore")
    assert result["status"] == "restored" and result["verified"] is True
    assert [track._state for track in song.tracks] == [0, 2]
    writes = [list(track.writes) for track in song.tracks]
    assert call(instance, plan)["status"] == "restored"  # Apply cannot reactivate a restored plan.
    assert call(instance, plan, "restore")["replayed"] is True
    assert [track.writes for track in song.tracks] == writes
    assert [monitoring._context(track) for track in song.tracks] == original_context
    assert [track.devices for track in song.tracks] == devices
    assert song.undo_started == song.undo_finished


@pytest.mark.parametrize("handles", [None, [], [None], [False], [[]], ["missing"], ["x"] * 33])
def test_invalid_handles_fail_before_writes(handles):
    instance, song = setup()
    targets = instance._get_edit_targets()
    with pytest.raises(ValueError):
        monitoring.configure(instance, "preview", targets["session_id"], handles, "live")
    assert all(not track.writes for track in song.tracks)


def test_duplicate_return_and_replaced_handles_rejected():
    instance, song = setup()
    song.return_tracks = [AudioTrack("Return")]
    targets = instance._get_edit_targets()
    handle = targets["tracks"][0]["track_handle"]
    for handles in ([handle, handle], [targets["tracks"][-1]["track_handle"]]):
        with pytest.raises(ValueError):
            preview(instance, handles=handles)
    song.tracks[0] = AudioTrack("Same name")
    with pytest.raises(ValueError):
        preview(instance, handles=[handle])


@pytest.mark.parametrize("field,value", [("is_foldable", True), ("is_frozen", True),
    ("has_audio_input", False), ("has_audio_input", None), ("is_frozen", "false"),
    ("arm", None), ("_state", True), ("_state", -1), ("_state", 3), ("_state", 1.0)])
def test_unsafe_track_shape_preflighted(field, value):
    instance, song = setup()
    setattr(song.tracks[-1], field, value)
    with pytest.raises(ValueError):
        preview(instance)
    assert all(not track.writes for track in song.tracks)


@pytest.mark.parametrize("stage", ["preview", "apply", "restore"])
@pytest.mark.parametrize("recording", ["record_mode", "session_record", "job", "unknown"])
def test_recording_blocks_every_operation_before_writes(stage, recording):
    instance, song = setup()
    plan = preview(instance) if stage != "preview" else None
    if stage == "restore":
        call(instance, plan)
        finish(instance, plan)
    writes = [list(track.writes) for track in song.tracks]
    if recording == "job": instance._auto_rec["active"] = True
    elif recording == "unknown": song.session_record = None
    else: setattr(song, recording, True)
    with pytest.raises(ValueError):
        preview(instance) if stage == "preview" else call(instance, plan, stage)
    assert [track.writes for track in song.tracks] == writes


@pytest.mark.parametrize("stage", ["apply", "restore"])
@pytest.mark.parametrize("change", ["monitor", "arm", "input", "output", "frozen", "group", "deleted", "replaced"])
def test_all_targets_preflight_before_apply_or_restore(stage, change):
    instance, song = setup()
    plan = preview(instance)
    if stage == "restore":
        call(instance, plan)
        finish(instance, plan)
    originals = list(song.tracks)
    track = song.tracks[-1]
    writes = [list(item.writes) for item in originals]
    if change == "monitor": track._state = 0 if stage == "restore" else 1
    elif change == "arm": track.arm = True
    elif change == "input": track.input_routing_channel = route("2")
    elif change == "output": track.output_routing_type = route("Other group")
    elif change == "frozen": track.is_frozen = True
    elif change == "group": track.is_foldable = True
    elif change == "deleted": song.tracks.pop()
    else: song.tracks[-1] = AudioTrack(track.name)
    with pytest.raises(ValueError):
        call(instance, plan, stage)
    assert [item.writes for item in originals] == writes


def test_track_rename_reorder_is_safe_on_apply_and_restore():
    instance, song = setup()
    originals = list(song.tracks)
    plan = preview(instance)
    song.tracks.reverse()
    originals[0].name = "Becky"
    call(instance, plan)
    result = finish(instance, plan)
    assert result["tracks"][0]["track_name"] == "Becky"
    song.tracks.reverse()
    originals[1].name = "Guitar"
    call(instance, plan, "restore")
    result = finish(instance, plan, "restore")
    assert result["tracks"][1]["track_name"] == "Guitar"
    assert [track._state for track in originals] == [0, 2]


@pytest.mark.parametrize("failure", ["before", "after", "silent"])
@pytest.mark.parametrize("operation", ["apply", "restore"])
def test_later_native_failure_rolls_back_only_owned_states(failure, operation):
    instance, song = setup((0, 0))
    plan = preview(instance)
    if operation == "restore":
        call(instance, plan)
        finish(instance, plan)
    # Fail only this operation's desired setter; rollback can succeed.
    bad_value = 1 if operation == "apply" else 0
    song.tracks[1].failure = lambda value, count: failure if value == bad_value else None
    assert call(instance, plan, operation)["status"] in ("applying", "restoring")
    result = finish(instance, plan, operation)
    assert result["status"] == "error" and result["rollback_verified"] is True
    assert [track._state for track in song.tracks] == ([0, 0] if operation == "apply" else [1, 1])
    writes = [list(track.writes) for track in song.tracks]
    assert call(instance, plan, operation)["replayed"] is True
    assert [track.writes for track in song.tracks] == writes


@pytest.mark.parametrize("change", ["monitor", "arm", "route", "frozen", "deleted", "replaced", "set", "record", "job", "read"])
def test_deferred_context_failure_never_overwrites_changed_targets(change):
    instance, song = setup((0, 0))
    plan = preview(instance)
    original = song.tracks[1]
    call(instance, plan)
    if change == "monitor": original._state = 2
    elif change == "arm": original.arm = True
    elif change == "route": original.output_routing_channel = route("3/4")
    elif change == "frozen": original.is_frozen = True
    elif change == "deleted": song.tracks.pop()
    elif change == "replaced": song.tracks[1] = AudioTrack(original.name)
    elif change == "set":
        replacement = type(song)()
        replacement.tracks = [AudioTrack("Another Set")]
        instance.song = lambda: replacement
    elif change == "record": song.record_mode = True
    elif change == "job": instance._auto_rec["active"] = True
    else: original.read_failure = True
    # Calling stored callbacks is read-only with respect to changed targets.
    for _ in range(3):
        tick(instance)
    retained = instance._monitoring_plans[plan["plan_id"]]["result"]
    assert retained["status"] == "partial" and retained["rollback_verified"] is False
    assert original.writes == [1]
    if change == "set":
        assert instance._song.tracks[0].writes == []
    if change == "replaced":
        assert song.tracks[1].writes == []


def test_synchronous_setter_side_effect_blocks_later_target_write():
    instance, song = setup((0, 0))
    plan = preview(instance)
    song.tracks[0].after_write = lambda track, value: setattr(song.tracks[1], "arm", True)
    call(instance, plan)
    result = finish(instance, plan)
    assert result["status"] == "partial"
    assert song.tracks[1].writes == []
    assert song.tracks[0]._state == 0


def test_rollback_write_failure_is_partial_and_retained():
    instance, song = setup((0, 0))
    plan = preview(instance)
    song.tracks[0].failure = lambda value, count: "silent" if value == 0 else None
    song.tracks[1].failure = "before"
    call(instance, plan)
    result = finish(instance, plan)
    assert result["status"] == "partial" and result["rollback_verified"] is False
    assert song.tracks[0]._state == 1
    assert call(instance, plan)["replayed"] is True


def test_late_deferred_setter_after_throw_is_detected_by_rollback_verification():
    instance, song = setup((0, 0))
    plan = preview(instance)
    song.tracks[1].defer = True
    song.tracks[1].failure = "after"
    call(instance, plan)
    result = finish(instance, plan)
    assert result["status"] == "partial" and result["rollback_verified"] is False
    assert song.tracks[1]._state == 1


def test_global_pending_lock_covers_apply_restore_and_failure_cleanup():
    instance, song = setup()
    first, second = preview(instance), preview(instance)
    call(instance, first)
    with pytest.raises(ValueError, match="pending"): preview(instance)
    with pytest.raises(ValueError, match="pending"): call(instance, second)
    finish(instance, first)
    call(instance, first, "restore")
    with pytest.raises(ValueError, match="pending"): preview(instance)
    finish(instance, first, "restore")
    song.tracks[1].failure = "after"
    call(instance, second)
    with pytest.raises(ValueError, match="pending"): preview(instance)
    finish(instance, second)
    assert preview(instance)["status"] == "preview"


def test_dropped_callback_deadline_terminates_releases_lock_and_cancels_late_callback(monkeypatch):
    instance, song = setup()
    now = [100.0]
    monkeypatch.setattr(monitoring.time, "monotonic", lambda: now[0])
    plan = preview(instance)
    call(instance, plan)
    now[0] += 31
    result = call(instance, plan)
    assert result["status"] == "partial" and result["rollback_verified"] is False
    assert "deadline" in result["message"]
    assert [track._state for track in song.tracks] == [0, 2]
    writes = [list(track.writes) for track in song.tracks]
    tick(instance)
    assert [track.writes for track in song.tracks] == writes
    assert preview(instance)["status"] == "preview"


def test_expired_preview_cannot_apply_but_applied_plan_can_restore(monkeypatch):
    instance, song = setup()
    now = [100.0]
    monkeypatch.setattr(monitoring.time, "monotonic", lambda: now[0])
    first, stale = preview(instance), preview(instance)
    call(instance, first)
    finish(instance, first)
    now[0] += 301
    with pytest.raises(ValueError, match="expired"): call(instance, stale)
    assert call(instance, first, "restore")["status"] == "restoring"
    assert finish(instance, first, "restore")["status"] == "restored"


def test_cache_eviction_is_explicit_unknown_not_unapplied():
    instance, song = setup()
    first = preview(instance)
    for _ in range(32): preview(instance)
    assert len(instance._monitoring_plans) == 32
    with pytest.raises(ValueError, match="Unknown does not prove unapplied"):
        call(instance, first)


@pytest.mark.parametrize("action,extra", [("bogus", {}), ("preview", {"plan_id": "x"}),
    ("apply", {"track_handles": []}), ("restore", {"monitoring_path": "live"}),
    ("preview", {"monitoring_path": "in"})])
def test_invalid_action_shape(action, extra):
    instance, song = setup()
    targets = instance._get_edit_targets()
    kwargs = {"track_handles": [row["track_handle"] for row in targets["tracks"]],
              "monitoring_path": "live"} if action == "preview" else {}
    kwargs.update(extra)
    with pytest.raises(ValueError):
        monitoring.configure(instance, action, targets["session_id"], **kwargs)
    assert all(not track.writes for track in song.tracks)


@pytest.mark.parametrize("stage", ["preview", "apply", "deferred"])
def test_unreadable_routing_fails_closed(stage):
    instance, song = setup()
    plan = preview(instance) if stage != "preview" else None
    if stage == "deferred":
        call(instance, plan)
    song.tracks[1].output_routing_type = None
    if stage == "deferred":
        result = finish(instance, plan)
        assert result["status"] == "partial"
        assert song.tracks[1].writes == [1]
    else:
        with pytest.raises((ValueError, AttributeError)):
            preview(instance) if stage == "preview" else call(instance, plan)
        assert all(not track.writes for track in song.tracks)


def test_scheduler_rejection_returns_partial_without_reapplying():
    instance, song = setup()
    plan = preview(instance)
    def fail_schedule(delay, callback):
        raise RuntimeError("scheduler unavailable")
    instance.schedule_message = fail_schedule
    result = call(instance, plan)
    assert result["status"] == "partial" and result["rollback_verified"] is False
    assert [track._state for track in song.tracks] == [0, 2]
    writes = [list(track.writes) for track in song.tracks]
    assert call(instance, plan)["replayed"] is True
    assert [track.writes for track in song.tracks] == writes


def test_dropped_rollback_callback_deadline_releases_pending_lock(monkeypatch):
    instance, song = setup()
    now = [100.0]
    monkeypatch.setattr(monitoring.time, "monotonic", lambda: now[0])
    plan = preview(instance)
    song.tracks[1].failure = "after"
    call(instance, plan)
    assert instance._monitoring_plans[plan["plan_id"]]["phase"] == "rollback_verification"
    now[0] += 31
    result = call(instance, plan)
    assert result["status"] == "partial" and result["rollback_verified"] is False
    assert preview(instance)["status"] == "preview"
    writes = [list(track.writes) for track in song.tracks]
    tick(instance)
    assert [track.writes for track in song.tracks] == writes


def test_restore_while_applying_and_restore_unapplied_plan_are_rejected():
    instance, song = setup()
    plan = preview(instance)
    with pytest.raises(ValueError, match="successfully applied"):
        call(instance, plan, "restore")
    call(instance, plan)
    with pytest.raises(ValueError, match="successfully applied"):
        call(instance, plan, "restore")
    assert finish(instance, plan)["status"] == "applied"


def test_changed_set_before_apply_has_no_writes():
    instance, song = setup()
    plan = preview(instance)
    replacement = type(song)()
    replacement.tracks = [AudioTrack("Replacement")]
    instance.song = lambda: replacement
    with pytest.raises(ValueError, match="Stale Set"):
        call(instance, plan)
    assert all(not track.writes for track in song.tracks + replacement.tracks)


def test_context_change_during_rollback_wait_is_not_overwritten():
    instance, song = setup((0, 0))
    plan = preview(instance)
    song.tracks[1].failure = "before"
    call(instance, plan)
    song.tracks[0]._state = 2
    writes = list(song.tracks[0].writes)
    result = finish(instance, plan)
    assert result["status"] == "partial" and song.tracks[0]._state == 2
    assert song.tracks[0].writes == writes


def test_maximum_track_count_is_bounded_and_all_verified():
    instance, song = setup((0,) * 32)
    plan = preview(instance, "direct")
    call(instance, plan)
    result = finish(instance, plan)
    assert result["status"] == "applied" and result["track_count"] == 32
    assert all(track.writes == [2] for track in song.tracks)
