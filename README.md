# Ableton MCP Extended

Control Ableton Live through an MCP-compatible assistant. The Python MCP server communicates with a Remote Script running inside Live, exposing tools for tracks, clips, notes, devices, routing, arrangement work and recording.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

## Version 1.2.0

Build `2026-09-08.4` extends `build_arrangement` with MIDI variations and trimmed audio placement. The server still exposes 152 tools. The [1.2.0 release examples](docs/RELEASE-1.2.0.md) show the exact preview/apply arguments and limits.

- **Make MIDI section variations:** remove selected pitches, thin individual notes deterministically, then transpose the survivors. Preview every resulting note before placing a new copy; the source stays unchanged.
- **Place audio excerpts:** copy a Session audio clip onto its own arrangement track at an absolute beat, including fractional positions. Choose source ranges in beats for warped audio or seconds for unwarped audio.
- **Check the whole placement:** reject overlaps, changed sources and active recording; verify the new clip's bounds and content. Retained completed plans replay without creating more copies, and failed operations attempt to remove only their own copies.

Audio sources must have aligned start/end and loop markers. Unwarped audio requires zero pitch offset and fixed, unautomated tempo with Link and tempo follower off. Each audio preview reserves space for the full initial copy and intermediate trimming, so the required empty range can be longer than the final excerpt. Plans support up to 64 placements and 10,000 source MIDI notes; unapplied plans expire after five minutes. The builder does not start playback or save the Set.

The current automated suite passes **792 tests**. Build `2026-09-08.4` passed actual Live 12.4.5 acceptance for three MIDI variations and two trimmed audio placements, including fractional positions, replay, rejected inputs and unchanged sources. All five copies passed verification after saving, unloading and reopening the disposable Set. See the [1.2.0 Live acceptance report](docs/LIVE-ACCEPTANCE-1.2.0-2026-09-08.md) for evidence and limits. The tested 1.1.0 baseline is committed as `d50b243` on `codex/arrangement-workflows`.

## Reliability workflows

The 1.1.0 baseline added eight workflow tools and addressed recording, MIDI-edit and connection failures found in the [September review](docs/MCP-REVIEW-2026-09-03.md) and subsequent real Live testing.

- **Keep targeting the same track:** discover stable track handles, preview an edit, and resolve the handle again when Live executes it. Deleted tracks and changed Sets are rejected.
- **See what changed:** capture session metadata and compare track, mixer, routing, device and clip changes by handle.
- **Build an arrangement from MIDI clips:** preview placements, reject overlaps and changed sources, then apply a retained plan without duplicate copies on retry.
- **Inspect recorded files:** recording jobs return operation IDs and output manifests. Optional FFmpeg analysis measures duration, channels, sample rate, sample peak and RMS, with silence and possible-clipping flags.
- **Recover from uncertain commands:** requests have IDs, bounded deadlines and status lookup. Expired queued writes do not run later, and uncertain writes are not automatically replayed.
- **Read and edit MIDI more safely:** paginated structured note results expose the read scope; edit inputs are validated before mutation, with compensation when a note edit partially fails.
- **Diagnose the code actually loaded:** `get_build_info` compares build labels, protocol and source hashes. Copying a Remote Script does not reload it in Live.

Verified for the 1.1.0 baseline in Live 12.4.5: short bounce and stem captures, cancellation followed by a new job, stable track edits, MIDI edits, arrangement placement safeguards, and saved-Set persistence. See the [baseline Live acceptance report](docs/LIVE-ACCEPTANCE-2026-09-08.md) for evidence and limits, and [1.1.0 release examples](docs/RELEASE-1.1.0.md) for those tool arguments.

## Existing capabilities

| Area | Tools cover |
| --- | --- |
| Session and transport | Session inspection, tempo, playback, scenes and cue points |
| Tracks and routing | Audio/MIDI/return tracks, names, mixer state, sends and routing |
| Clips and MIDI | Session and arrangement clips, notes, loop settings and supported transformations |
| Devices | Browser loading, device parameters, racks, chains, drum pads and supported instrument controls |
| Automation | Clip envelopes and real-time arrangement automation recording, subject to Live API constraints |
| Recording | Input recording, scene capture, real-time mix bounce and stem capture |
| Diagnostics | Session overview, meters, performance information, build agreement and command status |

The existing `freeze_track` tool performs a real-time bounce and can switch off the original track after output verification. It does not invoke Live's native Freeze/Flatten or promise CPU savings. Features depend on the Live version, edition, device and available API; consult [Live API facts](docs/LIVE-API-FACTS.md) and [Live 11 notes](LIVE_11_NOTES.md).

## Quick start

Use Python 3.10 or newer, Ableton Live 11 or 12, and an assistant that supports local stdio MCP servers. The current release work targets Live 12; Live 11 behavior has not been revalidated for this build.

