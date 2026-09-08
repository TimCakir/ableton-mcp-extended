"""Bounded read-only inventory of Live's reported device latency.

Direct device rows form a serial chain; nested chains are an inventory only.
Their latency is never added to a parent rack's already-reported latency. This
is not a measurement of audio-interface, monitoring or physical round-trip time.
All displayed collection indexes are one-based, not executable LOM paths.
"""

import math
import time


MAX_TRACKS = 256
MAX_DEVICES = 512
MAX_CHAINS = 64
MAX_TOTAL_CHAINS = 512
MAX_DEPTH = 8


def _same(left, right):
    if left is right:
        return True
    try:
        return bool(left == right)
    except Exception:
        return False


def _contains(values, target):
    return any(_same(value, target) for value in values)


def _problem(row, field, exc, unavailable=False):
    row["read_errors"].append({"field": field, "message": str(exc),
                                "unavailable": unavailable})


def _read(obj, field, row):
    try:
        value = getattr(obj, field)
        if value is None:
            _problem(row, field, "Property returned no readable value", True)
        return value
    except Exception as exc:
        _problem(row, field, exc, isinstance(exc, AttributeError))
        return None


def _boolean(obj, field, row):
    value = _read(obj, field, row)
    if value is None:
        return None
    if not isinstance(value, (bool, int)) or value not in (0, 1):
        _problem(row, field, "Expected a readable boolean")
        return None
    return bool(value)


def _latency(obj, field, row, samples=False):
    value = _read(obj, field, row)
    if value is None:
        return None
    try:
        valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                 and math.isfinite(value) and value >= 0 and (not samples or int(value) == value))
    except (OverflowError, TypeError, ValueError):
        valid = False
    if not valid:
        _problem(row, field, "Expected finite nonnegative " + ("whole samples" if samples else "milliseconds"))
        return None
    return int(value) if samples else float(value)


def _text(obj, field, row):
    value = _read(obj, field, row)
    if value is None or isinstance(value, str):
        return value
    _problem(row, field, "Expected readable text")
    return None


def _collection(obj, field, row, limit):
    """Read at most limit + 1 entries, including the truncation sentinel."""
    value = _read(obj, field, row)
    if value is None:
        return [], False
    values = []
    try:
        for item in value:
            if len(values) >= limit:
                row["truncated"] = True
                _problem(row, field, "Collection exceeds the reporting limit")
                return values, False
            values.append(item)
    except Exception as exc:
        _problem(row, field, exc)
        return values, False
    return values, True


def _context():
    return {"read_errors": [], "truncated": False}


def _device(device, path, state, ancestors, depth):
    row = dict(_context(), path=path, name=None, class_name=None,
               is_active=None, reported_latency_ms=None, reported_latency_samples=None,
               chains=[], return_chains=[])
    state["device_nodes"] += 1
    if _contains(ancestors, device):
        row["truncated"] = True
        _problem(row, "devices", "Cycle in the device/chain hierarchy")
        return row
    if depth > MAX_DEPTH:
        row["truncated"] = True
        _problem(row, "devices", "Device depth exceeded")
        return row
    state["devices_read"] += 1
    row["name"] = _text(device, "name", row)
    row["class_name"] = _text(device, "class_name", row)
    row["is_active"] = _boolean(device, "is_active", row)
    row["reported_latency_ms"] = _latency(device, "latency_in_ms", row)
    row["reported_latency_samples"] = _latency(device, "latency_in_samples", row, samples=True)
    can_have_chains = _boolean(device, "can_have_chains", row)
    if can_have_chains:
        for field in ("chains", "return_chains"):
            remaining = max(0, MAX_TOTAL_CHAINS - state["chains_read"])
            chains, _ = _collection(device, field, row, min(MAX_CHAINS, remaining))
            for index, chain in enumerate(chains):
                if state["chains_read"] >= MAX_TOTAL_CHAINS:
                    row["truncated"] = True
                    _problem(row, field, "Global chain budget exceeded")
                    break
                state["chains_read"] += 1
                chain_path = path + "/" + field + "[" + str(index + 1) + "]"
                child = dict(_context(), index=index + 1, name=None, path=chain_path, devices=[])
                child["name"] = _text(chain, "name", child)
                if _contains(ancestors + [device], chain):
                    child["truncated"] = True
                    _problem(child, "devices", "Cycle in the device/chain hierarchy")
                else:
                    child["devices"], _ = _device_list(chain, chain_path, child, state,
                                                        ancestors + [device, chain], depth + 1)
                row[field].append(child)
    return row


