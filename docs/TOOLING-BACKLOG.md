# Tooling backlog

What still needs building or fixing in this integration, ordered by how much it
blocks actual music work. See `LIVE-API-FACTS.md` for what is already verified
and what is genuinely impossible.

---

## Done

**Arrangement-clip note editing.** The note and region tools now take
`arrangement=True` and resolve through `_resolve_clip`. Arranged material can be
read and edited in place.

**Arrangement automation.** `record_arrangement_automation` writes real track
automation by recording off the transport. The old belief that this was
impossible is corrected in `LIVE-API-FACTS.md`. Verified end to end on 12.4.3
against a real Set: 44 points over 8 beats, `automation_state` 0 → 1, replaying
as a smooth interpolated ramp (0.75 mid-ramp, landing exactly on 0.95). The
pass disarms every track first and restores the arm map, so it cannot punch out
arranged material on whatever happened to be armed. `cancel_automation_record`
stops it mid-pass and disarms the transport.

**Section playback.** `play_section(from_bar, to_bar)` — `song.start_time` is
writable, so a section can be auditioned and metered.

**Build skew.** `get_build_info` compares the repo, the script Live loaded, and
the running server, and names which is stale. `./deploy.sh` pushes to every path
Live might read. This was the root cause of most "Live cannot do that" reports.

**Batch envelope.** `batch` runs a list of commands over one connection, through
the ordinary `_process_command` path. Not registered as mutating, so it runs on
the client thread and can recurse without deadlocking on its own queue.

**Take recording.** `record_over_range(track, from_bar, to_bar)` punches a take
without hand-timing the record button.

**Time-range arrangement deletes.** `delete_arrangement_clip(from_bar, to_bar)`
replaces positional indexing, which renumbered under its own deletions.

---

## Blocking real work

**0. `modify_clip_notes` is broken outright on Live 12.3.8 — BUG, not a gap.**
Every modification fails, not just some. `transpose`, `velocity_scale`,
`velocity_set`, `humanize_ms` and `probability` all return:

```
Clip.apply_note_modifications(Clip, tuple) did not match C++ signature:
  apply_note_modifications(TPyHandle<AClip>, std::vector<NClipApi::TNoteInfo>)
```

`_modify_clip_notes` passes `tuple(notes)` where `notes` came straight from
`get_notes_extended`. *Hypothesis, unverified:* the
`MidiNote` objects that come out of `get_notes_extended` are not re-submittable and
need converting to whatever `TNoteInfo` expects. Failures are clean — verified that a
failed call leaves note velocities untouched.

This is an exposed, documented tool that currently cannot do anything. It also blocks
transposing a finished song, which is the realistic path if a singer needs a
different key.

**0b. Per-clip scale has no tool at all.**
`set_song_scale` writes only the song-level field, but Live 12 stores scale on every
clip and clips re-assert it (see LIVE-API-FACTS). So the song scale cannot be made to
*stick* on any Set that already has clips. Needed: either a `scale` option on the
clip tools, or an `apply_song_scale_to_all_clips` command. Verify `Clip.root_note` /
`Clip.scale_name` exist via `inspect_lom` before building — do not assume from the
XML field names.

Real cost already paid: a song's key reverted to C Major twice, and the only
available fix is 271 individual clip writes.

**0c. Note probability may not survive the write path — unresolved.**
`_add_notes_extended` sets `probability` with `setattr` on a
`MidiNoteSpecification` *after* construction, inside a bare `except Exception: pass`.
Notes written with `probability` come back reading no probability. Cannot yet tell
whether the write silently fails or `get_clip_notes` cannot read it back, because
`modify_clip_notes` (the other write path) is broken per item 0.

Suggestive, not conclusive: the `Tops (probability)` clip in `Love on the Beach` —
documented in `SOUND.md` as 45–75% per note — also reports no probability. Either it
never wrote, or the readback is blind. Worth resolving because "breathing" patterns
are a recorded, approved part of that project's sound and may not exist.

**1. Section variation without clip envelopes.**
Clip envelopes still cannot reach the arrangement, so section-level change comes
from MIDI content, per-section clip variants, or `record_arrangement_automation`.
Worth a dedicated tool: generate a variant of a clip (drop the kick, thin the
voicing, double the density) and place it over a bar range in one call.

**2. Reading arrangement automation back.**
Automation can now be written but not read: there is no envelope object to
inspect, and `automation_state` only reports *that* a parameter is automated,
not its shape.

The cheap approach is ruled out. **A stopped transport does not evaluate
automation** — the parameter holds its last value regardless of where the
playhead sits. Confirmed on 12.4.3: with the playhead at beat 1.28 and a
recorded envelope present, `volume` read `0.85` (the pre-record value) while
stopped, then `0.15` — the true value at that point — the moment the transport
rolled. So scrubbing `start_time` and sampling will read a constant and look
like "no automation".

Sampling during playback does work: 0.15 → 0.55 → 0.95 came back in order.
That makes readback possible but real-time, i.e. reading a 16-bar envelope
costs 16 bars. Worth wrapping only if something actually needs it.

---

## Worth having

- **`insert_device` at a position.** Currently devices always append and then need
  `move_device`. Live exposes `Track.insert_device`; signature unverified.
- **Sample loading into Simpler / drum pads from a file path**, not only a browser
  URI — so a sample folder can be turned into a kit programmatically.
- **Take lanes end to end.** `create_take_lane` and `take_lanes` are wrapped, but
  comping (choosing a take per region) is not. Directly relevant for recording a
  singer over several passes.
- **Groove extraction** from a clip (`GroovePool` can hold it; extraction path
  unverified).

---

## Known rough edges

- `call_lom` truncates its "Available:" member list at 80 entries, which once made
  a survey silently incomplete. Use `inspect_lom` for a full list.
- `manage_clip_automation` (the original) can only create or clear an empty
  envelope — superseded by `write_clip_automation`, but still present.
- Several batch-6 wrappers were written against `class_deltas.json` rather than a
  live device (notably parts of `rack` and `external`). They are defensive
  (`hasattr` guarded) but only partly runtime-tested.

---

## Verification discipline

Two defects reached a commit because integration was checked by eye. Both were
caught later by a script, not by reading:

- a dispatch block with no section markers was silently dropped, leaving a stale
  3-argument call against a new 4-argument method;
- a command registered as mutating with **no dispatch branch** — it simply did
  not exist, and failed only at runtime.

**Before any commit that adds commands, assert programmatically that every
mutating command appears in all three places** (the `elif command_type in [...]`
list, an `elif command_type ==` branch, and a server tool), that no method or tool
is duplicated, and that no tool sends an unknown command.
