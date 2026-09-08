"""Audio plans guard source units, transient spans and copied sound settings."""

import copy
import importlib.util
from pathlib import Path
import types

import pytest


_PATH = Path(__file__).parents[2] / "AbletonMCP_Remote_Script" / "audio_arrangement.py"
_SPEC = importlib.util.spec_from_file_location("audio_arrangement", _PATH)
audio = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audio)


class AudioClip:
    def __init__(self, path, warped=True):
        self.name, self.color, self.muted = "Source", 32, False
        self.is_audio_clip, self.is_session_clip, self.is_arrangement_clip = True, True, False
        self.has_envelopes = self.has_groove = self.is_recording = False
        self.warping, self.warp_mode = warped, 4
        self.gain, self.pitch_coarse, self.pitch_fine = 0.8, 0, 0
        self.sample_rate, self.sample_length = 44100, 12 * 44100
        self.file_path, self.ram_mode = str(path), False
        self.length = 16.0
        self.looping = warped
        self.loop_start = self.start_marker = 0.0
        self.loop_end = self.end_marker = 16.0 if warped else 12.0
        self.warp_markers = [types.SimpleNamespace(beat_time=0.0, sample_time=0.0),
                             types.SimpleNamespace(beat_time=16.0, sample_time=12.0)]
        self.writes = []
        self.clamp_end = False
        self.corrupt_gain = False

    def __setattr__(self, name, value):
        if name in ("loop_start", "loop_end", "looping", "end_marker", "warping") and "writes" in vars(self):
            self.writes.append((name, value))
            if name == "loop_end" and self.clamp_end:
                value += 0.25
            if name == "loop_start":
                object.__setattr__(self, "start_marker", value)
                if self.corrupt_gain:
                    object.__setattr__(self, "gain", self.gain + 0.01)
        object.__setattr__(self, name, value)


def song(tempo=110.0):
    return types.SimpleNamespace(tempo=tempo, tempo_follower_enabled=False,
        is_ableton_link_enabled=False, master_track=types.SimpleNamespace(
            mixer_device=types.SimpleNamespace(song_tempo=types.SimpleNamespace(automation_state=0))))


@pytest.fixture
def clip(tmp_path):
    path = tmp_path / "tone.wav"
    path.write_bytes(b"fixture audio identity")
    return AudioClip(path)


def unwarped(clip):
    clip.warping = clip.looping = False
    clip.end_marker = clip.loop_end = 12.0
    clip.writes.clear()
    return clip


def duplicate(clip):
    result = copy.deepcopy(clip)
    result.is_session_clip, result.is_arrangement_clip = False, True
    result.writes.clear()
    return result


def test_capture_and_prepare_do_not_change_source_and_retain_all_evidence(clip):
    before = copy.deepcopy(vars(clip))
    source = audio.capture_source(clip, song())
    prepared = audio.prepare_audio(source, {"start": 2, "end": 8, "units": "beats"})
    assert source["kind"] == "audio" and source["notes"] == []
    assert source["file_identity"]["size_bytes"] == 22
    assert source["tempo_context"] is None
    assert len(source["fingerprint"]) == 64
    assert prepared["length_beats"] == 6
    assert prepared["reserved_length_beats"] == 16
    assert vars(clip) == before


def test_unwarped_length_uses_seconds_and_reserves_stale_duplicated_beat_length(clip):
    source = audio.capture_source(unwarped(clip), song())
    prepared = audio.prepare_audio(source, {"start": 2, "end": 5, "units": "seconds"})
    assert prepared["length_beats"] == 5.5
    assert prepared["reserved_length_beats"] == 22.0
    assert prepared["tempo"] == 110
    assert audio.prepare_audio(source)["length_beats"] == 22.0
    clip.length = 32.0
    assert audio.prepare_audio(audio.capture_source(clip, song()))["reserved_length_beats"] == 32


def test_reservation_covers_marker_extent_when_source_has_offset(clip):
    clip.loop_start = clip.start_marker = 4.0
    clip.loop_end = clip.end_marker = 8.0
    clip.length = 4.0
    prepared = audio.prepare_audio(audio.capture_source(clip, song()))
    assert prepared["length_beats"] == 4
    assert prepared["reserved_length_beats"] == 8


