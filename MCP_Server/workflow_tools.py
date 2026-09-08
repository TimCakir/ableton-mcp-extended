"""Small workflow tools built on explicit identities and structured outcomes."""

import math
from typing import Any, Literal

from MCP_Server.audio_analysis import analyze_audio_file as analyze_file
from MCP_Server.timing_analysis import analyze_recording_timing as analyze_timing
from MCP_Server.workflow_models import (
    ArrangementBuild, AudioAnalysis, CommandStatus, EditTargets, ExportVerification, SessionSnapshot,
    SnapshotComparison, TrackEdit, LatencyReport, MonitoringSetup, RecordingTiming,
)


def compare_snapshots(before: dict, after: dict) -> dict:
    if before.get("schema_version") != 1 or after.get("schema_version") != 1:
        raise ValueError("Both snapshots must use schema_version 1")
    if not before.get("session_id") or before.get("session_id") != after.get("session_id"):
        raise ValueError("Snapshots belong to different Set instances; handles cannot be compared")
    left = {r["track_handle"]: r for r in before.get("tracks", [])}
    right = {r["track_handle"]: r for r in after.get("tracks", [])}
    changed = []
    for handle in left.keys() & right.keys():
        fields = {key: {"before": left[handle].get(key), "after": right[handle].get(key)}
                  for key in sorted(left[handle].keys() | right[handle].keys())
                  if left[handle].get(key) != right[handle].get(key)}
        if fields:
            changed.append({"track_handle": handle, "name": right[handle]["name"], "changes": fields})
    return {
        "session_id": before["session_id"],
        "complete": not any(s.get("tracks_truncated") or any(t.get("clips_truncated") or t.get("read_errors")
                         for t in s.get("tracks", [])) for s in (before, after)),
        "added": [right[k] for k in right if k not in left],
        "removed": [left[k] for k in left if k not in right],
        "changed": sorted(changed, key=lambda item: item["track_handle"]),
        "song_changes": {key: {"before": before.get(key), "after": after.get(key)}
                         for key in ("tempo", "signature_numerator", "signature_denominator", "set_name", "set_file_path")
                         if before.get(key) != after.get(key)},
    }


