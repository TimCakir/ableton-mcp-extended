# Live API — verified facts and hard limits

## September 8, 2026: latency in Live 12.4.5

The disposable acceptance Set exposed `Device.latency_in_samples` and
`Device.latency_in_ms` through the Python Live objects. Its Main Limiter reported
128 samples / 2.902494331 ms. The vocal Multiband Dynamics reported zero in this
specific stopped Set; do not treat this as a universal device/preset constant.
Both `BECKY VOX` and `BECKY GTR` exposed `current_monitoring_state=2` (Off).

`inspect_lom` searches for latency/delay on the inspected Song and Track objects
did not expose buffer, track-delay or global compensation controls. This is an
observation of those objects and this build, not proof that every possible control
interface lacks them. `get_latency_report` reports those settings as unobserved;
`configure_monitoring` only writes track monitoring, and the local timing analyzer
measures recorded threshold edges. See [1.6.0 tool behavior](RELEASE-1.6.0.md).

The final `.8` workflow verified `current_monitoring_state` writes from Off (2)
to Auto (1) and back to Off on both Becky tracks, with next-tick and independent
readback. The restored Off values passed after save/unload/reopen; see the
[1.6.0 acceptance report](LIVE-ACCEPTANCE-1.6.0-2026-09-08.md). No buffer, driver,
track-delay or clock preference was written.

The Audio Settings UI, read without changes on the same date, showed CoreAudio,
No Device for input, MacBook Pro Speakers for output, 44.1 kHz, a 256-sample buffer,
9.00 ms displayed output/overall latency and 0.00 ms Driver Error Compensation.
These UI observations are not values read by the latency-report MCP tool and do
not establish a connected TR/Apollo path or physical monitoring latency.

Published semantics matter: Live retains latency when a device activator is off;
Reduced Latency When Monitoring does not remove latency along the monitored
track's own downstream path; Keep Monitoring Latency in Recording changes
recorded placement. Refer to the primary sources in the
[TR/vocal/guitar latency guide](LATENCY-GUIDE.md), not inferred meanings from
property names alone.

## Historical Live 12.4.3 observations

> The following sections retain earlier observations. Current installation
> instructions are in [INSTALLATION.md](../INSTALLATION.md); September workflow
> acceptance is recorded in the versioned release reports. Always check the loaded
> build before applying older observations to a new run.

The historical observations here were verified against a **running Live 12.4.3 Suite**, not recalled
or read from documentation. Published LOM docs are incomplete and
version-dependent; the running instance is the only authority.

Re-verified on 12.4.3 after the 12.4.1 → 12.4.3 update: `song.start_time` is
still writable, playback still starts from it (meters at bar 33 showed CHORDS at
`0.81` and PLUCK/ARP at `0.57`, both silent at bar 17, with FX/RISER still silent
until bar 37), and automation recording still writes real arrangement automation
(`automation_state` 0 → 1, replaying 0.15 → 0.55 → 0.95 unattended).

**Read the published reference too.** This file previously argued the running
instance is the only authority, and used that to skip the docs entirely. The
running instance is authoritative about *what exists*; it says nothing about
*semantics*. Cycling '74's LOM reference documents units, valid ranges, enum
meanings and deferral behaviour that no amount of `inspect_lom` will reveal —
including the deferred-write rule that was rediscovered here three times the
hard way. Start at
<https://docs.cycling74.com/apiref/lom/> (per-class reference) and
<https://docs.cycling74.com/userguide/m4l/live_api_overview/>.
It documents the Max for Live wrapper over the same object model, so member
lists can lag this build — cross-check existence with `inspect_lom`, and take
semantics from the docs.

**Two ways to check what exists rather than assume it:**
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

### Deferred writes are a documented Live behaviour, not a quirk of ours

Cycling '74's LOM reference says of `Clip.warping`: *"Internally, Live will
defer the setting of this property. This has the consequence that if you are
sequencing API calls from a single event, the actual order of operations may
differ from what you'd intuitively expect."*

The same class of deferral was hit independently here on `record_mode`,
`is_playing` and `arm` — three separate bugs, each diagnosed from scratch,
before reading that this is a known and documented property of the API. **Any
write may not be visible to a read in the same tick.** Never confirm a write by
reading it back immediately; report the intent, or re-read on a later tick.

The Max for Live guidance is the same rule in its own idiom: *"changes to a
Live Set and its contents are not possible from a notification"*, fixed with
`deferlow`. `schedule_message` is the remote-script equivalent and is why the
recorders run on Live's tick rather than in a loop.

