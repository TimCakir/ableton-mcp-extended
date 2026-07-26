import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP, Context
import socket
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List, Union

from MCP_Server.plugin_aliases import (
    get_alias_for_param,
    get_categories,
    resolve_alias,
)

# Configure logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AbletonMCPServer")

# Must match BUILD_ID in AbletonMCP_Remote_Script/__init__.py. Bump both
# together whenever a command is added, removed or changes signature.
#
# Three copies of this integration run at once and drift apart constantly:
# the repo, the script Live loaded at ITS startup, and this server process as
# the MCP host launched it. Live only re-reads a remote script on restart, and
# the host only re-reads this file when it restarts. Every "the Live API can't
# do that" that later proved false was traced to one of those copies being
# older than the others — the capability existed, the process answering the
# question just didn't have it. `get_build_info` makes that visible.
SERVER_BUILD_ID = "2026-07-26.11"

@dataclass
class AbletonConnection:
    host: str
    port: int
    sock: socket.socket = None
    
    def connect(self) -> bool:
        """Connect to the Ableton Remote Script socket server"""
        if self.sock:
            return True
            
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            logger.info(f"Connected to Ableton at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Ableton: {str(e)}")
            self.sock = None
            return False
    
    def disconnect(self):
        """Disconnect from the Ableton Remote Script"""
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Ableton: {str(e)}")
            finally:
                self.sock = None

    def receive_full_response(self, sock, buffer_size=8192):
        """Receive the complete response, potentially in multiple chunks"""
        chunks = []
        sock.settimeout(15.0)  # Increased timeout for operations that might take longer
        
        try:
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        if not chunks:
                            raise Exception("Connection closed before receiving any data")
                        break
                    
                    chunks.append(chunk)
                    
                    # Check if we've received a complete JSON object
                    try:
                        data = b''.join(chunks)
                        json.loads(data.decode('utf-8'))
                        logger.info(f"Received complete response ({len(data)} bytes)")
                        return data
                    except json.JSONDecodeError:
                        # Incomplete JSON, continue receiving
                        continue
                except socket.timeout:
                    logger.warning("Socket timeout during chunked receive")
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Socket connection error during receive: {str(e)}")
                    raise
        except Exception as e:
            logger.error(f"Error during receive: {str(e)}")
            raise
            
        # If we get here, we either timed out or broke out of the loop
        if chunks:
            data = b''.join(chunks)
            logger.info(f"Returning data after receive completion ({len(data)} bytes)")
            try:
                json.loads(data.decode('utf-8'))
                return data
            except json.JSONDecodeError:
                raise Exception("Incomplete JSON response received")
        else:
            raise Exception("No data received")

    def send_command(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a command to Ableton and return the response"""
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Ableton")
        
        command = {
            "type": command_type,
            "params": params or {}
        }
        
        # Check if this is a state-modifying command
        is_modifying_command = command_type in [
            "create_midi_track", "create_audio_track", "set_track_name",
            "create_clip", "add_notes_to_clip", "set_clip_name",
            "set_tempo", "fire_clip", "stop_clip", "set_device_parameter",
            "start_playback", "stop_playback", "load_instrument_or_effect",
            "set_song_time", "set_arrangement_loop", "jump_to_cue",
            "create_cue_point", "delete_cue_point",
            "create_arrangement_clip", "create_arrangement_audio_clip",
            "duplicate_to_arrangement", "delete_arrangement_clip",
            "set_arrangement_clip_property",
            "set_view", "control_arrangement_view",
            "manage_clip_automation",
            "add_notes_to_arrangement_clip",
            "modify_clip_notes", "remove_clip_notes", "add_notes_extended",
            "set_device_parameter", "set_device_enabled",
            "delete_device", "navigate_preset",
            "set_track_volume", "set_track_panning",
        ]
        
        try:
            logger.info(f"Sending command: {command_type} with params: {params}")
            
            # Send the command
            self.sock.sendall(json.dumps(command).encode('utf-8'))
            logger.info(f"Command sent, waiting for response...")
            
            # For state-modifying commands, add a small delay to give Ableton time to process
            if is_modifying_command:
                import time
                time.sleep(0.1)  # 100ms delay
            
            # Set timeout based on command type
            timeout = 15.0 if is_modifying_command else 10.0
            self.sock.settimeout(timeout)
            
            # Receive the response
            response_data = self.receive_full_response(self.sock)
            logger.info(f"Received {len(response_data)} bytes of data")
            
            # Parse the response
            response = json.loads(response_data.decode('utf-8'))
            logger.info(f"Response parsed, status: {response.get('status', 'unknown')}")
            
            if response.get("status") == "error":
                logger.error(f"Ableton error: {response.get('message')}")
                raise Exception(response.get("message", "Unknown error from Ableton"))
            
            # For state-modifying commands, add another small delay after receiving response
            if is_modifying_command:
                import time
                time.sleep(0.1)  # 100ms delay
            
            return response.get("result", {})
        except socket.timeout:
            logger.error("Socket timeout while waiting for response from Ableton")
            self.sock = None
            raise Exception("Timeout waiting for Ableton response")
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            logger.error(f"Socket connection error: {str(e)}")
            self.sock = None
            raise Exception(f"Connection to Ableton lost: {str(e)}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from Ableton: {str(e)}")
            if 'response_data' in locals() and response_data:
                logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
            self.sock = None
            raise Exception(f"Invalid response from Ableton: {str(e)}")
        except Exception as e:
            logger.error(f"Error communicating with Ableton: {str(e)}")
            self.sock = None
            raise Exception(f"Communication error with Ableton: {str(e)}")

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    try:
        logger.info("AbletonMCP server starting up")
        
        try:
            ableton = get_ableton_connection()
            logger.info("Successfully connected to Ableton on startup")
        except Exception as e:
            logger.warning(f"Could not connect to Ableton on startup: {str(e)}")
            logger.warning("Make sure the Ableton Remote Script is running")
        
        yield {}
    finally:
        global _ableton_connection
        if _ableton_connection:
            logger.info("Disconnecting from Ableton on shutdown")
            _ableton_connection.disconnect()
            _ableton_connection = None
        _invalidate_external_plugin_cache()
        logger.info("AbletonMCP server shut down")

# Create the MCP server with lifespan support
mcp = FastMCP(
    "AbletonMCP",
    instructions="Ableton Live integration through the Model Context Protocol",
    lifespan=server_lifespan
)

# ── Index conversion helpers ─────────────────────────────────────
#
# Convention: every MCP tool exposes **1-based** indices to callers.
# The Remote Script expects **0-based** indices.  These helpers
# enforce the rule in one place so individual tools stay simple.


def _to_zero_based(index: int, field_name: str = "index") -> int:
    """Convert a required 1-based MCP index to 0-based for the Remote Script.

    Raises ValueError when the caller passes 0 or a negative value.
    """
    if index < 1:
        raise ValueError(
            f"{field_name} must be >= 1 (1-based), got {index}"
        )
    return index - 1


def _optional_to_zero_based(index: int, field_name: str = "index") -> int | None:
    """Convert an optional 1-based index to 0-based.

    Returns None when *index* is 0 (meaning "not specified").
    Raises ValueError for negative values.
    """
    if index < 0:
        raise ValueError(
            f"{field_name} must be >= 0 (0 = unset, 1+ = 1-based), got {index}"
        )
    if index == 0:
        return None
    return index - 1


# Parameter normalization utilities


def normalize_param(value: float, min_val: float, max_val: float) -> float:
    """Normalize a raw parameter value to 0.0-1.0 range.

    Parameters:
    - value: raw parameter value
    - min_val: parameter minimum
    - max_val: parameter maximum

    Returns normalized value clamped to 0.0-1.0.
    """
    if max_val == min_val:
        return 0.0
    normalized = (value - min_val) / (max_val - min_val)
    return max(0.0, min(1.0, normalized))


def denormalize_param(normalized: float, min_val: float, max_val: float) -> float:
    """Convert a normalized 0.0-1.0 value to raw parameter range.

    Parameters:
    - normalized: value in 0.0-1.0 range
    - min_val: parameter minimum
    - max_val: parameter maximum

    Returns raw value. Input is clamped to 0.0-1.0 before conversion.
    """
    clamped = max(0.0, min(1.0, normalized))
    return min_val + clamped * (max_val - min_val)


# Bar/beat conversion utilities

def bar_to_beat(bar: int, numerator: int = 4, denominator: int = 4) -> float:
    """Convert a 1-based bar number to a beat position.

    Parameters:
    - bar: 1-based bar number (bar 1 = beat 0)
    - numerator: time signature numerator (e.g., 4 in 4/4)
    - denominator: time signature denominator (e.g., 4 in 4/4)
    """
    return (bar - 1) * numerator * (4 / denominator)


def beat_to_bar(beat: float, numerator: int = 4, denominator: int = 4) -> int:
    """Convert a beat position to a 1-based bar number.

    Parameters:
    - beat: beat position (0-based)
    - numerator: time signature numerator
    - denominator: time signature denominator
    """
    beats_per_bar = numerator * (4 / denominator)
    return int(beat / beats_per_bar) + 1


# Global connection for resources
_ableton_connection = None
_EXTERNAL_PLUGIN_CACHE_TTL_SECONDS = 120.0
_external_plugin_cache_lock = threading.Lock()
_external_plugin_cache: Dict[str, Any] = {
    "plugins": None,
    "built_at": 0.0,
}


def _invalidate_external_plugin_cache() -> None:
    """Invalidate cached external plugin discovery results."""
    with _external_plugin_cache_lock:
        _external_plugin_cache["plugins"] = None
        _external_plugin_cache["built_at"] = 0.0

def get_ableton_connection():
    """Get or create a persistent Ableton connection"""
    global _ableton_connection
    
    if _ableton_connection is not None:
        try:
            # Test the connection with a simple ping
            # We'll try to send an empty message, which should fail if the connection is dead
            # but won't affect Ableton if it's alive
            _ableton_connection.sock.settimeout(1.0)
            _ableton_connection.sock.sendall(b'')
            return _ableton_connection
        except Exception as e:
            logger.warning(f"Existing connection is no longer valid: {str(e)}")
            try:
                _ableton_connection.disconnect()
            except:
                pass
            _ableton_connection = None
            _invalidate_external_plugin_cache()
    
    # Connection doesn't exist or is invalid, create a new one
    if _ableton_connection is None:
        # Try to connect up to 3 times with a short delay between attempts
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Connecting to Ableton (attempt {attempt}/{max_attempts})...")
                _ableton_connection = AbletonConnection(
                    host=os.getenv("ABLETON_HOST", "localhost"),
                    port=int(os.getenv("ABLETON_PORT", "9877")),
                )
                if _ableton_connection.connect():
                    logger.info("Created new persistent connection to Ableton")
                    
                    # Validate connection with a simple command
                    try:
                        # Get session info as a test
                        _ableton_connection.send_command("get_session_info")
                        logger.info("Connection validated successfully")
                        return _ableton_connection
                    except Exception as e:
                        logger.error(f"Connection validation failed: {str(e)}")
                        _ableton_connection.disconnect()
                        _ableton_connection = None
                        _invalidate_external_plugin_cache()
                        # Continue to next attempt
                else:
                    _ableton_connection = None
            except Exception as e:
                logger.error(f"Connection attempt {attempt} failed: {str(e)}")
                if _ableton_connection:
                    _ableton_connection.disconnect()
                    _ableton_connection = None
                    _invalidate_external_plugin_cache()
            
            # Wait before trying again, but only if we have more attempts left
            if attempt < max_attempts:
                import time
                time.sleep(1.0)
        
        # If we get here, all connection attempts failed
        if _ableton_connection is None:
            logger.error("Failed to connect to Ableton after multiple attempts")
            raise Exception("Could not connect to Ableton. Make sure the Remote Script is running.")
    
    return _ableton_connection


# Core Tool endpoints

@mcp.tool()
def get_session_info(ctx: Context) -> str:
    """Get detailed information about the current Ableton session"""
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_session_info")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting session info from Ableton: {str(e)}")
        return f"Error getting session info: {str(e)}"

@mcp.tool()
def get_track_info(ctx: Context, track_index: int) -> str:
    """
    Get detailed information about a specific track in Ableton.

    Parameters:
    - track_index: Track number (1-based).
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        result = ableton.send_command("get_track_info", {"track_index": ti})
        if isinstance(result, dict):
            if isinstance(result.get("index"), int):
                result["index"] = result["index"] + 1
            for key in ("clip_slots", "devices"):
                items = result.get(key)
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict) and isinstance(item.get("index"), int):
                            item["index"] = item["index"] + 1
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting track info from Ableton: {str(e)}")
        return f"Error getting track info: {str(e)}"

@mcp.tool()
def create_midi_track(ctx: Context, index: int = -1) -> str:
    """
    Create a new MIDI track in the Ableton session.
    
    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_midi_track", {"index": index})
        return f"Created new MIDI track: {result.get('name', 'unknown')}"
    except Exception as e:
        logger.error(f"Error creating MIDI track: {str(e)}")
        return f"Error creating MIDI track: {str(e)}"


@mcp.tool()
def set_track_name(ctx: Context, track_index: int, name: str) -> str:
    """
    Set the name of a track.

    Parameters:
    - track_index: Track number (1-based).
    - name: The new name for the track.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        result = ableton.send_command("set_track_name", {"track_index": ti, "name": name})
        return f"Renamed track to: {result.get('name', name)}"
    except Exception as e:
        logger.error(f"Error setting track name: {str(e)}")
        return f"Error setting track name: {str(e)}"


@mcp.tool()
def get_track_volume(ctx: Context, track_index: int) -> str:
    """Get the current fader volume and panning for a track.

    Returns the raw normalized value, its min/max range, and the panning.
    Volume 0.85 = 0 dB unity gain. Use this before set_track_volume to
    understand the current state.

    Parameters:
    - track_index: Track number (1-based). Return tracks come after session tracks.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        result = ableton.send_command("get_track_volume", {"track_index": ti})
        vol = result.get("volume", 0)
        pan = result.get("panning", 0)
        name = result.get("track_name", "?")
        vol_min = result.get("volume_min", 0)
        vol_max = result.get("volume_max", 1)
        # Approximate dB: unity is at 0.85 normalized
        unity = 0.85
        if vol > 0:
            import math
            db_approx = 20 * math.log10(vol / unity) if vol > 0 else -float('inf')
            db_str = f"{db_approx:+.1f} dB" if vol > 0 else "-inf dB"
        else:
            db_str = "-inf dB"
        pan_str = "center" if abs(pan) < 0.01 else (f"{abs(pan):.2f} {'L' if pan < 0 else 'R'}")
        return (
            f"Track '{name}':\n"
            f"  Volume: {vol:.4f} (range {vol_min:.2f}–{vol_max:.2f}) ≈ {db_str}\n"
            f"  Panning: {pan:.4f} ({pan_str})\n"
            f"  Unity gain (0 dB) = 0.85"
        )
    except Exception as e:
        logger.error(f"Error getting track volume: {str(e)}")
        return f"Error getting track volume: {str(e)}"


@mcp.tool()
def get_session_overview(ctx: Context) -> str:
    """Get a compact map of the whole Set in one call.

    Returns tempo, signature, and every track (session tracks first, then
    return tracks) with its 1-based index, name, kind, devices, clips and
    mixer state — plus the scene list.

    Call this before any indexed operation. Track indices shift whenever a
    track is added, deleted or moved, so acting on remembered indices is the
    most common way to write to the wrong track. This is far cheaper than
    calling get_track_info per track.
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_session_overview")
        lines = [
            f"Tempo {result.get('tempo')} BPM | {result.get('signature')} | "
            f"{result.get('session_track_count')} tracks, "
            f"{result.get('return_track_count')} returns",
            "",
        ]
        for t in result.get("tracks", []):
            flags = []
            if t.get("mute"):
                flags.append("MUTED")
            if t.get("solo"):
                flags.append("SOLO")
            if t.get("arm"):
                flags.append("ARMED")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            devices = ", ".join(t.get("devices") or []) or "—"
            lines.append(
                f"{t['index'] + 1:>3}. {t['name']} ({t['kind']}){flag_str}"
            )
            lines.append(f"      devices: {devices}")
            clips = t.get("clips") or []
            if clips:
                clip_str = ", ".join(
                    f"slot {c['slot'] + 1}:'{c['name']}' ({c['length']}b)"
                    for c in clips
                )
                lines.append(f"      clips: {clip_str}")
        scenes = result.get("scenes", [])
        if scenes:
            lines.append("")
            lines.append(
                "Scenes: "
                + ", ".join(f"{s['index'] + 1}:'{s['name']}'" for s in scenes)
            )
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error getting session overview: {str(e)}")
        return f"Error getting session overview: {str(e)}"


@mcp.tool()
def call_lom(
    ctx: Context,
    path: str = "song",
    member: str = "",
    args: list | None = None,
    set_value: float | str | bool | None = None,
    set_from: str | None = None,
) -> str:
    """Read, set, or call anything in Live's object model. The escape hatch.

    Reaches any part of the Live API without needing a new purpose-built
    tool first. Undocumented C++ signatures surface as readable errors that
    state the expected argument types, which is how a signature gets
    discovered rather than guessed at.

    Parameters:
    - path: Dotted path from song. Numeric segments index collections, e.g.
      "tracks.3.devices.0", "return_tracks.0", "scenes.1",
      "tracks.0.mixer_device.sends.0". All indices here are 0-based, matching
      the Live API itself.
    - member: Property or method name. Omit to inspect the object at path.
    - args: Arguments if member is a method.
    - set_value: Value to assign if member is a writable property.
    - set_from: Name of a sibling collection to resolve set_value out of, by
      display name. Required for properties that must be assigned an object
      rather than a string — routing types, routing channels, grooves. Live's
      C++ layer rejects a plain string for those.

    Examples:
      path="tracks.0", member="name"                  → read a track name
      path="tracks.0.devices.0", member="parameters"  → list parameters
      path="song", member="tempo", set_value=124      → set tempo
      path="tracks.2.devices.1", member="input_routing_type",
        set_from="available_input_routing_types", set_value="DRUMS"
                                                      → set a sidechain source
    """
    try:
        ableton = get_ableton_connection()
        payload: dict = {"path": path, "member": member, "args": args or []}
        if set_from is not None:
            payload["set_from"] = set_from
        if set_value is not None:
            payload["set_value"] = set_value
            payload["has_set_value"] = True
        r = ableton.send_command("call_lom", payload)
        return json.dumps(r, indent=2)[:6000]
    except Exception as e:
        logger.error(f"Error in call_lom: {str(e)}")
        return f"Error in call_lom: {str(e)}"


@mcp.tool()
def get_clip_notes(
    ctx: Context,
    track_index: int,
    clip_index: int,
    from_time: float = 0.0,
    time_span: float | None = None,
    from_pitch: int = 0,
    pitch_span: int = 128,
    arrangement: bool = False,
    clip_name: str | None = None,
) -> str:
    """Read the notes in a MIDI clip — pitch, timing, velocity, probability.

    Essential before editing anything that already exists: a part recorded
    by a player can be analysed and transformed instead of overwritten.

    Parameters:
    - track_index / clip_index: 1-based.
    - from_time / time_span: Beat window. Defaults to the whole clip.
    - from_pitch / pitch_span: MIDI note window. Defaults to all notes.
    - arrangement: Read an ARRANGEMENT clip instead of a session clip.
      `clip_index` then indexes the track's arrangement clips in timeline
      order (1-based) — call `get_arrangement_info` to see that order.
      Note times stay clip-relative, NOT absolute timeline position.
    - clip_name: With arrangement=True, address the clip by name instead of
      index. Errors if the name is ambiguous on that track.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        ci = _to_zero_based(clip_index, "clip_index")
        payload: dict = {
            "track_index": ti, "clip_index": ci, "from_time": from_time,
            "from_pitch": from_pitch, "pitch_span": pitch_span,
            "arrangement": arrangement,
        }
        if time_span is not None:
            payload["time_span"] = time_span
        if clip_name is not None:
            payload["clip_name"] = clip_name
        r = ableton.send_command("get_clip_notes", payload)
        notes = r.get("notes") or []
        where = "arrangement" if r.get("arrangement") else "session"
        header = (
            f"'{r.get('clip_name')}' on '{r.get('track_name')}' ({where}) — "
            f"{r.get('note_count')} notes, {r.get('length')} beats, "
            f"pitch range {r.get('pitch_range')}"
        )
        lines = [header, ""]
        for n in notes[:200]:
            prob = n.get("probability")
            prob_s = f" p={prob}" if prob is not None and prob < 1 else ""
            lines.append(
                f"  {n.get('start_time')}: pitch {n.get('pitch')} "
                f"dur {n.get('duration')} vel {n.get('velocity')}{prob_s}"
            )
        if len(notes) > 200:
            lines.append(f"  … {len(notes) - 200} more")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error reading clip notes: {str(e)}")
        return f"Error reading clip notes: {str(e)}"


@mcp.tool()
def modify_clip_notes(
    ctx: Context,
    track_index: int,
    clip_index: int,
    transpose: int = 0,
    velocity_scale: float | None = None,
    velocity_set: float | None = None,
    humanize_ms: float | None = None,
    probability: float | None = None,
    from_time: float = 0.0,
    time_span: float | None = None,
    from_pitch: int = 0,
    pitch_span: int = 128,
    arrangement: bool = False,
    clip_name: str | None = None,
) -> str:
    """Transform notes already in a clip, in place, without rewriting it.

    Notes are read, changed and written back by id, so nothing outside the
    selected window is disturbed. Humanisation is deterministic, so calling
    it twice does not compound into sloppiness.

    This CANNOT delete notes — use `remove_clip_notes` for that.

    Parameters:
    - track_index / clip_index: 1-based.
    - transpose: Semitones, positive or negative.
    - velocity_scale: Multiply velocities, e.g. 0.8 to soften.
    - velocity_set: Set all velocities to one value (overrides scale).
    - humanize_ms: Timing spread in milliseconds.
    - probability: Set per-note probability, 0.0-1.0.
    - from_time / time_span / from_pitch / pitch_span: Restrict the window,
      e.g. pitch 42 only to affect just the hats in a drum clip.
    - arrangement / clip_name: Target an ARRANGEMENT clip. Each placed copy
      holds its own notes, so a change must be repeated per copy — editing
      the session clip does not propagate to clips already in the timeline.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        ci = _to_zero_based(clip_index, "clip_index")
        payload: dict = {
            "track_index": ti, "clip_index": ci, "transpose": transpose,
            "from_time": from_time, "from_pitch": from_pitch,
            "pitch_span": pitch_span, "arrangement": arrangement,
        }
        for key, val in (
            ("velocity_scale", velocity_scale), ("velocity_set", velocity_set),
            ("humanize_ms", humanize_ms), ("probability", probability),
            ("time_span", time_span), ("clip_name", clip_name),
        ):
            if val is not None:
                payload[key] = val
        r = ableton.send_command("modify_clip_notes", payload)
        return f"Modified {r.get('modified')} notes in '{r.get('clip_name')}'"
    except Exception as e:
        logger.error(f"Error modifying clip notes: {str(e)}")
        return f"Error modifying clip notes: {str(e)}"


@mcp.tool()
def remove_clip_notes(
    ctx: Context,
    track_index: int,
    clip_index: int,
    from_time: float = 0.0,
    time_span: float | None = None,
    from_pitch: int = 0,
    pitch_span: int = 128,
    arrangement: bool = False,
    clip_name: str | None = None,
) -> str:
    """Delete notes from a MIDI clip within a pitch and time window.

    The only way to take notes OUT of a part. `modify_clip_notes` transposes
    and rescales but never removes, and `add_notes_extended(replace=True)`
    clears the entire clip. This is the surgical option: lift one note out of
    a chord without rewriting the chord.

    Defaults to the WHOLE clip and ALL pitches — always narrow the window.
    To remove a single pitch, pass from_pitch=<n>, pitch_span=1.

    Returns what was removed, so the edit can be checked and reversed by hand.

    Parameters:
    - track_index / clip_index: 1-based.
    - from_time / time_span: Beat window. Defaults to the whole clip.
    - from_pitch / pitch_span: MIDI note window. Defaults to ALL notes.
    - arrangement / clip_name: Target an ARRANGEMENT clip instead of a session
      clip. Each placed copy is independent, so removing a note from a part
      that appears N times in the timeline takes N calls.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        ci = _to_zero_based(clip_index, "clip_index")
        payload: dict = {
            "track_index": ti, "clip_index": ci, "from_time": from_time,
            "from_pitch": from_pitch, "pitch_span": pitch_span,
            "arrangement": arrangement,
        }
        if time_span is not None:
            payload["time_span"] = time_span
        if clip_name is not None:
            payload["clip_name"] = clip_name
        r = ableton.send_command("remove_clip_notes", payload)
        where = "arrangement" if r.get("arrangement") else "session"
        removed = r.get("removed_notes") or []
        lines = [
            f"Removed {r.get('removed')} note(s) from '{r.get('clip_name')}' "
            f"on '{r.get('track_name')}' ({where}) — "
            f"{r.get('remaining')} remaining"
        ]
        for n in removed[:20]:
            lines.append(
                f"  {n.get('start_time')}: pitch {n.get('pitch')} "
                f"dur {n.get('duration')} vel {n.get('velocity')}"
            )
        if len(removed) > 20:
            lines.append(f"  … {len(removed) - 20} more")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error removing clip notes: {str(e)}")
        return f"Error removing clip notes: {str(e)}"


@mcp.tool()
def manage_clip_region(
    ctx: Context,
    track_index: int,
    clip_index: int,
    action: str = "info",
    region_start: float | None = None,
    region_end: float | None = None,
    destination_time: float | None = None,
    start_marker: float | None = None,
    end_marker: float | None = None,
) -> str:
    """Duplicate, crop or re-mark a clip region.

    duplicate_loop is how a 4-bar idea becomes 8 bars with its contents
    copied — the fastest way to grow material for an arrangement.

    Parameters:
    - track_index / clip_index: 1-based.
    - action: info, duplicate_loop, duplicate_region, crop.
    - region_start / region_end / destination_time: Beats, for duplicate_region.
    - start_marker / end_marker: Move the clip's play markers.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        ci = _to_zero_based(clip_index, "clip_index")
        payload: dict = {"track_index": ti, "clip_index": ci, "action": action}
        for key, val in (
            ("region_start", region_start), ("region_end", region_end),
            ("destination_time", destination_time),
            ("start_marker", start_marker), ("end_marker", end_marker),
        ):
            if val is not None:
                payload[key] = val
        r = ableton.send_command("manage_clip_region", payload)
        return "\n".join(f"{k}: {v}" for k, v in r.items())
    except Exception as e:
        logger.error(f"Error managing clip region: {str(e)}")
        return f"Error managing clip region: {str(e)}"


