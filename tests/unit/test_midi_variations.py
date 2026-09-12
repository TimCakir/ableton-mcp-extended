"""Exact MIDI variation previews and native-vector writes to owned copies."""

import copy
import math
import sys
import types

import pytest

framework = types.ModuleType("_Framework")
surface = types.ModuleType("_Framework.ControlSurface")
surface.ControlSurface = type("ControlSurface", (), {})
sys.modules.setdefault("_Framework", framework)
sys.modules.setdefault("_Framework.ControlSurface", surface)

from AbletonMCP_Remote_Script.midi_variations import apply_variation, prepare_variation


def note(pitch=60, start=0.0, note_id=1, **extra):
    result = dict(pitch=pitch, start_time=start, duration=0.25, velocity=90.125,
                  mute=False, probability=0.75, velocity_deviation=-4.5,
                  release_velocity=61.125, note_id=note_id)
    result.update(extra)
    return result


class NativeVector(list):
    pass


class NoteProxy:
    """A new wrapper is returned for each read, like Live's proxy objects."""

    def __init__(self, values):
        object.__setattr__(self, "values", copy.deepcopy(values))

    def __getattr__(self, field):
        return self.values[field]

    def __setattr__(self, field, value):
        self.values[field] = value


class Clip:
    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)
        self.vectors = []
        self.events = []
        self.remove_failure = self.modify_failure = None

    def get_all_notes_extended(self):
        # The host is free to enumerate MIDI notes in a different order.
        rows = self.rows if len(self.vectors) % 2 else list(reversed(self.rows))
        vector = NativeVector(NoteProxy(row) for row in rows)
        self.vectors.append(vector)
        return vector

    def remove_notes_by_id(self, ids):
        self.events.append(("remove", ids))
        if self.remove_failure == "before":
            raise RuntimeError("removal rejected")
        if self.remove_failure == "silent":
            return
        selected = ids[:1] if self.remove_failure == "partial" else ids
        self.rows = [row for row in self.rows if row["note_id"] not in selected]
        if self.remove_failure == "partial":
            raise RuntimeError("partial removal")

    def apply_note_modifications(self, vector):
        assert type(vector) is NativeVector
        assert vector is self.vectors[-1]
        assert {row["note_id"] for row in self.rows} == {note.note_id for note in vector}
        self.events.append(("modify", vector))
        if self.modify_failure == "before":
            raise RuntimeError("modification rejected")
        if self.modify_failure == "silent":
            return
        selected = vector[:1] if self.modify_failure == "partial" else vector
        updates = {note.note_id: note.values for note in selected}
        self.rows = [copy.deepcopy(updates.get(row["note_id"], row)) for row in self.rows]
        if self.modify_failure == "partial":
            raise RuntimeError("partial modification")


def remote():
    def serialise(vector, rounded=True):
        assert rounded is False
        return [copy.deepcopy(note.values) for note in vector]
    return types.SimpleNamespace(_serialise_notes=serialise)


def test_preview_removes_pitches_then_thins_notes_then_transposes_without_mutating_input():
    rows = [note(64, 2, 4), note(36, 0, 1), note(60, 0, 2), note(62, 1, 3)]
    original = copy.deepcopy(rows)
    spec = {"remove_pitches": [36, 36], "keep_every": 2, "keep_offset": 1, "transpose": 7}
    result = prepare_variation(rows, spec)
    assert rows == original
    assert spec["remove_pitches"] == [36, 36]
    assert result["notes"] == [{key: value for key, value in dict(rows[3], pitch=69).items()
                                if key != "note_id"}]
    assert result["kept_indexes"] == [3]
    assert result["removed_indexes"] == [0, 1, 2]
    assert result["variation"]["remove_pitches"] == [36]
    assert (result["source_note_count"], result["note_count"], result["removed_note_count"],
            result["modified_note_count"]) == (4, 1, 3, 1)


def test_order_and_regenerated_ids_do_not_change_preview_or_optional_values():
    rows = [note(64, 0, 10), note(60, 0, 11), note(67, 1, 12), note(62, 1, 13)]
    rows[0]["future_exposed_value"] = {"amount": 0.123456789123}
    permuted = [dict(row, note_id=100 + index) for index, row in enumerate(reversed(rows))]
    first = prepare_variation(rows, {"keep_every": 2, "keep_offset": 1, "transpose": -12})
    second = prepare_variation(permuted, first["variation"])
    assert first["notes"] == second["notes"]
    assert first["notes"][0]["future_exposed_value"] == {"amount": 0.123456789123}
    assert first["notes"][0]["velocity_deviation"] == -4.5
    first["notes"][0]["future_exposed_value"]["amount"] = 99
    assert rows[0]["future_exposed_value"]["amount"] == 0.123456789123