def prepare_track_edit(property_name: str, value: Any, send_index: int = 0) -> tuple[str, dict]:
    if property_name in ("volume", "panning", "send"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("value must be a finite number")
        lower = -1.0 if property_name == "panning" else 0.0
        if not lower <= value <= 1.0:
            raise ValueError("value is outside the supported range")
        if property_name == "send":
            if isinstance(send_index, bool) or not isinstance(send_index, int) or send_index < 1:
                raise ValueError("send_index must be 1-based")
            return "set_send", {"send_index": send_index - 1, "value": value}
        return "set_track_" + property_name, {property_name: value}
    if property_name in ("mute", "solo", "arm"):
        if not isinstance(value, bool):
            raise ValueError("value must be a boolean")
        return "set_track_state", {property_name: value}
    if property_name == "name":
        if not isinstance(value, str) or not value.strip() or len(value) > 1024:
            raise ValueError("name must be a nonempty string of at most 1024 characters")
        return "set_track_name", {"name": value}
    raise ValueError("property_name must be volume, panning, send, mute, solo, arm or name")


def register_workflow_tools(mcp, get_connection):
    @mcp.tool()
    def get_latency_report(session_id: str, track_handles: list[str] | None = None) -> LatencyReport:
        """Inspect device-reported latency, monitoring and routes in the current Set.

        First get_edit_targets for current session_id and stable handles. Omit handles
        for all tracks, or choose regular tracks; their parent groups, all returns and
        Main are included. Nested racks are inventoried with explicit errors/limits.
        Top-level device-chain totals include inactive devices, whose latency Live
        retains. Child latencies are not added again. These are device reports, not
        measured total monitoring or hardware round-trip latency. Buffer, driver,
        compensation preferences and hardware clock settings remain manual/unknown.
        Reads on Live's execution thread. Does not change or play the Set.
        """
        return get_connection().send_command("get_latency_report", {
            "session_id": session_id, "track_handles": track_handles})

    @mcp.tool()
    def configure_monitoring(action: Literal["preview", "apply", "restore"], session_id: str,
                             track_handles: list[str] | None = None,
                             monitoring_path: Literal["live", "direct"] | None = None,
                             plan_id: str = "") -> MonitoringSetup:
        """Preview, apply and restore monitoring for 1..32 regular audio tracks.

        Preview requires current get_edit_targets session_id/track_handles and a path:
        live sets Monitor Auto (armed tracks monitor through Live); direct sets Off
        for an already-configured interface/Console monitoring path. This controls
        Live only; verify the external feed separately. It does not arm, route, save,
        adjust gain, bypass devices or change buffer/clock/latency preferences.
        Apply within five minutes using only the returned session_id and plan_id.
        Poll apply with that same plan while status=applying; repeated terminal calls
        replay without writes. Restore uses the same plan and polls while restoring.
        Restore is retained until script restart or eviction from a 32-plan cache.
        Writes reject recording, stale Set/track identities, frozen targets and changed
        monitor/arm/routing context; all tracks are preflighted before any write.
        Next-tick readback is required for applied/restored. Pending deadline is 30s;
        callbacks and polling attempt guarded rollback on failure. partial means
        rollback could not be verified. Changes made since apply are not overwritten.
        """
        return get_connection().send_command("configure_monitoring", {
            "action": action, "session_id": session_id, "track_handles": track_handles,
            "monitoring_path": monitoring_path, "plan_id": plan_id})

    @mcp.tool()
    def analyze_recording_timing(path: str, expected_onsets_seconds: list[float],
                                 search_window_ms: float = 100.0, threshold_dbfs: float = -40.0,
                                 min_separation_ms: float = 50.0, channel: int = 1,
                                 skip_initial: int = 2) -> RecordingTiming:
        """Measure isolated recorded test hits against supplied file-relative times.

        Local finished uncompressed PCM WAV, 8/16/24/32-bit, 8..192 kHz, 1..64 channels;
        selects one 1-based channel without resampling. Supply 3..400 increasing times
        whose symmetric search windows do not overlap. Final window must fit the file
        and first 120s; at most 12 million frames are scanned. No optional dependencies.
        Detects absolute-amplitude threshold edges after continuous below-threshold
        min_separation_ms. Use isolated hits with silence, not a musical full mix.
        Missing/ambiguous/boundary hits are reported and excluded, as are skip_initial
        startup hits (default 2). At least 3 accepted hits are required for statistics.
        Positive median offset means a late recorded edge; jitter reports sample SD
        and peak-to-peak spread. Attack/noise/threshold affect results. This measures
        recorded alignment, not heard monitoring or total hardware round-trip latency,
        and does not suggest a universal clock/track-delay correction or change Live.
        """
        return analyze_timing(path, expected_onsets_seconds, search_window_ms, threshold_dbfs,
                              min_separation_ms, channel, skip_initial)

    @mcp.tool()
    def build_arrangement(action: Literal["preview", "apply"], session_id: str,
                          placements: list[dict[str, Any]] | None = None,
                          plan_id: str = "") -> ArrangementBuild:
        """Preview then place Session clips or local PCM WAV files in arrangement.

        First call get_edit_targets. Preview requires its session_id and 1..64
        placements. Session-clip placements contain track_handle, source_slot (1-based) and
        destination_beat (absolute zero-based quarter-note beats, fractions allowed).
        track_handle identifies the source track. Optional destination_track_handle
        identifies the receiving track; omit it to copy onto the source track.
        Both must be current regular tracks, not group, return or frozen tracks.
        MIDI requires a MIDI destination; audio requires an audio destination.
        Preview/result track_handle and track_name still describe the source;
        destination_track_handle and destination_track_name describe the receiver.
        Only the receiving track's arrangement range must be empty. Its existing
        instruments, devices and routing are used; this copies clips, not tracks.
        Optional name labels the new copy. MIDI copies use full source length;
        optional variation accepts remove_pitches (0..127), keep_every (1..10000),
        keep_offset (zero-based, below keep_every), transpose (-127..127), and
        thin_by ("note" by default, or "onset" for whole simultaneous groups).
        Pitches are removed first, surviving notes are sorted by onset then
        pitch/values, thinning keeps every Nth note or onset group, and transpose
        follows. Onset groups share exactly the same start_time after pitch removal;
        no timing tolerance or quantization is applied. Offset counts those groups.
        Preview includes exact resulting MIDI note values. Original notes are untouched.

        Audio optionally takes source_range={start, end, units}, using beats for
        warped audio or seconds for unwarped. Ranges must stay within aligned source
        start/loop/end markers. Copies become nonlooping. The destination must be
        empty through reserved_end_beat, including temporary copy/trim space.
        Unwarped audio requires unpitched clips and fixed, unautomated tempo with
        Link and tempo follower disabled. Audio files are guarded by file identity
        metadata, not a byte hash. Warping, gain, pitch and warp markers are preserved.
        Overlaps, clip envelopes and unverifiable sources are rejected.
        Maximum 10,000 source MIDI notes across the plan.

        Apply uses the returned plan_id and session_id, with placements omitted.
        Plans containing unwarped audio return status="applying" while Live settles
        their bounds. Poll apply with the same plan_id until applied/error/partial;
        polling never creates more copies. New plans are blocked during this step.
        Plans expire after five minutes. Apply rechecks both track identities, source metadata,
        exposed MIDI note values and destination ranges on Live's execution tick.
        Reapplying a retained completed plan returns its result without new copies.
        Per-note expression is not inspected. This does not start playback or save
        the Set. A partial result means rollback could not be verified.

        File plans use a separate placement form: file_path (absolute local PCM WAV),
        destination_track_handle, destination_beat, optional name and optional
        source_range={start, end, units:"seconds"}. Do not mix file placements with
        Session-clip placements in one plan. Supports mono/stereo 8/16/24/32-bit
        uncompressed PCM WAV; other encodings/formats fail explicitly.
        File copies are unwarped, nonlooping, unpitched, unmuted and at unity gain.
        Fixed tempo is required, with Link, tempo follower and tempo automation off.
        Preview reads bounded file metadata without changing Live; apply rechecks
        filesystem identity, sample metadata, Set/track identity and destination.
        Files are guarded by filesystem metadata, not a cryptographic byte hash.
        Each file needs an empty Session slot on its destination for temporary
        import. Preview reserves the entire file duration in arrangement beats,
        even for an excerpt. If Live's settled staging clip needs more space,
        the operation fails without enlarging that reservation.
        File plans return applying while staging, copying and cleanup finish.
        Poll the same plan until terminal status; applied includes verified removal
        of temporary Session clips. Existing clips are never used as staging slots.
        Deferred callbacks and apply polls check a 30-second pending deadline;
        expiry attempts owned-clip cleanup and returns error or partial.
        """
        return get_connection().send_command("build_arrangement", {
            "action": action, "session_id": session_id,
            "placements": placements, "plan_id": plan_id})

    @mcp.tool()
    def get_command_status(request_id: str) -> CommandStatus:
        """Reconcile an ambiguous request by ID. Unknown does not mean unapplied; never automatically replay it."""
        return get_connection().send_command("get_command_status", {"request_id": request_id})

    @mcp.tool()
    def get_edit_targets() -> EditTargets:
        """Get stable track handles and Set identity for edit_track. Handles survive reordering but expire on deletion, Set change or script restart."""
        return get_connection().send_command("get_edit_targets", {})

    @mcp.tool()
    def edit_track(session_id: str, track_handle: str, property_name: str,
                   value: Any, send_index: int = 0, expected_revision: str = "",
                   preview: bool = True) -> TrackEdit:
        """Preview or apply one track edit by stable handle. Default previews. Use returned Set/handle and preview=False to apply.

        volume/send use 0..1; panning -1..1; mute/solo/arm are booleans; name is text.
        send_index is 1-based. expected_revision optionally rejects any track reorder/rename since discovery.
        Applying resolves the target on Live's execution tick. It reports application, not saved-file persistence.
        """
        command, params = prepare_track_edit(property_name, value, send_index)
        if not session_id or not track_handle:
            raise ValueError("session_id and track_handle are required")
        connection = get_connection()
        targets = connection.send_command("get_edit_targets", {})
        if targets["session_id"] != session_id:
            raise ValueError("Stale Set identity; refresh get_edit_targets")
        matches = [t for t in targets["tracks"] if t["track_handle"] == track_handle]
        if not matches:
            raise ValueError("Track handle no longer exists")
        if expected_revision and targets["revision"] != expected_revision:
            raise ValueError("Track structure changed since preview")
        params.update(session_id=session_id, track_handle=track_handle)
        if expected_revision:
            params["expected_revision"] = expected_revision
        out = {"status": "preview" if preview else "applied", "target": matches[0],
               "session_id": session_id, "revision": targets["revision"],
               "requested": {"property": property_name, "value": value}}
        if not preview:
            out["result"] = connection.send_command(command, params)
        return out

    @mcp.tool()
    def get_session_snapshot() -> SessionSnapshot:
        """Read structured track/mixer/routing/device/clip metadata for comparison. Explicit truncation/errors mean incomplete; this is not a Set backup."""
        return get_connection().send_command("get_session_snapshot", {})

    @mcp.tool()
    def compare_session_snapshots(before: dict, after: dict) -> SnapshotComparison:
        """Compare two snapshots from the same loaded Set, using stable handles; no Live changes."""
        return compare_snapshots(before, after)

    @mcp.tool()
    def analyze_audio_file(path: str, analysis_seconds: float = 60.0,
                           silence_threshold_dbfs: float = -80.0) -> AudioAnalysis:
        """Measure a local audio file's duration, channels, sample rate, sample peak and RMS via FFmpeg.

        Reads up to analysis_seconds (greater than zero, at most 600); reports whether the full file was analyzed.
        Low level is a flag for listening, not proof of an erroneous export. No Live changes.
        """
        return analyze_file(path, analysis_seconds, silence_threshold_dbfs)

    @mcp.tool()
    def verify_export_outputs(analysis_seconds: float = 60.0) -> ExportVerification:
        """Read the latest recording manifest and measure every available output file. Reports missing, silent and partial outputs; never starts playback."""
        operation = get_connection().send_command("get_automation_record_status", {})
        outputs = operation.get("outputs") or []
        verified = []
        for output in outputs:
            record = dict(output)
            path = record.get("file_path")
            if operation.get("active"):
                record["analysis_status"] = "recording_active"
            elif not path:
                record["analysis_status"] = "missing_file"
            else:
                try:
                    record["audio_analysis"] = analyze_file(path, analysis_seconds)
                    record["analysis_status"] = "measured"
                except Exception as exc:
                    record["analysis_status"] = "error"
                    record["analysis_error"] = str(exc)
            verified.append(record)
        return {"operation_id": operation.get("operation_id"), "recording_status": operation.get("status"),
                "outputs": verified, "all_files_measured": bool(verified) and all(
                    item["analysis_status"] == "measured" for item in verified)}