```bash
git clone https://github.com/TimCakir/ableton-mcp-extended.git
cd ableton-mcp-extended
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

On Windows, use `py -3 -m venv .venv` and `.venv\Scripts\python.exe` in place of the macOS commands. Existing checkouts should install from their current repository directory. If you use uv, `uv sync --locked` installs the versions recorded in `uv.lock` instead of resolving fresh dependencies.

1. Copy all `.py` files from `AbletonMCP_Remote_Script/` into `<your Ableton User Library>/Remote Scripts/AbletonMCP/`. On macOS, `./deploy.sh` handles the default User Library location and verifies the copied files.
2. In Live's MIDI preferences, select **AbletonMCP** as a Control Surface with **Input** and **Output** set to **None**. Restart Live after updating its script.
3. Add the entry below to your assistant's existing MCP configuration, preserving its other servers and settings. Replace the Python path with your virtual environment's absolute path.

```json
{
  "mcpServers": {
    "AbletonMCP": {
      "command": "/absolute/path/to/ableton-mcp-extended/.venv/bin/python",
      "args": ["-m", "MCP_Server.server"]
    }
  }
}
```

Restart the MCP server in the assistant. Ask it to run `get_build_info`, then `get_session_overview`, and confirm the expected Set. Use a disposable copy of a Set for the first edit or recording test.

See [INSTALLATION.md](INSTALLATION.md) for platform paths, upgrade steps and troubleshooting.

## Try the new workflows

- “List stable edit targets. Preview changing the Bass track's volume to 0.7.”
- “Take a session snapshot before this edit, then compare it with a new snapshot.”
- “Preview placing this session MIDI clip at beats 0 and 4, then apply that plan.”
- “Preview a breakdown copy without pitch 36, and an alternate phrase transposed up seven semitones.”
- “Preview thinning this clip by keeping every second note. Show the resulting notes before applying.”
- “Preview placing seconds 2–5 of this unwarped Session audio clip at beat 128.25.”
- “Check the latest recording operation and measure every file in its output manifest.”
- “Analyze this local WAV file for duration, sample peak, RMS and possible silence.”

`edit_track` previews by default; applying requires `preview=false`. Use the actual `session_id` and `track_handle` returned by `get_edit_targets`. [The release examples](docs/RELEASE-1.1.0.md#stable-track-edit-preview-and-apply) show the exact arguments.

For `build_arrangement`, use `action="preview"` with placements, inspect the result, then use `action="apply"` with its `plan_id` and `session_id`. MIDI thinning acts on individual notes rather than chord groups; transposition rejects pitches outside 0–127. Audio uses the units appropriate to the source's existing warp state. See [MIDI and audio placement examples](docs/RELEASE-1.2.0.md).

Plans containing unwarped audio return `status="applying"` while Live settles the clip bounds. Poll `apply` with the same plan ID until `applied`, `error` or `partial`. Polling never creates another copy; new arrangement plans are blocked while this verification is pending.

## Optional components

- **Audio analysis:** install `ffmpeg` and `ffprobe` on the MCP process's `PATH`. Analysis reads the first 60 seconds by default, with a maximum of 600 seconds, and reports whether that covers the full file. It measures sample peak and RMS, not true peak or LUFS.
- **UDP companion:** `Ableton-MCP_hybrid-server/AbletonMCP_UDP/` is an experimental UDP-only companion on port 9878 for parameter controllers. The primary Remote Script owns TCP port 9877. Unsupported companion commands fail explicitly.
- **ElevenLabs:** the repository includes a separate MCP server for supported voice/audio generation workflows. It requires its own API key and configuration. Generated audio can then be imported using the Ableton tools.
- **XY controller:** see [the experimental controller example](experimental_tools/xy_mouse_controller/README.md).

## Development and documentation

With uv, use the recorded dependency versions:

```bash
uv sync --locked --extra dev
uv run --locked --extra dev pytest -q
```

Or use pip in the existing virtual environment:

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

The default test command excludes tests marked `integration`. Automated tests simulate Live objects and scheduled ticks; they cannot establish that a recording is audible or that an edit survives saving a real Set.

- [Release 1.2.0](docs/RELEASE-1.2.0.md)
- [1.2.0 Live acceptance](docs/LIVE-ACCEPTANCE-1.2.0-2026-09-08.md)
- [Release 1.1.0 baseline](docs/RELEASE-1.1.0.md)
- [Installation and upgrades](INSTALLATION.md)
- [Code and MCP review](docs/MCP-REVIEW-2026-09-03.md)
- [Live API observations](docs/LIVE-API-FACTS.md)
- [Tooling backlog](docs/TOOLING-BACKLOG.md)

## License and credits

Licensed under the [MIT License](LICENSE). This fork builds on [uisato's Ableton MCP Extended](https://github.com/uisato/ableton-mcp-extended), inspired by [ahujasid's ableton-mcp](https://github.com/ahujasid/ableton-mcp).

Built with the [Model Context Protocol](https://github.com/modelcontextprotocol), [Ableton Live](https://www.ableton.com) and the optional [ElevenLabs API](https://elevenlabs.io).

Original demonstration: [YouTube](https://www.youtube.com/watch?v=7ZKPIrJuuKk). More from uisato: [YouTube](https://www.youtube.com/@uisato_) · [Instagram](https://www.instagram.com/uisato_) · [Patreon](https://www.patreon.com/c/uisato) · [Website](https://www.uisato.art/).
