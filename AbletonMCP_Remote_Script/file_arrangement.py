"""Bounded local WAV placement through operation-owned Session staging clips.

The public plan remains pending while Live settles imported warp state, source
markers and arrangement edges. The existing Session planner performs the native
copy and audio verification. This module owns only its reserved empty slots and
the exact arrangement references created in the copy tick.
"""

import copy
import os
import time
import uuid

from . import arrangement_builder as core
from . import audio_arrangement as audio
from . import audio_file_source


_EPSILON = 0.00001
_MAX_SESSION_SLOTS = 4096
PENDING_TIMEOUT_SECONDS = 30


def _slot_has_clip(slot):
    return audio._boolean(slot.has_clip, "Session slot has_clip")


def _slots(track):
    slots = tuple(track.clip_slots)
    if len(slots) > _MAX_SESSION_SLOTS:
        raise ValueError("Track exceeds the 4096-slot staging verification limit")
    return slots


def _track_current(remote, plan, entry):
    if not core._same(remote._song, plan["song"]):
        raise ValueError("File plan belongs to a different Set instance")
    track = entry["track"]
    if (not core._same(remote._target_refs.get(entry["handle"]), track)
            or not core._contains(tuple(plan["song"].tracks), track)):
        raise ValueError("File destination track was deleted or replaced")
    return track


def _slot_current(remote, plan, entry):
    slots = _slots(_track_current(remote, plan, entry))
    if entry["slot"] >= len(slots) or not core._same(slots[entry["slot"]], entry["slot_object"]):
        raise ValueError("Reserved Session staging slot was moved, deleted or replaced")
    return slots[entry["slot"]]


def _public(entry, index):
    metadata = entry["file"]
    try:
        track_name = entry["track"].name
    except Exception:
        track_name = entry["track_name"]
    return {"index": index + 1, "kind": "audio", "file_path": metadata["file_identity"]["path"],
            "destination_track_handle": entry["handle"], "destination_track_name": track_name,
            "source_slot": entry["slot"] + 1, "name": entry["name"],
            "file_duration_seconds": metadata["duration_seconds"],
            "sample_rate": metadata["sample_rate"], "sample_frames": metadata["sample_frames"],
            "channels": metadata["channels"], "file_identity": copy.deepcopy(metadata["file_identity"]),
            "source_range": copy.deepcopy(entry["source_range"]),
            "destination_beat": entry["start"], "end_beat": entry["end"],
            "reserved_end_beat": entry["reserved_end"], "length_beats": entry["end"] - entry["start"]}


def _range(source_range, duration):
    if source_range is None:
        source_range = {"start": 0.0, "end": duration, "units": "seconds"}
    if (not isinstance(source_range, dict) or set(source_range) != {"start", "end", "units"}
            or source_range["units"] != "seconds"):
        raise ValueError("File source_range requires start, end and units='seconds'")
    start = core._number(source_range["start"], "source_range.start")
    end = core._number(source_range["end"], "source_range.end", positive=True)
    if end <= start or end > duration:
        raise ValueError("File source_range must have positive length within the file duration")
    return {"start": start, "end": end, "units": "seconds"}


