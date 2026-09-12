"""Retained, reversible monitoring profiles; called on Live's execution thread.

Only current_monitoring_state is written. A profile does not arm tracks, select
audio ports, bypass devices, or claim a measured latency improvement. Completed
plans remain replayable/restorable until the bounded process-local cache evicts
them; the five-minute TTL limits starting a previewed apply.
"""

import copy
import time
import uuid
from collections import OrderedDict

from . import arrangement_builder as core


MAX_TRACKS = 32
MAX_PLANS = 32
PLAN_TTL_SECONDS = 300
PENDING_TIMEOUT_SECONDS = 30
_STATES = ("In", "Auto", "Off")
_PENDING = ("applying", "restoring")
_ROUTES = ("input_routing_type", "input_routing_channel",
           "output_routing_type", "output_routing_channel")


def _boolean(value, name):
    if not isinstance(value, (bool, int)) or value not in (0, 1):
        raise ValueError(name + " cannot be verified")
    return bool(value)


def _monitor(track):
    value = track.current_monitoring_state
    if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1, 2):
        raise ValueError("Track monitoring state cannot be verified")
    return value


def _eligible(track):
    if _boolean(track.is_foldable, "Group state"):
        raise ValueError("Monitoring profiles require regular audio tracks, not groups")
    if _boolean(track.is_frozen, "Frozen state"):
        raise ValueError("Frozen tracks cannot use monitoring profiles")
    if not _boolean(track.has_audio_input, "Audio input kind"):
        raise ValueError("Monitoring profiles require audio tracks")


def _context(track):
    routing = {}
    for attribute in _ROUTES:
        route = getattr(track, attribute)
        display = route.display_name
        identifier = getattr(route, "identifier", None)
        if (not isinstance(display, str) or isinstance(identifier, bool)
                or (identifier is not None and not isinstance(identifier, (str, int)))):
            raise ValueError("Track routing cannot be verified")
        routing[attribute] = {"identifier": identifier, "display_name": display}
    return {"arm": _boolean(track.arm, "Track arm state"), "routing": routing}


def _recording(remote, song):
    if (getattr(remote, "_auto_rec", None) or {}).get("active"):
        raise ValueError("Finish or cancel the active recording job before changing monitoring")
    if (_boolean(song.record_mode, "Arrangement recording state")
            or _boolean(song.session_record, "Session recording state")):
        raise ValueError("Disable arrangement/session recording before changing monitoring")


def _identity(remote, plan, entry):
    if not core._same(remote._song, plan["song"]):
        raise ValueError("Monitoring plan belongs to a different Set instance")
    track = entry["track"]
    if (not core._same(remote._target_refs.get(entry["handle"]), track)
            or not core._contains(tuple(plan["song"].tracks), track)):
        raise ValueError("Monitoring track was deleted or replaced")
    _eligible(track)
    return track


def _validate(remote, plan, states):
    core._targets(remote, plan["session_id"])
    if not core._same(remote._song, plan["song"]):
        raise ValueError("Monitoring plan belongs to a different Set instance")
    _recording(remote, plan["song"])
    for entry, expected in zip(plan["entries"], states):
        track = _identity(remote, plan, entry)
        if _context(track) != entry["context"]:
            raise ValueError("Track arm or routing changed since monitoring preview")
        accepted = expected if isinstance(expected, tuple) else (expected,)
        if _monitor(track) not in accepted:
            raise ValueError("Track monitoring changed; refusing to overwrite another change")


def _row(entry, status):
    try:
        name = entry["track"].name
    except Exception:
        name = entry["name"]
    row = {"track_handle": entry["handle"], "track_name": name,
           "original_monitoring_state": entry["original"],
           "target_monitoring_state": entry["target"],
           "original_monitoring": _STATES[entry["original"]],
           "target_monitoring": _STATES[entry["target"]], "status": status}
    row.update(copy.deepcopy(entry["context"]))
    return row


def _result(plan, status, **extra):
    result = {"status": status, "plan_id": plan["id"], "session_id": plan["session_id"],
              "monitoring_path": plan["path"], "track_count": len(plan["entries"]),
              "tracks": [_row(entry, status) for entry in plan["entries"]],
              "replayed": False, "saved": False}
    result.update(extra)
    plan["status"], plan["result"] = status, result
    return result


