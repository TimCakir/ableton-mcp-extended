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
impossible is corrected in `LIVE-API-FACTS.md`.

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

**1. Section variation without clip envelopes.**
Clip envelopes still cannot reach the arrangement, so section-level change comes
from MIDI content, per-section clip variants, or `record_arrangement_automation`.
Worth a dedicated tool: generate a variant of a clip (drop the kick, thin the
voicing, double the density) and place it over a bar range in one call.

**2. Reading arrangement automation back.**
Automation can now be written but not read: there is no envelope object to
inspect, and `automation_state` only reports *that* a parameter is automated.
Sampling `param.value` while scrubbing looked unpromising in one observation —
after a record pass, reading the fader while stopped returned the last value
set (0.9) rather than the automated one (0.2) — so a stopped-playhead scrub
probably does not re-evaluate automation. Sampling during playback would, at
real-time cost. Untested; one experiment, not a plan.

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