def _device_list(owner, path, row, state, ancestors, depth):
    remaining = max(0, MAX_DEVICES - state["device_nodes"])
    devices, complete = _collection(owner, "devices", row, remaining)
    result = []
    for index, device in enumerate(devices):
        if state["device_nodes"] >= MAX_DEVICES:
            row["truncated"] = True
            _problem(row, "devices", "Global device budget exceeded")
            complete = False
            break
        result.append(_device(device, path + "/devices[" + str(index + 1) + "]",
                              state, ancestors, depth))
    return result, complete


def _routing(track, field, row):
    route = _read(track, field, row)
    if route is None:
        return None
    try:
        name = route.display_name
        if not isinstance(name, str):
            raise ValueError("Routing display name is not text")
        return name
    except Exception as exc:
        _problem(row, field, exc, isinstance(exc, AttributeError))
        return None


def _track_row(track, target, focus, state, group_info):
    row = _context()
    row.update(target)
    row.update(focused=focus, monitoring_state=None, monitoring_value=None,
               monitoring_applicable=False, arm=None, can_be_armed=None,
               is_group=None, group_track=group_info, devices=[],
               reported_device_chain_latency_ms=None, reported_device_chain_latency_samples=None)
    row["name"] = _text(track, "name", row)
    kind = target["kind"]
    row["is_group"] = _boolean(track, "is_foldable", row) if kind == "track" else False
    if kind == "track" and row["is_group"] is False:
        row["monitoring_applicable"] = True
        value = _read(track, "current_monitoring_state", row)
        if isinstance(value, int) and not isinstance(value, bool) and value in (0, 1, 2):
            row["monitoring_value"] = value
            row["monitoring_state"] = ("in", "auto", "off")[value]
        elif value is not None:
            _problem(row, "current_monitoring_state", "Expected In, Auto or Off enum (0..2)")
        row["can_be_armed"] = _boolean(track, "can_be_armed", row)
        if row["can_be_armed"]:
            row["arm"] = _boolean(track, "arm", row)
    else:
        row["can_be_armed"] = False if kind != "track" or row["is_group"] else None
    for field in ("input_routing_type", "input_routing_channel", "output_routing_type", "output_routing_channel"):
        if field.startswith("input_") and (kind != "track" or row["is_group"]):
            row[field] = None
        else:
            row[field] = _routing(track, field, row)
    row["devices"], direct_complete = _device_list(track, target.get("track_handle") or "master",
                                                   row, state, [], 0)
    for unit in ("ms", "samples"):
        values = [device["reported_latency_" + unit] for device in row["devices"]]
        if direct_complete and all(value is not None for value in values):
            total = sum(values)
            try:
                finite = math.isfinite(total)
            except OverflowError:
                finite = False
            if finite:
                row["reported_device_chain_latency_" + unit] = total
            else:
                _problem(row, "reported_device_chain_latency_" + unit, "Direct-chain sum is not finite")
    return row


def _walk_devices(row):
    for device in row.get("devices", []):
        yield device
        for field in ("chains", "return_chains"):
            for chain in device[field]:
                for child in _walk_devices(chain):
                    yield child


def _complete(row):
    if row["read_errors"] or row["truncated"]:
        return False
    for device in row.get("devices", []):
        if not _complete(device):
            return False
    for field in ("chains", "return_chains"):
        for chain in row.get(field, []):
            if not _complete(chain):
                return False
    return True