def _undo(remote, callback):
    song = remote._begin_note_undo()
    try:
        callback()
    finally:
        if song is not None:
            song.end_undo_step()


def _preview(remote, session_id, handles, path, plans):
    if path not in ("live", "direct"):
        raise ValueError("monitoring_path must be live or direct")
    if (not isinstance(handles, list) or not 1 <= len(handles) <= MAX_TRACKS
            or any(not isinstance(handle, str) or not handle for handle in handles)
            or len(set(handles)) != len(handles)):
        raise ValueError("track_handles must contain 1 to 32 unique current handles")
    targets = core._targets(remote, session_id)
    song = remote._song
    _recording(remote, song)
    regular = {row["track_handle"] for row in targets["tracks"] if row["kind"] == "track"}
    entries = []
    for handle in handles:
        track = remote._target_refs.get(handle)
        if handle not in regular or track is None or not core._contains(tuple(song.tracks), track):
            raise ValueError("Monitoring profiles require current regular audio track handles")
        _eligible(track)
        entries.append({"handle": handle, "track": track, "name": track.name,
                        "original": _monitor(track), "target": 1 if path == "live" else 2,
                        "context": _context(track)})
    plan = {"id": str(uuid.uuid4()), "session_id": session_id, "song": song,
            "created": time.monotonic(), "entries": entries, "path": path}
    _validate(remote, plan, [entry["original"] for entry in entries])
    result = _result(plan, "preview", expires_in_seconds=PLAN_TTL_SECONDS,
                     warnings=["Live uses Monitor Auto; it hears input only when the track is armed. Arming is unchanged.",
                               "Direct uses Monitor Off; arrange Apollo or other hardware monitoring separately.",
                               "This changes monitoring only, without measuring or compensating latency."])
    while len(plans) >= MAX_PLANS:
        plans.popitem(last=False)
    plans[plan["id"]] = plan
    return copy.deepcopy(result)


def _pending_result(plan, phase):
    return _result(plan, "applying" if plan["operation"] == "apply" else "restoring",
                   operation=plan["operation"], phase=phase,
                   message="Waiting for Live monitoring readback. Poll the same action and plan_id.")


def _verify_rollback(remote, plan, extra_errors=()):
    if plan["status"] not in _PENDING:
        return
    errors = list(plan["rollback_errors"]) + list(extra_errors)
    try:
        _validate(remote, plan, plan["before"])
    except Exception as exc:
        errors.append("Cannot verify the pre-operation monitoring context: " + str(exc))
    result = _result(plan, "partial" if errors else "error", operation=plan["operation"],
                     message=plan["failure"], rollback_verified=not errors, rollback_errors=errors)
    for row in result["tracks"]:
        row["status"] = "rollback_unverified" if errors else "rolled_back"


def _fail(remote, plan, exc):
    """Undo only this operation's writes when identity, context and value agree."""
    if plan["status"] not in _PENDING:
        return
    if plan.get("phase") == "rollback_verification":
        _verify_rollback(remote, plan, [str(exc)])
        return
    plan["failure"], plan["rollback_errors"] = str(exc), []
    errors = plan["rollback_errors"]
    def rollback():
        for index in reversed(plan["attempted"]):
            entry = plan["entries"][index]
            try:
                core._targets(remote, plan["session_id"])
                track = _identity(remote, plan, entry)
                current = _monitor(track)
                if current == plan["before"][index]:
                    continue
                _recording(remote, plan["song"])
                if _context(track) != entry["context"] or current != plan["after"][index]:
                    raise RuntimeError("Monitoring ownership or routing context changed; leaving track untouched")
                track.current_monitoring_state = plan["before"][index]
            except Exception as rollback_exc:
                errors.append(str(rollback_exc))
    try:
        # Do not open an undo step in a replacement Set or while recording.
        core._targets(remote, plan["session_id"])
        if not core._same(remote._song, plan["song"]):
            raise RuntimeError("Original Set is no longer available for monitoring rollback")
        _recording(remote, plan["song"])
        _undo(remote, rollback)
    except Exception as rollback_exc:
        errors.append(str(rollback_exc))
    plan["phase"] = "rollback_verification"
    _pending_result(plan, "rollback_verification")
    if time.monotonic() > plan["deadline"]:
        _verify_rollback(remote, plan, ["Pending deadline expired; deferred rollback readback is unavailable"])
        return
    try:
        remote.schedule_message(1, lambda: _run(remote, plan, rollback=True))
    except Exception as schedule_exc:
        _verify_rollback(remote, plan, ["Cannot schedule rollback readback: " + str(schedule_exc)])