def test_exact_duplicate_notes_are_counted_independently():
    rows = [note(note_id=index) for index in range(4)]
    result = prepare_variation(rows, {"keep_every": 2})
    assert result["note_count"] == 2
    assert result["notes"][0] == result["notes"][1]


def test_default_thinning_and_explicit_note_mode_keep_existing_individual_note_behavior():
    rows = [note(67, 0, 1), note(60, 0, 2), note(64, 0, 3), note(72, 1, 4)]
    spec = {"keep_every": 2, "keep_offset": 1}
    implicit = prepare_variation(rows, spec)
    assert implicit == prepare_variation(rows, dict(spec, thin_by="note"))
    assert implicit["variation"]["thin_by"] == "note"
    assert [(row["start_time"], row["pitch"]) for row in implicit["notes"]] == [(0, 64), (1, 72)]
    assert prepare_variation(rows, None) == prepare_variation(rows, {"thin_by": "note"})


@pytest.mark.parametrize("every,offset,expected", [
    (1, 0, [(0, 60), (0, 64), (0, 67), (0.125, 62), (2.75, 60), (2.75, 65),
            (7.125, 64), (7.125, 64), (7.125, 67)]),
    (2, 0, [(0, 60), (0, 64), (0, 67), (2.75, 60), (2.75, 65)]),
    (2, 1, [(0.125, 62), (7.125, 64), (7.125, 64), (7.125, 67)]),
    (3, 2, [(2.75, 60), (2.75, 65)]),
    (4, 3, [(7.125, 64), (7.125, 64), (7.125, 67)]),
    (5, 4, []),
])
def test_onset_thinning_counts_irregular_variable_size_groups_and_preserves_duplicates(every, offset, expected):
    rows = [note(67, 7.125, 1), note(60, 2.75, 2), note(64, 0, 3),
            note(64, 7.125, 4), note(60, 0, 5), note(65, 2.75, 6),
            note(62, 0.125, 7), note(67, 0, 8), note(64, 7.125, 9)]
    original = copy.deepcopy(rows)
    result = prepare_variation(rows, {"thin_by": "onset", "keep_every": every, "keep_offset": offset})
    assert [(row["start_time"], row["pitch"]) for row in result["notes"]] == expected
    assert result["note_count"] == len(expected)
    assert result["removed_note_count"] == len(rows) - len(expected)
    assert result["modified_note_count"] == 0
    assert rows == original
    assert set(result["kept_indexes"]).isdisjoint(result["removed_indexes"])
    assert sorted(result["kept_indexes"] + result["removed_indexes"]) == list(range(len(rows)))


def test_nearby_onsets_and_overlapping_notes_remain_separate_exact_groups():
    nearby = math.nextafter(1.0, 2.0)
    rows = [note(60, 1.0, 1, duration=4), note(64, 1.0, 2, duration=4),
            note(62, nearby, 3), note(65, nearby, 4), note(67, 1.000001, 5)]
    result = prepare_variation(rows, {"thin_by": "onset", "keep_every": 2, "keep_offset": 1})
    assert result["kept_indexes"] == [2, 3]
    assert [row["start_time"] for row in result["notes"]] == [nearby, nearby]


def test_pitch_removal_eliminates_empty_groups_before_onset_counting_and_transposition():
    rows = [note(36, 0, 1), note(60, 1, 2), note(36, 1, 3),
            note(36, 2, 4), note(64, 3, 5), note(67, 3, 6)]
    spec = {"thin_by": "onset", "remove_pitches": [36], "keep_every": 2,
            "keep_offset": 1, "transpose": -12}
    original_spec = copy.deepcopy(spec)
    result = prepare_variation(rows, spec)
    assert result["kept_indexes"] == [4, 5]
    assert [(row["start_time"], row["pitch"]) for row in result["notes"]] == [(3, 52), (3, 55)]
    assert result["modified_note_count"] == 2
    assert spec == original_spec