def _truncated(row):
    return row["truncated"] or any(_truncated(child) for field in ("devices", "chains", "return_chains")
                                   for child in row.get(field, []))


def _findings(tracks, master):
    findings = []
    for row in tracks + ([master] if master is not None else []):
        handle = row.get("track_handle")
        scope = {"track_handle": handle, "track_name": row["name"], "kind": row["kind"]}
        milliseconds = row["reported_device_chain_latency_ms"]
        if milliseconds is not None and milliseconds > 0:
            findings.append(dict(scope, code="master_device_latency" if row["kind"] == "master" else "device_chain_latency",
                severity="info", reported_latency_ms=milliseconds,
                message="Devices on this track report latency. Inspect this chain when choosing a monitoring path; this is not measured round-trip latency."))
        if row["focused"] and row["monitoring_state"] in ("in", "auto"):
            findings.append(dict(scope, code="live_monitoring_enabled", severity="info",
                monitoring_state=row["monitoring_state"], hardware_direct_monitoring=None,
                message="Live input monitoring is enabled or available while armed. Check the interface's direct-monitoring path separately to avoid hearing both paths."))
        for device in _walk_devices(row):
            if device["is_active"] is False and ((device["reported_latency_ms"] or 0) > 0
                                                   or (device["reported_latency_samples"] or 0) > 0):
                findings.append(dict(scope, code="inactive_device_reports_latency", severity="info",
                    device_path=device["path"], device_name=device["name"],
                    reported_latency_ms=device["reported_latency_ms"], reported_latency_samples=device["reported_latency_samples"],
                    message="This inactive device still reports latency. Its bypass state alone does not establish that the reported latency disappeared."))
    by_handle = {row["track_handle"]: row for row in tracks}
    for row in tracks:
        parent = row["group_track"]
        if row["focused"] and parent and parent.get("track_handle") in by_handle:
            group = by_handle[parent["track_handle"]]
            milliseconds = group["reported_device_chain_latency_ms"]
            if milliseconds is not None and milliseconds > 0:
                findings.append({"code": "parent_group_reports_latency", "severity": "info",
                    "track_handle": row["track_handle"], "track_name": row["name"],
                    "group_track_handle": group["track_handle"], "group_track_name": group["name"],
                    "reported_latency_ms": milliseconds,
                    "message": "The track's actual parent group has devices reporting latency. Check its routing before interpreting this as monitoring-path latency."})
    return findings