def _run(remote, plan, rollback=False):
    if plan["status"] not in _PENDING:
        return
    if rollback:
        _verify_rollback(remote, plan)
        return
    # A previously queued forward verifier must be inert after rollback starts.
    if plan.get("phase") == "rollback_verification":
        return
    try:
        if time.monotonic() > plan["deadline"]:
            raise RuntimeError("Monitoring operation exceeded its 30-second pending deadline")
        _validate(remote, plan, plan["after"])
        status = "applied" if plan["operation"] == "apply" else "restored"
        result = _result(plan, status, operation=plan["operation"], verified=True)
        for row, value in zip(result["tracks"], plan["after"]):
            row["actual_monitoring_state"] = value
    except Exception as exc:
        _fail(remote, plan, exc)


def _start(remote, plan, action):
    before = [entry["original"] if action == "apply" else entry["target"] for entry in plan["entries"]]
    after = [entry["target"] if action == "apply" else entry["original"] for entry in plan["entries"]]
    _validate(remote, plan, before)
    plan.update(operation=action, before=before, after=after, attempted=[], phase="verify",
                deadline=time.monotonic() + PENDING_TIMEOUT_SECONDS)
    _pending_result(plan, "verify")
    try:
        def mutate():
            for index, entry in enumerate(plan["entries"]):
                # Recheck all not-yet-written targets before each native setter.
                _validate(remote, plan, [(before[i], after[i]) if i < index else before[i]
                                         for i in range(len(before))])
                if before[index] == after[index]:
                    continue
                plan["attempted"].append(index)
                entry["track"].current_monitoring_state = after[index]
        _undo(remote, mutate)
        remote.schedule_message(1, lambda: _run(remote, plan))
    except Exception as exc:
        _fail(remote, plan, exc)
    return copy.deepcopy(plan["result"])


def configure(remote, action, session_id, track_handles=None, monitoring_path=None, plan_id=""):
    """Preview/apply/restore a profile; polling a retained action never reapplies it."""
    if action not in ("preview", "apply", "restore"):
        raise ValueError("action must be preview, apply or restore")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id is required")
    plans = getattr(remote, "_monitoring_plans", None)
    if plans is None:
        plans = remote._monitoring_plans = OrderedDict()
    for pending in tuple(plans.values()):
        if pending["status"] in _PENDING and time.monotonic() > pending["deadline"]:
            _fail(remote, pending, RuntimeError("Monitoring operation exceeded its 30-second pending deadline"))
    if action == "preview":
        if plan_id:
            raise ValueError("Preview does not accept plan_id")
        if any(plan["status"] in _PENDING for plan in plans.values()):
            raise ValueError("Another monitoring plan is pending; poll its original action")
        return _preview(remote, session_id, track_handles, monitoring_path, plans)
    if track_handles is not None or monitoring_path is not None:
        raise ValueError("Apply/restore accepts the retained plan_id, not replacement profile fields")
    if not isinstance(plan_id, str) or not plan_id or plan_id not in plans:
        raise ValueError("Unknown or evicted monitoring plan. Unknown does not prove unapplied.")
    plan = plans[plan_id]
    if session_id != plan["session_id"]:
        raise ValueError("Monitoring plan belongs to a different Set identity")
    core._targets(remote, session_id)
    if not core._same(remote._song, plan["song"]):
        raise ValueError("Monitoring plan belongs to a different Set instance")
    if ((action == "apply" and plan["status"] != "preview")
            or (action == "restore" and plan["status"] in ("restoring", "restored", "error", "partial"))):
        result = copy.deepcopy(plan["result"])
        result["replayed"] = True
        return result
    if any(other is not plan and other["status"] in _PENDING for other in plans.values()):
        raise ValueError("Another monitoring plan is pending; poll its original action")
    if action == "restore" and plan["status"] != "applied":
        raise ValueError("Restore requires a successfully applied monitoring plan")
    if action == "apply" and time.monotonic() - plan["created"] > PLAN_TTL_SECONDS:
        raise ValueError("Monitoring preview expired; create a new preview")
    return _start(remote, plan, action)