@pytest.mark.parametrize("requested", [
    {}, [], {"start": 0, "end": 4}, {"start": 0, "end": 4, "units": "beats", "extra": 1},
    {"start": 0, "end": 4, "units": "seconds"}, {"start": -1, "end": 4, "units": "beats"},
    {"start": True, "end": 4, "units": "beats"}, {"start": 4, "end": 4, "units": "beats"},
    {"start": 0, "end": 17, "units": "beats"}, {"start": 0, "end": float("inf"), "units": "beats"},
    {"start": float("nan"), "end": 4, "units": "beats"},
])
def test_invalid_range_rejected_before_writes(clip, requested):
    with pytest.raises(ValueError):
        audio.prepare_audio(audio.capture_source(clip, song()), requested)
    assert clip.writes == []


def test_range_cannot_extend_before_offset_source(clip):
    clip.start_marker = clip.loop_start = 4
    with pytest.raises(ValueError, match="inside"):
        audio.prepare_audio(audio.capture_source(clip, song()), {"start": 2, "end": 8, "units": "beats"})


@pytest.mark.parametrize("name,value", [
    ("is_audio_clip", False), ("is_session_clip", False), ("has_envelopes", True),
    ("has_groove", True), ("is_recording", True), ("has_envelopes", None),
    ("start_marker", 1), ("end_marker", 20), ("loop_start", -1),
    ("length", 0), ("sample_rate", 0), ("sample_length", float("nan")),
    ("gain", float("inf")), ("pitch_fine", True),
])
def test_unsupported_audio_source_rejected(clip, name, value):
    setattr(clip, name, value)
    with pytest.raises(ValueError):
        audio.capture_source(clip, song())


def test_unavailable_required_property_rejected(clip):
    del clip.sample_length
    with pytest.raises(ValueError, match="sample_length.*cannot be verified"):
        audio.capture_source(clip, song())


@pytest.mark.parametrize("name,value", [("pitch_coarse", 12), ("pitch_fine", -1), ("looping", True)])
def test_unwarped_pitch_and_looping_rejected(clip, name, value):
    unwarped(clip)
    setattr(clip, name, value)
    with pytest.raises(ValueError):
        audio.capture_source(clip, song())


def test_unwarped_markers_cannot_exceed_sample_duration(clip):
    unwarped(clip)
    clip.loop_end = clip.end_marker = 13.0
    with pytest.raises(ValueError, match="sample duration"):
        audio.capture_source(clip, song())


@pytest.mark.parametrize("change", ["tempo_automation", "automation_unknown", "link", "follower", "tempo_nan", "tempo_missing"])
def test_unwarped_variable_or_unreadable_tempo_rejected(clip, change):
    current = song()
    if change == "tempo_automation": current.master_track.mixer_device.song_tempo.automation_state = 1
    elif change == "automation_unknown": current.master_track.mixer_device.song_tempo.automation_state = None
    elif change == "link": current.is_ableton_link_enabled = True
    elif change == "follower": current.tempo_follower_enabled = True
    elif change == "tempo_nan": current.tempo = float("nan")
    elif change == "tempo_missing": del current.master_track
    with pytest.raises(ValueError):
        audio.capture_source(unwarped(clip), current)


def test_fixed_tempo_is_in_unwarped_source_fingerprint_but_not_warped(clip):
    first = audio.capture_source(clip, song(110))
    assert first["fingerprint"] == audio.capture_source(clip, song(120))["fingerprint"]
    unwarped(clip)
    first = audio.capture_source(clip, song(110))
    assert first["fingerprint"] != audio.capture_source(clip, song(120))["fingerprint"]


@pytest.mark.parametrize("name,value", [
    ("gain", 0.7), ("pitch_coarse", 1), ("pitch_fine", 1), ("warp_mode", 3),
    ("name", "Renamed"), ("color", 64), ("muted", True), ("ram_mode", True),
    ("sample_length", 123456), ("sample_rate", 48000), ("length", 17.0),
])
def test_each_audio_setting_is_in_source_fingerprint(clip, name, value):
    source = audio.capture_source(clip, song())
    setattr(clip, name, value)
    assert source["fingerprint"] != audio.capture_source(clip, song())["fingerprint"]


def test_file_replacement_changes_fingerprint_and_copy_readback_rejects_it(clip):
    source = audio.capture_source(clip, song())
    copy_clip = duplicate(clip)
    path = Path(clip.file_path)
    replacement = path.with_name("replacement.wav")
    replacement.write_bytes(path.read_bytes())
    replacement.replace(path)
    assert source["fingerprint"] != audio.capture_source(clip, song())["fingerprint"]
    with pytest.raises(RuntimeError, match="file identity"):
        audio.apply_audio(copy_clip, source, audio.prepare_audio(source))
    assert copy_clip.writes == []


