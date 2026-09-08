# Tooling backlog

## Current implementation handoff — 2026-09-08

Build `2026-09-08.6` / package `1.4.0` adds optional
`destination_track_handle` to `build_arrangement`. Session MIDI and audio clips
can supply another compatible arrangement track. Same-track defaults, MIDI
variations and audio ranges are preserved. Both track identities are rechecked;
overlaps are destination-based, and new clips on either track are enumerated
for verification and rollback. The full automated suite passed 896 tests.
See [1.4.0 release notes](RELEASE-1.4.0.md).

Actual Live 12.4.5 workflow and save/unload/reopen acceptance passed for one
six-note chord variation and two audio excerpts on separate destinations.
Only the three expected copies were added; Session sources and existing
arrangement material stayed unchanged. See the
[1.4.0 acceptance report](LIVE-ACCEPTANCE-1.4.0-2026-09-08.md).
Live runs `.6`, and both acceptance phases used fresh stdio servers with matching
build labels and source hashes. Codex's existing connection still holds `.4`
and needs a host refresh for the current server and description. Remote CI and
publication remain outstanding.

## Verified 1.3.0 baseline

Build `2026-09-08.5` / package `1.3.0` adds whole-onset MIDI thinning through
`variation.thin_by="onset"`. It keeps all notes sharing an exact start time,
after pitch removal; offsets count surviving groups. Individual-note thinning
remains the default. There is no timing tolerance or quantization. The full
automated suite passed 829 tests. See [1.3.0 release notes](RELEASE-1.3.0.md).

Both real Live 12.4.5 workflow and save/unload/reopen acceptance passed, verifying
three copies with 18 notes in total and the unchanged 12-note source. See the
[1.3.0 acceptance report](LIVE-ACCEPTANCE-1.3.0-2026-09-08.md). Live ran
`.5`; the acceptance runner's fresh stdio servers matched build and hashes.
At that checkpoint the existing Codex connection held `.4`; this historical
baseline does not claim remote CI or publication.

## Verified 1.2.0 baseline

Build `2026-09-08.4` / package `1.2.0` extends `build_arrangement` with MIDI
section variations and audio source-range placement. The tool count remains 152.
The full automated suite passes 792 tests. The deployed build passed actual
Live 12.4.5 workflow acceptance and saved-Set persistence checks. See
[1.2.0 release notes](RELEASE-1.2.0.md) for supported inputs and limits and the
[1.2.0 Live acceptance report](LIVE-ACCEPTANCE-1.2.0-2026-09-08.md) for evidence.

The disposable Set now contains three verified MIDI variations and warped and
unwarped audio excerpts at fractional beat positions. Preview/apply, retained-plan
replay, overlap and invalid-input rejection, unchanged sources and stopped
transport passed. All five placed clips passed verification after saving,
unloading the Set and reopening it.

Implemented in this build:

- MIDI copies support pitch removal, deterministic thinning of individual notes
  and transposition, in that order. Previews expose resulting notes; apply
  preserves the source and verifies the copied values. Out-of-range pitches
  reject the preview.
- Audio copies support explicit source ranges in beats when warped and seconds
  when unwarped, with fractional arrangement destinations. Source markers must
  align with loop bounds. Unwarped sources require zero pitch offsets and fixed,
  unautomated tempo with Link and tempo follower disabled.
- Audio plans reserve the initial copy and intermediate trimming space as well
  as the final excerpt. Apply checks source file identity metadata, audio
  settings, warp markers and final bounds. File bytes are not hashed.
- The existing stable identities, overlap checks, retained-plan replay and
  operation-owned rollback apply to both workflows. Active Live or MCP
  recording blocks previews and applies. Plans do not start playback or save.
- Unwarped plans return `applying` until a later Live tick verifies all copied
  content and final bounds. Poll the same plan's apply result; new plans are
  blocked while it is pending. A failed finalizer rolls back only owned copies.