@mcp.tool()
def set_wavetable_oscillator(
    ctx: Context,
    track_index: int,
    device_index: int,
    oscillator: int = 1,
    category: str | None = None,
    wavetable: str | None = None,
    effect_mode: int | None = None,
    unison_mode: int | None = None,
    unison_voices: int | None = None,
    mono_poly: int | None = None,
    poly_voices: int | None = None,
) -> str:
    """Choose Wavetable's actual wavetables and voicing, by name.

    Which wavetable is loaded is the biggest tonal decision in the
    instrument. Call with no changes to list the available categories and
    tables.

    Parameters:
    - track_index / device_index: 1-based.
    - oscillator: 1 or 2.
    - category: Wavetable category name, e.g. "Basics", "Bass".
    - wavetable: Table name within that category.
    - effect_mode / unison_mode / unison_voices / mono_poly / poly_voices:
      Integer modes; mono_poly 0 = mono, 1 = poly.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        payload: dict = {
            "track_index": ti, "device_index": di, "oscillator": oscillator,
        }
        for key, val in (
            ("category", category), ("wavetable", wavetable),
            ("effect_mode", effect_mode), ("unison_mode", unison_mode),
            ("unison_voices", unison_voices), ("mono_poly", mono_poly),
            ("poly_voices", poly_voices),
        ):
            if val is not None:
                payload[key] = val
        r = ableton.send_command("set_wavetable_oscillator", payload)
        lines = [f"'{r.get('device')}' oscillator {r.get('oscillator')}"]
        cats = r.get("categories") or []
        if cats:
            lines.append("  categories: " + ", ".join(cats))
        tables = r.get("wavetables_in_category") or []
        if tables:
            lines.append("  tables here: " + ", ".join(tables[:40]))
        changed = r.get("changed") or {}
        if changed:
            lines.append("  changed: " + ", ".join(
                f"{k}={v}" for k, v in changed.items()))
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error setting wavetable oscillator: {str(e)}")
        return f"Error setting wavetable oscillator: {str(e)}"


@mcp.tool()
def duplicate_device(ctx: Context, track_index: int, device_index: int) -> str:
    """Duplicate a device in place on its track.

    Parameters:
    - track_index / device_index: 1-based.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        r = ableton.send_command("duplicate_device", {
            "track_index": ti, "device_index": di,
        })
        return f"'{r.get('track')}' chain: " + " → ".join(r.get("chain") or [])
    except Exception as e:
        logger.error(f"Error duplicating device: {str(e)}")
        return f"Error duplicating device: {str(e)}"


@mcp.tool()
def undo_step(ctx: Context, action: str = "begin") -> str:
    """Group several changes into a single undo entry.

    Call with "begin", make the changes, then call with "end". One Cmd-Z
    then reverses the whole edit rather than unpicking it call by call.

    Parameters:
    - action: "begin" or "end".
    """
    try:
        ableton = get_ableton_connection()
        r = ableton.send_command("undo_step", {"action": action})
        return f"Undo step {r.get('action')}"
    except Exception as e:
        logger.error(f"Error in undo step: {str(e)}")
        return f"Error in undo step: {str(e)}"


@mcp.tool()
def transport_action(
    ctx: Context, action: str = "stop_all_clips", value: float | None = None
) -> str:
    """Fire a transport or session action.

    Parameters:
    - action: stop_all_clips, tap_tempo, capture_and_insert_scene,
      continue_playing, play_selection, trigger_session_record,
      jump_to_next_cue, jump_to_prev_cue, re_enable_automation,
      back_to_arranger, jump_by, scrub_by.
    - value: Beats, for jump_by and scrub_by.

    capture_and_insert_scene turns whatever is currently playing into a new
    scene — the way to keep a live jam that was never written down.
    """
    try:
        ableton = get_ableton_connection()
        payload: dict = {"action": action}
        if value is not None:
            payload["value"] = value
        r = ableton.send_command("transport_action", payload)
        return ", ".join(f"{k}={v}" for k, v in r.items())
    except Exception as e:
        logger.error(f"Error in transport action: {str(e)}")
        return f"Error in transport action: {str(e)}"


@mcp.tool()
def set_mixer_extras(
    ctx: Context,
    track_index: int,
    track_activator: bool | None = None,
    panning_mode: int | None = None,
    left_split_stereo: float | None = None,
    right_split_stereo: float | None = None,
    cue_volume: float | None = None,
) -> str:
    """Mixer controls beyond volume, pan and sends.

    Parameters:
    - track_index: 1-based.
    - track_activator: Track on/off (the mixer's own activator).
    - panning_mode: 0 = normal pan, 1 = split stereo pan.
    - left_split_stereo / right_split_stereo: 0.0-1.0, split stereo mode only.
    - cue_volume: Master cue level, 0.0-1.0.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        payload: dict = {"track_index": ti}
        for key, val in (
            ("track_activator", track_activator), ("panning_mode", panning_mode),
            ("left_split_stereo", left_split_stereo),
            ("right_split_stereo", right_split_stereo),
            ("cue_volume", cue_volume),
        ):
            if val is not None:
                payload[key] = val
        r = ableton.send_command("set_mixer_extras", payload)
        changed = r.get("changed") or {}
        if not changed:
            return f"No mixer changes requested for '{r.get('track')}'"
        return f"'{r.get('track')}': " + ", ".join(
            f"{k}={v}" for k, v in changed.items())
    except Exception as e:
        logger.error(f"Error setting mixer extras: {str(e)}")
        return f"Error setting mixer extras: {str(e)}"


@mcp.tool()
def set_view_detail(
    ctx: Context,
    track_index: int | None = None,
    clip_index: int | None = None,
    device_index: int | None = None,
    draw_mode: bool | None = None,
) -> str:
    """Focus Live's detail view on a clip or device, so the user sees it.

    Parameters:
    - track_index / clip_index / device_index: 1-based.
    - draw_mode: Turn the MIDI editor's draw mode on or off.
    """
    try:
        ableton = get_ableton_connection()
        payload: dict = {}
        for key, val in (
            ("track_index", track_index), ("clip_index", clip_index),
            ("device_index", device_index),
        ):
            if val is not None:
                payload[key] = _to_zero_based(val, key)
        if draw_mode is not None:
            payload["draw_mode"] = draw_mode
        if not payload:
            return "Nothing to focus"
        r = ableton.send_command("set_view_detail", payload)
        focused = r.get("focused") or {}
        return "Focused " + ", ".join(f"{k}='{v}'" for k, v in focused.items())
    except Exception as e:
        logger.error(f"Error setting view detail: {str(e)}")
        return f"Error setting view detail: {str(e)}"


@mcp.tool()
def set_scene_signature(
    ctx: Context,
    scene_index: int,
    numerator: int | None = None,
    denominator: int | None = None,
    enabled: bool | None = None,
) -> str:
    """Give a scene its own time signature, applied when it launches.

    Parameters:
    - scene_index: 1-based.
    - numerator / denominator: e.g. 6 and 8 for 6/8.
    - enabled: Whether the scene overrides the Set's signature.
    """
    try:
        ableton = get_ableton_connection()
        si = _to_zero_based(scene_index, "scene_index")
        payload: dict = {"scene_index": si}
        for key, val in (
            ("numerator", numerator), ("denominator", denominator),
            ("enabled", enabled),
        ):
            if val is not None:
                payload[key] = val
        r = ableton.send_command("set_scene_signature", payload)
        changed = r.get("changed") or {}
        return f"Scene '{r.get('scene')}': " + ", ".join(
            f"{k}={v}" for k, v in changed.items())
    except Exception as e:
        logger.error(f"Error setting scene signature: {str(e)}")
        return f"Error setting scene signature: {str(e)}"


@mcp.tool()
def set_device_sidechain(
    ctx: Context,
    track_index: int,
    device_index: int,
    source_track: str,
    channel: str | None = None,
) -> str:
    """Route a device's sidechain input from another track, by track name.

    Live's Compressor, Gate and Auto Filter expose input_routing_type, which
    IS the sidechain source — so classic kick-ducks-bass pumping can be set
    up entirely from here. Glue Compressor exposes no routing at all and
    therefore cannot be sidechained through the API; use Compressor instead.

    After routing, enable the device's own sidechain switch and set the
    threshold with set_device_parameter.

    Parameters:
    - track_index / device_index: 1-based location of the compressor.
    - source_track: Name of the track to listen to, e.g. "DRUMS".
    - channel: Optional input channel, e.g. "Pre FX" or "Post FX".
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        payload: dict = {
            "track_index": ti, "device_index": di, "source_track": source_track,
        }
        if channel is not None:
            payload["channel"] = channel
        r = ableton.send_command("set_device_sidechain", payload)
        extra = f", channel {r.get('channel')}" if r.get("channel") else ""
        return (
            f"'{r.get('device')}' on '{r.get('track')}' now sidechained from "
            f"'{r.get('sidechain_source')}'{extra}"
        )
    except Exception as e:
        logger.error(f"Error setting sidechain: {str(e)}")
        return f"Error setting sidechain: {str(e)}"


@mcp.tool()
def get_drift_modulation(
    ctx: Context,
    track_index: int,
    device_index: int,
) -> str:
    """Read Drift's modulation matrix, voicing and pitch bend range.

    Reach for this before rewiring Drift: it lists what each of the three
    matrix slots and the dedicated filter / LFO / pitch / shape sources are
    currently routed to, AND every legal option per slot with its exact
    spelling. Live's option lists differ between versions, so read first,
    then pass the names straight back into set_drift_modulation.

    Parameters:
    - track_index / device_index: 1-based.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        r = ableton.send_command("get_drift_modulation", {
            "track_index": ti, "device_index": di,
        })
        lines = [f"'{r.get('device')}' on '{r.get('track')}'"]
        slots = r.get("slots") or {}
        options = r.get("options") or {}
        for key, value in slots.items():
            choices = options.get(key) or []
            suffix = ""
            if choices:
                shown = ", ".join(str(c) for c in choices[:20])
                if len(choices) > 20:
                    shown += ", ... (+{0} more)".format(len(choices) - 20)
                suffix = f"  (options: {shown})"
            lines.append(f"  {key}: {value}{suffix}")
        if r.get("pitch_bend_range") is not None:
            lines.append(f"  pitch_bend_range: {r.get('pitch_bend_range')}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error reading Drift modulation: {str(e)}")
        return f"Error reading Drift modulation: {str(e)}"


@mcp.tool()
def set_drift_modulation(
    ctx: Context,
    track_index: int,
    device_index: int,
    source_1: str | None = None,
    target_1: str | None = None,
    source_2: str | None = None,
    target_2: str | None = None,
    source_3: str | None = None,
    target_3: str | None = None,
    filter_source_1: str | None = None,
    filter_source_2: str | None = None,
    lfo_source: str | None = None,
    pitch_source_1: str | None = None,
    pitch_source_2: str | None = None,
    shape_source: str | None = None,
    voice_count: str | int | None = None,
    voice_mode: str | None = None,
    pitch_bend_range: int | None = None,
) -> str:
    """Rewire Drift's modulation matrix and voice handling, all by NAME.

    This is where a Drift patch stops being static: route the envelope to
    pitch for a bass pluck, an LFO to filter for movement under a pad, or
    velocity to shape so playing harder opens the tone. The three general
    slots are source/target pairs; filter, LFO, pitch and shape have their
    own dedicated source picks with their own hard-wired destinations.

    Voicing lives here too — go mono with glide for an acid line, or raise
    the voice count for chords and long tails. Pitch bend range matters the
    moment you play Drift from a keyboard or automate bends in a clip.

    Names are resolved against Live's own option lists (exact, then
    case-insensitive, then partial), so run get_drift_modulation first to
    see the valid spellings. Anything omitted is left untouched, and any
    slot this Live version does not expose is reported as "<not supported>"
    rather than failing the whole call.

    Parameters:
    - track_index / device_index: 1-based.
    - source_1..3 / target_1..3: general matrix slots, e.g. source "LFO",
      target "Filter Freq".
    - filter_source_1 / filter_source_2 / lfo_source / pitch_source_1 /
      pitch_source_2 / shape_source: dedicated modulation sources.
    - voice_count: polyphony, as the label Live shows, e.g. "4" or 4.
    - voice_mode: e.g. "Poly", "Mono", "Stereo".
    - pitch_bend_range: in semitones, e.g. 2 or 12.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        payload: dict = {"track_index": ti, "device_index": di}
        for key, val in (
            ("source_1", source_1), ("target_1", target_1),
            ("source_2", source_2), ("target_2", target_2),
            ("source_3", source_3), ("target_3", target_3),
            ("filter_source_1", filter_source_1),
            ("filter_source_2", filter_source_2),
            ("lfo_source", lfo_source),
            ("pitch_source_1", pitch_source_1),
            ("pitch_source_2", pitch_source_2),
            ("shape_source", shape_source),
            ("voice_count", str(voice_count) if voice_count is not None
             else None),
            ("voice_mode", voice_mode),
            ("pitch_bend_range", pitch_bend_range),
        ):
            if val is not None:
                payload[key] = val
        if len(payload) == 2:
            return "No Drift changes requested"
        r = ableton.send_command("set_drift_modulation", payload)
        lines = [f"'{r.get('device')}' on '{r.get('track')}'"]
        changed = r.get("changed") or {}
        if changed:
            lines.append("  changed: " + ", ".join(
                f"{k}={v}" for k, v in changed.items()))
        slots = r.get("slots") or {}
        if slots:
            lines.append("  now: " + ", ".join(
                f"{k}={v}" for k, v in slots.items()))
        if r.get("pitch_bend_range") is not None:
            lines.append(f"  pitch_bend_range: {r.get('pitch_bend_range')}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error setting Drift modulation: {str(e)}")
        return f"Error setting Drift modulation: {str(e)}"

@mcp.tool()
def get_device_modes(
    ctx: Context,
    track_index: int,
    device_index: int,
    chain_index: int = 0,
) -> str:
    """List every mode switch a device exposes, with its exact option names.

    Some of Live's best instruments hide their character in enum lists rather
    than knobs: Spectral Resonator's Mode (Chord / Wall / Tube / Beam …),
    Hybrid Reverb's IR category and file, Roar's routing (Serial / Parallel /
    Mid-Side / Multiband), Shifter's Pitch / Ring / Frequency-shift mode,
    Meld's engine per voice, EQ Eight's edit and global modes. None of these
    are automatable parameters, so set_device_parameter cannot reach them.

    Call this first — the option names and their ordering differ per device
    and per Live version, and set_device_mode matches against exactly these
    strings. Plain numeric extras (polyphony, pitch bend range, voice counts,
    IR attack/decay/size) are reported underneath as values.

    Parameters:
    - track_index / device_index: 1-based location of the device.
    - chain_index: Chain number inside a rack (1-based, 0 = no chain). Reads
      the first device in that chain.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        ci = _optional_to_zero_based(chain_index, "chain_index")
        r = ableton.send_command("get_device_modes", {
            "track_index": ti,
            "device_index": di,
            "chain_index": ci,
        })
        lines = [
            f"'{r.get('device')}' ({r.get('class_name')}) on '{r.get('track')}'"
        ]
        modes = r.get("modes") or []
        if modes:
            lines.append("")
            lines.append("Modes (set by name with set_device_mode):")
            for m in modes:
                options = m.get("options") or []
                shown = ", ".join(str(o) for o in options[:24])
                if len(options) > 24:
                    shown += f", … (+{len(options) - 24} more)"
                lines.append(f"  {m['name']} = {m.get('current')}")
                lines.append(f"    options: {shown}")
        values = r.get("values") or []
        if values:
            lines.append("")
            lines.append("Values:")
            for v in values:
                lines.append(f"  {v['name']} = {v.get('value')}")
        if not modes and not values:
            lines.append("")
            lines.append(
                "This device exposes no modes beyond the standard Device "
                "members — everything about it lives in its parameters "
                "(use get_device_parameters / set_device_parameter)."
            )
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error getting device modes: {str(e)}")
        return f"Error getting device modes: {str(e)}"


@mcp.tool()
def set_device_mode(
    ctx: Context,
    track_index: int,
    device_index: int,
    mode: str | None = None,
    value: str | int | float | bool | None = None,
    settings: dict[str, str | int | float | bool] | None = None,
    chain_index: int = 0,
) -> str:
    """Set a device's mode switches by NAME — the character choices, not knobs.

    Reach for this when a sound needs to change identity rather than degree:
    Spectral Resonator from Chord to Tube, or from Poly to Mono so it tracks
    a bassline; Hybrid Reverb swapping its impulse response from a hall to a
    plate or a piece of metal; Roar going Mid-Side so distortion only hits
    the sides; Shifter switching from pitch-shift to ring modulation; Meld
    picking a different engine; EQ Eight going Stereo -> Mid/Side or turning
    on oversampling before a master bounce.

    These live in Live's enum lists, not in the automatable parameter list,
    so set_device_parameter cannot touch them. Values are matched
    case-insensitively against the device's own option strings (a unique
    partial match is accepted), so run get_device_modes first to see the
    exact names — the ordering is never assumed.

    The same call also sets plain numeric or on/off members that sit beside
    those lists: polyphony, pitch_bend_range, poly_voices, unison_voices,
    ir_attack_time, ir_decay_time, ir_size_factor, ir_time_shaping_on,
    env_listen, oversample.

    Parameters:
    - track_index / device_index: 1-based location of the device.
    - mode / value: one setting, e.g. mode="mod_mode", value="Wall", or
      mode="polyphony", value=4.
    - settings: several at once, applied in order, e.g.
      {"ir_category": "Halls", "ir_file": "Concert Hall", "ir_decay_time": 0.6}.
      Order matters where one list depends on another (pick ir_category
      before ir_file — the file list is re-read after each write). Omitted
      members are left unchanged. If one setting fails, the error names the
      settings that were already applied.
    - chain_index: Chain number inside a rack (1-based, 0 = no chain). Targets
      the first device in that chain.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        ci = _optional_to_zero_based(chain_index, "chain_index")
        combined: dict = {}
        if mode is not None:
            if value is None:
                raise ValueError("mode needs a value")
            combined[mode] = value
        if settings:
            combined.update(settings)
        if not combined:
            raise ValueError(
                "Pass mode + value, or a settings dict of mode -> value"
            )
        r = ableton.send_command("set_device_mode", {
            "track_index": ti,
            "device_index": di,
            "chain_index": ci,
            "settings": combined,
        })
        changed = r.get("changed") or {}
        if not changed:
            return f"'{r.get('device')}': nothing changed"
        return f"'{r.get('device')}' on '{r.get('track')}': " + ", ".join(
            f"{k} = {v}" for k, v in changed.items())
    except Exception as e:
        logger.error(f"Error setting device mode: {str(e)}")
        return f"Error setting device mode: {str(e)}"

@mcp.tool()
def get_plugin_info(
    ctx: Context,
    track_index: int,
    device_index: int,
    chain_index: int = 0,
    bank: int = 0,
) -> str:
    """Report what a VST/AU plugin or Max for Live device exposes to Live.

    Reach for this before trying to automate a third-party plugin. A plugin is
    a black box: Live only sees the parameters the plugin publishes, so the
    knob you can see in the plugin's own window may simply not exist here
    until you map it by hand in Live's Configure mode. This tool tells you
    what really is reachable — the parameter count, the plugin's own bank
    grouping (the pages Push and MIDI controllers page through), and which of
    the plugin's internal programs/presets is currently selected.

    Bank listings print each slot's real parameter number, which is what
    set_device_parameter's parameter_index takes, and values normalized
    0.0-1.0, which is what its value takes.

    Everything else about a VST stays opaque: no preset browsing by folder,
    no reading the plugin's GUI state, no saving new plugin presets.

    Parameters:
    - track_index / device_index: 1-based location of the plugin.
    - chain_index: Chain number inside a rack (1-based, 0 = not in a rack).
      device_index then addresses the rack, and its chain's first device is used.
    - bank: 1-based bank whose parameters you want listed. 0 = list bank
      names only, which is the cheap first call on a big synth.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        ci = _optional_to_zero_based(chain_index, "chain_index")
        payload: dict = {"track_index": ti, "device_index": di, "chain_index": ci}
        if bank:
            payload["bank"] = _to_zero_based(bank, "bank")
        r = ableton.send_command("get_plugin_info", payload)

        lines = [
            f"'{r.get('device')}' on '{r.get('track')}' "
            f"({r.get('class_name')})",
            f"  parameters: {r.get('parameter_count')}",
        ]
        if not (r.get("is_plugin") or r.get("is_max_device")):
            lines.append(
                "  note: this is a stock Live device, not a VST/AU or M4L device"
            )

        count = r.get("preset_count") or 0
        if count:
            sel = r.get("selected_preset_index")
            where = f"{sel + 1}/{count}" if isinstance(sel, int) else f"?/{count}"
            lines.append(
                f"  preset: '{r.get('selected_preset')}' ({where})"
            )
            names = r.get("presets") or []
            shown = ", ".join(names[:20])
            more = f" ... (+{len(names) - 20} more)" if len(names) > 20 else ""
            lines.append(f"  presets: {shown}{more}")
        else:
            lines.append("  presets: none exposed")

        bank_names = r.get("bank_names") or []
        if bank_names:
            lines.append(f"  banks ({r.get('bank_count')}): " + ", ".join(
                f"{i + 1}:{n}" for i, n in enumerate(bank_names)))
        elif r.get("bank_count"):
            lines.append(f"  banks: {r.get('bank_count')} (unnamed)")
        else:
            lines.append("  banks: none exposed")

        if r.get("bank_parameters") is not None:
            label = r.get("bank_name") or f"bank {(r.get('bank') or 0) + 1}"
            lines.append(f"\n  {label} (slot -> parameter_index):")
            for i, p in enumerate(r["bank_parameters"], start=1):
                if p is None:
                    lines.append(f"    slot {i}: -")
                    continue
                idx = p.get("index")
                pi = f"#{idx + 1}" if isinstance(idx, int) else "#?"
                quant = " [quantized]" if p.get("is_quantized") else ""
                lines.append(
                    f"    slot {i}: param {pi} {p['name']} = "
                    f"{p['display_value']} (normalized {p['value']:.3f})"
                    f"{quant}")
            lines.append(
                "    set with set_device_parameter(parameter_index=<param #>, "
                "value=<0.0-1.0>)")

        buses = r.get("device_io_buses") or {}
        if buses:
            lines.append("\n  device IO buses: " + ", ".join(
                f"{k}={v}" for k, v in buses.items())
                + "  (inspect with get_device_io)")
        if r.get("has_sidechain_input"):
            lines.append("  sidechain input available (use set_device_sidechain)")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error getting plugin info: {str(e)}")
        return f"Error getting plugin info: {str(e)}"


@mcp.tool()
def select_plugin_preset(
    ctx: Context,
    track_index: int,
    device_index: int,
    preset: str | None = None,
    preset_index: int | None = None,
    chain_index: int = 0,
) -> str:
    """Load a VST/AU program or Max for Live preset by name or number.

    The fast way to audition a third-party synth: ask get_plugin_info for the
    preset list, then jump straight to "Deep Bass 3" instead of clicking
    next/previous through 128 programs. Also how you recall a specific patch
    reproducibly when building a live set. Use navigate_device_preset instead
    when you just want to step to the next or previous one.

    Only the plugin's own program list is reachable — Live's browser presets
    and .vstpreset/.fxp files on disk are not, and there is no way to save a
    new preset back into the plugin from here.

    Parameters:
    - track_index / device_index: 1-based location of the plugin.
    - preset: Preset name; case-insensitive, a unique substring is enough.
    - preset_index: 1-based preset number, as an alternative to preset.
    - chain_index: Chain number inside a rack (1-based, 0 = not in a rack).
    """
    try:
        if preset is None and preset_index is None:
            return "Provide either preset (name) or preset_index (1-based)."
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        ci = _optional_to_zero_based(chain_index, "chain_index")
        payload: dict = {"track_index": ti, "device_index": di, "chain_index": ci}
        if preset is not None:
            payload["preset"] = preset
        if preset_index is not None:
            payload["preset_index"] = _to_zero_based(preset_index, "preset_index")
        r = ableton.send_command("select_plugin_preset", payload)
        return (
            f"'{r.get('device')}' on '{r.get('track')}' loaded preset "
            f"'{r.get('preset')}' ({(r.get('preset_index') or 0) + 1}/"
            f"{r.get('preset_count')})"
        )
    except Exception as e:
        logger.error(f"Error selecting plugin preset: {str(e)}")
        return f"Error selecting plugin preset: {str(e)}"


@mcp.tool()
def get_device_io(
    ctx: Context,
    track_index: int,
    device_index: int,
    chain_index: int = 0,
) -> str:
    """List a device's audio/MIDI buses and how each one is currently routed.

    Max for Live devices can have extra inputs and outputs beyond the signal
    chain they sit in — a sidechain input on an M4L compressor, a second
    output feeding a different track, a MIDI output driving another
    instrument. Those buses are invisible in the LOM until you look here, and
    this is the call that tells you which bus names and channel names
    set_device_io_routing will accept.

    Native Live devices have no such buses. The ones that can be sidechained
    (Compressor, Gate, Auto Filter) expose a single device-level input
    instead, reported separately below and set with set_device_sidechain.

    Parameters:
    - track_index / device_index: 1-based location of the device.
    - chain_index: Chain number inside a rack (1-based, 0 = not in a rack).
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        ci = _optional_to_zero_based(chain_index, "chain_index")
        r = ableton.send_command("get_device_io", {
            "track_index": ti, "device_index": di, "chain_index": ci,
        })
        lines = [
            f"IO for '{r.get('device')}' on '{r.get('track')}' "
            f"({r.get('class_name')}):"
        ]
        buses = r.get("buses") or []
        if not buses:
            lines.append("  no Device.IO buses (only Max for Live devices have them)")
        for b in buses:
            name = f" '{b['name']}'" if b.get("name") else ""
            lines.append(
                f"\n  {b['bus']}[{b['index'] + 1}]{name}: "
                f"type={b.get('routing_type')} channel={b.get('routing_channel')}"
            )
            for label, key in (("types", "available_types"),
                               ("channels", "available_channels")):
                vals = b.get(key) or []
                if vals:
                    shown = ", ".join(vals[:24])
                    more = f" ... (+{len(vals) - 24} more)" if len(vals) > 24 else ""
                    lines.append(f"    available {label}: {shown}{more}")
        sc = r.get("sidechain_input")
        if sc:
            lines.append(
                f"\n  sidechain input: type={sc.get('routing_type')} "
                f"channel={sc.get('routing_channel')} "
                f"(set with set_device_sidechain)"
            )
            vals = sc.get("available_types") or []
            if vals:
                lines.append("    available sources: " + ", ".join(vals[:24]))
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error getting device IO: {str(e)}")
        return f"Error getting device IO: {str(e)}"


