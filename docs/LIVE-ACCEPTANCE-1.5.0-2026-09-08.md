# Live acceptance: direct WAV placement 1.5.0

Date: September 8, 2026. Live 12.4.5. Package 1.5.0, build `2026-09-08.7`,
protocol 2.1. The full automated suite passed **1,031 tests in 12.38 seconds**,
including 95 file-planner cases and 40 bounded WAV-reader cases. Independent
review found no remaining blockers. Both Live phases passed using fresh stdio
servers with matching server, loaded Remote Script and repository hashes.
Discovery returned 152 tools and the updated file-placement description.

## Fixture

All edits use the existing disposable Set:

```text
dist/live-acceptance-2026-09-08/MCP Acceptance 2026-09-08 Project/MCP Acceptance 2026-09-08.als
```

The destination is `MCP QA CROSS AUDIO`, which retains its two verified 1.4.0
arrangement clips. Its 108 Session slots start empty. The Set has 37 audio/MIDI
tracks, one group and three returns, at 110 BPM, 4/4, with playback and recording off.

Both local source files are stereo 16-bit PCM WAV at 44.1 kHz, with 529,200
sample frames and 12-second duration. Their existing `.wav.asd` analysis files
remain in place. The runner independently records source WAV hashes and file
metadata before and after the test.

| Verified copy | Local file | Source seconds | Arrangement beats | Reserved through |
| --- | --- | --- | --- | --- |
| MCP FILE EXCERPT | source-a-220hz.wav | 2–5 | 256.5–262 | 278.5 |
| MCP FILE FULL | source-b-660hz.wav | 0–12 | 288.25–310.25 | 310.25 |

Arrangement positions are absolute zero-based quarter-note beats. Imports are
unwarped, nonlooping, unpitched, unmuted and at unity clip gain. Existing track
devices, mixer state and routing still apply.

## Initial failure and reconciliation

The first actual Live run reached file staging and copying, then failed while
inserting the second file. It reported `Existing arrangement material was removed
or its bounds changed`, with one completed placement before failure. The retained
plan returned `error`, `rollback_verified=true` and `staging_cleaned=true`.

A separate read-only reconciliation confirmed every track's snapshot metadata
matched the pre-edit baseline, both earlier arrangement clips retained their
bounds, all 108 destination Session slots were empty and transport/recording
remained off. The failed result was retained rather than retried blindly.

The cause was an intermediate guard treating an earlier operation-owned copy as
pre-existing material. A second native insertion can settle the first unwarped
copy's pending edge during the same Live tick. The fix preserves exact checks on
pre-existing clips, checks owned copies against their identity and reserved range
during settling, then requires exact final bounds and content. Five regression
cases cover valid settling, original damage, an escaped reservation, a wrong
final edge and a deleted owned copy.

## Successful final results

Workflow acceptance passed at **19:40:53 Istanbul time**. Both copies matched
the expected bounds, markers, file paths, sample count/rate, gain, pitch, mute and
warp state. Preview made no edits. Invalid ranges/units, mixed file/Session
plans, wrong destination type, missing files and overlapping reservations were
rejected. Apply returned `applying`, then `applied` after copy verification and
temporary-clip cleanup. Replaying the retained plan added no duplicates.

Whole-Set snapshots showed exactly the two expected additions. Original track,
device, routing and clip metadata remained unchanged. Direct reads confirmed the
earlier destination clips' audio properties, and all 108 Session slots remained
empty. Both source WAV files retained their bytes and filesystem metadata.

The Set was saved through Live, unloaded into Untitled and reopened from Live's
recent-files menu. Read-only persistence acceptance passed at **19:42:24**, with
a different Set session ID. Both copy readbacks, original material, source files,
empty staging slots and stopped transport matched the successful workflow.

## Evidence

Runner: `tests/integration/audio_file_acceptance.py`. It checks exact Set path,
loaded build/source hashes and the discovered tool description. It writes every
request before dispatch and records its response. Persistence requires a passed
workflow evidence file and a fresh Set instance after save/unload/reopen.

Local evidence includes:

- `dist/live-acceptance-2026-09-08/audio-file-workflows-1.5.0-failed-deferred-bounds.json`
- `dist/live-acceptance-2026-09-08/audio-file-rollback-1.5.0.json`
- `dist/live-acceptance-2026-09-08/audio-file-workflows-1.5.0.json`
- `dist/live-acceptance-2026-09-08/audio-file-persistence-1.5.0.json`

Generated Sets and raw evidence stay outside Git. This fixture establishes API
placement and saved persistence; it does not establish listening quality, other
file encodings, hardware routing or Live 11 behavior.

Live is running `.7`. Codex's existing MCP process holds `.4` and needs a host
refresh for the current server code and tool description. Both accepted runs used
fresh `.7` servers. Remote CI and publication are not claimed.
