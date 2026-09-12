# Live acceptance: latency and monitoring 1.6.0

Date: September 8, 2026. Live 12.4.5. Package 1.6.0, build `2026-09-08.8`,
protocol 2.1. The full automated suite passed **1,196 tests in 12.43 seconds**,
including 50 report cases, 88 monitoring cases and 24 timing-analysis cases.
Independent review found no remaining blockers. Fresh stdio acceptance servers
matched the repository and Live's loaded build/source hashes, and exposed all
155 tools with the expected read-only/mutating annotations.

## Fixture and hardware limits

Only the existing disposable Set was used:

```text
dist/live-acceptance-2026-09-08/MCP Acceptance 2026-09-08 Project/MCP Acceptance 2026-09-08.als
```

It contained 38 regular tracks, including one group, plus three returns, at
110 BPM and 4/4. Transport and recording were off. `BECKY VOX` and `BECKY GTR`
started and ended with Monitor Off. The TR kick track was `TR BD - Kick`.

A read-only Audio Settings UI check showed CoreAudio, **No Device** for input,
MacBook Pro Speakers for output, 44.1 kHz and a 256-sample buffer. Displayed
output/overall latency was 9.00 ms and Driver Error Compensation was 0.00 ms.
No audio preference was changed. These values were observed through the UI,
not supplied by `get_latency_report`. This is software acceptance, not a test
of a selected TR/Apollo input or what a performer hears.

## Workflow result

Workflow acceptance passed at **20:21:54 Istanbul time**:

- Full report covered all tracks and Main, inspecting **122 devices and 73 rack
  chains** with `complete=true`, no read errors and no truncation. The subset
  included the requested vocal, guitar and TR kick tracks, the TR parent group,
  all returns and Main.
- Main Limiter reported **128 samples / 2.902494331 ms**, matching an independent
  direct Live property read. This is its reported device delay, not a measured
  total headphone or hardware round trip.
- Stale Set/handle, duplicate target and MIDI-track monitoring previews were
  rejected without changing the whole-Set snapshot or monitoring report.
- A two-track Live-monitoring preview left the Set unchanged. Apply returned
  `applying`, then `applied` after next-tick verification of Auto on both tracks.
  Independent direct reads confirmed Auto with the original arm and routing state.
- Retained apply replay made no further change. Restore returned `restoring`,
  then `restored` with verified Off on both tracks. Further apply and restore
  calls replayed the retained restored result without writes.
- The whole-Set snapshot, every reported device/routing/monitoring field and
  stopped transport matched the baseline after restoration.

The runner created a separate four-second, mono, 16-bit PCM WAV at 8 kHz.
Five single-sample impulses had known offsets of 70, 50, 7, 8 and 9 ms. Through
the actual stdio tool, the analyzer excluded the first two startup hits and
reported three accepted hits, **8 ms median, 1 ms sample SD and 2 ms range**.
The source file hash stayed unchanged. This synthetic signal verifies arithmetic
and MCP output, not physical calibration.

## Save and reopen

The restored Set was saved through Live's UI, unloaded into Untitled and reopened
from the recent-files menu. Read-only persistence acceptance passed at
**20:23:30 Istanbul time**, with a different Set session ID. Track/device/clip
snapshots, full and subset latency reports, direct monitoring/routing reads,
transport and the unchanged synthetic analysis matched the workflow evidence.
This verifies the restored Off state after save/reopen; Auto was verified during
the workflow but was not saved across a reload. Retained restore plans themselves
are process-local and are not saved in the Set.

## Historical recording check

The new analyzer also read the two retained September 5 TR reference WAVs without
editing them. References were the 32 beat times at the audit's 129.160003662 BPM,
with the first two excluded, using the default −40 dBFS threshold and 50 ms quiet
interval. The adjusted `0002 [2026-09-05 225340]` recording produced all 30 settled
hits: **6.7236 ms median offset and 0.6402 ms sample SD**. The earlier
`0001 [2026-09-05 225012]` recording produced only 18 accepted settled hits and
correctly reported `complete=false`; its matched-hit median was 58.9213 ms.

These are new threshold measurements of old recordings. The original audit used
an adaptive derivative/template method, so exact onset values differ. Neither
the earlier clock correction nor these offsets are a preset for the current rig.

## Evidence and installation state

Runner: `tests/integration/latency_acceptance.py`. It verifies the exact Set path,
build hashes, tool discovery and fixture, journals every request before dispatch,
and requires successful prior evidence plus a fresh Set instance for persistence.

Local evidence, excluded from Git:

- `dist/live-acceptance-2026-09-08/latency-workflows-1.6.0.json`
- `dist/live-acceptance-2026-09-08/latency-workflows-1.6.0.synthetic.wav`
- `dist/live-acceptance-2026-09-08/latency-persistence-1.6.0.json`
- `dist/live-acceptance-2026-09-08/tr-historical-threshold-analysis-1.6.0.json`

The Remote Script was deployed and Live loaded the final `.8` source. The Python
wheel and source archive were built locally; their ten MCP server Python files
matched source. The Remote Script remains a separate source-checkout installation.
Codex's existing MCP process still holds `.4` and needs a host refresh; both
accepted runs used fresh `.8` processes. Remote CI, publication, Live 11 behavior
and new physical latency calibration are not claimed.