@pytest.mark.parametrize("path_kind", ["missing", "empty", "directory", "relative"])
def test_unverifiable_audio_files_rejected(clip, tmp_path, path_kind):
    if path_kind == "missing": clip.file_path = str(tmp_path / "missing.wav")
    elif path_kind == "empty": Path(clip.file_path).write_bytes(b"")
    elif path_kind == "directory": clip.file_path = str(tmp_path)
    elif path_kind == "relative": clip.file_path = "relative.wav"
    with pytest.raises(ValueError):
        audio.capture_source(clip, song())


def test_full_warp_mapping_captured_and_verified_without_64_marker_truncation(clip):
    clip.warp_markers = [types.SimpleNamespace(beat_time=float(i), sample_time=i * 0.75) for i in range(-1, 100)]
    source = audio.capture_source(clip, song())
    assert len(source["warp_markers"]) == 101
    copy_clip = duplicate(clip)
    copy_clip.warp_markers[-1].sample_time += 0.001
    with pytest.raises(RuntimeError, match="warp markers"):
        audio.apply_audio(copy_clip, source, audio.prepare_audio(source))
    assert copy_clip.writes == []


def test_warp_marker_changes_invalidate_fingerprint(clip):
    source = audio.capture_source(clip, song())
    clip.warp_markers[-1].sample_time += 0.0000001
    assert source["fingerprint"] != audio.capture_source(clip, song())["fingerprint"]


def test_warp_mapping_unreadable_nonfinite_or_oversized_rejected(clip):
    clip.warp_markers[0].beat_time = float("nan")
    with pytest.raises(ValueError, match="warp markers"):
        audio.capture_source(clip, song())
    clip.warp_markers = [object()]
    with pytest.raises(ValueError, match="warp markers"):
        audio.capture_source(clip, song())
    clip.warp_markers *= audio.MAX_WARP_MARKERS + 1
    with pytest.raises(ValueError, match="4096-warp-marker"):
        audio.capture_source(clip, song())


def test_audio_apply_normalizes_owned_copy_without_touching_source_or_warping(clip):
    clip.pitch_coarse, clip.pitch_fine = 7, 11
    source = audio.capture_source(clip, song())
    prepared = audio.prepare_audio(source, {"start": 2, "end": 8, "units": "beats"})
    copy_clip = duplicate(clip)
    result = audio.apply_audio(copy_clip, source, prepared)
    assert result["audio_verified"] and result["file_identity_verified"]
    assert copy_clip.writes == [("looping", False), ("loop_end", 16), ("loop_start", 2),
                               ("end_marker", 8), ("loop_end", 8)]
    assert copy_clip.pitch_coarse == 7 and copy_clip.pitch_fine == 11
    assert copy_clip.warp_markers == clip.warp_markers
    assert copy_clip.start_marker == 2 and copy_clip.end_marker == 8
    assert clip.writes == []
    assert audio.capture_source(clip, song())["fingerprint"] == source["fingerprint"]


def test_no_range_unwarped_apply_corrects_live_stale_duplicate_loop_end(clip):
    source = audio.capture_source(unwarped(clip), song())
    copy_clip = duplicate(clip)
    copy_clip.loop_end = 16 * 60 / 110  # Live used stale 16-beat clip.length.
    copy_clip.writes.clear()
    result = audio.apply_audio(copy_clip, source, audio.prepare_audio(source))
    assert result["audio_verified"]
    assert copy_clip.loop_end == copy_clip.end_marker == 12
    assert copy_clip.start_marker == copy_clip.loop_start == 0
    assert copy_clip.writes == [("looping", False), ("loop_end", 12), ("loop_start", 0),
                               ("end_marker", 12), ("loop_end", 12)]


@pytest.mark.parametrize("start,end", [(2, 5), (10, 12)])
def test_unwarped_end_recomputed_only_on_loop_end_still_matches_preview(clip, start, end):
    class EndOnLoopEnd(AudioClip):
        def __setattr__(self, name, value):
            if name == "loop_start" and value >= self.loop_end:
                raise RuntimeError("start must precede the current copied loop end")
            super().__setattr__(name, value)
            if name == "loop_end":
                # Model the suspected Live behavior: marker reads update on
                # every write, but arrangement bounds refresh on loop_end.
                self.end_time = self.start_time + (self.loop_end - self.loop_start) * 110 / 60
                self.observed_ends.append(self.end_time)

    source = audio.capture_source(unwarped(clip), song())
    prepared = audio.prepare_audio(source, {"start": start, "end": end, "units": "seconds"})
    copy_clip = duplicate(clip)
    copy_clip.loop_end = 16 * 60 / 110
    copy_clip.start_time, copy_clip.end_time = 128.5, 144.5
    copy_clip.observed_ends = []
    copy_clip.__class__ = EndOnLoopEnd
    audio.apply_audio(copy_clip, source, prepared)
    assert copy_clip.end_time == pytest.approx(copy_clip.start_time + prepared["length_beats"])
    assert all(end <= copy_clip.start_time + prepared["reserved_length_beats"]
               for end in copy_clip.observed_ends)
    assert clip.writes == []