@mcp.tool()
def set_device_io_routing(
    ctx: Context,
    track_index: int,
    device_index: int,
    bus: str,
    routing_type: str | None = None,
    routing_channel: str | None = None,
    bus_index: int = 1,
    chain_index: int = 0,
) -> str:
    """Route one of a Max for Live device's audio or MIDI buses.

    This is how you wire an M4L device into the rest of the Set: feed an M4L
    sidechain/analysis input from the kick track, send an M4L device's second
    audio output to its own track for parallel processing, or point an M4L
    sequencer's MIDI output at another instrument. Anything that in the UI is
    the little routing chooser on the device itself rather than on the track.

    Names match case-insensitively and a unique substring is enough, but the
    options depend on the device and on which tracks exist — call
    get_device_io first rather than guessing. Passing both routing_type and
    routing_channel is safe: the type is applied first, then the channel is
    matched against the choices that type leaves available. Native Live
    devices have no such buses; for Compressor/Gate/Auto Filter sidechain use
    set_device_sidechain.

    Parameters:
    - track_index / device_index: 1-based location of the device.
    - bus: "audio_in", "audio_out", "midi_in" or "midi_out".
    - routing_type: Source or destination, e.g. "DRUMS", "Ext. In", "No Input".
    - routing_channel: Channel within that type, e.g. "Pre FX", "1/2".
    - bus_index: 1-based, when the device has more than one bus of that kind.
    - chain_index: Chain number inside a rack (1-based, 0 = not in a rack).
    """
    try:
        if routing_type is None and routing_channel is None:
            return "Provide routing_type and/or routing_channel."
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        ci = _optional_to_zero_based(chain_index, "chain_index")
        payload: dict = {
            "track_index": ti,
            "device_index": di,
            "chain_index": ci,
            "bus": bus,
            "bus_index": _to_zero_based(bus_index, "bus_index"),
        }
        if routing_type is not None:
            payload["routing_type"] = routing_type
        if routing_channel is not None:
            payload["routing_channel"] = routing_channel
        r = ableton.send_command("set_device_io_routing", payload)
        changed = r.get("changed") or {}
        detail = ", ".join(f"{k}={v}" for k, v in changed.items())
        return (
            f"'{r.get('device')}' on '{r.get('track')}' "
            f"{r.get('bus')}[{(r.get('bus_index') or 0) + 1}]: {detail}"
        )
    except Exception as e:
        logger.error(f"Error setting device IO routing: {str(e)}")
        return f"Error setting device IO routing: {str(e)}"

@mcp.tool()
def control_looper(
    ctx: Context,
    track_index: int,
    action: str = "info",
    device_index: int | None = None,
    scene_index: int | None = None,
) -> str:
    """Drive a Looper like a foot pedal — record, overdub, play, stop, export.

    This is the live-performance loop: hit `record` to lay a guitar or vocal
    pass, `overdub` to stack the next layer, `play` to let it run under the
    song, `undo` to drop the last overdub when a harmony misses, and `clear`
    to reset between songs. `half_speed`/`double_speed` are the octave trick —
    sing a line, halve the speed and it becomes a bass part. `half_length`/
    `double_length` reinterpret the same audio against a longer or shorter
    bar count. `export_to_clip_slot` bounces the loop into Session so a live
    take turns into a clip you can keep.

    Every call returns the Looper's state afterwards (State, loop_length,
    record_length, tempo) so you can see whether it is recording or playing.
    Actions the device does not expose are rejected with the list it does
    expose, in `available_actions`.

    Parameters:
    - track_index: 1-based track holding the Looper.
    - action: info, record, overdub, play, stop, clear, undo,
      double_length, half_length, double_speed, half_speed,
      export_to_clip_slot.
    - device_index: 1-based, if the track has more than one Looper.
    - scene_index: 1-based scene to export into (export_to_clip_slot only).
      Omit to use whatever clip slot is currently selected.
    """
    try:
        ableton = get_ableton_connection()
        payload: dict = {
            "track_index": _to_zero_based(track_index, "track_index"),
            "action": action,
        }
        if device_index is not None:
            payload["device_index"] = _to_zero_based(
                device_index, "device_index")
        if scene_index is not None:
            payload["scene_index"] = _to_zero_based(scene_index, "scene_index")
        r = ableton.send_command("control_looper", payload)
        if not isinstance(r, dict):
            return str(r)
        return "\n".join(f"{k}: {v}" for k, v in r.items())
    except Exception as e:
        logger.error(f"Error controlling looper: {str(e)}")
        return f"Error controlling looper: {str(e)}"


@mcp.tool()
def configure_looper(
    ctx: Context,
    track_index: int,
    device_index: int | None = None,
    record_length: str | None = None,
    overdub_after_record: bool | None = None,
    tempo: float | None = None,
) -> str:
    """Set up a Looper before the gig — record length, overdub behaviour, tempo.

    Reach for this when soundchecking rather than mid-song. `record_length`
    decides whether the Looper records freely until you stop it or punches out
    automatically after a fixed number of bars — set it to a bar count when the
    duo needs loops that always land on the phrase, and leave it free when the
    intro is rubato. `overdub_after_record` makes the Looper slide straight
    into overdub when the first pass ends, so the guitarist can keep both hands
    on the instrument instead of reaching back to trigger the next layer.

    Pass a record length by NAME exactly as Live shows it; the available names
    come back in every response as `record_length_options` (and from
    control_looper's `info`). Never guess a number — the ordering differs
    between Live builds. Anything omitted is left unchanged. Settings this
    Live build refuses come back under `read_only`; settings this device does
    not have at all come back under `unsupported`.

    Parameters:
    - track_index: 1-based track holding the Looper.
    - device_index: 1-based, if the track has more than one Looper.
    - record_length: display name from `record_length_options`,
      e.g. "none", "1 bar", "4 bars" (case-insensitive, partial match allowed).
    - overdub_after_record: True to jump into overdub the moment recording ends.
    - tempo: the Looper's tempo in BPM.
    """
    try:
        ableton = get_ableton_connection()
        payload: dict = {
            "track_index": _to_zero_based(track_index, "track_index"),
        }
        if device_index is not None:
            payload["device_index"] = _to_zero_based(
                device_index, "device_index")
        if record_length is not None:
            payload["record_length"] = record_length
        if overdub_after_record is not None:
            payload["overdub_after_record"] = overdub_after_record
        if tempo is not None:
            payload["tempo"] = tempo
        r = ableton.send_command("configure_looper", payload)
        if not isinstance(r, dict):
            return str(r)
        return "\n".join(f"{k}: {v}" for k, v in r.items())
    except Exception as e:
        logger.error(f"Error configuring looper: {str(e)}")
        return f"Error configuring looper: {str(e)}"

# NOTE: manage_rack below SUPERSEDES the existing manage_rack tool (~line 1192) —
# DELETE that one and put this in its place. Old call sites keep working (same
# action names, same params), with one deliberate correction: the variation index
# passed as `value` is now 1-based like every other index in this server.


@mcp.tool()
def get_rack_map(
    ctx: Context,
    track_index: int,
    device_index: int,
    pads: str = "filled",
    include_devices: bool = True,
) -> str:
    """Read a whole rack in one shot: macros, chains, return chains, drum pads.

    Reach for this before touching anything inside a rack — it is the map that
    tells you which chain index is the sub bass, which pad note the hi-hat sits
    on, whether the pads are already choked, and how many macro variations are
    stored. Much cheaper than probing chain by chain.

    Parameters:
    - track_index / device_index: 1-based.
    - pads: 'filled' (only pads holding samples, default), 'visible' (the 16
      pads currently on screen) or 'none'.
    - include_devices: list the devices inside each chain.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        r = ableton.send_command("get_rack_map", {
            "track_index": ti,
            "device_index": di,
            "pads": pads,
            "include_devices": include_devices,
        })

        lines = [f"{r.get('device')} ({r.get('class_name')}) on '{r.get('track')}'"]
        lines.append(
            f"  macros visible: {r.get('visible_macro_count')} | "
            f"mapped: {r.get('has_macro_mappings')} | "
            f"variations: {r.get('variation_count')} "
            f"(selected {r.get('selected_variation_index')})"
        )
        for m in r.get("macros", []):
            lines.append(f"    macro {m['index']}: {m['name']} = {m['display']}")
        sel = r.get("chain_selector")
        if sel:
            lines.append(f"  chain selector: {sel.get('display')}")

        def chain_lines(items: list, label: str) -> None:
            if not items:
                return
            lines.append(f"  {label} ({len(items)}):")
            for c in items:
                flags = "".join([
                    " [muted]" if c.get("mute") else "",
                    " [solo]" if c.get("solo") else "",
                    "" if c.get("chain_activator_name") in (None, "On")
                    else f" [{c.get('chain_activator_name')}]",
                ])
                devs = ", ".join(c.get("devices", [])) or "empty"
                mix = c.get("volume")
                mix_str = f" vol {mix}" if mix else ""
                idx = c.get("index")
                num = "?" if idx is None else idx + 1
                lines.append(
                    f"    {num}. {c['name']}{flags}{mix_str} → {devs}"
                )

        chain_lines(r.get("chains", []), "chains")
        chain_lines(r.get("return_chains", []), "return chains")

        pad_list = r.get("pads")
        if pad_list is not None:
            lines.append(f"  drum pads ({r.get('pad_filter')}, {len(pad_list)}):")
            for p in pad_list:
                flags = "".join([
                    " [muted]" if p.get("mute") else "",
                    " [solo]" if p.get("solo") else "",
                ])
                choke = p.get("choke_group")
                choke_str = f" choke {choke}" if choke else ""
                devs = ", ".join(p.get("devices", [])) or "empty"
                lines.append(
                    f"    note {p['note']}: {p['name']}{flags}{choke_str} → {devs}"
                )
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error getting rack map: {str(e)}")
        return f"Error getting rack map: {str(e)}"


@mcp.tool()
def manage_rack(
    ctx: Context,
    track_index: int,
    device_index: int,
    action: str = "info",
    value: float | None = None,
    count: int = 1,
    chain_index: int | None = None,
) -> str:
    """Shape a rack: macro count, macro randomisation, chain selector, chain list.

    This is the "performance surface" tool. Add macros when you want fewer,
    bigger knobs to automate or MIDI-map; randomize_macros when you want a
    sound-design accident to react to; chain_selector when a rack is being used
    as a morph/switch between layers (the Live way to A/B whole sound worlds
    from one automation lane). show_chains reveals the chain list in the UI so
    you can see what you are addressing.

    Parameters:
    - track_index / device_index: 1-based.
    - action: info, add_macro, remove_macro, randomize_macros, chain_selector,
      show_chains, hide_chains, insert_chain, and the legacy variation actions
      (store_variation, recall_variation, delete_variation,
      recall_last_variation) which forward to manage_rack_variations.
    - value: 0.0-1.0 for chain_selector; for recall_variation /
      delete_variation it is the 1-based variation number (same base as
      manage_rack_variations).
    - count: how many macros to add/remove (default 1). Live caps at 16.
    - chain_index: 1-based insert position for insert_chain (default: end).
    """
    _VARIATION_ACTIONS = (
        "recall_variation", "delete_variation",
    )
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        payload: dict = {
            "track_index": ti,
            "device_index": di,
            "action": action,
            "count": count,
        }
        if value is not None:
            if action in _VARIATION_ACTIONS:
                # `value` is a variation index here, and every index this
                # server exposes is 1-based.
                payload["value"] = _to_zero_based(int(value), "value")
            else:
                payload["value"] = value
        if chain_index is not None:
            payload["chain_index"] = _to_zero_based(chain_index, "chain_index")
        r = ableton.send_command("manage_rack", payload)
        return "\n".join(f"{k}: {v}" for k, v in r.items())
    except Exception as e:
        logger.error(f"Error managing rack: {str(e)}")
        return f"Error managing rack: {str(e)}"


@mcp.tool()
def manage_rack_variations(
    ctx: Context,
    track_index: int,
    device_index: int,
    action: str = "list",
    variation_index: int | None = None,
) -> str:
    """Store and recall macro variations — snapshots of every macro at once.

    Use this to build arrangement states inside a single rack: store one
    variation for the intro filter position, one for the drop, one for the
    breakdown, then recall them live instead of drawing eight automation
    lanes. recall_last flips back to whatever you were on, which is the
    fastest A/B when you are dialling a sound in.

    Parameters:
    - track_index / device_index: 1-based.
    - action: list, store, select, recall, recall_last, delete.
    - variation_index: 1-based variation to select/recall/delete.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        payload: dict = {
            "track_index": ti,
            "device_index": di,
            "action": action,
        }
        if variation_index is not None:
            payload["variation_index"] = _to_zero_based(
                variation_index, "variation_index"
            )
        r = ableton.send_command("manage_rack_variations", payload)
        return "\n".join(f"{k}: {v}" for k, v in r.items())
    except Exception as e:
        logger.error(f"Error managing rack variations: {str(e)}")
        return f"Error managing rack variations: {str(e)}"


@mcp.tool()
def set_chain_state(
    ctx: Context,
    track_index: int,
    device_index: int,
    chain_index: int | None = None,
    chain_name: str | None = None,
    pad_note: int | None = None,
    return_chain: bool = False,
    name: str | None = None,
    color_index: int | None = None,
    mute: bool | None = None,
    solo: bool | None = None,
    volume: float | None = None,
    panning: float | None = None,
    chain_activator: bool | str | None = None,
    send_index: int | None = None,
    send_value: float | None = None,
) -> str:
    """Mix and label one chain inside a rack. Omitted values are left alone.

    A rack chain has its own little mixer, and that is where layered sounds
    actually get balanced — pull the sub layer down 3 dB, pan the two detuned
    saws apart, mute the noise layer for the verse, feed a chain into the
    rack's internal return for reverb. Naming and colouring chains is what
    makes a big instrument rack navigable a month later.

    Parameters:
    - track_index / device_index: 1-based.
    - chain_index: 1-based chain to address (default: the first one).
    - chain_name: address the chain by name instead; wins over chain_index.
    - pad_note: address a Drum Rack pad's chain by MIDI note (36 = C1, the
      bottom-left pad). NOT an index — pass the note number as-is. The device
      must be a Drum Rack.
    - return_chain: True to address the rack's internal return chains.
    - name / color_index: rename and recolour the chain.
    - mute / solo: chain-level mute and solo.
    - volume / panning: 0.0-1.0 normalized over the parameter's own range
      (panning 0.0 = hard left, 0.5 = centre).
    - chain_activator: the chain on/off switch. Pass True/False, or the exact
      value name from get_rack_map's chain_activator_list — it is a quantized
      enum, so it is resolved by name rather than by a raw number.
    - send_index (1-based) + send_value (0.0-1.0): level into one of the
      rack's internal return chains. Both are required together.
    """
    try:
        ableton = get_ableton_connection()
        payload: dict = {
            "track_index": _to_zero_based(track_index, "track_index"),
            "device_index": _to_zero_based(device_index, "device_index"),
            "return_chain": return_chain,
        }
        if chain_index is not None:
            payload["chain_index"] = _to_zero_based(chain_index, "chain_index")
        if send_index is not None:
            payload["send_index"] = _to_zero_based(send_index, "send_index")
        if pad_note is not None:
            # A MIDI note, not an index — never zero-based converted.
            payload["pad_note"] = pad_note
        for key, val in (
            ("chain_name", chain_name), ("name", name),
            ("color_index", color_index), ("mute", mute), ("solo", solo),
            ("volume", volume), ("panning", panning),
            ("chain_activator", chain_activator), ("send_value", send_value),
        ):
            if val is not None:
                payload[key] = val
        result = ableton.send_command("set_chain_state", payload)
        changed = result.get("changed", {})
        if not changed:
            return f"No changes requested for chain '{result.get('chain')}'"
        detail = ", ".join(f"{k}={v}" for k, v in changed.items())
        return f"Set {result.get('target')} '{result.get('chain')}': {detail}"
    except Exception as e:
        logger.error(f"Error setting chain state: {str(e)}")
        return f"Error setting chain state: {str(e)}"


@mcp.tool()
def manage_drum_pad(
    ctx: Context,
    track_index: int,
    device_index: int,
    pad_note: int,
    name: str | None = None,
    mute: bool | None = None,
    solo: bool | None = None,
    choke_group: int | None = None,
    out_note: int | None = None,
    copy_to_note: int | None = None,
) -> str:
    """Name, mute, choke and copy pads in a Drum Rack.

    Choke groups are the reason to reach for this: put the closed hat, open
    hat and pedal hat on the same choke group (1-16) and they cut each other
    off like a real hi-hat instead of ringing over one another. Same trick for
    808 sub slides and for cymbal chokes. copy_to_note duplicates a pad's
    whole chain onto another note — the fast way to build a velocity-layered
    or pitched variant next to the original. out_note re-maps what note the
    pad's chain sends onward, which matters when a pad feeds an external
    sampler or a second drum rack.

    Parameters:
    - track_index / device_index: 1-based; the device must be a Drum Rack.
    - pad_note: MIDI note of the pad (36 = C1 = bottom-left). NOT an index.
    - name: rename the pad (falls back to renaming its chain).
    - mute / solo: pad-level mute and solo.
    - choke_group: 1-16, or 0 for no choking.
    - out_note: MIDI note the pad's chain outputs.
    - copy_to_note: MIDI note of the destination pad to copy this pad onto.
    """
    try:
        ableton = get_ableton_connection()
        payload: dict = {
            "track_index": _to_zero_based(track_index, "track_index"),
            "device_index": _to_zero_based(device_index, "device_index"),
            # MIDI notes below, never index-converted.
            "pad_note": pad_note,
        }
        for key, val in (
            ("name", name), ("mute", mute), ("solo", solo),
            ("choke_group", choke_group), ("out_note", out_note),
            ("copy_to_note", copy_to_note),
        ):
            if val is not None:
                payload[key] = val
        result = ableton.send_command("manage_drum_pad", payload)
        changed = result.get("changed", {})
        if not changed:
            return (
                f"No changes requested for pad {result.get('note')} "
                f"('{result.get('pad')}')"
            )
        detail = ", ".join(f"{k}={v}" for k, v in changed.items())
        return f"Pad {result.get('note')} '{result.get('pad')}': {detail}"
    except Exception as e:
        logger.error(f"Error managing drum pad: {str(e)}")
        return f"Error managing drum pad: {str(e)}"

def _simpler_chain_path(chain_path: str) -> str | None:
    """Convert a 1-based 'chain.device' path to the 0-based form Live wants.

    "1.1" (first chain, first device) becomes "0.0". Empty means the Simpler
    sits directly on the track.
    """
    if not chain_path:
        return None
    parts = [p for p in str(chain_path).split(".") if p != ""]
    if not parts:
        return None
    if len(parts) % 2 != 0:
        raise ValueError(
            f"chain_path must be pairs of chain.device indices, got '{chain_path}'"
        )
    return ".".join(
        str(_to_zero_based(int(p), "chain_path segment")) for p in parts
    )


@mcp.tool()
def get_simpler_info(
    ctx: Context,
    track_index: int,
    device_index: int,
    chain_path: str = "",
) -> str:
    """Read a Simpler's playback state, its sample and its slice grid.

    Reach for this before any chop or edit: it tells you whether the Simpler is
    in Classic/One-Shot/Slicing mode, where the start and end markers sit, how
    loud the sample is, whether it's warped, and exactly where every slice
    falls — in both sample frames and beats, so the follow-up call can speak
    whichever unit is convenient. Also the way to find out that a Simpler is
    multisampled, in which case Live exposes no editable sample at all.

    Parameters:
    - track_index / device_index: 1-based.
    - chain_path: Optional 1-based "chain.device" pairs to reach a Simpler
      nested inside a rack — e.g. "1.1" for the first device of the first
      chain, "1.1.1.1" one level deeper. Drum pads count as chains.
    """
    try:
        ableton = get_ableton_connection()
        payload: dict = {
            "track_index": _to_zero_based(track_index, "track_index"),
            "device_index": _to_zero_based(device_index, "device_index"),
        }
        cp = _simpler_chain_path(chain_path)
        if cp is not None:
            payload["chain_path"] = cp
        r = ableton.send_command("get_simpler_info", payload)

        lines = [
            f"{r.get('device')} on '{r.get('track')}' ({r.get('class_name')})",
            f"  playback: {r.get('playback_mode_name', r.get('playback_mode'))}"
            f" | slicing playback: "
            f"{r.get('slicing_playback_mode_name', r.get('slicing_playback_mode'))}"
            f" | pad slicing: {r.get('pad_slicing')}",
            f"  voices: {r.get('voices')} | retrigger: {r.get('retrigger')}"
            f" | pitch bend: {r.get('pitch_bend_range')}"
            f" | per-note bend: {r.get('note_pitch_bend_range')}",
            f"  multi-sample: {r.get('multi_sample_mode')}"
            f" | playing position: {r.get('playing_position')}"
            f" (live: {r.get('playing_position_enabled')})",
            f"  can warp as/half/double: {r.get('can_warp_as')}/"
            f"{r.get('can_warp_half')}/{r.get('can_warp_double')}",
        ]
        s = r.get("sample")
        if not s:
            lines.append(f"  sample: none — {r.get('note')}")
            return "\n".join(lines)

        lines.append(f"  sample: {s.get('file_path')}")
        lines.append(
            f"    {s.get('length')} frames @ {s.get('sample_rate')} Hz"
            f" | markers {s.get('start_marker')}-{s.get('end_marker')}"
            f" | gain {s.get('gain')} ({s.get('gain_display_string')})"
        )
        lines.append(
            f"    warping: {s.get('warping')}"
            f" | warp mode: {s.get('warp_mode_name', s.get('warp_mode'))}"
            f" | warp markers: {len(s.get('warp_markers') or [])}"
        )
        lines.append(
            f"    slicing: {s.get('slicing_style_name', s.get('slicing_style'))}"
            f" | division idx {s.get('slicing_beat_division')}"
            f" | regions {s.get('slicing_region_count')}"
            f" | sensitivity {s.get('slicing_sensitivity')}"
        )
        lines.append(f"    slices: {s.get('slice_count', 0)}")
        if s.get("slices_frames"):
            lines.append(f"      frames: {s['slices_frames']}")
            lines.append(f"      beats:  {s.get('slices_beats')}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error getting simpler info: {str(e)}")
        return f"Error getting simpler info: {str(e)}"


@mcp.tool()
def set_simpler_playback(
    ctx: Context,
    track_index: int,
    device_index: int,
    chain_path: str = "",
    playback_mode: str | None = None,
    slicing_playback_mode: str | None = None,
    pad_slicing: bool | None = None,
    voices: int | None = None,
    retrigger: bool | None = None,
    pitch_bend_range: int | None = None,
    note_pitch_bend_range: int | None = None,
) -> str:
    """Set how a Simpler responds to notes. Omitted values are left alone.

    This is the "what kind of instrument is this" switch. Reach for it when a
    one-shot should stop retriggering mid-tail (playback_mode="one_shot"), when
    a chopped loop needs each slice on its own key ("slicing" plus
    pad_slicing=True), when a stacked pad is eating polyphony (voices), or when
    a bass patch should be strictly monophonic (voices=1, retrigger=True).
    slicing_playback_mode decides whether slices choke each other (mono), stack
    (poly), or pass through (thru).

    Names are resolved against Live's own list when it publishes one and
    against the documented ordering otherwise, so a raw index is accepted too
    and Live rejects anything out of range.

    Parameters:
    - track_index / device_index: 1-based.
    - chain_path: Optional 1-based "chain.device" pairs for a Simpler nested in
      a rack or under a drum pad.
    - playback_mode: classic, one_shot or slicing (or 0-2).
    - slicing_playback_mode: mono, poly or thru (or 0-2).
    - pad_slicing: True lays the slices out across the keyboard/pads.
    - voices: 1-32.
    - retrigger: True restarts the sample on a repeated note.
    - pitch_bend_range: semitones, clamps at 24.
    - note_pitch_bend_range: MPE per-note bend range in semitones.

    multi_sample_mode, playing_position and playing_position_enabled are
    read-only in Live — read them with get_simpler_info.
    """
    try:
        ableton = get_ableton_connection()
        payload: dict = {
            "track_index": _to_zero_based(track_index, "track_index"),
            "device_index": _to_zero_based(device_index, "device_index"),
        }
        cp = _simpler_chain_path(chain_path)
        if cp is not None:
            payload["chain_path"] = cp
        for key, val in (
            ("playback_mode", playback_mode),
            ("slicing_playback_mode", slicing_playback_mode),
            ("pad_slicing", pad_slicing),
            ("voices", voices),
            ("retrigger", retrigger),
            ("pitch_bend_range", pitch_bend_range),
            ("note_pitch_bend_range", note_pitch_bend_range),
        ):
            if val is not None:
                payload[key] = val
        r = ableton.send_command("set_simpler_playback", payload)
        changed = r.get("changed", {})
        if not changed:
            return f"No changes requested for '{r.get('device')}'"
        detail = ", ".join(f"{k}={v}" for k, v in changed.items())
        return f"Set '{r.get('device')}' on '{r.get('track')}': {detail}"
    except Exception as e:
        logger.error(f"Error setting simpler playback: {str(e)}")
        return f"Error setting simpler playback: {str(e)}"


