# Installation and upgrades

The assistant runs the Python MCP server; Ableton Live loads a separate Remote Script. Both components must be updated and restarted to use a new build.

## Requirements

- Python 3.10 or newer and Git.
- Ableton Live 11 or 12. Individual features depend on the Live version, edition and exposed API; see [Live 11 notes](LIVE_11_NOTES.md).
- An assistant that can launch a local stdio MCP server.
- Optional: `ffmpeg` and `ffprobe` for file-level audio measurements.

The current implementation keeps the MCP Python SDK on `>=1.28.1,<2`. Install the project's declared dependencies rather than upgrading the SDK to version 2 independently.

## 1. Install the Python package

Keep this checkout in a normal local code directory, outside cloud-synced Documents folders.

### macOS

```bash
git clone https://github.com/TimCakir/ableton-mcp-extended.git
cd ableton-mcp-extended
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -c "import MCP_Server.server; print('MCP server imports successfully')"
```

### Windows PowerShell

```powershell
git clone https://github.com/TimCakir/ableton-mcp-extended.git
cd ableton-mcp-extended
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -c "import MCP_Server.server; print('MCP server imports successfully')"
```

For an existing checkout, run the install command from that repository directory. The import check verifies the Python package, not the connection to Live.

### Reproducible setup with uv

If you use uv, run `uv sync --locked` from the repository instead of creating and installing the environment with pip. This creates `.venv` using the versions in `uv.lock`. For development, include the test extra:

```bash
uv sync --locked --extra dev
uv run --locked --extra dev pytest -q
```

Pip remains supported, but it resolves versions within the declared dependency ranges rather than consuming `uv.lock`.

## 2. Install the Remote Script

Find the actual **User Library** location in Live's library preferences. If it has been moved, use that location. The usual defaults are:

