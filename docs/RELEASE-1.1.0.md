# Release 1.1.0

Build: `2026-09-08.1` · Remote protocol: `2.1`

This release adds workflows for stable track editing, MIDI arrangement placement, session comparison and recorded-file inspection. It repairs failures identified in the [September 3 review](MCP-REVIEW-2026-09-03.md) and real Live testing on September 8. The matching Remote Script was deployed and loaded in Live 12.4.5; a fresh stdio MCP server exposed 152 tools. Short recording, arrangement and save/reopen checks passed in a disposable Set. See the [acceptance report](LIVE-ACCEPTANCE-2026-09-08.md) for the exact scope.

## Eight new tools

| Tool | Result |
| --- | --- |
| `get_edit_targets` | Set identity, revision and stable handles for regular and return tracks |
| `edit_track` | Preview or apply a supported mixer, state or name change by handle |
| `build_arrangement` | Preview then place session MIDI clips on their own tracks with source and overlap checks |
| `get_session_snapshot` | Structured session metadata, with explicit truncation and read errors |
| `compare_session_snapshots` | Added, removed and changed tracks plus selected song changes |
| `get_command_status` | The retained state and response for a request ID |
| `analyze_audio_file` | Local-file audio metadata, sample peak, RMS and level flags |
| `verify_export_outputs` | The latest recording manifest with file-analysis results |

## Stable track edit: preview and apply

Call `get_edit_targets` with `{}`. Select the intended track from the returned list by inspecting its name and kind, then retain its actual `track_handle`, the `session_id` and optionally the `revision`. Do not invent handles or reuse them across Sets.

For example, submit these arguments to `edit_track`, replacing the values in angle brackets with returned values:

```json
{
  "session_id": "<returned session_id>",
  "track_handle": "<returned track_handle>",
  "property_name": "volume",
  "value": 0.7,
  "expected_revision": "<returned revision>",
  "preview": true
}
```

Inspect the returned target and requested change. Apply that change with the same arguments and `"preview": false`. The handle is resolved again on Live's execution tick, so a preceding insertion or reorder does not silently redirect the edit to another track.

`expected_revision` is optional. Supplying it rejects changes after any track reorder, addition, deletion or rename; omitting it allows the handle to follow the original track through those structural changes. Refresh discovery after a rejection. Handles expire when their track is deleted, the Set changes, or the Remote Script restarts.

Supported properties:

| Property | Value |
| --- | --- |
| `volume`, `send` | Number from 0 to 1; `send` also requires a 1-based `send_index` |
| `panning` | Number from -1 to 1 |
| `mute`, `solo`, `arm` | Boolean |
| `name` | Nonempty text, up to 1024 characters |

An `applied` result reports an in-memory Live edit. Save and reopen a disposable Set when verifying persistence. Stable handles currently apply to this track-edit workflow; legacy tools that accept indexes still require fresh discovery before structural edits.

## Build a MIDI arrangement

Call `get_edit_targets`, inspect the intended MIDI track, then preview explicit placements:

```json
{
  "action": "preview",
  "session_id": "<returned session_id>",
  "placements": [
    {"track_handle": "<returned handle>", "source_slot": 1, "destination_beat": 0},
    {"track_handle": "<returned handle>", "source_slot": 1, "destination_beat": 4}
  ]
}
```

This example requires a four-beat source and empty destination ranges. Slots are 1-based; destinations are zero-based quarter-note beats. Every copy uses the source's current length. Inspect the preview, then call `build_arrangement` with `action="apply"`, its `session_id` and `plan_id`, omitting placements.

Plans retain actual Live objects and expire after five minutes. Apply rechecks source identity, clip metadata, exposed MIDI note values and all destination ranges on Live's execution tick. It rejects overlaps, changed/replaced sources, clip envelopes, frozen/group/return tracks and active recording jobs. Limits are 64 placements, 10,000 source notes and 32 retained plans. Per-note expression is not inspected. This first version supports same-track session MIDI copies; audio placement and custom copy lengths are separate work.

Repeated apply of a retained completed plan returns its result with `replayed=true`, without new copies. An unknown plan is not evidence that it never ran. Copied notes and bounds are read back. Failure removes only copies owned by that operation, with `error` for verified rollback or `partial` when rollback cannot be verified. It does not start playback or save the Set.

## Compare a session before and after an edit

1. Call `get_session_snapshot` and retain the returned object as `before`.
2. Make the intended edit.
3. Call `get_session_snapshot` again and retain the object as `after`.
4. Call `compare_session_snapshots` with `{"before": before, "after": after}` using those actual objects.

