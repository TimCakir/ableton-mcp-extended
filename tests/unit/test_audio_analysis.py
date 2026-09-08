"""File-based measurements, independent of Live's display meters."""

import json
import math
import shutil
import struct
import types
import wave

import pytest

from MCP_Server.audio_analysis import analyze_audio_file


def test_analysis_validates_bounds_before_running_ffmpeg(tmp_path):
    path = tmp_path / "audio.wav"
    path.write_bytes(b"not decoded")
    for seconds in (0, -1, float("nan"), float("inf"), 601):
        with pytest.raises(ValueError):
            analyze_audio_file(str(path), seconds)


def test_metadata_and_measurements_are_structured_and_command_is_not_shell(tmp_path, monkeypatch):
    path = tmp_path / "audio ; $(unsafe).wav"
    path.write_bytes(b"test")
    commands = []
    monkeypatch.setattr("MCP_Server.audio_analysis.shutil.which", lambda name: "/bin/" + name)
    def run(command, **kwargs):
        commands.append(command)
        assert not kwargs.get("shell", False)
        if "ffprobe" in command[0]:
            return types.SimpleNamespace(stdout=json.dumps({"streams": [{"sample_rate": "48000", "channels": 2}],
                                                           "format": {"duration": "120"}}))
        return types.SimpleNamespace(stderr="mean_volume: -18.4 dB\nmax_volume: -3.2 dB\n")
    monkeypatch.setattr("MCP_Server.audio_analysis.subprocess.run", run)
    result = analyze_audio_file(str(path))
    assert result["duration_seconds"] == 120
    assert result["sample_peak_dbfs"] == -3.2
    assert not result["analysis_complete"]
    assert str(path) in commands[1]


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="optional FFmpeg executables absent")
@pytest.mark.parametrize("silent", [False, True])
def test_real_generated_wave_calibrates_levels(tmp_path, silent):
    path = tmp_path / "calibration.wav"
    samples = [0 if silent else int(16383 * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(8000)]
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(struct.pack("<" + "h" * len(samples), *samples))
    result = analyze_audio_file(str(path), analysis_seconds=2)
    assert result["analysis_complete"]
    assert result["duration_seconds"] == pytest.approx(1)
    assert result["channels"] == 1
    assert result["below_silence_threshold"] is silent
    if not silent:
        assert result["sample_peak_dbfs"] == pytest.approx(-6.0, abs=0.15)
        assert result["rms_dbfs"] == pytest.approx(-9.0, abs=0.15)