def test_onset_preview_and_native_apply_are_order_and_id_independent_preserving_all_other_values():
    source = [note(60, 0.123456789123, 1), note(67, 1.234567891234, 2),
              note(64, 0.123456789123, 3), note(65, 2.345678912345, 4),
              note(72, 0.123456789123, 5), note(65, 2.345678912345, 6)]
    source[0].update(duration=0.987654321987, mute=True, probability=0.123456789123,
                     future_exposed_value={"amounts": [0.123456789123, -2]})
    spec = {"thin_by": "onset", "remove_pitches": [72], "keep_every": 2, "transpose": 12}
    source_original = copy.deepcopy(source)
    expected = prepare_variation(source, spec)
    for order in (source, list(reversed(source)), source[2:] + source[:2]):
        copied_rows = [dict(row, note_id=100 + index) for index, row in enumerate(order)]
        assert prepare_variation(copied_rows, spec)["notes"] == expected["notes"]
        clip = Clip(copied_rows)
        result = apply_variation(remote(), clip, spec, expected["notes"])
        assert result["notes"] == expected["notes"]
        assert prepare_variation(clip.rows, {})["notes"] == expected["notes"]
        assert [event[0] for event in clip.events] == ["remove", "modify"]
        assert clip.events[1][1] is clip.vectors[1]
        assert clip.events[1][1] is not clip.vectors[0]
        for row in clip.rows:
            original = next(old for old in copied_rows if old["note_id"] == row["note_id"])
            assert row == dict(original, pitch=original["pitch"] + 12)
    assert source == source_original
    assert expected["note_count"] == 4
    assert expected["notes"][0]["future_exposed_value"] == {"amounts": [0.123456789123, -2]}


@pytest.mark.parametrize("source,spec", [
    ([], {"keep_every": 10000, "keep_offset": 9999, "transpose": -127}),
    ([note(36), note(36, 1, 2)], {"remove_pitches": [36], "transpose": -127}),
    ([note(127), note(60, 0, 2)], {"keep_every": 2, "keep_offset": 1, "transpose": 127}),
])
def test_empty_onset_results_are_valid_and_skip_unnecessary_native_mutations(source, spec):
    spec = dict(spec, thin_by="onset")
    clip = Clip(source)
    expected = prepare_variation(source, spec)
    assert expected["notes"] == []
    assert expected["modified_note_count"] == 0
    apply_variation(remote(), clip, spec, [])
    assert clip.rows == []
    assert [event[0] for event in clip.events] == (["remove"] if source else [])


def test_onset_groups_obey_note_limit_and_transpose_bounds_for_every_retained_note():
    source = [note(0, 0, 1), note(126, 0, 2), note(127, 1, 3)]
    result = prepare_variation(source, {"thin_by": "onset", "keep_every": 2, "transpose": 1})
    assert [row["pitch"] for row in result["notes"]] == [1, 127]
    clip = Clip([note(60, 0, 1), note(127, 0, 2)])
    with pytest.raises(ValueError, match="transposed pitch"):
        apply_variation(remote(), clip, {"thin_by": "onset", "transpose": 1}, [])
    assert clip.events == []
    assert clip.rows == [note(60, 0, 1), note(127, 0, 2)]
    source = [note()] * 10000
    assert prepare_variation(source, {"thin_by": "onset", "keep_every": 10000})["note_count"] == 10000
    with pytest.raises(ValueError, match="10000"):
        prepare_variation(source + [note()], {"thin_by": "onset"})


def test_all_removed_and_empty_sources_are_valid_explicit_variations():
    result = prepare_variation([note(36)], {"remove_pitches": [36], "transpose": -127})
    assert result["notes"] == []
    assert prepare_variation([], {"keep_every": 4, "keep_offset": 3})["note_count"] == 0


@pytest.mark.parametrize("spec", [
    [], "transpose", True, {"randomize": True}, {"velocity_scale": 0.5},
    {"remove_pitches": None}, {"remove_pitches": (36,)}, {"remove_pitches": [36] * 129},
    {"remove_pitches": [True]}, {"remove_pitches": [36.0]}, {"remove_pitches": [-1]},
    {"remove_pitches": [128]}, {"keep_every": 0}, {"keep_every": True},
    {"keep_every": 2.0}, {"keep_every": 10001}, {"keep_offset": 1},
    {"keep_every": 2, "keep_offset": -1}, {"keep_every": 2, "keep_offset": True},
    {"keep_every": 2, "keep_offset": 2}, {"transpose": -128}, {"transpose": 128},
    {"transpose": True}, {"transpose": 1.0}, {"transpose": float("nan")},
    {"thin_by": None}, {"thin_by": True}, {"thin_by": False}, {"thin_by": 0},
    {"thin_by": 1.0}, {"thin_by": []}, {"thin_by": {}}, {"thin_by": ("onset",)},
    {"thin_by": ""}, {"thin_by": "chord"}, {"thin_by": "Onset"}, {"thin_by": " onset"},
])
def test_bad_variations_fail_before_mutation(spec):
    clip = Clip([note()])
    with pytest.raises(ValueError):
        apply_variation(remote(), clip, spec, [])
    assert clip.events == []
    assert clip.rows == [note()]