The comparison uses track handles and reports selected song fields, added/removed tracks and changed track metadata. Snapshots contain mixer, routing, device names/classes and clip metadata. They are capped at 256 tracks and 256 clips per track, with explicit truncation/read-error fields. Check the comparison's `complete` value before treating an empty diff as evidence of no change.

A snapshot is not a Set backup: it does not capture every device parameter, audio sample, MIDI note or automation point. Comparison requires the same loaded Set instance; snapshots taken across a script restart cannot be joined by their expired handles.

## Diagnose loaded code and uncertain commands

Run `get_build_info` first after an upgrade. It compares the server build, repository Remote Script build, loaded Remote Script build, wire protocol and source hashes. Package hashes cover all Python modules, including helpers, and distinguish loaded files from current disk files. `match`, `mismatch`, `unknown` and `unreachable` are distinct outcomes. Follow the returned `recovery` steps; copying files alone does not reload Live.

The transport now sends a request ID with every command. If a call reports `outcome=running` or `outcome=unknown`, retain the ID and call:

```json
{
  "request_id": "<request_id from the error>"
}
```

Pass these arguments to `get_command_status`. A completed request includes its retained response, which can itself be a success or an error. Queued requests that expire before execution do not subsequently mutate the Set. A running request may finish after the original wait ends.

The registry is bounded and lives in the Remote Script process. An `unknown` result may mean the request was never received, its result was evicted, or the script restarted. It does not prove the edit was unapplied. Inspect the Set before issuing another edit; the client never automatically replays an uncertain write.

## Record and inspect exported files

`bounce_to_audio`, `export_stems`, `freeze_track`, `record_over_range`, `capture_session_to_arrangement` and `record_arrangement_automation` share one recording-job lifecycle. Starts return an operation ID; status and cancellation return structured objects.

For a mix capture in a disposable Set:

1. Inspect the Set, routing and intended arrangement range.
2. Call `bounce_to_audio` with `{"from_bar": 1, "to_bar": 5, "source": "Resampling", "name": "MCP verification bounce"}`. The end bar is exclusive, and recording runs in real time.
3. Retain the returned `operation_id`, then poll `get_automation_record_status` with `{}`. Confirm it still refers to that operation. A `finalizing` status means output verification is still pending.
4. Inspect the terminal status and every expected output. `done` indicates the recording job completed; cancelled or failed jobs retain partial/missing output information.
5. Call `verify_export_outputs` with `{"analysis_seconds": 60}` to inspect available recorded files. It does not start playback and will not analyze files while a job is active.
6. Listen to the capture and verify saved-Set behavior separately.

Each file-producing job's manifest includes source and recording-track identities, requested/captured bounds, output status and file path/existence information. Available verified files also include file size and media type. `verify_export_outputs` attaches duration, channel count, sample rate, level flags and an explicit per-file analysis status. `all_files_measured` only means all listed files were measured; it does not establish a complete or musically correct capture.

The job restores captured transport and arm state on completion, cancellation and error where the owning Set and objects remain available. It retains object references rather than restoring by shifted track indexes. A second recording job cannot start while one is active, and structural edits that could invalidate a job are guarded. Cancellation stops the job; material already recorded remains in the Set.

These tools record through Live's transport. They do not provide offline file export. `freeze_track` performs a bounce and optionally switches off the original after output verification; it does not call native Freeze/Flatten or disable a device chain.

## Audio measurement limits

`analyze_audio_file` accepts a local file path and requires `ffmpeg` and `ffprobe` on the MCP process's `PATH`. `analysis_seconds` defaults to 60 and must be greater than zero and at most 600. Measurement starts at the beginning of the first audio stream. The result distinguishes the file's total duration from the analyzed duration and reports `analysis_complete`.

Levels come from FFmpeg `volumedetect`: sample peak and RMS with 16-bit measurement and 0.1 dB precision, not true peak or LUFS. A peak at or below the configured silence threshold (default -80 dBFS) sets a listening flag; a peak at or above -0.1 dBFS sets `possible_clipping`. Neither flag proves an export is wrong. Material outside the analyzed segment is not measured.

## Reliability changes