The tested 1.1.0 baseline is committed as `d50b243` on
`codex/arrangement-workflows`. Its [Live acceptance report](LIVE-ACCEPTANCE-2026-09-08.md)
records bounce, two distinct measured stems, cancellation followed by a new
capture, MIDI transpose/probability, stable track targeting, guarded MIDI copying
and save/reopen persistence in Live 12.4.5. Its 586 tests passed with MCP 1.28.1
and 1.30.0. Remote CI and a published release remain outstanding; this local
acceptance does not establish either. Codex's subsequent restart verified all
1.2.0 components matched build `.4` and their source hashes before 1.3.0 work.

## Next work, in order

1. **Remote CI and publication.** Run the configured remote checks and publish
   only when authorized. The local baseline, 1.4.0 implementation and real Live
   acceptance are complete; no remote CI result or published release is claimed.
2. **Extend arrangement coverage where needed.** Direct audio-file placement,
   grouping staggered notes and note-density generation remain outside the
   current planner. Same-track and cross-track copies, pitch removal,
   individual-note/whole-onset thinning and transposition are implemented.
3. **Broader recording acceptance.** Exercise longer captures and the actual
   hardware routing. Test freeze-style source deactivation and scene capture
   independently; passing bounce tests does not establish these workflows.

**Per-clip scale remains an API investigation, not a missing wrapper.** On
September 8, `inspect_lom` against the acceptance Set's MIDI clip returned no
members for either `scale` or `root` in Live 12.4.5. The current
[Clip reference](https://docs.cycling74.com/apiref/lom/clip/) also lists neither.
Do not implement guessed property writes; offline Set editing would be separate
work with its own persistence checks.

## Historical July/August observations

The observations below retain the state and priorities from their original runs;
they are not current outstanding tasks. In particular,
`batch` now preserves each read result, and `_add_notes_extended` validates
probability before destructive writes. Batch rebasing only changes named index
fields; it does not decrement arbitrary numeric values such as beat positions.
Note probability, two-track stem export and host refresh have since passed the
September baseline acceptance checks. Section variations and same-track audio
planning are implemented in 1.2.0 and have passed actual Live and saved-Set checks.
Native Freeze, complete comping and SDK v2 migration remain open.

---

What still needs building or fixing in this integration, ordered by how much it
blocks actual music work. See `LIVE-API-FACTS.md` for what is already verified
and what is genuinely impossible.

---

## Historical handoff — build 2026-07-26.20, five tools then unexecuted

Run `get_build_info` first. Live and the MCP host were last restarted at build
`.18`; the repo is at `.20`, so **both need restarting** before any of the below
can be tested.

**Verified against the real Set (15):** `get_build_info`, `play_section`,
`batch`, `record_arrangement_automation`, `get_automation_record_status`,
`cancel_automation_record`, `bounce_to_audio`, `import_audio_file`,
`insert_device`, `measure_section`, `get_performance_report`,
`manage_song_data`, `set_song_options`, `delete_arrangement_clip` (bar range),
`record_over_range` (single stem, real audio at −10.0 dBFS).

**Written but NEVER EXECUTED (5) — treat as unproven:**

| Tool | Status |
|---|---|
| `export_stems` (multi-track) | Failed twice, fixed twice. Single stem works. |
| `freeze_track` | Shares the verified bounce path; deactivation untested. |
| `capture_session_to_arrangement` | **Destructive.** Scratch scene only. |
| `modify_clip_notes` | Repair reasoned from the error, never run. |
| `delete_return_track` | Trivial, but untried. |

**Test `export_stems` on 2–3 named tracks, never all 11 first.** Check each
file's SSND peak individually and confirm they differ — the failure mode to
hunt is *only the last stem has audio*. Afterwards check FX / RISER did **not**
gain a clip across the export range: that bug has occurred once already.

`capture_session_to_arrangement` overwrites the arrangement across its range on
every track in the scene, and is not undoable through this API. Build a scratch
MIDI track and a scratch scene for it. Do not point it at `Love on the Beach`.

---

## Done

**Arrangement-clip note editing.** The note and region tools now take
`arrangement=True` and resolve through `_resolve_clip`. Arranged material can be
read and edited in place.

**`modify_clip_notes` repaired.** It had been broken *outright* on 12.3.8 — not
partially. `transpose`, `velocity_scale`, `velocity_set`, `humanize_ms` and
`probability` all failed with:

```
Clip.apply_note_modifications(Clip, tuple) did not match C++ signature:
  apply_note_modifications(TPyHandle<AClip>, std::vector<NClipApi::TNoteInfo>)
```

Cause: `get_notes_extended` returns a **`MidiNoteVector`**, and
`apply_note_modifications` wants that same type back. Wrapping it in `tuple()` (or
`list()`) destroyed it. The correct pattern is **fetch the vector, mutate the notes in
place, hand the same vector back**. Failures were at least clean — a failed call left
note data untouched, verified by reading velocities back.

Two lessons kept from it:
- **A bare `except Exception: pass` around an optional property write made this
  undiagnosable.** A build where `MidiNote` has no `probability` looked identical to
  one that wrote it successfully. The tool now reports `probability_written` /
  `probability_error` rather than hiding the outcome.
- **The tool now reads a note back** (`verify_first_note`) instead of inferring success
  from the absence of an exception. Worth copying anywhere a write can half-succeed.

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

**0. Per-clip scale has no tool at all.**
`set_song_scale` writes only the song-level field, but Live 12 stores scale on every
clip and clips re-assert it (see LIVE-API-FACTS). So the song scale cannot be made to
*stick* on any Set that already has clips. Needed: either a `scale` option on the
clip tools, or an `apply_song_scale_to_all_clips` command. Verify `Clip.root_note` /
`Clip.scale_name` exist via `inspect_lom` before building — do not assume from the
XML field names.

Real cost already paid: a song's key reverted to C Major twice, and the only
available fix is 271 individual clip writes.

**0b. Note probability — now instrumented, still unanswered.**
`modify_clip_notes` now reports `probability_written` and any `probability_error`
instead of swallowing it, and reads a note back as `verify_first_note`. So the
question is diagnosable — but **nobody has run it against Live yet**, so it is not
answered.

What to run: `modify_clip_notes(..., probability=0.65)` on a narrow pitch window, then
check `probability_written`, `probability_error` and `verify_first_note`. That
distinguishes "this build's `MidiNote` has no `probability`" from "the write worked and
`get_clip_notes` cannot read it back".

Then also settle `_add_notes_extended`, which still sets probability with `setattr` on
a `MidiNoteSpecification` *after* construction inside a bare `except: pass` — the same
silent-failure shape that was just removed from `modify_clip_notes`. Notes written
through it come back reading no probability.

Why it matters: the `Tops (probability)` clip in `Love on the Beach` is documented in
that project's `SOUND.md` as 45–75% per note, and reports none. Either it never wrote,
or the readback is blind. Its "breathing" hats are a recorded, Tim-approved part of
the sound and may not actually exist. Same for the rim-shot ghosts in `AC Garage 110`.

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
  a survey silently incomplete. Use `inspect_lom` for a full list. It truncates
  **alphabetically**, so a member late in the alphabet looks absent: the list for
  `Clip` stopped at `is_overdubbing`, and "`Clip` has no member `scale_name`" was
  briefly accepted as proof when the list had simply not reached `s`.
- **`batch` discards read results.** Eight `get_track_volume` commands returned only
  "8/8 succeeded, 0 failed" with no values. Treat `batch` as write-only; any command
  whose point is its return value has to be sent individually. Worth either
  surfacing per-command results or documenting the limitation in the tool
  description, because it silently wastes a round trip.
- **`batch` param semantics bite on `position`.** `insert_device`'s tool-level
  `position=0` means "append", but the wire command reads 0 as index 0, which for an
  audio effect fails with "Insert audio effects after instruments. A valid index
  would be 1." The tool description warns params differ; this is the concrete case.
  Failure was clean (0/3 applied, stopped early), which is the right behaviour.
- **`indices_are_one_based=False` is the safer mode for hand-built batches.** With it
  false nothing is converted, so 0-based indices and real values (beats, times) pass
  through untouched. With it true there is ambiguity about whether a non-index
  numeric ≥ 1 gets converted — passing `destination_time` in beats through a
  converting batch is a silent-misplacement risk.
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
