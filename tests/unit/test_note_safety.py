"""Musical-value integrity for MIDI reads, edits, and failed replacements."""

import copy
import sys
import types
from unittest.mock import MagicMock

import pytest

_framework = types.ModuleType("_Framework")
_control_surface = types.ModuleType("_Framework.ControlSurface")
_control_surface.ControlSurface = type("ControlSurface", (), {})
sys.modules.setdefault("_Framework", _framework)
sys.modules.setdefault("_Framework.ControlSurface", _control_surface)

from AbletonMCP_Remote_Script import AbletonMCP


class Note:
    def __init__(self, pitch=60, start_time=0.0, duration=0.25, velocity=100.0,
                 mute=False, probability=1.0, velocity_deviation=0.0,
                 release_velocity=64.0, note_id=1):
        self.pitch = pitch
        self.start_time = start_time
        self.duration = duration
        self.velocity = velocity
        self.mute = mute
        self.probability = probability
        self.velocity_deviation = velocity_deviation
        self.release_velocity = release_velocity
        self.note_id = note_id


class Clip:
    name = "Performance"
    is_midi_clip = True
    is_audio_clip = False
    length = 16.0
    start_marker = 16.0
    end_marker = 32.0
    loop_start = 16.0
    loop_end = 32.0

    def __init__(self, notes):
        self.notes = copy.deepcopy(notes)
        self.remove_failures = []
        self.add_failures = []
        self.apply_failures = []
        self.remove_calls = 0
        self.add_calls = 0
        self.apply_calls = 0
        self.queries = []
        self.duplicate_region = MagicMock()

    def get_all_notes_extended(self):
        return copy.deepcopy(self.notes)

    @staticmethod
    def in_window(note, pitch, pitch_span, start, span):
        return pitch <= note.pitch < pitch + pitch_span and start <= note.start_time < start + span

    def get_notes_extended(self, pitch, pitch_span, start, span):
        self.queries.append((pitch, pitch_span, start, span))
        return copy.deepcopy([n for n in self.notes if self.in_window(n, pitch, pitch_span, start, span)])

    def remove_notes_extended(self, *args):
        self.remove_calls += 1
        failure = self.remove_failures.pop(0) if self.remove_failures else None
        if failure == "before":
            raise RuntimeError("remove rejected")
        matches = [n for n in self.notes if self.in_window(n, *args)]
        if failure == "partial":
            if matches:
                self.notes.remove(matches[0])
            raise RuntimeError("partial removal")
        self.notes = [n for n in self.notes if not self.in_window(n, *args)]

    def add_new_notes(self, specs):
        self.add_calls += 1
        failure = self.add_failures.pop(0) if self.add_failures else None
        if failure == "before":
            raise RuntimeError("insert rejected")
        for spec in specs:
            note = copy.deepcopy(spec)
            note.note_id = max([n.note_id for n in self.notes] + [100]) + 1
            self.notes.append(note)
            if failure == "partial":
                raise RuntimeError("partial insertion")

    def apply_note_modifications(self, vector):
        self.apply_calls += 1
        changes = {n.note_id: n for n in vector}
        failure = self.apply_failures.pop(0) if self.apply_failures else None
        if failure == "partial":
            first_id = vector[0].note_id
            self.notes = [copy.deepcopy(changes[n.note_id]) if n.note_id == first_id else n
                          for n in self.notes]
            raise RuntimeError("partial modification")
        self.notes = [copy.deepcopy(changes.get(n.note_id, n)) for n in self.notes]


@pytest.fixture(autouse=True)
def live_note_specification(monkeypatch):
    monkeypatch.setitem(sys.modules, "Live", types.SimpleNamespace(
        Clip=types.SimpleNamespace(MidiNoteSpecification=Note)))


def make_script(clip):
    script = AbletonMCP.__new__(AbletonMCP)
    script.log_message = lambda _message: None
    song = types.SimpleNamespace(tempo=120.0, begin_undo_step=MagicMock(), end_undo_step=MagicMock())
    script.song = lambda: song
    track = types.SimpleNamespace(name="Piano")
    script._resolve_clip = lambda *_args: (track, clip)
    script._clip_at = lambda *_args: (track, clip)
    return script


def values(clip):
    return sorted(tuple((k, v) for k, v in vars(n).items() if k != "note_id") for n in clip.notes)


@pytest.mark.parametrize("bad_note", [
    {"pitch": "not a pitch"}, {"pitch": 128}, {"pitch": -1}, {"pitch": 60.5},
    {"start_time": float("nan")}, {"start_time": float("inf")},
    {"duration": 0}, {"duration": -1}, {"duration": float("inf")},
    {"velocity": float("nan")}, {"velocity": 128}, {"velocity": -1},
    {"probability": 1.01}, {"probability": float("nan")},
    {"velocity_deviation": -128}, {"release_velocity": float("inf")},
    {"mute": "false"}, None,
])
def test_invalid_replacement_is_validated_before_any_mutation(bad_note):
    clip = Clip([Note(start_time=20.0)])
    original = values(clip)
    script = make_script(clip)

    with pytest.raises(ValueError):
        script._add_notes_extended(0, 0, [{"pitch": 67}, bad_note], replace=True)

    assert values(clip) == original
    assert clip.remove_calls == clip.add_calls == 0
    script._song.begin_undo_step.assert_not_called()


