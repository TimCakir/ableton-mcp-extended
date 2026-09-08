"""Read-only audio source guards and trimming for operation-owned clip copies.

The arrangement builder owns planning, track identity, overlap checks, copying
and rollback. This module never writes to a source, creates clips, changes warp
mode or touches transport. File guards compare filesystem identity metadata;
they deliberately do not hash large audio files on Live's execution thread.
"""

import hashlib
import json
import math
import os
import stat


MAX_WARP_MARKERS = 4096
_TOLERANCE = 0.00001
_MARKER_FIELDS = ("start_marker", "end_marker", "loop_start", "loop_end")
_STATIC_FIELDS = ("name", "color", "muted", "warping", "warp_mode", "gain",
                  "pitch_coarse", "pitch_fine", "sample_length", "sample_rate")
_OPTIONAL_FIELDS = ("ram_mode", "signature_numerator", "signature_denominator",
                    "beats_granulation_resolution", "beats_transient_loop_mode",
                    "beats_transient_envelope", "tones_grain_size",
                    "texture_grain_size", "texture_flux", "complex_pro_formants",
                    "complex_pro_envelope")


def _number(value, name, positive=False, signed=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(name + " must be a finite number")
    if not math.isfinite(value) or (not signed and value < 0) or (positive and value <= 0):
        raise ValueError(name + " is outside the supported range")
    return float(value)


def _boolean(value, name):
    if value not in (False, True, 0, 1) or not isinstance(value, (bool, int)):
        raise ValueError(name + " must be a readable boolean")
    return bool(value)


def _equal_number(left, right):
    return abs(left - right) <= _TOLERANCE


def _read(clip, name):
    try:
        return getattr(clip, name)
    except Exception as exc:
        raise ValueError("Audio property " + name + " cannot be verified: " + str(exc))


def _metadata(clip):
    result = {name: _read(clip, name) for name in _STATIC_FIELDS + _MARKER_FIELDS + ("length", "looping")}
    for name in _OPTIONAL_FIELDS:
        try:
            result[name] = getattr(clip, name)
        except AttributeError:
            continue
        except Exception as exc:
            raise ValueError("Audio property " + name + " cannot be verified: " + str(exc))
    for name in ("muted", "warping", "looping"):
        result[name] = _boolean(result[name], name)
    for name in _MARKER_FIELDS:
        _number(result[name], name)
    for name in ("length", "sample_length", "sample_rate"):
        _number(result[name], name, positive=True)
    _number(result["gain"], "gain")
    for name in ("pitch_coarse", "pitch_fine"):
        _number(result[name], name, signed=True)
    # Live's WarpMode may be an integer-like enumeration rather than an int.
    if isinstance(result["warp_mode"], bool):
        raise ValueError("warp_mode cannot be verified")
    try:
        result["warp_mode"] = int(result["warp_mode"])
        json.dumps(result, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Audio metadata cannot be verified: " + str(exc))
    return result


def _file_identity(clip):
    path = _read(clip, "file_path")
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        raise ValueError("Audio source must reference an existing absolute local file")
    try:
        resolved = os.path.realpath(path)
        info = os.stat(resolved)
    except OSError as exc:
        raise ValueError("Audio source file cannot be verified: " + str(exc))
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise ValueError("Audio source must reference a nonempty regular file")
    return {"path": path, "real_path": resolved, "device": info.st_dev,
            "inode": info.st_ino, "size_bytes": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def _warp_markers(clip, warped):
    if not warped:
        return []
    try:
        markers = tuple(clip.warp_markers)
        if len(markers) > MAX_WARP_MARKERS:
            raise ValueError("Audio source exceeds the 4096-warp-marker verification limit")
        result = [{"beat_time": _number(marker.beat_time, "warp marker beat", signed=True),
                   "sample_time": _number(marker.sample_time, "warp marker sample time", signed=True)}
                  for marker in markers]
    except Exception as exc:
        raise ValueError("Audio warp markers cannot be verified: " + str(exc))
    # Negative virtual markers can legitimately describe the mapping before
    # the audio begins. Preserve every exposed marker without truncation.
    return result


def _tempo_context(song):
    try:
        tempo = _number(song.tempo, "tempo", positive=True)
        automation = song.master_track.mixer_device.song_tempo.automation_state
        follower = _boolean(song.tempo_follower_enabled, "tempo_follower_enabled")
        link = _boolean(song.is_ableton_link_enabled, "is_ableton_link_enabled")
    except Exception as exc:
        raise ValueError("Unwarped audio requires verifiable fixed tempo: " + str(exc))
    if automation != 0 or follower or link:
        raise ValueError("Unwarped audio requires tempo automation, Link and tempo follower to be off")
    return {"tempo": tempo, "tempo_automation_state": 0,
            "tempo_follower_enabled": False, "is_ableton_link_enabled": False}


def capture_source(clip, song):
    """Capture immutable source evidence; callers re-capture before applying."""
    if not _boolean(_read(clip, "is_audio_clip"), "is_audio_clip"):
        raise ValueError("Audio arrangement requires a session audio clip")
    if not _boolean(_read(clip, "is_session_clip"), "is_session_clip"):
        raise ValueError("Audio arrangement requires a session audio clip")
    for name in ("has_envelopes", "has_groove", "is_recording"):
        if _boolean(_read(clip, name), name):
            raise ValueError("Audio source " + name + " must be false")
    metadata = _metadata(clip)
    start, end = metadata["loop_start"], metadata["loop_end"]
    if end <= start:
        raise ValueError("Audio source must have a positive active region")
    if metadata["start_marker"] != start or metadata["end_marker"] != end:
        raise ValueError("Audio source start/end markers must align with loop bounds")
    tempo = None
    if not metadata["warping"]:
        if metadata["looping"]:
            raise ValueError("Unwarped audio source cannot be looping")
        if metadata["pitch_coarse"] != 0 or metadata["pitch_fine"] != 0:
            raise ValueError("Pitched unwarped audio is not supported; use zero coarse/fine pitch")
        duration = metadata["sample_length"] / metadata["sample_rate"]
        if end > duration + _TOLERANCE:
            raise ValueError("Unwarped audio source markers exceed the sample duration")
        tempo = _tempo_context(song)
    source = {"kind": "audio", "metadata": metadata,
              "file_identity": _file_identity(clip),
              "warp_markers": _warp_markers(clip, metadata["warping"]),
              "tempo_context": tempo, "notes": []}
    source["fingerprint"] = hashlib.sha256(json.dumps(
        source, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()
    return source


def prepare_audio(source, source_range=None):
    """Resolve requested units and reserve every potential intermediate span."""
    metadata = source["metadata"]
    units = "beats" if metadata["warping"] else "seconds"
    if source_range is None:
        requested = {"start": metadata["loop_start"], "end": metadata["loop_end"], "units": units}
    else:
        if not isinstance(source_range, dict) or set(source_range) != {"start", "end", "units"}:
            raise ValueError("source_range requires only start, end and units")
        requested = dict(source_range)
        if requested["units"] != units:
            raise ValueError("Audio source_range units must be " + units)
    start = _number(requested["start"], "source_range.start")
    end = _number(requested["end"], "source_range.end")
    if end <= start:
        raise ValueError("Audio source_range.end must exceed start")
    if start < metadata["loop_start"] or end > metadata["loop_end"]:
        raise ValueError("Audio source_range must stay inside the source active region")
    factor = 1.0 if metadata["warping"] else source["tempo_context"]["tempo"] / 60.0
    length = _number((end - start) * factor, "Audio placement length", positive=True)
    # Live can duplicate unwarped audio using stale clip.length in beats.
    # Normalizing loop_end may subsequently extend it to the real duration.
    # Also reserve the full marker extent while source offsets are being set.
    reserved = _number(max(metadata["length"], metadata["end_marker"] * factor,
                           metadata["loop_end"] * factor, length),
                       "Audio temporary placement length", positive=True)
    return {"source_range": {"start": start, "end": end, "units": units},
            "length_beats": length, "reserved_length_beats": reserved,
            "trim": True, "tempo": None if metadata["warping"] else source["tempo_context"]["tempo"]}


def _verify_static(clip, source, expected_name=None):
    actual = _metadata(clip)
    expected = source["metadata"]
    for name in _STATIC_FIELDS + _OPTIONAL_FIELDS:
        expected_value = expected_name if name == "name" and expected_name is not None else expected.get(name)
        if (name in actual) != (name in expected) or actual.get(name) != expected_value:
            raise RuntimeError("Placed audio setting differs from preview: " + name)
    if _file_identity(clip) != source["file_identity"]:
        raise RuntimeError("Placed audio source file identity differs from preview")
    if _warp_markers(clip, actual["warping"]) != source["warp_markers"]:
        raise RuntimeError("Placed audio warp markers differ from preview")
    for name in ("has_envelopes", "has_groove", "is_recording"):
        if _boolean(_read(clip, name), name):
            raise RuntimeError("Placed audio " + name + " differs from preview")
    return actual


def _require_arrangement_audio(clip):
    if not _boolean(_read(clip, "is_audio_clip"), "is_audio_clip") or not _boolean(
            _read(clip, "is_arrangement_clip"), "is_arrangement_clip"):
        raise ValueError("Audio trimming requires an operation-owned arrangement audio copy")


def verify_audio(clip, source, prepared, expected_name=None):
    """Read-only validation suitable for a later Live tick or after undo closure.

    The builder separately verifies arrangement start/end times and retained
    source/track identity. expected_name permits its explicit copied-clip rename;
    every other captured sound setting and the normalized source range must match.
    """
    _require_arrangement_audio(clip)
    requested = prepared["source_range"]
    if prepared != prepare_audio(source, requested):
        raise ValueError("Prepared audio range differs from retained source evidence")
    actual = _verify_static(clip, source, expected_name)
    if actual["looping"]:
        raise RuntimeError("Placed audio remained looping after normalization")
    start, end = requested["start"], requested["end"]
    for name, expected in (("start_marker", start), ("loop_start", start),
                           ("end_marker", end), ("loop_end", end)):
        if not _equal_number(actual[name], expected):
            raise RuntimeError("Placed audio source bounds differ from preview: " + name)
    return {"source_range": dict(requested), "audio_verified": True,
            "file_identity_verified": True, "warp_settings_verified": True}


def apply_audio(clip, source, prepared):
    """Normalize only a newly duplicated clip; caller rolls back any exception."""
    _require_arrangement_audio(clip)
    requested = prepared["source_range"]
    # Rebuild the derivation so accidental mutation of a retained plan cannot
    # request writes beyond its reserved source region.
    if prepared != prepare_audio(source, requested):
        raise ValueError("Prepared audio range differs from retained source evidence")
    actual = _verify_static(clip, source)
    for name in ("start_marker", "end_marker", "loop_start"):
        if not _equal_number(actual[name], source["metadata"][name]):
            raise RuntimeError("Duplicated audio source bounds differ from preview: " + name)
    factor = 1.0 if source["metadata"]["warping"] else source["tempo_context"]["tempo"] / 60.0
    if max(actual[name] for name in _MARKER_FIELDS) * factor > prepared["reserved_length_beats"] + _TOLERANCE:
        raise RuntimeError("Duplicated audio markers exceed the reserved temporary footprint")
    start, end = requested["start"], requested["end"]
    # Never toggle warping: Live explicitly defers that write. Normalize the
    # full active end first because unwarped duplication can leave loop_end
    # shorter than the requested start. All intermediate extents fit the
    # reservation. Write the final end AFTER the start; Live may still defer
    # arrangement-bound recomputation. The caller requires exact bound readback
    # and can run verify_audio again after undo closure or on a later tick.
    clip.looping = False
    clip.loop_end = source["metadata"]["end_marker"]
    clip.loop_start = start
    clip.end_marker = end
    clip.loop_end = end
    return verify_audio(clip, source, prepared)