def get_report(remote, session_id, track_handles=None):
    """Inspect requested tracks, their parent groups, all returns and Master.

    With no handle filter all regular and return tracks are requested. Bounds,
    unreadable fields and missing API properties produce an incomplete report,
    never an inferred zero. Set/track identity changes reject the whole snapshot.
    """
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id is required")
    if track_handles is not None and (not isinstance(track_handles, list)
            or not 1 <= len(track_handles) <= MAX_TRACKS
            or any(not isinstance(handle, str) or not handle for handle in track_handles)
            or len(set(track_handles)) != len(track_handles)):
        raise ValueError("track_handles must be omitted or contain 1..256 unique stable handles")
    targets = remote._get_edit_targets()
    if targets["session_id"] != session_id:
        raise ValueError("Stale Set identity; refresh get_edit_targets")
    song = remote._song
    rows = targets["tracks"]
    by_handle = {row["track_handle"]: row for row in rows}
    requested = list(by_handle) if track_handles is None else list(track_handles)
    if any(handle not in by_handle for handle in requested):
        raise ValueError("Requested track was deleted or its handle is stale")
    refs = {handle: remote._target_refs.get(handle) for handle in by_handle}
    if any(refs[handle] is None for handle in requested):
        raise ValueError("Requested track handle cannot be resolved")
    out = dict(_context(), session_id=session_id, revision=targets["revision"],
               set_name=targets.get("set_name", ""), set_file_path=targets.get("set_file_path", ""),
               captured_at=time.time(), tracks=[], master=None)
    selected = [row["track_handle"] for row in rows if row["kind"] == "return"]
    selected.extend(handle for handle in requested if handle not in selected)
    groups = {}
    index = 0
    while index < len(selected) and index < MAX_TRACKS:
        handle = selected[index]
        index += 1
        if by_handle[handle]["kind"] != "track":
            groups[handle] = None
            continue
        track = refs[handle]
        error_start = len(out["read_errors"])
        is_grouped = _boolean(track, "is_grouped", out)
        group = _read(track, "group_track", out) if is_grouped else None
        parent_handle = next((key for key, value in refs.items() if _same(value, group)), None) if group is not None else None
        if is_grouped and parent_handle is None:
            _problem(out, "group_track", "Parent group cannot be resolved to a current stable track handle")
        for error in out["read_errors"][error_start:]:
            error["track_handle"] = handle
        groups[handle] = {"track_handle": parent_handle, "name": by_handle[parent_handle]["name"]} if parent_handle else None
        if parent_handle and parent_handle not in selected:
            selected.append(parent_handle)
    if len(selected) > MAX_TRACKS:
        out["truncated"] = True
        _problem(out, "tracks", "Track count exceeds the reporting limit")
        selected = selected[:MAX_TRACKS]
    state = {"device_nodes": 0, "devices_read": 0, "chains_read": 0}
    master_track = _read(song, "master_track", out)
    if master_track is not None:
        out["master"] = _track_row(master_track, {"kind": "master", "track_handle": None,
            "track_index": None, "name": None}, False, state, None)
    for handle in selected:
        out["tracks"].append(_track_row(refs[handle], by_handle[handle], handle in requested,
                                       state, groups.get(handle)))
    current = remote._get_edit_targets()
    if (not _same(remote._song, song) or current["session_id"] != session_id
            or current["revision"] != targets["revision"]
            or any(not _same(remote._target_refs.get(handle), refs[handle]) for handle in selected)):
        raise ValueError("Set or track identity changed while reading latency; refresh the report")
    out["scope"] = {"requested_track_handles": requested, "reported_track_handles": selected,
                    "includes_master": out["master"] is not None,
                    "includes_all_returns": all(row["track_handle"] in selected for row in rows if row["kind"] == "return"),
                    "includes_parent_group_context": not any(error["field"] in ("is_grouped", "group_track")
                        for error in out["read_errors"]) and all(parent is None or parent["track_handle"] in selected
                                                               for parent in groups.values())}
    out["limits"] = {"tracks": MAX_TRACKS, "devices": MAX_DEVICES, "chains_per_device": MAX_CHAINS,
                     "chains": MAX_TOTAL_CHAINS, "depth": MAX_DEPTH,
                     "devices_read": state["devices_read"], "device_nodes": state["device_nodes"],
                     "chains_read": state["chains_read"]}
    out["findings"] = _findings(out["tracks"], out["master"])
    out["manual_settings"] = [{"setting": name, "value": None, "status": "not_observed",
        "message": "Check Live Preferences, the interface control panel or a physical calibration; this report does not read this value."}
        for name in ("audio_buffer_size", "audio_sample_rate", "driver_input_output_latency",
                     "reduced_latency_when_monitoring", "keep_monitoring_latency_in_recording",
                     "delay_compensation", "driver_error_compensation",
                     "track_delay", "hardware_direct_monitoring", "hardware_midi_clock_delay", "physical_round_trip_latency")]
    out["measurement_scope"] = "Reported latency of direct devices in each track's chain, including inactive devices. Nested devices are inventory only. No routing-path aggregate, interface or round-trip measurement."
    out["complete"] = _complete(out) and out["master"] is not None and _complete(out["master"])
    out["truncated"] = out["truncated"] or any(_truncated(row)
        for row in out["tracks"] + ([out["master"]] if out["master"] else []))
    # _complete walks device descendants; report tracks are a separate collection.
    out["complete"] = out["complete"] and all(_complete(row) for row in out["tracks"])
    out["status"] = "complete" if out["complete"] else "incomplete"
    return out
