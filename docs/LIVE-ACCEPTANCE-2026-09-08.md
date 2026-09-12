# Live acceptance: September 8, 2026

Build **2026-09-08.1**, package **1.1.0**, protocol **2.1** was deployed and loaded in **Ableton Live 12.4.5** on macOS. A fresh stdio MCP server discovered **152 tools**. All three opt-in acceptance phases passed: recordings, arrangement and persistence.

## Test environment and scope

The initial untitled Set was saved separately under `dist/live-acceptance-2026-09-08/MCP Acceptance 2026-09-08 Project/`. The default writing template and production Sets were not edited. Tests added dedicated QA tracks and used generated quiet 220 Hz and 660 Hz stereo WAVs. Live's audio output was changed from No Device to MacBook Pro Speakers; input remained No Device, at 44.1 kHz with a 256-sample buffer. The acceptance Set was left open with playback stopped.

The installed Remote Script initially reported July build `2026-07-26.20`. After deployment and restart, the final remote package hash agreed across loaded code, installed files and repository:

`831bc95ea9cb874bc4792dff46e40798e67a9429396781e06f856ff7b04d8d2b`

The fresh MCP process's loaded and disk package hashes agreed:

`8313a9f01fe8b0be5b891d306e391085025aa71f09f97e2b5c9ce0b93b16c1ea`

These checks cover all Python modules, not only the main source file. `get_build_info` returned `match` again after the final save/reopen.

After the user restarted Codex, this task's actual Ableton MCP connection exposed 152 tools, including `build_arrangement`. `get_build_info` returned `match` for build `2026-09-08.1`, with matching loaded/disk server fingerprints and loaded/disk/repository Remote Script fingerprints. The session overview and stable target discovery succeeded. An arrangement preview on `MCP QA MIDI VERIFIED` accepted a four-beat, two-note copy at beats 16–20; the preview was not applied. The reconnect evidence is retained in `codex-reconnect.json` in the local evidence directory below.

## Failures found and fixed in Live

1. **Recording proxy identity.** Live returned different Python wrappers for the same Song/Track. Identity-only comparisons caused bounce setup to report that it could not identify the newly created recording track, then incorrectly reported a Set change. Recording ownership, membership, source checks and output comparisons now use exception-safe Live entity equality. Tests cover fresh proxies and genuinely replaced entities with identical names.
2. **Group/return snapshot reads.** Live raises when reading arrangement clips on those track kinds. Snapshots now report complete empty clip lists for them, while retaining errors for failed reads on clip-capable tracks.
3. **MCP startup.** Package initialization imported the server before `python -m MCP_Server.server` executed it, producing a duplicate-module warning. The public connection helper now imports lazily; package version metadata is 1.1.0. The final stdio persistence run started without that warning.

## Passed checks

| Check | Observed result |
| --- | --- |
| Stable targeting | A MIDI handle still edited its original track after insertion shifted its index from 29 to 30 |
| Revision guard | Applying against the pre-insertion revision returned a real MCP error |
| Return editing | Return name changed and restored through the same stable handle |
| MIDI validation | An invalid pitch in a later input note rejected the entire request; original notes remained unchanged |
| MIDI transformation | Transpose/probability edits updated both notes while preserving IDs and timing |
| Snapshot metadata | Final snapshots included groups/returns with no read errors or truncation |
| Bounce and stems | Short captures completed with nonempty stereo audio files and measured signal |
| Cancellation | A cancelled pass became inactive; a new capture immediately afterward completed |
| Arrangement preview/apply | Two four-beat copies were verified at beats 0–4 and 4–8 |
| Duplicate apply | Reapplying the retained plan returned `replayed=true`, with no new copies |
| Overlap guard | A plan over the existing copies was rejected |
| Changed-source guard | Editing source velocities after preview caused apply to reject the stale plan |
| Source preservation | Copying left the source notes unchanged; subsequent deliberate source edits did not alter the copies |
| Save/reopen | After quitting Live and reopening the saved Set, both arrangement copies and all four successful recording files remained available |

Audio measurements from the actual recording manifests:

| Output | Duration (seconds) | Sample peak (dBFS) | RMS (dBFS) |
| --- | ---: | ---: | ---: |
| Bounce A | 4.469841 | -32.0 | -35.1 |
| Stem A | 4.376961 | -32.0 | -35.1 |
| Stem B | 4.376961 | -38.1 | -41.1 |
| Capture after cancellation | 4.429206 | -38.1 | -41.1 |

All were stereo 44.1 kHz, fully measured, above the silence threshold and below the clipping flag. Separate frequency checks on each stem found its expected 220 Hz or 660 Hz component dominant, confirming distinct source routing. Measurement uses FFmpeg sample peak/RMS, not true peak or LUFS.

## Reproduction and evidence

`tests/integration/live_acceptance.py` is a manual opt-in runner, excluded from automatic pytest collection. It requires an existing disposable Set and the named QA fixtures. Run `--phase recordings`, then `--phase arrangement`; save, quit/reopen the Set and run `--phase persistence`. Supply `--set-path` and a different `--output` JSON path for each phase. Arrangement placement intentionally requires empty initial destination ranges, so repeat it from a fresh fixture copy.

Local detailed evidence is in `dist/live-acceptance-2026-09-08/`: `first-pass-evidence.json`, `recordings.json`, `arrangement.json`, `persistence.json` and `stem-frequency-check.json`, alongside the Set and recorded audio. These generated files are ignored by Git; this report retains the results.

The unit suite passed **586 tests** with MCP 1.28.1 and **586** with MCP 1.30.0. The final wheel's 16 Python files matched current source byte-for-byte. Wheel SHA-256:

`990c466d9eef7b5ebf246440429e00b283054127d49d132a648037b8c5b8e5f1`

## Limits and remaining work

- This verifies short captures, measured signal and persistence; no subjective listening verdict was recorded. Hardware routing, long recordings, other Live versions and every legacy command were not tested.
- Real-time stopping occurs on a scheduled tick. Captures extended slightly past the requested eight beats; manifests report actual bounds. Sample-exact export trimming is not implemented.
- The arrangement builder currently supports same-track session MIDI copies. It rejects clip envelopes and does not inspect per-note expression. Native Freeze, comping, audio arrangement plans and SDK 2 migration remain separate work.
- Changes and build artifacts are local; no commit, push or remote CI completion is claimed.