def test_unsupported_probability_fails_before_replacement(monkeypatch):
    class LegacySpecification:
        __slots__ = ("pitch", "start_time", "duration", "velocity", "mute")

        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    monkeypatch.setitem(sys.modules, "Live", types.SimpleNamespace(
        Clip=types.SimpleNamespace(MidiNoteSpecification=LegacySpecification)))
    clip = Clip([Note(start_time=20.0)])
    original = values(clip)

    with pytest.raises(ValueError, match="probability is unsupported"):
        make_script(clip)._add_notes_extended(0, 0, [{"probability": 0.8}], replace=True)

    assert values(clip) == original
    assert clip.remove_calls == 0


def test_failed_removal_does_not_append_replacement():
    clip = Clip([Note(start_time=20.0)])
    clip.remove_failures = ["before"]
    original = values(clip)
    script = make_script(clip)

    with pytest.raises(RuntimeError, match="original notes unchanged"):
        script._add_notes_extended(0, 0, [{"pitch": 72}], replace=True)

    assert values(clip) == original
    assert clip.notes[0].note_id == 1
    assert clip.add_calls == 0
    script._song.end_undo_step.assert_called_once()


@pytest.mark.parametrize("failure_stage", ["remove", "insert", "append"])
def test_partial_mutation_restores_original_performance_values(failure_stage):
    # Both notes are outside the loop, including one before beat zero. Their
    # sub-five-decimal timing and expression values must survive recovery.
    clip = Clip([
        Note(start_time=-2.123456789, probability=0.45, velocity_deviation=-12.5,
             release_velocity=31.5, mute=True, note_id=8),
        Note(pitch=72, start_time=41.123456789, duration=0.812345678, note_id=9),
    ])
    original = values(clip)
    script = make_script(clip)
    if failure_stage == "remove":
        clip.remove_failures = ["partial"]
    else:
        clip.add_failures = ["partial"]

    with pytest.raises(RuntimeError, match="original note values restored"):
        script._add_notes_extended(0, 0, [{"pitch": 50, "start_time": 96.0}],
                                   replace=failure_stage != "append")

    assert values(clip) == original
    script._song.begin_undo_step.assert_called_once()
    script._song.end_undo_step.assert_called_once()


def test_failed_recovery_reports_primary_and_rollback_errors():
    clip = Clip([Note(start_time=20.0)])
    clip.add_failures = ["partial", "before"]
    script = make_script(clip)

    with pytest.raises(RuntimeError, match="partial insertion; rollback failed: insert rejected"):
        script._add_notes_extended(0, 0, [{"pitch": 50}], replace=True)

    script._song.end_undo_step.assert_called_once()


def test_successful_replacement_clears_all_stored_notes_and_preserves_new_metadata():
    clip = Clip([Note(start_time=-2.0), Note(start_time=48.0)])
    script = make_script(clip)
    result = script._add_notes_extended(0, 0, [{
        "pitch": 72, "start_time": 20.25, "duration": 0.75,
        "probability": 0.7, "velocity_deviation": -5.0, "release_velocity": 29.0,
    }], replace=True)

    assert len(clip.notes) == 1
    assert clip.notes[0].pitch == 72
    assert clip.notes[0].probability == 0.7
    assert clip.notes[0].velocity_deviation == -5.0
    assert clip.notes[0].release_velocity == 29.0
    assert result["read_scope"] == "all_stored"
    script._song.end_undo_step.assert_called_once()


def test_default_read_reports_markers_and_notes_outside_nonzero_loop():
    clip = Clip([Note(start_time=24.0), Note(start_time=48.0, note_id=2)])
    result = make_script(clip)._get_clip_notes(0, 0)

    assert result["note_count"] == 2
    assert result["read_scope"] == "all_stored"
    assert result["start_marker"] == result["loop_start"] == 16.0
    assert result["end_marker"] == result["loop_end"] == 32.0
    assert result["query_bounds"]["time_span"] > 48.0


@pytest.mark.parametrize("action", ["read", "modify", "remove"])
def test_explicit_windows_preserve_unselected_notes(action):
    clip = Clip([Note(pitch=60, start_time=24.0), Note(pitch=61, start_time=48.0, note_id=2)])
    script = make_script(clip)
    kwargs = {"from_time": 16.0, "time_span": 16.0}
    if action == "read":
        result = script._get_clip_notes(0, 0, **kwargs)
        assert result["note_count"] == 1
    elif action == "modify":
        result = script._modify_clip_notes(0, 0, transpose=12, **kwargs)
        assert [n.pitch for n in clip.notes] == [72, 61]
    else:
        result = script._remove_clip_notes(0, 0, **kwargs)
        assert [(n.pitch, n.start_time) for n in clip.notes] == [(61, 48.0)]
    assert result["read_scope"] == "explicit_window"


