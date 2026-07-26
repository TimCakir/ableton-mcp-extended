# Live API — verified facts and hard limits

Everything here was verified against a **running Live 12.3.8 Suite**, not recalled
or read from documentation. Published LOM docs are incomplete and
version-dependent; the running instance is the only authority.

**Two ways to check something rather than assume it:**
1. `inspect_lom(target=…, filter=…)` — real properties and methods of a live object.
2. Decode `/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/MIDI Remote Scripts/_MxDCore/LomTypes.pyc`
   — Max for Live's own type table, i.e. the authoritative member list for this build.

`call_lom` reaches anything not yet wrapped, and undocumented C++ signatures
report their expected argument types in the error — that is how a signature gets
discovered instead of guessed.

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

### Wire command names ≠ MCP tool names
e.g. the tool `duplicate_clip_to_arrangement` sends `duplicate_to_arrangement`,
and it takes `destination_time` **in beats**, not bar/beat.

### Repeated destructive calls can trip the permission classifier
Deleting many tracks in a row gets blocked. Ask the user to do bulk deletion by
hand — it is faster for them anyway.

---

## Confirmed impossible (Live does not expose these)

| Thing | Detail |
|---|---|
| **Arrangement clip automation** | `create_automation_envelope` on an arrangement clip → *"Not a session clip"*. Verified with a correctly-resolved parameter, so not a lookup artefact. |
| **Envelopes surviving duplication** | `duplicate_clip_to_arrangement` **strips clip envelopes**. `has_envelopes` reads False on every copy. Re-placing does not help. Together with the row above: **clip automation cannot reach the arrangement at all.** Draw it in the UI. |
| **Positioning arrangement playback** | `start_playback` always restarts from bar 1. `set_song_time` + play, `continue_playing`, `jump_to_cue` + continue all reset. `song.start_time` is read-only. So metering a *specific section* is impossible; fire a scene instead to measure all parts together. |
| **Creating group tracks** | Existing groups can be folded (`fold_state`), never created. |
| **Freeze / flatten** | `is_frozen` / `can_be_frozen` are readable; no method to trigger. |
| **Rendering / exporting audio** | Not in the API. |
| **Follow actions** | Not exposed on `Clip` or `ClipSlot` in 12.3 — neither object has any `follow_action` member. |
| **VST/AU internals** | Opaque unless mapped via Live's Configure mode. Banks and presets are reachable. |

---

## Corrected assumptions (all of these were wrongly believed impossible)

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

Because playback cannot be positioned, measure by **firing a scene** so all parts
sound together, from a known unity-gain baseline. Reverb and delay tails bleed
into a sample window for several seconds after a section ends — do not read that
as a track still playing.
