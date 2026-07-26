# Live API — verified facts and hard limits

Everything here was verified against a **running Live 12.4.3 Suite**, not recalled
or read from documentation. Published LOM docs are incomplete and
version-dependent; the running instance is the only authority.

Re-verified on 12.4.3 after the 12.4.1 → 12.4.3 update: `song.start_time` is
still writable, playback still starts from it (meters at bar 33 showed CHORDS at
`0.81` and PLUCK/ARP at `0.57`, both silent at bar 17, with FX/RISER still silent
until bar 37), and automation recording still writes real arrangement automation
(`automation_state` 0 → 1, replaying 0.15 → 0.55 → 0.95 unattended).

**Two ways to check something rather than assume it:**
1. `inspect_lom(target=…, filter=…)` — real properties and methods of a live object.
   This is the authority.
2. `strings` over `/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/MIDI Remote Scripts/_MxDCore/LomTypes.pyc`
   — Max for Live's own type table. Useful as a fast "does this name exist
   anywhere" oracle, but **it is a subset, not the full API**: `Clip` really has
   `automation_envelope`, `create_automation_envelope` and `automation_envelopes`,
   and none of the three appear in that table. Never conclude something is absent
   from its silence — confirm with `inspect_lom`.

`call_lom` reaches anything not yet wrapped, and undocumented C++ signatures
report their expected argument types in the error — that is how a signature gets
discovered instead of guessed.

---

## Before believing any limitation: check you are asking a current build

**This is the single largest source of false limitations in this repo.** Three
copies of the integration run at once and drift apart within hours:

| Copy | Reloads when |
|---|---|
| the repo working tree | immediately (it is just a file) |
| the remote script Live loaded | **Ableton Live restarts** |
| the MCP server process | **the MCP host (Claude Code) restarts** |

Editing a file changes nothing for the two running processes. A command added an
hour ago is genuinely absent from a Live started two hours ago — and the failure
looks exactly like a Live API limitation.

This has happened repeatedly. At the time of writing, arrangement-clip note
editing was fully implemented and *uncommitted*, so `get_clip_notes` had no
`arrangement` parameter in the running server while the working tree had it —
producing "arranged material cannot be edited", which was never true.

**Run `get_build_info` first, every time.** It reports all three builds and says
which is stale. Deploy with `./deploy.sh`, then restart Live.

---

## Install

Live 12 loads remote scripts from **`~/Music/Ableton/User Library/Remote Scripts/AbletonMCP/`**.
The repo's INSTALLATION.md gives the Live 9/10 path (`~/Library/Preferences/Ableton/<ver>/User Remote Scripts/`),
which Live 12 ignores — the script simply never appears in the Control Surface
dropdown. Deploy to both. Live must be **restarted** to reload the script;
the MCP server needs **Claude Code restarted** to register new tools.

---

## Traps that have actually caused damage

### Track indices shift silently
Adding or deleting a track renumbers everything below it. Commands then write to
the *wrong track* and every call still reports success. This corrupted a whole
build twice.
**Always call `get_session_overview` before any indexed operation.**

### `DeviceParameter.name` is abbreviated
Wavetable's cutoff reports as `Flt 1 Freq` in `.name`, but the UI and
`.original_name` say `Filter 1 Freq`. Match **both**, or automation calls fail
with "parameter not found" while `set_device_parameter` works fine.

### A stopped transport does not evaluate automation

Reading a parameter while stopped returns whatever value it is holding, not the
automated value at the playhead. Confirmed on 12.4.3: with a recorded envelope
present and the playhead at beat 1.28, `volume` read `0.85` while stopped and
`0.15` — the real value there — as soon as the transport rolled.

So a parameter read that "proves" automation is absent proves nothing unless the
transport was actually rolling.

### `song.is_playing` lags `start_playing()` by several ticks

Not one tick — several. Measured on 12.4.3: a recorder that treated two
consecutive `is_playing == False` readings as "the user stopped" ended its pass
after ~2 ticks having written nothing, and reported success. The same pass
judged on `current_song_time` instead ran to completion with 44 points.

**Never use `is_playing` as a liveness signal.** Use the playhead: wait for
`current_song_time` to advance past where it started, and treat a position that
stops advancing as the stop. `is_playing` is fine for a one-off "is something
happening" question and useless for control flow.

### Creating a track silently disarms whatever was armed

Observed, not inferred: `create_midi_track` left the new track armed, and Live's
exclusive-arm behaviour unarmed the track that had been armed before it. The new
track was then deleted, and the original track stayed unarmed — the arm was gone
with no call having touched it.

Nothing reports this. If a scratch track is created for a test, **read the arm
state first and put it back**, the same way track indices have to be re-read
after any structural change.

