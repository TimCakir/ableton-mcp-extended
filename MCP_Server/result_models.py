"""Structured results for operation tracking and diagnostics."""

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class OpenResult(BaseModel):
    model_config = ConfigDict(extra="allow")


class RecordingStatus(OpenResult):
    status: str
    operation_id: str | None = None
    progress: float | None = None
    requested: dict[str, float] | None = None
    captured: dict[str, float] | None = None
    outputs: list[dict[str, Any]] = Field(default_factory=list)


class BatchItem(OpenResult):
    index: int
    command: str
    status: Literal["success", "error", "skipped"]
    result: Any = None
    message: str | None = None


class BatchResult(OpenResult):
    status: Literal["success", "partial", "error"]
    total: int
    ran: int
    succeeded: int
    failed: int
    skipped: int
    stopped_early: bool
    results: list[BatchItem]


class BuildInfo(OpenResult):
    status: Literal["match", "mismatch", "unknown", "unreachable"]
    server_build: str
    repo_build: str | None = None
    remote_script_build: str | None = None
    protocol_version: str | None = None
    server_protocol_version: str
    remote_source_sha256: str | None = None
    repo_source_sha256: str | None = None
    server_loaded_source_sha256: str | None = None
    server_disk_source_sha256: str | None = None
    remote_package_sha256: str | None = None
    remote_disk_package_sha256: str | None = None
    repo_package_sha256: str | None = None
    server_loaded_package_sha256: str | None = None
    server_disk_package_sha256: str | None = None
    live_version: str | None = None
    set_name: str | None = None
    set_file_path: str | None = None
    recovery: list[str] = Field(default_factory=list)
    message: str | None = None


class NotePage(OpenResult):
    notes: list[dict[str, Any]]
    total: int
    offset: int
    limit: int
    returned: int
    next_offset: int | None = None
    read_scope: str | None = None
    query_bounds: dict[str, float] | None = None
    scope_warning: str | None = None


class NoteEditResult(OpenResult):
    clip_name: str | None = None
    read_scope: str | None = None
    query_bounds: dict[str, float] | None = None
    scope_warning: str | None = None
    modified: int | None = None
    notes_added: int | None = None
    removed: int | None = None
    remaining: int | None = None
    removed_notes: list[dict[str, Any]] | None = None