@mcp.tool()
def manage_simpler_sample(
    ctx: Context,
    track_index: int,
    device_index: int,
    chain_path: str = "",
    action: str = "",
    start_beats: float | None = None,
    end_beats: float | None = None,
    start_frames: int | None = None,
    end_frames: int | None = None,
    gain: float | None = None,
    warping: bool | None = None,
    warp_mode: str | None = None,
    warp_as_beats: float | None = None,
) -> str:
    """Trim, gain, warp and reshape the sample inside a Simpler.

    The tidy-up pass on a raw sample: cut the silence off the front of a vocal
    chop (start_beats), stop a one-shot before its tail (end_beats), match a
    loop to the Set's tempo (warping=True plus warp_as_beats=8 to declare it as
    8 beats long), pick a stretch algorithm that suits the material
    (warp_mode="beats" for drums, "complex_pro" for full mixes and vocals,
    "repitch" for the tape-slowdown sound), level a sample against its
    neighbours (gain), or flip it for a reverse-cymbal riser (action="reverse").
    action="crop" permanently discards everything outside the markers, so run
    it only once the markers are right.

    Markers are sample frames in Live's API — pass beats and they get converted
    for you. gain is normalised 0.0-1.0, NOT decibels: 0.4 is unity (0.0 dB).

    Parameters:
    - track_index / device_index: 1-based.
    - chain_path: Optional 1-based "chain.device" pairs for a nested Simpler.
    - action: reverse, crop, guess_playback_length, warp_as, warp_half,
      warp_double. Omit to only set properties.
    - start_beats / end_beats: playback region, in beats from the file start.
    - start_frames / end_frames: the same, in raw sample frames.
    - gain: 0.0-1.0 (0.4 = 0.0 dB).
    - warping: True to lock the sample to the Set's tempo.
    - warp_mode: beats, tones, texture, repitch, complex, rex, complex_pro
      (or a raw 0-6 index).
    - warp_as_beats: required by action="warp_as"; the sample's length in beats.
    """
    try:
        ableton = get_ableton_connection()
        payload: dict = {
            "track_index": _to_zero_based(track_index, "track_index"),
            "device_index": _to_zero_based(device_index, "device_index"),
        }
        cp = _simpler_chain_path(chain_path)
        if cp is not None:
            payload["chain_path"] = cp
        if action:
            payload["action"] = action
        for key, val in (
            ("start_beats", start_beats),
            ("end_beats", end_beats),
            ("start_frames", start_frames),
            ("end_frames", end_frames),
            ("gain", gain),
            ("warping", warping),
            ("warp_mode", warp_mode),
            ("warp_as_beats", warp_as_beats),
        ):
            if val is not None:
                payload[key] = val
        r = ableton.send_command("manage_simpler_sample", payload)
        changed = r.get("changed", {})
        head = (
            f"'{r.get('device')}' on '{r.get('track')}': "
            + (", ".join(f"{k}={v}" for k, v in changed.items())
               if changed else "nothing changed")
        )
        tail = (
            f"  now: markers {r.get('start_marker')}-{r.get('end_marker')} "
            f"of {r.get('length')} frames | warping {r.get('warping')} "
            f"| warp mode {r.get('warp_mode_name', r.get('warp_mode'))} "
            f"| gain {r.get('gain_display')}"
        )
        return f"{head}\n{tail}"
    except Exception as e:
        logger.error(f"Error managing simpler sample: {str(e)}")
        return f"Error managing simpler sample: {str(e)}"


@mcp.tool()
def manage_simpler_slices(
    ctx: Context,
    track_index: int,
    device_index: int,
    chain_path: str = "",
    action: str = "info",
    at_beats: float | None = None,
    to_beats: float | None = None,
    at_frames: int | None = None,
    to_frames: int | None = None,
    slicing_style: str | None = None,
    beat_division: int | None = None,
    region_count: int | None = None,
    sensitivity: float | None = None,
) -> str:
    """Chop a sample into slices — how a loop becomes a playable instrument.

    The core move: drop a breakbeat or vocal loop into a Simpler, put it in
    Slicing mode (set_simpler_playback playback_mode="slicing",
    pad_slicing=True), then decide where the cuts land. slicing_style="transient"
    lets Live find the hits and sensitivity tunes how many it finds;
    "beat" cuts on a fixed musical grid (beat_division); "region" cuts into
    region_count equal pieces; "manual" freezes the grid so add/remove/move can
    place every slice by hand. Use that manual mode to nudge a lazy snare onto
    the beat, or to delete a cut that landed mid-hat.

    Slices are addressed by POSITION, never by an ordinal — pass at_beats (or
    at_frames) for the slice you mean. Run this with action="info" first to see
    where they currently are; it lists them in both units.

    Parameters:
    - track_index / device_index: 1-based.
    - chain_path: Optional 1-based "chain.device" pairs for a nested Simpler.
    - action: info, add, remove, move, clear, reset. "clear" empties the grid,
      "reset" rebuilds it from the current slicing settings.
    - at_beats / at_frames: position of the slice to add, remove or move.
    - to_beats / to_frames: destination for action="move".
    - slicing_style: transient, beat, region or manual (or 0-3).
    - beat_division: grid step index for "beat" style, 0-10. Live publishes no
      names for these, so it is an index into Simpler's Division menu.
    - region_count: 2-64 equal regions for "region" style.
    - sensitivity: 0.0-1.0 transient detection threshold for "transient" style.
    """
    try:
        ableton = get_ableton_connection()
        payload: dict = {
            "track_index": _to_zero_based(track_index, "track_index"),
            "device_index": _to_zero_based(device_index, "device_index"),
            "action": action,
        }
        cp = _simpler_chain_path(chain_path)
        if cp is not None:
            payload["chain_path"] = cp
        for key, val in (
            ("at_beats", at_beats),
            ("to_beats", to_beats),
            ("at_frames", at_frames),
            ("to_frames", to_frames),
            ("slicing_style", slicing_style),
            ("beat_division", beat_division),
            ("region_count", region_count),
            ("sensitivity", sensitivity),
        ):
            if val is not None:
                payload[key] = val
        r = ableton.send_command("manage_simpler_slices", payload)
        changed = r.get("changed", {})
        lines = [
            f"'{r.get('device')}' on '{r.get('track')}': "
            + (", ".join(f"{k}={v}" for k, v in changed.items())
               if changed else "read only"),
            f"  playback_mode "
            f"{r.get('playback_mode_name', r.get('playback_mode'))} "
            f"| pad_slicing {r.get('pad_slicing')} "
            f"| style {r.get('slicing_style_name', r.get('slicing_style'))} "
            f"| division {r.get('slicing_beat_division')} | regions "
            f"{r.get('slicing_region_count')} | sensitivity "
            f"{r.get('slicing_sensitivity')}",
            f"  {r.get('slice_count', 0)} slices",
        ]
        if r.get("slices_frames"):
            lines.append(f"    frames: {r['slices_frames']}")
            lines.append(f"    beats:  {r.get('slices_beats')}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error managing simpler slices: {str(e)}")
        return f"Error managing simpler slices: {str(e)}"

@mcp.tool()
def manage_take_lanes(
    ctx: Context,
    track_index: int,
    action: str = "info",
    lane_index: int | None = None,
    name: str | None = None,
    include_in_playback: bool | None = None,
) -> str:
    """Inspect, create and comp the take lanes on a track.

    Reach for this while tracking a singer: each pass lands in its own take
    lane, and afterwards you audition them by including one lane in playback
    at a time ("use_take") instead of muting clips by hand. Naming lanes
    ("verse - close mic", "take 3 - the good one") is what makes a comping
    session survive a break.

    Parameters:
    - track_index: 1-based track being recorded.
    - action: info, create, rename, update, use_take.
    - lane_index: 1-based take lane, for rename / update / use_take.
    - name: New lane name (also names a lane created with 'create').
    - include_in_playback: For 'update' — include or exclude just this lane.
    """
    try:
        ableton = get_ableton_connection()
        payload: dict = {
            "track_index": _to_zero_based(track_index, "track_index"),
            "action": action,
        }
        if lane_index is not None:
            payload["lane_index"] = _to_zero_based(lane_index, "lane_index")
        if name is not None:
            payload["name"] = name
        if include_in_playback is not None:
            payload["include_in_playback"] = include_in_playback
        r = ableton.send_command("manage_take_lanes", payload)
        lanes = r.get("take_lanes") or []
        if not lanes:
            return (
                f"Track '{r.get('track_name')}' has no take lanes yet. "
                "Record over an armed arrangement track, or use action='create'."
            )
        lines = [f"Take lanes on '{r.get('track_name')}' (action: {r.get('action')}):"]
        for lane in lanes:
            mark = "*" if lane.get("is_included_in_playback") else " "
            lines.append(
                f" {mark} {lane.get('index', 0) + 1}. {lane.get('name')} "
                f"— {lane.get('clip_count', 0)} clip(s)"
            )
        lines.append("(* = included in playback)")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error managing take lanes: {str(e)}")
        return f"Error managing take lanes: {str(e)}"


@mcp.tool()
def edit_cue_point(
    ctx: Context,
    cue_index: int | None = None,
    name: str | None = None,
    new_name: str | None = None,
    bar: int | None = None,
    beat: float = 0.0,
    jump: bool = False,
) -> str:
    """Rename, move or jump to an existing locator.

    Locators are the map of an arrangement — "intro", "drop", "breakdown".
    Use this when the sections have shifted and the labels no longer match,
    or to park playback on a section before rehearsing it. Identify the
    locator by its list position or by its current name. Complements
    create_cue_point / delete_cue_point / jump_to_cue_point, which cannot
    rename or move an existing locator.

    Parameters:
    - cue_index: 1-based position in the locator list.
    - name: Current locator name, as an alternative to cue_index.
    - new_name: Rename it to this.
    - bar / beat: Move it here (1-based bar). Live often makes locator time
      read-only — if so, delete and re-create instead.
    - jump: Also move playback to this locator.
    """
    try:
        ableton = get_ableton_connection()
        payload: dict = {"jump": jump}
        if cue_index is not None:
            payload["cue_index"] = _to_zero_based(cue_index, "cue_index")
        if name is not None:
            payload["name"] = name
        if new_name is not None:
            payload["new_name"] = new_name
        if bar is not None:
            payload["time"] = _convert_bar_to_beat(bar, beat)
        r = ableton.send_command("edit_cue_point", payload)
        return "\n".join(f"{k}: {v}" for k, v in r.items())
    except Exception as e:
        logger.error(f"Error editing cue point: {str(e)}")
        return f"Error editing cue point: {str(e)}"


@mcp.tool()
def manage_groove_pool(
    ctx: Context,
    groove: str | None = None,
    base: str | None = None,
    quantization_amount: float | None = None,
    timing_amount: float | None = None,
    random_amount: float | None = None,
    velocity_amount: float | None = None,
    global_amount: float | None = None,
    new_name: str | None = None,
) -> str:
    """Read the groove pool and edit how a groove actually feels.

    Assigning a groove to a clip is only half of it — the swing lives in
    these four amounts. Timing is how far notes are pulled toward the groove,
    quantize is how hard they are first snapped to the grid, random adds
    human scatter, velocity transfers the groove's accents. Turn timing down
    to about 50% when a borrowed MPC groove drags the whole track. Call with
    no arguments for a full read-out of the pool.

    Parameters:
    - groove: Groove name (or its 1-based pool position as a string).
    - base: Grid the groove is measured against, e.g. "eighth", "sixteenth".
      Only settable when this Live build reports base names — see
      base_settable in the read-out.
    - quantization_amount / timing_amount / random_amount / velocity_amount:
      0.0-1.0 (velocity may be bipolar). Omitted = unchanged.
    - global_amount: Set's master Groove Amount, 0.0-1.0.
    - new_name: Rename the groove.
    """
    try:
        ableton = get_ableton_connection()
        payload: dict = {}
        if groove is not None:
            text = str(groove).strip()
            payload["groove"] = (
                _to_zero_based(int(text), "groove") if text.isdigit() else groove
            )
        for key, value in (
            ("base", base),
            ("quantization_amount", quantization_amount),
            ("timing_amount", timing_amount),
            ("random_amount", random_amount),
            ("velocity_amount", velocity_amount),
            ("global_amount", global_amount),
            ("new_name", new_name),
        ):
            if value is not None:
                payload[key] = value
        r = ableton.send_command("manage_groove_pool", payload)
        if not r.get("supported", True):
            return "This Live build does not expose the groove pool."

        lines = [f"Global groove amount: {r.get('groove_amount')}"]
        if r.get("changed"):
            lines.append(
                "Changed: "
                + ", ".join(f"{k}={v}" for k, v in r["changed"].items())
            )
        entries = r.get("grooves")
        if entries is None:
            entries = [r.get("groove")] if r.get("groove") else []
        if not entries:
            return "\n".join(lines + ["Groove pool is empty."])
        for g in entries:
            lines.append(
                f"  {g.get('index', 0) + 1}. {g.get('name')} — base {g.get('base')}, "
                f"quantize {g.get('quantization_amount')}, "
                f"timing {g.get('timing_amount')}, "
                f"random {g.get('random_amount')}"
                + (
                    f", velocity {g['velocity_amount']}"
                    if g.get("velocity_amount") is not None
                    else ""
                )
            )
        first = entries[0] or {}
        options = first.get("base_options") or []
        if options:
            lines.append("Base options: " + ", ".join(options))
        elif first.get("base_settable") is False:
            lines.append(
                "This Live build does not report groove base names, so 'base' "
                "cannot be set from here."
            )
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error managing groove pool: {str(e)}")
        return f"Error managing groove pool: {str(e)}"


@mcp.tool()
def get_application_info(ctx: Context) -> str:
    """Report Live's version, its main views, and any modal dialog that is open.

    Check this first when automation mysteriously stops responding: a "Save
    changes?" or missing-file dialog blocks everything until it is dismissed.
    Also the honest way to know which features exist before using them, since
    take lanes, tuning systems and Live 12 devices are all version-gated.
    """
    try:
        ableton = get_ableton_connection()
        r = ableton.send_command("get_application_info")
        lines = [f"Ableton Live {r.get('version', 'unknown')}"]
        views = r.get("main_views") or []
        if views:
            lines.append(
                "Main views: "
                + ", ".join(
                    f"{v['name']}{'*' if v.get('visible') else ''}" for v in views
                )
                + "  (* = visible)"
            )
        if r.get("focused_document_view"):
            lines.append(f"Focused view: {r['focused_document_view']}")
        dialog = r.get("dialog") or {}
        if dialog.get("open"):
            lines.append(
                f"DIALOG OPEN — \"{dialog.get('current_dialog_message')}\" with "
                f"{dialog.get('current_dialog_button_count')} button(s). "
                + (
                    "Dismiss it with dismiss_live_dialog before automating further."
                    if dialog.get("can_press", True)
                    else "This build cannot press it remotely — dismiss it by hand."
                )
            )
        else:
            lines.append("No modal dialog open.")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error getting application info: {str(e)}")
        return f"Error getting application info: {str(e)}"


@mcp.tool()
def dismiss_live_dialog(ctx: Context, button_index: int = 1) -> str:
    """Press a button on Live's current modal dialog to unblock automation.

    Live puts up modal dialogs for missing files, plugin scans and unsaved
    changes, and while one is up every other command silently does nothing.
    Run get_application_info first to read the message and the button count,
    then press the button you want.

    Parameters:
    - button_index: 1-based button on the dialog. 1 is usually the default
      (OK / Save); the last one is usually Cancel.
    """
    try:
        ableton = get_ableton_connection()
        r = ableton.send_command(
            "press_dialog_button",
            {"button_index": _to_zero_based(button_index, "button_index")},
        )
        if r.get("pressed") is None:
            return "No dialog is open — nothing to dismiss."
        remaining = (
            " Another dialog is still open."
            if r.get("dialog_open")
            else " No dialogs remain."
        )
        return f"Dismissed: \"{r.get('dismissed_message')}\".{remaining}"
    except Exception as e:
        logger.error(f"Error dismissing dialog: {str(e)}")
        return f"Error dismissing dialog: {str(e)}"


@mcp.tool()
def control_live_view(
    ctx: Context,
    action: str = "show",
    view_name: str = "Session",
    direction: str = "down",
    modifier: bool = False,
) -> str:
    """Show, hide, focus, toggle, scroll or zoom any of Live's main views.

    Use this to set the screen up before a take — hide the Browser and
    Detail panes so the Session grid fills the display, or focus the Detail
    view so the right device is under your hands. Complements
    set_ableton_view (which only switches) by adding hide, focus and toggle.

    Parameters:
    - action: show, hide, focus, toggle, scroll, zoom.
    - view_name: Browser, Arranger, Session, Detail, Detail/Clip,
      Detail/DeviceChain. Run get_application_info for this build's exact list.
    - direction: up, down, left, right — for scroll and zoom.
    - modifier: Pass the modifier key with a scroll/zoom (larger step).
    """
    try:
        ableton = get_ableton_connection()
        r = ableton.send_command(
            "control_live_view",
            {
                "action": action,
                "view_name": view_name,
                "direction": direction,
                "modifier": modifier,
            },
        )
        return "\n".join(f"{k}: {v}" for k, v in r.items())
    except Exception as e:
        logger.error(f"Error controlling Live view: {str(e)}")
        return f"Error controlling Live view: {str(e)}"


@mcp.tool()
def manage_tuning_system(
    ctx: Context, action: str = "info", name: str | None = None
) -> str:
    """Report, load or clear the Set's tuning system (microtuning).

    Live 12 can retune the whole Set from an .ascl/.scl file — just
    intonation, maqam, gamelan, historical temperaments. Reach for this when
    a piece should not be in 12-tone equal temperament, or to confirm which
    tuning a Set was written in before editing its MIDI. Live types this as
    an object, so a custom tuning is applied by loading it from the browser
    rather than by name assignment; 'clear' returns the Set to 12-TET.

    Parameters:
    - action: info, load, clear.
    - name: Tuning to load, matched against the browser's Tunings folder.
    """
    try:
        ableton = get_ableton_connection()
        payload: dict = {"action": action}
        if name is not None:
            payload["name"] = name
        r = ableton.send_command("manage_tuning_system", payload)
        if not r.get("supported", True):
            return f"Tuning systems unavailable: {r.get('message')}"
        current = r.get("current") or {}
        lines = [f"Current tuning: {current.get('name')}"]
        available = r.get("available") or []
        lines.append(
            "Browser tunings: " + (", ".join(available) if available else
                                   "(none — add .ascl/.scl files to the User Library)")
        )
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error managing tuning system: {str(e)}")
        return f"Error managing tuning system: {str(e)}"


@mcp.tool()
def get_meters(ctx: Context) -> str:
    """Read output level meters for every track and the master.

    Not hearing, but measurement: which track is loudest, whether anything
    is clipping, whether a part is actually audible. Play the Set first —
    meters read near zero when stopped.
    """
    try:
        ableton = get_ableton_connection()
        r = ableton.send_command("get_meters")
        lines = [f"Playing: {r.get('is_playing')}", ""]
        for t in r.get("tracks", []):
            level = t.get("output_meter_level")
            bar = "█" * int(min(1.0, (level or 0)) * 30)
            lines.append(f"{t['index'] + 1:>3}. {t['name'][:20]:<20} {level} {bar}")
        m = r.get("master", {})
        lines.append("")
        lines.append(f"     MASTER               {m.get('output_meter_level')}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error reading meters: {str(e)}")
        return f"Error reading meters: {str(e)}"


@mcp.tool()
def get_modulation_targets(ctx: Context, track_index: int, device_index: int) -> str:
    """List modulation sources and modulatable parameters on a device.

    Wavetable exposes its modulation matrix through the API, so
    envelope-to-filter and LFO-to-pitch can be routed programmatically.
    Most other devices do not — this reports which case applies.

    Parameters:
    - track_index / device_index: 1-based.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        r = ableton.send_command("get_modulation_targets", {
            "track_index": ti, "device_index": di,
        })
        if not r.get("supports_modulation"):
            return f"'{r.get('device')}': {r.get('note')}"
        lines = [f"'{r.get('device')}' modulation matrix:", ""]
        targets = r.get("targets") or []
        if targets:
            lines.append("  current targets: " + ", ".join(targets))
        for name, vals in (r.get("source_lists") or {}).items():
            lines.append(f"  {name}: {', '.join(str(v) for v in vals[:20])}")
        mods = r.get("modulatable_parameters") or []
        if mods:
            lines.append("")
            lines.append(f"  modulatable ({len(mods)}): " + ", ".join(mods[:40]))
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error reading modulation targets: {str(e)}")
        return f"Error reading modulation targets: {str(e)}"


@mcp.tool()
def set_device_modulation(
    ctx: Context,
    track_index: int,
    device_index: int,
    target: str,
    source: str = "",
    value: float = 0.5,
) -> str:
    """Route modulation to a parameter and set its depth (Wavetable etc).

    This is what makes a filter envelope possible without dragging in the
    UI — e.g. target "Filter 1 Freq" with an envelope source for a plucky
    filter sweep.

    Parameters:
    - track_index / device_index: 1-based.
    - target: Parameter name to modulate, e.g. "Filter 1 Freq".
    - source: Modulation source name from get_modulation_targets.
    - value: Depth, -1.0 to 1.0.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        r = ableton.send_command("set_device_modulation", {
            "track_index": ti, "device_index": di,
            "target": target, "source": source, "value": value,
        })
        return (
            f"Modulating '{r.get('parameter')}' on '{r.get('device')}' "
            f"at depth {r.get('depth')} (readback {r.get('readback')})"
        )
    except Exception as e:
        logger.error(f"Error setting modulation: {str(e)}")
        return f"Error setting modulation: {str(e)}"


@mcp.tool()
def move_device(
    ctx: Context,
    track_index: int,
    device_index: int,
    target_track_index: int | None = None,
    position: int = 1,
) -> str:
    """Move a device within a track's chain, or to another track.

    Device order is signal order, so this fixes a chain without deleting and
    reloading — e.g. putting an EQ in front of a compressor so low rumble
    stops triggering gain reduction.

    Parameters:
    - track_index / device_index: 1-based source.
    - target_track_index: 1-based destination track, or omit to stay put.
    - position: 1-based slot to land in.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        payload: dict = {
            "track_index": ti, "device_index": di,
            "position": _to_zero_based(position, "position"),
        }
        if target_track_index is not None:
            payload["target_track_index"] = _to_zero_based(
                target_track_index, "target_track_index")
        r = ableton.send_command("move_device", payload)
        chain = " → ".join(r.get("chain") or [])
        return (
            f"Moved '{r.get('device')}' to '{r.get('to_track')}' "
            f"position {r.get('position', 0) + 1}\n  chain: {chain}"
        )
    except Exception as e:
        logger.error(f"Error moving device: {str(e)}")
        return f"Error moving device: {str(e)}"




@mcp.tool()
def set_song_scale(
    ctx: Context,
    root_note: int | None = None,
    scale_name: str | None = None,
    swing_amount: float | None = None,
    clip_trigger_quantization: int | None = None,
) -> str:
    """Set the Set's key and scale, global swing, and clip launch quantization.

    Live 12 tracks a Set-wide root note and scale that the MIDI editor and
    scale-aware devices follow — worth setting in a template so every new
    clip starts in the right key.

    Parameters:
    - root_note: 0 = C, 1 = C#, ... 9 = A, 11 = B.
    - scale_name: e.g. "Major", "Minor", "Dorian", "Mixolydian".
    - swing_amount: 0.0-1.0 global swing.
    - clip_trigger_quantization: 0 = None, 1 = 8 bars ... typically 4 = 1 bar.
    """
    try:
        ableton = get_ableton_connection()
        payload: dict = {}
        for key, val in (
            ("root_note", root_note), ("scale_name", scale_name),
            ("swing_amount", swing_amount),
            ("clip_trigger_quantization", clip_trigger_quantization),
        ):
            if val is not None:
                payload[key] = val
        if not payload:
            return "No scale changes requested"
        r = ableton.send_command("set_song_scale", payload)
        changed = r.get("changed", {})
        return "Song: " + ", ".join(f"{k}={v}" for k, v in changed.items())
    except Exception as e:
        logger.error(f"Error setting song scale: {str(e)}")
        return f"Error setting song scale: {str(e)}"


@mcp.tool()
def add_notes_extended(
    ctx: Context,
    track_index: int,
    clip_index: int,
    notes: list,
    replace: bool = False,
    arrangement: bool = False,
    clip_name: str | None = None,
) -> str:
    """Add MIDI notes with per-note probability and velocity deviation.

    The standard note API cannot express probability, which is what makes
    programmed parts breathe — a hat that lands 80% of the time instead of
    identically every loop.

    Additive by default: existing notes survive unless replace=True.

    Parameters:
    - track_index / clip_index: 1-based.
    - notes: list of {"pitch", "start_time", "duration", "velocity",
      "mute", "probability" (0.0-1.0), "velocity_deviation",
      "release_velocity"}.
    - replace: Clear existing notes first.
    - arrangement / clip_name: Write into an ARRANGEMENT clip instead of a
      session clip. `start_time` stays clip-relative, not absolute timeline
      position. This is how a single section is varied in place, rather than
      re-placing a session clip over it.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        ci = _to_zero_based(clip_index, "clip_index")
        payload: dict = {
            "track_index": ti, "clip_index": ci,
            "notes": notes, "replace": replace,
            "arrangement": arrangement,
        }
        if clip_name is not None:
            payload["clip_name"] = clip_name
        r = ableton.send_command("add_notes_extended", payload)
        where = "arrangement" if r.get("arrangement") else "session"
        return (
            f"Added {r.get('notes_added')} notes to '{r.get('clip_name')}' "
            f"({where})"
            + (" (replaced existing)" if r.get("replaced") else "")
        )
    except Exception as e:
        logger.error(f"Error adding extended notes: {str(e)}")
        return f"Error adding extended notes: {str(e)}"


@mcp.tool()
def inspect_lom(
    ctx: Context,
    target: str = "song",
    track_index: int | None = None,
    clip_index: int | None = None,
    device_index: int | None = None,
    scene_index: int | None = None,
    filter: str = "",
) -> str:
    """Report the real API surface of a live object in THIS Live version.

    The Live Object Model changes between versions and the published
    documentation is incomplete, so the running instance is the only
    authoritative source. Use this instead of guessing at property names —
    e.g. inspect_lom(target="clip", track_index=1, clip_index=1,
    filter="follow") shows exactly which follow-action properties exist.

    Parameters:
    - target: song, track, clip, clip_slot, device, scene, mixer, master, view.
    - track_index / clip_index / device_index / scene_index: 1-based, as needed.
    - filter: only show names containing this substring.
    """
    try:
        ableton = get_ableton_connection()
        payload: dict = {"target": target, "filter": filter}
        for key, val in (
            ("track_index", track_index), ("clip_index", clip_index),
            ("device_index", device_index), ("scene_index", scene_index),
        ):
            if val is not None:
                payload[key] = _to_zero_based(val, key)
        r = ableton.send_command("inspect_lom", payload)
        lines = [f"{r.get('target')} — Live API surface", ""]
        props = r.get("properties") or []
        if props:
            lines.append("Properties:")
            for p in props:
                lines.append(f"  {p['name']} = {p['value']}")
        methods = r.get("methods") or []
        if methods:
            lines.append("")
            lines.append("Methods: " + ", ".join(methods))
        if not props and not methods:
            lines.append("(nothing matched)")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error inspecting LOM: {str(e)}")
        return f"Error inspecting LOM: {str(e)}"