### The same property can carry different UNITS

`loop_start`, `loop_end` and `playing_position` are in **beats** for MIDI and
warped audio, and in **seconds** for unwarped audio. No error is raised either
way, so a caller thinking in bars silently writes seconds. `manage_clip_region`
now reports which unit applies.

Other documented values worth not guessing:

| Property | Documented values |
|---|---|
| `count_in_duration` | 0 = None, 1 = 1 Bar, 2 = 2 Bars, 3 = 4 Bars |
| `tempo` | 20.0 – 999.0, and may itself be automated |
| `clip_trigger_quantization` | 0 = None … 13 = 1/32 |
| Warp marker BPM | segments must stay within [5, 999] |
| `song.file_path` / `name` | **empty until the Set has been saved** |
| `visible_tracks` | excludes tracks inside a folded group |

`Clip.start_time` also means different things by context: for arrangement clips
it is the offset in the arrangement; for session clips it is *the time the clip
was started*, and it **can be negative**. Only ever range-filter it on
`arrangement_clips`.

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

### Scale is stored per CLIP as well as per song — `set_song_scale` is not enough

Live 12 keeps a `<ScaleInformation>` block (`Root`, `Name`) on **every clip**, not
only on the song. A clip bakes in the song's scale **at creation time** and then
keeps it.

Consequences, all measured on `Love on the Beach` (2026-07-26):

- `set_song_scale` writes only the song-level field. In that Set, **271 of 280 clips
  carried `Root=0, Name=0` (C Major)**; the only 9 holding `Root=9, Name=1` (A Minor)
  were the ones created *after* the call.
- With `InKey = true`, the global key display follows the selected clip, so a Set
  full of stale clips **re-asserts the old scale** and the next save persists it.
  This reverted twice and read as spontaneous.
- The origin was the **template**: its song-level scale was C Major, so every clip
  in every song grown from it was born in C Major. Fix a template's scale while it
  is still empty — there are no per-clip fields to chase, which is the whole
  asymmetry. `set_song_scale`'s own docstring already says exactly this ("worth
  setting in a template so every new clip starts in the right key"); the knowledge
  was present and simply not applied.
- `Name` is an index into Live's scale list: `0` = Major, `1` = Minor.
- **Nothing is transposed by any of this.** It is editor highlighting and
  scale-aware-device behaviour, not note data.

**Per-clip scale cannot be reached from the API at all — CONFIRMED IMPOSSIBLE.**
On 12.4.3, `inspect_lom(target="clip", filter="scale")` and `filter="root"` both
return nothing. `Clip` exposes no scale or root member. The fields exist in the
`.als` XML but there is no LOM property behind them, so **no tool can be built for
this** — the only route is offline `.als` editing with Live closed. Do not file it
as a missing wrapper.

Near-miss worth recording: `call_lom(path="…clip", member="scale_name")` returned
"has no member 'scale_name'. Available: …" and that list was accepted as proof.
It was not — **the list truncates alphabetically** and stopped at `is_overdubbing`,
never reaching `r` or `s`. `inspect_lom` with a filter is what actually settled it.
A truncated member list is not evidence of absence.

### A stale arrangement LOOP BRACE silently zeroes every measurement

`measure_section` returned `--` for all fourteen tracks *and* the master, with
"Never audible in this section" for everything — on bars that demonstrably had
clips. Cause: a loop brace left enabled at bars 89–110 by an earlier
`play_section(..., loop=True)`. Playback gets pulled back inside the brace, so the
requested window never sounds.

It looks exactly like a routing or mute fault, and it cost two full real-time
measurement passes. **Clear the loop before metering** —
`play_section(from_bar=1, loop=False, play=False)` — and treat "master reads `--`"
as "the transport is not where you think", never as "the tracks are silent".

### Reading real parameter units without changing anything

`DeviceParameter.str_for_value(value)` converts a raw value to its display string:

```
path="tracks.2.devices.1.parameters.2", member="str_for_value",
  args=[0.8]                                        → "5.00 : 1"
```

This is how to read a ratio, a time or a dB figure **without writing** to the
parameter. The alternative — calling `set_device_parameter` with the value it
already has, to see the reply — works but is a write, and on a Set someone else is
mixing in that is not free.