def preview(remote, session_id, placements, plans):
    core._check_pending(plans)
    if not isinstance(placements, list) or not 1 <= len(placements) <= core.MAX_PLACEMENTS:
        raise ValueError("placements must contain 1 to 64 entries")
    targets = core._targets(remote, session_id)
    song = remote._song
    core._check_recording(remote, song)
    tempo = audio._tempo_context(song)
    rows = {row["track_handle"]: row for row in targets["tracks"]}
    entries = []
    required = {"file_path", "destination_track_handle", "destination_beat"}
    for placement in placements:
        if (not isinstance(placement, dict) or not required <= set(placement)
                or set(placement) - required - {"source_range", "name"}):
            raise ValueError("File plans accept only file_path, destination_track_handle, destination_beat, "
                             "source_range and name; mixed Session/file plans are not supported")
        handle = placement["destination_track_handle"]
        if (not isinstance(handle, str) or handle not in rows or rows[handle]["kind"] != "track"
                or not core._contains(tuple(song.tracks), remote._target_refs.get(handle))):
            raise ValueError("File placement requires a current regular audio track handle")
        track = remote._target_refs[handle]
        core._eligible(track)
        core._destination_kind(track, "audio")
        metadata = audio_file_source.probe_file(placement["file_path"])
        duration = core._number(metadata["duration_seconds"], "file duration", positive=True)
        requested = _range(placement.get("source_range"), duration)
        name = placement.get("name", os.path.splitext(os.path.basename(metadata["file_identity"]["path"]))[0])
        if not isinstance(name, str) or not name.strip() or len(name) > 1024:
            raise ValueError("name must be nonempty text of at most 1024 characters")
        reserved = [entry["slot_object"] for entry in entries if core._same(entry["track"], track)]
        available = [(index, slot) for index, slot in enumerate(_slots(track))
                     if not _slot_has_clip(slot) and not core._contains(reserved, slot)]
        if not available:
            raise ValueError("Each file placement requires a separate empty Session slot on its destination track")
        index, slot = available[0]
        if not callable(getattr(slot, "create_audio_clip", None)) or not callable(getattr(slot, "delete_clip", None)):
            raise ValueError("Reserved Session slot lacks audio import or cleanup capability")
        start = core._number(placement["destination_beat"], "destination_beat")
        factor = tempo["tempo"] / 60.0
        end = core._number(start + (requested["end"] - requested["start"]) * factor,
                           "placement end", positive=True)
        reserved_end = core._number(start + duration * factor, "reserved end", positive=True)
        entries.append({"handle": handle, "track": track, "slot": index, "slot_object": slot,
                        "track_name": track.name,
                        "file": metadata, "source_range": requested, "name": name,
                        "start": start, "end": end, "reserved_end": reserved_end})
    core._check_ranges(entries)
    if not core._same(remote._song, song) or audio._tempo_context(song) != tempo:
        raise ValueError("Set or tempo changed during file preview")
    plan_id = str(uuid.uuid4())
    result = {"status": "preview", "plan_id": plan_id, "session_id": session_id,
              "expires_in_seconds": core.PLAN_TTL_SECONDS,
              "placements": [_public(entry, index) for index, entry in enumerate(entries)],
              "placement_count": len(entries), "source_note_count": 0, "note_count": 0,
              "warnings": ["Imports local PCM WAV files through reserved empty Session slots, then removes owned staging clips.",
                           "Uses unwarped zero-pitch audio at fixed tempo. Reserve the whole file through reserved_end_beat.",
                           "File guards use identity metadata, not a content hash. No playback, recording or file save."]}
    while len(plans) >= core.MAX_PLANS:
        plans.popitem(last=False)
    plans[plan_id] = {"kind": "file", "song": song, "session_id": session_id, "created": time.monotonic(),
                      "entries": entries, "tempo": tempo, "status": "preview", "result": result,
                      "owned": [], "initial": [], "session_initial": [], "cleanup_errors": []}
    return copy.deepcopy(result)


def _validate(remote, plan, check_ranges=True):
    core._targets(remote, plan["session_id"])
    if not core._same(remote._song, plan["song"]):
        raise ValueError("File plan belongs to a different Set instance")
    core._check_recording(remote, plan["song"])
    if audio._tempo_context(plan["song"]) != plan["tempo"]:
        raise ValueError("Tempo changed since file preview")
    for entry in plan["entries"]:
        track = _track_current(remote, plan, entry)
        core._eligible(track)
        core._destination_kind(track, "audio")
        slot = _slot_current(remote, plan, entry)
        staged = entry.get("staged")
        if staged is None or entry.get("cleaned"):
            if _slot_has_clip(slot):
                raise ValueError("Reserved empty Session slot now contains another clip")
        elif not _slot_has_clip(slot) or not core._same(slot.clip, staged) or not entry.get("path_verified"):
            raise ValueError("Owned Session staging clip was moved, deleted or replaced")
        if audio_file_source.probe_file(entry["file"]["file_identity"]["path"]) != entry["file"]:
            raise ValueError("Audio file changed since preview")
        if staged is not None and not entry.get("cleaned"):
            if audio._file_identity(staged) != entry["file"]["file_identity"]:
                raise ValueError("Staging audio file differs from preview")
    _verify_sessions(remote, plan)
    if check_ranges:
        core._check_ranges(plan["entries"])


def _verify_sessions(remote, plan):
    for entry, originals in plan["session_initial"]:
        current = _slots(_track_current(remote, plan, entry))
        if len(current) != len(originals):
            raise RuntimeError("Original Session slot structure changed")
        reserved = [item["slot_object"] for item in plan["entries"] if core._same(item["track"], entry["track"])]
        for slot, (old_slot, had_clip, old_clip) in zip(current, originals):
            if not core._same(slot, old_slot):
                raise RuntimeError("Original Session slot structure changed")
            if core._contains(reserved, slot):
                continue
            if _slot_has_clip(slot) != had_clip or (had_clip and not core._same(slot.clip, old_clip)):
                raise RuntimeError("Original Session material changed")


