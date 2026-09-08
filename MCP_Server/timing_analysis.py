"""Threshold-edge timing measurements for isolated test hits in local PCM WAVs.

This measures recorded alignment to a supplied reference, not monitoring latency.
Keep the original sample rate; never infer a DAW or hardware correction sign.
"""

import math
import os
from pathlib import Path
import statistics
import wave

from MCP_Server.mcp_adapter import raise_if_tool_cancelled

MAX_SECONDS = 120
MAX_FRAMES = 12_000_000


def _number(value, name, lower, upper):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not lower <= value <= upper):
        raise ValueError("%s must be a finite number between %s and %s" % (name, lower, upper))
    return float(value)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_edges(source, channel, end_seconds, threshold, separation_seconds):
    """Scan one channel in bounded chunks; require quiet before another edge."""
    before = _identity(source.stat())
    with source.open("rb") as handle:
        if _identity(os.fstat(handle.fileno())) != before:
            raise ValueError("Source file changed before analysis")
        try:
            reader = wave.open(handle, "rb")
        except (wave.Error, EOFError) as exc:
            raise ValueError("Timing analysis requires an uncompressed PCM WAV file") from exc
        with reader:
            channels, width, rate, total = (reader.getnchannels(), reader.getsampwidth(),
                                           reader.getframerate(), reader.getnframes())
            if (reader.getcomptype() != "NONE" or width not in (1, 2, 3, 4)
                    or not 1 <= channels <= 64 or not 8000 <= rate <= 192000 or total < 1):
                raise ValueError("Unsupported PCM WAV metadata")
            if channel > channels:
                raise ValueError("channel exceeds the WAV channel count")
            needed = int(math.ceil(end_seconds * rate)) + 1
            if needed > MAX_FRAMES:
                raise ValueError("Requested analysis exceeds the 12-million-frame limit")
            if needed > total:
                raise ValueError("WAV must cover the full final search window")
            stride, offset = channels * width, (channel - 1) * width
            quiet_needed = max(1, int(math.ceil(separation_seconds * rate)))
            quiet = 0
            position = 0
            edges = []
            starts_above = False
            clipped = 0
            peak = 0.0
            scale = float(1 << (8 * width - 1))
            while position < needed:
                raise_if_tool_cancelled()
                count = min(needed - position, max(1, 262144 // stride))
                block = reader.readframes(count)
                if len(block) != count * stride:
                    raise ValueError("Truncated WAV data")
                for frame in range(count):
                    start = frame * stride + offset
                    raw = block[start:start + width]
                    sample = (raw[0] - 128) if width == 1 else int.from_bytes(raw, "little", signed=True)
                    amplitude = abs(sample) / scale
                    peak = max(peak, amplitude)
                    if sample == -int(scale) or sample == int(scale) - 1:
                        clipped += 1
                    if amplitude >= threshold:
                        if position + frame == 0:
                            starts_above = True
                        if quiet >= quiet_needed:
                            edges.append((position + frame) / float(rate))
                        quiet = 0
                    else:
                        quiet += 1
                position += count
        if (_identity(os.fstat(handle.fileno())) != before
                or _identity(source.stat()) != before):
            raise ValueError("Source file changed during analysis; measure a finished recording")
    return edges, {"sample_rate": rate, "channels": channels, "sample_width_bits": width * 8,
                   "duration_seconds": total / float(rate), "analyzed_seconds": needed / float(rate),
                   "sample_resolution_ms": 1000.0 / rate, "source_identity": list(before),
                   "sample_peak_dbfs": 20 * math.log10(peak) if peak else None,
                   "full_scale_sample_count": clipped, "starts_above_threshold": starts_above}


def analyze_recording_timing(path: str, expected_onsets_seconds: list[float],
                             search_window_ms: float = 100.0, threshold_dbfs: float = -40.0,
                             min_separation_ms: float = 50.0, channel: int = 1,
                             skip_initial: int = 2) -> dict:
    window = _number(search_window_ms, "search_window_ms", 0.1, 1000) / 1000.0
    threshold_dbfs = _number(threshold_dbfs, "threshold_dbfs", -100, -1)
    separation = _number(min_separation_ms, "min_separation_ms", 1, 1000) / 1000.0
    if isinstance(channel, bool) or not isinstance(channel, int) or not 1 <= channel <= 64:
        raise ValueError("channel must be an integer between 1 and 64")
    if (not isinstance(expected_onsets_seconds, list)
            or not 3 <= len(expected_onsets_seconds) <= 400):
        raise ValueError("Supply 3..400 expected onset times in seconds relative to file start")
    expected = [_number(v, "expected onset", 0, MAX_SECONDS) for v in expected_onsets_seconds]
    if any(b - a <= max(2 * window, separation) for a, b in zip(expected, expected[1:])):
        raise ValueError("Expected times must increase with nonoverlapping search windows and separation")
    if expected[-1] + window > MAX_SECONDS:
        raise ValueError("Final search window must end within 120 seconds of file start")
    if (isinstance(skip_initial, bool) or not isinstance(skip_initial, int)
            or not 0 <= skip_initial <= len(expected) - 3):
        raise ValueError("skip_initial must leave at least three reference hits")
    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("path must identify a finished local PCM WAV file")
    edges, metadata = _read_edges(source, channel, expected[-1] + window,
                                 10 ** (threshold_dbfs / 20.0), separation)
    hits = []
    accepted = []
    for index, reference in enumerate(expected):
        matches = [edge for edge in edges if abs(edge - reference) <= window]
        # A truncated leading window cannot rule out an earlier hit before the file.
        boundary = reference - window < separation
        status = "boundary" if boundary else "matched" if len(matches) == 1 else "missing" if not matches else "ambiguous"
        offset = (matches[0] - reference) * 1000 if status == "matched" else None
        included = index >= skip_initial and status == "matched"
        if included:
            accepted.append(offset)
        hits.append({"index": index + 1, "expected_seconds": reference, "status": status,
                     "detected_seconds": matches[0] if status == "matched" else None,
                     "candidate_seconds": matches, "offset_ms": offset,
                     "excluded_startup": index < skip_initial, "included_in_statistics": included})
    enough = len(accepted) >= 3
    complete = all(hit["status"] == "matched" for hit in hits[skip_initial:])
    return {"status": "measured" if enough else "insufficient_data", "complete": complete,
            "file_path": str(source), **metadata, "channel": channel,
            "expected_hit_count": len(expected), "measured_hit_count": len(accepted),
            "skip_initial": skip_initial, "threshold_dbfs": threshold_dbfs,
            "search_window_ms": window * 1000, "min_separation_ms": separation * 1000,
            "median_offset_ms": statistics.median(accepted) if enough else None,
            "jitter_stddev_ms": statistics.stdev(accepted) if enough else None,
            "jitter_peak_to_peak_ms": max(accepted) - min(accepted) if enough else None,
            "minimum_offset_ms": min(accepted) if enough else None,
            "maximum_offset_ms": max(accepted) if enough else None, "hits": hits,
            "detected_edge_count": len(edges),
            "method": "First absolute-amplitude threshold crossing after continuous below-threshold separation; original sample rate",
            "interpretation": "Positive offset means the recorded threshold edge is later than the supplied reference. Jitter is spread of those offsets.",
            "limitations": ["Measures recorded alignment, not audible monitoring or hardware round-trip latency.",
                            "Attack shape, noise and threshold choice shift detected edges; inspect the hits and compare repeat captures with identical settings.",
                            "Use isolated repeated hits with silence between them, not vocals, chords or a full mix.",
                            "No automatic correction: MIDI clock, MIDI notes, Track Delay and hardware Sync Delay have different meanings and signs.",
                            "File identity uses filesystem metadata, not a byte hash. Source must not be actively recording."]}