Companions: `value_items` lists a quantized parameter's options in order (that is
how EQ Eight's filter-type list was found), and `min` / `max` reveal whether a
parameter is normalised 0–1 or in real units. EQ Eight's `Frequency` is
normalised, so `set_value=130` is rejected with "Invalid value. Check the
parameters range with min/max".

### EQ Eight: band 1 is a Low Shelf, and frequency is logarithmic

Two traps, both hit in one session:

- **Band 1 defaults to `Low Shelf`, not a high-pass.** Setting `1 Frequency A`
  without first setting `1 Filter Type A` produces a shelf at that frequency, which
  is not the low-cut anyone intends. Type list order:
  `0` High Pass 48dB · `1` High Pass 12dB · `2` Low Shelf · `3` Bell · `4` Notch ·
  `5` High Shelf · `6` Low Pass 12dB · `7` Low Pass 48dB.
  Set it by raw index through `call_lom` rather than guessing a normalised value.
- **Frequency is logarithmic over 10 Hz – 22 kHz:**

  ```
  normalised = ln(freq / 10) / ln(2200)
  ```

  Verified exact on four bands: 0.323→120 Hz, 0.333→130 Hz, 0.433→280 Hz,
  0.442→300 Hz. `set_device_parameter` echoes the resolved Hz, so always read the
  reply rather than trusting the sum.

### Meters cannot resolve sidechain ducking on a sustained tone

A sine SUB with a correctly configured 5:1 sidechain from DRUMS measured
**0.734 peak / 0.734 avg** — and measured *identically* with the compressor
bypassed, and identically again in a section with 9 kicks per 4 bars versus one
with 16.

Do not conclude "the sidechain is broken" from that. The bypass A/B is the point:
if the compressor were applying constant gain reduction, bypassing it would have
*raised* the level, and it did not move by 0.001. So either it applies no reduction
or Live's meter ballistics cannot show it, and **meters cannot distinguish those
two**. `bounce_to_audio` over 4 bars and inspect the waveform — pumping is
unmissable there. Meters are for *relative* balance between tracks, not for
verifying dynamics processing.

### `measure_section`'s avg-vs-peak is a dynamics diagnostic

The gap between avg and peak says whether anything is actually moving. On
`Love on the Beach` every track came back with avg within 2% of peak — DRUMS
0.783/0.790, SUB 0.734/0.734 — which identified "nothing in this mix is dynamic"
in one call. No single `get_meters` reading can show that, and it is the kind of
finding that gets misdiagnosed as an EQ problem.

### The API reports memory; only the saved file reports what survived

Reading a value back confirms the **write landed**. It says nothing about whether
the value will still be there after a save. Those are different questions, and
answering the first while reporting the second is how the scale bug above hid for
five consecutive saves.

For anything that must persist, verify the file:

```bash
gunzip -c "Song.als" | tr '>' '>\n' | grep -A2 '<ScaleInformation'
```

`.als` is gzipped XML. Indentation depth distinguishes scope — a block at **depth 2**
is song-level; deeper blocks are per-clip.

**`Backup/` is a save-by-save history.** Live writes a copy on every save, so a
value can be dated to the exact save it changed at. One trap: the timestamp in the
filename is when the **backup** was written, and its contents are the *previous*
version — order by file mtime, not by filename, or you will name the wrong save.
(That mistake was made and corrected the same day.)

### Do not infer a cause from a state change you did not observe
Sends were found at zero after clips were deleted, and this was written up as
"deleting clips resets sends". **It was wrong** — the user had zeroed them by hand.
Nothing in the API had done it.

A second instance, same shape: `root_note=0 / scale_name="Major"` was written up as
"the field was never set". That was a guess about history with no evidence behind
it — the real cause (inherited from the template, then re-asserted by 271 clips) was
readable from the file the entire time.

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

## `get_clip_notes` defaults to beats 0→length, not the clip's start marker (2026-08-28)

