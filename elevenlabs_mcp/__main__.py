import os
import json
from pathlib import Path
import sys
from dotenv import load_dotenv
import argparse
import tempfile

load_dotenv()


def get_claude_config_path() -> Path | None:
    """Get the Claude config directory based on platform."""
    if sys.platform == "win32":
        path = Path(Path.home(), "AppData", "Roaming", "Claude")
    elif sys.platform == "darwin":
        path = Path(Path.home(), "Library", "Application Support", "Claude")
    elif sys.platform.startswith("linux"):
        path = Path(
            os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"), "Claude"
        )
    else:
        return None

    if path.exists():
        return path
    return None


def get_python_path():
    return sys.executable


def generate_config(api_key: str | None = None):
    module_dir = Path(__file__).resolve().parent
    server_path = module_dir / "server.py"
    python_path = get_python_path()

    final_api_key = api_key or os.environ.get("ELEVENLABS_API_KEY")
    if not final_api_key:
        print("Error: ElevenLabs API key is required.")
        print("Please either:")
        print("  1. Pass the API key using --api-key argument, or")
        print("  2. Set the ELEVENLABS_API_KEY environment variable, or")
        print("  3. Add ELEVENLABS_API_KEY to your .env file")
        sys.exit(1)

    config = {
        "mcpServers": {
            "ElevenLabs": {
                "command": python_path,
                "args": [
                    str(server_path),
                ],
                "env": {"ELEVENLABS_API_KEY": final_api_key},
            }
        }
    }

    return config


def write_claude_config(config_file: Path, generated: dict) -> dict:
    """Merge one server into an existing config, with a recoverable write."""
    config_file = Path(config_file).resolve()
    original_bytes = config_file.read_bytes() if config_file.exists() else None
    current = json.loads(original_bytes.decode("utf-8")) if original_bytes is not None else {}
    if not isinstance(current, dict):
        raise ValueError("Existing Claude configuration must be a JSON object")
    servers = current.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("Existing mcpServers must be a JSON object")
    merged = dict(current)
    merged_servers = dict(servers)
    for name, entry in generated.get("mcpServers", {}).items():
        existing = servers.get(name, {})
        if not isinstance(existing, dict):
            raise ValueError("Existing server configuration must be a JSON object: " + name)
        merged_entry = dict(existing)
        merged_entry.update(entry)
        if "env" in entry:
            previous_env = existing.get("env", {})
            if not isinstance(previous_env, dict):
                raise ValueError("Existing server env must be a JSON object: " + name)
            merged_entry["env"] = dict(previous_env, **entry["env"])
        merged_servers[name] = merged_entry
    merged["mcpServers"] = merged_servers
    if original_bytes is not None and merged == current:
        return {"path": config_file, "backup": None, "changed": False}

    content = (json.dumps(merged, indent=2) + "\n").encode("utf-8")
    config_file.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if original_bytes is not None:
        backup_fd, backup_name = tempfile.mkstemp(
            prefix=config_file.name + ".backup.", dir=str(config_file.parent))
        backup_path = Path(backup_name)
        try:
            with os.fdopen(backup_fd, "wb") as backup:
                backup.write(original_bytes)
                backup.flush()
                os.fsync(backup.fileno())
        except Exception:
            backup_path.unlink(missing_ok=True)
            raise
    temp_fd, temp_name = tempfile.mkstemp(
        prefix="." + config_file.name + ".", suffix=".tmp", dir=str(config_file.parent))
    temporary_path = Path(temp_name)
    try:
        with os.fdopen(temp_fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Preserve edits made by another process while this file was prepared.
        latest = config_file.read_bytes() if config_file.exists() else None
        if latest != original_bytes:
            raise RuntimeError("Claude configuration changed during setup; no changes were applied")
        os.replace(temporary_path, config_file)
    finally:
        temporary_path.unlink(missing_ok=True)
    return {"path": config_file, "backup": backup_path, "changed": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--print",
        action="store_true",
        help="Print config to screen instead of writing to file",
    )
    parser.add_argument(
        "--api-key",
        help="ElevenLabs API key (alternatively, set ELEVENLABS_API_KEY environment variable)",
    )
    parser.add_argument(
        "--config-path",
        type=Path,
        help="Custom Claude config directory or claude_desktop_config.json file",
    )
    args = parser.parse_args()

    config = generate_config(args.api_key)

    if args.print:
        print(json.dumps(config, indent=2))
    else:
        claude_path = args.config_path if args.config_path else get_claude_config_path()
        if claude_path is None:
            print(
                "Could not find Claude config path automatically. Use --config-path with the config directory or JSON file."
            )
            sys.exit(1)

        config_file = (claude_path if claude_path.suffix == ".json"
                       else claude_path / "claude_desktop_config.json")
        try:
            result = write_claude_config(config_file, config)
        except (OSError, ValueError, RuntimeError) as exc:
            print("Could not update Claude configuration:", exc, file=sys.stderr)
            sys.exit(1)
        print("Updated config:" if result["changed"] else "Config already current:", result["path"])
        if result["backup"] is not None:
            print("Previous config saved to", result["backup"])
