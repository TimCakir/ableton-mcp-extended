"""Bounded request execution and stable targeting without a running Live instance."""

import json
import sys
import threading
import types

import pytest

framework = types.ModuleType("_Framework")
surface = types.ModuleType("_Framework.ControlSurface")
surface.ControlSurface = type("ControlSurface", (), {})
sys.modules.setdefault("_Framework", framework)
sys.modules.setdefault("_Framework.ControlSurface", surface)
from AbletonMCP_Remote_Script import AbletonMCP


def script():
    remote = AbletonMCP.__new__(AbletonMCP)
    remote.log_message = lambda message: None
    remote.running = True
    remote._closing = False
    scheduled = []
    remote.schedule_message = lambda delay, fn: scheduled.append(fn)
    return remote, scheduled


def test_expired_mutation_never_executes():
    remote, scheduled = script()
    mutations = []
    remote._create_midi_track = lambda index: mutations.append(index)
    result = remote._process_command({"id": "expired", "type": "create_midi_track",
                                      "timeout_seconds": 0.002, "params": {"index": -1}})
    assert result["status"] == "error"
    assert result["result"]["status"] == "expired"
    scheduled[0]()
    assert mutations == []
    assert remote._get_command_status("expired")["status"] == "expired"


def test_running_request_can_be_reconciled_after_timeout():
    remote, _ = script()
    finish = threading.Event()
    workers = []
    def create(index):
        assert finish.wait(2)
        return {"index": index}
    remote._create_midi_track = create
    def schedule(delay, callback):
        worker = threading.Thread(target=callback)
        workers.append(worker)
        worker.start()
    remote.schedule_message = schedule
    result = remote._process_command({"id": "slow", "type": "create_midi_track",
                                      "timeout_seconds": 0.01, "params": {"index": -1}})
    assert result["status"] == "pending"
    assert remote._get_command_status("slow")["status"] == "running"
    finish.set()
    workers[0].join(2)
    status = remote._get_command_status("slow")
    assert status["status"] == "completed"
    assert status["response"]["result"] == {"index": -1}
    assert status["response"]["id"] == "slow"


def test_same_request_id_is_not_replayed_and_changed_payload_is_rejected():
    remote, _ = script()
    calls = []
    remote.schedule_message = lambda delay, fn: fn()
    remote._create_midi_track = lambda index: calls.append(index) or {"index": index}
    request = {"id": "once", "type": "create_midi_track", "params": {"index": -1}}
    assert remote._process_command(request) == remote._process_command(request)
    assert calls == [-1]
    assert remote._process_command(dict(request, params={"index": 2}))["status"] == "error"


def test_scheduling_failure_never_falls_back_to_worker_thread_write():
    remote, _ = script()
    mutations = []
    remote.schedule_message = lambda *args: (_ for _ in ()).throw(AssertionError("scheduler unavailable"))
    remote._create_midi_track = lambda index: mutations.append(index)
    result = remote._process_command({"type": "create_midi_track"})
    assert result["status"] == "error"
    assert not mutations


def test_shutdown_after_recv_cannot_dispatch():
    remote, _ = script()
    remote._process_command = lambda command: pytest.fail("dispatched after shutdown")
    class Socket:
        def settimeout(self, timeout): pass
        def recv(self, size):
            remote.running = False
            return b'{"type":"create_midi_track"}\n'
        def close(self): pass
    remote._handle_client(Socket())


def test_multiple_frames_and_fragmented_unicode():
    remote, _ = script()
    calls, replies = [], []
    remote._process_command = lambda command: calls.append(command) or {"id": command["id"], "status": "success"}
    first = json.dumps({"id": "a", "type": "rename", "params": {"name": "İstanbul"}}, ensure_ascii=False).encode()
    split = first.index("İ".encode()) + 1
    chunks = iter([first[:split], first[split:] + b'\n{"id":"b","type":"read"}\n', b''])
    class Socket:
        def settimeout(self, timeout): pass
        def recv(self, size): return next(chunks)
        def sendall(self, value): replies.append(value)
        def close(self): pass
    remote._handle_client(Socket())
    assert [c["id"] for c in calls] == ["a", "b"]
    assert all(reply.endswith(b"\n") for reply in replies)
    assert calls[0]["params"]["name"] == "İstanbul"


