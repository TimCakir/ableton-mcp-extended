# Release 1.3.0

Build: `2026-09-08.5` · Remote protocol: `2.1`

`build_arrangement` can thin entire groups of simultaneous notes when creating
a MIDI variation. Given four triads, it can keep triads one and three intact,
instead of removing individual tones from all four. The original Session clip
stays unchanged.

## Usage

Read `get_edit_targets`, then preview a placement using its current identities:

```json
{
  "action": "preview",
  "session_id": "<returned session_id>",
  "placements": [
    {
      "track_handle": "<returned MIDI track handle>",
      "source_slot": 1,
      "destination_beat": 32.5,
      "name": "Breakdown chords",
      "variation": {"thin_by": "onset", "keep_every": 2, "keep_offset": 0}
    }
  ]
}
```

Inspect the exact preview notes, then apply with `action="apply"`, the returned
`plan_id` and the same `session_id`, omitting `placements`. Retained completed
plans replay without creating another copy.

- `thin_by="note"` is the default and preserves individual-note thinning.
- `thin_by="onset"` groups notes with exactly equal `start_time`. It keeps all
  surviving notes in each selected group, including duplicates and muted notes.
- `keep_every` counts groups in onset mode; zero-based `keep_offset` selects
  where counting starts. With `keep_every=2`, offset 0 keeps groups 1, 3, 5;
  offset 1 keeps groups 2, 4, 6.
- Pitch removal runs before grouping. A group with no surviving notes no longer
  counts. Transposition runs last and rejects any surviving pitch outside 0–127.

Grouping uses exact values, with no rounding, quantization, timing tolerance or
chord-name detection. Slightly staggered or humanized notes remain separate even
if they sound like a chord. Overlapping notes with different start times are
also separate. Timing, duration, velocity, probability and other exposed values
are preserved; per-note expression is not inspected.

The normalized preview variation now includes `thin_by`, including the default
`"note"`. Clients reading that dictionary should accept the additive field.
Other [1.2.0 placement limits and guards](RELEASE-1.2.0.md#limits-and-compatibility)
still apply. The top-level tool count remains 152.

## Live API and MCP

This feature is implemented in the MCP Remote Script. Live supplies clip
duplication and note read/remove/modify operations; the integration supplies
grouping, previews, retained plans, verification and rollback. It does not
register a native Ableton menu command or change Live's API.

The [official Clip reference](https://docs.cycling74.com/apiref/lom/clip/)
documents `get_all_notes_extended`, `remove_notes_by_id` and
`apply_note_modifications`. This integration uses Live's Python Remote Script
binding and preserves the native note vector when modifying retained notes.

## Verification

The full automated suite passed **829 tests in 11.64 seconds**, including 37
new helper/builder cases. Independent review found no blockers. Actual Live
12.4.5 verification passed for two whole-chord variants and one default-mode
control. Each contained the six expected notes; all three survived saving,
unloading and reopening the Set, with the 12-note source unchanged.

See the [1.3.0 acceptance report](LIVE-ACCEPTANCE-1.3.0-2026-09-08.md) for exact
placements, local evidence and limits. Live loaded build `.5`; fresh stdio MCP
servers used for both phases matched the repository and loaded source hashes.
The existing Codex MCP process still holds `.4` and needs a connection restart
to refresh its server code and tool description. Live has already been reloaded.