@mcp.tool()
def get_clip_automation(ctx: Context, track_index: int, clip_index: int) -> str:
    """List which parameters already have automation envelopes on a clip.

    Parameters:
    - track_index / clip_index: 1-based track and clip slot.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        ci = _to_zero_based(clip_index, "clip_index")
        r = ableton.send_command("get_clip_automation", {
            "track_index": ti, "clip_index": ci,
        })
        automated = r.get("automated") or []
        if not automated:
            return f"Clip '{r.get('clip_name')}' has no automation envelopes"
        listing = "\n".join(
            f"  {a['device']} → {a['parameter']}" for a in automated
        )
        return f"Clip '{r.get('clip_name')}' automates:\n{listing}"
    except Exception as e:
        logger.error(f"Error reading clip automation: {str(e)}")
        return f"Error reading clip automation: {str(e)}"


@mcp.tool()
def write_arrangement_automation(
    ctx: Context,
    track_index: int,
    parameter_name: str,
    points: list,
    device_index: int | None = None,
    clear_first: bool = True,
    from_time: float | None = None,
    to_time: float | None = None,
) -> str:
    """Write an automation envelope into every arrangement clip on a track.

    Clip envelopes are NOT carried when a session clip is copied into the
    arrangement — the copy arrives with its automation stripped. So
    automation written to a session clip never reaches the arrangement, and
    this is the tool that puts it there.

    Point times are relative to each clip's own start, so one shape is
    stamped onto every clip in the range — e.g. a filter opening across each
    4-bar block.

    Parameters:
    - track_index: Track number (1-based).
    - parameter_name: "Volume", "Pan", a send name (e.g. "C-DELAY"), or a
      device parameter such as "Filter Freq".
    - points: list of {"time": beats, "value": 0.0-1.0, "length": beats},
      relative to each clip's start.
    - device_index: Restrict the parameter search to one device (1-based).
    - clear_first: Wipe each clip's existing envelope first.
    - from_time / to_time: Limit to clips starting within this beat range,
      so automation can be applied to one section only.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        payload: dict = {
            "track_index": ti, "parameter_name": parameter_name,
            "points": points, "clear_first": clear_first,
        }
        if device_index is not None:
            payload["device_index"] = _to_zero_based(device_index, "device_index")
        for key, val in (("from_time", from_time), ("to_time", to_time)):
            if val is not None:
                payload[key] = val
        r = ableton.send_command("write_arrangement_automation", payload)
        return (
            f"Wrote {r.get('points_per_clip')} points to '{r.get('parameter')}' "
            f"on {r.get('clips_written')} arrangement clips of "
            f"'{r.get('track')}' ({r.get('clips_skipped')} skipped)"
        )
    except Exception as e:
        logger.error(f"Error writing arrangement automation: {str(e)}")
        return f"Error writing arrangement automation: {str(e)}"


@mcp.tool()
def record_arrangement_automation(
    ctx: Context,
    track_index: int,
    parameter_name: str,
    points: list,
    device_index: int | None = None,
    return_to_start: bool = True,
) -> str:
    """Write REAL arrangement automation, by recording it off the transport.

    This is how automation actually reaches the arrangement. Clip envelopes
    cannot: Live refuses them on arrangement clips, and copying a session
    clip into the arrangement strips them. But that is a limit on CLIP
    envelopes — arrangement automation is TRACK automation, and Live writes
    it the same way it does for a hardware fader: arm arrangement record,
    roll the transport, move the parameter. That is exactly what this does,
    and Live treats the result as its own automation.

    Runs in real time, so a 16-bar sweep takes 16 bars of wall clock. The
    call returns immediately — poll `get_automation_record_status` until
    status is "done". Sampling is one point per Live tick (~100 ms), and
    values are interpolated against the true playhead, so ramps come out
    smooth and cannot drift out of time.

    Overwrites existing automation for that parameter across the recorded
    range, exactly as a real record pass would.

    Parameters:
    - track_index: Track number (1-based).
    - parameter_name: "Volume", "Pan", a send name, or a device parameter
      such as "Filter Freq".
    - points: list of {"time": <absolute beat>, "value": 0.0-1.0}. At least
      two — recording captures movement, so one value has nothing to record.
      Times are ABSOLUTE arrangement beats, not clip-relative.
    - device_index: Restrict the parameter search to one device (1-based).
    - return_to_start: Put the arrangement start marker back afterwards.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        payload: dict = {
            "track_index": ti, "parameter_name": parameter_name,
            "points": points, "return_to_start": return_to_start,
        }
        if device_index is not None:
            payload["device_index"] = _to_zero_based(device_index, "device_index")
        r = ableton.send_command("record_arrangement_automation", payload)
        secs = r.get("estimated_seconds")
        secs_txt = f" (~{secs:.1f}s)" if isinstance(secs, (int, float)) else ""
        return (
            f"Recording '{r.get('parameter')}' on '{r.get('track')}' from beat "
            f"{r.get('from_beat')} to {r.get('to_beat')}{secs_txt}. "
            f"Poll get_automation_record_status until status is 'done'."
        )
    except Exception as e:
        logger.error(f"Error recording arrangement automation: {str(e)}")
        return f"Error recording arrangement automation: {str(e)}"


@mcp.tool()
def record_over_range(
    ctx: Context,
    track_index: int,
    from_bar: int,
    to_bar: int,
    arm_track: bool = True,
    return_to_start: bool = True,
) -> str:
    """Record a track's live input into the arrangement over a bar range.

    Punches a take without hand-timing the record button: arms the track,
    arms arrangement record, rolls from `from_bar` and stops at `to_bar`.
    Runs in real time, so recording bars 33-49 takes 16 bars of wall clock.
    Returns immediately — poll `get_automation_record_status`.

    Whatever the track monitors is what lands. If the take comes back silent,
    check input routing with `get_routing_options` first.

    The arm state of every track is captured and restored afterwards, because
    arming one track makes Live's exclusive arm silently disarm the others.

    Parameters:
    - track_index: Track number (1-based). Must be armable — group and return
      tracks have no input.
    - from_bar / to_bar: Bar range (1-based, to_bar exclusive).
    - arm_track: Arm the track first. Set False if it is already armed and
      you want its current state left alone.
    - return_to_start: Put the arrangement start marker back afterwards.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        num, denom = _get_time_signature()
        r = ableton.send_command("record_over_range", {
            "track_index": ti,
            "from_beat": bar_to_beat(from_bar, num, denom),
            "to_beat": bar_to_beat(to_bar, num, denom),
            "arm_track": arm_track,
            "return_to_start": return_to_start,
        })
        secs = r.get("estimated_seconds")
        secs_txt = f" (~{secs:.1f}s)" if isinstance(secs, (int, float)) else ""
        return (
            f"Recording input on '{r.get('track')}' over bars "
            f"{from_bar}-{to_bar - 1}{secs_txt}. "
            f"Poll get_automation_record_status until status is 'done'."
        )
    except Exception as e:
        logger.error(f"Error recording over range: {str(e)}")
        return f"Error recording over range: {str(e)}"


@mcp.tool()
def bounce_to_audio(
    ctx: Context,
    from_bar: int,
    to_bar: int,
    source: str = "Resampling",
    name: str | None = None,
) -> str:
    """Render a bar range to an audio file, by resampling it in real time.

    Live exposes no render/export call — but an audio track accepts
    "Resampling" (the main bus) or any individual track as its INPUT, so
    arming one and rolling the transport writes a real audio file into the
    project's Samples/Recorded folder. Verified: bars 33-35 produced a 48 kHz
    stereo AIFF peaking at -8.5 dBFS.

    This covers three things the API supposedly cannot do:
    - `source="Resampling"` — bounce the full mix (export/mixdown)
    - `source="<track name>"` — bounce one track (stem export)
    - the same, then disable the original — a freeze/flatten stand-in

    Real time: bouncing 32 bars takes 32 bars. Returns immediately; poll
    `get_automation_record_status`, which reports `file_path` once done.

    Creates a new audio track to record onto and leaves it in place, so the
    result is audible and editable. Delete it when finished with it.

    Parameters:
    - from_bar / to_bar: Bar range (1-based, to_bar exclusive).
    - source: "Resampling" for the full mix, or a track name for a stem.
      Matched against Live's actual routing list — call get_routing_options
      on any audio track to see what this system offers.
    - name: Optional name for the new audio track.
    """
    try:
        ableton = get_ableton_connection()
        num, denom = _get_time_signature()
        payload: dict = {
            "from_beat": bar_to_beat(from_bar, num, denom),
            "to_beat": bar_to_beat(to_bar, num, denom),
            "source": source,
        }
        if name:
            payload["name"] = name
        r = ableton.send_command("bounce_to_audio", payload)
        secs = r.get("estimated_seconds")
        secs_txt = f" (~{secs:.1f}s)" if isinstance(secs, (int, float)) else ""
        return (
            f"Bouncing '{r.get('source')}' over bars {from_bar}-{to_bar - 1}"
            f"{secs_txt} onto new track '{r.get('bounce_track')}'. "
            f"Poll get_automation_record_status for status and file_path."
        )
    except Exception as e:
        logger.error(f"Error bouncing to audio: {str(e)}")
        return f"Error bouncing to audio: {str(e)}"


@mcp.tool()
def get_automation_record_status(ctx: Context) -> str:
    """Progress of an in-flight `record_arrangement_automation` pass.

    Status is one of: idle, recording, done, cancelled, failed.
    """
    try:
        ableton = get_ableton_connection()
        r = ableton.send_command("get_automation_record_status", {})
        status = r.get("status", "idle")
        if status == "idle":
            return "No automation recording has run this session."
        head = (
            f"{status.upper()} — '{r.get('parameter')}' on '{r.get('track')}' "
            f"(beats {r.get('from_beat')}-{r.get('to_beat')})"
        )
        pos = r.get("position")
        if isinstance(pos, (int, float)):
            head += f"\nPlayhead at beat {pos:.2f}, {r.get('samples')} points written"
        if r.get("file_path"):
            head += f"\nRecorded file: {r.get('file_path')}"
        return head
    except Exception as e:
        logger.error(f"Error reading automation record status: {str(e)}")
        return f"Error reading automation record status: {str(e)}"


@mcp.tool()
def cancel_automation_record(ctx: Context) -> str:
    """Abort an in-flight automation record pass and disarm the transport.

    Whatever was already recorded stays — undo it in Live if unwanted.
    """
    try:
        ableton = get_ableton_connection()
        r = ableton.send_command("cancel_automation_record", {})
        if not r.get("cancelled"):
            return f"Nothing to cancel: {r.get('reason')}"
        return (
            f"Cancelled recording '{r.get('parameter')}' on "
            f"'{r.get('track')}' at beat {r.get('stopped_at_beat')}"
        )
    except Exception as e:
        logger.error(f"Error cancelling automation record: {str(e)}")
        return f"Error cancelling automation record: {str(e)}"


@mcp.tool()
def write_clip_automation(
    ctx: Context,
    track_index: int,
    clip_index: int,
    parameter_name: str,
    points: list,
    device_index: int | None = None,
    clear_first: bool = True,
) -> str:
    """Write real automation points into a clip envelope.

    This actually draws automation, rather than only creating or clearing an
    empty envelope.

    Parameters:
    - track_index / clip_index: 1-based track and clip slot.
    - parameter_name: "Volume", "Pan", a send name, or a device parameter
      such as "Filter Freq". Matched exactly first, then by substring.
    - points: list of {"time": beats, "value": 0.0-1.0, "length": beats}.
      Value is normalized and mapped onto the parameter's own range. Steps
      are flat, so approximate a ramp with several short steps — e.g. a
      4-beat filter sweep as 16 steps of length 0.25 with rising values.
    - device_index: restrict the search to one device (1-based).
    - clear_first: wipe the existing envelope before writing.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        ci = _to_zero_based(clip_index, "clip_index")
        payload: dict = {
            "track_index": ti, "clip_index": ci,
            "parameter_name": parameter_name, "points": points,
            "clear_first": clear_first,
        }
        if device_index is not None:
            payload["device_index"] = _to_zero_based(device_index, "device_index")
        r = ableton.send_command("write_clip_automation", payload)
        return (
            f"Wrote {r.get('points_written')} automation points to "
            f"'{r.get('parameter')}' ({r.get('device')}) on clip "
            f"'{r.get('clip_name')}'"
        )
    except Exception as e:
        logger.error(f"Error writing clip automation: {str(e)}")
        return f"Error writing clip automation: {str(e)}"


@mcp.tool()
def set_clip_launch(
    ctx: Context,
    track_index: int,
    clip_index: int,
    launch_mode: int | None = None,
    launch_quantization: int | None = None,
    legato: bool | None = None,
    velocity_amount: float | None = None,
) -> str:
    """Set how a clip launches — trigger/gate/toggle/repeat, quantization, legato.

    Parameters:
    - track_index / clip_index: 1-based.
    - launch_mode: 0 = Trigger, 1 = Gate, 2 = Toggle, 3 = Repeat.
    - launch_quantization: 0 = Global, then Live's quantization list.
    - legato: Keep playback position when switching clips — essential for a
      live set where one song's clips swap without restarting the phrase.
    - velocity_amount: How much MIDI velocity affects clip volume.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        ci = _to_zero_based(clip_index, "clip_index")
        payload: dict = {"track_index": ti, "clip_index": ci}
        for key, val in (
            ("launch_mode", launch_mode),
            ("launch_quantization", launch_quantization),
            ("legato", legato), ("velocity_amount", velocity_amount),
        ):
            if val is not None:
                payload[key] = val
        r = ableton.send_command("set_clip_launch", payload)
        changed = r.get("changed", {})
        if not changed:
            return f"No launch changes requested for '{r.get('clip_name')}'"
        detail = ", ".join(f"{k}={v}" for k, v in changed.items())
        return f"Set '{r.get('clip_name')}' launch: {detail}"
    except Exception as e:
        logger.error(f"Error setting clip launch: {str(e)}")
        return f"Error setting clip launch: {str(e)}"


@mcp.tool()
def set_clip_follow_action(
    ctx: Context, track_index: int, clip_index: int, settings: dict | None = None
) -> str:
    """Read or set a clip's follow actions.

    Follow actions were reworked in Live 12, so this adapts to whatever the
    running version exposes rather than assuming property names. Call it
    with no settings first to see the available properties and their current
    values, then set those names.

    Parameters:
    - track_index / clip_index: 1-based.
    - settings: dict of follow-action property names to values, e.g.
      {"follow_action_time": 4.0, "follow_action_enabled": True}. Omit to
      just read.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        ci = _to_zero_based(clip_index, "clip_index")
        r = ableton.send_command("set_clip_follow_action", {
            "track_index": ti, "clip_index": ci, "settings": settings or {},
        })
        lines = [f"Clip '{r.get('clip_name')}' follow actions:"]
        available = r.get("available") or []
        lines.append("  available: " + (", ".join(available) or "none"))
        current = r.get("current") or {}
        if current:
            lines.append("  current: " + ", ".join(
                f"{k}={v}" for k, v in current.items()))
        changed = r.get("changed") or {}
        if changed:
            lines.append("  changed: " + ", ".join(
                f"{k}={v}" for k, v in changed.items()))
        unsupported = r.get("unsupported") or []
        if unsupported:
            lines.append("  NOT supported: " + ", ".join(str(u) for u in unsupported))
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error setting follow action: {str(e)}")
        return f"Error setting follow action: {str(e)}"


@mcp.tool()
def manage_warp_markers(
    ctx: Context,
    track_index: int,
    clip_index: int,
    action: str = "list",
    beat_time: float | None = None,
    sample_time: float | None = None,
    warp_mode: int | None = None,
    warping: bool | None = None,
) -> str:
    """List, add or remove warp markers on an audio clip, and set warp mode.

    Parameters:
    - track_index / clip_index: 1-based.
    - action: "list", "add", "remove", or "set" (for warp_mode/warping only).
    - beat_time: Position in beats — required for add and remove.
    - sample_time: Optional source position for an added marker.
    - warp_mode: 0 Beats, 1 Tones, 2 Texture, 3 Re-Pitch, 4 Complex, 6 Complex Pro.
    - warping: Turn warping on or off for the clip.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        ci = _to_zero_based(clip_index, "clip_index")
        payload: dict = {"track_index": ti, "clip_index": ci, "action": action}
        for key, val in (
            ("beat_time", beat_time), ("sample_time", sample_time),
            ("warp_mode", warp_mode), ("warping", warping),
        ):
            if val is not None:
                payload[key] = val
        r = ableton.send_command("manage_warp_markers", payload)
        lines = [f"Clip '{r.get('clip_name')}': {r.get('marker_count')} warp markers"]
        for key in ("warping", "warp_mode", "added_at_beat", "removed_at_beat"):
            if key in r:
                lines.append(f"  {key} = {r[key]}")
        markers = r.get("warp_markers") or []
        if action == "list" and markers:
            preview = ", ".join(
                f"{m['beat_time']:.2f}" for m in markers[:16]
            )
            lines.append(f"  beats: {preview}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error managing warp markers: {str(e)}")
        return f"Error managing warp markers: {str(e)}"


@mcp.tool()
def get_routing_options(ctx: Context, track_index: int) -> str:
    """List the input/output routing choices available on a track.

    Available routings depend on the audio interface and on which other
    tracks exist, so always call this before set_track_routing rather than
    guessing at names.

    Parameters:
    - track_index: Track number (1-based).
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        r = ableton.send_command("get_routing_options", {"track_index": ti})
        lines = [f"Routing for '{r.get('track_name')}':", ""]
        lines.append(f"  input type:     {r.get('current_input_type')}")
        lines.append(f"  input channel:  {r.get('current_input_channel')}")
        lines.append(f"  output type:    {r.get('current_output_type')}")
        lines.append(f"  output channel: {r.get('current_output_channel')}")
        lines.append(f"  monitoring:     {r.get('monitoring_state')}")
        for label, key in (
            ("available input types", "available_input_types"),
            ("available input channels", "available_input_channels"),
            ("available output types", "available_output_types"),
            ("available output channels", "available_output_channels"),
        ):
            vals = r.get(key) or []
            if vals:
                lines.append(f"\n  {label}: {', '.join(vals)}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error getting routing options: {str(e)}")
        return f"Error getting routing options: {str(e)}"


@mcp.tool()
def set_track_routing(
    ctx: Context,
    track_index: int,
    input_type: str | None = None,
    input_channel: str | None = None,
    output_type: str | None = None,
    output_channel: str | None = None,
) -> str:
    """Set a track's input and output routing by display name.

    Names match case-insensitively; a unique substring is enough. Use this
    for resampling, feeding one track from another, sending a track to a
    specific interface output, or routing an external instrument.

    Parameters:
    - track_index: Track number (1-based).
    - input_type / input_channel: e.g. "Ext. In" and "1/2", or another track's name.
    - output_type / output_channel: e.g. "Master", or an interface output pair.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        payload: dict = {"track_index": ti}
        for key, val in (
            ("input_type", input_type), ("input_channel", input_channel),
            ("output_type", output_type), ("output_channel", output_channel),
        ):
            if val is not None:
                payload[key] = val
        r = ableton.send_command("set_track_routing", payload)
        changed = r.get("changed", {})
        if not changed:
            return f"No routing changes requested for '{r.get('track_name')}'"
        detail = ", ".join(f"{k}={v}" for k, v in changed.items())
        return f"Routed '{r.get('track_name')}': {detail}"
    except Exception as e:
        logger.error(f"Error setting track routing: {str(e)}")
        return f"Error setting track routing: {str(e)}"


@mcp.tool()
def set_track_monitoring(ctx: Context, track_index: int, state: str) -> str:
    """Set input monitoring on a track.

    Parameters:
    - track_index: Track number (1-based).
    - state: "in", "auto" or "off". Use "in" to hear a live input at all
      times, "auto" to hear it only while armed, "off" for playback only.
    """
    try:
        mapping = {"in": 0, "auto": 1, "off": 2}
        key = str(state).strip().lower()
        if key not in mapping:
            return f"Invalid state '{state}'. Use 'in', 'auto' or 'off'."
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        r = ableton.send_command("set_track_monitoring", {
            "track_index": ti, "state": mapping[key],
        })
        return f"Set '{r.get('track_name')}' monitoring to {r.get('monitoring_state')}"
    except Exception as e:
        logger.error(f"Error setting monitoring: {str(e)}")
        return f"Error setting monitoring: {str(e)}"


@mcp.tool()
def set_crossfade_assign(ctx: Context, track_index: int, assign: str) -> str:
    """Assign a track to a side of the crossfader.

    Parameters:
    - track_index: Track number (1-based).
    - assign: "a", "b" or "none".
    """
    try:
        mapping = {"a": 0, "none": 1, "b": 2}
        key = str(assign).strip().lower()
        if key not in mapping:
            return f"Invalid assign '{assign}'. Use 'a', 'b' or 'none'."
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        r = ableton.send_command("set_crossfade_assign", {
            "track_index": ti, "assign": mapping[key],
        })
        return f"Assigned '{r.get('track_name')}' to crossfader {r.get('crossfade_assign')}"
    except Exception as e:
        logger.error(f"Error setting crossfade assign: {str(e)}")
        return f"Error setting crossfade assign: {str(e)}"


@mcp.tool()
def set_crossfader(ctx: Context, value: float) -> str:
    """Set the crossfader position.

    Parameters:
    - value: 0.0 = full A, 0.5 = centre, 1.0 = full B.
    """
    try:
        ableton = get_ableton_connection()
        r = ableton.send_command("set_crossfader", {"value": value})
        return f"Crossfader set to {r.get('display_value')}"
    except Exception as e:
        logger.error(f"Error setting crossfader: {str(e)}")
        return f"Error setting crossfader: {str(e)}"


@mcp.tool()
def set_master_volume(ctx: Context, volume: float) -> str:
    """Set the master track fader.

    Parameters:
    - volume: Normalized 0.0-1.0. 0.85 is unity gain (0 dB).
    """
    try:
        ableton = get_ableton_connection()
        r = ableton.send_command("set_master_volume", {"volume": volume})
        return f"Master volume set to {r.get('display_value')}"
    except Exception as e:
        logger.error(f"Error setting master volume: {str(e)}")
        return f"Error setting master volume: {str(e)}"


@mcp.tool()
def load_sample_to_drum_pad(
    ctx: Context, track_index: int, pad_note: int, uri: str
) -> str:
    """Load a sample or device onto a single pad of a drum rack.

    This is how a custom kit gets built — swapping individual pads rather
    than loading a whole preset kit. Find URIs with get_browser_items_at_path.

    Parameters:
    - track_index: Track with the drum rack (1-based).
    - pad_note: MIDI note of the pad. 36 = C1 kick, 38 = snare, 42 = closed
      hat, 46 = open hat.
    - uri: Browser URI of the sample or device to load.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        r = ableton.send_command("load_sample_to_drum_pad", {
            "track_index": ti, "pad_note": pad_note, "uri": uri,
        })
        return (
            f"Loaded '{r.get('loaded')}' onto pad {r.get('pad_note')} "
            f"('{r.get('pad_name')}') of '{r.get('track_name')}'"
        )
    except Exception as e:
        logger.error(f"Error loading sample to drum pad: {str(e)}")
        return f"Error loading sample to drum pad: {str(e)}"


@mcp.tool()
def get_grooves(ctx: Context) -> str:
    """List grooves in the Set's groove pool, and the global groove amount.

    The groove pool is empty until grooves are dragged in from the browser
    or extracted from a clip, so an empty result is normal in a new Set.
    """
    try:
        ableton = get_ableton_connection()
        r = ableton.send_command("get_grooves")
        grooves = r.get("grooves") or []
        if not grooves:
            return (
                "Groove pool is empty. Drag a groove from the browser, or "
                "extract one from a clip, then it becomes assignable.\n"
                f"Global groove amount: {r.get('groove_amount')}"
            )
        listing = "\n".join(
            f"  {g['index'] + 1}. {g['name']}" for g in grooves
        )
        return (
            f"Groove pool ({len(grooves)}):\n{listing}\n\n"
            f"Global groove amount: {r.get('groove_amount')}"
        )
    except Exception as e:
        logger.error(f"Error getting grooves: {str(e)}")
        return f"Error getting grooves: {str(e)}"


@mcp.tool()
def set_groove_amount(ctx: Context, value: float) -> str:
    """Set the global groove amount — how strongly clip grooves are applied.

    Parameters:
    - value: 0.0-1.0.
    """
    try:
        ableton = get_ableton_connection()
        r = ableton.send_command("set_groove_amount", {"value": value})
        return f"Global groove amount set to {r.get('groove_amount')}"
    except Exception as e:
        logger.error(f"Error setting groove amount: {str(e)}")
        return f"Error setting groove amount: {str(e)}"


@mcp.tool()
def apply_clip_groove(
    ctx: Context, track_index: int, clip_index: int, groove_name: str
) -> str:
    """Assign a groove from the groove pool to a clip.

    Parameters:
    - track_index / clip_index: 1-based track and clip slot.
    - groove_name: Name as listed by get_grooves.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        ci = _to_zero_based(clip_index, "clip_index")
        r = ableton.send_command("apply_clip_groove", {
            "track_index": ti, "clip_index": ci, "groove_name": groove_name,
        })
        return f"Applied groove '{r.get('groove')}' to clip '{r.get('clip_name')}'"
    except Exception as e:
        logger.error(f"Error applying groove: {str(e)}")
        return f"Error applying groove: {str(e)}"