| Platform | Remote Script destination |
| --- | --- |
| macOS | `~/Music/Ableton/User Library/Remote Scripts/AbletonMCP/` |
| Windows | `%USERPROFILE%\Documents\Ableton\User Library\Remote Scripts\AbletonMCP\` |

Create the destination directory and copy every `.py` file from `AbletonMCP_Remote_Script/` into it. The resulting path must include `Remote Scripts/AbletonMCP/__init__.py`.

On macOS, the helper performs the copy and compares the installed files with the source:

```bash
./deploy.sh
```

For a custom User Library, pass the full **AbletonMCP destination directory**:

```bash
ABLETON_MCP_REMOTE_SCRIPT_DIR="/absolute/path/to/User Library/Remote Scripts/AbletonMCP" ./deploy.sh
```

The default macOS helper also updates older `User Remote Scripts` locations when those folders already exist. The current User Library path is the primary installation destination. The helper does not restart Live or verify which build Live has loaded.

## 3. Enable the script in Live

1. Save the Set you are working on, then restart Live to load the installed code.
2. Open **Settings/Preferences → Link, Tempo & MIDI**.
3. Select **AbletonMCP** in an available **Control Surface** slot.
4. Set its **Input** and **Output** to **None**.

The primary script listens on local TCP port **9877**. A copied file and an enabled Control Surface are setup steps; the build check below establishes which code is answering.

## 4. Configure the assistant

Use the absolute path to the Python executable inside the virtual environment. Launch the installed package as a module, so its imports work independently of the assistant's working directory.

For hosts that use an `mcpServers` JSON object, add or update only its `AbletonMCP` entry. Preserve all existing servers and unrelated settings; the following is an example for a fresh configuration:

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

For Windows, use a Python path such as `C:\\Developer\\ableton-mcp-extended\\.venv\\Scripts\\python.exe` in JSON. The `args` stay the same. Hosts with another configuration format need the same executable and argument list.

Restart the MCP server in the assistant after saving the configuration. To print the interpreter path from the repository:

```bash
.venv/bin/python -c "import sys; print(sys.executable)"
```

The default connection is `localhost:9877`. `ABLETON_HOST` and `ABLETON_PORT` override the Python client's connection target; setting them does not reconfigure the Remote Script listener.

## 5. Verify the installed build

Start with read-only tool calls:

1. Run `get_build_info`. For a source checkout with both components loaded correctly, `status` should be `match`, with build `2026-09-08.5` and protocol `2.1`.
2. If the result is `mismatch`, follow its `recovery` messages. Redeploy and restart Live for stale Remote Script code; restart the assistant's MCP server for stale Python server code.
3. If it is `unknown`, a build value or source hash is unavailable. This is not confirmation of agreement. If `unreachable`, check that Live is running and the Control Surface is loaded.
4. Run `get_session_overview` and confirm the Set name and tracks.
5. Run `get_edit_targets`, then preview an `edit_track` call using a returned identity. The default preview does not change Live.

For an actual edit or recording test, use a disposable copy of a Set. Check the result in Live and, when testing persistence, save and reopen that copy. Local automated tests do not verify a real recording or saved-Set behavior.

## Upgrade checklist

1. Update the intended checkout and install its dependencies into the same virtual environment used by the assistant.
2. Copy the Remote Script with `./deploy.sh` or the manual instructions above.
3. Save your current work and restart Live; restart the MCP server in the assistant.
4. Repeat `get_build_info` and the read-only Set checks.
5. Update integrations that parse old text results: note operations, recording operations, batch results and build diagnostics now expose structured result objects. See [release compatibility notes](docs/RELEASE-1.1.0.md#response-and-compatibility-changes).

## Optional audio analysis

Install FFmpeg through your normal package manager and ensure both `ffmpeg` and `ffprobe` are available on the MCP process's `PATH`. On macOS with Homebrew:

```bash
brew install ffmpeg
ffmpeg -version
ffprobe -version
```

If a desktop host has a restricted `PATH`, add an `env.PATH` entry to this server's configuration containing the directory where these executables are installed, along with the usual system paths. Restart that MCP server after changing its environment.

`analyze_audio_file` reads the first 60 seconds by default and accepts `analysis_seconds` greater than zero and up to 600. It reports total duration and whether the measured segment covers the entire file. Sample peak and RMS use FFmpeg's 16-bit `volumedetect` measurement at 0.1 dB precision; these are not LUFS or true-peak measurements.

`verify_export_outputs` reads the latest recording manifest and measures available files. It skips analysis while a recording is active. Missing files, partial captures and file-analysis errors remain visible in the result.

## Optional UDP companion

Copy `Ableton-MCP_hybrid-server/AbletonMCP_UDP/__init__.py` into a separate `Remote Scripts/AbletonMCP_UDP/` directory and enable that Control Surface with Input/Output set to None.

This version starts only UDP port **9878**, leaving TCP port **9877** to the primary script. Older companion versions started a competing TCP listener, so update the companion before enabling both. It is an experimental parameter-controller interface; unsupported commands raise errors. See the [XY controller example](experimental_tools/xy_mouse_controller/README.md) for its own requirements.

## Optional ElevenLabs server

The separate ElevenLabs server requires `ELEVENLABS_API_KEY`. With that variable set in your environment, the setup helper can add its entry to an existing Claude configuration:

```bash
.venv/bin/python -m elevenlabs_mcp --config-path "/absolute/path/to/claude_desktop_config.json"
```

The helper merges the ElevenLabs entry, preserves other servers and settings, creates a backup when the existing file changes, and replaces the configuration atomically. It refuses malformed JSON or concurrent changes. This configures ElevenLabs only; configure Ableton separately as above. The configuration and backup may contain the API key, so keep them private.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `AbletonMCP` is missing from Control Surfaces | Confirm the actual User Library and `Remote Scripts/AbletonMCP/__init__.py` layout, then restart Live and inspect its log for import errors. |
| Python module cannot be imported | Run the import check with the exact executable in the host configuration; install the package into that environment. |
| `Connection refused` | Confirm Live is running and the primary Control Surface is enabled. A tool list in the assistant does not establish a Live connection. |
| Build `mismatch` | Follow `get_build_info.recovery`; both Live and the MCP process may need restarting after their files are updated. |
| A command timed out after it may have started | Use the returned request ID with `get_command_status`. `unknown` does not mean it was unapplied; inspect the Set before issuing another edit. |
| Audio analysis cannot find FFmpeg | Check `ffmpeg` and `ffprobe` on the MCP process's `PATH`, then restart that process. |

For Python-side diagnostics, launch `.venv/bin/python -m MCP_Server.server` from a terminal. It waits for MCP messages on stdin and logs to stderr; it is not an interactive command prompt. See [release examples](docs/RELEASE-1.1.0.md) for request reconciliation and recording verification.
