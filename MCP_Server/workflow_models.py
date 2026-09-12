"""Discoverable schemas for safe editing, comparison and audio inspection."""

from typing import Any
from pydantic import Field
from MCP_Server.result_models import OpenResult


class CommandStatus(OpenResult):
    request_id: str
    status: str
    response: dict[str, Any] | None = None
    message: str | None = None


class LatencyReport(OpenResult):
    status: str
    complete: bool
    session_id: str
    tracks: list[dict[str, Any]]
    master: dict[str, Any] | None
    findings: list[dict[str, Any]]
    read_errors: list[dict[str, Any]]


class MonitoringSetup(OpenResult):
    status: str
    plan_id: str
    session_id: str
    monitoring_path: str
    track_count: int
    tracks: list[dict[str, Any]]
    replayed: bool = False
    saved: bool = False


class RecordingTiming(OpenResult):
    status: str
    complete: bool
    file_path: str
    sample_rate: int
    channels: int
    measured_hit_count: int
    median_offset_ms: float | None
    jitter_stddev_ms: float | None
    jitter_peak_to_peak_ms: float | None
    hits: list[dict[str, Any]]


class EditTargets(OpenResult):
    session_id: str
    revision: str
    set_name: str = ""
    set_file_path: str = ""
    tracks: list[dict[str, Any]]


class TrackEdit(OpenResult):
    status: str
    target: dict[str, Any]
    session_id: str
    revision: str
    requested: dict[str, Any]
    result: dict[str, Any] | None = None


class ArrangementBuild(OpenResult):
    status: str
    plan_id: str
    session_id: str
    placements: list[dict[str, Any]]
    placement_count: int
    expires_in_seconds: int | None = None
    replayed: bool = False
    saved: bool = False
    warnings: list[str] = Field(default_factory=list)
    message: str | None = None
    rollback_verified: bool | None = None
    rollback_errors: list[str] = Field(default_factory=list)


class SessionSnapshot(EditTargets):
    schema_version: int
    captured_at: float
    tempo: float
    signature_numerator: int
    signature_denominator: int
    tracks_truncated: bool


class SnapshotComparison(OpenResult):
    session_id: str
    complete: bool
    added: list[dict[str, Any]]
    removed: list[dict[str, Any]]
    changed: list[dict[str, Any]]
    song_changes: dict[str, Any]


class AudioAnalysis(OpenResult):
    file_path: str
    duration_seconds: float | None
    sample_rate: int
    channels: int
    analyzed_seconds: float
    analysis_complete: bool
    sample_peak_dbfs: float | None
    rms_dbfs: float | None
    below_silence_threshold: bool
    possible_clipping: bool
    method: str


class ExportVerification(OpenResult):
    operation_id: str | None = None
    recording_status: str | None = None
    outputs: list[dict[str, Any]] = Field(default_factory=list)
    all_files_measured: bool