@mcp.tool()
def set_transport_state(
    ctx: Context,
    metronome: bool | None = None,
    loop: bool | None = None,
    session_record: bool | None = None,
    record_mode: bool | None = None,
    punch_in: bool | None = None,
    punch_out: bool | None = None,
) -> str:
    """Set transport toggles. Omitted values are left alone.

    Parameters:
    - metronome: Click on/off.
    - loop: Arrangement loop on/off.
    - session_record: Session record button.
    - record_mode: Arrangement record arm.
    - punch_in / punch_out: Punch recording toggles.
    """
    try:
        ableton = get_ableton_connection()
        payload: dict = {}
        for key, val in (
            ("metronome", metronome), ("loop", loop),
            ("session_record", session_record), ("record_mode", record_mode),
            ("punch_in", punch_in), ("punch_out", punch_out),
        ):
            if val is not None:
                payload[key] = val
        if not payload:
            return "No transport changes requested"
        r = ableton.send_command("set_transport_state", payload)
        changed = r.get("changed", {})
        return "Transport: " + ", ".join(f"{k}={v}" for k, v in changed.items())
    except Exception as e:
        logger.error(f"Error setting transport state: {str(e)}")
        return f"Error setting transport state: {str(e)}"


@mcp.tool()
def capture_midi(ctx: Context) -> str:
    """Capture recently played MIDI into a clip — Live's Capture button.

    Works even when nothing was armed for recording, so an idea played while
    noodling can still be recovered.
    """
    try:
        ableton = get_ableton_connection()
        ableton.send_command("capture_midi")
        return "Captured recent MIDI into a clip"
    except Exception as e:
        logger.error(f"Error capturing MIDI: {str(e)}")
        return f"Error capturing MIDI: {str(e)}"


@mcp.tool()
def undo_redo(ctx: Context, action: str = "undo") -> str:
    """Undo or redo the last operation in Live.

    The safety net: any change made through this API can be reversed without
    touching the keyboard.

    Parameters:
    - action: "undo" or "redo".
    """
    try:
        ableton = get_ableton_connection()
        r = ableton.send_command("undo_redo", {"action": action})
        if not r.get("performed"):
            return f"Nothing to {action} ({r.get('reason')})"
        return f"Performed {r.get('action')}"
    except Exception as e:
        logger.error(f"Error in undo/redo: {str(e)}")
        return f"Error in undo/redo: {str(e)}"


@mcp.tool()
def set_track_fold(ctx: Context, track_index: int, folded: bool = True) -> str:
    """Fold or unfold a group track.

    Group tracks cannot be created through the API — Live does not expose
    that — but existing groups can be folded and unfolded.

    Parameters:
    - track_index: Group track number (1-based).
    - folded: True to collapse, False to expand.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        r = ableton.send_command("set_track_fold", {
            "track_index": ti, "folded": folded,
        })
        state = "folded" if r.get("folded") else "unfolded"
        return f"'{r.get('track_name')}' {state}"
    except Exception as e:
        logger.error(f"Error folding track: {str(e)}")
        return f"Error folding track: {str(e)}"


@mcp.tool()
def select_view_target(
    ctx: Context, track_index: int | None = None, scene_index: int | None = None
) -> str:
    """Select a track and/or scene in Live's UI.

    Useful for showing the user what is being discussed, and required before
    some browser load operations.

    Parameters:
    - track_index: Track number (1-based), optional.
    - scene_index: Scene number (1-based), optional.
    """
    try:
        ableton = get_ableton_connection()
        payload: dict = {}
        if track_index is not None:
            payload["track_index"] = _to_zero_based(track_index, "track_index")
        if scene_index is not None:
            payload["scene_index"] = _to_zero_based(scene_index, "scene_index")
        if not payload:
            return "Nothing to select"
        r = ableton.send_command("select_view_target", payload)
        sel = r.get("selected", {})
        return "Selected " + ", ".join(f"{k}='{v}'" for k, v in sel.items())
    except Exception as e:
        logger.error(f"Error selecting view target: {str(e)}")
        return f"Error selecting view target: {str(e)}"


@mcp.tool()
def delete_scene(ctx: Context, scene_index: int) -> str:
    """Delete a scene. Scene indices below it shift up by one.

    Parameters:
    - scene_index: Scene number (1-based).
    """
    try:
        ableton = get_ableton_connection()
        si = _to_zero_based(scene_index, "scene_index")
        r = ableton.send_command("delete_scene", {"scene_index": si})
        return (
            f"Deleted scene '{r.get('deleted')}' "
            f"({r.get('scene_count')} scenes remain)"
        )
    except Exception as e:
        logger.error(f"Error deleting scene: {str(e)}")
        return f"Error deleting scene: {str(e)}"


@mcp.tool()
def duplicate_scene(ctx: Context, scene_index: int) -> str:
    """Duplicate a scene with all its clips — the fastest way to build an
    arrangement from a working loop.

    Parameters:
    - scene_index: Scene number (1-based).
    """
    try:
        ableton = get_ableton_connection()
        si = _to_zero_based(scene_index, "scene_index")
        r = ableton.send_command("duplicate_scene", {"scene_index": si})
        return (
            f"Duplicated to scene {r.get('index', 0) + 1} "
            f"({r.get('scene_count')} scenes total)"
        )
    except Exception as e:
        logger.error(f"Error duplicating scene: {str(e)}")
        return f"Error duplicating scene: {str(e)}"


@mcp.tool()
def set_scene_tempo(
    ctx: Context, scene_index: int, tempo: float | None = None
) -> str:
    """Give a scene its own tempo, so launching it changes the Set's tempo.

    This is the mechanism for a live set where songs run at different
    speeds — label scenes as songs and each one arrives at its own BPM.

    Parameters:
    - scene_index: Scene number (1-based).
    - tempo: BPM, or omit to disable the scene's tempo override.
    """
    try:
        ableton = get_ableton_connection()
        si = _to_zero_based(scene_index, "scene_index")
        payload: dict = {"scene_index": si}
        if tempo is not None:
            payload["tempo"] = tempo
        r = ableton.send_command("set_scene_tempo", payload)
        if not r.get("tempo_enabled"):
            return f"Disabled tempo override on scene {scene_index}"
        return f"Scene {scene_index} will set tempo to {r.get('tempo')} BPM"
    except Exception as e:
        logger.error(f"Error setting scene tempo: {str(e)}")
        return f"Error setting scene tempo: {str(e)}"


@mcp.tool()
def create_audio_track(ctx: Context, index: int = -1) -> str:
    """Create a new audio track.

    Parameters:
    - index: 1-based position to insert at, or -1 for the end of the list.
    """
    try:
        ableton = get_ableton_connection()
        zero_based = -1 if index == -1 else _to_zero_based(index, "index")
        result = ableton.send_command("create_audio_track", {"index": zero_based})
        return (
            f"Created audio track '{result.get('name')}' "
            f"at index {result.get('index', 0) + 1}"
        )
    except Exception as e:
        logger.error(f"Error creating audio track: {str(e)}")
        return f"Error creating audio track: {str(e)}"


@mcp.tool()
def create_return_track(ctx: Context) -> str:
    """Create a new return track at the end of the return list.

    Return tracks are addressed by indices after all session tracks: with 10
    session tracks, returns A/B/C are indices 11/12/13.
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_return_track")
        return (
            f"Created return track '{result.get('name')}' at index "
            f"{result.get('index', 0) + 1} "
            f"({result.get('return_count')} returns total)"
        )
    except Exception as e:
        logger.error(f"Error creating return track: {str(e)}")
        return f"Error creating return track: {str(e)}"


@mcp.tool()
def duplicate_track(ctx: Context, track_index: int) -> str:
    """Duplicate a session track with all its devices and clips.

    The copy is inserted directly after the source, so every track below it
    shifts down by one — call get_session_overview afterwards.

    Parameters:
    - track_index: Track number (1-based).
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        result = ableton.send_command("duplicate_track", {"track_index": ti})
        return (
            f"Duplicated '{result.get('source')}' -> '{result.get('name')}' "
            f"at index {result.get('index', 0) + 1}. "
            f"Indices below have shifted; re-check with get_session_overview."
        )
    except Exception as e:
        logger.error(f"Error duplicating track: {str(e)}")
        return f"Error duplicating track: {str(e)}"


@mcp.tool()
def set_send(ctx: Context, track_index: int, send_index: int, value: float) -> str:
    """Set how much of a track is sent to a return track.

    Parameters:
    - track_index: Track number (1-based).
    - send_index: Return to send to, 1-based (1 = return A, 2 = B, 3 = C).
    - value: Normalized 0.0-1.0, where 0.0 is no send and 1.0 is full.

    Keep sub and bass tracks dry — reverb on low frequencies is the most
    common cause of a muddy mix.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        si = _to_zero_based(send_index, "send_index")
        result = ableton.send_command("set_send", {
            "track_index": ti,
            "send_index": si,
            "value": value,
        })
        return (
            f"Set '{result.get('track_name')}' send "
            f"{chr(ord('A') + si)} to {result.get('display_value')}"
        )
    except Exception as e:
        logger.error(f"Error setting send: {str(e)}")
        return f"Error setting send: {str(e)}"


@mcp.tool()
def set_track_state(
    ctx: Context,
    track_index: int,
    mute: bool | None = None,
    solo: bool | None = None,
    arm: bool | None = None,
    color_index: int | None = None,
) -> str:
    """Set mute, solo, arm and/or colour on a track. Omitted values are left alone.

    Parameters:
    - track_index: Track number (1-based).
    - mute / solo / arm: True or False. Arm fails on tracks that can't be armed.
    - color_index: Live colour palette index (0-69).
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        payload: dict = {"track_index": ti}
        for key, val in (
            ("mute", mute), ("solo", solo), ("arm", arm),
            ("color_index", color_index),
        ):
            if val is not None:
                payload[key] = val
        result = ableton.send_command("set_track_state", payload)
        changed = result.get("changed", {})
        if not changed:
            return f"No changes requested for '{result.get('track_name')}'"
        detail = ", ".join(f"{k}={v}" for k, v in changed.items())
        return f"Set '{result.get('track_name')}': {detail}"
    except Exception as e:
        logger.error(f"Error setting track state: {str(e)}")
        return f"Error setting track state: {str(e)}"


@mcp.tool()
def create_scene(ctx: Context, index: int = -1) -> str:
    """Create a new scene.

    Parameters:
    - index: 1-based position, or -1 for the end of the scene list.
    """
    try:
        ableton = get_ableton_connection()
        zero_based = -1 if index == -1 else _to_zero_based(index, "index")
        result = ableton.send_command("create_scene", {"index": zero_based})
        return (
            f"Created scene {result.get('index', 0) + 1} "
            f"({result.get('scene_count')} scenes total)"
        )
    except Exception as e:
        logger.error(f"Error creating scene: {str(e)}")
        return f"Error creating scene: {str(e)}"


@mcp.tool()
def set_scene_name(ctx: Context, scene_index: int, name: str) -> str:
    """Rename a scene. Useful for labelling song sections in Session view.

    Parameters:
    - scene_index: Scene number (1-based).
    - name: New scene name, e.g. "Verse 1" or "Drop".
    """
    try:
        ableton = get_ableton_connection()
        si = _to_zero_based(scene_index, "scene_index")
        result = ableton.send_command("set_scene_name", {
            "scene_index": si, "name": name,
        })
        return f"Renamed scene {result.get('index', 0) + 1} to '{result.get('name')}'"
    except Exception as e:
        logger.error(f"Error setting scene name: {str(e)}")
        return f"Error setting scene name: {str(e)}"


@mcp.tool()
def fire_scene(ctx: Context, scene_index: int) -> str:
    """Launch a scene, firing every clip in that row.

    Parameters:
    - scene_index: Scene number (1-based).
    """
    try:
        ableton = get_ableton_connection()
        si = _to_zero_based(scene_index, "scene_index")
        result = ableton.send_command("fire_scene", {"scene_index": si})
        return f"Fired scene {result.get('index', 0) + 1} '{result.get('name')}'"
    except Exception as e:
        logger.error(f"Error firing scene: {str(e)}")
        return f"Error firing scene: {str(e)}"


@mcp.tool()
def set_clip_properties(
    ctx: Context,
    track_index: int,
    clip_index: int,
    looping: bool | None = None,
    loop_end: float | None = None,
    gain: float | None = None,
    warping: bool | None = None,
    color_index: int | None = None,
    quantize_to: int | None = None,
    quantize_amount: float = 1.0,
) -> str:
    """Set clip loop, gain, warp, colour, and optionally quantize it.

    Parameters:
    - track_index / clip_index: 1-based track and clip slot.
    - looping: Whether the clip loops.
    - loop_end: Loop end position in beats.
    - gain: Audio clip gain, 0.0-1.0 (ignored on MIDI clips).
    - warping: Whether an audio clip is warped (ignored on MIDI clips).
    - color_index: Live colour palette index (0-69).
    - quantize_to: Grid constant — 4 = 1/8, 5 = 1/16, 6 = 1/32.
    - quantize_amount: 0.0-1.0. Use less than 1.0 to tighten while keeping feel.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        ci = _to_zero_based(clip_index, "clip_index")
        payload: dict = {"track_index": ti, "clip_index": ci,
                         "quantize_amount": quantize_amount}
        for key, val in (
            ("looping", looping), ("loop_end", loop_end), ("gain", gain),
            ("warping", warping), ("color_index", color_index),
            ("quantize_to", quantize_to),
        ):
            if val is not None:
                payload[key] = val
        result = ableton.send_command("set_clip_properties", payload)
        changed = result.get("changed", {})
        if not changed:
            return f"No changes requested for clip '{result.get('clip_name')}'"
        detail = ", ".join(f"{k}={v}" for k, v in changed.items())
        return (
            f"Set '{result.get('clip_name')}' on "
            f"'{result.get('track_name')}': {detail}"
        )
    except Exception as e:
        logger.error(f"Error setting clip properties: {str(e)}")
        return f"Error setting clip properties: {str(e)}"


@mcp.tool()
def set_track_volume(ctx: Context, track_index: int, volume: float) -> str:
    """Set the mixer fader volume for a track directly.

    This controls the actual track fader, not any device parameter.

    Volume scale (normalized):
      0.0   = silence
      0.85  = 0 dB (unity gain, Ableton's default fader position)
      1.0   = maximum (~+6 dB)

    Parameters:
    - track_index: Track number (1-based). Return tracks come after session tracks.
    - volume: Normalized volume 0.0–1.0. Use 0.85 for unity (0 dB).
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        result = ableton.send_command("set_track_volume", {
            "track_index": ti,
            "volume": volume,
        })
        name = result.get("track_name", "?")
        vol = result.get("volume", volume)
        import math
        unity = 0.85
        db_str = f"{20 * math.log10(vol / unity):+.1f} dB" if vol > 0 else "-inf dB"
        return f"Set '{name}' fader to {vol:.4f} (≈ {db_str})"
    except Exception as e:
        logger.error(f"Error setting track volume: {str(e)}")
        return f"Error setting track volume: {str(e)}"


@mcp.tool()
def set_track_panning(ctx: Context, track_index: int, panning: float) -> str:
    """Set the mixer panning for a track.

    Parameters:
    - track_index: Track number (1-based).
    - panning: -1.0 = full left, 0.0 = center, +1.0 = full right.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        result = ableton.send_command("set_track_panning", {
            "track_index": ti,
            "panning": panning,
        })
        name = result.get("track_name", "?")
        pan = result.get("panning", panning)
        pan_str = "center" if abs(pan) < 0.01 else (f"{abs(pan):.2f} {'L' if pan < 0 else 'R'}")
        return f"Set '{name}' panning to {pan:.4f} ({pan_str})"
    except Exception as e:
        logger.error(f"Error setting track panning: {str(e)}")
        return f"Error setting track panning: {str(e)}"


@mcp.tool()
def create_clip(ctx: Context, track_index: int, clip_index: int, length: float = 4.0) -> str:
    """
    Create a new MIDI clip in the specified track and clip slot.

    Parameters:
    - track_index: Track number (1-based).
    - clip_index: Clip slot number (1-based).
    - length: The length of the clip in beats (default: 4.0).
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        ci = _to_zero_based(clip_index, "clip_index")
        result = ableton.send_command("create_clip", {
            "track_index": ti,
            "clip_index": ci,
            "length": length
        })
        return f"Created new clip at track {track_index}, slot {clip_index} with length {length} beats"
    except Exception as e:
        logger.error(f"Error creating clip: {str(e)}")
        return f"Error creating clip: {str(e)}"

@mcp.tool()
def add_notes_to_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    notes: List[Dict[str, Union[int, float, bool]]]
) -> str:
    """
    Add MIDI notes to a clip.

    Parameters:
    - track_index: Track number (1-based).
    - clip_index: Clip slot number (1-based).
    - notes: List of note dictionaries, each with pitch, start_time, duration, velocity, and mute.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        ci = _to_zero_based(clip_index, "clip_index")
        result = ableton.send_command("add_notes_to_clip", {
            "track_index": ti,
            "clip_index": ci,
            "notes": notes
        })
        return f"Added {len(notes)} notes to clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error adding notes to clip: {str(e)}")
        return f"Error adding notes to clip: {str(e)}"

@mcp.tool()
def set_clip_name(ctx: Context, track_index: int, clip_index: int, name: str) -> str:
    """
    Set the name of a clip.

    Parameters:
    - track_index: Track number (1-based).
    - clip_index: Clip slot number (1-based).
    - name: The new name for the clip.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        ci = _to_zero_based(clip_index, "clip_index")
        result = ableton.send_command("set_clip_name", {
            "track_index": ti,
            "clip_index": ci,
            "name": name
        })
        return f"Renamed clip at track {track_index}, slot {clip_index} to '{name}'"
    except Exception as e:
        logger.error(f"Error setting clip name: {str(e)}")
        return f"Error setting clip name: {str(e)}"

@mcp.tool()
def set_tempo(ctx: Context, tempo: float) -> str:
    """
    Set the tempo of the Ableton session.
    
    Parameters:
    - tempo: The new tempo in BPM
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_tempo", {"tempo": tempo})
        return f"Set tempo to {tempo} BPM"
    except Exception as e:
        logger.error(f"Error setting tempo: {str(e)}")
        return f"Error setting tempo: {str(e)}"


@mcp.tool()
def load_instrument_or_effect(ctx: Context, track_index: int, uri: str) -> str:
    """
    Load an instrument or effect onto a track using its URI.

    Parameters:
    - track_index: Track number (1-based).
    - uri: The URI of the instrument or effect to load (e.g., 'query:Synths#Instrument%20Rack:Bass:FileId_5116').
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        result = ableton.send_command("load_browser_item", {
            "track_index": ti,
            "item_uri": uri
        })
        
        if result.get("loaded", False):
            new_devices = result.get("new_devices", [])
            if new_devices:
                return f"Loaded instrument with URI '{uri}' on track {track_index}. New devices: {', '.join(new_devices)}"
            devices = result.get("devices_after", [])
            if devices:
                return f"Loaded instrument with URI '{uri}' on track {track_index}. Devices on track: {', '.join(devices)}"
            item_name = result.get("item_name", "")
            if item_name:
                return f"Loaded '{item_name}' on track {track_index}."
            return f"Loaded instrument with URI '{uri}' on track {track_index}."
        else:
            return f"Failed to load instrument with URI '{uri}'"
    except Exception as e:
        logger.error(f"Error loading instrument by URI: {str(e)}")
        return f"Error loading instrument by URI: {str(e)}"

@mcp.tool()
def fire_clip(ctx: Context, track_index: int, clip_index: int) -> str:
    """
    Start playing a clip.

    Parameters:
    - track_index: Track number (1-based).
    - clip_index: Clip slot number (1-based).
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        ci = _to_zero_based(clip_index, "clip_index")
        result = ableton.send_command("fire_clip", {
            "track_index": ti,
            "clip_index": ci
        })
        return f"Started playing clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error firing clip: {str(e)}")
        return f"Error firing clip: {str(e)}"

@mcp.tool()
def stop_clip(ctx: Context, track_index: int, clip_index: int) -> str:
    """
    Stop playing a clip.

    Parameters:
    - track_index: Track number (1-based).
    - clip_index: Clip slot number (1-based).
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        ci = _to_zero_based(clip_index, "clip_index")
        result = ableton.send_command("stop_clip", {
            "track_index": ti,
            "clip_index": ci
        })
        return f"Stopped clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error stopping clip: {str(e)}")
        return f"Error stopping clip: {str(e)}"

@mcp.tool()
def start_playback(ctx: Context) -> str:
    """Start playing the Ableton session."""
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("start_playback")
        return "Started playback"
    except Exception as e:
        logger.error(f"Error starting playback: {str(e)}")
        return f"Error starting playback: {str(e)}"

@mcp.tool()
def stop_playback(ctx: Context) -> str:
    """Stop playing the Ableton session."""
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_playback")
        return "Stopped playback"
    except Exception as e:
        logger.error(f"Error stopping playback: {str(e)}")
        return f"Error stopping playback: {str(e)}"

@mcp.tool()
def get_browser_tree(ctx: Context, category_type: str = "all") -> str:
    """
    Get a hierarchical tree of browser categories from Ableton.
    
    Parameters:
    - category_type: Type of categories to get ('all', 'instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects')
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_tree", {
            "category_type": category_type
        })
        
        # Check if we got any categories
        if "available_categories" in result and len(result.get("categories", [])) == 0:
            available_cats = result.get("available_categories", [])
            return (f"No categories found for '{category_type}'. "
                   f"Available browser categories: {', '.join(available_cats)}")
        
        # Format the tree in a more readable way
        total_folders = result.get("total_folders", 0)
        formatted_output = f"Browser tree for '{category_type}' (showing {total_folders} folders):\n\n"
        
        def format_tree(item, indent=0):
            output = ""
            if item:
                prefix = "  " * indent
                name = item.get("name", "Unknown")
                path = item.get("path", "")
                has_more = item.get("has_more", False)
                
                # Add this item
                output += f"{prefix}• {name}"
                if path:
                    output += f" (path: {path})"
                if has_more:
                    output += " [...]"
                output += "\n"
                
                # Add children
                for child in item.get("children", []):
                    output += format_tree(child, indent + 1)
            return output
        
        # Format each category
        for category in result.get("categories", []):
            formatted_output += format_tree(category)
            formatted_output += "\n"
        
        return formatted_output
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        else:
            logger.error(f"Error getting browser tree: {error_msg}")
            return f"Error getting browser tree: {error_msg}"

@mcp.tool()
def get_browser_items_at_path(ctx: Context, path: str, limit: int = 50, offset: int = 0) -> str:
    """
    Get browser items at a specific path in Ableton's browser.

    Parameters:
    - path: Path in the format "category/folder/subfolder"
            where category is one of the available browser categories in Ableton
    - limit: Max items to return (default 50). Large sample folders can hold
             500+ items — the default caps the response to keep tool-call
             output tight. Set to 0 for all items.
    - offset: Number of items to skip (for pagination). Default 0.

    Response is JSON including 'items', 'total_count', 'returned', 'offset',
    and 'limit'. If total_count > offset + returned, call again with
    offset = offset + returned to get the next page.
    """
    try:
        ableton = get_ableton_connection()
        # Coerce defensively — MCP callers sometimes send numeric args as strings
        lim = int(limit) if limit is not None else 0
        off = int(offset) if offset is not None else 0
        # limit=0 → unlimited (omit from payload, Remote Script treats None as unlimited)
        payload = {"path": path, "offset": off}
        if lim > 0:
            payload["limit"] = lim
        result = ableton.send_command("get_browser_items_at_path", payload)

        # Check if there was an error with available categories
        if "error" in result and "available_categories" in result:
            error = result.get("error", "")
            available_cats = result.get("available_categories", [])
            return (f"Error: {error}\n"
                   f"Available browser categories: {', '.join(available_cats)}")

        return json.dumps(result, indent=2)
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        elif "Unknown or unavailable category" in error_msg:
            logger.error(f"Invalid browser category: {error_msg}")
            return f"Error: {error_msg}. Please check the available categories using get_browser_tree."
        elif "Path part" in error_msg and "not found" in error_msg:
            logger.error(f"Path not found: {error_msg}")
            return f"Error: {error_msg}. Please check the path and try again."
        else:
            logger.error(f"Error getting browser items at path: {error_msg}")
            return f"Error getting browser items at path: {error_msg}"


def _normalize_plugin_search_text(value: str) -> str:
    """Normalize plugin names/queries for tolerant matching."""
    if not value:
        return ""
    cleaned = re.sub(r"[\s\-_]+", " ", value.strip().lower())
    return re.sub(r"\s+", " ", cleaned)


def _plugin_match_score(plugin_name: str, query: str) -> int:
    """Compute a rough match score for plugin name search."""
    normalized_name = _normalize_plugin_search_text(plugin_name)
    normalized_query = _normalize_plugin_search_text(query)

    if not normalized_query:
        return 1
    if normalized_name == normalized_query:
        return 1000  # exact match
    if normalized_name.startswith(normalized_query):
        return 900  # strong prefix match

    query_tokens = [t for t in normalized_query.split(" ") if t]
    if query_tokens and all(token in normalized_name for token in query_tokens):
        # token coverage, weighted by total query token length
        return 700 + sum(len(t) for t in query_tokens)

    if normalized_query in normalized_name:
        return 600 + len(normalized_query)  # simple substring match

    return 0


def _collect_external_plugins_from_root(
    ableton: AbletonConnection,
    root_path: str,
    max_depth: int = 8,
    max_visited_paths: int = 2000,
) -> List[Dict[str, Any]]:
    """Recursively walk a browser root path and collect loadable plugin items."""
    stack: List[tuple[str, int]] = [(root_path, 0)]
    visited: set[str] = set()
    plugins: List[Dict[str, Any]] = []

    while stack:
        current_path, depth = stack.pop()
        if current_path in visited:
            continue
        visited.add(current_path)

        if len(visited) > max_visited_paths:
            raise RuntimeError(
                "Plugin traversal exceeded safety limit ({0} paths).".format(max_visited_paths)
            )

        result = ableton.send_command("get_browser_items_at_path", {"path": current_path})
        if "error" in result:
            # Root errors matter; deeper path misses are expected from stale paths.
            if depth == 0:
                raise ValueError(result.get("error", "Unknown browser root error"))
            continue

        items = result.get("items", [])
        for item in items:
            name = (item.get("name") or "").strip()
            if not name:
                continue

            child_path = "{0}/{1}".format(current_path, name)
            is_folder = bool(item.get("is_folder", False))
            is_loadable = bool(item.get("is_loadable", False))
            uri = item.get("uri")

            if is_loadable and uri:
                plugins.append({
                    "name": name,
                    "uri": uri,
                    "path": child_path,
                    "is_device": bool(item.get("is_device", False)),
                    "root": root_path,
                })

            if is_folder and depth < max_depth:
                stack.append((child_path, depth + 1))

    return plugins