def _pending(plan, phase):
    plan["status"] = "applying"
    plan["result"] = {"status": "applying", "plan_id": plan["result"]["plan_id"],
                      "session_id": plan["session_id"], "placement_count": len(plan["entries"]),
                      "placements": [_public(entry, index) for index, entry in enumerate(plan["entries"])],
                      "phase": phase, "message": "Waiting for Live audio import and verification. Poll apply with this same plan_id.",
                      "replayed": False, "saved": False}


def _schedule(remote, plan, phase, callback):
    _pending(plan, phase)
    remote.schedule_message(1, lambda: _run(remote, plan, callback))


def _run(remote, plan, callback):
    if plan["status"] != "applying":
        return
    try:
        if time.monotonic() > plan["deadline"]:
            raise RuntimeError("File arrangement exceeded its 30-second pending deadline")
        callback(remote, plan)
    except Exception as exc:
        _fail(remote, plan, exc)


def _undo(remote, callback):
    song = remote._begin_note_undo()
    try:
        callback()
    finally:
        if song is not None:
            song.end_undo_step()


def _capture_staging(remote, plan, entry):
    slot = entry["slot_object"]
    if _slot_has_clip(slot):
        entry["staged"] = slot.clip
        entry["path_verified"] = False
        # Establish ownership immediately after the synchronous native import.
        # An unreadable or unexpected path is never grounds to delete that clip.
        _slot_current(remote, plan, entry)
        if audio._file_identity(entry["staged"]) != entry["file"]["file_identity"]:
            raise RuntimeError("Imported staging clip file identity differs from preview")
        entry["path_verified"] = True


def _restore_import_name(remote, plan, entry, before):
    track = _track_current(remote, plan, entry)
    after = track.name
    if after != before:
        # Both observations bracket only create_audio_clip on Live's thread.
        # Never restore a name observed during a later deferred phase.
        if track.name != after:
            raise RuntimeError("Destination track name changed during import")
        track.name = before
        if track.name != before:
            raise RuntimeError("Could not restore destination track name after import")


def _import(remote, plan):
    _validate(remote, plan)
    def mutate():
        for entry in plan["entries"]:
            slot = _slot_current(remote, plan, entry)
            if _slot_has_clip(slot):
                raise RuntimeError("Reserved Session slot became occupied before import")
            if audio_file_source.probe_file(entry["file"]["file_identity"]["path"]) != entry["file"]:
                raise RuntimeError("Audio file changed before import")
            name_before = entry["track"].name
            try:
                slot.create_audio_clip(entry["file"]["file_identity"]["path"])
            finally:
                try:
                    _capture_staging(remote, plan, entry)
                finally:
                    try:
                        _restore_import_name(remote, plan, entry, name_before)
                    except Exception as exc:
                        plan["cleanup_errors"].append("Destination track name restoration is unverified: " + str(exc))
                        raise
            if entry.get("staged") is None:
                raise RuntimeError("Audio import did not create an identifiable staging clip")
            clip = entry["staged"]
            clip.warping = False
            clip.looping = False
            clip.pitch_coarse = clip.pitch_fine = 0
            clip.gain, clip.muted, clip.name = 1.0, False, entry["name"]
    _undo(remote, mutate)
    _schedule(remote, plan, "normalize_source", _normalize)


def _normalize(remote, plan):
    _validate(remote, plan)
    def mutate():
        for entry in plan["entries"]:
            clip = entry["staged"]
            if audio._boolean(clip.warping, "staging warping"):
                raise RuntimeError("Live did not settle unwarped staging audio")
            duration = entry["file"]["duration_seconds"]
            clip.end_marker = duration
            clip.loop_end = duration
            clip.loop_start = 0.0
            clip.start_marker = 0.0
    _undo(remote, mutate)
    _schedule(remote, plan, "copy_audio", _copy)


