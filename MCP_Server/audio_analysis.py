"""Bounded, local-file audio measurements using optional FFmpeg executables."""

import json
import math
from pathlib import Path
import re
import shutil
import subprocess


def analyze_audio_file(path: str, analysis_seconds: float = 60.0,
                       silence_threshold_dbfs: float = -80.0) -> dict:
    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("Audio path must be a local file")
    if not math.isfinite(analysis_seconds) or not 0 < analysis_seconds <= 600:
        raise ValueError("analysis_seconds must be in (0, 600]")
    if not math.isfinite(silence_threshold_dbfs) or not -120 <= silence_threshold_dbfs <= 0:
        raise ValueError("silence_threshold_dbfs must be between -120 and 0")
    ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        raise RuntimeError("Audio analysis requires ffmpeg and ffprobe on PATH")
    metadata = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=sample_rate,channels,duration:format=duration", "-of", "json", str(source)],
        capture_output=True, text=True, check=True, timeout=20,
    )
    info = json.loads(metadata.stdout)
    streams = info.get("streams", [])
    if not streams:
        raise ValueError("File contains no readable audio stream")
    stream = streams[0]
    raw_duration = stream.get("duration") or info.get("format", {}).get("duration")
    duration = float(raw_duration) if raw_duration and raw_duration != "N/A" else None
    if duration is not None and not math.isfinite(duration):
        duration = None
    measured = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-v", "info", "-i", str(source),
         "-map", "0:a:0", "-t", str(analysis_seconds), "-af", "volumedetect",
         "-f", "null", "-"], capture_output=True, text=True, check=True,
        timeout=max(30.0, min(180.0, analysis_seconds)),
    )
    def measurement(key):
        matches = re.findall(r"\b" + key + r":\s*(-?[\d.]+|-?inf)\s*dB", measured.stderr)
        if not matches:
            raise RuntimeError("FFmpeg did not return " + key)
        value = float(matches[-1])
        return value if math.isfinite(value) else None
    peak, rms = measurement("max_volume"), measurement("mean_volume")
    return {
        "file_path": str(source), "duration_seconds": duration,
        "sample_rate": int(stream["sample_rate"]), "channels": int(stream["channels"]),
        "analyzed_seconds": min(duration, analysis_seconds) if duration is not None else analysis_seconds,
        "analysis_complete": duration is not None and duration <= analysis_seconds,
        "sample_peak_dbfs": peak, "rms_dbfs": rms,
        "below_silence_threshold": peak is None or peak <= silence_threshold_dbfs,
        "silence_threshold_dbfs": silence_threshold_dbfs,
        "possible_clipping": peak is not None and peak >= -0.1,
        "method": "ffmpeg volumedetect (16-bit measurement, 0.1 dB precision; not true peak or LUFS)",
    }
