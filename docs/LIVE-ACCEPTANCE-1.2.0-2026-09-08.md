# Live acceptance: arrangement workflows 1.2.0

Date: September 8, 2026. Live 12.4.5. Package 1.2.0, build `2026-09-08.4`,
Remote protocol 2.1. The fresh stdio MCP process discovered 152 tools and
reported matching loaded server, Remote Script and repository builds/hashes.

The MIDI variation and audio source-range workflows passed, including saved-Set
persistence. The full automated suite passed **792 tests in 10.59 seconds**.
These results extend the [1.1.0 baseline](LIVE-ACCEPTANCE-2026-09-08.md),
committed as `d50b243` on `codex/arrangement-workflows`.

## Test Set and scope

All edits used the disposable Set:

```text
dist/live-acceptance-2026-09-08/MCP Acceptance 2026-09-08 Project/MCP Acceptance 2026-09-08.als
```

The Set contained 35 tracks and three returns at 110 BPM, 4/4. The
runner resolved current track handles by unique fixture names before reading or
editing indexed clips. Playback, arrangement recording, Session recording and MCP
recording jobs were inactive. The runner did not play or save the Set.

The MIDI source on `MCP QA MIDI VERIFIED`, Session slot 1, contained pitches
62 and 66 at velocity 82 with probability approximately 0.8. It included
non-rounded timing values to test preservation beyond displayed precision.
The two audio sources were local 12-second, stereo 44.1 kHz fixtures on
`MCP QA A` and `MCP QA B`; one was warped and the other unwarped.

## Verified placements

All destinations below use absolute zero-based quarter-note beats. Every
placement was previewed, applied and checked against its retained plan.

| Copy name | Change or source excerpt | Arrangement bounds | Result |
| --- | --- | --- | --- |
| MCP V1 BREAKDOWN | Remove pitch 62; transpose remaining note down 12 | 16.5–20.5 | Pitch 54 |
| MCP V1 THIN | Keep every second individual note, offset 1 | 20.5–24.5 | Pitch 66 |
| MCP V1 PHRASE | Transpose up seven semitones | 24.5–28.5 | Pitches 69 and 73 |
| MCP V1 AUDIO BEATS | Warped source beats 2–8 | 128.25–134.25 | Six beats, nonlooping |
| MCP V1 AUDIO SECONDS | Unwarped source seconds 2–5 | 128.5–134 | Three seconds / 5.5 beats, nonlooping |

MIDI readback preserved timing, duration, velocity, mute, probability, release
velocity and velocity deviation. The builder compares unrounded values;
the public `get_clip_notes` endpoint reports floats at five decimal places, so
the acceptance runner compares that endpoint at its documented precision.

Audio readback verified names, source files, gain, pitch, warp mode, exposed warp
markers, source markers and final arrangement bounds. Source Session clips were
unchanged. A repeated apply returned the retained result without creating more
copies. An invalid later MIDI transposition and wrong audio units rejected the
preview without edits; an overlapping plan was also rejected.

## Live timing defect found and fixed

The first mixed-plan run verified the three MIDI copies and warped audio, then
rejected the unwarped copy's bounds. The entire plan rolled back, with no owned
copies left. A revised marker order alone did not solve the problem: within the
same execution tick, Live reported the initial 16-beat copy at 128.5–144.5 even
though its source markers already read seconds 2–5.

Build `2026-09-08.4` closes the mutation undo group and schedules final readback
on the next Live tick. The actual accepted run first returned `applying` with
the stale 144.5 endpoint, then returned `applied` with the correct 134 endpoint
when the same plan was polled. Finalization rechecks every copy, source identity,
audio settings and original arrangement bounds. It does not weaken the bounds
tolerance or duplicate the operation. New plans are blocked while verification
is pending; failure rolls back owned copies in a separate undo group.

## Persistence

After workflow acceptance, the Set was saved through Live, unloaded into an
Untitled Set, then reopened from its saved file. A separate fresh stdio MCP
process repeated the readback checks and passed at 17:16:25 local time.
All five clips retained their notes, names, source ranges, warp state and
arrangement positions. Source clips still matched their expected values, and
playback/recording remained inactive.

An initial persistence attempt ran while Untitled was still open and stopped
at the exact Set-path guard, before reading or changing workflow fixtures.
Its evidence is retained separately from the successful run.

## Evidence and limits

The opt-in runner is `tests/integration/arrangement_workflows_acceptance.py`.
Local JSON evidence, including every MCP request and response, is retained in:

- `dist/live-acceptance-2026-09-08/workflows-1.2.0.json`
- `dist/live-acceptance-2026-09-08/persistence-1.2.0.json`
- `dist/live-acceptance-2026-09-08/workflows-1.2.0-initial-failure.json`
- `dist/live-acceptance-2026-09-08/workflows-1.2.0-marker-order-failure.json`
- `dist/live-acceptance-2026-09-08/unwarped-timing-probe.json`
- `dist/live-acceptance-2026-09-08/persistence-1.2.0-before-reopen.json`

The generated Set, audio and raw evidence remain local, outside Git. This report
records the verified outcome without publishing the user's test workspace.

This acceptance establishes native API behavior, content/geometry readback and
save/reopen persistence for these fixtures. It does not establish listening
quality, per-note expression preservation, all audio formats, Live 11 behavior,
cross-track placement, hardware recording or later tempo-change behavior.
Audio file guards compare filesystem identity metadata, not audio byte hashes.

Live loaded the current Remote Script. The existing Codex MCP process still
reported build `2026-09-08.1`; it needs a host restart to refresh its server code
and tool descriptions. The fresh stdio processes used for both accepted runs
matched build `2026-09-08.4`. Remote CI and publication remain separate work.