def _source(entry, song):
    source = audio.capture_source(entry["staged"], song)
    metadata = source["metadata"]
    file = entry["file"]
    if (source["file_identity"] != file["file_identity"]
            or metadata["sample_rate"] != file["sample_rate"]
            or metadata["sample_length"] != file["sample_frames"]):
        raise RuntimeError("Imported audio sample metadata differs from the previewed file")
    expected = {"warping": False, "looping": False, "pitch_coarse": 0, "pitch_fine": 0,
                "gain": 1.0, "muted": False, "name": entry["name"], "start_marker": 0.0,
                "loop_start": 0.0, "end_marker": file["duration_seconds"], "loop_end": file["duration_seconds"]}
    if any(metadata[key] != value for key, value in expected.items()):
        raise RuntimeError("Staging audio normalization readback differs from the plan")
    prepared = audio.prepare_audio(source, entry["source_range"])
    if (prepared["reserved_length_beats"] > entry["reserved_end"] - entry["start"] + _EPSILON
            or abs(prepared["length_beats"] - (entry["end"] - entry["start"])) > _EPSILON):
        raise RuntimeError("Imported audio exceeds the previewed reservation; create a new plan with clean audio metadata")
    return source, prepared


def _copy(remote, plan):
    _validate(remote, plan)
    entries = []
    for entry in plan["entries"]:
        source, prepared = _source(entry, plan["song"])
        entries.append(dict(entry, clip=entry["staged"], source=source, audio=prepared,
                            source_track=entry["track"], source_handle=entry["handle"], variation=None))
    plan_id = plan["result"]["plan_id"]
    inner = {"song": plan["song"], "session_id": plan["session_id"], "created": time.monotonic(),
             "entries": entries, "status": "preview", "result": {"plan_id": plan_id}}
    plan["inner"] = inner
    snapshots = [(track, core._arrangement(track)) for track, _originals in plan["initial"]]
    try:
        core._apply(remote, plan["session_id"], plan_id, {plan_id: inner})
    finally:
        errors = []
        for track, before in snapshots:
            try:
                plan["owned"].extend((track, clip) for clip in core._arrangement(track)
                                      if not core._contains(before, clip))
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError("Cannot identify file arrangement copies: " + "; ".join(errors))
    if inner["status"] != "applying":
        raise RuntimeError("File arrangement copy failed: " + inner["result"].get("message", inner["status"]))
    _schedule(remote, plan, "verify_and_cleanup", _complete)


def _cleanup_staging(remote, plan):
    errors = []
    for entry in reversed(plan["entries"]):
        staged = entry.get("staged")
        if staged is None or entry.get("cleaned"):
            continue
        try:
            slot = _slot_current(remote, plan, entry)
            if not _slot_has_clip(slot):
                entry["cleaned"] = True
                continue
            if not entry.get("path_verified") or not core._same(slot.clip, staged):
                raise RuntimeError("Staging clip ownership is uncertain; leaving the Session clip untouched")
            if (staged.file_path != entry["file"]["file_identity"]["path"]
                    or os.path.realpath(staged.file_path) != entry["file"]["file_identity"]["real_path"]):
                raise RuntimeError("Staging clip now refers to another file; leaving it untouched")
            slot.delete_clip()
            if _slot_has_clip(slot):
                raise RuntimeError("Owned staging clip remains after cleanup")
            entry["cleaned"] = True
        except Exception as exc:
            errors.append(str(exc))
    return errors


def _verify_arranged(plan):
    inner = plan["inner"]
    if len(plan["owned"]) != len(plan["entries"]):
        raise RuntimeError("Not every file arrangement copy is identifiable")
    verified = []
    for entry, inner_entry in zip(plan["entries"], inner["entries"]):
        matches = [(track, clip) for track, clip in plan["owned"]
                   if core._same(track, entry["track"])
                   and abs(core._number(clip.start_time, "placed clip start") - entry["start"]) <= _EPSILON]
        if len(matches) != 1 or core._contains(verified, matches[0][1]):
            raise RuntimeError("File arrangement copy identity is ambiguous")
        track, clip = matches[0]
        verified.append(clip)
        if not core._contains(core._arrangement(track), clip):
            raise RuntimeError("File arrangement copy was moved or deleted")
        if (abs(core._number(clip.start_time, "placed clip start") - entry["start"]) > _EPSILON
                or abs(core._number(clip.end_time, "placed clip end", positive=True) - entry["end"]) > _EPSILON):
            raise RuntimeError("File arrangement bounds differ from preview")
        audio.verify_audio(clip, inner_entry["source"], inner_entry["audio"], expected_name=entry["name"])
    for track, originals in plan["initial"]:
        core._unchanged_bounds(core._arrangement(track), originals)


