"""File previews reject unverifiable data without reading full audio files."""

import importlib.util
import io
import os
from pathlib import Path
import struct
import wave

import pytest


_PATH = Path(__file__).parents[2] / "AbletonMCP_Remote_Script" / "audio_file_source.py"
_SPEC = importlib.util.spec_from_file_location("audio_file_source", _PATH)
files = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(files)


def wav(path, channels=2, width=2, rate=44100, frames=100):
    with wave.open(str(path), "wb") as output:
        output.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        output.writeframes(b"\x00" * frames * channels * width)
    return path


@pytest.mark.parametrize("channels", [1, 2])
@pytest.mark.parametrize("width", [1, 2, 3, 4])
def test_pcm_metadata_uses_sample_count_without_modifying_file(tmp_path, channels, width):
    path = wav(tmp_path / "source.wav", channels, width, 48000, 123)
    before = path.read_bytes()
    result = files.probe_file(str(path))
    assert result["duration_seconds"] == 123 / 48000
    assert result["sample_frames"] == 123 and result["sample_rate"] == 48000
    assert result["channels"] == channels and result["sample_width"] == width
    assert result["format"] == "pcm" and path.read_bytes() == before
    identity = result["file_identity"]
    assert identity["path"] == identity["real_path"] == str(path)
    assert identity["size_bytes"] == len(before)


@pytest.mark.parametrize("value", [None, True, 1, {}, [], "", "relative.wav", "/tmp/null\x00.wav"])
def test_invalid_paths_rejected(value):
    with pytest.raises(ValueError, match="absolute local"):
        files.probe_file(value)


@pytest.mark.parametrize("kind", ["missing", "empty", "directory", "not_wav", "float", "multichannel", "zero_frames", "truncated"])
def test_unusable_files_rejected(tmp_path, kind):
    path = tmp_path / "source.wav"
    if kind == "directory":
        path.mkdir()
    elif kind == "empty":
        path.touch()
    elif kind == "not_wav":
        path.write_bytes(b"not a wave file")
    elif kind != "missing":
        wav(path, channels=3 if kind == "multichannel" else 2, frames=0 if kind == "zero_frames" else 100)
        data = bytearray(path.read_bytes())
        if kind == "float":
            data[20:22] = struct.pack("<H", 3)
        elif kind == "truncated":
            data = data[:-1]
        path.write_bytes(data)
    with pytest.raises(ValueError):
        files.probe_file(str(path))


def test_file_replacement_and_same_size_write_change_identity(tmp_path):
    path = wav(tmp_path / "source.wav")
    initial = files.probe_file(str(path))
    replacement = wav(tmp_path / "replacement.wav")
    replacement.replace(path)
    second = files.probe_file(str(path))
    assert initial["file_identity"] != second["file_identity"]
    data = bytearray(path.read_bytes())
    data[-1] = 1
    path.write_bytes(data)
    os.utime(path, ns=(path.stat().st_atime_ns, second["file_identity"]["mtime_ns"] + 1000000))
    assert second["file_identity"] != files.probe_file(str(path))["file_identity"]


def test_symlink_target_replacement_changes_identity(tmp_path):
    first = wav(tmp_path / "first.wav")
    second = wav(tmp_path / "second.wav")
    path = tmp_path / "link.wav"
    path.symlink_to(first)
    before = files.probe_file(str(path))
    path.unlink()
    path.symlink_to(second)
    assert before["file_identity"] != files.probe_file(str(path))["file_identity"]


def test_file_change_during_header_read_is_detected(tmp_path, monkeypatch):
    path = wav(tmp_path / "source.wav")
    original = files._BoundedReader.read
    changed = []
    def read(reader, count):
        result = original(reader, count)
        if not changed:
            changed.append(True)
            with path.open("ab") as output:
                output.write(b"changed")
        return result
    monkeypatch.setattr(files._BoundedReader, "read", read)
    with pytest.raises(ValueError, match="changed"):
        files.probe_file(str(path))


def test_large_audio_reads_only_headers_and_final_frame(tmp_path, monkeypatch):
    path = wav(tmp_path / "source.wav", frames=1000000)
    reads = []
    original = files._BoundedReader.read
    def read(reader, count):
        result = original(reader, count)
        reads.append(len(result))
        return result
    monkeypatch.setattr(files._BoundedReader, "read", read)
    assert files.probe_file(str(path))["sample_frames"] == 1000000
    assert sum(reads) < 100 and max(reads) <= 16


def test_header_chunk_flood_is_bounded(tmp_path):
    path = wav(tmp_path / "source.wav")
    data = path.read_bytes()
    extra = (b"JUNK" + struct.pack("<I", 0)) * (files.MAX_READ_CALLS + 1)
    data = b"RIFF" + struct.pack("<I", len(data) - 8 + len(extra)) + b"WAVE" + extra + data[12:]
    path.write_bytes(data)
    with pytest.raises(ValueError, match="bounded"):
        files.probe_file(str(path))


def test_chunk_extending_beyond_file_is_rejected(tmp_path):
    path = tmp_path / "source.wav"
    path.write_bytes(b"RIFF" + struct.pack("<I", 1000000) + b"WAVEJUNK" + struct.pack("<I", 999999))
    with pytest.raises(ValueError, match="beyond"):
        files.probe_file(str(path))


@pytest.mark.parametrize("offset,encoding,value", [
    (40, "I", 401), (34, "H", 12), (32, "H", 1), (28, "I", 1), (24, "I", 0),
])
def test_inconsistent_pcm_headers_are_rejected(tmp_path, offset, encoding, value):
    path = wav(tmp_path / "source.wav")
    data = bytearray(path.read_bytes())
    struct.pack_into("<" + encoding, data, offset, value)
    path.write_bytes(data)
    with pytest.raises(ValueError):
        files.probe_file(str(path))


@pytest.mark.parametrize("kind", [b"fmt ", b"data"])
def test_duplicate_audio_chunks_are_rejected(tmp_path, kind):
    path = wav(tmp_path / "source.wav")
    data = path.read_bytes()
    extra = data[12:36] if kind == b"fmt " else data[36:]
    data = data[:4] + struct.pack("<I", len(data) - 8 + len(extra)) + data[8:] + extra
    path.write_bytes(data)
    with pytest.raises(ValueError, match="one"):
        files.probe_file(str(path))


def test_ancillary_chunks_are_skipped_without_reading_payload(tmp_path, monkeypatch):
    path = wav(tmp_path / "source.wav")
    data = path.read_bytes()
    extra = b"JUNK" + struct.pack("<I", 100000) + b"\x00" * 100000
    path.write_bytes(data[:4] + struct.pack("<I", len(data) - 8 + len(extra)) + data[8:] + extra)
    monkeypatch.setattr(files, "MAX_HEADER_BYTES", 128)
    assert files.probe_file(str(path))["sample_frames"] == 100


def test_metadata_reader_rejects_unbounded_or_oversized_reads():
    reader = files._BoundedReader(io.BytesIO(b"123456"), 6)
    for count in (-1, files.MAX_HEADER_BYTES + 1):
        with pytest.raises(ValueError, match="bounded"):
            reader.read(count)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO guard")
def test_fifo_is_rejected_before_open(tmp_path):
    path = tmp_path / "source.wav"
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular"):
        files.probe_file(str(path))
