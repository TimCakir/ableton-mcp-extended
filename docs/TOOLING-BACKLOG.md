# Tooling backlog

What still needs building or fixing in this integration, ordered by how much it
blocks actual music work. See `LIVE-API-FACTS.md` for what is already verified
and what is genuinely impossible.

---

## Blocking real work

**1. Arrangement-clip note editing.**
`get_clip_notes` / `modify_clip_notes` / `write_clip_automation` all resolve
through `_clip_at`, which only looks at `clip_slots` — i.e. session clips.
`_resolve_arrangement_clip` exists but is only used by the arrangement-clip
property commands. Consequence: once material is in the arrangement it cannot be
read or edited, only overwritten by re-placing a session clip. This is the single
biggest gap for arranging.
*Fix:* add an `arrangement=True` path (or an `arrangement_clip_index`) to the note
and region tools, routed through `_resolve_arrangement_clip`.

**2. Section variation without automation.**
Since clip automation can never reach the arrangement, section-level change has to
come from MIDI content or per-section clip variants. Worth a dedicated tool:
generate a variant of a clip (drop the kick, thin the voicing, double the density)
and place it over a bar range in one call.

**3. `delete_arrangement_clip` addressing is fragile.**
It takes a positional index into `arrangement_clips`, which shifts as clips are
added or removed — the same class of bug as track-index drift.
*Fix:* address by time range (`from_time` / `to_time`), which is how arrangement
edits are actually thought about.

---

## Worth having

- **Batch command envelope.** Every call is one socket round-trip (~0.2 s on a
  persistent connection, ~0.6 s on a fresh one). Building an arrangement took
  ~260 calls. A `batch` command taking a list of commands would turn minutes into
  seconds and make large edits practical.
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