def _complete(remote, plan):
    inner = plan["inner"]
    if inner["status"] != "applied":
        raise RuntimeError("File arrangement verification failed: " + inner["result"].get("message", inner["status"]))
    _validate(remote, plan, check_ranges=False)
    _verify_arranged(plan)
    def cleanup():
        errors = _cleanup_staging(remote, plan)
        if errors:
            raise RuntimeError("Staging cleanup failed: " + "; ".join(errors))
    _undo(remote, cleanup)
    _validate(remote, plan, check_ranges=False)
    _verify_arranged(plan)
    rows = []
    for index, entry in enumerate(plan["entries"]):
        row = _public(entry, index)
        row.update(status="verified", actual_start_beat=entry["start"], actual_end_beat=entry["end"],
                   actual_name=entry["name"], audio_verified=True, file_identity_verified=True,
                   staging_cleaned=True)
        rows.append(row)
    result = {"status": "applied", "plan_id": plan["result"]["plan_id"], "session_id": plan["session_id"],
              "placements": rows, "placement_count": len(rows), "staging_cleaned": True,
              "replayed": False, "saved": False}
    plan["status"], plan["result"] = "applied", result


def _fail(remote, plan, exc):
    inner = plan.get("inner")
    if inner is not None and inner["status"] == "applying":
        # Its callback can already be queued. Make it inert before releasing the
        # outer pending lock or allowing any later plan to edit Live.
        inner["status"] = "error"
    placed = plan.get("inner", {}).get("result", {}).get("placements", [])
    rollback = core._rollback(remote, plan, exc, plan["owned"], placed, plan["initial"])
    cleanup_errors = list(plan["cleanup_errors"])
    try:
        if core._same(remote._song, plan["song"]):
            _undo(remote, lambda: cleanup_errors.extend(_cleanup_staging(remote, plan)))
        else:
            cleanup_errors.extend(_cleanup_staging(remote, plan))
    except Exception as cleanup_exc:
        cleanup_errors.append("Could not complete staging cleanup undo group: " + str(cleanup_exc))
    try:
        _verify_sessions(remote, plan)
        for entry in plan["entries"]:
            if _slot_has_clip(_slot_current(remote, plan, entry)):
                raise RuntimeError("Reserved Session slot is not empty after cleanup")
    except Exception as cleanup_exc:
        cleanup_errors.append("Cannot verify Session restoration: " + str(cleanup_exc))
    inner_result = plan.get("inner", {}).get("result", {})
    if inner_result.get("status") == "partial":
        cleanup_errors.extend(inner_result.get("rollback_errors", ["Native copy rollback was unverified"]))
    rollback["rollback_errors"].extend(cleanup_errors)
    rollback["rollback_verified"] = not rollback["rollback_errors"]
    rollback["status"] = "error" if rollback["rollback_verified"] else "partial"
    rollback["staging_cleaned"] = not cleanup_errors
    rollback["placements"] = [dict(_public(entry, index),
        status="rolled_back" if rollback["rollback_verified"] else "rollback_unverified")
        for index, entry in enumerate(plan["entries"])]
    plan["status"], plan["result"] = rollback["status"], rollback
    return copy.deepcopy(rollback)


def apply(remote, session_id, plan_id, plans):
    plan = plans[plan_id]
    core._targets(remote, session_id)
    if plan["session_id"] != session_id or not core._same(remote._song, plan["song"]):
        raise ValueError("File plan belongs to a different Set instance")
    if plan["status"] in ("applying", "applied", "error", "partial"):
        if plan["status"] == "applying" and time.monotonic() > plan["deadline"]:
            _fail(remote, plan, RuntimeError("File arrangement exceeded its 30-second pending deadline"))
        result = copy.deepcopy(plan["result"])
        result["replayed"] = True
        return result
    if time.monotonic() - plan["created"] > core.PLAN_TTL_SECONDS:
        raise ValueError("Plan expired; create a new preview")
    core._check_pending(plans, current=plan)
    _validate(remote, plan)
    for entry in plan["entries"]:
        if any(core._same(track, entry["track"]) for track, _originals in plan["initial"]):
            continue
        plan["initial"].append((entry["track"], core._bounds(core._arrangement(entry["track"]))))
        originals = [(slot, _slot_has_clip(slot), slot.clip if _slot_has_clip(slot) else None)
                     for slot in _slots(entry["track"])]
        plan["session_initial"].append((entry, originals))
    try:
        plan["deadline"] = time.monotonic() + PENDING_TIMEOUT_SECONDS
        _schedule(remote, plan, "import_audio", _import)
    except Exception as exc:
        return _fail(remote, plan, exc)
    return copy.deepcopy(plan["result"])
