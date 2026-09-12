# Release 1.5.0

Build: `2026-09-08.7` · Remote protocol: `2.1`

`build_arrangement` can preview and place a local PCM WAV file, including a
seconds-based excerpt, without requiring an existing Session source clip.
It verifies the target, file identity, placement and temporary-clip cleanup
before returning success.

## Usage

Read `get_edit_targets`, then use a current audio-track handle:

```json
{
  "action": "preview",
  "session_id": "<returned session_id>",
  "placements": [
    {
      "file_path": "/absolute/path/to/effect.wav",
      "destination_track_handle": "<audio track handle>",
      "destination_beat": 128.5,
      "name": "FX excerpt",
      "source_range": {"start": 2, "end": 5, "units": "seconds"}
    }
  ]
}
```

Omit `source_range` to use the entire file. At 110 BPM, the three-second excerpt
above ends at beat 134. All arrangement positions use absolute zero-based
quarter-note beats. Inspect the preview, then apply with its `plan_id` and the
same `session_id`, omitting `placements`. Poll the same apply request while its
status is `applying`. Retained terminal plans never import another copy on replay.

## Limits and behavior

- A file plan contains 1–64 file placements. Session-clip placements use their
  existing form and must go in a separate plan.
- The first version accepts mono/stereo RIFF PCM WAV with format tag 1 and
  8/16/24/32-bit integer samples. Float WAV, WAVE_FORMAT_EXTENSIBLE, RF64, AIFF,
  MP3 and compressed WAV are rejected explicitly. Files are referenced in place;
  keep them available after saving the Set.
- Imports are unwarped, nonlooping, unpitched, unmuted and at unity clip gain.
  The song must have fixed tempo with tempo automation, Link and tempo follower
  disabled. Track devices, mixer levels and routing still affect playback.
- Each placement reserves an empty Session slot on its destination track. Apply
  imports there temporarily, normalizes only that owned clip, then uses the
  guarded arrangement-copy workflow. `applied` requires removal of the temporary
  Session clips. Existing Session clips are never used as staging slots.
- The preview reserves the full file duration converted to beats, even for an
  excerpt. For a 12-second file at 110 BPM starting at beat 128.5, space must be
  clear through beat 150.5. Existing arrangement material inside this reservation
  blocks the preview.
- Live can retain auto-warp or analysis-file values in the imported clip's
  initial length. If the settled temporary clip requires more space than the
  preview reserved, apply fails and cleans up its owned clips. It never enlarges
  the reservation silently.
- File metadata is checked before and after a bounded header/sample read and
  again during the workflow. Guards include path resolution, device/inode,
  size, modification/change times, sample rate and sample count. This detects
  ordinary edits and replacements; it is not a cryptographic content hash.
- Preview changes no Live content. Apply checks Set, track and staging-slot
  identity, recording state, file identity and destination bounds. Failed work
  rolls back only the operation's owned clips. `partial` means cleanup or
  rollback could not be verified and needs inspection.

Plans expire after five minutes before apply. An applying plan blocks other
arrangement plans until it reaches a terminal result. A 30-second pending deadline
is checked on deferred callbacks and apply polls; expiration triggers owned-clip
cleanup and a terminal result. The builder neither starts
playback nor saves the Set. The top-level tool count remains 152.

## Live API and MCP

The workflow uses Live's documented
[`ClipSlot.create_audio_clip`](https://docs.cycling74.com/apiref/lom/clipslot/#create_audio_clip)
and [`Track.duplicate_clip_to_arrangement`](https://docs.cycling74.com/apiref/lom/track/#duplicate_clip_to_arrangement).
Temporary Session import lets it check Live's actual clip geometry before placing
anything near existing arrangement material. Preview, identity checks, retained
results and cleanup are supplied by the MCP integration.

## Verification

The full automated suite passed **1,031 tests in 12.38 seconds**, including 95
file-planner cases and 40 bounded WAV-reader cases. Independent review found
no remaining blockers.

Actual Live 12.4.5 workflow and save/unload/reopen acceptance passed for one
three-second excerpt and one full 12-second WAV on the same destination track.
Both runs used fresh stdio servers with matching loaded builds and source hashes.
Only the two expected arrangement clips were added; staging slots, original
material and source WAV bytes remained unchanged. See the
[1.5.0 acceptance report](LIVE-ACCEPTANCE-1.5.0-2026-09-08.md) for the initial
timing defect, verified rollback, fix and successful final evidence.

The existing Codex connection still needs an MCP host refresh to load this
server version and its tool description. Remote CI and publication are not claimed.
