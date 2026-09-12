# Release 1.6.0

Build: `2026-09-08.8` · Remote protocol: `2.1` · 155 top-level tools

Three new tools help diagnose latency, configure monitoring and measure recorded
test-hit alignment. The [latency guide](LATENCY-GUIDE.md) explains how to use them
for TR-1000 timing, vocals and guitars, including the controls managed outside MCP.

## Inspect the monitoring path

Call `get_edit_targets`, then `get_latency_report`:

```json
{
  "session_id": "<current Set ID>",
  "track_handles": ["<vocal handle>", "<guitar handle>", "<TR kick handle>"]
}
```

Omit `track_handles` to inspect all tracks. A subset also includes parent groups,
all returns and Main. The report includes monitoring state, arm state, input and
output names, nested rack chains, each device's reported samples/milliseconds and
read errors. Direct-chain sums count only top-level devices, including inactive
devices: Live retains latency when the yellow activator is switched off. Nested
children are an inventory and are never added to their parent rack again.

These totals are **device reports, not total monitoring or physical round-trip
measurements**. No routing graph total is inferred. Missing properties remain
null; unavailable fields and traversal limits produce `complete=false` and
`status="incomplete"`. Manual audio settings remain explicitly unobserved, rather
than being guessed from the audio file or another session. Limits are 256 tracks,
512 devices, 512 chains overall, 64 chains per device collection and depth 8.

## Preview, apply and restore monitoring

```json
{
  "action": "preview",
  "session_id": "<current Set ID>",
  "track_handles": ["<vocal handle>", "<guitar handle>"],
  "monitoring_path": "live"
}
```

`live` targets Monitor Auto. `direct` targets Monitor Off, for an interface/Console
monitoring path that is already set up separately. Auto monitoring still depends
on track arming and clip playback. The tool does not arm tracks, choose ports,
change gains or devices, save the Set, or control buffer/clock preferences.

Inspect the preview, then call:

```json
{"action":"apply","session_id":"<same Set ID>","plan_id":"<preview ID>"}
```

Poll the same action and plan while `status="applying"`. An `applied` result
requires next-tick readback. To return to the original monitoring states:

```json
{"action":"restore","session_id":"<same Set ID>","plan_id":"<same preview ID>"}
```

Poll while `restoring`; successful completion is `restored`. Retained results
replay without more writes. Plans support 1–32 regular, unfrozen audio tracks;
all targets are checked before writes. Recording or changed Set, track, arm,
routing or monitoring context blocks the operation. Apply must start within
five minutes of preview. Restore is available while the plan survives in the
32-plan process-local cache; a script restart or cache eviction removes it.

Only one monitoring operation may be pending. A 30-second deadline is checked
by callbacks and polls. Failure attempts rollback only where the tool still owns
the exact monitoring state and context. `error` with `rollback_verified=true`
means the pre-operation state was verified; `partial` requires inspection.

## Measure a finished test recording

```json
{
  "path": "/absolute/path/to/finished-kick-test.wav",
  "expected_onsets_seconds": [0.5, 1.0, 1.5, 2.0, 2.5],
  "search_window_ms": 100,
  "threshold_dbfs": -40,
  "min_separation_ms": 50,
  "channel": 1,
  "skip_initial": 2
}
```

The reference times are supplied by the caller and are relative to file start.
Detection uses the first absolute-amplitude threshold crossing after a continuous
below-threshold quiet interval. Use repeated isolated hits, not a full song or
vocal performance. Attack shape, noise and threshold affect the detected edge.

Positive `median_offset_ms` means the recorded edge is late. `jitter_stddev_ms`
is the sample standard deviation; `jitter_peak_to_peak_ms` is the offset range.
Startup, missing, ambiguous and leading-boundary hits are explicitly listed and
excluded. At least three accepted hits are needed for statistics, otherwise
`status="insufficient_data"` and statistics are null. A measured result can still
be incomplete if some references were missed. There is no automatic correction:
clock delay, note response, Track Delay and hardware Sync Delay differ.

Reads finished integer PCM WAV files with 8/16/24/32-bit samples, 8–192 kHz and
1–64 channels; one channel is selected without resampling. Other encodings and
formats are not promised. No FFmpeg or new dependency is required. Supply 3–400
increasing reference times with nonoverlapping search windows. The final window
must fit the file and its first 120 seconds; at most 12 million frames are scanned.
File identity is checked before and after reading using filesystem metadata,
not a cryptographic content hash. The tool never connects to Live.

## Verification

The full automated suite passed 1,196 tests in 12.43 seconds, including 50 latency
report cases, 88 monitoring setup cases, 24 timing-analysis cases and SDK contract
coverage. Independent review found no remaining blockers. Actual Live 12.4.5
acceptance verified full/subset reports, Auto apply/readback, Off restore/readback,
retained replay, unchanged routing and Set metadata, and saved/restored persistence.
The detector also passed a known synthetic signal through stdio and read the
retained TR test WAVs. See the [acceptance report](LIVE-ACCEPTANCE-1.6.0-2026-09-08.md).

Live is running the final `.8` source. Fresh acceptance MCP processes matched it;
the existing Codex MCP process needs a host refresh to load the new tools.

The Python package and Live Remote Script remain separate installations.
This release does not establish hardware monitoring feel, current TR clock
calibration, a published release, remote CI, or Live 11 compatibility.