def test_later_audio_verification_is_read_only_and_allows_explicit_copy_rename(clip):
    source = audio.capture_source(clip, song())
    prepared = audio.prepare_audio(source, {"start": 2, "end": 8, "units": "beats"})
    copy_clip = duplicate(clip)
    expected = audio.apply_audio(copy_clip, source, prepared)
    copy_clip.name = "Arrangement section"
    copy_clip.writes.clear()
    before = copy.deepcopy(vars(copy_clip))
    assert audio.verify_audio(copy_clip, source, prepared, expected_name="Arrangement section") == expected
    with pytest.raises(RuntimeError, match="name"):
        audio.verify_audio(copy_clip, source, prepared)
    assert vars(copy_clip) == before


@pytest.mark.parametrize("change", ["gain", "markers", "looping", "warp_marker", "file"])
def test_later_audio_verification_rejects_changes_without_rewriting_clip(clip, change):
    source = audio.capture_source(clip, song())
    prepared = audio.prepare_audio(source, {"start": 2, "end": 8, "units": "beats"})
    copy_clip = duplicate(clip)
    audio.apply_audio(copy_clip, source, prepared)
    if change == "gain": copy_clip.gain += 0.01
    elif change == "markers": copy_clip.loop_start = 3
    elif change == "looping": copy_clip.looping = True
    elif change == "warp_marker": copy_clip.warp_markers[-1].sample_time += 0.1
    elif change == "file": Path(copy_clip.file_path).write_bytes(b"changed audio file identity")
    copy_clip.writes.clear()
    before = copy.deepcopy(vars(copy_clip))
    with pytest.raises(RuntimeError):
        audio.verify_audio(copy_clip, source, prepared)
    assert vars(copy_clip) == before


def test_source_cannot_be_passed_to_apply(clip):
    source = audio.capture_source(clip, song())
    with pytest.raises(ValueError, match="operation-owned"):
        audio.apply_audio(clip, source, audio.prepare_audio(source))
    assert clip.writes == []


def test_modified_prepared_range_rejected_before_writes(clip):
    source = audio.capture_source(clip, song())
    prepared = audio.prepare_audio(source)
    prepared["reserved_length_beats"] = 4
    copy_clip = duplicate(clip)
    with pytest.raises(ValueError, match="retained source"):
        audio.apply_audio(copy_clip, source, prepared)
    assert copy_clip.writes == []


@pytest.mark.parametrize("name,value", [("start_marker", 1), ("end_marker", 32), ("loop_start", 1), ("loop_end", 32)])
def test_unexpected_duplicate_markers_cannot_expand_beyond_reserved_footprint(clip, name, value):
    source = audio.capture_source(clip, song())
    copy_clip = duplicate(clip)
    setattr(copy_clip, name, value)
    copy_clip.writes.clear()
    with pytest.raises(RuntimeError):
        audio.apply_audio(copy_clip, source, audio.prepare_audio(source))
    assert copy_clip.writes == []


@pytest.mark.parametrize("change", ["gain", "missing_optional", "new_envelope", "new_groove", "marker_clamp", "gain_corrupted"])
def test_copy_corruption_or_partial_marker_write_is_explicit_failure(clip, change):
    source = audio.capture_source(clip, song())
    copy_clip = duplicate(clip)
    if change == "gain": copy_clip.gain = 0.6
    elif change == "missing_optional": del copy_clip.ram_mode
    elif change == "new_envelope": copy_clip.has_envelopes = True
    elif change == "new_groove": copy_clip.has_groove = True
    elif change == "marker_clamp": copy_clip.clamp_end = True
    elif change == "gain_corrupted": copy_clip.corrupt_gain = True
    with pytest.raises(RuntimeError):
        audio.apply_audio(copy_clip, source, audio.prepare_audio(source, {"start": 2, "end": 8, "units": "beats"}))
    assert clip.writes == []