@pytest.mark.parametrize("field,value", [
    ("pitch", 128), ("pitch", True), ("duration", 0), ("duration", float("nan")),
    ("start_time", float("inf")), ("velocity", -1), ("velocity", 128),
    ("mute", 1), ("probability", 1.1), ("velocity_deviation", -128),
    ("release_velocity", 128), ("future_exposed_value", float("nan")),
])
def test_unverifiable_source_values_are_rejected(field, value):
    with pytest.raises(ValueError):
        prepare_variation([dict(note(), **{field: value})], {})


@pytest.mark.parametrize("rows", [[None], [{}], None, "notes"])
def test_invalid_note_shapes_rejected(rows):
    with pytest.raises(ValueError):
        prepare_variation(rows, {})


def test_bounds_are_exact_and_transposition_never_clamps():
    assert prepare_variation([note(0), note(126, 1)], {"transpose": 1})["notes"][1]["pitch"] == 127
    with pytest.raises(ValueError, match="transposed pitch"):
        prepare_variation([note(127)], {"transpose": 1})
    assert prepare_variation([note()] * 10000, {})["note_count"] == 10000
    with pytest.raises(ValueError, match="10000"):
        prepare_variation([note()] * 10001, {})


def test_combined_apply_refetches_native_vector_and_preserves_source_and_other_values():
    # Pitch 60 moves onto the removed pitch 72. Removal must precede transpose.
    source = [note(72, 0, 1), note(60, 0, 2), note(62, 1, 3), note(64, 2, 4)]
    original = copy.deepcopy(source)
    clip = Clip(source)
    spec = {"remove_pitches": [72], "keep_every": 2, "transpose": 12}
    expected = prepare_variation(source, spec)
    applied = apply_variation(remote(), clip, spec, expected["notes"])
    assert source == original
    assert prepare_variation(clip.rows, {})["notes"] == expected["notes"]
    assert [event[0] for event in clip.events] == ["remove", "modify"]
    assert clip.events[1][1] is clip.vectors[1]
    assert clip.events[1][1] is not clip.vectors[0]
    assert applied["note_count"] == 2
    for row in clip.rows:
        baseline = next(note for note in source if note["note_id"] == row["note_id"])
        assert row == dict(baseline, pitch=baseline["pitch"] + 12)


def test_unnecessary_native_mutations_are_skipped():
    clip = Clip([note()])
    expected = prepare_variation(clip.rows, None)["notes"]
    apply_variation(remote(), clip, None, expected)
    assert clip.events == []


def test_removing_all_notes_skips_modify_and_verifies_empty_vector():
    clip = Clip([note(36)])
    apply_variation(remote(), clip, {"remove_pitches": [36], "transpose": 12}, [])
    assert clip.rows == []
    assert [event[0] for event in clip.events] == ["remove"]


@pytest.mark.parametrize("phase", ["remove", "modify"])
@pytest.mark.parametrize("failure", ["before", "partial", "silent"])
def test_native_failures_propagate_for_caller_owned_clip_rollback(phase, failure):
    clip = Clip([note(36, 0, 1), note(36, 1, 2), note(60, 2, 3), note(64, 3, 4)])
    spec = {"remove_pitches": [36], "transpose": 7}
    expected = prepare_variation(clip.rows, spec)["notes"]
    setattr(clip, "remove_failure" if phase == "remove" else "modify_failure", failure)
    with pytest.raises(RuntimeError):
        apply_variation(remote(), clip, spec, expected)
    if phase == "remove":
        assert [event[0] for event in clip.events] == ["remove"]


def test_retained_preview_mismatch_rejects_before_any_write():
    clip = Clip([note(36, 0, 1), note(60, 1, 2)])
    expected = prepare_variation(clip.rows, {"remove_pitches": [36]})["notes"]
    clip.rows[1]["velocity"] += 0.000000001
    with pytest.raises(ValueError, match="retained preview"):
        apply_variation(remote(), clip, {"remove_pitches": [36]}, expected)
    assert clip.events == []


@pytest.mark.parametrize("ids", [[1, 1], [True, 2], [-1, 2], [1.0, 2], [None, 2]])
def test_invalid_or_duplicate_ids_reject_before_mutation(ids):
    clip = Clip([note(36, 0, ids[0]), note(60, 1, ids[1])])
    with pytest.raises(ValueError, match="note IDs"):
        apply_variation(remote(), clip, {"remove_pitches": [36]}, [])
    assert clip.events == []


def test_missing_modifier_is_checked_before_removal():
    clip = Clip([note(36, 0, 1), note(60, 1, 2)])
    clip.apply_note_modifications = None
    spec = {"remove_pitches": [36], "transpose": 7}
    with pytest.raises(ValueError, match="apply_note_modifications"):
        apply_variation(remote(), clip, spec, prepare_variation(clip.rows, spec)["notes"])
    assert clip.events == []
