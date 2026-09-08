# Release 1.2.0

Build: `2026-09-08.4` · Remote protocol: `2.1`

`build_arrangement` now creates MIDI variations and places trimmed audio from
Session clips on their own tracks. It retains the preview/apply, source identity,
overlap, replay and operation-owned rollback checks introduced in 1.1.0. The tool
count remains 152; these are extensions to the existing arrangement tool.

## MIDI variations

Discover the current `session_id` and intended `track_handle` with
`get_edit_targets`, then preview explicit placements:

```json
{
  "action": "preview",
  "session_id": "<returned session_id>",
  "placements": [
    {
      "track_handle": "<returned MIDI track handle>",
      "source_slot": 1,
      "destination_beat": 64.5,
      "name": "Breakdown",
      "variation": {"remove_pitches": [36], "keep_every": 2, "keep_offset": 0, "transpose": 0}
    }
  ]
}
```

Destinations are absolute zero-based quarter-note beats, including fractional
positions. Source slots are 1-based. `name` is optional and labels only the copy.

Transformations run in this order:

1. Remove notes whose pitches appear in `remove_pitches` (integers 0–127).
2. Sort remaining individual notes by onset, pitch and their other exposed values.
3. Keep every `keep_every`th note, starting at zero-based `keep_offset`. Defaults
   are 1 and 0. This thins individual notes, not whole chord groups.
4. Add `transpose` semitones to surviving pitches. Out-of-range pitches reject
   the entire preview; pitches are never clamped.

The preview exposes every resulting note's values, without regenerated note IDs.
It preserves timing, duration, velocity, mute, probability and other exposed note
values. Removing all notes is allowed and produces an empty clip. Per-note
expression is not inspected. The source remains unchanged.

Inspect the preview, then apply with `action="apply"`, its `session_id` and
`plan_id`, omitting `placements`. Before each variation is modified, the newly
copied notes are checked against the source. Removal uses current IDs, then a
fresh native Live note vector is fetched before transposition. Final note values
are read back against the retained preview.

## Audio placement and trimming

Import a local audio file into an empty Session slot using `import_audio_file`,
then use the same preview/apply flow:

```json
{
  "action": "preview",
  "session_id": "<returned session_id>",
  "placements": [
    {
      "track_handle": "<returned audio track handle>",
      "source_slot": 1,
      "destination_beat": 128.25,
      "name": "Stem excerpt",
      "source_range": {"start": 2, "end": 5, "units": "seconds"}
    }
  ]
}
```

`source_range` uses `"beats"` for warped audio and `"seconds"` for unwarped
audio. Its end is exclusive. Omit it to copy the full active source range. The
source's start/end markers must align with its loop bounds, and requested ranges
must stay inside them. New audio copies are nonlooping. Warping is not toggled;
gain, pitch, warp mode and exposed warp markers are checked after copying.

For unwarped audio, coarse/fine pitch must be zero. Tempo must be unchanged and
unautomated, with Link and tempo follower disabled. The preview converts seconds
to arrangement beats using that fixed tempo and rechecks it before applying.
At 110 BPM, a three-second excerpt occupies 5.5 beats.
These bounds are verified at apply time; later tempo edits can change unwarped
audio's extent in beats.

Live defers unwarped arrangement-bound updates. A plan containing unwarped audio
therefore returns `status="applying"` after creating the copies and closing the
undo group. The next Live tick verifies every copy, all source guards and original
arrangement bounds. Poll `action="apply"` with the same `session_id` and `plan_id`
until the result is `applied`, `error` or `partial`. Polling does not copy again.
New arrangement plans are blocked while verification is pending. Failed final
verification rolls back owned copies in a separate undo group.

Every audio preview exposes both the final `end_beat` and `reserved_end_beat`.
The latter includes the full initial copy and intermediate trimming space;
it must be empty even when the final excerpt is shorter. This is deliberately
conservative. Live can retain a stale `clip.length` after warping is disabled,
so that value is guarded as a possible copy span, never used as the desired
duration of unwarped audio.

Sources with envelopes, grooves, recording activity, missing files or
unverifiable metadata are rejected. Audio file guards compare path, filesystem
identity, size and modification/change times. They do not hash audio bytes or
guarantee protection against a change that evades those metadata checks.

## Limits and compatibility

- Same-track Session sources only. Cross-track plans and direct file placements
  without a Session source remain separate work.
- Up to 64 placements and 10,000 source MIDI notes per plan, 32 retained plans,
  five-minute expiry for unapplied plans. Track and Set handles expire on restart.
- Retained terminal results replay without creating more copies. Unknown plans
  do not establish that a previous request never applied.
- Failed operations remove only copies they own. `error` means rollback was
  verified; `partial` reports what could not be restored. Neither implies a save.
- No playback, file export or Set save is started by the arrangement builder.
- Active Live arrangement/session recording and MCP recording jobs reject new
  previews and applies. The planner does not stop the recording for the user.
- The older `create_arrangement_audio_clip` and `duplicate_clip_to_arrangement`
  wrappers give positive bar arguments precedence over beat arguments. Use
  bar=0 with absolute beats for fractional positions; their descriptions now
  state this explicitly. The new planner always uses absolute beats.

The [Clip reference](https://docs.cycling74.com/apiref/lom/clip/) documents
unit differences and note APIs; the [Track reference](https://docs.cycling74.com/apiref/lom/track/)
documents arrangement duplication in beats. The running Live version is checked
separately because Python and Max bindings differ.

## Verification

The full automated suite passed **792 tests**. Both the five-placement workflow
and its separate save/unload/reopen persistence check passed in Live 12.4.5 with
matching build `2026-09-08.4` components. See the
[1.2.0 Live acceptance report](LIVE-ACCEPTANCE-1.2.0-2026-09-08.md) for exact
placements, the deferred audio timing fix, evidence and remaining limits.
The independently tested 1.1.0 baseline is committed as `d50b243`; its evidence
is retained in [the September 8 report](LIVE-ACCEPTANCE-2026-09-08.md).