class Track:
    def __init__(self, name): self.name = name


def target_script():
    remote, queued = script()
    song = types.SimpleNamespace(tracks=[Track("Bass"), Track("Lead")], return_tracks=[], name="Lab", file_path="Lab.als")
    remote.song = lambda: song
    return remote, queued, song


def test_handle_targets_original_track_after_insertion():
    remote, _, song = target_script()
    targets = remote._get_edit_targets()
    handle = targets["tracks"][1]["track_handle"]
    song.tracks.insert(0, Track("New"))
    params = {"session_id": targets["session_id"], "track_handle": handle}
    remote._guard_command_target(params)
    assert params["track_index"] == 2
    assert song.tracks[params["track_index"]].name == "Lead"


def test_deleted_track_and_changed_set_handles_are_rejected():
    remote, _, song = target_script()
    targets = remote._get_edit_targets()
    params = {"session_id": targets["session_id"], "track_handle": targets["tracks"][0]["track_handle"]}
    song.tracks.pop(0)
    with pytest.raises(ValueError, match="deleted"):
        remote._guard_command_target(params)
    remote.song = lambda: types.SimpleNamespace(tracks=[Track("Bass")], return_tracks=[], name="Other", file_path="Other.als")
    with pytest.raises(ValueError, match="Stale Set"):
        remote._guard_command_target(params)


def test_expected_revision_rejects_structure_change():
    remote, _, song = target_script()
    targets = remote._get_edit_targets()
    song.tracks.reverse()
    with pytest.raises(ValueError, match="structure changed"):
        remote._guard_command_target({"session_id": targets["session_id"], "expected_revision": targets["revision"]})


def test_malformed_batch_is_rejected_before_any_edit():
    remote, _ = script()
    remote._process_command = lambda command: pytest.fail("batch started before validating envelopes")
    with pytest.raises(ValueError):
        remote._batch([{"command": "set_tempo", "params": {"tempo": 120}}, {"command": "set_tempo", "params": []}])


@pytest.mark.parametrize("stop_on_error", [True, False])
def test_batch_deadline_stops_unstarted_children(stop_on_error, monkeypatch):
    import AbletonMCP_Remote_Script as module
    remote, _ = script()
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    remote.schedule_message = lambda delay, fn: fn()
    mutations = []
    def set_tempo(tempo):
        mutations.append(tempo)
        clock[0] += 2.0
        return {"tempo": tempo}
    remote._set_tempo = set_tempo
    reply = remote._process_command({"id": "deadline-batch", "type": "batch", "timeout_seconds": 1.0,
        "params": {"stop_on_error": stop_on_error, "commands": [
            {"command": "set_tempo", "params": {"tempo": 120}},
            {"command": "set_tempo", "params": {"tempo": 130}}]}})
    result = reply["result"]
    assert mutations == [120]
    assert result["status"] == "partial"
    assert result["ran"] == result["succeeded"] == result["skipped"] == 1
    assert result["results"][1]["status"] == "skipped"
    assert result["stop_reason"] == "Batch deadline expired"
    child_id = result["results"][0]["request_id"]
    assert remote._requests[child_id]["deadline"] == remote._requests["deadline-batch"]["deadline"]


def test_batch_rejected_claim_never_starts(monkeypatch):
    import AbletonMCP_Remote_Script as module
    remote, _ = script()
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    claim = remote._claim_request
    def late_claim(state):
        clock[0] += 11
        return claim(state)
    remote._claim_request = late_claim
    remote._batch = lambda *args: pytest.fail("Unclaimed batch executed")
    reply = remote._process_command({"id": "unclaimed", "type": "batch", "params": {"commands": []}})
    assert reply["status"] == "error"
    assert remote._get_command_status("unclaimed")["status"] == "expired"