- **Connection handling:** bounded connection/response waits, 8 MiB frames, UTF-8 fragmentation support, request correlation, serialized socket ownership and failed-socket cleanup. Blocking domain work runs outside the MCP event loop.
- **Request lifecycle:** queued-write expiry, duplicate-request handling and retained outcomes. Shutdown closes accepted clients and prevents queued edits from starting.
- **Batches and targets:** all children share the batch deadline; subsequent edits are skipped after expiry. Concurrent clients share one synchronized handle registry, and return tracks support handle-based renaming. Raw `call_lom` batches recognize explicit `set_value`, including zero and false; raw deletion requests must provide a target index.
- **Recording jobs:** preflight before side effects, one active owner, Set/object identity checks, interruption handling, shared cleanup and deferred output manifests.
- **Live proxy identity:** recording checks compare the underlying Live entity across fresh Python wrappers. This fixes a real bounce failure that ordinary object mocks did not reproduce.
- **Snapshot track kinds:** group and return tracks report complete empty clip lists instead of errors from reading unsupported arrangement collections.
- **MIDI edits:** finite-value validation before mutation, compensation for partial failures, and explicit rollback errors when restoration also fails. Compensation is not a transactional guarantee across Live failures.
- **Note scope:** default note reads use all stored notes where the API supports it, including notes outside the loop; older APIs return a marker-bound fallback and scope warning. Clip-region lengths use the marker span. Humanization is deterministic but repeated calls accumulate changes to current note times.
- **MCP results:** real execution errors are surfaced with MCP error status, including partial batch failures. Read-only tool annotations are explicit; unknown or mixed tools receive conservative hints.
- **Setup:** SDK 1.x compatibility bound, a dependency lockfile, development test extra, package installation fixes, configuration merging with backup/atomic replacement, truthful deployment output and an optional UDP companion that does not compete for the primary TCP port.
- **Bundled ElevenLabs startup:** declares its missing filename-matching dependency and keeps startup logging off the MCP stdout stream. A real stdio initialization/discovery test uses a dummy key and makes no API calls.
- **Metering cancellation:** cancelling `measure_section` stops future sampling and prevents its delayed final stop command. Playback already started remains running; use `stop_playback` explicitly if needed.

## Response and compatibility changes

Integrations that parse formatted text need updating for these structured result families:

| Tools | Fields to consume |
| --- | --- |
| `get_clip_notes` | `notes`, `total`, `offset`, `limit`, `returned`, `next_offset`, scope metadata |
| Note-edit tools | Mutation counts and scope/rollback information where applicable |
| Recording start/status/cancel | `status`, `operation_id`, progress, bounds and `outputs` |
| `batch` | Counts, per-command results, skipped commands and partial/error status |
| `get_build_info` | Agreement status, builds, protocol, hashes and recovery instructions |
| Eight new workflow tools | Their advertised output schemas |

`get_clip_notes` defaults to 200 notes per page and accepts limits from 1 to 2000. Continue with the returned `next_offset` until it is null. Each page reads current Live state, so do not edit between pages. Pagination occurs after retrieval from Live and does not bypass the transport's frame-size limit for extremely large note collections.

The Python SDK is constrained to `mcp[cli]>=1.28.1,<2`; SDK 2 migration is separate work. Protocol 2.1 tolerates legacy unframed replies, but old Remote Scripts cannot provide the new safety and status guarantees. Install matching components rather than relying on that fallback.

## Verification and remaining work

Verified on September 8, 2026: **586 tests pass** with MCP 1.28.1 in the development environment, and **586 pass** with MCP 1.30.0 in the temporary environment installed from `uv.lock` (Python 3.14.6). CI is configured for Python 3.10 and 3.14; remote CI has not been observed for these uncommitted changes.

Local regression coverage exercises scheduled recording ticks, interruption and cancellation, partial note failures, socket framing/deadlines, request reconciliation, stable track identities, structured MCP contracts, setup preservation and audio analysis. A generated WAV provides a known signal for the FFmpeg measurement check when those executables are installed. The installable wheel was built and its 16 Python files compared byte-for-byte with the source.

The [Live acceptance report](LIVE-ACCEPTANCE-2026-09-08.md) records matching loaded package hashes, successful bounce/stem files, cancellation and restart, guarded arrangement copying, MIDI preservation and save/reopen verification. Real-time capture stops on a scheduled tick and can extend slightly beyond the requested end; the manifest reports actual captured bounds. File measurement verifies signal and file integrity, not a subjective listening verdict.

Native Freeze, comping, per-clip scale controls and SDK 2 migration are not implemented. Hardware routing, long recordings, Live 11 and every existing legacy command remain outside this acceptance run. The review report is historical evidence; its defect-reproduction scripts assert old behavior and are not the release acceptance suite. Use current unit tests and the opt-in `tests/integration/live_acceptance.py` against a disposable fixture Set.
