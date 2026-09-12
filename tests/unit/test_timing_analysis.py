"""Known timing signals exercise the measurement independently of Live."""

import wave

import pytest

from MCP_Server.timing_analysis import analyze_recording_timing


def recording(tmp_path, times, width=2, channels=1, rate=8000, duration=4, selected=1):
    path = tmp_path / "hits.wav"
    signal = bytearray()
    pulses = {round(t * rate) for t in times}
    for frame in range(round(duration * rate)):
        for channel in range(1, channels + 1):
            value = int((1 << (width * 8 - 2)) if frame in pulses and channel == selected else 0)
            signal += bytes([value + 128]) if width == 1 else value.to_bytes(width, "little", signed=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(signal)
    return path


@pytest.mark.parametrize("width", [1, 2, 3, 4])
def test_known_late_hits_keep_original_sample_rate_and_skip_startup(tmp_path, width):
    refs = [.5, 1., 1.5, 2., 2.5]
    path = recording(tmp_path, [.57, 1.05, 1.507, 2.008, 2.509], width=width)
    result = analyze_recording_timing(str(path), refs)
    assert result["status"] == "measured" and result["complete"]
    assert result["measured_hit_count"] == 3
    assert result["median_offset_ms"] == pytest.approx(8)
    assert result["jitter_stddev_ms"] == pytest.approx(1)
    assert result["jitter_peak_to_peak_ms"] == pytest.approx(2)
    assert result["hits"][0]["offset_ms"] == pytest.approx(70)
    assert not result["hits"][0]["included_in_statistics"]
    assert result["sample_resolution_ms"] == .125


def test_early_hits_and_explicit_channel_selection(tmp_path):
    path = recording(tmp_path, [.48, .98, 1.48], channels=2, selected=2)
    result = analyze_recording_timing(str(path), [.5, 1, 1.5], channel=2, skip_initial=0)
    assert result["median_offset_ms"] == pytest.approx(-20)
    silent = analyze_recording_timing(str(path), [.5, 1, 1.5], skip_initial=0)
    assert silent["status"] == "insufficient_data"
    assert silent["median_offset_ms"] is None and silent["jitter_stddev_ms"] is None
    assert all(h["status"] == "missing" for h in silent["hits"])


def test_ambiguous_and_missing_edges_are_not_silently_matched(tmp_path):
    path = recording(tmp_path, [.45, .55, 1.52])
    result = analyze_recording_timing(str(path), [.5, 1, 1.5], skip_initial=0)
    assert [h["status"] for h in result["hits"]] == ["ambiguous", "missing", "matched"]
    assert not result["complete"] and result["measured_hit_count"] == 1
    assert result["median_offset_ms"] is None


def test_file_boundary_does_not_claim_zero_latency(tmp_path):
    path = recording(tmp_path, [0, .5, 1, 1.5])
    result = analyze_recording_timing(str(path), [0, .5, 1, 1.5], skip_initial=0)
    assert result["hits"][0]["status"] == "boundary"
    assert result["starts_above_threshold"]
    assert not result["complete"] and result["measured_hit_count"] == 3


def test_close_ringing_does_not_become_a_second_hit(tmp_path):
    path = recording(tmp_path, [.51, .52, .53, 1.01, 1.02, 1.51, 1.52])
    result = analyze_recording_timing(str(path), [.5, 1, 1.5], skip_initial=0)
    assert result["median_offset_ms"] == pytest.approx(10)
    assert result["detected_edge_count"] == 3


@pytest.mark.parametrize("arguments", [
    {"expected_onsets_seconds": [0, .1, .2]},
    {"expected_onsets_seconds": [1, .5, 2]},
    {"expected_onsets_seconds": [.5, 1, float('nan')]},
    {"expected_onsets_seconds": [.5, 1, 120]},
    {"channel": True}, {"channel": 0}, {"channel": 2},
    {"skip_initial": 1}, {"skip_initial": True},
    {"threshold_dbfs": float('inf')}, {"threshold_dbfs": True},
    {"min_separation_ms": 0}, {"search_window_ms": -1},
])
def test_invalid_or_ambiguous_inputs_fail(tmp_path, arguments):
    path = recording(tmp_path, [.5, 1, 1.5])
    params = {"expected_onsets_seconds": [.5, 1, 1.5], "skip_initial": 0, **arguments}
    with pytest.raises(ValueError):
        analyze_recording_timing(str(path), **params)


def test_file_must_cover_whole_search_window(tmp_path):
    path = recording(tmp_path, [.5, 1, 1.5], duration=1.55)
    with pytest.raises(ValueError, match="full final search window"):
        analyze_recording_timing(str(path), [.5, 1, 1.5], skip_initial=0)


def test_unsupported_and_truncated_sources_are_errors(tmp_path):
    path = tmp_path / "fake.wav"
    path.write_text("not a WAV")
    with pytest.raises(ValueError, match="PCM WAV"):
        analyze_recording_timing(str(path), [.5, 1, 1.5], skip_initial=0)
    path = recording(tmp_path, [.5, 1, 1.5])
    path.write_bytes(path.read_bytes()[:200])
    with pytest.raises(ValueError, match="Truncated"):
        analyze_recording_timing(str(path), [.5, 1, 1.5], skip_initial=0)


def test_source_change_and_cancellation_discard_measurement(tmp_path, monkeypatch):
    path = recording(tmp_path, [.5, 1, 1.5])
    def change():
        path.touch()
    monkeypatch.setattr("MCP_Server.timing_analysis.raise_if_tool_cancelled", change)
    with pytest.raises(ValueError, match="changed during"):
        analyze_recording_timing(str(path), [.5, 1, 1.5], skip_initial=0)
    def cancel():
        raise RuntimeError("cancelled")
    monkeypatch.setattr("MCP_Server.timing_analysis.raise_if_tool_cancelled", cancel)
    with pytest.raises(RuntimeError, match="cancelled"):
        analyze_recording_timing(str(path), [.5, 1, 1.5], skip_initial=0)
