"""Deterministic MIDI variations, applied only to operation-owned clip copies.

Preparation is pure: note IDs and Live's enumeration order do not influence the
result. Application keeps Live's native note vectors and changes only pitch;
all other exposed note values survive exactly as read.
"""

import copy
import json
import math


MAX_VARIATION_NOTES = 10000
_SPEC_FIELDS = {"remove_pitches", "keep_every", "keep_offset", "thin_by", "transpose"}


def _integer(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(name + " must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError("{0} must be between {1} and {2}".format(name, minimum, maximum))
    return value


def _number(value, name, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(name + " must be a finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(name + " must be a finite number")
    if minimum is not None and value < minimum:
        raise ValueError(name + " is below its supported range")
    if maximum is not None and value > maximum:
        raise ValueError(name + " exceeds its supported range")
    return value


def _normalise_spec(spec):
    if spec is None:
        spec = {}
    if not isinstance(spec, dict) or set(spec) - _SPEC_FIELDS:
        raise ValueError("variation accepts only remove_pitches, keep_every, keep_offset, thin_by and transpose")
    pitches = spec.get("remove_pitches", [])
    if not isinstance(pitches, list) or len(pitches) > 128:
        raise ValueError("remove_pitches must be a list of at most 128 MIDI pitches")
    pitches = sorted({_integer(pitch, "remove_pitches entry", 0, 127) for pitch in pitches})
    every = _integer(spec.get("keep_every", 1), "keep_every", 1, MAX_VARIATION_NOTES)
    offset = _integer(spec.get("keep_offset", 0), "keep_offset", 0, every - 1)
    thin_by = spec.get("thin_by", "note")
    if not isinstance(thin_by, str) or thin_by not in {"note", "onset"}:
        raise ValueError("thin_by must be 'note' or 'onset'")
    transpose = _integer(spec.get("transpose", 0), "transpose", -127, 127)
    return {"remove_pitches": pitches, "keep_every": every,
            "keep_offset": offset, "thin_by": thin_by, "transpose": transpose}


def _values(row):
    if not isinstance(row, dict):
        raise ValueError("Each note must be a serialized note object")
    result = copy.deepcopy(row)
    result.pop("note_id", None)
    required = {"pitch", "start_time", "duration", "velocity", "mute"}
    if not required <= set(result):
        raise ValueError("Source note is missing required musical values")
    _integer(result["pitch"], "note pitch", 0, 127)
    start = _number(result["start_time"], "note start_time")
    duration = _number(result["duration"], "note duration", 0)
    if duration == 0:
        raise ValueError("note duration must be positive")
    _number(start + duration, "note end_time")
    _number(result["velocity"], "note velocity", 0, 127)
    if not isinstance(result["mute"], bool):
        raise ValueError("note mute must be a boolean")
    for field, minimum, maximum in (("probability", 0, 1),
                                     ("velocity_deviation", -127, 127),
                                     ("release_velocity", 0, 127)):
        if field in result:
            _number(result[field], "note " + field, minimum, maximum)
    try:
        json.dumps(result, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Source note values cannot be verified: " + str(exc))
    return result


def _signature(rows):
    return sorted(json.dumps(_values(row), sort_keys=True, allow_nan=False) for row in rows)


def prepare_variation(rows, spec):
    """Return exact note values and input indexes without changing either input.

    Pitches are removed first. Remaining notes are ordered by onset, pitch,
    duration and all other exposed values. keep_every/keep_offset counts either
    individual notes (thin_by="note", the default) or groups sharing exactly the
    same start_time (thin_by="onset"), with a zero-based offset. Onset thinning
    keeps every note in a selected group, including duplicates; nearby onsets
    remain separate without quantization. Transposition follows filtering and
    fails if a retained pitch would leave MIDI's range; pitches are never clamped.
    """
    variation = _normalise_spec(spec)
    if not isinstance(rows, (list, tuple)) or len(rows) > MAX_VARIATION_NOTES:
        raise ValueError("Variation source must contain at most 10000 serialized notes")
    values = [_values(row) for row in rows]
    removed_pitches = set(variation["remove_pitches"])
    candidates = [index for index, row in enumerate(values) if row["pitch"] not in removed_pitches]
    candidates.sort(key=lambda index: (
        values[index]["start_time"], values[index]["pitch"], values[index]["duration"],
        json.dumps(values[index], sort_keys=True, allow_nan=False)))
    kept = []
    rank = -1
    previous_onset = None
    for index in candidates:
        onset = values[index]["start_time"]
        if variation["thin_by"] == "note" or rank < 0 or onset != previous_onset:
            rank += 1
        previous_onset = onset
        if rank % variation["keep_every"] == variation["keep_offset"]:
            kept.append(index)
    notes = []
    for index in kept:
        row = values[index]
        row["pitch"] = _integer(row["pitch"] + variation["transpose"], "transposed pitch", 0, 127)
        notes.append(row)
    kept_set = set(kept)
    return {"variation": variation, "notes": notes, "kept_indexes": kept,
            "removed_indexes": [index for index in range(len(rows)) if index not in kept_set],
            "source_note_count": len(rows), "note_count": len(notes),
            "removed_note_count": len(rows) - len(notes),
            "modified_note_count": len(notes) if variation["transpose"] else 0}


def _read(remote, clip):
    reader = getattr(clip, "get_all_notes_extended", None)
    if not callable(reader):
        raise ValueError("Variation requires get_all_notes_extended")
    vector = reader()
    if len(vector) > MAX_VARIATION_NOTES:
        raise ValueError("Variation source exceeds the 10000-note verification limit")
    rows = remote._serialise_notes(vector, rounded=False)
    if len(rows) != len(vector):
        raise ValueError("Native note vector could not be fully serialized")
    ids = []
    for row in rows:
        note_id = row.get("note_id")
        if isinstance(note_id, bool) or not isinstance(note_id, int) or note_id < 0:
            raise ValueError("Variation requires valid note IDs")
        ids.append(note_id)
    if len(set(ids)) != len(ids):
        raise ValueError("Variation requires unique note IDs")
    return vector, rows


def apply_variation(remote, clip, spec, expected_notes):
    """Apply to a newly copied clip; caller owns rollback on any raised error.

    Removal invalidates the old note vector. Re-read before modification so
    apply_note_modifications receives Live's original native vector containing
    only extant IDs, never a converted Python list/tuple or a stale vector.
    """
    _vector, rows = _read(remote, clip)
    prepared = prepare_variation(rows, spec)
    expected_signature = _signature(expected_notes)
    if _signature(prepared["notes"]) != expected_signature:
        raise ValueError("Copied MIDI variation differs from the retained preview")
    remove_ids = tuple(rows[index]["note_id"] for index in prepared["removed_indexes"])
    remover = getattr(clip, "remove_notes_by_id", None)
    modifier = getattr(clip, "apply_note_modifications", None)
    if remove_ids and not callable(remover):
        raise ValueError("Variation requires remove_notes_by_id")
    if prepared["modified_note_count"] and not callable(modifier):
        raise ValueError("Variation requires apply_note_modifications")
    survivors = [rows[index] for index in prepared["kept_indexes"]]
    if remove_ids:
        remover(remove_ids)
    vector, remaining = _read(remote, clip)
    if _signature(remaining) != _signature(survivors):
        raise RuntimeError("Variation removal readback differs from the planned surviving notes")
    if prepared["modified_note_count"]:
        transpose = prepared["variation"]["transpose"]
        for note in vector:
            note.pitch = note.pitch + transpose
        modifier(vector)
    _vector, actual = _read(remote, clip)
    if _signature(actual) != expected_signature:
        raise RuntimeError("MIDI variation readback differs from the retained preview")
    return prepared