def test_shutdown_stops_batch_even_when_continuing_after_errors():
    remote, _ = script()
    remote.schedule_message = lambda delay, fn: fn()
    mutations = []
    def set_tempo(tempo):
        mutations.append(tempo)
        remote._closing = True
        return {"tempo": tempo}
    remote._set_tempo = set_tempo
    reply = remote._process_command({"type": "batch", "params": {"stop_on_error": False, "commands": [
        {"command": "set_tempo", "params": {"tempo": 120}},
        {"command": "set_tempo", "params": {"tempo": 130}}]}})
    assert mutations == [120]
    assert reply["result"]["skipped"] == 1
    assert reply["result"]["stop_reason"] == "Remote Script is shutting down"


def test_queued_batch_child_expires_with_parent_and_never_mutates():
    remote, scheduled = script()
    mutations = []
    remote._set_tempo = lambda tempo: mutations.append(tempo)
    reply = remote._process_command({"id": "queued-batch", "type": "batch", "timeout_seconds": .002,
        "params": {"stop_on_error": False, "commands": [
            {"command": "set_tempo", "params": {"tempo": 120}},
            {"command": "set_tempo", "params": {"tempo": 130}}]}})
    assert len(scheduled) == 1
    scheduled[0]()
    assert mutations == []
    assert reply["result"]["failed"] == 1
    assert reply["result"]["skipped"] == 1


@pytest.mark.parametrize("child_fails", [False, True])
def test_pending_batch_reconciles_after_running_child_finishes(child_fails):
    remote, _ = script()
    finish = threading.Event()
    workers, mutations = [], []
    def set_tempo(tempo):
        mutations.append(tempo)
        assert finish.wait(2)
        if child_fails:
            raise ValueError("Unable to set tempo")
        return {"tempo": tempo}
    remote._set_tempo = set_tempo
    def schedule(delay, callback):
        worker = threading.Thread(target=callback)
        workers.append(worker)
        worker.start()
    remote.schedule_message = schedule
    request = {"id": "pending-batch", "type": "batch", "timeout_seconds": .02,
        "params": {"stop_on_error": False, "commands": [
            {"command": "set_tempo", "params": {"tempo": 120}},
            {"command": "set_tempo", "params": {"tempo": 130}}]}}
    try:
        reply = remote._process_command(request)
        assert reply["status"] == "pending"
        assert reply["result"]["request_id"] == "pending-batch"
        child_id = reply["result"]["pending_request_id"]
        assert remote._get_command_status(child_id)["status"] == "running"
        assert remote._process_command(request)["status"] == "pending"
    finally:
        finish.set()
        for worker in workers:
            worker.join(2)
    status = remote._get_command_status("pending-batch")
    assert status["status"] == "completed"
    result = status["response"]["result"]
    assert result["pending"] == 0
    assert "pending_request_id" not in result
    assert "still running" not in result.get("message", "")
    assert result["skipped"] == 1
    assert result["failed"] == int(child_fails)
    assert result["succeeded"] == int(not child_fails)
    assert result["results"][0]["status"] == ("error" if child_fails else "success")
    assert remote._process_command(request) == status["response"]
    assert mutations == [120]


def test_concurrent_target_discovery_preserves_one_handle_map():
    remote, _, song = target_script()
    first_read, second_read, release = threading.Event(), threading.Event(), threading.Event()
    reads = []
    class SlowTrack:
        @property
        def name(self):
            reads.append(threading.get_ident())
            if len(reads) == 1:
                first_read.set()
                assert release.wait(2)
            else:
                second_read.set()
            return "Bass"
    song.tracks = [SlowTrack()]
    results, errors = [], []
    def discover():
        try:
            results.append(remote._get_edit_targets())
        except Exception as exc:
            errors.append(exc)
    first = threading.Thread(target=discover)
    second = threading.Thread(target=discover)
    first.start()
    try:
        assert first_read.wait(1)
        second.start()
        assert not second_read.wait(.03), "Second discovery entered an unpublished registry"
    finally:
        release.set()
        first.join(2)
        second.join(2)
    assert not errors
    assert len(results) == 2
    assert results[0] == results[1]
    assert results[0]["tracks"][0]["track_handle"] in remote._target_refs