### Wire command names ≠ MCP tool names
e.g. the tool `duplicate_clip_to_arrangement` sends `duplicate_to_arrangement`,
and it takes `destination_time` **in beats**, not bar/beat.

### Do not infer a cause from a state change you did not observe
Sends were found at zero after clips were deleted, and this was written up as
"deleting clips resets sends". **It was wrong** — the user had zeroed them by hand.
Nothing in the API had done it.

The reasoning error: a state change was noticed, a plausible mechanism was
invented, and it went straight into the docs as a verified fact without being
reproduced. A real finding needs the cause *observed*, not inferred — read the
value, make the change, read it again. Anything less is a hypothesis and should be
labelled as one.

### Do not widen one failed call into a whole missing capability

The costliest mistake in this file. `create_automation_envelope` fails on
arrangement clips — true, and re-verified. From that came "arrangement automation
is impossible", which is false: automation reached the arrangement fine through a
different mechanism that was never tried. One API door being shut is evidence
about that door, not about the room.

Before writing *impossible*, name the mechanism that was tested and ask what else
Live itself uses to do the same job — here, the answer was the record button.
Prefer wording like "`create_automation_envelope` refuses arrangement clips" over
"arrangement automation is impossible": the first stays true, the second was wrong
within a version and blocked real work in the meantime.

**The question that keeps working: "how does a human do this in Live, and is
_that_ path exposed?"** Every entry below was recorded as impossible because one
obvious API call failed. None of them were.

| Written off as | The call that failed | What a human does instead | Reachable? |
|---|---|---|---|
| Arrangement automation | `create_automation_envelope` | arm record, move the fader | yes |
| Section metering | `start_playback` "always bar 1" | drag the start marker | yes — `start_time` |
| Rendering audio | no export call | resample onto an audio track | yes |
| Freeze | `is_frozen` has no setter | bounce the track, disable the original | yes, in effect |
| Editing arranged clips | — | — | it was already built, just stale |

Four of the five were **real-time transport tricks**, not data-model calls. That
is the shape of this API: it is thin on offline operations and rich in things you
can drive the transport into doing. When an offline call is missing, look for the
real-time route before concluding anything.

### Repeated destructive calls can trip the permission classifier
Deleting many tracks in a row gets blocked. Ask the user to do bulk deletion by
hand — it is faster for them anyway.

---

## Confirmed impossible (Live does not expose these)

| Thing | Detail |
|---|---|
| **Clip envelopes on arrangement clips** | `create_automation_envelope` on an arrangement clip → *"Not a session clip"*. Verified again on 12.4.1 with a correctly-resolved same-track parameter, so not a lookup artefact. **This is a limit on clip envelopes only — see below, arrangement automation itself is writable.** |
| **Envelopes surviving duplication** | `duplicate_clip_to_arrangement` **strips clip envelopes**. `has_envelopes` reads False on every copy. Re-placing does not help. |
| **Creating group tracks** | `song` exposes only `create_midi_track`, `create_audio_track`, `create_return_track`, `create_scene`. Existing groups can be folded (`fold_state`), never created. |
| **Freeze / flatten** | `is_frozen` / `can_be_frozen` are readable; setting `is_frozen` raises *"property of 'Track' object has no setter"*. No method triggers it. **But `bounce_to_audio(source="<track>")` achieves the same end** — resample the track, then disable the original. |
| **Rendering / exporting audio** | No render or export call exists. **This does not mean audio cannot be produced** — see the resampling entry below. |
| **Follow actions** | Not exposed on `Clip` or `ClipSlot` in 12.4.1 — `Clip` has `launch_mode` and `launch_quantization` but no `follow_action` member, and `ClipSlot` has none either. |
| **VST/AU internals** | Opaque unless mapped via Live's Configure mode. Banks and presets are reachable. |

---

## Corrected assumptions (all of these were wrongly believed impossible)

- **Audio can be rendered, by resampling in real time.** "Rendering / exporting
  audio: not in the API" was true of *export* and false of the goal. An audio
  track accepts **`Resampling`** (the main bus) or **any individual track** as
  its INPUT, so arming one and rolling the transport writes a real file into
  `Samples/Recorded`. Verified: bars 33-35 produced an 843 KB 48 kHz stereo
  AIFF, 4.39 s, **peak −8.5 dBFS, RMS −20.9 dBFS** — measured off the SSND
  chunk, because a plausible file size is not evidence of audio. Wrapped as
  **`bounce_to_audio`**. One mechanism covers mixdown, stem export, and a
  freeze/flatten stand-in. Real time: 32 bars costs 32 bars.
