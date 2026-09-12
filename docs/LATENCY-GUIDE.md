# Recording and monitoring latency

Start by deciding what needs fixing: the delay a performer hears, the position of
a recorded take, or the timing of an external sequencer. These are different
measurements. A recording can land on the grid while monitoring still feels late.
An old calibration value is valid only for the route and settings it measured.

## Choose one monitoring path

| Recording setup | Monitoring path | Live track Monitor |
| --- | --- | --- |
| Becky vocals or guitar through Apollo, with Console effects | Apollo Console to headphones; record the input into Live | Off |
| Vocals or guitar that must pass through Live effects | Live to headphones; mute the parallel direct feed in Console | Auto, then arm the intended recording track |
| TR-1000 analog outputs into Apollo | Choose Console direct monitoring or Live processing for each audible signal | Off for direct; Auto for Live |

Console monitoring avoids the DAW buffer round trip. It still has conversion and
processing delay. Monitoring through both Console and Live produces two audible
versions of the input, which can sound doubled or phasey.
[UA monitoring guidance](https://help.uaudio.com/hc/en-us/articles/360050596812-Why-am-I-Getting-Latency-in-my-DAW-Sessions)

For a dry vocal/guitar capture, Console's **UAD MON** keeps standard insert
processing in the monitor path. **Unison processing is still recorded**, and aux
processing is recorded on the aux feed. Decide what to commit before recording.
Keep Console Input Delay Compensation in mind for stereo or multiple-microphone
recordings; changing it can change channel alignment and reported recording
latency. [UA inserts](https://help.uaudio.com/hc/en-us/articles/25350369296660-UAD-Plug-In-Inserts),
[UA Console latency](https://help.uaudio.com/hc/en-us/articles/43460658366612-Managing-Latency-in-UAD-Console-for-Apollo-the-DAW)

For Live monitoring, choose the lowest buffer that plays cleanly under the actual
session load. Check effects on the input track, its groups and Main track.
Turning off a device's yellow activator **does not remove its latency** in Live.
Use a tracking chain without high-latency devices; do not expect a blanket bypass
to solve the delay. Negative track delay makes other tracks wait and can increase
monitoring delay. [Ableton latency explanation](https://help.ableton.com/hc/en-us/articles/360010545559-How-Latency-Works)

## Understand Live's timing controls

- **Delay Compensation** aligns paths inside the Set. Normally leave it enabled.
- **Reduced Latency When Monitoring** removes compensation for devices elsewhere
  in the Set. Devices on the monitored track and downstream groups/Main still add
  latency. Return effects can lag behind the direct monitored signal.
  [Ableton RLWM FAQ](https://help.ableton.com/hc/en-us/articles/209072249-Reduced-Latency-When-Monitoring-FAQ)
- **Keep Monitoring Latency in Recording** changes recorded placement, not the
  monitoring delay. With In/Auto monitoring, On retains the timing the performer
  heard; Off removes monitoring latency from placement. Choose according to how
  the part is performed and check a recorded take.
- **Driver Error Compensation** corrects inaccurate interface latency reporting.
  Leave it at zero unless an interface loopback test establishes an error. It is
  not a general vocal or drum-machine offset.
  [Ableton recording alignment](https://help.ableton.com/hc/en-us/articles/19450890686876-Recordings-are-out-of-sync)

Buffer size, audio device/sample rate, Live's global compensation options,
per-port MIDI clock delay, Console settings and TR project settings remain manual
controls in this workflow. The monitoring tool changes only the selected tracks'
Monitor mode; it does not configure these controls or establish a physical route.

## Calibrate the TR-1000 for how it is played

**Live triggers the drum notes:** use the appropriate MIDI port/channel and an
audio return. Live's External Instrument device provides the MIDI/audio routing
relationship and accounts for known interface latency. Its Hardware Latency
control is for additional external-device delay. Measure a repeated sharp sound
before choosing a value. [Live 12 External Instrument](https://www.ableton.com/en/live-manual/12/live-instrument-reference/#external-instrument)

**The TR runs its own patterns:** send MIDI clock to the TR, allow several bars
for synchronization to settle, and compare a simple repeated pattern with Live's
reference. Adjust Live's MIDI Clock Sync Delay or the TR's Sync Delay, one control
at a time. A dedicated clock port can improve stability. This calibration does
not establish the timing of individual MIDI note triggers.
[Ableton MIDI synchronization](https://help.ableton.com/hc/en-us/articles/209071149-Synchronizing-Live-via-MIDI)

Roland's Sync Delay values run from −100 to +100: positive advances the sequencer;
negative delays it. The cited support article does not state units, so do not
treat those numbers as milliseconds. Roland advises zero when the TR is master.
**All Tracks** aligns voices to the slowest voice path; **Layered Gens** reduces
delay but stops aligning separate tracks. Do not change this setting just to
reuse a previous offset. [Roland Sync Delay](https://support.roland.com/hc/en-us/articles/43050129615515-TR-1000-Sync-Delay-Setting),
[Roland Track Sync](https://support.roland.com/hc/en-us/articles/43219202586267-TR-1000-Sound-latency-occurs-when-synchronized-with-external-MIDI-devices)

Keep separate measurements for analog audio through Apollo and TR USB audio.
TR USB supports individual-instrument recording and requires Roland's driver.
If USB audio is combined with Apollo inputs/outputs, verify Live's actual device
configuration and sample rate first. USB audio does not automatically enter
Apollo's direct input-monitor path. [TR-1000 reference manual](https://static.roland.com/assets/media/pdf/TR-1000_reference_eng02_W.pdf)

## Use the MCP tools

Read `get_edit_targets` first and use its current `session_id` and stable track
handles. Track numbers can change after insertion or reordering.

`get_latency_report(session_id, track_handles=None)` reads the selected tracks
and device latency information, with parent groups, returns and Main for context.
Omitting the filter requests all tracks. Inspect incomplete/truncated results and
unknown settings before drawing conclusions. It cannot measure acoustic delay, external hardware delay
or a complete headphones round trip. Current public LOM documentation exposes
device latency in samples and milliseconds, but nested device readings must not
be blindly summed into a claimed total.
[Device LOM](https://docs.cycling74.com/apiref/lom/device/)

Use `configure_monitoring` with `action="preview"`, the current `session_id`,
explicit `track_handles` and `monitoring_path="live"` or `"direct"`. Inspect
the proposed changes, then apply the returned `plan_id` with the same session.
Select 1–32 unique regular, unfrozen audio tracks. Recording must be off, and
the tracks' identity, monitoring, arm and routing state must still match preview.
Live means **Auto**; direct means **Off**. Auto still requires the intended track
to be armed. The operation does not arm tracks, start recording, change routing
or turn Console monitoring on. Restore uses the retained plan and checks that
its targets and applied settings still match before restoring prior modes.

Poll the same action and `plan_id` while the result is `applying` or `restoring`.
A preview expires after five minutes before apply. Restore depends on the
retained process-local plan: a Remote Script restart or cache eviction loses it.
Inspect an `error` or `partial` result and its rollback evidence before proceeding.

If a request times out, reconcile its retained request/plan result before sending
another mutation. A timeout is not evidence that nothing changed. A Set reload
requires fresh targets; never reuse old handles or restore across Set instances.

## Measure recording alignment

Record or export a clean, repeated transient test as an integer PCM WAV with a
known time reference. The analyzer accepts 8/16/24/32-bit samples at 8–192 kHz,
with a selected 1-based channel from up to 64 channels. Its analysis region is
limited to the first 120 seconds and 12 million frames.
Then run the local, read-only `analyze_recording_timing` tool with:

```text
path: absolute path to the recorded audio file
expected_onsets_seconds: expected transient times relative to that file's start
search_window_ms: 100
threshold_dbfs: -40
min_separation_ms: 50
channel: 1
skip_initial: 2
```

These are detector defaults, not measured calibration values. Use sharp isolated
hits with silence between them. Supply 3–400 increasing expected times whose
search windows do not overlap; the default startup skip requires at least five
reference hits so three remain. Allow leading silence and enough audio after the
last reference to cover its complete search window.

The detector finds an absolute-amplitude threshold crossing after continuous
below-threshold separation. Boundary, missing and ambiguous hits are excluded;
statistics require at least three matched hits after the startup skip. A positive
offset means the recorded edge is later than the reference. Attack shape, noise
and threshold choice affect this measurement, so inspect individual hits and the
report's completeness. Use the same settings when comparing captures.

An export with an unexplained crop or offset cannot establish hardware latency.
Measure several hits after preroll so a consistent offset can be distinguished
from timing variation. This measures **recorded onset alignment**; it does not
measure what Becky heard in the headphones or the TR's complete live response.

Record the audio/MIDI route, sample rate, buffer, monitoring modes, compensation
settings, plugin path and TR kit/Track Sync with the measurement. Repeat after
changing them. Apply one justified correction, make another recording, and check
the result before keeping it as that setup's calibration.