def test_snapshot_uses_one_song_and_rejects_mid_read_set_switch():
    remote, _, song = target_script()
    song.tempo = 120
    song.signature_numerator = song.signature_denominator = 4
    other = types.SimpleNamespace(tracks=[], return_tracks=[], name="Other", file_path="Other.als",
                                  tempo=130, signature_numerator=3, signature_denominator=4)
    reads = [song, other]
    remote.song = lambda: reads.pop(0)
    with pytest.raises(RuntimeError, match="Set changed"):
        remote._get_session_snapshot()


def test_snapshot_groups_and_returns_have_complete_empty_clip_lists():
    remote, _, song = target_script()
    song.tempo = 120
    song.signature_numerator = song.signature_denominator = 4

    class NonClipTrack:
        def __init__(self, name, group):
            self.name, self.is_foldable = name, group
            self.mixer_device = types.SimpleNamespace(
                volume=types.SimpleNamespace(value=.8), panning=types.SimpleNamespace(value=0), sends=[])
            self.devices = []

        @property
        def arrangement_clips(self):
            raise RuntimeError("Main, Group and Return Tracks have no arrangement clips")

        @property
        def clip_slots(self):
            raise RuntimeError("No clip slots")

    song.tracks = [NonClipTrack("Group", True)]
    song.return_tracks = [NonClipTrack("Return", False)]
    result = remote._get_session_snapshot()
    for track in result["tracks"]:
        assert track["clips"] == []
        assert track["clip_count"] == 0
        assert not track["clips_truncated"]
        assert track["read_errors"] == []


def test_return_track_can_be_renamed_by_stable_handle():
    remote, _, song = target_script()
    song.return_tracks = [Track("Reverb")]
    remote.schedule_message = lambda delay, fn: fn()
    targets = remote._get_edit_targets()
    reply = remote._process_command({"type": "set_track_name", "params": {
        "session_id": targets["session_id"], "track_handle": targets["tracks"][-1]["track_handle"],
        "name": "Room"}})
    assert reply["status"] == "success"
    assert song.return_tracks[0].name == "Room"


@pytest.mark.parametrize("value", [0, False])
def test_batch_lom_falsey_set_value_is_a_write(value):
    remote, _, song = target_script()
    song.tempo = True if value is False else 120
    remote.schedule_message = lambda delay, fn: fn()
    reply = remote._process_command({"type": "batch", "params": {"commands": [
        {"command": "call_lom", "params": {"path": "song", "member": "tempo", "set_value": value}}]}})
    assert reply["result"]["succeeded"] == 1
    assert song.tempo is value


def test_delete_track_requires_explicit_target():
    remote, _ = script()
    remote.schedule_message = lambda delay, fn: fn()
    remote._delete_track = lambda index: pytest.fail("Missing target defaulted to a track")
    reply = remote._process_command({"type": "delete_track", "params": {"track_name": "Bass"}})
    assert reply["status"] == "error"
    assert "explicit track_index" in reply["message"]


def test_batch_child_uses_the_full_remaining_parent_budget(monkeypatch):
    import AbletonMCP_Remote_Script as module
    remote, _ = script()
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    mutations = []
    remote._set_tempo = lambda tempo: mutations.append(tempo) or {"tempo": tempo}
    def delayed_schedule(delay, callback):
        clock[0] += 11.0
        callback()
    remote.schedule_message = delayed_schedule
    reply = remote._process_command({"id": "longer-batch", "type": "batch", "timeout_seconds": 15.0,
        "params": {"commands": [{"command": "set_tempo", "params": {"tempo": 120}}]}})
    assert reply["result"]["succeeded"] == 1
    assert mutations == [120]
