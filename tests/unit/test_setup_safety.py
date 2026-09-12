"""Setup must preserve existing settings and report installation honestly."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import types
from unittest.mock import MagicMock

import pytest

from elevenlabs_mcp import __main__ as setup


ROOT = Path(__file__).resolve().parents[2]


def test_bundled_elevenlabs_server_starts_over_stdio_without_api_calls():
    """Catch missing package dependencies and stdout contamination on startup."""
    script = '''
import asyncio, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def check():
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "elevenlabs_mcp.server"],
        env={**os.environ, "ELEVENLABS_API_KEY": "offline-contract-test"})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "ElevenLabs"
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert "text_to_speech" in names
asyncio.run(check())
'''
    completed = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                               text=True, capture_output=True, timeout=20)
    assert completed.returncode == 0, completed.stdout + completed.stderr


GENERATED = {"mcpServers": {"ElevenLabs": {
    "command": "/test/python", "args": ["/test/server.py"],
    "env": {"ELEVENLABS_API_KEY": "test-key"},
}}}


def test_config_merge_preserves_servers_preferences_and_existing_server_options(tmp_path):
    target = tmp_path / "claude_desktop_config.json"
    existing = {
        "preferences": {"theme": "dark"},
        "mcpServers": {
            "Other": {"command": "other-server", "args": ["--existing"]},
            "ElevenLabs": {"disabled": False, "timeout": 45,
                           "env": {"CUSTOM_SETTING": "keep", "ELEVENLABS_API_KEY": "old-key"}},
        },
    }
    original_bytes = json.dumps(existing).encode()
    target.write_bytes(original_bytes)

    result = setup.write_claude_config(target, GENERATED)

    current = json.loads(target.read_text())
    assert current["preferences"] == existing["preferences"]
    assert current["mcpServers"]["Other"] == existing["mcpServers"]["Other"]
    server = current["mcpServers"]["ElevenLabs"]
    assert server["env"] == {"CUSTOM_SETTING": "keep", "ELEVENLABS_API_KEY": "test-key"}
    assert server["timeout"] == 45
    assert server["command"] == "/test/python"
    assert result["changed"] is True
    assert result["backup"].read_bytes() == original_bytes
    assert target.stat().st_mode & 0o777 == 0o600


def test_config_write_creates_a_new_parent_and_is_idempotent(tmp_path):
    target = tmp_path / "new" / "claude_desktop_config.json"
    first = setup.write_claude_config(target, GENERATED)
    original_bytes = target.read_bytes()
    second = setup.write_claude_config(target, GENERATED)
    assert first["changed"] is True
    assert first["backup"] is None
    assert second["changed"] is False
    assert target.read_bytes() == original_bytes
    assert list(target.parent.iterdir()) == [target]


@pytest.mark.parametrize("contents", [
    "{invalid", "[]", '{"mcpServers": []}',
    '{"mcpServers": {"ElevenLabs": []}}',
    '{"mcpServers": {"ElevenLabs": {"env": []}}}',
])
def test_invalid_existing_config_is_never_overwritten(tmp_path, contents):
    target = tmp_path / "claude_desktop_config.json"
    target.write_text(contents)
    with pytest.raises(ValueError):
        setup.write_claude_config(target, GENERATED)
    assert target.read_text() == contents
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_replace_failure_preserves_config_and_backup(tmp_path, monkeypatch):
    target = tmp_path / "claude_desktop_config.json"
    original_bytes = b'{"preferences":{"theme":"dark"}}\n'
    target.write_bytes(original_bytes)
    monkeypatch.setattr(setup.os, "replace", MagicMock(side_effect=OSError("replace failed")))

    with pytest.raises(OSError, match="replace failed"):
        setup.write_claude_config(target, GENERATED)

    assert target.read_bytes() == original_bytes
    backups = list(tmp_path.glob("*.backup.*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original_bytes
    assert not list(tmp_path.glob("*.tmp"))


def test_concurrent_edit_is_preserved_during_config_write(tmp_path, monkeypatch):
    target = tmp_path / "claude_desktop_config.json"
    target.write_text("{}")
    edited = '{"preferences":{"theme":"light"}}'
    fsync = setup.os.fsync
    calls = []

    def fsync_and_edit(fd):
        fsync(fd)
        calls.append(fd)
        if len(calls) == 2:  # Backup is complete, then the new temp file.
            target.write_text(edited)

    monkeypatch.setattr(setup.os, "fsync", fsync_and_edit)
    with pytest.raises(RuntimeError, match="changed during setup"):
        setup.write_claude_config(target, GENERATED)
    assert target.read_text() == edited
    assert not list(tmp_path.glob("*.tmp"))


def test_deploy_fixture_copies_all_modules_without_claiming_loaded_state(tmp_path):
    # This executes against a disposable fake repository and explicit target;
    # neither the real Ableton installation nor the user's library is touched.
    fake_repo = tmp_path / "repo"
    source = fake_repo / "AbletonMCP_Remote_Script"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text('BUILD_ID = "fixture.1"\n')
    (source / "remote_helpers.py").write_text("VALUE = 1\n")
    script = fake_repo / "deploy.sh"
    script.write_text((ROOT / "deploy.sh").read_text())
    destination = tmp_path / "installed"
    env = dict(os.environ, ABLETON_MCP_REMOTE_SCRIPT_DIR=str(destination))

    first = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    second = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)

    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert (destination / "__init__.py").read_bytes() == (source / "__init__.py").read_bytes()
    assert (destination / "remote_helpers.py").read_bytes() == (source / "remote_helpers.py").read_bytes()
    assert "already match" in second.stdout
    assert "versions were not checked" in second.stdout
    assert "Nothing to restart" not in second.stdout
    assert not (fake_repo / ".deploy-server-hash").exists()


@pytest.fixture
def companion(monkeypatch):
    class StubControlSurface:
        def __init__(self, _instance):
            pass

        def song(self):
            return "test-song"

        def log_message(self, _message):
            pass

        def show_message(self, _message):
            pass

    control_surface = types.ModuleType("_Framework.ControlSurface")
    control_surface.ControlSurface = StubControlSurface
    monkeypatch.setitem(sys.modules, "_Framework", types.ModuleType("_Framework"))
    monkeypatch.setitem(sys.modules, "_Framework.ControlSurface", control_surface)
    path = ROOT / "Ableton-MCP_hybrid-server" / "AbletonMCP_UDP" / "__init__.py"
    spec = importlib.util.spec_from_file_location("udp_companion_safety_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_companion_startup_does_not_claim_the_primary_tcp_port(companion, monkeypatch):
    tcp = MagicMock(side_effect=AssertionError("TCP must not start"))
    udp = MagicMock()
    monkeypatch.setattr(companion.AbletonMCP, "start_tcp_server", tcp)
    monkeypatch.setattr(companion.AbletonMCP, "start_udp_server", udp)
    companion.AbletonMCP(None)
    tcp.assert_not_called()
    udp.assert_called_once()


def test_companion_explicit_tcp_start_fails_without_creating_socket(companion, monkeypatch):
    socket = MagicMock(side_effect=AssertionError("socket must not open"))
    monkeypatch.setattr(companion.socket, "socket", socket)
    instance = companion.AbletonMCP.__new__(companion.AbletonMCP)
    with pytest.raises(NotImplementedError, match="UDP-only"):
        instance.start_tcp_server()
    socket.assert_not_called()


@pytest.mark.parametrize("method,args", [
    ("_create_midi_track", (0,)), ("_get_notes_from_clip", (0, 0)),
    ("get_browser_tree", ()), ("_get_scenes_info", ()),
])
def test_companion_unimplemented_commands_raise_instead_of_report_success(companion, method, args):
    instance = companion.AbletonMCP.__new__(companion.AbletonMCP)
    with pytest.raises(NotImplementedError, match="primary MCP script"):
        getattr(instance, method)(*args)


def test_companion_rejects_unknown_udp_command_before_scheduling(companion):
    instance = companion.AbletonMCP.__new__(companion.AbletonMCP)
    instance.schedule_message = MagicMock()
    with pytest.raises(NotImplementedError, match="Unsupported UDP command"):
        instance._process_udp_command({"type": "create_track"})
    instance.schedule_message.assert_not_called()


@pytest.mark.parametrize("change", ["disconnect", "new_set"])
def test_companion_scheduled_parameter_write_expires_after_lifecycle_change(companion, change):
    instance = companion.AbletonMCP.__new__(companion.AbletonMCP)
    instance.running = True
    song = [object()]
    instance.song = lambda: song[0]
    scheduled = []
    instance.schedule_message = lambda _ticks, task: scheduled.append(task)
    instance._set_device_parameter = MagicMock()
    instance._process_udp_command({"type": "set_device_parameter", "params": {"value": 0.5}})
    if change == "disconnect":
        instance.running = False
    else:
        song[0] = object()
    scheduled[0]()
    instance._set_device_parameter.assert_not_called()
