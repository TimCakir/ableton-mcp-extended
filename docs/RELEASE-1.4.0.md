# Release 1.4.0

Build: `2026-09-08.6` · Remote protocol: `2.1`

`build_arrangement` can copy a Session clip onto another compatible arrangement
track. MIDI variations and audio source ranges work across tracks, so a source
phrase can supply a separate breakdown track or an audio excerpt can go onto a
dedicated arrangement track.

## Usage

Read `get_edit_targets`, then preview using its current identities:

```json
{
  "action": "preview",
  "session_id": "<returned session_id>",
  "placements": [
    {
      "track_handle": "<source MIDI track handle>",
      "source_slot": 1,
      "destination_track_handle": "<destination MIDI track handle>",
      "destination_beat": 64.5,
      "name": "Breakdown chords",
      "variation": {"thin_by": "onset", "keep_every": 2, "transpose": 12}
    }
  ]
}
```

Inspect the preview, then apply with `action="apply"`, its `plan_id` and the same
`session_id`, omitting `placements`. If the result is `applying`, poll the same
plan until it finishes. Retained completed plans replay without another copy.

- `track_handle` and `source_slot` identify the original Session clip.
- Optional `destination_track_handle` identifies the receiving track. Omitting
  it preserves same-track placement.
- Response fields `track_handle` and `track_name` still identify the source.
  Additive fields `destination_track_handle` and `destination_track_name`
  identify the receiving track.
- Both tracks must be current regular, non-group, non-frozen tracks. MIDI goes
  to a MIDI track; audio goes to an audio track. A MIDI destination may already
  contain an instrument, but this operation does not copy devices or routing.
- Overlaps and reserved audio trimming space are checked on the destination.
  Existing source arrangement clips do not block an otherwise clear destination.
- Both track identities and the Session source are rechecked when applying and
  during deferred audio verification. Track reordering does not redirect a copy.
- New clips are enumerated on both source and destination. Unexpected extra
  copies or copies on the wrong track fail verification and trigger rollback of
  the copies owned by the operation.

All [MIDI and audio placement limits](RELEASE-1.2.0.md#limits-and-compatibility)
and [whole-onset thinning rules](RELEASE-1.3.0.md) still apply. The builder does
not start playback or save the Set. The top-level tool count remains 152.

## Live API and MCP

Live supplies `Track.duplicate_clip_to_arrangement(clip, destination_time)`;
the integration resolves the two tracks, builds a preview, verifies the result
and manages rollback. See the [official Track reference](https://docs.cycling74.com/apiref/lom/track/).
This extends an MCP workflow using Live's existing API.

## Verification

The full automated suite passed **896 tests in 14.18 seconds**, including 67
new builder cases. Independent review found no blockers. Actual Live 12.4.5
accepted foreign-track sources for a six-note chord variation, a warped audio
excerpt and an unwarped audio excerpt. Only the three expected destination
clips were added. Sources and existing arrangement material stayed unchanged.

All three copies passed again after saving, unloading and reopening the Set.
See the [1.4.0 acceptance report](LIVE-ACCEPTANCE-1.4.0-2026-09-08.md) for exact
placements, local evidence and limits. Live loaded `.6`; both acceptance phases
used fresh stdio servers with matching build labels and source hashes.

The existing Codex MCP process still holds `.4` and needs a connection restart
to refresh its loaded server and tool description. Live is already running
the updated script. Remote CI and publication are outstanding.