def _discover_external_plugins(ableton: AbletonConnection) -> List[Dict[str, Any]]:
    """Discover loadable external plugins from common browser roots."""
    # Include aliases to survive differences in browser category naming.
    candidate_roots = ["plugins", "vst3", "vst2", "au", "plug-ins"]
    discovered_any_root = False
    errors: List[str] = []

    for root in candidate_roots:
        try:
            found = _collect_external_plugins_from_root(ableton, root_path=root)
            discovered_any_root = True
            if not found:
                continue

            # First successful non-empty root is enough; aliases can point to the same tree
            # and rescanning them is expensive.
            found.sort(key=lambda p: _normalize_plugin_search_text(p.get("name", "")))
            return found
        except Exception as e:
            errors.append("{0}: {1}".format(root, str(e)))

    if discovered_any_root:
        return []

    raise ValueError(
        "Could not discover external plugins. Tried roots: {0}. Last errors: {1}".format(
            ", ".join(candidate_roots),
            " | ".join(errors) if errors else "none",
        )
    )


def _get_cached_external_plugins(
    ableton: AbletonConnection,
    force_refresh: bool = False,
) -> List[Dict[str, Any]]:
    """Get external plugins using a short-lived cache to avoid repeated deep scans."""
    now = time.monotonic()
    with _external_plugin_cache_lock:
        cached_plugins = _external_plugin_cache.get("plugins")
        built_at = float(_external_plugin_cache.get("built_at", 0.0) or 0.0)
        if (
            not force_refresh
            and cached_plugins is not None
            and (now - built_at) <= _EXTERNAL_PLUGIN_CACHE_TTL_SECONDS
        ):
            return list(cached_plugins)

    discovered = _discover_external_plugins(ableton)
    with _external_plugin_cache_lock:
        _external_plugin_cache["plugins"] = list(discovered)
        _external_plugin_cache["built_at"] = time.monotonic()
    return discovered


@mcp.tool()
def list_external_plugins(
    ctx: Context,
    query: str = "",
    max_results: int = 50,
    refresh_cache: bool = False,
) -> str:
    """List discovered external plugins (VST/AU), optionally filtered by name query.

    Parameters:
    - query: Optional case-insensitive search string.
    - max_results: Maximum number of plugins to display.
    - refresh_cache: If True, force a rescan instead of using cached results.
    """
    try:
        ableton = get_ableton_connection()
        plugins = _get_cached_external_plugins(ableton, force_refresh=refresh_cache)

        if query:
            scored = []
            for plugin in plugins:
                score = _plugin_match_score(plugin.get("name", ""), query)
                if score > 0:
                    scored.append((score, plugin))
            scored.sort(key=lambda x: (-x[0], _normalize_plugin_search_text(x[1].get("name", ""))))
            filtered = [item for _, item in scored]
        else:
            filtered = plugins

        if not filtered:
            if query:
                return "No external plugins matched query '{0}'.".format(query)
            return "No external plugins were discovered."

        max_results = max(1, int(max_results))
        shown = filtered[:max_results]
        lines = [
            "External plugins discovered: {0} total, showing {1}".format(len(filtered), len(shown)),
            "",
        ]
        for idx, plugin in enumerate(shown, start=1):
            lines.append(
                "  {0}. {1} (path: {2})".format(
                    idx,
                    plugin.get("name", "Unknown"),
                    plugin.get("path", "?"),
                )
            )

        if len(filtered) > len(shown):
            lines.append("")
            lines.append(
                "Use max_results={0} (or a tighter query) to see more.".format(len(filtered))
            )
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error listing external plugins: {str(e)}")
        return f"Error listing external plugins: {str(e)}"


@mcp.tool()
def load_external_plugin(
    ctx: Context,
    track_index: int,
    plugin_name: str,
    exact_match: bool = False,
    refresh_cache: bool = False,
) -> str:
    """Load an external plugin onto a track by plugin name (no URI required).

    Parameters:
    - track_index: Track number (1-based).
    - plugin_name: Plugin name to match (e.g., "FabFilter Pro-Q 3").
    - exact_match: If True, require exact normalized name match.
    - refresh_cache: If True, force a rescan before matching.
    """
    try:
        if not plugin_name or not plugin_name.strip():
            return "Error: plugin_name is required."

        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        plugins = _get_cached_external_plugins(ableton, force_refresh=refresh_cache)

        scored = []
        for plugin in plugins:
            score = _plugin_match_score(plugin.get("name", ""), plugin_name)
            if exact_match and score < 1000:
                continue
            if score > 0:
                scored.append((score, plugin))

        scored.sort(key=lambda x: (-x[0], _normalize_plugin_search_text(x[1].get("name", ""))))
        if not scored:
            return (
                "No external plugin matched '{0}'. Try list_external_plugins(query='{0}') "
                "to inspect candidates."
            ).format(plugin_name)

        top_score = scored[0][0]
        top_plugins = [plugin for score, plugin in scored if score == top_score]

        # For non-exact lookup, avoid guessing when multiple strongest candidates exist.
        if len(top_plugins) > 1 and top_score < 1000:
            options = ", ".join(p.get("name", "?") for p in top_plugins[:5])
            return (
                "Multiple plugins match '{0}': {1}. "
                "Please be more specific or set exact_match=True."
            ).format(plugin_name, options)

        chosen = top_plugins[0]
        result = ableton.send_command("load_browser_item", {
            "track_index": ti,
            "item_uri": chosen.get("uri"),
        })

        if result.get("loaded", False):
            return (
                "Loaded external plugin '{0}' on track {1} (matched '{2}')."
            ).format(chosen.get("name", "?"), track_index, plugin_name)
        return "Failed to load external plugin '{0}'.".format(chosen.get("name", "?"))
    except Exception as e:
        logger.error(f"Error loading external plugin: {str(e)}")
        return f"Error loading external plugin: {str(e)}"


_BROWSER_URI_SCHEME_RE = re.compile(r"^[a-z][a-z0-9.+-]*:")


def _looks_like_browser_uri(value: str) -> bool:
    return isinstance(value, str) and bool(_BROWSER_URI_SCHEME_RE.match(value))


@mcp.tool()
def load_drum_kit(ctx: Context, track_index: int, rack_uri: str, kit_path: str) -> str:
    """
    Load a drum rack and then load a specific drum kit into it.

    Parameters:
    - track_index: Track number (1-based).
    - rack_uri: Browser URI of the drum rack (e.g., 'query:Drums#Drum%20Rack').
    - kit_path: Either a browser URI of the kit (e.g., 'query:Drums#FileId_4197')
                or a browser path. Stock kits live as .adg leaves directly under
                'drums', e.g. 'drums/808 Core Kit.adg'. Folder paths fall back
                to loading the first loadable child (e.g. 'user-library/My Kits').
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")

        rack_result = ableton.send_command("load_browser_item", {
            "track_index": ti,
            "item_uri": rack_uri,
        })
        if not rack_result.get("loaded", False):
            return f"Failed to load drum rack with URI '{rack_uri}'"

        if _looks_like_browser_uri(kit_path):
            kit_uri = kit_path
            kit_name = kit_path
        else:
            kit_result = ableton.send_command("get_browser_items_at_path", {
                "path": kit_path,
            })
            if "error" in kit_result:
                return f"Loaded drum rack but failed to find drum kit: {kit_result.get('error')}"

            if kit_result.get("is_loadable") and kit_result.get("uri"):
                kit_uri = kit_result["uri"]
                kit_name = kit_result.get("name") or kit_path
            else:
                loadable_kits = [
                    item for item in kit_result.get("items", [])
                    if item.get("is_loadable", False)
                ]
                if not loadable_kits:
                    return f"Loaded drum rack but no loadable drum kits found at '{kit_path}'"
                kit_uri = loadable_kits[0].get("uri")
                kit_name = loadable_kits[0].get("name")

        ableton.send_command("load_browser_item", {
            "track_index": ti,
            "item_uri": kit_uri,
        })
        return f"Loaded drum rack and kit '{kit_name}' on track {track_index}"
    except Exception as e:
        logger.error(f"Error loading drum kit: {str(e)}")
        return f"Error loading drum kit: {str(e)}"

# --- Arrangement View Tools ---

_ARRANGEMENT_TIP = "\nTip: use set_ableton_view(view='Arranger') to see changes in arrangement view."


def _get_time_signature():
    """Get current time signature from Ableton."""
    ableton = get_ableton_connection()
    info = ableton.send_command("get_session_info")
    return info.get("signature_numerator", 4), info.get("signature_denominator", 4)


def _convert_bar_to_beat(bar: int, beat: float = 0.0) -> float:
    """Convert bar (1-based) to beat, fetching time signature from Ableton."""
    if bar > 0:
        num, denom = _get_time_signature()
        return bar_to_beat(bar, num, denom)
    return beat


@mcp.tool()
def get_arrangement_info(ctx: Context, track_index: int = 0) -> str:
    """Get arrangement clips and transport state.

    Parameters:
    - track_index: Track number (1-based). 0 = all tracks.
    """
    try:
        ableton = get_ableton_connection()
        idx = _optional_to_zero_based(track_index, "track_index")
        result = ableton.send_command("get_arrangement_info", {"track_index": idx if idx is not None else -1})

        num = result.get("transport", {}).get("signature_numerator", 4)
        denom = result.get("transport", {}).get("signature_denominator", 4)
        transport = result.get("transport", {})

        lines = ["=== Arrangement Info ==="]
        lines.append(f"Tempo: {transport.get('tempo')} BPM | "
                     f"Time Sig: {num}/{denom} | "
                     f"Playing: {transport.get('is_playing')} | "
                     f"Position: bar {beat_to_bar(transport.get('current_time', 0), num, denom)}")
        if transport.get("loop_enabled"):
            ls = transport.get("loop_start", 0)
            ll = transport.get("loop_length", 0)
            lines.append(f"Loop: bars {beat_to_bar(ls, num, denom)}-"
                         f"{beat_to_bar(ls + ll, num, denom)}")

        for t in result.get("tracks", []):
            clips = t.get("arrangement_clips", [])
            lines.append(f"\nTrack {t['index'] + 1}: {t['name']} "
                         f"({'MIDI' if t.get('is_midi') else 'Audio'}) — "
                         f"{len(clips)} clip(s)")
            for c in clips:
                st = c.get("start_time", 0)
                et = c.get("end_time", 0)
                muted = " [MUTED]" if c.get("muted") else ""
                lines.append(f"  {c.get('index', 0) + 1}. \"{c.get('name', '')}\" "
                             f"bars {beat_to_bar(st, num, denom)}-"
                             f"{beat_to_bar(et, num, denom)}{muted}")

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error getting arrangement info: {str(e)}")
        return f"Error getting arrangement info: {str(e)}"


@mcp.tool()
def get_cue_points(ctx: Context) -> str:
    """List all cue points (locators) with bar positions."""
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_cue_points")
        num, denom = _get_time_signature()

        cues = result.get("cue_points", [])
        if not cues:
            return "No cue points in this project."

        lines = ["=== Cue Points ==="]
        for cp in cues:
            bar = beat_to_bar(cp.get("time", 0), num, denom)
            lines.append(f"  \"{cp.get('name', '')}\" — bar {bar}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error getting cue points: {str(e)}")
        return f"Error getting cue points: {str(e)}"


@mcp.tool()
def set_song_time(ctx: Context, bar: int = 0, beat: float = 0.0) -> str:
    """Jump playback to a position.

    Parameters:
    - bar: Bar number (1-based). Takes precedence over beat.
    - beat: Beat position (0-based).
    """
    try:
        ableton = get_ableton_connection()
        time_val = _convert_bar_to_beat(bar, beat)
        result = ableton.send_command("set_song_time", {"time": time_val})
        num, denom = _get_time_signature()
        return f"Jumped to bar {beat_to_bar(time_val, num, denom)} (beat {time_val})"
    except Exception as e:
        logger.error(f"Error setting song time: {str(e)}")
        return f"Error setting song time: {str(e)}"


# Tool name -> wire command, for the handful that differ. Sending the tool
# name in a batch would otherwise fail with "Unknown command", which is a
# documented trap in LIVE-API-FACTS.md and pointless to make callers relearn.
_BATCH_COMMAND_ALIASES = {
    "duplicate_clip_to_arrangement": "duplicate_to_arrangement",
    "load_instrument_or_effect": "load_browser_item",
    "set_ableton_view": "set_view",
}

# Index fields converted from the 1-based convention every tool uses to the
# 0-based one the Live API uses. Only values >= 1 are touched: 0 means "not
# specified" / "all" / "resolve by name" in several commands, and negatives
# mean "append at end" for track creation.
_BATCH_INDEX_FIELDS = (
    "track_index", "clip_index", "device_index", "scene_index", "chain_index",
)


def _prepare_batch_commands(commands: list,
                            indices_are_one_based: bool = True) -> list:
    """Normalise a batch payload: resolve aliases, rebase indices.

    Split out from the tool so the index rule is directly testable — it is
    the part most likely to silently write to the wrong track.
    """
    prepared = []
    for i, entry in enumerate(commands):
        if not isinstance(entry, dict):
            raise ValueError(f"command {i + 1} is not an object")
        name = entry.get("command") or entry.get("type")
        if not name:
            raise ValueError(f"command {i + 1} has no 'command' name")
        name = _BATCH_COMMAND_ALIASES.get(name, name)
        params = dict(entry.get("params") or {})
        if indices_are_one_based:
            for field in _BATCH_INDEX_FIELDS:
                val = params.get(field)
                # bool is an int subclass; never rebase a flag.
                if isinstance(val, int) and not isinstance(val, bool):
                    if val >= 1:
                        params[field] = val - 1
        prepared.append({"command": name, "params": params})
    return prepared


@mcp.tool()
def batch(
    ctx: Context,
    commands: list,
    stop_on_error: bool = True,
    indices_are_one_based: bool = True,
) -> str:
    """Run many commands in one call instead of one call each.

    Every other tool is a full round trip, and above the socket each one
    costs a separate model turn. Building a single arrangement took roughly
    260 of them — the latency, not Live, is what makes large edits
    impractical. Use this for anything repetitive: placing clips across an
    arrangement, setting levels on every track, renaming a batch of scenes.

    Commands run in order, through exactly the same code path as sending
    them individually.

    Parameters:
    - commands: list of {"command": <name>, "params": {...}}, e.g.
      [{"command": "set_track_volume", "params": {"track_index": 3,
        "volume": 0.8}},
       {"command": "set_track_name", "params": {"track_index": 3,
        "name": "BASS"}}]
      Use the wire command name; the few tool names that differ from it
      (duplicate_clip_to_arrangement, load_instrument_or_effect,
      set_ableton_view) are translated automatically.
    - stop_on_error: Stop at the first failure (default) rather than running
      the rest. Anything already applied stays applied — undo in Live.
    - indices_are_one_based: Keep the 1-based indices every other tool uses.
      Only values >= 1 are converted, so 0 keeps its "all / by name / not
      specified" meaning. Set False to pass raw Live indices straight
      through.

    NOTE: params here are the underlying command's, which are not always
    identical to the tool's — bar numbers are not converted to beats, for
    instance. For one-off calls prefer the dedicated tool.
    """
    try:
        if not isinstance(commands, list):
            return "Error: 'commands' must be a list"
        if not commands:
            return "Error: 'commands' is empty"

        prepared = _prepare_batch_commands(commands, indices_are_one_based)

        ableton = get_ableton_connection()
        r = ableton.send_command("batch", {
            "commands": prepared, "stop_on_error": stop_on_error})

        ran = r.get("ran", 0)
        total = r.get("total", 0)
        ok = r.get("succeeded", 0)
        bad = r.get("failed", 0)
        lines = [f"Batch: {ok}/{total} succeeded, {bad} failed"
                 + (f" (stopped early after {ran})"
                    if r.get("stopped_early") else "")]
        for record in r.get("results") or []:
            if record.get("status") == "error":
                lines.append(
                    f"  #{record.get('index', 0) + 1} {record.get('command')}: "
                    f"{record.get('message')}")
        if bad == 0:
            lines.append("  (all clean)")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error running batch: {str(e)}")
        return f"Error running batch: {str(e)}"


@mcp.tool()
def get_build_info(ctx: Context) -> str:
    """Check that Live, this server, and the repo are running the same build.

    RUN THIS FIRST when a command seems to be missing, a parameter the docs
    describe is rejected, or Live "cannot" do something you believe it can.
    Three copies of this integration run at once — the repo on disk, the
    remote script Live loaded at its own startup, and this server process —
    and they drift apart constantly, because Live only re-reads a remote
    script when Live restarts and this server only re-reads its source when
    the MCP host restarts.

    Every limitation found so far that turned out not to be real was one of
    those copies being stale, not a limit in the Live API.
    """
    try:
        server_build = SERVER_BUILD_ID
        repo_build = None
        repo_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "AbletonMCP_Remote_Script", "__init__.py")
        try:
            with open(repo_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    m = re.match(r'^BUILD_ID\s*=\s*["\'](.+?)["\']', line)
                    if m:
                        repo_build = m.group(1)
                        break
        except OSError:
            pass

        ableton = get_ableton_connection()
        r = ableton.send_command("get_build_info", {})
        live_build = r.get("remote_script_build")

        lines = [
            f"Live {r.get('live_version', '?')}",
            f"  remote script loaded by Live : {live_build}",
            f"  this MCP server process      : {server_build}",
            f"  repo on disk                 : {repo_build or '?'}",
            f"  script file: {r.get('script_file', '?')}",
        ]
        builds = {b for b in (live_build, server_build, repo_build) if b}
        if len(builds) > 1:
            lines.append("")
            lines.append("MISMATCH — these are not the same build.")
            if repo_build and live_build != repo_build:
                lines.append(
                    "  Live is stale: redeploy the remote script, then "
                    "restart Ableton Live (it only reads the script at "
                    "startup).")
            if repo_build and server_build != repo_build:
                lines.append(
                    "  This server is stale: restart the MCP host (Claude "
                    "Code) so it re-imports server.py.")
            lines.append(
                "  Until they match, a missing command means a stale build, "
                "NOT a Live API limitation.")
        else:
            lines.append("")
            lines.append("All three match — a missing command is a real gap.")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error getting build info: {str(e)}")
        return f"Error getting build info: {str(e)}"


@mcp.tool()
def play_section(
    ctx: Context,
    from_bar: int = 1,
    to_bar: int | None = None,
    loop: bool = False,
    play: bool = True,
) -> str:
    """Play the arrangement from a specific bar — not always from bar 1.

    `song.start_time` is writable and `start_playing()` honours it, so a
    single section can be auditioned and metered. (An earlier note in this
    repo called start_time read-only and concluded section metering was
    impossible; it is not.)

    Pair with `get_meters` to measure one section's balance instead of
    guessing, e.g. play_section(from_bar=33) then get_meters.

    Parameters:
    - from_bar: Bar to start at (1-based).
    - to_bar: Optional end bar; sets the arrangement loop brace to this span.
    - loop: Loop that span rather than playing through.
    - play: Set False to move the start marker without starting playback.
    """
    try:
        ableton = get_ableton_connection()
        num, denom = _get_time_signature()
        payload: dict = {
            "from_beat": bar_to_beat(from_bar, num, denom),
            "loop": loop, "play": play,
        }
        if to_bar is not None:
            payload["to_beat"] = bar_to_beat(to_bar, num, denom)
        r = ableton.send_command("play_section", payload)
        span = f"bar {from_bar}" + (f" to {to_bar}" if to_bar else "")
        verb = "Playing from" if r.get("playing") else "Start marker set to"
        tail = " (looping)" if r.get("loop") else ""
        return f"{verb} {span}{tail}"
    except Exception as e:
        logger.error(f"Error playing section: {str(e)}")
        return f"Error playing section: {str(e)}"


@mcp.tool()
def set_arrangement_loop(
    ctx: Context,
    enabled: bool = True,
    start_bar: int = 0,
    end_bar: int = 0,
    start_beat: float = 0.0,
    length_beats: float = 0.0,
) -> str:
    """Enable/disable arrangement loop and set region.

    Parameters:
    - enabled: Whether loop is on.
    - start_bar: Loop start (1-based). Takes precedence over start_beat.
    - end_bar: Loop end (1-based). Used with start_bar to compute length.
    - start_beat: Loop start in beats.
    - length_beats: Loop length in beats.
    """
    try:
        ableton = get_ableton_connection()
        num, denom = _get_time_signature()

        start = None
        length = None
        if start_bar > 0:
            start = bar_to_beat(start_bar, num, denom)
            if end_bar > start_bar:
                length = bar_to_beat(end_bar, num, denom) - start
        else:
            if start_beat > 0:
                start = start_beat
            if length_beats > 0:
                length = length_beats

        params = {"enabled": enabled}
        if start is not None:
            params["start"] = start
        if length is not None:
            params["length"] = length

        result = ableton.send_command("set_arrangement_loop", params)
        state = "enabled" if result.get("enabled") else "disabled"
        ls = result.get("start", 0)
        ll = result.get("length", 0)
        return (f"Loop {state}: bars {beat_to_bar(ls, num, denom)}-"
                f"{beat_to_bar(ls + ll, num, denom)}")
    except Exception as e:
        logger.error(f"Error setting arrangement loop: {str(e)}")
        return f"Error setting arrangement loop: {str(e)}"


@mcp.tool()
def jump_to_cue_point(ctx: Context, direction: str = "", name: str = "") -> str:
    """Jump to a cue point.

    Parameters:
    - direction: "next" or "prev"
    - name: Cue point name to jump to.
    """
    try:
        ableton = get_ableton_connection()
        params = {}
        if direction:
            params["direction"] = direction
        if name:
            params["name"] = name
        result = ableton.send_command("jump_to_cue", params)

        landed_name = result.get("name")
        landed_dir = result.get("direction")
        time_val = result.get("time")
        location = ""
        if time_val is not None:
            num, denom = _get_time_signature()
            location = f" at bar {beat_to_bar(time_val, num, denom)}"

        if landed_name:
            return f"Jumped to cue point '{landed_name}'{location}"
        if landed_dir:
            return f"Jumped {landed_dir} to cue point{location}"
        return f"Jumped to cue point{location}"
    except Exception as e:
        logger.error(f"Error jumping to cue point: {str(e)}")
        return f"Error jumping to cue point: {str(e)}"


def _readback_cue_name(ableton, time_val: float, attempts: int = 4, delay: float = 0.05):
    """Return the actual locator name at ``time_val`` after a create.

    The Remote Script defers cue creation + rename via ``schedule_message`` so
    the remote can move the playhead first; a tight retry covers the race
    between our follow-up read and Live's tick.
    """
    import time as _time
    for i in range(attempts):
        result = ableton.send_command("get_cue_points", {})
        for cp in result.get("cue_points", []):
            if abs(cp.get("time", -1) - time_val) < 0.01:
                return cp.get("name", "")
        if i < attempts - 1:
            _time.sleep(delay)
    return None


def _readback_cue_absent(ableton, time_val: float, attempts: int = 4, delay: float = 0.05):
    """Return True once the cue at ``time_val`` is gone after a delete.

    Symmetric to ``_readback_cue_name`` — the Remote Script defers
    ``set_or_delete_cue`` via ``schedule_message`` so the playhead set lands
    first, and the readback covers the tick race.
    """
    import time as _time
    for i in range(attempts):
        result = ableton.send_command("get_cue_points", {})
        present = any(
            abs(cp.get("time", -1) - time_val) < 0.01
            for cp in result.get("cue_points", []))
        if not present:
            return True
        if i < attempts - 1:
            _time.sleep(delay)
    return False


@mcp.tool()
def create_cue_point(ctx: Context, bar: int = 0, beat: float = 0.0, name: str = "") -> str:
    """Create a cue point at a position.

    Parameters:
    - bar: Bar number (1-based).
    - beat: Beat position (0-based).
    - name: Name for the cue point.
    """
    try:
        ableton = get_ableton_connection()
        time_val = _convert_bar_to_beat(bar, beat)
        ableton.send_command("create_cue_point", {"time": time_val, "name": name})
        if bar > 0:
            bar_str = str(bar)
        else:
            num, denom = _get_time_signature()
            bar_str = str(beat_to_bar(time_val, num, denom))
        actual_name = _readback_cue_name(ableton, time_val)
        if actual_name is None:
            return f"Created cue point at bar {bar_str}"
        if name and actual_name != name:
            return (f"Created cue point '{actual_name}' at bar {bar_str} "
                    f"(requested name '{name}' not applied)")
        if actual_name:
            return f"Created cue point '{actual_name}' at bar {bar_str}"
        return f"Created cue point at bar {bar_str}"
    except Exception as e:
        logger.error(f"Error creating cue point: {str(e)}")
        return f"Error creating cue point: {str(e)}"


@mcp.tool()
def delete_cue_point(ctx: Context, bar: int = 0, beat: float = 0.0) -> str:
    """Delete a cue point at a position.

    Parameters:
    - bar: Bar number (1-based).
    - beat: Beat position (0-based).
    """
    try:
        ableton = get_ableton_connection()
        time_val = _convert_bar_to_beat(bar, beat)
        ableton.send_command("delete_cue_point", {"time": time_val})
        if bar > 0:
            bar_str = str(bar)
        else:
            num, denom = _get_time_signature()
            bar_str = str(beat_to_bar(time_val, num, denom))
        if _readback_cue_absent(ableton, time_val):
            return f"Deleted cue point at bar {bar_str}"
        return (f"Delete request sent for bar {bar_str}, but cue still "
                f"present after readback (Live tick race)")
    except Exception as e:
        logger.error(f"Error deleting cue point: {str(e)}")
        return f"Error deleting cue point: {str(e)}"


@mcp.tool()
def create_arrangement_midi_clip(
    ctx: Context,
    track_index: int,
    start_bar: int = 0,
    end_bar: int = 0,
    start_beat: float = 0.0,
    length_beats: float = 4.0,
    name: str = "",
) -> str:
    """Create an empty MIDI clip in the arrangement.

    Parameters:
    - track_index: Track number (1-based).
    - start_bar: Start bar (1-based). Takes precedence over start_beat.
    - end_bar: End bar (1-based). Used with start_bar to compute length.
    - start_beat: Start position in beats.
    - length_beats: Clip length in beats.
    - name: Optional clip name.
    """
    try:
        ableton = get_ableton_connection()
        num, denom = _get_time_signature()

        if start_bar > 0:
            position = bar_to_beat(start_bar, num, denom)
            if end_bar > start_bar:
                length = bar_to_beat(end_bar, num, denom) - position
            else:
                length = length_beats
        else:
            position = start_beat
            length = length_beats

        ti = _to_zero_based(track_index, "track_index")
        result = ableton.send_command("create_arrangement_clip", {
            "track_index": ti,
            "position": position,
            "length": length,
            "name": name,
        })

        msg = (f"Created MIDI clip on track {track_index} at "
               f"bar {beat_to_bar(position, num, denom)}, "
               f"length {length} beats")

        overlapped = result.get("overlapped_clips", [])
        if overlapped:
            msg += f"\nWarning: overlapped existing clips: {', '.join(overlapped)}"

        return msg + _ARRANGEMENT_TIP
    except Exception as e:
        logger.error(f"Error creating arrangement MIDI clip: {str(e)}")
        return f"Error creating arrangement MIDI clip: {str(e)}"


@mcp.tool()
def create_arrangement_audio_clip(
    ctx: Context,
    track_index: int,
    file_path: str,
    start_bar: int = 0,
    start_beat: float = 0.0,
) -> str:
    """Place an audio file as a clip in the arrangement.

    Parameters:
    - track_index: Track number (1-based).
    - file_path: Path to the audio file.
    - start_bar: Start bar (1-based).
    - start_beat: Start position in beats.
    """
    try:
        ableton = get_ableton_connection()
        position = _convert_bar_to_beat(start_bar, start_beat)

        ti = _to_zero_based(track_index, "track_index")
        result = ableton.send_command("create_arrangement_audio_clip", {
            "track_index": ti,
            "position": position,
            "file_path": file_path,
        })
        return f"Created audio clip from '{file_path}' on track {track_index}" + _ARRANGEMENT_TIP
    except Exception as e:
        logger.error(f"Error creating arrangement audio clip: {str(e)}")
        return f"Error creating arrangement audio clip: {str(e)}"


@mcp.tool()
def duplicate_clip_to_arrangement(
    ctx: Context,
    track_index: int,
    clip_index: int,
    destination_bar: int = 0,
    destination_beat: float = 0.0,
) -> str:
    """Copy a session clip to the arrangement.

    Parameters:
    - track_index: Track number (1-based).
    - clip_index: Session clip slot (1-based).
    - destination_bar: Destination bar (1-based).
    - destination_beat: Destination beat.
    """
    try:
        ableton = get_ableton_connection()
        dest = _convert_bar_to_beat(destination_bar, destination_beat)

        ti = _to_zero_based(track_index, "track_index")
        ci = _to_zero_based(clip_index, "clip_index")
        result = ableton.send_command("duplicate_to_arrangement", {
            "track_index": ti,
            "clip_index": ci,
            "destination_time": dest,
        })
        return f"Duplicated session clip to arrangement on track {track_index}" + _ARRANGEMENT_TIP
    except Exception as e:
        logger.error(f"Error duplicating clip to arrangement: {str(e)}")
        return f"Error duplicating clip to arrangement: {str(e)}"


@mcp.tool()
def delete_arrangement_clip(
    ctx: Context,
    track_index: int,
    clip_index: int = 0,
    clip_name: str = "",
    from_bar: int = 0,
    to_bar: int = 0,
) -> str:
    """Delete arrangement clips, by position/name or by bar range.

    PREFER the bar range for anything but a single clip. `clip_index` is a
    position in the track's arrangement list, and that list renumbers as soon
    as a clip is removed — so deleting several by index hits the wrong clips
    after the first one, and every call still reports success. It is the same
    failure as stale track indices, one level down.

    Parameters:
    - track_index: Track number (1-based).
    - clip_index: Clip position in arrangement (1-based). Single clip only.
    - clip_name: Clip name (alternative to clip_index).
    - from_bar / to_bar: Delete every clip STARTING in this bar range
      (from_bar inclusive, to_bar exclusive), e.g. from_bar=33, to_bar=49
      clears bars 33-48. Give one and the range is open-ended on the other
      side. Takes precedence over clip_index / clip_name.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        params: dict = {"track_index": ti}

        if from_bar > 0 or to_bar > 0:
            num, denom = _get_time_signature()
            if from_bar > 0:
                params["from_time"] = bar_to_beat(from_bar, num, denom)
            if to_bar > 0:
                params["to_time"] = bar_to_beat(to_bar, num, denom)
            r = ableton.send_command("delete_arrangement_clip", params)
            count = r.get("deleted", 0)
            where = (f"bars {from_bar}-{to_bar - 1}" if from_bar and to_bar
                     else (f"from bar {from_bar}" if from_bar
                           else f"before bar {to_bar}"))
            if not count:
                return f"No arrangement clips {where} on track {track_index}"
            return (
                f"Deleted {count} arrangement clip(s) {where} on "
                f"'{r.get('track')}'" + _ARRANGEMENT_TIP
            )

        if clip_name:
            params["clip_name"] = clip_name
        elif clip_index > 0:
            params["clip_index"] = _to_zero_based(clip_index, "clip_index")
        else:
            return "Error: provide clip_index, clip_name, or from_bar/to_bar"

        ableton.send_command("delete_arrangement_clip", params)
        ref = f"'{clip_name}'" if clip_name else f"#{clip_index}"
        return f"Deleted arrangement clip {ref} on track {track_index}" + _ARRANGEMENT_TIP
    except Exception as e:
        logger.error(f"Error deleting arrangement clip: {str(e)}")
        return f"Error deleting arrangement clip: {str(e)}"