- **`song.file_path` and `song.name` say which Set is open.** Live swaps
  documents silently and every call then addresses the new one. An afternoon
  went into "the arrangement is empty" that was really "you are looking at the
  template". `get_build_info` now reports both, next to the build check.
- **`song.exclusive_arm` / `exclusive_solo` are readable and writable.** This is
  the mechanism behind the arm theft documented above — it reads `True` here.
- **`Song.delete_return_track` exists.** Return tracks were creatable but not
  deletable purely because it was never wrapped.

- **Arrangement automation is writable.** Previously recorded here as flatly
  impossible. Two true facts — Live refuses clip envelopes on arrangement clips,
  and duplication strips envelopes — were generalised into a third claim that
  does not follow. Arrangement automation is not clip automation; it is **track
  automation**, and Live writes it the way it does for a hardware fader: arm
  arrangement record (`song.record_mode = True`), roll the transport, move the
  parameter. Verified on 12.4.1: `automation_state` went `0 → 1` and the fader
  then replayed the recorded shape unattended (0.2 → 0.2 → 0.5 while playing,
  untouched). Wrapped as **`record_arrangement_automation`**, which runs off
  Live's tick so the UI never blocks. It records in real time — a 16-bar sweep
  takes 16 bars.
- **`song.start_time` is writable, so playback CAN be positioned.** Previously
  recorded as read-only, which led to "metering a specific section is
  impossible". Setting `start_time = 64` then `start_playing()` began playback at
  bar 17, confirmed by `current_song_time` and by meters showing exactly the
  tracks that have clips there (CHORDS, PLUCK and FX silent — they do not enter
  until bar 33+). Wrapped as **`play_section(from_bar, to_bar)`**. `song.loop`,
  `loop_start` and `loop_length` give a second way to scope a section.
- **Arrangement clips can be read and edited.** The note and region tools take
  `arrangement=True` and resolve through `_resolve_clip`. When this appeared
  impossible, the code existed but was uncommitted and undeployed — see the
  staleness section above.
- **Sidechain routing works.** `Compressor`, `Gate` and `Auto Filter` expose
  `input_routing_type` with a full `available_input_routing_types` list — that
  *is* the sidechain source. **Glue Compressor exposes no routing at all**, so it
  cannot be sidechained. Assigning needs the RoutingType **object**, not a
  string (use `set_from`).
  Turn **`S/C EQ On` off** — it defaults to an 80 Hz high-pass that filters out
  the very kick fundamental meant to trigger it.
- **Wavetable's modulation matrix works.** Signature is
  `set_modulation_value(int target_index, int source_index, float value)` — integer
  indices, not parameter objects. A parameter must first be added via
  `add_parameter_to_modulation_matrix` before it appears in
  `visible_modulation_target_names`.
- **`move_device` exists**, so device order is fixable in place — no need to
  delete and rebuild a chain to get an EQ in front of a compressor.
- **Warp markers are editable** — `add_warp_marker`, `remove_warp_marker`,
  `move_warp_marker`, `warp_markers`.

---

## Device classes

**Plain `Device` — `parameters` only, nothing more:**
`Operator, Analog, Sampler, Collision, Tension, Electric`. These were always at
100% coverage; there is nothing extra to wrap.

**Carry extra members:** Drift (+29, full mod matrix), Rack (+22), Simpler (+20),
Looper (+16), SpectralResonator (+12), HybridReverb (+8), Wavetable, Meld (+4),
Eq8 / Roar / Shifter (+3 each).

**Enum properties come in `_index` / `_list` pairs.** Always resolve a name
against the `_list` and set the `_index`. Never assume an ordering. Some Simpler
enums have *no* companion list, so accept a raw index as the escape hatch.

---

## Value tapers (measured, not assumed)

| Control | Mapping |
|---|---|
| Track fader | `0.85` = 0 dB, `1.0` ≈ +6 dB. **Compressive near the top** — a 6 dB cut barely moves the meter above 0.85. |
| Sends | `0.5` = −20 dB, `0.7` = −12 dB. `0.08` = −58 dB, i.e. silence. Percentages are *not* dB. |
| Device params | `set_device_parameter` takes normalised 0–1 and reports the real value back. **Always read the reply** rather than trusting the mapping. |

When a device is too loud, fix it at the **device's own volume**, not the fader —
a preset with high internal gain will resist a −10 dB fader (the "Andreas Wide
Pad" case).

---

## Metering

`get_meters` reads per-track output levels. It is not hearing, but it makes level
*relationships* measurable — what is loudest, what is clipping, what is inaudible.

Playback **can** be positioned, so measure the section you actually care about:
`play_section(from_bar=33)` then `get_meters`. Firing a scene still works for
judging session material. Reverb and delay tails bleed into a sample window for
several seconds after a section ends — do not read that as a track still playing.
