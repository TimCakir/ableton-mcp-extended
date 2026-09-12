"""Bounded PCM WAV metadata reads for file-placement previews.

Only headers and one final sample frame are read. No decoding, external process
or full-file hash runs on Live's execution thread. Filesystem identity guards
detect ordinary edits/replacements; they are not a cryptographic content check.
"""

import os
import stat
import struct


MAX_HEADER_BYTES = 65536
MAX_READ_CALLS = 512


def _identity(path, resolved, info):
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise ValueError("Audio source must be a nonempty regular PCM WAV file")
    return {"path": path, "real_path": resolved, "device": info.st_dev,
            "inode": info.st_ino, "size_bytes": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


class _BoundedReader:
    def __init__(self, stream, size):
        self.stream, self.size = stream, size
        self.bytes_read, self.read_calls = 0, 0

    def read(self, count):
        self.read_calls += 1
        if (count < 0 or count > MAX_HEADER_BYTES - self.bytes_read
                or self.read_calls > MAX_READ_CALLS):
            raise ValueError("WAV metadata exceeds the bounded header-read limit")
        data = self.stream.read(count)
        self.bytes_read += len(data)
        return data

    def tell(self):
        return self.stream.tell()

    def seek(self, offset, whence=0):
        position = offset if whence == 0 else self.tell() + offset if whence == 1 else self.size + offset
        if whence not in (0, 1, 2) or not 0 <= position <= self.size:
            raise ValueError("WAV chunk extends beyond the source file")
        return self.stream.seek(position)


def _pcm_metadata(reader):
    header = reader.read(12)
    if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
        raise ValueError("File placement supports RIFF PCM WAV files")
    riff_end = struct.unpack("<I", header[4:8])[0] + 8
    if not 12 <= riff_end <= reader.size:
        raise ValueError("WAV chunk extends beyond the source file")
    format_info, sample_data = None, None
    while reader.tell() < riff_end:
        if riff_end - reader.tell() < 8:
            raise ValueError("WAV has an incomplete chunk header")
        chunk = reader.read(8)
        if len(chunk) != 8:
            raise ValueError("WAV chunk header is truncated")
        kind, size = struct.unpack("<4sI", chunk)
        start, end = reader.tell(), reader.tell() + size
        if end > riff_end:
            raise ValueError("WAV chunk extends beyond the source file or RIFF boundary")
        if kind == b"fmt ":
            if format_info is not None or size < 16:
                raise ValueError("WAV requires one complete PCM format chunk")
            raw = reader.read(16)
            if len(raw) != 16:
                raise ValueError("WAV format chunk is truncated")
            tag, channels, rate, byte_rate, alignment, bits = struct.unpack("<HHIIHH", raw)
            if tag != 1 or channels not in (1, 2) or bits not in (8, 16, 24, 32):
                raise ValueError("File placement supports mono/stereo 8/16/24/32-bit PCM WAV files")
            width = bits // 8
            if rate <= 0 or alignment != channels * width or byte_rate != rate * alignment:
                raise ValueError("PCM WAV sample rate, block alignment or byte rate is invalid")
            format_info = (channels, width, rate, alignment)
        elif kind == b"data":
            if sample_data is not None or format_info is None:
                raise ValueError("WAV requires one data chunk after its PCM format chunk")
            if not size or size % format_info[3]:
                raise ValueError("PCM WAV data must contain complete nonempty sample frames")
            sample_data = (start, size)
        # Ancillary chunk payloads are skipped. Permit an unpadded final odd
        # chunk, as emitted for mono 8-bit PCM by Python's standard WAV writer.
        reader.seek(min(end + (size & 1), riff_end))
    if format_info is None or sample_data is None:
        raise ValueError("PCM WAV format or sample data is missing")
    channels, width, rate, alignment = format_info
    start, size = sample_data
    reader.seek(start + size - alignment)
    if len(reader.read(alignment)) != alignment:
        raise ValueError("PCM WAV sample data is truncated")
    return channels, width, rate, size // alignment


def probe_file(path):
    """Return verified file identity and exact PCM sample-count duration."""
    if not isinstance(path, str) or not path or not os.path.isabs(path) or "\x00" in path:
        raise ValueError("file_path must be an absolute local PCM WAV path")
    try:
        resolved = os.path.realpath(path)
        before = _identity(path, resolved, os.stat(resolved))
        # Nonblocking open prevents a raced replacement with a FIFO from hanging
        # Live. fstat then confirms the opened descriptor is the same regular file.
        descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = _identity(path, resolved, os.fstat(stream.fileno()))
            if opened != before:
                raise ValueError("Audio file changed while opening it")
            reader = _BoundedReader(stream, before["size_bytes"])
            channels, width, rate, frames = _pcm_metadata(reader)
            after_descriptor = _identity(path, resolved, os.fstat(stream.fileno()))
        current_resolved = os.path.realpath(path)
        after = _identity(path, current_resolved, os.stat(current_resolved))
        if before != after or before != after_descriptor:
            raise ValueError("Audio file changed while reading its metadata")
    except (OSError, EOFError, struct.error) as exc:
        raise ValueError("Cannot read an uncompressed PCM WAV source: " + str(exc))
    return {"file_identity": before, "sample_rate": rate, "sample_frames": frames,
            "channels": channels, "sample_width": width, "format": "pcm",
            "duration_seconds": frames / float(rate)}