@pytest.mark.parametrize("action", ["modify", "remove"])
def test_default_mutations_cover_notes_outside_loop(action):
    clip = Clip([Note(start_time=24.0), Note(start_time=48.0, note_id=2)])
    script = make_script(clip)
    if action == "modify":
        result = script._modify_clip_notes(0, 0, transpose=12)
        assert [n.pitch for n in clip.notes] == [72, 72]
    else:
        result = script._remove_clip_notes(0, 0)
        assert clip.notes == []
        assert result["removed"] == 2
    assert result["read_scope"] == "all_stored"


def test_legacy_read_covers_markers_and_reports_bounded_fallback():
    clip = Clip([Note(start_time=24.0), Note(start_time=48.0, note_id=2)])
    clip.get_all_notes_extended = None
    result = make_script(clip)._get_clip_notes(0, 0)

    assert result["note_count"] == 1
    assert result["read_scope"] == "marker_bounds_fallback"
    assert result["all_notes_supported"] is False
    assert "may be excluded" in result["scope_warning"]
    assert clip.queries[-1] == (0, 128, 0.0, 32.0)


def test_pitch_filter_with_default_time_span_includes_later_notes():
    clip = Clip([Note(pitch=60, start_time=24.0), Note(pitch=72, start_time=48.0, note_id=2)])
    result = make_script(clip)._get_clip_notes(0, 0, from_pitch=72, pitch_span=1)
    assert [n["pitch"] for n in result["notes"]] == [72]


def test_remove_recovers_only_its_selected_window():
    clip = Clip([Note(start_time=20.0), Note(start_time=24.0, note_id=2),
                 Note(start_time=48.0, note_id=3)])
    clip.remove_failures = ["partial"]
    original = values(clip)

    with pytest.raises(RuntimeError, match="original note values restored"):
        make_script(clip)._remove_clip_notes(0, 0, from_time=16.0, time_span=16.0)

    assert values(clip) == original
    assert next(n for n in clip.notes if n.start_time == 48.0).note_id == 3


@pytest.mark.parametrize("kwargs", [
    {"transpose": float("nan")}, {"velocity_scale": float("inf")},
    {"velocity_set": float("nan")}, {"humanize_ms": float("inf")},
    {"probability": float("nan")}, {"from_time": float("nan")},
    {"time_span": float("inf")}, {"time_span": -1.0},
])
def test_invalid_transform_does_not_apply(kwargs):
    clip = Clip([Note(start_time=20.0)])
    original = values(clip)
    with pytest.raises(ValueError):
        make_script(clip)._modify_clip_notes(0, 0, **kwargs)
    assert clip.apply_calls == 0
    assert values(clip) == original


def test_humanization_explicitly_reports_cumulative_behavior():
    clip = Clip([Note(start_time=20.0), Note(start_time=24.0, note_id=2)])
    script = make_script(clip)
    first = script._modify_clip_notes(0, 0, humanize_ms=10.0)
    second = script._modify_clip_notes(0, 0, humanize_ms=10.0)
    assert clip.notes[1].start_time == pytest.approx(24.024)
    assert first["humanize_behavior"] == second["humanize_behavior"]
    assert "cumulative" in second["humanize_behavior"]


def test_humanization_does_not_clamp_stored_pickup_notes_to_zero():
    clip = Clip([Note(start_time=-2.0), Note(start_time=-1.0, note_id=2)])
    make_script(clip)._modify_clip_notes(0, 0, humanize_ms=10.0)
    assert clip.notes[0].start_time == -2.0
    assert clip.notes[1].start_time == pytest.approx(-0.988)


def test_partial_note_modification_restores_values_by_original_note_ids():
    clip = Clip([Note(start_time=20.0), Note(start_time=48.0, note_id=2)])
    clip.apply_failures = ["partial"]
    original = values(clip)
    script = make_script(clip)
    with pytest.raises(RuntimeError, match="original note values restored"):
        script._modify_clip_notes(0, 0, transpose=12, probability=0.25)
    assert values(clip) == original
    assert [note.note_id for note in clip.notes] == [1, 2]
    script._song.end_undo_step.assert_called_once()


def test_transform_readback_verifies_the_selected_note():
    clip = Clip([Note(pitch=60, start_time=0.0), Note(pitch=72, start_time=24.0, note_id=2)])
    result = make_script(clip)._modify_clip_notes(
        0, 0, transpose=7, from_time=16.0, time_span=16.0)
    assert result["verify_first_note"]["pitch"] == 79


def test_duplicate_region_uses_length_not_end_coordinate():
    clip = Clip([])
    result = make_script(clip)._manage_clip_region(
        0, 0, action="duplicate_region", region_start=8.0, region_end=12.0,
        destination_time=16.0)
    clip.duplicate_region.assert_called_once_with(8.0, 4.0, 16.0)
    assert result["region_length"] == 4.0


def test_invalid_region_is_rejected_before_marker_changes():
    clip = Clip([])
    with pytest.raises(ValueError, match="region_end"):
        make_script(clip)._manage_clip_region(
            0, 0, action="duplicate_region", region_start=12.0, region_end=8.0,
            destination_time=16.0, start_marker=20.0)
    assert clip.start_marker == 16.0
    clip.duplicate_region.assert_not_called()
