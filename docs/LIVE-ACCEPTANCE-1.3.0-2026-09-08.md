# Live acceptance: chord thinning 1.3.0

Date: September 8, 2026. Live 12.4.5. Package 1.3.0, build `2026-09-08.5`,
protocol 2.1. The full automated suite passed **829 tests in 11.64 seconds**.
Both workflow and save/unload/reopen acceptance passed through fresh stdio MCP
servers, with matching loaded server, Remote Script and repository hashes.
The server exposed 152 tools and the updated `thin_by` description.

## Fixture

All changes used the existing disposable Set:

```text
dist/live-acceptance-2026-09-08/MCP Acceptance 2026-09-08 Project/MCP Acceptance 2026-09-08.als
```

A new MIDI track, `MCP QA CHORDS`, holds `MCP CHORD SOURCE` in Session slot 1.
The clip is eight beats long, with four triads at clip-relative beats 0, 2, 4
and 6: pitches 60/64/67, 62/65/69, 64/67/71 and 65/69/72. All 12 notes have
duration 1, velocity 90 and probability approximately 0.8. Playback and both
arrangement/Session recording were inactive. The Set remained at 110 BPM, 4/4.

## Verified results

Destinations are absolute zero-based quarter-note beats. Each copy is eight
beats long, with six notes. Original note timing is retained inside each copy.

| Copy | Variation | Arrangement bounds | Retained clip-relative notes |
| --- | --- | --- | --- |
| MCP CHORD EVEN | Onset mode, every 2, offset 0 | 32.5–40.5 | Beat 0: 60/64/67; beat 4: 64/67/71 |
| MCP CHORD ODD | Onset mode, every 2, offset 1, transpose +12 | 40.5–48.5 | Beat 2: 74/77/81; beat 6: 77/81/84 |
| MCP CHORD NOTES | Default note mode, every 2 | 48.5–56.5 | Beat 0: 60/67; beat 2: 65; beat 4: 64/71; beat 6: 69 |

The runner checked the exact preview, applied it, read every placed note, and
replayed the retained plan without duplicate copies. Invalid `thin_by` in a
later placement rejected the entire preview without edits; a subsequent
overlapping preview was also rejected.

Readback compared all exposed note values, ignoring regenerated IDs. Internal
builder verification uses unrounded values. The public note endpoint rounds
to five decimal places, so cross-endpoint acceptance uses that precision.
Clip names, length, loop/start/end markers, mute and color matched expectations.
The Session source's notes and metadata stayed unchanged.

## Persistence and evidence

Workflow acceptance passed at **18:31:16 Istanbul time**. The Set was saved
through Live, unloaded into Untitled, then reopened from its saved file.
Read-only persistence acceptance passed at **18:33:10**, with a different
Set session ID. It rechecked all three copies and compared the saved source
with the workflow baseline. Playback and recording remained inactive.

Runner: `tests/integration/chord_variations_acceptance.py`. It never creates
fixtures, starts playback or saves the Set. Persistence requires the successful
workflow evidence and a fresh Set instance. Every MCP request and response is
retained locally in:

- `dist/live-acceptance-2026-09-08/chords-workflows-1.3.0.json`
- `dist/live-acceptance-2026-09-08/chords-persistence-1.3.0.json`

Generated Sets and raw evidence remain outside Git. This establishes API
content/geometry and saved persistence for this fixture, not listening quality,
staggered-note grouping, per-note expression preservation or Live 11 behavior.
Grouping uses exact onset equality, not harmonic recognition or quantization.

Live loaded the current `.5` Remote Script. Codex's existing MCP process still
holds `.4`; a connection restart refreshes its server and tool description.
Both accepted runs used fresh `.5` servers. Remote CI and publication are not
claimed by this local acceptance.