@mcp.tool()
def set_arrangement_clip_property(
    ctx: Context,
    track_index: int,
    clip_index: int = 1,
    clip_name: str = "",
    name: str = "",
    muted: bool = None,
    color: int = None,
    looping: bool = None,
    loop_start: float = None,
    loop_end: float = None,
    gain: float = None,
    pitch_coarse: int = None,
    pitch_fine: float = None,
    warping: bool = None,
    warp_mode: int = None,
) -> str:
    """Set properties on an arrangement clip.

    Parameters:
    - track_index: Track number (1-based).
    - clip_index: Clip position (1-based).
    - clip_name: Clip name (alternative to clip_index).
    - name: New clip name.
    - muted: Mute state.
    - color: Color (0x00RRGGBB).
    - looping: Loop on/off.
    - loop_start: Loop start in beats.
    - loop_end: Loop end in beats.
    - gain: Audio gain (0.0-1.0).
    - pitch_coarse: Semitone pitch shift (-48 to 48).
    - pitch_fine: Fine pitch shift (-50 to 49 cents).
    - warping: Warp on/off.
    - warp_mode: Warp mode (0-6).
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        ci = _to_zero_based(clip_index, "clip_index") if clip_index > 0 else 0

        props = {
            "name": name, "muted": muted, "color": color, "looping": looping,
            "loop_start": loop_start, "loop_end": loop_end, "gain": gain,
            "pitch_coarse": pitch_coarse, "pitch_fine": pitch_fine,
            "warping": warping, "warp_mode": warp_mode,
        }

        changes = []
        for prop_name, value in props.items():
            if value is not None and value != "":
                ableton.send_command("set_arrangement_clip_property", {
                    "track_index": ti,
                    "clip_index": ci,
                    "property": prop_name,
                    "value": value,
                })
                changes.append(f"{prop_name}={value}")

        if not changes:
            return "No properties specified to change."

        ref = f"'{clip_name}'" if clip_name else f"clip {clip_index}"
        return f"Updated {ref} on track {track_index}: {', '.join(changes)}"
    except Exception as e:
        logger.error(f"Error setting arrangement clip property: {str(e)}")
        return f"Error setting arrangement clip property: {str(e)}"


@mcp.tool()
def set_ableton_view(ctx: Context, view: str = "Arranger") -> str:
    """Switch Ableton's main view.

    Parameters:
    - view: View name. Options: Arranger, Session, Detail, Detail/Clip,
            Detail/DeviceChain, Browser.
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_view", {"view_name": view})
        return f"Switched to {view} view"
    except Exception as e:
        logger.error(f"Error setting view: {str(e)}")
        return f"Error setting view: {str(e)}"


@mcp.tool()
def control_arrangement_view(ctx: Context, action: str, track_index: int = 0) -> str:
    """Control the arrangement view.

    Parameters:
    - action: One of: zoom_in, zoom_out, scroll_left, scroll_right,
              follow_on, follow_off, collapse_track, expand_track.
    - track_index: Track number (1-based, for collapse/expand).
    """
    try:
        ableton = get_ableton_connection()
        ti = _optional_to_zero_based(track_index, "track_index")
        result = ableton.send_command("control_arrangement_view", {
            "action": action,
            "track_index": ti if ti is not None else 0,
        })
        return f"Arrangement view: {action} done"
    except Exception as e:
        logger.error(f"Error controlling arrangement view: {str(e)}")
        return f"Error controlling arrangement view: {str(e)}"


@mcp.tool()
def manage_clip_automation(
    ctx: Context,
    track_index: int,
    clip_index: int = 1,
    clip_name: str = "",
    action: str = "create",
    parameter_name: str = "volume",
) -> str:
    """Create or clear automation envelopes on a session clip.

    Live's Clip.create_automation_envelope only accepts session clips, so
    this tool resolves against the track's clip_slots.

    Parameters:
    - track_index: Track number (1-based).
    - clip_index: Session clip slot (1-based). Ignored when clip_name is set.
    - clip_name: Resolve by clip name across the track's slots.
    - action: "create", "clear", or "clear_all".
    - parameter_name: Parameter to automate. Aliases "volume" and "panning"
      map to Track Volume / Track Panning. Otherwise matched by exact
      (case-insensitive) name against mixer sends and devices on the track.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        payload = {
            "track_index": ti,
            "action": action,
            "parameter_name": parameter_name,
        }
        if clip_name:
            payload["clip_name"] = clip_name
        else:
            payload["clip_index"] = _to_zero_based(clip_index, "clip_index")

        result = ableton.send_command("manage_clip_automation", payload)

        target = clip_name or f"slot {clip_index}"
        if action == "clear_all":
            return f"Cleared all automation on {target}, track {track_index}"
        param = result.get("parameter", parameter_name)
        return f"Automation {action}: {param} on {target}, track {track_index}"
    except Exception as e:
        logger.error(f"Error managing clip automation: {str(e)}")
        return f"Error managing clip automation: {str(e)}"


# ── Device / Parameter Tools ──────────────────────────────────────

@mcp.tool()
def get_device_parameters(
    ctx: Context,
    track_index: int,
    device_index: int = 1,
    chain_index: int = 0,
    category: str = "",
    show_all: bool = False,
) -> str:
    """List parameters for a device on a track.

    Parameters:
    - track_index: Track number (1-based).
    - device_index: Device number on the track (1-based, default 1).
    - chain_index: Chain number inside a rack (1-based, 0 = no chain).
    - category: Filter by category name (returns detail for that category).
    - show_all: If True, return all parameters in detail mode.

    Default mode returns a summary grouped by category with counts.
    Specify category or show_all=True for full parameter details.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        ci = _optional_to_zero_based(chain_index, "chain_index")
        result = ableton.send_command("get_device_parameters", {
            "track_index": ti,
            "device_index": di,
            "chain_index": ci,
            "show_all": True,  # Always get full list from RS, group MCP-side
        })

        device_name = result.get("device_name", "Unknown")
        params = result.get("parameters", [])
        param_count = result.get("parameter_count", len(params))

        # Attach aliases
        for p in params:
            alias = get_alias_for_param(device_name, p["name"])
            if alias:
                p["alias"] = alias

        # Category grouping
        categories = get_categories(device_name)

        def categorize(p_name):
            if categories:
                for cat_name, prefixes in categories.items():
                    for prefix in prefixes:
                        if p_name.startswith(prefix):
                            return cat_name
            return "Other"

        # Detail mode
        if show_all or category:
            filtered = params
            if category:
                filtered = [p for p in params if categorize(p["name"]).lower() == category.lower()]
                if not filtered:
                    return "No parameters found in category '{0}'. Available categories: {1}".format(
                        category, ", ".join(sorted(set(categorize(p["name"]) for p in params))))

            lines = ["{0} — {1} parameters".format(device_name, len(filtered)), ""]
            for p in filtered:
                alias_str = " ({0})".format(p["alias"]) if p.get("alias") else ""
                enabled_str = "" if p["is_enabled"] else " [disabled]"
                lines.append("  {0}. {1}{2}: {3} (normalized {4}){5}".format(
                    p["index"] + 1, p["name"], alias_str,
                    p["display_value"], round(p["value"], 2), enabled_str))
            return "\n".join(lines)

        # Summary mode
        groups = {}
        for p in params:
            cat = categorize(p["name"])
            groups.setdefault(cat, []).append(p)

        lines = ["{0} — {1} parameters total".format(device_name, param_count), ""]
        if len(groups) == 1:
            # Only one bucket — categorization isn't informative for this device
            # (typically: device has no defined category prefixes, or all params
            # share one prefix). Skip the bucket display, point at show_all.
            lines.append("Use show_all=True for full parameter details.")
        else:
            for cat_name, cat_params in groups.items():
                lines.append("  {0}: {1} parameters".format(cat_name, len(cat_params)))
            lines.append("")
            lines.append("Use category='<name>' or show_all=True for full details.")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error getting device parameters: {str(e)}")
        return f"Error getting device parameters: {str(e)}"


@mcp.tool()
def set_device_parameter(
    ctx: Context,
    track_index: int,
    value: float,
    device_index: int = 1,
    chain_index: int = 0,
    parameter_name: str = "",
    parameter_index: int = 0,
) -> str:
    """Set a device parameter value.

    Parameters:
    - track_index: Track number (1-based).
    - value: Normalized value 0.0-1.0 (required — pass as ``value=``, not
      a synonym like ``normalized_value=``).
    - device_index: Device number (1-based, default 1).
    - chain_index: Chain number inside a rack (1-based, 0 = no chain).
    - parameter_name: Parameter name, friendly alias, or partial match.
    - parameter_index: Parameter number (1-based, alternative to name).
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        ci = _optional_to_zero_based(chain_index, "chain_index")
        pi = _optional_to_zero_based(parameter_index, "parameter_index")

        # Resolve alias if parameter_name is provided
        resolved_name = parameter_name
        alias_used = None
        if parameter_name:
            # First get device name for alias resolution
            info = ableton.send_command("get_device_parameters", {
                "track_index": ti,
                "device_index": di,
                "chain_index": ci,
                "show_all": False,
            })
            device_name = info.get("device_name", "")
            real_name = resolve_alias(device_name, parameter_name)
            if real_name:
                alias_used = parameter_name
                resolved_name = real_name

        result = ableton.send_command("set_device_parameter", {
            "track_index": ti,
            "device_index": di,
            "chain_index": ci,
            "parameter_name": resolved_name if resolved_name else None,
            "parameter_index": pi,
            "value": value,
        })

        param_name = result.get("parameter_name", "?")
        display = result.get("display_value", "?")
        new_val = result.get("new_value", value)
        clamped = result.get("clamped", False)

        msg = "Set {0} to {1} (normalized {2})".format(param_name, display, round(new_val, 2))
        if alias_used:
            msg += " [alias: {0}]".format(alias_used)
        if clamped:
            msg += " (value was clamped to 0.0-1.0 range)"
        return msg
    except Exception as e:
        logger.error(f"Error setting device parameter: {str(e)}")
        return f"Error setting device parameter: {str(e)}"


@mcp.tool()
def enable_device(
    ctx: Context,
    track_index: int,
    device_index: int = 0,
    device_name: str = "",
    chain_index: int = 0,
) -> str:
    """Enable (activate) a device on a track.

    Parameters:
    - track_index: Track number (1-based).
    - device_index: Device number (1-based). Use 0 if using device_name.
    - device_name: Device name (alternative to device_index).
    - chain_index: Chain number inside a rack (1-based, 0 = no chain).
    """
    return _toggle_device(track_index, device_index, device_name, chain_index, True)


@mcp.tool()
def disable_device(
    ctx: Context,
    track_index: int,
    device_index: int = 0,
    device_name: str = "",
    chain_index: int = 0,
) -> str:
    """Disable (bypass) a device on a track.

    Parameters:
    - track_index: Track number (1-based).
    - device_index: Device number (1-based). Use 0 if using device_name.
    - device_name: Device name (alternative to device_index).
    - chain_index: Chain number inside a rack (1-based, 0 = no chain).
    """
    return _toggle_device(track_index, device_index, device_name, chain_index, False)


def _toggle_device(track_index, device_index, device_name, chain_index, enabled):
    """Shared logic for enable/disable device."""
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _optional_to_zero_based(device_index, "device_index")
        ci = _optional_to_zero_based(chain_index, "chain_index")

        # If device_name provided, resolve to index
        if device_name and di is None:
            info = ableton.send_command("get_track_info", {"track_index": ti})
            devices = info.get("devices", [])
            matches = [d for d in devices if d["name"].lower() == device_name.lower()]
            if len(matches) == 0:
                return "Error: Device '{0}' not found on track {1}".format(device_name, track_index)
            if len(matches) > 1:
                match_list = ", ".join("{0} (index {1})".format(d["name"], d["index"] + 1) for d in matches)
                return "Error: Multiple devices named '{0}' on track {1}: {2}".format(
                    device_name, track_index, match_list)
            di = matches[0]["index"]
        elif di is None:
            di = 0

        result = ableton.send_command("set_device_enabled", {
            "track_index": ti,
            "device_index": di,
            "chain_index": ci,
            "enabled": enabled,
        })

        name = result.get("device_name", "?")
        state = "enabled" if result.get("is_active", enabled) else "disabled"
        return "{0} {1}".format(name, state)
    except Exception as e:
        logger.error(f"Error toggling device: {str(e)}")
        return f"Error toggling device: {str(e)}"


@mcp.tool()
def get_chain_info(
    ctx: Context,
    track_index: int,
    device_index: int = 1,
    chain_index: int = 0,
) -> str:
    """List chains in a rack device, or devices within a specific chain.

    Parameters:
    - track_index: Track number (1-based).
    - device_index: Device number (1-based, default 1).
    - chain_index: Chain number (1-based) to drill into. 0 = list all chains.
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        ci = _optional_to_zero_based(chain_index, "chain_index")
        result = ableton.send_command("get_chain_info", {
            "track_index": ti,
            "device_index": di,
            "chain_index": ci,
        })

        if chain_index > 0:
            # Detail for specific chain
            chain_name = result.get("chain_name", "?")
            devices = result.get("devices", [])
            lines = ["Chain '{0}' — {1} devices".format(chain_name, len(devices)), ""]
            for d in devices:
                active = "" if d.get("is_active", True) else " [disabled]"
                lines.append("  {0}. {1} ({2}, {3} params){4}".format(
                    d["index"] + 1, d["name"], d["type"],
                    d.get("parameter_count", "?"), active))
            return "\n".join(lines)
        else:
            # List all chains
            device_name = result.get("device_name", "?")
            chains = result.get("chains", [])
            lines = ["{0} — {1} chains".format(device_name, len(chains)), ""]
            for c in chains:
                mute_str = " [muted]" if c.get("mute") else ""
                solo_str = " [solo]" if c.get("solo") else ""
                dev_names = ", ".join(d["name"] for d in c.get("devices", []))
                lines.append("  {0}. {1}{2}{3}: {4} devices ({5})".format(
                    c["index"] + 1, c["name"], mute_str, solo_str,
                    c["device_count"], dev_names or "empty"))
            return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error getting chain info: {str(e)}")
        return f"Error getting chain info: {str(e)}"


@mcp.tool()
def get_drum_pad_info(ctx: Context, track_index: int, device_index: int = 1) -> str:
    """List filled drum pads in a Drum Rack.

    Parameters:
    - track_index: Track number (1-based).
    - device_index: Device number (1-based, default 1).
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        result = ableton.send_command("get_drum_pad_info", {
            "track_index": ti,
            "device_index": di,
        })

        device_name = result.get("device_name", "?")
        pads = result.get("filled_pads", [])
        lines = ["{0} — {1} filled pads".format(device_name, len(pads)), ""]
        for pad in pads:
            mute_str = " [muted]" if pad.get("mute") else ""
            solo_str = " [solo]" if pad.get("solo") else ""
            dev_names = []
            for chain in pad.get("chains", []):
                for d in chain.get("devices", []):
                    dev_names.append(d["name"])
            lines.append("  Note {0}: {1}{2}{3} → {4}".format(
                pad["note"], pad["name"], mute_str, solo_str,
                ", ".join(dev_names) or "empty"))
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error getting drum pad info: {str(e)}")
        return f"Error getting drum pad info: {str(e)}"


@mcp.tool()
def delete_device(
    ctx: Context,
    track_index: int,
    device_index: int = 0,
    device_name: str = "",
) -> str:
    """Delete a device from a track.

    Parameters:
    - track_index: Track number (1-based).
    - device_index: Device number (1-based).
    - device_name: Device name (alternative to device_index).
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _optional_to_zero_based(device_index, "device_index")

        # Resolve by name if needed
        if device_name and di is None:
            info = ableton.send_command("get_track_info", {"track_index": ti})
            devices = info.get("devices", [])
            matches = [d for d in devices if d["name"].lower() == device_name.lower()]
            if len(matches) == 0:
                return "Error: Device '{0}' not found on track {1}".format(device_name, track_index)
            if len(matches) > 1:
                match_list = ", ".join("{0} (index {1})".format(d["name"], d["index"] + 1) for d in matches)
                return "Error: Multiple devices named '{0}': {1}".format(device_name, match_list)
            di = matches[0]["index"]
        elif di is None:
            di = 0

        result = ableton.send_command("delete_device", {
            "track_index": ti,
            "device_index": di,
        })

        return "Deleted {0}. {1} devices remaining.".format(
            result.get("deleted_device", "device"),
            result.get("remaining_devices", "?"))
    except Exception as e:
        logger.error(f"Error deleting device: {str(e)}")
        return f"Error deleting device: {str(e)}"


@mcp.tool()
def get_track_deletion_status(ctx: Context) -> str:
    """Check whether session tracks can be deleted right now.

    Returns a quick safety summary so agents can avoid attempting deletes
    when Ableton's minimum-track constraint would block them.
    """
    try:
        ableton = get_ableton_connection()
        info = ableton.send_command("get_session_info")
        track_count = info.get("track_count", 0)
        max_deletions_now = max(0, track_count - 1)

        if track_count <= 1:
            return (
                "Track deletion blocked: 1 session track remaining. "
                "Ableton requires at least one session track. "
                "Create a new track before deleting."
            )

        return (
            "Track deletion available: {0} session tracks currently exist. "
            "You can delete up to {1} more track(s) before hitting Ableton's "
            "minimum-track limit."
        ).format(track_count, max_deletions_now)
    except Exception as e:
        logger.error(f"Error checking track deletion status: {str(e)}")
        return f"Error checking track deletion status: {str(e)}"


@mcp.tool()
def delete_track(
    ctx: Context,
    track_index: int = 0,
    track_name: str = "",
) -> str:
    """Delete a track from the Ableton session.

    Parameters:
    - track_index: Track number (1-based). Use 0 to resolve by name instead.
    - track_name: Track name (alternative to track_index). If both are given, track_index takes priority.
    """
    try:
        ableton = get_ableton_connection()
        info = ableton.send_command("get_session_info")
        track_count = info.get("track_count", 0)

        # Safety guard: Ableton requires at least one session track.
        if track_count <= 1:
            return (
                "Error: Cannot delete the last remaining session track. "
                "Ableton must always have at least one track. "
                "Create a new track before deleting."
            )

        # Resolve by name if no index given
        if track_index <= 0:
            if not track_name:
                return "Error: provide either track_index (1-based) or track_name."
            matched_index = None
            for i in range(track_count):
                t = ableton.send_command("get_track_info", {"track_index": i})
                if t.get("name", "").lower() == track_name.lower():
                    matched_index = i
                    break
            if matched_index is None:
                return f"Error: No track named '{track_name}' found."
            ti = matched_index
        else:
            ti = _to_zero_based(track_index, "track_index")

        result = ableton.send_command("delete_track", {"track_index": ti})
        return "Deleted track '{0}'. {1} tracks remaining.".format(
            result.get("deleted_track", "unknown"),
            result.get("remaining_tracks", "?"),
        )
    except Exception as e:
        logger.error(f"Error deleting track: {str(e)}")
        return f"Error deleting track: {str(e)}"


@mcp.tool()
def navigate_device_preset(
    ctx: Context,
    track_index: int,
    device_index: int = 1,
    chain_index: int = 0,
    direction: str = "next",
) -> str:
    """Navigate device presets (next/previous/current).

    Note: only plugin (VST/AU) devices expose presets via this API. Stock
    Live devices (Operator, Drum Rack, etc.) will return
    ``Device 'X' has no presets available``; load their factory patches via
    the browser instead (``load_instrument_or_effect`` / ``load_drum_kit``).

    Parameters:
    - track_index: Track number (1-based).
    - device_index: Device number (1-based, default 1).
    - chain_index: Chain number inside a rack (1-based, 0 = no chain).
    - direction: "next", "previous", or "current".
    """
    try:
        ableton = get_ableton_connection()
        ti = _to_zero_based(track_index, "track_index")
        di = _to_zero_based(device_index, "device_index")
        ci = _optional_to_zero_based(chain_index, "chain_index")
        result = ableton.send_command("navigate_preset", {
            "track_index": ti,
            "device_index": di,
            "chain_index": ci,
            "direction": direction,
        })

        preset_name = result.get("preset_name", "?")
        preset_idx = result.get("preset_index", 0)
        preset_count = result.get("preset_count", 0)
        device_n = result.get("device_name", "?")

        if direction == "current":
            return "{0}: current preset is '{1}' ({2}/{3})".format(
                device_n, preset_name, preset_idx + 1, preset_count)
        return "{0}: loaded preset '{1}' ({2}/{3})".format(
            device_n, preset_name, preset_idx + 1, preset_count)
    except Exception as e:
        logger.error(f"Error navigating preset: {str(e)}")
        return f"Error navigating preset: {str(e)}"


# Main execution
def main():
    """Run the MCP server"""
    mcp.run()

if __name__ == "__main__":
    main()
