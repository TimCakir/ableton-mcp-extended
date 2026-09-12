# Live acceptance: cross-track copying 1.4.0

Date: September 8, 2026. Live 12.4.5. Package 1.4.0, build `2026-09-08.6`,
protocol 2.1. The full automated suite passed **896 tests in 14.18 seconds**,
including 67 new builder cases. Independent review found no blockers.
Both Live acceptance phases used fresh stdio MCP servers with matching
loaded server, Remote Script and repository hashes. Discovery returned 152
tools and the updated `destination_track_handle` description.

## Fixture

All changes used the existing disposable Set:

```text
dist/live-acceptance-2026-09-08/MCP Acceptance 2026-09-08 Project/MCP Acceptance 2026-09-08.als
```

Two empty destination tracks were added and saved before acceptance:
`MCP QA CROSS MIDI` and `MCP QA CROSS AUDIO`. The existing sources were
`MCP QA CHORDS`, `MCP QA A` and `MCP QA B`, each in Session slot 1.
The Set contained 38 regular tracks and three returns, at 110 BPM, 4/4.
Playback and both arrangement/Session recording were inactive throughout.

The MIDI source has four triads at clip-relative beats 0, 2, 4 and 6. Its
12 notes and eight-beat length match the [1.3.0 fixture](LIVE-ACCEPTANCE-1.3.0-2026-09-08.md).
The audio sources reference distinct 12-second, 44.1 kHz fixture files,
`source-a-220hz.wav` and `source-b-660hz.wav`. A is warped with aligned 0–16 beat
markers; B is unwarped with aligned 0–12 second markers and zero pitch offsets.

## Verified results

Arrangement bounds are absolute zero-based quarter-note beats.

| Copy | Source → destination track | Content/range | Arrangement bounds |
| --- | --- | --- | --- |
| MCP CROSS CHORDS | MCP QA CHORDS → MCP QA CROSS MIDI | Keep every second onset, transpose +12 | 64.5–72.5 |
| MCP CROSS BEATS | MCP QA A → MCP QA CROSS AUDIO | Source beats 2–8, warped | 192.25–198.25 |
| MCP CROSS SECONDS | MCP QA B → MCP QA CROSS AUDIO | Source seconds 2–5, unwarped | 224.5–230 |

The MIDI copy contains exactly six notes: pitches 72/76/79 at clip-relative
beat 0 and 76/79/83 at beat 4. Duration 1, velocity 90, probability 0.8, mute,
velocity deviation and release velocity matched the expected surviving notes.
Public note values were compared at the endpoint's five-decimal precision;
internal builder verification uses exact unrounded values.

Audio file paths, warp state/mode, sample length/rate, gain, pitch, mute and
color matched their sources. Each excerpt had the expected markers, length and
disabled looping. MIDI clip names, length, markers, loop state, mute and color
also matched expectations.

Preview exposed distinct source and destination identities. Audio-to-MIDI and
MIDI-to-audio previews were rejected, as were proposed and existing destination
overlaps. Preview made no edits. Apply returned `applying`, then `applied`
after deferred verification. Replaying the retained plan added no duplicates.

Whole-Set snapshots showed exactly three destination additions. Source Session
clips, their existing arrangement clips, other tracks, devices and routing
metadata remained unchanged. Direct source note and clip-property reads provided
additional content checks beyond the snapshot metadata.

## Persistence and evidence

Workflow acceptance passed at **19:04:34 Istanbul time**. The Set was saved
through Live, unloaded into Untitled, then reopened from Live's recent-files
menu. The runner checked the exact saved path and a different Set session ID.
Read-only persistence acceptance passed at **19:06:21**, comparing all three
copy readbacks and the source baselines against the successful workflow evidence.

Runner: `tests/integration/cross_track_acceptance.py`. It never creates fixtures,
starts playback or saves. It records every MCP request before dispatch and every
response. Persistence requires successful workflow evidence and a fresh Set
instance. Raw evidence is retained locally in:

- `dist/live-acceptance-2026-09-08/cross-track-workflows-1.4.0.json`
- `dist/live-acceptance-2026-09-08/cross-track-persistence-1.4.0.json`

Generated Sets and raw evidence remain outside Git. This establishes API
content/geometry and saved persistence for this fixture. It does not establish
listening quality, per-note expression, direct audio-file placement, hardware
routing or Live 11 behavior. The MIDI destination has no instrument; the feature
copies clips without supplying the instrument or routing needed for playback.

Live is running `.6`. Codex's existing MCP process holds `.4` and needs a
connection restart to refresh its server code and tool description. Both
accepted runs used fresh `.6` servers. Remote CI and publication are not claimed.