Found on mandalina: CHORDS clip with `start_marker=16, loop 16–32` read as **0 notes**;
KEYS clip with `loop 28–44` read as 4 notes instead of 32. Raw `get_notes_extended` on
the same clips returned 51 and 32. The wrapper's default window is `from_time=0,
time_span=clip.length`, and `length` is the loop length — so any clip Tim has recorded
long and then trimmed with the start/loop markers reads empty or partial.

Workaround: pass `from_time=0, time_span=128` (or read `start_marker`/`end_marker` via
`call_lom` first and window on those). Fix candidate in `server.py`: default the window
to `start_marker → end_marker`. **Do not conclude a clip is empty from the default read.**

## Talking to the Remote Script directly: everything is 0-based (2026-08-28)

When bypassing `server.py` and sending commands straight to port 9877 (as done for bulk
note writes on mandalina), `get_track_info` returns `clip_slots[i].index` **0-based**, and
`create_clip` / `set_clip_name` / `add_notes_extended` take **0-based** `track_index` /
`clip_index`. The 1-based numbers you see in MCP tool output are added by the wrapper.
A `s["index"]-1` in a direct script renamed the slot *below* every target on mandalina.
Rule: index by list position, never by the `index` field, when scripting the socket.

## `export_stems` can record silence with correct routing (2026-08-29, imissyou)

Three passes on a 17-track Set (Live 12.4.5): stem tracks were created, routed
(`input_routing_type` = source track, channel `Post Mixer`), armed, status reached DONE, and
every file was full-length at −91 dBFS. One pass caught faint note tails on MIDI tracks only,
which means the source tracks were **not playing the arrangement during the record pass** —
the pass ran with `back_to_arranger` False and all session clips stopped. Cause unknown.
Before relying on stems: run `bounce_to_audio(source="Resampling")` for 2 bars and check the
file's level with `ffmpeg -af volumedetect`; then one `source="<track>"` bounce; compare.
Related: `song.back_to_arranger` cannot be cleared by setting it while the transport is
stopped; `play_section` clears it as a side effect.

## Turning warping OFF leaves `end_marker` in the old beat units (2026-09-04, live set)

Importing a 432 s stem with `import_audio_file` auto-warps it; `set_clip_properties(warping=False)`
then leaves `end_marker` / `length` at the *beat* value from the warped state (864 at 120 BPM)
even though an unwarped clip's markers are read in **seconds**. One drums stem, auto-warped at
a different detected tempo, was left with `end_marker` 431.5 — read as beats that looks like a
half-length clip; read as seconds it is the full file. `length` never updates at all.
**Fix: after `warping=False`, write `end_marker` = file duration in seconds** via `call_lom`
(Live clamps anything larger to the sample length). Verify on `end_marker`, not `length`.

## Four more traps from the live-set build (2026-09-04)

- **`create_audio_track(index=N)` / `create_midi_track(index=N)` insert AFTER position N**
  (the wrapper passes N straight to Live's 0-based insert). With one track present,
  `index=1` lands the new track at position 2. Build in order, then read the overview.
- **Importing into a freshly created, still auto-named track renames the track to the clip
  name** (`2-Audio` became `2-YL 32 Intro · Drums`). Name tracks after the first import, or
  re-name afterwards.
- **`call_lom` inside `batch` does not apply `set_value`** — it reads and reports the
  unchanged value as success. Property writes need the dedicated `call_lom` call. Confirmed
  again 5 Sept 2026 for track/clip `name` and `color` (34 writes, all no-ops).
- **Re-timing an arrangement audio clip without moving it (5 Sept 2026, Live 12.4.5):**
  `start_time` has no setter, but on a non-looping arrangement clip `loop_start` sets which
  file position plays at the clip's (unchanged) arrangement start and `loop_end` sets where
  the clip ends (`end_time = start_time + loop_end − loop_start`). `end_marker` alone does not
  change the length. So "move this clip 20 bars earlier" = trim the neighbour with `loop_end`,
  create a new clip from the same file at the target position, set `loop_start`/`loop_end`.
  A freshly created clip inherits the file's saved `.asd` markers, not 0/end — set both.
- **`create_arrangement_audio_clip` ignores `start_beat`** (bar 200 + 1.5 beats landed at
  bar 200) and in that call left TWO clips at the bar. For off-bar positions call
  `Track.create_audio_clip(path, position_in_beats)` through `call_lom` — a float position works.
- **Default-named tracks renumber when a track is inserted** (`20-Audio` → `21-Audio`), and
  every index after the insert shifts. If the user is in the Set, re-read the overview right
  before writing and check the `was` field of each `call_lom` result — that is how a write to
  the wrong track was caught on 5 Sept 2026.
- **Unwarped-clip marker order:** `start_marker` cannot be set directly (it follows
  `loop_start`); `loop_end` cannot go below `end_marker`. Working order: `end_marker`,
  then `loop_end`, then `loop_start`. At one dedicated call each this is too slow for
  hundreds of clips — pre-cut the audio with ffmpeg and import the pieces instead.

## What the API cannot do, the saved file can (2026-09-04, live set)

Verified by editing `Another Couple Live.als` with the Set closed, then reopening in 12.4.5:

- **Group tracks**: insert a `<GroupTrack>` block copied from a Set Live 12 wrote (one
  `<GroupTrackSlot>` per scene, FreezeSequencer `<ClipSlot>` per scene, all pointee `Id`s
  ≥ 1000 renumbered above `NextPointeeId`, which is then bumped), placed before its first
  member; set each member's `<TrackGroupId>` to the group's track Id. Members must be
  adjacent. Script: `Another Couple/05-Live/_tools/group-tracks.py`. Gotchas: a Set with
  zero tracks, or a track whose send count differs from the return count, is refused as
  "corrupt"; the top-level `<ClipSlot Id="n">` nests a same-named `<ClipSlot>` child, so
  split on indentation, not regex.
- **Blank tracks with hardware routing (2026-09-06, writing template)**: clone an existing
  `<AudioTrack>`/`<MidiTrack>` from the same Set (slot count then matches the scene count),
  strip the track-level `<Devices>…</Devices>` (the one at 6-tab indent) to `<Devices />`,
  renumber pointee Ids ≥ 1000, give it a fresh track Id. Routing targets:
  `AudioIn/External/M<n-1>` = mono Ext. In n, `AudioIn/External/S<k>` = stereo pair
  2k+1/2k+2, `AudioOut/GroupTrack` for a grouped child. `<MonitoringEnum>` (first
  occurrence = MainSequencer): **0 = In, 1 = Auto, 2 = Off**. `<TrackDelay><Value>` is ms
  and has no API property at all (`inspect_lom` filter "delay" on a track: nothing).
  Routing to hardware that is not plugged in is disk-only too — the API only offers
  names that currently exist. Script: `Another Couple/01-Production/_tools/tr1000-template.py`.
- **Cmd-N from AppleScript** (`System Events` keystroke) opens a fresh default Set with no
  prompt when the current Set is unmodified; the MCP socket drops once during the reload,
  so retry the first call after it.
- **Follow actions**: `<FollowAction>` per clip. `FollowActionA` 4 = Next, 1 = Stop
  (matches Tim's own Ruins Set), `FollowActionEnabled` true, `IsLinked` true = time follows
  clip length. Not readable through the API afterwards — verify by re-reading the file.
- **Warping with exact markers**: `<IsWarped>` true plus two `<WarpMarker>`s,
  (0 s, 0 beats) and (file length, bar count × 4). `WarpMode` 0 = Beats, 6 = Complex Pro.
  Also set CurrentEnd / LoopEnd / OutMarker / HiddenLoopEnd to the beat count and
  `<LoopOn>`. Script: `_tools/warp-loop-follow.py`.

## `delete_track(track_name=...)` deleted the wrong track (2026-09-04)

Called with `track_name="STEM 01 Kick"` on a 55-track Set it deleted **track 1 (GKIK)** and
reported success. Never delete by name; read the overview and delete by index, and read the
overview again afterwards. Same class of failure as stale indices: a confident success
message about the wrong target.

## `.als` ClipSlot Ids are not row positions (2026-09-04, late)

Inside `<ClipSlotList>` the outer `<ClipSlot Id="n">` Ids are allocation order, not scene
order: scenes inserted later get new Ids while keeping their position, and every row after
them keeps its old Id. A disk pass that keys per-row data (tempo, follow action) off the Id
silently applies the wrong song's tempo to shifted rows — clips came back at 61.94 or 66.13
beats instead of 64. Key everything off the element's **position** in the list. Same rule
for `<Scene Id>` and, as already documented, for track indices over the API.

## `batch` + `call_lom set_value` on device parameters: silently no-op (5 Sept 2026, Live 12.4.5)

Nine `call_lom` writes of `master_track.devices.N.parameters.M` `value` inside one `batch` all
returned success and changed nothing (read-back identical). The same calls made directly through
`call_lom` applied immediately (`was`/`now` reported). `set_from` routing writes inside `batch`
did apply. Until the cause is found: **write device parameter values with direct `call_lom`
calls, use `batch` for reads and routing.** Also: `master_track` is reachable only through
`call_lom` (`insert_device(name, index)` works there); the indexed tools stop at the last return.

## Track output meters can read 0.0 on a track that is audibly playing

Four audio tracks (drum stems, Beats warp, inside a group) reported `output_meter_level` 0.0
while their clips played and the master carried them (solo + master meter proved it). Cause
unknown. **Never conclude silence from one track meter** — solo it and read the master.
