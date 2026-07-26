# AbletonMCP/init.py
from __future__ import absolute_import, print_function, unicode_literals

from _Framework.ControlSurface import ControlSurface
import socket
import json
import threading
import time
import traceback
from collections import Counter

# Change queue import for Python 2
try:
    import Queue as queue  # Python 2
except ImportError:
    import queue  # Python 3

# Constants for socket communication
DEFAULT_PORT = 9877
HOST = "localhost"

# Bumped by hand whenever a command is added, removed or changes signature.
# Three copies of this integration exist at once — the repo, the script Live
# actually loaded at startup, and the MCP server process Claude is talking to —
# and they drift apart constantly, because Live only reloads a remote script on
# restart and the server only reloads when its host restarts. Every "Live can't
# do that" that later turned out to be false was traced to one of those copies
# being older than the others. `get_build_info` reports this back so the skew
# is visible instead of being rediscovered as a phantom API limit.
BUILD_ID = "2026-07-26.15"

def create_instance(c_instance):
    """Create and return the AbletonMCP script instance"""
    return AbletonMCP(c_instance)


# --- module level, next to DEFAULT_PORT / HOST at the top of the file ---

# Simpler and Sample expose these as bare ints. On the builds tested there is
# no companion list of display strings to resolve against (unlike routing types
# or grooves), so these tables are the FALLBACK mapping only: every lookup goes
# through _simpler_enum_names(), which prefers a real list off the live object
# whenever one exists. A raw index is always accepted as the escape hatch, and
# Live itself rejects out-of-range values.
SIMPLER_PLAYBACK_MODES = ("classic", "one_shot", "slicing")
SIMPLER_SLICING_PLAYBACK_MODES = ("mono", "poly", "thru")
SAMPLE_SLICING_STYLES = ("transient", "beat", "region", "manual")
SAMPLE_WARP_MODES = ("beats", "tones", "texture", "repitch",
                     "complex", "rex", "complex_pro")

# Companion-list attribute names to probe before falling back to the tables
# above. Live has never published these for Simpler on the builds tested, but
# probing costs nothing and means a future build's own strings win.
SIMPLER_ENUM_LISTS = {
    "playback_mode": ("playback_mode_list", "available_playback_modes"),
    "slicing_playback_mode": ("slicing_playback_mode_list",
                              "available_slicing_playback_modes"),
    "slicing_style": ("slicing_style_list", "available_slicing_styles"),
    "warp_mode": ("warp_mode_list", "available_warp_modes"),
}


# --- methods on the AbletonMCP control surface class ---


class AbletonMCP(ControlSurface):
    """AbletonMCP Remote Script for Ableton Live"""
    
    def __init__(self, c_instance):
        """Initialize the control surface"""
        ControlSurface.__init__(self, c_instance)
        self.log_message("AbletonMCP Remote Script initializing...")
        
        # Socket server for communication
        self.server = None
        self.client_threads = []
        self.server_thread = None
        self.running = False
        
        # In-flight arrangement-automation record pass, if any
        self._auto_rec = self._auto_rec_state_default()

        # Start the socket server
        self.start_server()
        
        self.log_message("AbletonMCP initialized")
        
        # Show a message in Ableton
        self.show_message("AbletonMCP: Listening for commands on port " + str(DEFAULT_PORT))
    
    @property
    def _song(self):
        """The document Live has open RIGHT NOW.

        This used to be captured once in __init__ and reused by ~200 call
        sites. Live replaces the Song object when a different Set is opened,
        so after the user switched documents every one of those call sites was
        addressing the previous Set — reads returned the old Set's contents
        and writes would have gone somewhere invisible.

        It was caught exactly that way: opening a Set with a full arrangement
        still reported zero arrangement clips, and get_session_overview failed
        with a raw handle error ("did not match C++ signature:
        TPyHandle<ASong>") when called during the switch. Resolving fresh
        costs nothing and removes the whole class.
        """
        return self.song()

    def disconnect(self):
        """Called when Ableton closes or the control surface is removed"""
        self.log_message("AbletonMCP disconnecting...")
        self.running = False

        # Never leave Live armed for arrangement record because a pass was
        # still in flight when the script went away.
        try:
            if getattr(self, "_auto_rec", None) and self._auto_rec.get("active"):
                self._finish_auto_rec("cancelled")
        except Exception:
            pass

        # Stop the server
        if self.server:
            try:
                self.server.close()
            except:
                pass
        
        # Wait for the server thread to exit
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(1.0)
            
        # Clean up any client threads
        for client_thread in self.client_threads[:]:
            if client_thread.is_alive():
                # We don't join them as they might be stuck
                self.log_message("Client thread still alive during disconnect")
        
        ControlSurface.disconnect(self)
        self.log_message("AbletonMCP disconnected")
    
    def start_server(self):
        """Start the socket server in a separate thread"""
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind((HOST, DEFAULT_PORT))
            self.server.listen(5)  # Allow up to 5 pending connections
            
            self.running = True
            self.server_thread = threading.Thread(target=self._server_thread)
            self.server_thread.daemon = True
            self.server_thread.start()
            
            self.log_message("Server started on port " + str(DEFAULT_PORT))
        except Exception as e:
            self.log_message("Error starting server: " + str(e))
            self.show_message("AbletonMCP: Error starting server - " + str(e))
    
    def _server_thread(self):
        """Server thread implementation - handles client connections"""
        try:
            self.log_message("Server thread started")
            # Set a timeout to allow regular checking of running flag
            self.server.settimeout(1.0)
            
            while self.running:
                try:
                    # Accept connections with timeout
                    client, address = self.server.accept()
                    self.log_message("Connection accepted from " + str(address))
                    self.show_message("AbletonMCP: Client connected")
                    
                    # Handle client in a separate thread
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client,)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                    
                    # Keep track of client threads
                    self.client_threads.append(client_thread)
                    
                    # Clean up finished client threads
                    self.client_threads = [t for t in self.client_threads if t.is_alive()]
                    
                except socket.timeout:
                    # No connection yet, just continue
                    continue
                except Exception as e:
                    if self.running:  # Only log if still running
                        self.log_message("Server accept error: " + str(e))
                    time.sleep(0.5)
            
            self.log_message("Server thread stopped")
        except Exception as e:
            self.log_message("Server thread error: " + str(e))
    
    def _handle_client(self, client):
        """Handle communication with a connected client"""
        self.log_message("Client handler started")
        client.settimeout(None)  # No timeout for client socket
        buffer = ''  # Changed from b'' to '' for Python 2
        
        try:
            while self.running:
                try:
                    # Receive data
                    data = client.recv(8192)
                    
                    if not data:
                        # Client disconnected
                        self.log_message("Client disconnected")
                        break
                    
                    # Accumulate data in buffer with explicit encoding/decoding
                    try:
                        # Python 3: data is bytes, decode to string
                        buffer += data.decode('utf-8')
                    except AttributeError:
                        # Python 2: data is already string
                        buffer += data
                    
                    try:
                        # Try to parse command from buffer
                        command = json.loads(buffer)  # Removed decode('utf-8')
                        buffer = ''  # Clear buffer after successful parse
                        
                        self.log_message("Received command: " + str(command.get("type", "unknown")))
                        
                        # Process the command and get response
                        response = self._process_command(command)
                        
                        # Send the response with explicit encoding
                        try:
                            # Python 3: encode string to bytes
                            client.sendall(json.dumps(response).encode('utf-8'))
                        except AttributeError:
                            # Python 2: string is already bytes
                            client.sendall(json.dumps(response))
                    except ValueError:
                        # Incomplete data, wait for more
                        continue
                        
                except Exception as e:
                    self.log_message("Error handling client data: " + str(e))
                    self.log_message(traceback.format_exc())
                    
                    # Send error response if possible
                    error_response = {
                        "status": "error",
                        "message": str(e)
                    }
                    try:
                        # Python 3: encode string to bytes
                        client.sendall(json.dumps(error_response).encode('utf-8'))
                    except AttributeError:
                        # Python 2: string is already bytes
                        client.sendall(json.dumps(error_response))
                    except:
                        # If we can't send the error, the connection is probably dead
                        break
                    
                    # For serious errors, break the loop
                    if not isinstance(e, ValueError):
                        break
        except Exception as e:
            self.log_message("Error in client handler: " + str(e))
        finally:
            try:
                client.close()
            except:
                pass
            self.log_message("Client handler stopped")
    
    def _process_command(self, command):
        """Process a command from the client and return a response"""
        command_type = command.get("type", "")
        params = command.get("params", {})
        
        # Initialize response
        response = {
            "status": "success",
            "result": {}
        }
        
        try:
            # Route the command to the appropriate handler
            if command_type == "get_session_info":
                response["result"] = self._get_session_info()
            elif command_type == "get_track_info":
                track_index = params.get("track_index", 0)
                response["result"] = self._get_track_info(track_index)
            elif command_type == "get_session_overview":
                response["result"] = self._get_session_overview()
            elif command_type == "get_routing_options":
                response["result"] = self._get_routing_options(
                    params.get("track_index", 0))
            elif command_type == "get_grooves":
                response["result"] = self._get_grooves()
            elif command_type == "batch":
                response["result"] = self._batch(
                    params.get("commands", []),
                    params.get("stop_on_error", True))
            elif command_type == "get_performance_report":
                response["result"] = self._get_performance_report()
            elif command_type == "get_build_info":
                response["result"] = self._get_build_info()
            elif command_type == "get_automation_record_status":
                response["result"] = self._get_automation_record_status()
            elif command_type == "inspect_lom":
                response["result"] = self._inspect_lom(
                    params.get("target", "song"),
                    params.get("track_index", None),
                    params.get("clip_index", None),
                    params.get("device_index", None),
                    params.get("scene_index", None),
                    params.get("filter", ""))
            elif command_type == "get_clip_notes":
                response["result"] = self._get_clip_notes(
                    params.get("track_index", 0),
                    params.get("clip_index", 0),
                    params.get("from_time", 0.0),
                    params.get("time_span", None),
                    params.get("from_pitch", 0),
                    params.get("pitch_span", 128),
                    params.get("arrangement", False),
                    params.get("clip_name", None))
            elif command_type == "get_meters":
                response["result"] = self._get_meters()
            elif command_type == "get_drift_modulation":
                response["result"] = self._get_drift_modulation(
                    params.get("track_index", 0),
                    params.get("device_index", 0))
            elif command_type == "get_device_modes":
                response["result"] = self._get_device_modes(
                    params.get("track_index", 0),
                    params.get("device_index", 0),
                    params.get("chain_index", None))
            elif command_type == "get_plugin_info":
                response["result"] = self._get_plugin_info(
                    params.get("track_index", 0),
                    params.get("device_index", 0),
                    params.get("chain_index", None),
                    params.get("bank", None))
            elif command_type == "get_device_io":
                response["result"] = self._get_device_io(
                    params.get("track_index", 0),
                    params.get("device_index", 0),
                    params.get("chain_index", None))
#     and must NOT go in the mutating command list. ---
            elif command_type == "get_rack_map":
                ti = params.get("track_index", 0)
                di = params.get("device_index", 0)
                response["result"] = self._get_rack_map(
                    ti, di,
                    params.get("include_devices", True),
                    params.get("pads", "filled"))
            elif command_type == "get_simpler_info":
                response["result"] = self._get_simpler_info(
                    params.get("track_index", 0),
                    params.get("device_index", 0),
                    params.get("chain_path", None))
            elif command_type == "get_application_info":
                response["result"] = self._get_application_info()
            elif command_type == "get_modulation_targets":
                response["result"] = self._get_modulation_targets(
                    params.get("track_index", 0),
                    params.get("device_index", 0))
            elif command_type == "get_clip_automation":
                response["result"] = self._get_clip_automation(
                    params.get("track_index", 0),
                    params.get("clip_index", 0))
            # Commands that modify Live's state should be scheduled on the main thread
            elif command_type in ["create_midi_track", "create_audio_track",
                                 "create_return_track", "duplicate_track",
                                 "set_send", "set_track_state",
                                 "create_scene", "set_scene_name", "fire_scene",
                                 "set_clip_properties",
                                 "set_track_routing", "set_track_monitoring",
                                 "set_crossfade_assign", "set_crossfader",
                                 "load_sample_to_drum_pad",
                                 "set_groove_amount", "apply_clip_groove",
                                 "set_transport_state", "capture_midi",
                                 "undo_redo", "set_master_volume",
                                 "set_track_fold", "select_view_target",
                                 "delete_scene", "duplicate_scene",
                                 "set_scene_tempo",
                                 "write_clip_automation",
                                 "set_clip_launch", "set_clip_follow_action",
                                 "manage_warp_markers",
                                 "modify_clip_notes", "remove_clip_notes",
                                 "manage_clip_region",
                                 "set_wavetable_oscillator", "duplicate_device",
                                 "undo_step", "transport_action",
                                 "set_mixer_extras", "set_view_detail",
                                 "set_scene_signature",
                                 "set_drift_modulation",
                                 "set_device_mode",
                                 "select_plugin_preset",
                                 "set_device_io_routing",
                                 "configure_looper",
                                 "manage_rack_variations",
                                 "set_chain_state",
                                 "manage_drum_pad",
                                 "set_simpler_playback",
                                 "manage_simpler_sample",
                                 "manage_simpler_slices",
                                 "manage_take_lanes",
                                 "edit_cue_point",
                                 "manage_groove_pool",
                                 "press_dialog_button",
                                 "control_live_view",
                                 "manage_tuning_system",
                                 "write_arrangement_automation",
                                 "record_arrangement_automation",
                                 "record_over_range",
                                 "bounce_to_audio",
                                 "delete_return_track",
                                 "import_audio_file",
                                 "insert_device",
                                 "manage_song_data",
                                 "set_song_options",
                                 "cancel_automation_record",
                                 "play_section",
                                 "call_lom", "set_device_sidechain",
                                 "set_device_modulation", "move_device",
                                 "manage_rack", "control_looper",
                                 "set_song_scale", "add_notes_extended",
                                 "set_track_name",
                                 "create_clip", "add_notes_to_clip", "set_clip_name",
                                 "set_tempo", "fire_clip", "stop_clip",
                                 "start_playback", "stop_playback", "load_browser_item",
                                 "set_song_time", "set_arrangement_loop", "jump_to_cue",
                                 "create_cue_point", "delete_cue_point",
                                 "create_arrangement_clip", "create_arrangement_audio_clip",
                                 "duplicate_to_arrangement", "delete_arrangement_clip",
                                 "set_arrangement_clip_property",
                                 "set_view", "control_arrangement_view",
                                 "manage_clip_automation",
                                 "add_notes_to_arrangement_clip",
                                 "set_device_parameter", "set_device_enabled",
                                 "delete_device", "navigate_preset",
                                 "delete_track",
                                 "set_track_volume", "set_track_panning"]:
                # Use a thread-safe approach with a response queue
                response_queue = queue.Queue()
                
                # Define a function to execute on the main thread
                def main_thread_task():
                    try:
                        result = None
                        if command_type == "create_midi_track":
                            index = params.get("index", -1)
                            result = self._create_midi_track(index)
                        elif command_type == "create_audio_track":
                            index = params.get("index", -1)
                            result = self._create_audio_track(index)
                        elif command_type == "create_return_track":
                            result = self._create_return_track()
                        elif command_type == "duplicate_track":
                            result = self._duplicate_track(params.get("track_index", 0))
                        elif command_type == "set_send":
                            result = self._set_send(
                                params.get("track_index", 0),
                                params.get("send_index", 0),
                                params.get("value", 0.0))
                        elif command_type == "set_track_state":
                            result = self._set_track_state(
                                params.get("track_index", 0),
                                params.get("mute", None),
                                params.get("solo", None),
                                params.get("arm", None),
                                params.get("color_index", None))
                        elif command_type == "create_scene":
                            result = self._create_scene(params.get("index", -1))
                        elif command_type == "set_scene_name":
                            result = self._set_scene_name(
                                params.get("scene_index", 0),
                                params.get("name", ""))
                        elif command_type == "fire_scene":
                            result = self._fire_scene(params.get("scene_index", 0))
                        elif command_type == "set_clip_properties":
                            result = self._set_clip_properties(
                                params.get("track_index", 0),
                                params.get("clip_index", 0),
                                params.get("looping", None),
                                params.get("loop_end", None),
                                params.get("gain", None),
                                params.get("warping", None),
                                params.get("color_index", None),
                                params.get("quantize_to", None),
                                params.get("quantize_amount", 1.0))
                        elif command_type == "modify_clip_notes":
                            result = self._modify_clip_notes(
                                params.get("track_index", 0),
                                params.get("clip_index", 0),
                                params.get("transpose", 0),
                                params.get("velocity_scale", None),
                                params.get("velocity_set", None),
                                params.get("humanize_ms", None),
                                params.get("probability", None),
                                params.get("from_time", 0.0),
                                params.get("time_span", None),
                                params.get("from_pitch", 0),
                                params.get("pitch_span", 128),
                                params.get("arrangement", False),
                                params.get("clip_name", None))
                        elif command_type == "remove_clip_notes":
                            result = self._remove_clip_notes(
                                params.get("track_index", 0),
                                params.get("clip_index", 0),
                                params.get("from_time", 0.0),
                                params.get("time_span", None),
                                params.get("from_pitch", 0),
                                params.get("pitch_span", 128),
                                params.get("arrangement", False),
                                params.get("clip_name", None))
                        elif command_type == "manage_clip_region":
                            result = self._manage_clip_region(
                                params.get("track_index", 0),
                                params.get("clip_index", 0),
                                params.get("action", "info"),
                                params.get("region_start", None),
                                params.get("region_end", None),
                                params.get("destination_time", None),
                                params.get("start_marker", None),
                                params.get("end_marker", None))
                        elif command_type == "set_wavetable_oscillator":
                            result = self._set_wavetable_oscillator(
                                params.get("track_index", 0),
                                params.get("device_index", 0),
                                params.get("oscillator", 1),
                                params.get("category", None),
                                params.get("wavetable", None),
                                params.get("effect_mode", None),
                                params.get("unison_mode", None),
                                params.get("unison_voices", None),
                                params.get("mono_poly", None),
                                params.get("poly_voices", None))
                        elif command_type == "duplicate_device":
                            result = self._duplicate_device(
                                params.get("track_index", 0),
                                params.get("device_index", 0))
                        elif command_type == "undo_step":
                            result = self._undo_step(params.get("action", "begin"))
                        elif command_type == "transport_action":
                            result = self._transport_action(
                                params.get("action", "stop_all_clips"),
                                params.get("value", None))
                        elif command_type == "set_mixer_extras":
                            result = self._set_mixer_extras(
                                params.get("track_index", 0),
                                params.get("track_activator", None),
                                params.get("panning_mode", None),
                                params.get("left_split_stereo", None),
                                params.get("right_split_stereo", None),
                                params.get("cue_volume", None))
                        elif command_type == "set_view_detail":
                            result = self._set_view_detail(
                                params.get("track_index", None),
                                params.get("clip_index", None),
                                params.get("device_index", None),
                                params.get("draw_mode", None))
                        elif command_type == "set_scene_signature":
                            result = self._set_scene_signature(
                                params.get("scene_index", 0),
                                params.get("numerator", None),
                                params.get("denominator", None),
                                params.get("enabled", None))
                        elif command_type == "set_drift_modulation":
                            result = self._set_drift_modulation(
                                params.get("track_index", 0),
                                params.get("device_index", 0),
                                params.get("source_1", None),
                                params.get("target_1", None),
                                params.get("source_2", None),
                                params.get("target_2", None),
                                params.get("source_3", None),
                                params.get("target_3", None),
                                params.get("filter_source_1", None),
                                params.get("filter_source_2", None),
                                params.get("lfo_source", None),
                                params.get("pitch_source_1", None),
                                params.get("pitch_source_2", None),
                                params.get("shape_source", None),
                                params.get("voice_count", None),
                                params.get("voice_mode", None),
                                params.get("pitch_bend_range", None))
                        elif command_type == "set_device_mode":
                            result = self._set_device_mode(
                                params.get("track_index", 0),
                                params.get("device_index", 0),
                                params.get("chain_index", None),
                                params.get("settings", None))
                        elif command_type == "select_plugin_preset":
                            result = self._select_plugin_preset(
                                params.get("track_index", 0),
                                params.get("device_index", 0),
                                params.get("chain_index", None),
                                params.get("preset", None),
                                params.get("preset_index", None))
                        elif command_type == "set_device_io_routing":
                            result = self._set_device_io_routing(
                                params.get("track_index", 0),
                                params.get("device_index", 0),
                                params.get("bus", None),
                                params.get("bus_index", 0),
                                params.get("chain_index", None),
                                params.get("routing_type", None),
                                params.get("routing_channel", None))
#     (~line 445) with this one, then add the three new branches. ---
                        elif command_type == "manage_rack":
                            result = self._manage_rack(
                                params.get("track_index", 0),
                                params.get("device_index", 0),
                                params.get("action", "info"),
                                params.get("value", None),
                                params.get("count", 1),
                                params.get("chain_index", None))
                        elif command_type == "manage_rack_variations":
                            result = self._manage_rack_variations(
                                params.get("track_index", 0),
                                params.get("device_index", 0),
                                params.get("action", "list"),
                                params.get("variation_index", None))
                        elif command_type == "set_chain_state":
                            result = self._set_chain_state(
                                params.get("track_index", 0),
                                params.get("device_index", 0),
                                params.get("chain_index", None),
                                params.get("chain_name", None),
                                params.get("pad_note", None),
                                params.get("return_chain", False),
                                params.get("name", None),
                                params.get("color_index", None),
                                params.get("mute", None),
                                params.get("solo", None),
                                params.get("volume", None),
                                params.get("panning", None),
                                params.get("chain_activator", None),
                                params.get("send_index", None),
                                params.get("send_value", None))
                        elif command_type == "manage_drum_pad":
                            result = self._manage_drum_pad(
                                params.get("track_index", 0),
                                params.get("device_index", 0),
                                params.get("pad_note", 36),
                                params.get("name", None),
                                params.get("mute", None),
                                params.get("solo", None),
                                params.get("choke_group", None),
                                params.get("out_note", None),
                                params.get("copy_to_note", None))
# --- The three names below MUST also be added to the
# --- (see REMOTE_COMMAND_NAMES) or they never reach these branches.
                        elif command_type == "set_simpler_playback":
                            result = self._set_simpler_playback(
                                params.get("track_index", 0),
                                params.get("device_index", 0),
                                params.get("chain_path", None),
                                params.get("playback_mode", None),
                                params.get("slicing_playback_mode", None),
                                params.get("pad_slicing", None),
                                params.get("voices", None),
                                params.get("retrigger", None),
                                params.get("pitch_bend_range", None),
                                params.get("note_pitch_bend_range", None))
                        elif command_type == "manage_simpler_sample":
                            result = self._manage_simpler_sample(
                                params.get("track_index", 0),
                                params.get("device_index", 0),
                                params.get("chain_path", None),
                                params.get("action", None),
                                params.get("start_frames", None),
                                params.get("start_beats", None),
                                params.get("end_frames", None),
                                params.get("end_beats", None),
                                params.get("gain", None),
                                params.get("warping", None),
                                params.get("warp_mode", None),
                                params.get("warp_as_beats", None))
                        elif command_type == "manage_simpler_slices":
                            result = self._manage_simpler_slices(
                                params.get("track_index", 0),
                                params.get("device_index", 0),
                                params.get("chain_path", None),
                                params.get("action", "info"),
                                params.get("at_frames", None),
                                params.get("at_beats", None),
                                params.get("to_frames", None),
                                params.get("to_beats", None),
                                params.get("slicing_style", None),
                                params.get("beat_division", None),
                                params.get("region_count", None),
                                params.get("sensitivity", None))
                        elif command_type == "manage_take_lanes":
                            result = self._manage_take_lanes(
                                params.get("track_index", 0),
                                params.get("action", "info"),
                                params.get("lane_index", None),
                                params.get("name", None),
                                params.get("include_in_playback", None))
                        elif command_type == "edit_cue_point":
                            result = self._edit_cue_point(
                                params.get("cue_index", None),
                                params.get("name", None),
                                params.get("new_name", None),
                                params.get("time", None),
                                params.get("jump", False))
                        elif command_type == "manage_groove_pool":
                            result = self._manage_groove_pool(
                                params.get("groove", None),
                                params.get("base", None),
                                params.get("quantization_amount", None),
                                params.get("timing_amount", None),
                                params.get("random_amount", None),
                                params.get("velocity_amount", None),
                                params.get("global_amount", None),
                                params.get("new_name", None))
                        elif command_type == "press_dialog_button":
                            result = self._press_dialog_button(
                                params.get("button_index", 0))
                        elif command_type == "control_live_view":
                            result = self._control_live_view(
                                params.get("action", "show"),
                                params.get("view_name", None),
                                params.get("direction", "down"),
                                params.get("modifier", False))
                        elif command_type == "manage_tuning_system":
                            result = self._manage_tuning_system(
                                params.get("action", "info"),
                                params.get("name", None))
                        elif command_type == "control_looper":
                            result = self._control_looper(
                                params.get("track_index", 0),
                                params.get("device_index", None),
                                params.get("action", "info"),
                                params.get("scene_index", None))
                        elif command_type == "configure_looper":
                            result = self._configure_looper(
                                params.get("track_index", 0),
                                params.get("device_index", None),
                                params.get("record_length", None),
                                params.get("overdub_after_record", None),
                                params.get("tempo", None))
                        elif command_type == "call_lom":
                            result = self._call_lom(
                                params.get("path", "song"),
                                params.get("member", ""),
                                params.get("args", []),
                                params.get("set_value", None),
                                params.get("has_set_value", False),
                                params.get("set_from", None))
                        elif command_type == "set_device_sidechain":
                            result = self._set_device_sidechain(
                                params.get("track_index", 0),
                                params.get("device_index", 0),
                                params.get("source_track", ""),
                                params.get("channel", None))
                        elif command_type == "set_device_modulation":
                            result = self._set_device_modulation(
                                params.get("track_index", 0),
                                params.get("device_index", 0),
                                params.get("target", ""),
                                params.get("source", ""),
                                params.get("value", 0.0))
                        elif command_type == "move_device":
                            result = self._move_device(
                                params.get("track_index", 0),
                                params.get("device_index", 0),
                                params.get("target_track_index", None),
                                params.get("position", 0))
                        elif command_type == "manage_rack":
                            result = self._manage_rack(
                                params.get("track_index", 0),
                                params.get("device_index", 0),
                                params.get("action", "info"),
                                params.get("value", None))

                        elif command_type == "set_song_scale":
                            result = self._set_song_scale(
                                params.get("root_note", None),
                                params.get("scale_name", None),
                                params.get("swing_amount", None),
                                params.get("clip_trigger_quantization", None))
                        elif command_type == "add_notes_extended":
                            result = self._add_notes_extended(
                                params.get("track_index", 0),
                                params.get("clip_index", 0),
                                params.get("notes", []),
                                params.get("replace", False),
                                params.get("arrangement", False),
                                params.get("clip_name", None))
                        elif command_type == "write_arrangement_automation":
                            result = self._write_arrangement_automation(
                                params.get("track_index", 0),
                                params.get("parameter_name", ""),
                                params.get("points", []),
                                params.get("device_index", None),
                                params.get("clear_first", True),
                                params.get("from_time", None),
                                params.get("to_time", None))
                        elif command_type == "record_arrangement_automation":
                            result = self._record_arrangement_automation(
                                params.get("track_index", 0),
                                params.get("parameter_name", ""),
                                params.get("points", []),
                                params.get("device_index", None),
                                params.get("return_to_start", True))
                        elif command_type == "record_over_range":
                            result = self._record_over_range(
                                params.get("track_index", 0),
                                params.get("from_beat", 0.0),
                                params.get("to_beat", 0.0),
                                params.get("arm_track", True),
                                params.get("return_to_start", True))
                        elif command_type == "insert_device":
                            result = self._insert_device(
                                params.get("track_index", 0),
                                params.get("device_name", ""),
                                params.get("position", None))
                        elif command_type == "manage_song_data":
                            result = self._manage_song_data(
                                params.get("action", "get"),
                                params.get("key", ""),
                                params.get("value", None),
                                params.get("track_index", None))
                        elif command_type == "set_song_options":
                            result = self._set_song_options(**params)
                        elif command_type == "import_audio_file":
                            result = self._import_audio_file(
                                params.get("track_index", 0),
                                params.get("file_path", ""),
                                params.get("clip_index", None),
                                params.get("position", None),
                                params.get("name", None))
                        elif command_type == "delete_return_track":
                            result = self._delete_return_track(
                                params.get("return_index", 0))
                        elif command_type == "bounce_to_audio":
                            result = self._bounce_to_audio(
                                params.get("from_beat", 0.0),
                                params.get("to_beat", 0.0),
                                params.get("source", "Resampling"),
                                params.get("name", None))
                        elif command_type == "cancel_automation_record":
                            result = self._cancel_automation_record()
                        elif command_type == "play_section":
                            result = self._play_section(
                                params.get("from_beat", 0.0),
                                params.get("to_beat", None),
                                params.get("loop", False),
                                params.get("play", True))
                        elif command_type == "write_clip_automation":
                            result = self._write_clip_automation(
                                params.get("track_index", 0),
                                params.get("clip_index", 0),
                                params.get("parameter_name", ""),
                                params.get("points", []),
                                params.get("device_index", None),
                                params.get("clear_first", True))
                        elif command_type == "set_clip_launch":
                            result = self._set_clip_launch(
                                params.get("track_index", 0),
                                params.get("clip_index", 0),
                                params.get("launch_mode", None),
                                params.get("launch_quantization", None),
                                params.get("legato", None),
                                params.get("velocity_amount", None))
                        elif command_type == "set_clip_follow_action":
                            result = self._set_clip_follow_action(
                                params.get("track_index", 0),
                                params.get("clip_index", 0),
                                params.get("settings", {}))
                        elif command_type == "manage_warp_markers":
                            result = self._manage_warp_markers(
                                params.get("track_index", 0),
                                params.get("clip_index", 0),
                                params.get("action", "list"),
                                params.get("beat_time", None),
                                params.get("sample_time", None),
                                params.get("warp_mode", None),
                                params.get("warping", None))
                        elif command_type == "set_track_routing":
                            result = self._set_track_routing(
                                params.get("track_index", 0),
                                params.get("input_type", None),
                                params.get("input_channel", None),
                                params.get("output_type", None),
                                params.get("output_channel", None))
                        elif command_type == "set_track_monitoring":
                            result = self._set_track_monitoring(
                                params.get("track_index", 0),
                                params.get("state", 1))
                        elif command_type == "set_crossfade_assign":
                            result = self._set_crossfade_assign(
                                params.get("track_index", 0),
                                params.get("assign", 1))
                        elif command_type == "set_crossfader":
                            result = self._set_crossfader(params.get("value", 0.5))
                        elif command_type == "load_sample_to_drum_pad":
                            result = self._load_sample_to_drum_pad(
                                params.get("track_index", 0),
                                params.get("pad_note", 36),
                                params.get("uri", ""))
                        elif command_type == "set_groove_amount":
                            result = self._set_groove_amount(params.get("value", 1.0))
                        elif command_type == "apply_clip_groove":
                            result = self._apply_clip_groove(
                                params.get("track_index", 0),
                                params.get("clip_index", 0),
                                params.get("groove_name", ""))
                        elif command_type == "set_transport_state":
                            result = self._set_transport_state(
                                params.get("metronome", None),
                                params.get("loop", None),
                                params.get("session_record", None),
                                params.get("record_mode", None),
                                params.get("punch_in", None),
                                params.get("punch_out", None))
                        elif command_type == "capture_midi":
                            result = self._capture_midi()
                        elif command_type == "undo_redo":
                            result = self._undo_redo(params.get("action", "undo"))
                        elif command_type == "set_master_volume":
                            result = self._set_master_volume(params.get("volume", 0.85))
                        elif command_type == "set_track_fold":
                            result = self._set_track_fold(
                                params.get("track_index", 0),
                                params.get("folded", True))
                        elif command_type == "select_view_target":
                            result = self._select_view_target(
                                params.get("track_index", None),
                                params.get("scene_index", None))
                        elif command_type == "delete_scene":
                            result = self._delete_scene(params.get("scene_index", 0))
                        elif command_type == "duplicate_scene":
                            result = self._duplicate_scene(params.get("scene_index", 0))
                        elif command_type == "set_scene_tempo":
                            result = self._set_scene_tempo(
                                params.get("scene_index", 0),
                                params.get("tempo", None))
                        elif command_type == "set_track_name":
                            track_index = params.get("track_index", 0)
                            name = params.get("name", "")
                            result = self._set_track_name(track_index, name)
                        elif command_type == "create_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            length = params.get("length", 4.0)
                            result = self._create_clip(track_index, clip_index, length)
                        elif command_type == "add_notes_to_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            notes = params.get("notes", [])
                            result = self._add_notes_to_clip(track_index, clip_index, notes)
                        elif command_type == "set_clip_name":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            name = params.get("name", "")
                            result = self._set_clip_name(track_index, clip_index, name)
                        elif command_type == "set_tempo":
                            tempo = params.get("tempo", 120.0)
                            result = self._set_tempo(tempo)
                        elif command_type == "fire_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            result = self._fire_clip(track_index, clip_index)
                        elif command_type == "stop_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            result = self._stop_clip(track_index, clip_index)
                        elif command_type == "start_playback":
                            result = self._start_playback()
                        elif command_type == "stop_playback":
                            result = self._stop_playback()
                        elif command_type == "load_instrument_or_effect":
                            track_index = params.get("track_index", 0)
                            uri = params.get("uri", "")
                            result = self._load_instrument_or_effect(track_index, uri)
                        elif command_type == "load_browser_item":
                            track_index = params.get("track_index", 0)
                            item_uri = params.get("item_uri", "")
                            result = self._load_browser_item(track_index, item_uri)
                        elif command_type == "set_song_time":
                            time_val = params.get("time", 0.0)
                            result = self._set_song_time(time_val)
                        elif command_type == "set_arrangement_loop":
                            enabled = params.get("enabled", True)
                            start = params.get("start", None)
                            length = params.get("length", None)
                            result = self._set_arrangement_loop(enabled, start, length)
                        elif command_type == "jump_to_cue":
                            direction = params.get("direction", None)
                            name = params.get("name", None)
                            result = self._jump_to_cue(direction, name)
                        elif command_type == "create_cue_point":
                            time_val = params.get("time", 0.0)
                            name = params.get("name", "")
                            result = self._create_cue_point(time_val, name)
                        elif command_type == "delete_cue_point":
                            time_val = params.get("time", 0.0)
                            result = self._delete_cue_point(time_val)
                        elif command_type == "create_arrangement_clip":
                            ti = params.get("track_index", 0)
                            pos = params.get("position", 0.0)
                            length = params.get("length", 4.0)
                            name = params.get("name", "")
                            result = self._create_arrangement_clip(ti, pos, length, name=name)
                        elif command_type == "create_arrangement_audio_clip":
                            ti = params.get("track_index", 0)
                            pos = params.get("position", 0.0)
                            fp = params.get("file_path", "")
                            result = self._create_arrangement_audio_clip(ti, pos, fp)
                        elif command_type == "duplicate_to_arrangement":
                            ti = params.get("track_index", 0)
                            ci = params.get("clip_index", 0)
                            dt = params.get("destination_time", 0.0)
                            result = self._duplicate_to_arrangement(ti, ci, dt)
                        elif command_type == "delete_arrangement_clip":
                            ti = params.get("track_index", 0)
                            ci = params.get("clip_index", None)
                            cn = params.get("clip_name", None)
                            result = self._delete_arrangement_clip(
                                ti, ci, cn,
                                params.get("from_time", None),
                                params.get("to_time", None))
                        elif command_type == "set_arrangement_clip_property":
                            ti = params.get("track_index", 0)
                            ci = params.get("clip_index", 0)
                            prop = params.get("property", "")
                            val = params.get("value", None)
                            result = self._set_arrangement_clip_property(ti, ci, prop, val)
                        elif command_type == "set_view":
                            vn = params.get("view_name", "Arranger")
                            result = self._set_view(vn)
                        elif command_type == "control_arrangement_view":
                            action = params.get("action", "")
                            ti = params.get("track_index", 0)
                            result = self._control_arrangement_view(action, ti)
                        elif command_type == "manage_clip_automation":
                            ti = params.get("track_index", 0)
                            ci = params.get("clip_index", None)
                            cn = params.get("clip_name", None)
                            action = params.get("action", "create")
                            pn = params.get("parameter_name", "")
                            result = self._manage_clip_automation(ti, ci, action, pn, cn)
                        elif command_type == "add_notes_to_arrangement_clip":
                            ti = params.get("track_index", 0)
                            ci = params.get("clip_index", 0)
                            notes = params.get("notes", [])
                            result = self._add_notes_to_arrangement_clip(
                                ti, ci, notes, params.get("replace", False))
                        # Device modifying commands
                        elif command_type == "set_device_parameter":
                            ti = params.get("track_index", 0)
                            di = params.get("device_index", 0)
                            ci = params.get("chain_index", None)
                            pn = params.get("parameter_name", None)
                            pi = params.get("parameter_index", None)
                            val = params.get("value", 0.0)
                            result = self._set_device_parameter(ti, di, ci, pn, pi, val)
                        elif command_type == "set_device_enabled":
                            ti = params.get("track_index", 0)
                            di = params.get("device_index", 0)
                            ci = params.get("chain_index", None)
                            enabled = params.get("enabled", True)
                            result = self._set_device_enabled(ti, di, ci, enabled)
                        elif command_type == "delete_device":
                            ti = params.get("track_index", 0)
                            di = params.get("device_index", 0)
                            result = self._delete_device(ti, di)
                        elif command_type == "delete_track":
                            ti = params.get("track_index", 0)
                            result = self._delete_track(ti)
                        elif command_type == "set_track_volume":
                            ti = params.get("track_index", 0)
                            volume = params.get("volume", 0.85)
                            result = self._set_track_volume(ti, volume)
                        elif command_type == "set_track_panning":
                            ti = params.get("track_index", 0)
                            panning = params.get("panning", 0.0)
                            result = self._set_track_panning(ti, panning)
                        elif command_type == "navigate_preset":
                            ti = params.get("track_index", 0)
                            di = params.get("device_index", 0)
                            ci = params.get("chain_index", None)
                            direction = params.get("direction", "current")
                            result = self._navigate_preset(ti, di, ci, direction)

                        # Put the result in the queue
                        response_queue.put({"status": "success", "result": result})
                    except Exception as e:
                        self.log_message("Error in main thread task: " + str(e))
                        self.log_message(traceback.format_exc())
                        response_queue.put({"status": "error", "message": str(e)})
                
                # Schedule the task to run on the main thread
                try:
                    self.schedule_message(0, main_thread_task)
                except AssertionError:
                    # If we're already on the main thread, execute directly
                    main_thread_task()
                
                # Wait for the response with a timeout
                try:
                    task_response = response_queue.get(timeout=10.0)
                    if task_response.get("status") == "error":
                        response["status"] = "error"
                        response["message"] = task_response.get("message", "Unknown error")
                    else:
                        response["result"] = task_response.get("result", {})
                except queue.Empty:
                    response["status"] = "error"
                    response["message"] = "Timeout waiting for operation to complete"
            elif command_type == "get_track_volume":
                ti = params.get("track_index", 0)
                response["result"] = self._get_track_volume(ti)
            elif command_type == "get_browser_item":
                uri = params.get("uri", None)
                path = params.get("path", None)
                response["result"] = self._get_browser_item(uri, path)
            elif command_type == "get_browser_categories":
                category_type = params.get("category_type", "all")
                response["result"] = self._get_browser_categories(category_type)
            elif command_type == "get_browser_items":
                path = params.get("path", "")
                item_type = params.get("item_type", "all")
                response["result"] = self._get_browser_items(path, item_type)
            # Add the new browser commands
            elif command_type == "get_browser_tree":
                category_type = params.get("category_type", "all")
                response["result"] = self.get_browser_tree(category_type)
            elif command_type == "get_browser_items_at_path":
                path = params.get("path", "")
                limit = params.get("limit")  # None = unlimited (backward compat)
                offset = params.get("offset", 0)
                response["result"] = self.get_browser_items_at_path(path, limit=limit, offset=offset)
            elif command_type == "get_arrangement_info":
                track_index = params.get("track_index", -1)
                response["result"] = self._get_arrangement_info(track_index)
            elif command_type == "get_cue_points":
                response["result"] = self._get_cue_points()
            # Device read-only commands
            elif command_type == "get_device_parameters":
                ti = params.get("track_index", 0)
                di = params.get("device_index", 0)
                ci = params.get("chain_index", None)
                show_all = params.get("show_all", False)
                response["result"] = self._get_device_parameters(ti, di, ci, show_all)
            elif command_type == "get_chain_info":
                ti = params.get("track_index", 0)
                di = params.get("device_index", 0)
                ci = params.get("chain_index", None)
                response["result"] = self._get_chain_info(ti, di, ci)
            elif command_type == "get_drum_pad_info":
                ti = params.get("track_index", 0)
                di = params.get("device_index", 0)
                response["result"] = self._get_drum_pad_info(ti, di)
            else:
                response["status"] = "error"
                response["message"] = "Unknown command: " + command_type
        except Exception as e:
            self.log_message("Error processing command: " + str(e))
            self.log_message(traceback.format_exc())
            response["status"] = "error"
            response["message"] = str(e)
        
        return response
    
    # Arrangement helper methods

    def _get_arrangement_clip_info(self, clip):
        """Serialize a Clip object to ArrangementClipInfo dict."""
        try:
            return {
                "name": clip.name,
                "start_time": clip.start_time,
                "end_time": clip.end_time,
                "length": clip.end_time - clip.start_time,
                "is_midi": clip.is_midi_clip,
                "is_audio": clip.is_audio_clip,
                "muted": clip.muted,
                "color": clip.color,
                "looping": clip.looping,
                "loop_start": clip.loop_start,
                "loop_end": clip.loop_end,
            }
        except Exception as e:
            self.log_message("Error getting arrangement clip info: " + str(e))
            return {"name": "unknown", "error": str(e)}

    def _get_transport_info(self):
        """Serialize Song transport state to TransportInfo dict."""
        try:
            return {
                "is_playing": self._song.is_playing,
                "tempo": self._song.tempo,
                "signature_numerator": self._song.signature_numerator,
                "signature_denominator": self._song.signature_denominator,
                "current_time": self._song.current_song_time,
                "song_length": self._song.song_length,
                "loop_enabled": self._song.loop,
                "loop_start": self._song.loop_start,
                "loop_length": self._song.loop_length,
                "arrangement_overdub": self._song.arrangement_overdub,
                "back_to_arranger": self._song.back_to_arranger,
            }
        except Exception as e:
            self.log_message("Error getting transport info: " + str(e))
            raise

    def _resolve_arrangement_clip(self, track_index, clip_index=None, clip_name=None):
        """Resolve an arrangement clip by index or name.

        Returns (track, clip) tuple.
        """
        if track_index < 0 or track_index >= len(self._song.tracks):
            raise IndexError("Track index {0} out of range (0-{1})".format(
                track_index, len(self._song.tracks) - 1))

        track = self._song.tracks[track_index]
        clips = track.arrangement_clips

        if clip_name:
            matches = [(i, c) for i, c in enumerate(clips) if c.name == clip_name]
            if len(matches) == 0:
                raise ValueError("No arrangement clip named '{0}' on track '{1}'".format(
                    clip_name, track.name))
            if len(matches) > 1:
                raise ValueError("Ambiguous: {0} clips named '{1}' on track '{2}'".format(
                    len(matches), clip_name, track.name))
            return track, matches[0][1]

        if clip_index is None:
            raise ValueError("Either clip_index or clip_name must be provided")

        if clip_index < 0 or clip_index >= len(clips):
            raise IndexError("Clip index {0} out of range (0-{1}) on track '{2}'".format(
                clip_index, len(clips) - 1, track.name))

        return track, clips[clip_index]

    def _check_overlap(self, track, position, length):
        """Return list of clip names that overlap with [position, position+length)."""
        overlapped = []
        end = position + length
        for clip in track.arrangement_clips:
            if clip.start_time < end and clip.end_time > position:
                overlapped.append(clip.name or "(unnamed)")
        return overlapped

    # Command implementations

    def _get_session_info(self):
        """Get information about the current session"""
        try:
            result = {
                "tempo": self._song.tempo,
                "signature_numerator": self._song.signature_numerator,
                "signature_denominator": self._song.signature_denominator,
                "track_count": len(self._song.tracks),
                "return_track_count": len(self._song.return_tracks),
                "master_track": {
                    "name": "Master",
                    "volume": self._song.master_track.mixer_device.volume.value,
                    "panning": self._song.master_track.mixer_device.panning.value
                }
            }
            return result
        except Exception as e:
            self.log_message("Error getting session info: " + str(e))
            raise
    
    def _get_track_info(self, track_index):
        """Get information about a track"""
        try:
            track = self._track_at(track_index)

            # Get clip slots
            clip_slots = []
            for slot_index, slot in enumerate(track.clip_slots):
                clip_info = None
                if slot.has_clip:
                    clip = slot.clip
                    clip_info = {
                        "name": clip.name,
                        "length": clip.length,
                        "is_playing": clip.is_playing,
                        "is_recording": clip.is_recording
                    }
                
                clip_slots.append({
                    "index": slot_index,
                    "has_clip": slot.has_clip,
                    "clip": clip_info
                })
            
            # Get devices
            devices = []
            for device_index, device in enumerate(track.devices):
                devices.append({
                    "index": device_index,
                    "name": device.name,
                    "class_name": device.class_name,
                    "type": self._get_device_type(device)
                })
            
            is_group = bool(getattr(track, "is_foldable", False))

            result = {
                "index": track_index,
                "name": track.name,
                "is_audio_track": track.has_audio_input,
                "is_midi_track": track.has_midi_input,
                "is_group_track": is_group,
                "mute": track.mute,
                "solo": track.solo,
                "arm": None if is_group else track.arm,
                "volume": track.mixer_device.volume.value,
                "panning": track.mixer_device.panning.value,
                "clip_slots": clip_slots,
                "devices": devices
            }
            return result
        except Exception as e:
            self.log_message("Error getting track info: " + str(e))
            raise
    
    def _create_midi_track(self, index):
        """Create a new MIDI track at the specified index"""
        try:
            # Create the track
            self._song.create_midi_track(index)
            
            # Get the new track
            new_track_index = len(self._song.tracks) - 1 if index == -1 else index
            new_track = self._song.tracks[new_track_index]
            
            result = {
                "index": new_track_index,
                "name": new_track.name
            }
            return result
        except Exception as e:
            self.log_message("Error creating MIDI track: " + str(e))
            raise
    
    
    def _set_track_name(self, track_index, name):
        """Set the name of a track"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            # Set the name
            track = self._song.tracks[track_index]
            track.name = name
            
            result = {
                "name": track.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting track name: " + str(e))
            raise

    def _get_track_volume(self, track_index):
        """Get current volume and panning for a track's mixer fader."""
        try:
            all_tracks = list(self._song.tracks) + list(self._song.return_tracks)
            if track_index < 0 or track_index >= len(all_tracks):
                raise IndexError("Track index {0} out of range (0-{1})".format(
                    track_index, len(all_tracks) - 1))
            track = all_tracks[track_index]
            vol_param = track.mixer_device.volume
            pan_param = track.mixer_device.panning
            return {
                "track_name": track.name,
                "volume": vol_param.value,
                "volume_min": vol_param.min,
                "volume_max": vol_param.max,
                "panning": pan_param.value,
                "panning_min": pan_param.min,
                "panning_max": pan_param.max,
            }
        except Exception as e:
            self.log_message("Error getting track volume: " + str(e))
            raise

    def _set_track_volume(self, track_index, volume):
        """Set the mixer fader volume for a track.
        
        Args:
            track_index: 0-based track index (includes return tracks after session tracks)
            volume: normalized 0.0 (silence) to 1.0 (max). 0.85 = 0dB unity gain.
        """
        try:
            all_tracks = list(self._song.tracks) + list(self._song.return_tracks)
            if track_index < 0 or track_index >= len(all_tracks):
                raise IndexError("Track index {0} out of range (0-{1})".format(
                    track_index, len(all_tracks) - 1))
            track = all_tracks[track_index]
            vol_param = track.mixer_device.volume
            # Clamp to valid range
            clamped = max(vol_param.min, min(vol_param.max, float(volume)))
            vol_param.value = clamped
            return {
                "track_name": track.name,
                "volume": vol_param.value,
            }
        except Exception as e:
            self.log_message("Error setting track volume: " + str(e))
            raise

    def _set_track_panning(self, track_index, panning):
        """Set the mixer panning for a track.
        
        Args:
            track_index: 0-based track index
            panning: -1.0 (full left) to +1.0 (full right), 0.0 = center
        """
        try:
            all_tracks = list(self._song.tracks) + list(self._song.return_tracks)
            if track_index < 0 or track_index >= len(all_tracks):
                raise IndexError("Track index {0} out of range (0-{1})".format(
                    track_index, len(all_tracks) - 1))
            track = all_tracks[track_index]
            pan_param = track.mixer_device.panning
            clamped = max(pan_param.min, min(pan_param.max, float(panning)))
            pan_param.value = clamped
            return {
                "track_name": track.name,
                "panning": pan_param.value,
            }
        except Exception as e:
            self.log_message("Error setting track panning: " + str(e))
            raise

    def _create_clip(self, track_index, clip_index, length):
        """Create a new MIDI clip in the specified track and clip slot"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            # Check if the clip slot already has a clip
            if clip_slot.has_clip:
                raise Exception("Clip slot already has a clip")
            
            # Create the clip
            clip_slot.create_clip(length)
            
            result = {
                "name": clip_slot.clip.name,
                "length": clip_slot.clip.length
            }
            return result
        except Exception as e:
            self.log_message("Error creating clip: " + str(e))
            raise
    
    def _add_notes_to_clip(self, track_index, clip_index, notes):
        """Add MIDI notes to a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip = clip_slot.clip
            
            # Convert note data to Live's format
            live_notes = []
            for note in notes:
                pitch = note.get("pitch", 60)
                start_time = note.get("start_time", 0.0)
                duration = note.get("duration", 0.25)
                velocity = note.get("velocity", 100)
                mute = note.get("mute", False)
                
                live_notes.append((pitch, start_time, duration, velocity, mute))
            
            # Add the notes
            clip.set_notes(tuple(live_notes))
            
            result = {
                "note_count": len(notes)
            }
            return result
        except Exception as e:
            self.log_message("Error adding notes to clip: " + str(e))
            raise
    
    def _set_clip_name(self, track_index, clip_index, name):
        """Set the name of a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip = clip_slot.clip
            clip.name = name
            
            result = {
                "name": clip.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting clip name: " + str(e))
            raise
    
    def _set_tempo(self, tempo):
        """Set the tempo of the session"""
        try:
            self._song.tempo = tempo
            
            result = {
                "tempo": self._song.tempo
            }
            return result
        except Exception as e:
            self.log_message("Error setting tempo: " + str(e))
            raise
    
    def _fire_clip(self, track_index, clip_index):
        """Fire a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip_slot.fire()
            
            result = {
                "fired": True
            }
            return result
        except Exception as e:
            self.log_message("Error firing clip: " + str(e))
            raise
    
    def _stop_clip(self, track_index, clip_index):
        """Stop a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            clip_slot.stop()
            
            result = {
                "stopped": True
            }
            return result
        except Exception as e:
            self.log_message("Error stopping clip: " + str(e))
            raise
    
    
    def _start_playback(self):
        """Start playing the session"""
        try:
            self._song.start_playing()
            
            result = {
                "playing": self._song.is_playing
            }
            return result
        except Exception as e:
            self.log_message("Error starting playback: " + str(e))
            raise
    
    def _stop_playback(self):
        """Stop playing the session"""
        try:
            self._song.stop_playing()
            
            result = {
                "playing": self._song.is_playing
            }
            return result
        except Exception as e:
            self.log_message("Error stopping playback: " + str(e))
            raise

    # Browser helper methods

    def _normalize_browser_category_name(self, category_name):
        """Normalize browser category names for robust matching."""
        try:
            normalized = (category_name or "").strip().lower()
            normalized = normalized.replace("-", "_").replace(" ", "_")
            while "__" in normalized:
                normalized = normalized.replace("__", "_")
            normalized = normalized.strip("_")

            # Canonical category aliases
            alias_map = {
                "instrument": "instruments",
                "sound": "sounds",
                "drum": "drums",
                "audioeffects": "audio_effects",
                "audio_fx": "audio_effects",
                "audiofx": "audio_effects",
                "midieffects": "midi_effects",
                "midi_fx": "midi_effects",
                "midifx": "midi_effects",
                "plugin": "plugins",
                "vst": "plugins",
                "vst2": "plugins",
                "vst3": "plugins",
                "au": "plugins",
            }

            if normalized in alias_map:
                return alias_map[normalized]

            compact = normalized.replace("_", "")
            if compact in alias_map:
                return alias_map[compact]

            return normalized
        except Exception:
            return (category_name or "").strip().lower()

    def _split_browser_path(self, path):
        """Split a browser path into normalized path parts."""
        if not path:
            return []
        return [part.strip() for part in path.split("/") if part and part.strip()]

    def _resolve_browser_root_category(self, browser, root_category, browser_attrs=None):
        """Resolve a root browser category to the corresponding browser item."""
        normalized_root = self._normalize_browser_category_name(root_category)

        standard_roots = {
            "instruments": "instruments",
            "sounds": "sounds",
            "drums": "drums",
            "audio_effects": "audio_effects",
            "midi_effects": "midi_effects",
            "plugins": "plugins",
        }

        attr_name = standard_roots.get(normalized_root)
        if attr_name and hasattr(browser, attr_name):
            return getattr(browser, attr_name), attr_name

        attrs = browser_attrs if browser_attrs is not None else [a for a in dir(browser) if not a.startswith('_')]
        for attr in attrs:
            if self._normalize_browser_category_name(attr) == normalized_root:
                try:
                    return getattr(browser, attr), attr
                except Exception as e:
                    self.log_message("Error accessing browser attribute {0}: {1}".format(attr, str(e)))

        return None, normalized_root
    
    def _get_browser_item(self, uri, path):
        """Get a browser item by URI or path"""
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            result = {
                "uri": uri,
                "path": path,
                "found": False
            }
            
            # Try to find by URI first if provided
            if uri:
                item = self._find_browser_item_by_uri(app.browser, uri)
                if item:
                    result["found"] = True
                    result["item"] = {
                        "name": item.name,
                        "is_folder": item.is_folder,
                        "is_device": item.is_device,
                        "is_loadable": item.is_loadable,
                        "uri": item.uri
                    }
                    return result
            
            # If URI not provided or not found, try by path
            if path:
                # Parse the path and navigate to the specified item
                path_parts = self._split_browser_path(path)
                if not path_parts:
                    result["error"] = "Invalid path"
                    return result
                
                # Determine the root based on the first part
                current_item, resolved_attr = self._resolve_browser_root_category(app.browser, path_parts[0])
                if current_item is None:
                    # Default to instruments if not specified
                    current_item = app.browser.instruments
                    # Don't skip the first part in this case
                    path_parts = ["instruments"] + path_parts
                elif resolved_attr != "instruments":
                    # Keep path parts aligned with resolved root
                    path_parts[0] = resolved_attr
                
                # Navigate through the path
                for i in range(1, len(path_parts)):
                    part = path_parts[i]
                    if not part:  # Skip empty parts
                        continue
                    
                    found = False
                    for child in current_item.children:
                        if child.name.lower() == part.lower():
                            current_item = child
                            found = True
                            break
                    
                    if not found:
                        result["error"] = "Path part '{0}' not found".format(part)
                        return result
                
                # Found the item
                result["found"] = True
                result["item"] = {
                    "name": current_item.name,
                    "is_folder": current_item.is_folder,
                    "is_device": current_item.is_device,
                    "is_loadable": current_item.is_loadable,
                    "uri": current_item.uri
                }
            
            return result
        except Exception as e:
            self.log_message("Error getting browser item: " + str(e))
            self.log_message(traceback.format_exc())
            raise   
    
    
    
    def _load_browser_item(self, track_index, item_uri):
        """Load a browser item onto a track by its URI"""
        try:
            track = self._track_at(track_index)

            # Access the application's browser instance instead of creating a new one
            app = self.application()
            
            # Find the browser item by URI
            item = self._find_browser_item_by_uri(app.browser, item_uri)
            
            if not item:
                raise ValueError("Browser item with URI '{0}' not found".format(item_uri))
            
            # Select the track
            self._song.view.selected_track = track

            devices_before = [d.name for d in tuple(track.devices)]

            # Load the item
            app.browser.load_item(item)

            devices_after = [d.name for d in tuple(track.devices)]
            remaining = Counter(devices_before)
            new_devices = []
            for name in devices_after:
                if remaining[name] > 0:
                    remaining[name] -= 1
                else:
                    new_devices.append(name)

            result = {
                "loaded": True,
                "item_name": item.name,
                "track_name": track.name,
                "uri": item_uri,
                "devices_after": devices_after,
                "new_devices": new_devices,
            }
            return result
        except Exception as e:
            self.log_message("Error loading browser item: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
    
    def _find_browser_item_by_uri(self, browser_or_item, uri, max_depth=10, current_depth=0):
        """Find a browser item by its URI"""
        try:
            # Check if this is the item we're looking for
            if hasattr(browser_or_item, 'uri') and browser_or_item.uri == uri:
                return browser_or_item
            
            # Stop recursion if we've reached max depth
            if current_depth >= max_depth:
                return None
            
            # Check if this is a browser with root categories
            if hasattr(browser_or_item, 'instruments'):
                # Check all main categories
                categories = [
                    browser_or_item.instruments,
                    browser_or_item.sounds,
                    browser_or_item.drums,
                    browser_or_item.audio_effects,
                    browser_or_item.midi_effects,
                    browser_or_item.plugins,
                ]
                
                for category in categories:
                    item = self._find_browser_item_by_uri(category, uri, max_depth, current_depth + 1)
                    if item:
                        return item
                
                return None
            
            # Check if this item has children
            if hasattr(browser_or_item, 'children') and browser_or_item.children:
                for child in browser_or_item.children:
                    item = self._find_browser_item_by_uri(child, uri, max_depth, current_depth + 1)
                    if item:
                        return item
            
            return None
        except Exception as e:
            self.log_message("Error finding browser item by URI: {0}".format(str(e)))
            return None
    
    # Arrangement command handlers

    def _get_arrangement_info(self, track_index):
        """Get arrangement clips and transport for one or all tracks."""
        try:
            transport = self._get_transport_info()
            tracks_data = []

            if track_index == -1:
                tracks = list(enumerate(self._song.tracks))
            else:
                if track_index < 0 or track_index >= len(self._song.tracks):
                    raise IndexError("Track index out of range")
                tracks = [(track_index, self._song.tracks[track_index])]

            for idx, track in tracks:
                is_group = bool(getattr(track, "is_foldable", False))
                if is_group and track_index == -1:
                    continue

                clips = []
                if not is_group:
                    for ci, clip in enumerate(track.arrangement_clips):
                        clip_info = self._get_arrangement_clip_info(clip)
                        clip_info["index"] = ci
                        clips.append(clip_info)

                tracks_data.append({
                    "index": idx,
                    "name": track.name,
                    "is_midi": track.has_midi_input,
                    "is_audio": track.has_audio_input,
                    "is_group_track": is_group,
                    "arrangement_clips": clips,
                    "clip_count": len(clips),
                })

            return {"transport": transport, "tracks": tracks_data}
        except Exception as e:
            self.log_message("Error getting arrangement info: " + str(e))
            raise

    def _get_cue_points(self):
        """Get all cue points."""
        try:
            cue_points = []
            for cp in tuple(self._song.cue_points):
                cue_points.append({"name": cp.name, "time": cp.time})
            return {"cue_points": cue_points}
        except Exception as e:
            self.log_message("Error getting cue points: " + str(e))
            raise

    def _set_song_time(self, time):
        """Set playback position."""
        try:
            self._song.current_song_time = time
            return {"time": self._song.current_song_time}
        except Exception as e:
            self.log_message("Error setting song time: " + str(e))
            raise

    def _set_arrangement_loop(self, enabled, start=None, length=None):
        """Set arrangement loop state and region."""
        try:
            self._song.loop = enabled
            if start is not None:
                self._song.loop_start = start
            if length is not None:
                self._song.loop_length = length
            return {
                "enabled": self._song.loop,
                "start": self._song.loop_start,
                "length": self._song.loop_length,
            }
        except Exception as e:
            self.log_message("Error setting arrangement loop: " + str(e))
            raise

    def _jump_to_cue(self, direction=None, name=None):
        """Jump to cue point by direction or name."""
        try:
            if direction == "next":
                self._song.jump_to_next_cue()
                return {"direction": "next", "time": self._song.current_song_time}
            elif direction == "prev":
                self._song.jump_to_prev_cue()
                return {"direction": "prev", "time": self._song.current_song_time}
            elif name:
                for cp in tuple(self._song.cue_points):
                    if cp.name == name:
                        cp.jump()
                        return {"name": cp.name, "time": cp.time}
                raise ValueError("Cue point '{0}' not found".format(name))
            else:
                raise ValueError("Provide direction ('next'/'prev') or name")
        except Exception as e:
            self.log_message("Error jumping to cue: " + str(e))
            raise

    def _create_cue_point(self, time, name=""):
        """Create a cue point at the given time.

        If ``name`` is provided, the locator is renamed after creation;
        rename failures are logged.
        """
        try:
            for cp in tuple(self._song.cue_points):
                if abs(cp.time - time) < 0.01:
                    raise ValueError("Cue point already exists at this position: " + cp.name)
            self._song.current_song_time = time

            def _finalize():
                try:
                    self._song.set_or_delete_cue()
                    if name:
                        for cp in tuple(self._song.cue_points):
                            if abs(cp.time - time) < 0.01:
                                try:
                                    cp.name = name
                                except (AttributeError, RuntimeError) as e:
                                    self.log_message(
                                        "CuePoint.name assignment failed: " + str(e))
                                break
                except Exception as e:
                    self.log_message("Error finalizing cue point: " + str(e))

            self.schedule_message(1, _finalize)
            return {"time": time, "name": name}
        except Exception as e:
            self.log_message("Error creating cue point: " + str(e))
            raise

    def _delete_cue_point(self, time):
        """Delete a cue point at the given time."""
        try:
            found = False
            for cp in tuple(self._song.cue_points):
                if abs(cp.time - time) < 0.01:
                    found = True
                    break
            if not found:
                raise ValueError("No cue point at this position")
            self._song.current_song_time = time

            def _finalize():
                try:
                    self._song.set_or_delete_cue()
                except Exception as e:
                    self.log_message("Error finalizing cue delete: " + str(e))

            self.schedule_message(1, _finalize)
            return {"deleted": True}
        except Exception as e:
            self.log_message("Error deleting cue point: " + str(e))
            raise

    def _validate_not_return_or_master(self, track_index):
        """Raise if track is a return, master, or group (foldable) track."""
        track = self._song.tracks[track_index]
        # Return tracks and master track are separate in the Live API,
        # but if accessed via tracks list they are regular tracks.
        # Check by comparing against return_tracks and master_track.
        for rt in self._song.return_tracks:
            if track == rt:
                raise ValueError("Cannot create arrangement clips on return track '{0}'".format(track.name))
        if track == self._song.master_track:
            raise ValueError("Cannot create arrangement clips on master track")
        # Group tracks are foldable. They have no usable clip_slots/arrangement_clips
        # for our purposes — the Live 11 fallback would iterate to no avail.
        if getattr(track, "is_foldable", False):
            raise ValueError("Cannot create arrangement clips on group track '{0}'".format(track.name))

    def _create_arrangement_clip(self, track_index, position, length, name=""):
        """Create MIDI clip in arrangement.

        Live 12 exposes Track.create_midi_clip(start_time, length) directly.
        Live 11 has no such method, so round-trip through a session slot:
        create_clip -> duplicate_clip_to_arrangement -> delete the session clip.
        """
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            self._validate_not_return_or_master(track_index)
            track = self._song.tracks[track_index]
            overlapped = self._check_overlap(track, position, length)

            create_midi_clip = getattr(track, "create_midi_clip", None)
            if create_midi_clip is not None:
                # Live 12: LOM does not document a return value for
                # Track.create_midi_clip, so don't trust it — scan instead.
                create_midi_clip(position, length)
                new_clip = None
            else:
                new_clip = self._create_arrangement_clip_via_session(track, position, length)

            if new_clip is None:
                for clip in tuple(track.arrangement_clips):
                    if abs(clip.start_time - position) < 0.01:
                        new_clip = clip
                        break

            if new_clip is not None and name:
                new_clip.name = name

            if new_clip is not None:
                result = self._get_arrangement_clip_info(new_clip)
            else:
                result = {"start_time": position, "length": length, "is_midi": True}
            result["overlapped_clips"] = overlapped
            return result
        except Exception as e:
            self.log_message("Error creating arrangement clip: " + str(e))
            raise

    def _create_arrangement_clip_via_session(self, track, position, length):
        slot_index = -1
        for i, slot in enumerate(track.clip_slots):
            if not slot.has_clip:
                slot_index = i
                break
        if slot_index < 0:
            raise RuntimeError(
                "No empty session clip slot available on track '{0}'; "
                "Live 11 needs one free slot to stage an arrangement MIDI clip".format(
                    getattr(track, "name", "?")))
        slot = track.clip_slots[slot_index]
        slot.create_clip(length)
        try:
            return track.duplicate_clip_to_arrangement(slot.clip, position)
        finally:
            slot.delete_clip()

    def _import_audio_file(self, track_index, file_path, clip_index=None,
                           position=None, name=None):
        """Load an audio file into a session slot, or into the arrangement.

        The bridge that was missing. `ClipSlot.create_audio_clip` has always
        existed, but only the arrangement path was ever wrapped, so a
        generated or recorded file could be placed on the timeline and never
        auditioned in a session slot. The ElevenLabs subsystem in this repo
        documents an `import_audio_file` tool for exactly this and it was
        never built on the Ableton side.
        """
        track = self._track_at(track_index)
        if not getattr(track, "has_audio_input", False):
            raise ValueError(
                "'{0}' is not an audio track — an audio file needs one. Use "
                "load_sample_to_drum_pad or manage_simpler_sample to put a "
                "sample inside an instrument on a MIDI track.".format(
                    track.name))

        if clip_index is None and position is None:
            raise ValueError("Give clip_index (session) or position (arrangement)")

        if clip_index is not None:
            slots = track.clip_slots
            if clip_index < 0 or clip_index >= len(slots):
                raise IndexError(
                    "Clip slot {0} out of range (0-{1})".format(
                        clip_index, len(slots) - 1))
            slot = slots[clip_index]
            if slot.has_clip:
                raise ValueError(
                    "Slot {0} on '{1}' already holds '{2}' — delete it first "
                    "rather than overwriting silently".format(
                        clip_index, track.name, slot.clip.name))
            slot.create_audio_clip(file_path)
            clip = slot.clip
            if name:
                try:
                    clip.name = name
                except Exception:
                    pass
            return {"where": "session", "track": track.name,
                    "clip_index": clip_index, "name": clip.name,
                    "length": clip.length, "file_path": file_path}

        track.create_audio_clip(file_path, float(position))
        placed = None
        for clip in track.arrangement_clips:
            if abs(clip.start_time - float(position)) < 0.01:
                placed = clip
                break
        if placed is not None and name:
            try:
                placed.name = name
            except Exception:
                pass
        return {"where": "arrangement", "track": track.name,
                "position": float(position), "file_path": file_path,
                "name": getattr(placed, "name", None),
                "length": getattr(placed, "length", None)}

    def _create_arrangement_audio_clip(self, track_index, position, file_path):
        """Create audio clip in arrangement from file."""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            self._validate_not_return_or_master(track_index)
            track = self._song.tracks[track_index]
            track.create_audio_clip(file_path, position)
            # Find the newly created clip
            result = {"start_time": position, "file_path": file_path, "is_audio": True}
            for clip in track.arrangement_clips:
                if abs(clip.start_time - position) < 0.01:
                    result = self._get_arrangement_clip_info(clip)
                    break
            return result
        except Exception as e:
            self.log_message("Error creating arrangement audio clip: " + str(e))
            raise

    def _duplicate_to_arrangement(self, track_index, clip_index, destination_time):
        """Duplicate a session clip to arrangement."""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            track = self._song.tracks[track_index]
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip slot index out of range")
            slot = track.clip_slots[clip_index]
            if not slot.has_clip:
                raise ValueError("No clip in slot {0}".format(clip_index))
            clip = slot.clip
            track.duplicate_clip_to_arrangement(clip, destination_time)
            # Find the duplicated clip
            result = {"start_time": destination_time, "duplicated": True}
            for arr_clip in track.arrangement_clips:
                if abs(arr_clip.start_time - destination_time) < 0.01:
                    result = self._get_arrangement_clip_info(arr_clip)
                    result["duplicated"] = True
                    break
            return result
        except Exception as e:
            self.log_message("Error duplicating to arrangement: " + str(e))
            raise

    def _add_notes_to_arrangement_clip(self, track_index, clip_index, notes,
                                       replace=False):
        """Add MIDI notes to an arrangement clip.

        Kept for wire compatibility; delegates to `_add_notes_extended` with
        arrangement=True, which is the supported path and the one the MCP tool
        layer exposes.

        **Behaviour change (2026-07-26):** this used to call `clip.set_notes`,
        which *replaces* every note in the clip rather than adding to it —
        directly contradicting its own name, and destructive on any clip that
        already had content. It now adds, and drops the existing notes only
        when replace=True is asked for explicitly. It also no longer silently
        discards probability and velocity_deviation. Nothing depended on the
        old behaviour: the command had no MCP tool, so it was unreachable.
        """
        return self._add_notes_extended(
            track_index, clip_index, notes, replace=replace, arrangement=True)

    def _delete_arrangement_clip(self, track_index, clip_index=None,
                                 clip_name=None, from_time=None, to_time=None):
        """Delete arrangement clips, by position/name or by time range.

        Positional indexing into `arrangement_clips` is fragile in exactly the
        way track indices are: the collection renumbers the moment anything is
        removed, so deleting several clips by index walks off target after the
        first one — and every call still reports success. A time range is both
        stable under mutation and how arrangement edits are actually thought
        about ("clear bars 33 to 49").
        """
        try:
            if from_time is None and to_time is None:
                track, clip = self._resolve_arrangement_clip(
                    track_index, clip_index, clip_name)
                name = clip.name
                track.delete_clip(clip)
                return {"deleted": 1, "names": [name]}

            track = self._track_at(track_index)
            lo = float(from_time) if from_time is not None else float("-inf")
            hi = float(to_time) if to_time is not None else float("inf")
            if hi <= lo:
                raise ValueError("to_time must be greater than from_time")

            # Re-scan the collection after every delete rather than deleting
            # from a snapshot: the proxies in a stale snapshot may no longer
            # refer to what they did, which is the same trap one level down.
            names = []
            while True:
                target = None
                for clip in tuple(track.arrangement_clips):
                    if lo <= clip.start_time < hi:
                        target = clip
                        break
                if target is None:
                    break
                names.append(target.name)
                track.delete_clip(target)
                if len(names) > 5000:
                    raise RuntimeError(
                        "refusing to delete more than 5000 clips in one call")

            return {"deleted": len(names), "names": names,
                    "track": track.name,
                    "from_time": None if from_time is None else lo,
                    "to_time": None if to_time is None else hi}
        except Exception as e:
            self.log_message("Error deleting arrangement clip: " + str(e))
            raise

    def _set_arrangement_clip_property(self, track_index, clip_index, property_name, value):
        """Set a property on an arrangement clip."""
        try:
            ALLOWED = ("name", "muted", "color", "looping", "loop_start", "loop_end",
                       "gain", "pitch_coarse", "pitch_fine", "warping", "warp_mode")
            if property_name not in ALLOWED:
                raise ValueError("Property '{0}' not allowed. Allowed: {1}".format(
                    property_name, ", ".join(ALLOWED)))
            track, clip = self._resolve_arrangement_clip(track_index, clip_index)
            setattr(clip, property_name, value)
            return {"property": property_name, "value": getattr(clip, property_name)}
        except Exception as e:
            self.log_message("Error setting arrangement clip property: " + str(e))
            raise

    def _set_view(self, view_name):
        """Switch Ableton view."""
        try:
            app = self.application()
            app.view.show_view(view_name)
            return {"visible": app.view.is_view_visible(view_name)}
        except Exception as e:
            self.log_message("Error setting view: " + str(e))
            raise

    def _control_arrangement_view(self, action, track_index=0):
        """Dispatch arrangement view control actions."""
        try:
            app = self.application()
            if action == "zoom_in":
                app.view.zoom_view(1, "Arranger", False)
            elif action == "zoom_out":
                app.view.zoom_view(0, "Arranger", False)
            elif action == "scroll_right":
                app.view.scroll_view(1, "Arranger", False)
            elif action == "scroll_left":
                app.view.scroll_view(0, "Arranger", False)
            elif action == "follow_on":
                self._song.view.follow_song = True
            elif action == "follow_off":
                self._song.view.follow_song = False
            elif action == "collapse_track":
                if track_index < 0 or track_index >= len(self._song.tracks):
                    raise IndexError("Track index out of range")
                self._song.tracks[track_index].view.is_collapsed = True
            elif action == "expand_track":
                if track_index < 0 or track_index >= len(self._song.tracks):
                    raise IndexError("Track index out of range")
                self._song.tracks[track_index].view.is_collapsed = False
            else:
                raise ValueError("Unknown action: " + action)
            return {"action": action, "done": True}
        except Exception as e:
            self.log_message("Error controlling arrangement view: " + str(e))
            raise

    def _manage_clip_automation(self, track_index, clip_index, action,
                                parameter_name="", clip_name=None):
        """Create or clear automation envelopes on a session clip."""
        try:
            track, clip = self._resolve_session_clip(
                track_index, clip_index, clip_name)
            if action == "clear_all":
                clip.clear_all_envelopes()
                return {"action": "clear_all", "done": True}
            param = self._find_automatable_parameter(track, parameter_name)
            if action == "create":
                clip.create_automation_envelope(param)
                return {"action": "create", "parameter": param.name, "done": True}
            elif action == "clear":
                clip.clear_envelope(param)
                return {"action": "clear", "parameter": param.name, "done": True}
            else:
                raise ValueError("Unknown action: " + action)
        except Exception as e:
            self.log_message("Error managing clip automation: " + str(e))
            raise

    def _resolve_session_clip(self, track_index, clip_index=None, clip_name=None):
        """Resolve a session clip by slot index or clip name."""
        if track_index < 0 or track_index >= len(self._song.tracks):
            raise IndexError("Track index {0} out of range (0-{1})".format(
                track_index, len(self._song.tracks) - 1))
        track = self._song.tracks[track_index]
        slots = tuple(track.clip_slots)

        if clip_name:
            matches = [(i, s.clip) for i, s in enumerate(slots)
                       if s.has_clip and s.clip.name == clip_name]
            if not matches:
                raise ValueError("No session clip named '{0}' on track '{1}'".format(
                    clip_name, track.name))
            if len(matches) > 1:
                raise ValueError("Ambiguous: {0} session clips named '{1}' on track '{2}'".format(
                    len(matches), clip_name, track.name))
            return track, matches[0][1]

        if clip_index is None:
            raise ValueError("Either clip_index or clip_name must be provided")
        if clip_index < 0 or clip_index >= len(slots):
            raise IndexError("Clip slot {0} out of range (0-{1}) on track '{2}'".format(
                clip_index, len(slots) - 1, track.name))
        slot = slots[clip_index]
        if not slot.has_clip:
            raise ValueError("No clip in session slot {0} on track '{1}'".format(
                clip_index, track.name))
        return track, slot.clip

    def _find_automatable_parameter(self, track, parameter_name):
        """Find a parameter on a track by name, with mixer-volume/pan aliases."""
        if not parameter_name:
            raise ValueError("parameter_name is required")
        target = parameter_name.strip().lower()
        mixer = track.mixer_device

        aliases = {
            "volume": mixer.volume,
            "track volume": mixer.volume,
            "pan": mixer.panning,
            "panning": mixer.panning,
            "track panning": mixer.panning,
        }
        if target in aliases:
            return aliases[target]

        for send in tuple(mixer.sends):
            if send.name.strip().lower() == target:
                return send
        for device in tuple(track.devices):
            for p in tuple(device.parameters):
                if p.name.strip().lower() == target:
                    return p
        raise ValueError("Parameter '{0}' not found on track '{1}'".format(
            parameter_name, track.name))

    # ── Device command handlers ──────────────────────────────────────

    def _get_device_parameters(self, track_index, device_index, chain_index=None, show_all=False):
        """Return parameter list for a device."""
        try:
            track, device = self._resolve_device(track_index, device_index)

            # If chain_index, navigate into chain
            target_device = device
            if chain_index is not None:
                if not device.can_have_chains:
                    raise ValueError("Device '{0}' is not a rack".format(device.name))
                chains = device.chains
                if chain_index < 0 or chain_index >= len(chains):
                    raise IndexError("Chain index {0} out of range".format(chain_index))
                chain = chains[chain_index]
                if not chain.devices:
                    raise ValueError("Chain '{0}' has no devices".format(chain.name))
                target_device = chain.devices[0]

            params = target_device.parameters
            param_list = []
            for i, p in enumerate(params):
                pmin = p.min
                pmax = p.max
                raw_val = p.value
                norm = (raw_val - pmin) / (pmax - pmin) if pmax != pmin else 0.0
                entry = {
                    "index": i,
                    "name": p.name,
                    "value": round(norm, 4),
                    "min": pmin,
                    "max": pmax,
                    "display_value": str(p),
                    "is_enabled": p.is_enabled,
                    "is_quantized": p.is_quantized,
                    "value_items": list(p.value_items) if p.is_quantized else [],
                }
                param_list.append(entry)

            result = {
                "device_name": target_device.name,
                "device_class": target_device.class_name,
                "parameter_count": len(params),
                "parameters": param_list,
            }
            return result
        except Exception as e:
            self.log_message("Error getting device parameters: " + str(e))
            raise

    def _set_device_parameter(self, track_index, device_index, chain_index=None,
                              parameter_name=None, parameter_index=None, value=0.0):
        """Set a device parameter by name or index. Value is normalized 0.0-1.0."""
        try:
            track, device = self._resolve_device(track_index, device_index)

            target_device = device
            if chain_index is not None:
                if not device.can_have_chains:
                    raise ValueError("Device '{0}' is not a rack".format(device.name))
                chain = device.chains[chain_index]
                if not chain.devices:
                    raise ValueError("Chain '{0}' has no devices".format(chain.name))
                target_device = chain.devices[0]

            param, match_type = self._find_parameter(
                target_device, name=parameter_name, index=parameter_index)

            if not param.is_enabled:
                raise ValueError("Parameter '{0}' is currently disabled".format(param.name))

            pmin = param.min
            pmax = param.max
            old_norm = (param.value - pmin) / (pmax - pmin) if pmax != pmin else 0.0

            # Clamp normalized value
            clamped = max(0.0, min(1.0, value))
            was_clamped = (clamped != value)

            # Denormalize
            raw_value = pmin + clamped * (pmax - pmin)
            param.value = raw_value

            return {
                "parameter_name": param.name,
                "old_value": round(old_norm, 4),
                "new_value": round(clamped, 4),
                "display_value": str(param),
                "clamped": was_clamped,
            }
        except Exception as e:
            self.log_message("Error setting device parameter: " + str(e))
            raise

    def _set_device_enabled(self, track_index, device_index, chain_index=None, enabled=True):
        """Enable or disable a device via its 'Device On' parameter."""
        try:
            track, device = self._resolve_device(track_index, device_index)

            target_device = device
            if chain_index is not None:
                if not device.can_have_chains:
                    raise ValueError("Device '{0}' is not a rack".format(device.name))
                chain = device.chains[chain_index]
                if not chain.devices:
                    raise ValueError("Chain has no devices")
                target_device = chain.devices[0]

            # Use "Device On" parameter (always index 0) instead of
            # is_active which is read-only in the Live API.
            params = target_device.parameters
            if not params:
                raise ValueError("Device '{0}' has no parameters".format(target_device.name))
            on_param = params[0]
            on_param.value = on_param.max if enabled else on_param.min

            return {
                "device_name": target_device.name,
                "is_active": enabled,
            }
        except Exception as e:
            self.log_message("Error setting device enabled: " + str(e))
            raise

    def _get_chain_info(self, track_index, device_index, chain_index=None):
        """Get chain information for a rack device."""
        try:
            track, device = self._resolve_device(track_index, device_index)

            if not device.can_have_chains:
                raise ValueError("Device '{0}' is not a rack and has no chains".format(device.name))

            if chain_index is not None:
                # Detail for a specific chain
                chains = device.chains
                if chain_index < 0 or chain_index >= len(chains):
                    raise IndexError("Chain index {0} out of range".format(chain_index))
                chain = chains[chain_index]
                devices = []
                for di, d in enumerate(chain.devices):
                    devices.append({
                        "index": di,
                        "name": d.name,
                        "class_name": d.class_name,
                        "type": self._get_device_type(d),
                        "is_active": d.is_active,
                        "parameter_count": len(d.parameters),
                    })
                return {
                    "chain_name": chain.name,
                    "devices": devices,
                }
            else:
                # List all chains
                chain_list = []
                for ci, chain in enumerate(device.chains):
                    chain_devices = []
                    for di, d in enumerate(chain.devices):
                        chain_devices.append({
                            "index": di,
                            "name": d.name,
                            "type": self._get_device_type(d),
                        })
                    chain_list.append({
                        "index": ci,
                        "name": chain.name,
                        "mute": chain.mute,
                        "solo": chain.solo,
                        "device_count": len(chain.devices),
                        "devices": chain_devices,
                    })
                return {
                    "device_name": device.name,
                    "chain_count": len(device.chains),
                    "chains": chain_list,
                }
        except Exception as e:
            self.log_message("Error getting chain info: " + str(e))
            raise

    def _get_drum_pad_info(self, track_index, device_index):
        """Get drum pad info for a Drum Rack."""
        try:
            track, device = self._resolve_device(track_index, device_index)

            if not device.can_have_drum_pads:
                raise ValueError("Device '{0}' is not a Drum Rack".format(device.name))

            filled_pads = []
            for pad in device.drum_pads:
                if pad.chains:
                    pad_devices = []
                    for chain in pad.chains:
                        for d in chain.devices:
                            pad_devices.append({
                                "index": 0,
                                "name": d.name,
                                "type": self._get_device_type(d),
                            })
                    filled_pads.append({
                        "note": pad.note,
                        "name": pad.name,
                        "mute": pad.mute,
                        "solo": pad.solo,
                        "chains": [{
                            "name": c.name,
                            "devices": [{"index": di, "name": d.name, "type": self._get_device_type(d)}
                                        for di, d in enumerate(c.devices)]
                        } for c in pad.chains],
                    })

            return {
                "device_name": device.name,
                "filled_pads": filled_pads,
            }
        except Exception as e:
            self.log_message("Error getting drum pad info: " + str(e))
            raise

    def _delete_device(self, track_index, device_index):
        """Delete a device from a track."""
        try:
            track, device = self._resolve_device(track_index, device_index)
            device_name = device.name
            track.delete_device(device_index)
            return {
                "deleted_device": device_name,
                "remaining_devices": len(track.devices),
            }
        except Exception as e:
            self.log_message("Error deleting device: " + str(e))
            raise

    def _delete_track(self, track_index):
        """Delete a track from the session."""
        try:
            if len(self._song.tracks) <= 1:
                raise ValueError(
                    "Cannot delete the last remaining session track. "
                    "Ableton must always have at least one track."
                )
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index {0} out of range (0-{1})".format(
                    track_index, len(self._song.tracks) - 1))
            track_name = self._song.tracks[track_index].name
            self._song.delete_track(track_index)
            return {
                "deleted_track": track_name,
                "remaining_tracks": len(self._song.tracks),
            }
        except Exception as e:
            self.log_message("Error deleting track: " + str(e))
            raise

    def _navigate_preset(self, track_index, device_index, chain_index=None, direction="current"):
        """Navigate device presets."""
        try:
            track, device = self._resolve_device(track_index, device_index)

            target_device = device
            if chain_index is not None:
                if not device.can_have_chains:
                    raise ValueError("Device '{0}' is not a rack".format(device.name))
                chain = device.chains[chain_index]
                if not chain.devices:
                    raise ValueError("Chain has no devices")
                target_device = chain.devices[0]

            if not hasattr(target_device, 'presets') or not target_device.presets:
                raise ValueError("Device '{0}' has no presets available".format(
                    target_device.name))

            presets = list(target_device.presets)
            current_idx = target_device.selected_preset_index

            if direction == "next":
                new_idx = min(current_idx + 1, len(presets) - 1)
                target_device.selected_preset_index = new_idx
            elif direction == "previous":
                new_idx = max(current_idx - 1, 0)
                target_device.selected_preset_index = new_idx
            elif direction == "current":
                new_idx = current_idx
            else:
                raise ValueError("Invalid direction: {0}".format(direction))

            return {
                "device_name": target_device.name,
                "preset_name": presets[new_idx] if new_idx < len(presets) else "",
                "preset_index": new_idx,
                "preset_count": len(presets),
            }
        except Exception as e:
            self.log_message("Error navigating preset: " + str(e))
            raise

    # Session / mixer / scene methods

    def _get_session_overview(self):
        """Compact map of the whole Set.

        One small payload describing every track (session + returns), its
        devices, clip count and mixer state. Cheap enough to call before any
        indexed operation, which is the reliable way to avoid acting on stale
        indices after a track is added, deleted or reordered.
        """
        try:
            def describe(track, index, kind):
                devices = []
                try:
                    devices = [d.name for d in track.devices]
                except Exception:
                    pass
                clips = []
                try:
                    for slot_index, slot in enumerate(track.clip_slots):
                        if slot.has_clip:
                            clips.append({
                                "slot": slot_index,
                                "name": slot.clip.name,
                                "length": slot.clip.length,
                            })
                except Exception:
                    pass
                info = {
                    "index": index,
                    "name": track.name,
                    "kind": kind,
                    "devices": devices,
                    "clips": clips,
                }
                for attr in ("mute", "solo"):
                    try:
                        info[attr] = getattr(track, attr)
                    except Exception:
                        pass
                try:
                    info["arm"] = track.arm if track.can_be_armed else None
                except Exception:
                    pass
                try:
                    info["color_index"] = track.color_index
                except Exception:
                    pass
                return info

            tracks = []
            offset = 0
            for i, t in enumerate(self._song.tracks):
                kind = "group" if t.is_foldable else (
                    "midi" if t.has_midi_input else "audio")
                tracks.append(describe(t, i, kind))
                offset = i + 1
            for j, t in enumerate(self._song.return_tracks):
                tracks.append(describe(t, offset + j, "return"))

            scenes = []
            try:
                for i, s in enumerate(self._song.scenes):
                    scenes.append({"index": i, "name": s.name})
            except Exception:
                pass

            return {
                "tempo": self._song.tempo,
                "signature": "{0}/{1}".format(
                    self._song.signature_numerator,
                    self._song.signature_denominator),
                "session_track_count": len(self._song.tracks),
                "return_track_count": len(self._song.return_tracks),
                "tracks": tracks,
                "scenes": scenes,
            }
        except Exception as e:
            self.log_message("Error building session overview: " + str(e))
            raise

    def _create_audio_track(self, index):
        """Create a new audio track at index (-1 = end)."""
        try:
            self._song.create_audio_track(index)
            new_track = self._song.tracks[index if index >= 0 else -1]
            return {"index": list(self._song.tracks).index(new_track),
                    "name": new_track.name}
        except Exception as e:
            self.log_message("Error creating audio track: " + str(e))
            raise

    def _create_return_track(self):
        """Create a new return track at the end of the return list."""
        try:
            self._song.create_return_track()
            new_track = self._song.return_tracks[-1]
            return {"index": len(self._song.tracks) + len(self._song.return_tracks) - 1,
                    "name": new_track.name,
                    "return_count": len(self._song.return_tracks)}
        except Exception as e:
            self.log_message("Error creating return track: " + str(e))
            raise

    def _duplicate_track(self, track_index):
        """Duplicate a session track, including its devices and clips."""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index {0} out of range (0-{1})".format(
                    track_index, len(self._song.tracks) - 1))
            source_name = self._song.tracks[track_index].name
            self._song.duplicate_track(track_index)
            new_track = self._song.tracks[track_index + 1]
            return {"source": source_name,
                    "index": track_index + 1,
                    "name": new_track.name}
        except Exception as e:
            self.log_message("Error duplicating track: " + str(e))
            raise

    def _set_send(self, track_index, send_index, value):
        """Set a track's send level to a return track.

        send_index 0 = return A, 1 = return B, and so on. Value is
        normalized 0.0-1.0.
        """
        try:
            track = self._track_at(track_index)
            sends = track.mixer_device.sends
            if send_index < 0 or send_index >= len(sends):
                raise IndexError(
                    "Send index {0} out of range on '{1}' (0-{2})".format(
                        send_index, track.name, len(sends) - 1))
            send = sends[send_index]
            target = send.min + (send.max - send.min) * max(0.0, min(1.0, value))
            send.value = target
            return {"track_name": track.name,
                    "send_index": send_index,
                    "send_name": send.name,
                    "value": send.value,
                    "display_value": str(send)}
        except Exception as e:
            self.log_message("Error setting send: " + str(e))
            raise

    def _set_track_state(self, track_index, mute=None, solo=None, arm=None,
                         color_index=None):
        """Set mute / solo / arm / colour on a track. Omitted values unchanged."""
        try:
            track = self._track_at(track_index)
            changed = {}
            if mute is not None:
                track.mute = bool(mute)
                changed["mute"] = track.mute
            if solo is not None:
                track.solo = bool(solo)
                changed["solo"] = track.solo
            if arm is not None:
                if not track.can_be_armed:
                    raise ValueError(
                        "Track '{0}' cannot be armed".format(track.name))
                track.arm = bool(arm)
                changed["arm"] = track.arm
            if color_index is not None:
                track.color_index = int(color_index)
                changed["color_index"] = track.color_index
            return {"track_name": track.name, "changed": changed}
        except Exception as e:
            self.log_message("Error setting track state: " + str(e))
            raise

    def _create_scene(self, index):
        """Create a scene at index (-1 = end)."""
        try:
            self._song.create_scene(index)
            pos = index if index >= 0 else len(self._song.scenes) - 1
            return {"index": pos, "name": self._song.scenes[pos].name,
                    "scene_count": len(self._song.scenes)}
        except Exception as e:
            self.log_message("Error creating scene: " + str(e))
            raise

    def _set_scene_name(self, scene_index, name):
        """Rename a scene."""
        try:
            if scene_index < 0 or scene_index >= len(self._song.scenes):
                raise IndexError("Scene index {0} out of range (0-{1})".format(
                    scene_index, len(self._song.scenes) - 1))
            scene = self._song.scenes[scene_index]
            scene.name = name
            return {"index": scene_index, "name": scene.name}
        except Exception as e:
            self.log_message("Error setting scene name: " + str(e))
            raise

    def _fire_scene(self, scene_index):
        """Launch a scene."""
        try:
            if scene_index < 0 or scene_index >= len(self._song.scenes):
                raise IndexError("Scene index {0} out of range (0-{1})".format(
                    scene_index, len(self._song.scenes) - 1))
            scene = self._song.scenes[scene_index]
            scene.fire()
            return {"index": scene_index, "name": scene.name, "fired": True}
        except Exception as e:
            self.log_message("Error firing scene: " + str(e))
            raise

    def _set_clip_properties(self, track_index, clip_index, looping=None,
                             loop_end=None, gain=None, warping=None,
                             color_index=None, quantize_to=None,
                             quantize_amount=1.0):
        """Set clip properties and optionally quantize it.

        quantize_to is a Live quantization grid constant (e.g. 5 = 1/16).
        Audio-only properties (gain, warping) are ignored on MIDI clips.
        """
        try:
            track = self._track_at(track_index)
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index {0} out of range".format(clip_index))
            slot = track.clip_slots[clip_index]
            if not slot.has_clip:
                raise ValueError("No clip at slot {0} on '{1}'".format(
                    clip_index, track.name))
            clip = slot.clip
            changed = {}
            if looping is not None:
                clip.looping = bool(looping)
                changed["looping"] = clip.looping
            if loop_end is not None:
                clip.loop_end = float(loop_end)
                changed["loop_end"] = clip.loop_end
            if color_index is not None:
                clip.color_index = int(color_index)
                changed["color_index"] = clip.color_index
            if not clip.is_midi_clip:
                if gain is not None:
                    clip.gain = float(gain)
                    changed["gain"] = clip.gain
                if warping is not None:
                    clip.warping = bool(warping)
                    changed["warping"] = clip.warping
            if quantize_to is not None:
                clip.quantize(int(quantize_to), float(quantize_amount))
                changed["quantized_to"] = int(quantize_to)
                changed["quantize_amount"] = float(quantize_amount)
            return {"track_name": track.name, "clip_name": clip.name,
                    "changed": changed}
        except Exception as e:
            self.log_message("Error setting clip properties: " + str(e))
            raise

    # Reading and editing existing notes

    def _clip_at(self, track_index, clip_index):
        track = self._track_at(track_index)
        if clip_index < 0 or clip_index >= len(track.clip_slots):
            raise IndexError("Clip slot {0} out of range".format(clip_index))
        slot = track.clip_slots[clip_index]
        if not slot.has_clip:
            raise ValueError("No clip at slot {0} on '{1}'".format(
                clip_index, track.name))
        return track, slot.clip

    def _resolve_clip(self, track_index, clip_index, arrangement=False,
                      clip_name=None):
        """Resolve a clip in either the session or the arrangement.

        Session and arrangement clips live in different LOM collections —
        `clip_slots` vs `arrangement_clips`. Every note tool used to resolve
        only through `_clip_at`, i.e. session clips, which is why arranged
        material could be overwritten but never read or edited. Routing
        through here is what closes that gap.

        Note times are clip-relative in BOTH cases: an arrangement clip
        reports its notes from 0, not from its position on the timeline.
        """
        if arrangement:
            return self._resolve_arrangement_clip(
                track_index, clip_index, clip_name)
        return self._clip_at(track_index, clip_index)

    def _serialise_notes(self, note_objects):
        out = []
        for n in note_objects:
            entry = {}
            for attr in ("pitch", "start_time", "duration", "velocity", "mute",
                         "probability", "velocity_deviation", "release_velocity",
                         "note_id"):
                try:
                    val = getattr(n, attr)
                    entry[attr] = round(val, 5) if isinstance(val, float) else val
                except Exception:
                    pass
            out.append(entry)
        return out

    def _get_clip_notes(self, track_index, clip_index, from_time=0.0,
                        time_span=None, from_pitch=0, pitch_span=128,
                        arrangement=False, clip_name=None):
        """Read the notes in a MIDI clip, in the session or the arrangement.

        Without this, material can only be written, never inspected — so an
        existing part cannot be analysed, transposed or edited, only
        replaced. Returns pitch, timing, velocity and probability per note.
        """
        try:
            track, clip = self._resolve_clip(
                track_index, clip_index, arrangement, clip_name)
            if not clip.is_midi_clip:
                raise ValueError("'{0}' is an audio clip".format(clip.name))
            span = float(time_span) if time_span is not None else float(clip.length)
            notes = clip.get_notes_extended(
                int(from_pitch), int(pitch_span), float(from_time), span)
            serialised = self._serialise_notes(notes)
            pitches = [n["pitch"] for n in serialised if "pitch" in n]
            return {"clip_name": clip.name, "track_name": track.name,
                    "length": clip.length, "note_count": len(serialised),
                    "arrangement": bool(arrangement),
                    "pitch_range": [min(pitches), max(pitches)] if pitches else None,
                    "notes": serialised}
        except Exception as e:
            self.log_message("Error reading clip notes: " + str(e))
            raise

    def _modify_clip_notes(self, track_index, clip_index, transpose=0,
                           velocity_scale=None, velocity_set=None,
                           humanize_ms=None, probability=None,
                           from_time=0.0, time_span=None,
                           from_pitch=0, pitch_span=128,
                           arrangement=False, clip_name=None):
        """Transform notes already in a clip, in place.

        Reads the selected notes, applies the requested changes, and writes
        them back by note id so nothing else in the clip is disturbed.
        Humanisation is deterministic (a fixed pattern of small offsets)
        rather than random, so repeated calls do not drift.

        This cannot delete notes — use `remove_clip_notes` for that.
        """
        try:
            track, clip = self._resolve_clip(
                track_index, clip_index, arrangement, clip_name)
            if not clip.is_midi_clip:
                raise ValueError("'{0}' is an audio clip".format(clip.name))
            span = float(time_span) if time_span is not None else float(clip.length)
            notes = list(clip.get_notes_extended(
                int(from_pitch), int(pitch_span), float(from_time), span))
            if not notes:
                return {"clip_name": clip.name, "modified": 0}

            # Deterministic offsets in beats, derived from tempo.
            beats_per_ms = self._song.tempo / 60000.0
            offsets = [0.0, 0.6, -0.4, 0.9, -0.7, 0.3, -0.2, 0.8]

            for i, n in enumerate(notes):
                if transpose:
                    n.pitch = max(0, min(127, int(n.pitch) + int(transpose)))
                if velocity_set is not None:
                    n.velocity = max(1.0, min(127.0, float(velocity_set)))
                elif velocity_scale is not None:
                    n.velocity = max(1.0, min(
                        127.0, float(n.velocity) * float(velocity_scale)))
                if humanize_ms:
                    shift = offsets[i % len(offsets)] * float(humanize_ms) * beats_per_ms
                    n.start_time = max(0.0, float(n.start_time) + shift)
                if probability is not None:
                    try:
                        n.probability = max(0.0, min(1.0, float(probability)))
                    except Exception:
                        pass

            clip.apply_note_modifications(tuple(notes))
            return {"clip_name": clip.name, "track_name": track.name,
                    "modified": len(notes),
                    "applied": {"transpose": transpose,
                                "velocity_scale": velocity_scale,
                                "velocity_set": velocity_set,
                                "humanize_ms": humanize_ms,
                                "probability": probability}}
        except Exception as e:
            self.log_message("Error modifying clip notes: " + str(e))
            raise

    def _remove_clip_notes(self, track_index, clip_index, from_time=0.0,
                           time_span=None, from_pitch=0, pitch_span=128,
                           arrangement=False, clip_name=None):
        """Delete notes from a MIDI clip within a pitch/time window.

        `modify_clip_notes` can transpose and rescale but never remove, and
        `add_notes_extended(replace=True)` clears the whole clip — so there
        was no way to take a single note out of a part. Surgical removal is
        what a voicing change actually needs: lifting one non-diatonic note
        out of a chord without rewriting the chord.

        Returns the notes that were removed so the edit is auditable and
        reversible by hand.
        """
        try:
            track, clip = self._resolve_clip(
                track_index, clip_index, arrangement, clip_name)
            if not clip.is_midi_clip:
                raise ValueError("'{0}' is an audio clip".format(clip.name))
            span = float(time_span) if time_span is not None else float(clip.length)
            doomed = self._serialise_notes(clip.get_notes_extended(
                int(from_pitch), int(pitch_span), float(from_time), span))
            clip.remove_notes_extended(
                int(from_pitch), int(pitch_span), float(from_time), span)
            return {"clip_name": clip.name, "track_name": track.name,
                    "arrangement": bool(arrangement),
                    "removed": len(doomed), "removed_notes": doomed,
                    "remaining": len(clip.get_notes_extended(
                        0, 128, 0.0, float(clip.length)))}
        except Exception as e:
            self.log_message("Error removing clip notes: " + str(e))
            raise

    def _manage_clip_region(self, track_index, clip_index, action="info",
                            region_start=None, region_end=None,
                            destination_time=None, start_marker=None,
                            end_marker=None):
        """Duplicate, crop or re-mark a clip region — arrangement building blocks."""
        try:
            track, clip = self._clip_at(track_index, clip_index)
            result = {"clip_name": clip.name, "action": action}

            if start_marker is not None:
                clip.start_marker = float(start_marker)
                result["start_marker"] = clip.start_marker
            if end_marker is not None:
                clip.end_marker = float(end_marker)
                result["end_marker"] = clip.end_marker

            if action == "duplicate_loop":
                # Doubles the loop length, copying its contents — the standard
                # way to grow a 4-bar idea into 8 bars.
                clip.duplicate_loop()
                result["new_loop_end"] = clip.loop_end
            elif action == "duplicate_region":
                if region_start is None or region_end is None or \
                        destination_time is None:
                    raise ValueError(
                        "duplicate_region needs region_start, region_end "
                        "and destination_time")
                clip.duplicate_region(float(region_start), float(region_end),
                                      float(destination_time))
                result["duplicated"] = [float(region_start), float(region_end),
                                        float(destination_time)]
            elif action == "crop":
                clip.crop()
                result["cropped_length"] = clip.length
            elif action != "info":
                raise ValueError(
                    "action must be info, duplicate_loop, duplicate_region or crop")

            for attr in ("length", "loop_start", "loop_end",
                         "start_marker", "end_marker"):
                try:
                    result[attr] = getattr(clip, attr)
                except Exception:
                    pass
            return result
        except Exception as e:
            self.log_message("Error managing clip region: " + str(e))
            raise

    def _set_wavetable_oscillator(self, track_index, device_index, oscillator=1,
                                  category=None, wavetable=None,
                                  effect_mode=None, unison_mode=None,
                                  unison_voices=None, mono_poly=None,
                                  poly_voices=None):
        """Choose Wavetable's actual wavetables and voicing.

        Selecting the wavetable is the single biggest tonal decision in the
        instrument, and it is exposed by name — no need to click through the
        UI list.
        """
        try:
            track = self._track_at(track_index)
            device = track.devices[device_index]
            osc = 1 if int(oscillator) not in (1, 2) else int(oscillator)
            cat_attr = "oscillator_{0}_wavetable_category".format(osc)
            idx_attr = "oscillator_{0}_wavetable_index".format(osc)
            list_attr = "oscillator_{0}_wavetables".format(osc)
            eff_attr = "oscillator_{0}_effect_mode".format(osc)

            if not hasattr(device, cat_attr):
                raise ValueError(
                    "'{0}' is not a Wavetable device".format(device.name))

            changed = {}
            categories = []
            try:
                categories = [str(c) for c in device.oscillator_wavetable_categories]
            except Exception:
                pass

            if category is not None:
                if isinstance(category, str):
                    wanted = category.strip().lower()
                    match = None
                    for i, c in enumerate(categories):
                        if c.strip().lower() == wanted:
                            match = i
                            break
                    if match is None:
                        for i, c in enumerate(categories):
                            if wanted in c.strip().lower():
                                match = i
                                break
                    if match is None:
                        raise ValueError(
                            "Category '{0}' not found. Available: {1}".format(
                                category, ", ".join(categories)))
                    setattr(device, cat_attr, match)
                else:
                    setattr(device, cat_attr, int(category))
                changed["category"] = categories[getattr(device, cat_attr)] \
                    if categories else getattr(device, cat_attr)

            tables = []
            try:
                tables = [str(t) for t in getattr(device, list_attr)]
            except Exception:
                pass

            if wavetable is not None:
                if isinstance(wavetable, str):
                    wanted = wavetable.strip().lower()
                    match = None
                    for i, t in enumerate(tables):
                        if t.strip().lower() == wanted:
                            match = i
                            break
                    if match is None:
                        for i, t in enumerate(tables):
                            if wanted in t.strip().lower():
                                match = i
                                break
                    if match is None:
                        raise ValueError(
                            "Wavetable '{0}' not found in this category. "
                            "Available: {1}".format(wavetable, ", ".join(tables)))
                    setattr(device, idx_attr, match)
                else:
                    setattr(device, idx_attr, int(wavetable))
                changed["wavetable"] = tables[getattr(device, idx_attr)] \
                    if tables else getattr(device, idx_attr)

            for attr, val in ((eff_attr, effect_mode),
                              ("unison_mode", unison_mode),
                              ("unison_voice_count", unison_voices),
                              ("mono_poly", mono_poly),
                              ("poly_voices", poly_voices)):
                if val is None:
                    continue
                if not hasattr(device, attr):
                    changed[attr] = "<not supported>"
                    continue
                setattr(device, attr, int(val))
                changed[attr] = getattr(device, attr)

            return {"device": device.name, "oscillator": osc,
                    "categories": categories, "wavetables_in_category": tables,
                    "changed": changed}
        except Exception as e:
            self.log_message("Error setting wavetable oscillator: " + str(e))
            raise

    def _duplicate_device(self, track_index, device_index):
        """Duplicate a device in place on its track."""
        try:
            track = self._track_at(track_index)
            track.duplicate_device(int(device_index))
            return {"track": track.name,
                    "chain": [d.name for d in track.devices]}
        except Exception as e:
            self.log_message("Error duplicating device: " + str(e))
            raise

    def _undo_step(self, action="begin"):
        """Group several changes into one undo entry.

        Wrap a multi-step edit in begin/end so a single Cmd-Z reverses the
        whole thing instead of unpicking it one call at a time.
        """
        try:
            if action == "begin":
                self._song.begin_undo_step()
            elif action == "end":
                self._song.end_undo_step()
            else:
                raise ValueError("action must be 'begin' or 'end'")
            return {"action": action, "done": True}
        except Exception as e:
            self.log_message("Error in undo step: " + str(e))
            raise

    def _transport_action(self, action, value=None):
        """Transport and session actions that take no persistent state."""
        try:
            song = self._song
            simple = {
                "stop_all_clips": lambda: song.stop_all_clips(),
                "tap_tempo": lambda: song.tap_tempo(),
                "capture_and_insert_scene": lambda: song.capture_and_insert_scene(),
                "continue_playing": lambda: song.continue_playing(),
                "play_selection": lambda: song.play_selection(),
                "trigger_session_record": lambda: song.trigger_session_record(),
                "jump_to_next_cue": lambda: song.jump_to_next_cue(),
                "jump_to_prev_cue": lambda: song.jump_to_prev_cue(),
                "re_enable_automation": lambda: song.re_enable_automation(),
                "back_to_arranger": lambda: setattr(song, "back_to_arranger", False),
            }
            if action == "jump_by":
                if value is None:
                    raise ValueError("jump_by needs a value in beats")
                song.jump_by(float(value))
                return {"action": action, "beats": float(value),
                        "current_song_time": song.current_song_time}
            if action == "scrub_by":
                if value is None:
                    raise ValueError("scrub_by needs a value in beats")
                song.scrub_by(float(value))
                return {"action": action, "beats": float(value)}
            if action not in simple:
                raise ValueError("Unknown action '{0}'. Available: {1}".format(
                    action, ", ".join(sorted(list(simple.keys()) +
                                             ["jump_by", "scrub_by"]))))
            simple[action]()
            return {"action": action, "done": True,
                    "is_playing": song.is_playing}
        except Exception as e:
            self.log_message("Error in transport action: " + str(e))
            raise

    def _set_mixer_extras(self, track_index, track_activator=None,
                          panning_mode=None, left_split_stereo=None,
                          right_split_stereo=None, cue_volume=None):
        """Mixer controls beyond volume/pan/sends."""
        try:
            track = self._track_at(track_index)
            mixer = track.mixer_device
            changed = {}

            def set_param(param, val, label):
                target = param.min + (param.max - param.min) * \
                    max(0.0, min(1.0, float(val)))
                param.value = target
                changed[label] = str(param)

            if track_activator is not None:
                mixer.track_activator.value = 1.0 if track_activator else 0.0
                changed["track_activator"] = mixer.track_activator.value
            if panning_mode is not None:
                mixer.panning_mode = int(panning_mode)
                changed["panning_mode"] = mixer.panning_mode
            if left_split_stereo is not None:
                set_param(mixer.left_split_stereo, left_split_stereo,
                          "left_split_stereo")
            if right_split_stereo is not None:
                set_param(mixer.right_split_stereo, right_split_stereo,
                          "right_split_stereo")
            if cue_volume is not None:
                set_param(self._song.master_track.mixer_device.cue_volume,
                          cue_volume, "cue_volume")
            return {"track": track.name, "changed": changed}
        except Exception as e:
            self.log_message("Error setting mixer extras: " + str(e))
            raise

    def _set_view_detail(self, track_index=None, clip_index=None,
                         device_index=None, draw_mode=None):
        """Focus Live's detail view on a specific clip or device."""
        try:
            view = self._song.view
            focused = {}
            if track_index is not None:
                track = self._track_at(track_index)
                view.selected_track = track
                focused["track"] = track.name
                if clip_index is not None:
                    slot = track.clip_slots[clip_index]
                    if slot.has_clip:
                        view.detail_clip = slot.clip
                        focused["clip"] = slot.clip.name
                if device_index is not None:
                    device = track.devices[device_index]
                    view.select_device(device)
                    focused["device"] = device.name
            if draw_mode is not None:
                view.draw_mode = bool(draw_mode)
                focused["draw_mode"] = view.draw_mode
            return {"focused": focused}
        except Exception as e:
            self.log_message("Error setting view detail: " + str(e))
            raise

    def _set_scene_signature(self, scene_index, numerator=None,
                             denominator=None, enabled=None):
        """Give a scene its own time signature."""
        try:
            if scene_index < 0 or scene_index >= len(self._song.scenes):
                raise IndexError("Scene index out of range")
            scene = self._song.scenes[scene_index]
            changed = {}
            if numerator is not None:
                scene.time_signature_numerator = int(numerator)
                changed["numerator"] = scene.time_signature_numerator
            if denominator is not None:
                scene.time_signature_denominator = int(denominator)
                changed["denominator"] = scene.time_signature_denominator
            if enabled is not None:
                scene.time_signature_enabled = bool(enabled)
                changed["enabled"] = scene.time_signature_enabled
            return {"scene": scene.name or "(unnamed)", "changed": changed}
        except Exception as e:
            self.log_message("Error setting scene signature: " + str(e))
            raise


    # --- Drift ---------------------------------------------------------
    # Every Drift enum is an (_index, _list) pair. The _list is a read-only
    # tuple of display strings; only the _index is writable.
    DRIFT_ENUMS = [
        ("source_1", "mod_matrix_source_1"),
        ("target_1", "mod_matrix_target_1"),
        ("source_2", "mod_matrix_source_2"),
        ("target_2", "mod_matrix_target_2"),
        ("source_3", "mod_matrix_source_3"),
        ("target_3", "mod_matrix_target_3"),
        ("filter_source_1", "mod_matrix_filter_source_1"),
        ("filter_source_2", "mod_matrix_filter_source_2"),
        ("lfo_source", "mod_matrix_lfo_source"),
        ("pitch_source_1", "mod_matrix_pitch_source_1"),
        ("pitch_source_2", "mod_matrix_pitch_source_2"),
        ("shape_source", "mod_matrix_shape_source"),
        ("voice_count", "voice_count"),
        ("voice_mode", "voice_mode"),
    ]

    def _drift_device(self, track_index, device_index):
        """Resolve a device and verify it is a Drift."""
        track, device = self._resolve_device(track_index, device_index)
        if not hasattr(device, "mod_matrix_source_1_index"):
            raise ValueError(
                "'{0}' is not a Drift device".format(device.name))
        return track, device

    def _drift_options(self, device, base):
        """Read the display-string tuple for a Drift enum, as a list."""
        if not hasattr(device, base + "_list"):
            return []
        try:
            return [str(o) for o in getattr(device, base + "_list")]
        except Exception:
            return []

    def _drift_read_slot(self, device, base):
        """Current value of one enum slot as a name, or None if absent.

        Falls back to the raw index when the option list is unavailable.
        """
        index_attr = base + "_index"
        if not hasattr(device, index_attr):
            return None
        try:
            index = getattr(device, index_attr)
        except Exception:
            return None
        options = self._drift_options(device, base)
        if options and 0 <= index < len(options):
            return options[index]
        return index

    def _drift_set_enum(self, device, base, value):
        """Resolve a name (or raw int) against base + '_list' and set the index.

        Matching order: exact, case-insensitive exact, case-insensitive
        partial. Never assume the ordering of the list — always look it up.
        A raw int is only accepted when the option list can be read, so an
        index can be range-checked rather than blindly trusted.
        """
        index_attr = base + "_index"
        if not hasattr(device, index_attr):
            return "<not supported>"
        options = self._drift_options(device, base)
        if isinstance(value, bool):
            raise ValueError("'{0}' needs a name or an index".format(base))
        if isinstance(value, (int, float)) and not isinstance(value, str):
            if not options:
                raise ValueError(
                    "Cannot verify index {0} for {1}: this Live version does "
                    "not expose {1}_list. Pass a name instead.".format(
                        value, base))
            target = int(value)
            if target < 0 or target >= len(options):
                raise ValueError(
                    "Index {0} out of range for {1} (0-{2})".format(
                        target, base, len(options) - 1))
        else:
            wanted = str(value).strip()
            target = None
            for i, o in enumerate(options):
                if o == wanted:
                    target = i
                    break
            if target is None:
                low = wanted.lower()
                for i, o in enumerate(options):
                    if o.strip().lower() == low:
                        target = i
                        break
            if target is None:
                low = wanted.lower()
                for i, o in enumerate(options):
                    if low in o.strip().lower():
                        target = i
                        break
            if target is None:
                raise ValueError(
                    "'{0}' is not a valid {1}. Available: {2}".format(
                        value, base, ", ".join(options) if options else "none"))
        setattr(device, index_attr, target)
        new_index = getattr(device, index_attr)
        if options and 0 <= new_index < len(options):
            return options[new_index]
        return new_index

    def _get_drift_modulation(self, track_index, device_index):
        """Report Drift's whole modulation matrix, voicing and pitch bend.

        Also returns every legal option per slot, so a caller can pick a
        source or target BY NAME without guessing the ordering.
        """
        try:
            track, device = self._drift_device(track_index, device_index)
            result = {
                "track": track.name,
                "device": device.name,
                "slots": {},
                "options": {},
            }
            for key, base in self.DRIFT_ENUMS:
                current = self._drift_read_slot(device, base)
                if current is None:
                    continue
                result["slots"][key] = current
                result["options"][key] = self._drift_options(device, base)
            if hasattr(device, "pitch_bend_range"):
                try:
                    result["pitch_bend_range"] = device.pitch_bend_range
                except Exception:
                    pass
            return result
        except Exception as e:
            self.log_message("Error reading Drift modulation: " + str(e))
            raise

    def _set_drift_modulation(self, track_index, device_index,
                              source_1=None, target_1=None,
                              source_2=None, target_2=None,
                              source_3=None, target_3=None,
                              filter_source_1=None, filter_source_2=None,
                              lfo_source=None, pitch_source_1=None,
                              pitch_source_2=None, shape_source=None,
                              voice_count=None, voice_mode=None,
                              pitch_bend_range=None):
        """Rewire Drift's modulation matrix and voicing. Omitted = unchanged."""
        try:
            track, device = self._drift_device(track_index, device_index)
            requested = {
                "source_1": source_1, "target_1": target_1,
                "source_2": source_2, "target_2": target_2,
                "source_3": source_3, "target_3": target_3,
                "filter_source_1": filter_source_1,
                "filter_source_2": filter_source_2,
                "lfo_source": lfo_source,
                "pitch_source_1": pitch_source_1,
                "pitch_source_2": pitch_source_2,
                "shape_source": shape_source,
                "voice_count": voice_count,
                "voice_mode": voice_mode,
            }
            changed = {}
            for key, base in self.DRIFT_ENUMS:
                value = requested.get(key, None)
                if value is None:
                    continue
                changed[key] = self._drift_set_enum(device, base, value)

            if pitch_bend_range is not None:
                if not hasattr(device, "pitch_bend_range"):
                    changed["pitch_bend_range"] = "<not supported>"
                else:
                    device.pitch_bend_range = int(pitch_bend_range)
                    changed["pitch_bend_range"] = device.pitch_bend_range

            if not changed:
                raise ValueError("No Drift changes requested")

            result = {
                "track": track.name,
                "device": device.name,
                "changed": changed,
                "slots": {},
            }
            for key, base in self.DRIFT_ENUMS:
                current = self._drift_read_slot(device, base)
                if current is None:
                    continue
                result["slots"][key] = current
            if hasattr(device, "pitch_bend_range"):
                try:
                    result["pitch_bend_range"] = device.pitch_bend_range
                except Exception:
                    pass
            return result
        except Exception as e:
            self.log_message("Error setting Drift modulation: " + str(e))
            raise

    # Members every Device (and every Rack) already has — anything beyond this
    # list is what makes a device special, and is what get_device_modes reports.
    _BASE_DEVICE_MEMBERS = (
        "View", "can_compare_ab", "can_have_chains", "can_have_drum_pads",
        "can_show_chains", "canonical_parent", "chains", "class_display_name",
        "class_name", "drum_pads", "has_drum_pads", "has_macro_mappings",
        "is_active", "is_collapsed", "is_using_compare_preset_b",
        "latency_in_ms", "latency_in_samples", "name", "parameters",
        "return_chains", "save_preset_to_compare_ab_slot", "selected_chain",
        "selected_drum_pad", "selected_variation_index", "store_chosen_bank",
        "type", "variation_count", "view", "visible_drum_pads",
        "visible_macro_count")

    def _mode_device(self, track_index, device_index, chain_index=None):
        """Resolve the device whose modes are being read or written.

        _resolve_device() is deliberately called WITHOUT chain_index: it
        returns the rack itself for a chain reference, which is not what
        this tool wants. The chain hop is done here instead.
        """
        track, device = self._resolve_device(track_index, device_index)
        if chain_index is not None:
            if not getattr(device, "can_have_chains", False):
                raise ValueError("Device '{0}' is not a rack and has no chains".format(
                    getattr(device, "name", "?")))
            chains = list(getattr(device, "chains", []) or [])
            if chain_index < 0 or chain_index >= len(chains):
                raise IndexError("Chain index {0} out of range on '{1}' (0-{2})".format(
                    chain_index, getattr(device, "name", "?"), len(chains) - 1))
            chain = chains[chain_index]
            chain_devices = list(getattr(chain, "devices", []) or [])
            if not chain_devices:
                raise ValueError("Chain '{0}' has no devices".format(
                    getattr(chain, "name", "?")))
            device = chain_devices[0]
        return track, device

    def _mode_options(self, device, list_attr):
        """Read a *_list property as a plain list of display strings."""
        try:
            return [str(v) for v in getattr(device, list_attr)]
        except Exception:
            return []

    def _scan_device_modes(self, device):
        """Split a device's non-base members into enum pairs and plain values.

        Live models its character switches as a PAIR: a writable integer
        (either '<name>_index' or plain '<name>') plus a read-only
        '<name>_list' of display strings. The ordering is per-device and per
        Live version, so the list is always the authority — never a guess.

        A '<name>_list' whose partner is readable but is NOT an integer (a
        string, a bool) is not an index pair at all; it is left to the plain
        values so the setter never tries to write an int into it.

        Returns (enums, values) where enums maps a mode name to a dict with
        the index attribute, the option strings and the current selection.
        """
        names = []
        for attr in dir(device):
            if attr.startswith("_") or attr in self._BASE_DEVICE_MEMBERS:
                continue
            names.append(attr)

        enums = {}
        consumed = set()
        for attr in names:
            if not attr.endswith("_list"):
                continue
            base = attr[:-5]
            options = self._mode_options(device, attr)
            if not options:
                continue
            index_attr = None
            if base + "_index" in names:
                index_attr = base + "_index"
            elif base in names:
                index_attr = base
            if index_attr is None:
                continue
            readable = True
            raw = None
            try:
                raw = getattr(device, index_attr)
            except Exception:
                readable = False
            if readable:
                # bool is a subclass of int, so it has to be excluded first.
                if isinstance(raw, bool) or not isinstance(raw, int):
                    continue
                current_index = int(raw)
            else:
                current_index = None
            current = None
            if current_index is not None and 0 <= current_index < len(options):
                current = options[current_index]
            enums[base] = {
                "index_attr": index_attr,
                "options": options,
                "current_index": current_index,
                "current": current,
            }
            consumed.add(attr)
            consumed.add(index_attr)

        values = {}
        for attr in names:
            if attr in consumed:
                continue
            try:
                value = getattr(device, attr)
            except Exception:
                continue
            if callable(value):
                continue
            if isinstance(value, bool) or isinstance(value, (int, float)):
                values[attr] = value
            elif isinstance(value, str):
                values[attr] = value
        return enums, values

    def _get_device_modes(self, track_index, device_index, chain_index=None):
        """List every mode switch a device exposes, with its options."""
        try:
            track, device = self._mode_device(track_index, device_index, chain_index)
            enums, values = self._scan_device_modes(device)
            mode_list = []
            for name in sorted(enums.keys()):
                info = enums[name]
                mode_list.append({
                    "name": name,
                    "current": info["current"],
                    "current_index": info["current_index"],
                    "options": info["options"],
                })
            value_list = []
            for name in sorted(values.keys()):
                value_list.append({
                    "name": name,
                    "value": self._describe_value(values[name]),
                })
            return {
                "track": getattr(track, "name", "?"),
                "device": getattr(device, "name", "?"),
                "class_name": getattr(device, "class_name", ""),
                "modes": mode_list,
                "values": value_list,
            }
        except Exception as e:
            self.log_message("Error getting device modes: " + str(e))
            raise

    def _resolve_mode_choice(self, mode, info, value):
        """Turn a user-supplied name (or index) into a valid list index."""
        options = info["options"]
        if isinstance(value, bool):
            raise ValueError(
                "'{0}' expects one of: {1}".format(mode, ", ".join(options)))
        if isinstance(value, (int, float)):
            index = int(value)
        else:
            wanted = str(value).strip().lower()
            index = None
            for i, opt in enumerate(options):
                if opt.strip().lower() == wanted:
                    index = i
                    break
            if index is None:
                partial = [i for i, opt in enumerate(options)
                           if wanted in opt.strip().lower()]
                if len(partial) == 1:
                    index = partial[0]
                elif len(partial) > 1:
                    raise ValueError("'{0}' is ambiguous for {1}: {2}".format(
                        value, mode,
                        ", ".join(options[i] for i in partial)))
            if index is None and wanted.isdigit():
                index = int(wanted)
            if index is None:
                raise ValueError("'{0}' is not a {1}. Available: {2}".format(
                    value, mode, ", ".join(options)))
        if index < 0 or index >= len(options):
            raise ValueError(
                "{0} index {1} out of range (0-{2})".format(
                    mode, index, len(options) - 1))
        return index

    def _coerce_mode_value(self, device, attr, value):
        """Coerce a plain (non-enum) member to the type it already holds."""
        current = None
        try:
            current = getattr(device, attr)
        except Exception:
            pass
        if isinstance(current, bool) or isinstance(value, bool):
            if isinstance(value, str):
                wanted = value.strip().lower()
                if wanted in ("on", "true", "yes", "1"):
                    return True
                if wanted in ("off", "false", "no", "0"):
                    return False
                raise ValueError(
                    "'{0}' expects on/off, got '{1}'".format(attr, value))
            return bool(value)
        if isinstance(current, float):
            return float(value)
        if isinstance(current, int):
            return int(float(value))
        if isinstance(current, str):
            return str(value)
        try:
            return int(float(value))
        except Exception:
            return value

    def _set_device_mode(self, track_index, device_index, chain_index=None,
                         settings=None):
        """Set one or more mode switches on a device, resolving names to indices.

        'settings' is an ordered mapping of member name -> value. Enum-style
        members are matched against their own *_list (never assumed), plain
        members are coerced to the type they already hold.

        The device is re-scanned after every write. Some of Live's lists are
        dependent — Hybrid Reverb replaces ir_file_list when ir_category
        changes — so a cached option list would resolve the second setting
        against the previous category's files.
        """
        try:
            track, device = self._mode_device(track_index, device_index, chain_index)
            if not settings:
                raise ValueError("set_device_mode needs at least one mode to set")
            enums, values = self._scan_device_modes(device)
            device_name = getattr(device, "name", "?")

            changed = {}
            for raw_mode in settings:
                value = settings[raw_mode]
                if value is None:
                    continue
                mode = str(raw_mode).strip()
                try:
                    info = enums.get(mode)
                    if info is None and mode.endswith("_index"):
                        info = enums.get(mode[:-6])
                        if info is not None:
                            mode = mode[:-6]

                    if info is not None:
                        index = self._resolve_mode_choice(mode, info, value)
                        options = info["options"]
                        try:
                            setattr(device, info["index_attr"], index)
                        except Exception as write_error:
                            raise ValueError(
                                "'{0}' on '{1}' would not accept '{2}': {3}".format(
                                    mode, device_name, options[index],
                                    str(write_error)))
                        try:
                            now = int(getattr(device, info["index_attr"]))
                        except Exception:
                            now = index
                        changed[mode] = (options[now]
                                         if 0 <= now < len(options) else now)
                    else:
                        current = None
                        if mode in values:
                            current = values[mode]
                        elif (mode not in self._BASE_DEVICE_MEMBERS
                              and hasattr(device, mode)):
                            probe = getattr(device, mode, None)
                            if callable(probe):
                                raise ValueError(
                                    "'{0}' on '{1}' is a method, not a "
                                    "settable mode".format(mode, device_name))
                        else:
                            available = sorted(
                                list(enums.keys()) + list(values.keys()))
                            raise ValueError(
                                "'{0}' has no mode '{1}'. Available: {2}".format(
                                    device_name, mode, ", ".join(available)))
                        try:
                            setattr(device, mode,
                                    self._coerce_mode_value(device, mode, value))
                        except ValueError:
                            raise
                        except Exception as write_error:
                            raise ValueError(
                                "'{0}' on '{1}' is read-only or rejected "
                                "'{2}': {3}".format(mode, device_name, value,
                                                    str(write_error)))
                        changed[mode] = self._describe_value(
                            getattr(device, mode, current))

                    # Dependent lists (ir_file_list after ir_category) are
                    # rebuilt by Live on write — re-read, never reuse.
                    enums, values = self._scan_device_modes(device)
                except Exception as e:
                    if changed:
                        raise ValueError("{0} (already applied: {1})".format(
                            str(e),
                            ", ".join("{0} = {1}".format(k, changed[k])
                                      for k in sorted(changed.keys()))))
                    raise

            return {
                "track": getattr(track, "name", "?"),
                "device": device_name,
                "class_name": getattr(device, "class_name", ""),
                "changed": changed,
            }
        except Exception as e:
            self.log_message("Error setting device mode: " + str(e))
            raise


    def _external_device_at(self, track_index, device_index, chain_index=None):
        """Resolve a device, optionally one nested inside a rack chain.

        Plugins and Max devices are very often parked inside an Instrument or
        Audio Effect Rack, so external-device tools must be able to reach in.
        When chain_index is given, device_index addresses the rack and the
        first device of that chain is returned (same contract as the existing
        navigate_preset / get_device_parameters commands).
        """
        track = self._track_at(track_index)
        devices = tuple(track.devices)
        if device_index < 0 or device_index >= len(devices):
            raise IndexError("Device index {0} out of range on '{1}' (0-{2})".format(
                device_index, track.name, len(devices) - 1 if devices else 0))
        device = devices[device_index]
        if chain_index is not None:
            if not getattr(device, "can_have_chains", False):
                raise ValueError("'{0}' is not a rack and has no chains".format(
                    device.name))
            chains = tuple(device.chains)
            if chain_index < 0 or chain_index >= len(chains):
                raise IndexError("Chain index {0} out of range on '{1}' (0-{2})".format(
                    chain_index, device.name, len(chains) - 1 if chains else 0))
            chain = chains[chain_index]
            if not chain.devices:
                raise ValueError("Chain '{0}' has no devices".format(chain.name))
            device = chain.devices[0]
        return track, device

    def _match_routing_option(self, options, wanted, label):
        """Pick an element out of an available_* collection by display name.

        Routing properties are typed as Live objects, so they can only ever be
        assigned an element that came out of the matching available_*
        collection — a string will not do.
        """
        options = list(options)
        if not options:
            raise ValueError("No options available for {0}".format(label))
        wanted_lower = str(wanted).strip().lower()
        for opt in options:
            if opt.display_name.strip().lower() == wanted_lower:
                return opt
        partial = [o for o in options
                   if wanted_lower in o.display_name.strip().lower()]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            raise ValueError("'{0}' is ambiguous for {1}, matches: {2}".format(
                wanted, label, ", ".join(o.display_name for o in partial)))
        raise ValueError("'{0}' not found for {1}. Available: {2}".format(
            wanted, label, ", ".join(o.display_name for o in options)))

    def _device_io_buses(self, device):
        """Map the four Device.IO collections a Max device can expose."""
        return [
            ("audio_in", getattr(device, "audio_inputs", None)),
            ("audio_out", getattr(device, "audio_outputs", None)),
            ("midi_in", getattr(device, "midi_inputs", None)),
            ("midi_out", getattr(device, "midi_outputs", None)),
        ]

    def _describe_device_io(self, io_obj):
        """Summarise one Device.IO bus."""
        info = {}
        for attr in ("routing_type", "routing_channel"):
            try:
                info[attr] = getattr(io_obj, attr).display_name
            except Exception:
                info[attr] = None
        for attr, key in (("available_routing_types", "available_types"),
                          ("available_routing_channels", "available_channels")):
            try:
                info[key] = [x.display_name for x in getattr(io_obj, attr)]
            except Exception:
                info[key] = []
        try:
            info["default_external_routing_channel_is_none"] = \
                io_obj.default_external_routing_channel_is_none
        except Exception:
            pass
        return info

    def _describe_device_parameter(self, param, index):
        """One parameter, in the same shape _get_device_parameters returns.

        value is normalized 0.0-1.0 so it can be round-tripped straight into
        set_device_parameter; index is the 0-based position in
        device.parameters, which is what parameter_index addresses.
        """
        pmin = param.min
        pmax = param.max
        raw = param.value
        norm = (raw - pmin) / (pmax - pmin) if pmax != pmin else 0.0
        entry = {
            "index": index,
            "name": param.name,
            "value": round(norm, 4),
            "raw_value": raw,
            "min": pmin,
            "max": pmax,
            "display_value": str(param),
        }
        try:
            entry["is_quantized"] = bool(param.is_quantized)
        except Exception:
            entry["is_quantized"] = False
        return entry

    def _get_plugin_info(self, track_index, device_index, chain_index=None,
                         bank=None):
        """Report what a VST/AU plugin or Max for Live device actually exposes.

        A plugin is a black box to Live: only the parameters the plugin
        publishes (or that were mapped by hand in Configure mode) exist in the
        API. This says which of them there are, how the plugin groups them into
        banks, and which of its own programs/presets is selected.
        """
        try:
            track, device = self._external_device_at(
                track_index, device_index, chain_index)

            class_name = getattr(device, "class_name", "")
            all_params = list(getattr(device, "parameters", []))
            info = {
                "track": track.name,
                "device": device.name,
                "class_name": class_name,
                "is_plugin": class_name == "PluginDevice",
                "is_max_device": class_name == "MaxDevice",
                "parameter_count": len(all_params),
            }

            presets = []
            try:
                presets = [str(p) for p in getattr(device, "presets", [])]
            except Exception:
                presets = []
            info["presets"] = presets
            info["preset_count"] = len(presets)
            info["selected_preset_index"] = None
            info["selected_preset"] = None
            if presets and hasattr(device, "selected_preset_index"):
                try:
                    idx = int(device.selected_preset_index)
                    info["selected_preset_index"] = idx
                    if 0 <= idx < len(presets):
                        info["selected_preset"] = presets[idx]
                except Exception:
                    pass

            bank_count = 0
            if hasattr(device, "get_bank_count"):
                try:
                    bank_count = int(device.get_bank_count())
                except Exception:
                    bank_count = 0
            info["bank_count"] = bank_count

            bank_names = []
            if bank_count and hasattr(device, "get_bank_name"):
                for i in range(bank_count):
                    try:
                        bank_names.append(str(device.get_bank_name(i)))
                    except Exception:
                        bank_names.append("Bank {0}".format(i + 1))
            info["bank_names"] = bank_names

            if bank is not None:
                b = int(bank)
                if not hasattr(device, "get_bank_parameters") or not bank_count:
                    raise ValueError(
                        "'{0}' does not expose parameter banks. Only VST/AU "
                        "plugins and Max devices do.".format(device.name))
                if b < 0 or b >= bank_count:
                    raise ValueError("Bank {0} out of range (0-{1})".format(
                        b, bank_count - 1))
                raw = list(device.get_bank_parameters(b))
                params = []
                for entry in raw:
                    # Live returns INDICES into device.parameters, -1 = empty
                    if isinstance(entry, int):
                        if entry < 0 or entry >= len(all_params):
                            params.append(None)
                            continue
                        p_index = entry
                        p = all_params[entry]
                    else:
                        p = entry
                        p_index = None
                        for j, cand in enumerate(all_params):
                            if cand is p:
                                p_index = j
                                break
                    if p is None:
                        params.append(None)
                        continue
                    try:
                        params.append(self._describe_device_parameter(p, p_index))
                    except Exception:
                        params.append(None)
                info["bank"] = b
                info["bank_name"] = bank_names[b] if b < len(bank_names) else None
                info["bank_parameters"] = params

            io_summary = {}
            for label, collection in self._device_io_buses(device):
                if collection is None:
                    continue
                try:
                    io_summary[label] = len(tuple(collection))
                except Exception:
                    pass
            info["device_io_buses"] = io_summary
            info["has_sidechain_input"] = hasattr(
                device, "available_input_routing_types")

            return info
        except Exception as e:
            self.log_message("Error getting plugin info: " + str(e))
            raise

    def _select_plugin_preset(self, track_index, device_index, chain_index=None,
                              preset=None, preset_index=None):
        """Select a VST/AU program or Max for Live preset by name or number.

        `presets` is a read-only tuple of names and `selected_preset_index` is
        the writable int — so a name is always resolved against the tuple
        rather than assigned.
        """
        try:
            track, device = self._external_device_at(
                track_index, device_index, chain_index)

            presets = []
            try:
                presets = [str(p) for p in getattr(device, "presets", [])]
            except Exception:
                presets = []
            if not presets or not hasattr(device, "selected_preset_index"):
                raise ValueError(
                    "'{0}' exposes no selectable preset list. Only VST/AU "
                    "plugins and some Max devices do; load stock Live patches "
                    "through the browser instead.".format(device.name))

            target = None
            if preset is not None:
                wanted = str(preset).strip().lower()
                for i, name in enumerate(presets):
                    if name.strip().lower() == wanted:
                        target = i
                        break
                if target is None:
                    partial = [i for i, name in enumerate(presets)
                               if wanted in name.strip().lower()]
                    if len(partial) == 1:
                        target = partial[0]
                    elif len(partial) > 1:
                        raise ValueError("'{0}' is ambiguous, matches: {1}".format(
                            preset, ", ".join(presets[i] for i in partial[:12])))
                if target is None:
                    raise ValueError(
                        "Preset '{0}' not found on '{1}'. First few: {2}".format(
                            preset, device.name, ", ".join(presets[:12])))
            elif preset_index is not None:
                target = int(preset_index)
            else:
                raise ValueError("Provide either preset or preset_index")

            if target < 0 or target >= len(presets):
                raise ValueError("Preset index {0} out of range (0-{1})".format(
                    target, len(presets) - 1))

            device.selected_preset_index = target
            try:
                now = int(device.selected_preset_index)
            except Exception:
                now = target
            return {
                "track": track.name,
                "device": device.name,
                "preset": presets[now] if 0 <= now < len(presets) else None,
                "preset_index": now,
                "preset_count": len(presets),
            }
        except Exception as e:
            self.log_message("Error selecting plugin preset: " + str(e))
            raise

    def _get_device_io(self, track_index, device_index, chain_index=None):
        """List every audio/MIDI bus a device exposes, and how each is routed.

        Max for Live devices expose audio_inputs / audio_outputs /
        midi_inputs / midi_outputs as Device.IO objects. Native Live devices
        that can be sidechained (Compressor, Gate, Auto Filter) instead expose
        input_routing_type directly on the device.
        """
        try:
            track, device = self._external_device_at(
                track_index, device_index, chain_index)

            buses = []
            for label, collection in self._device_io_buses(device):
                if collection is None:
                    continue
                for i, io_obj in enumerate(tuple(collection)):
                    entry = {"bus": label, "index": i,
                             "name": getattr(io_obj, "name", None)}
                    entry.update(self._describe_device_io(io_obj))
                    buses.append(entry)

            sidechain = None
            if hasattr(device, "available_input_routing_types"):
                sidechain = {}
                try:
                    sidechain["routing_type"] = device.input_routing_type.display_name
                except Exception:
                    sidechain["routing_type"] = None
                try:
                    sidechain["routing_channel"] = \
                        device.input_routing_channel.display_name
                except Exception:
                    sidechain["routing_channel"] = None
                try:
                    sidechain["available_types"] = [
                        x.display_name for x in device.available_input_routing_types]
                except Exception:
                    sidechain["available_types"] = []
                try:
                    sidechain["available_channels"] = [
                        x.display_name
                        for x in device.available_input_routing_channels]
                except Exception:
                    sidechain["available_channels"] = []

            return {
                "track": track.name,
                "device": device.name,
                "class_name": getattr(device, "class_name", ""),
                "buses": buses,
                "sidechain_input": sidechain,
            }
        except Exception as e:
            self.log_message("Error getting device IO: " + str(e))
            raise

    def _set_device_io_routing(self, track_index, device_index, bus=None,
                               bus_index=0, chain_index=None,
                               routing_type=None, routing_channel=None):
        """Point one of a Max device's audio/MIDI buses at a source or target.

        routing_type and routing_channel are Live objects, so the requested
        display name is resolved against available_routing_types /
        available_routing_channels on that same bus and the resulting element
        is assigned. The type is resolved and assigned first, because
        assigning it rewrites available_routing_channels.
        """
        try:
            track, device = self._external_device_at(
                track_index, device_index, chain_index)

            key = str(bus).strip().lower()
            aliases = {
                "audio_in": "audio_in", "audio_input": "audio_in",
                "audio_inputs": "audio_in", "in": "audio_in",
                "audio_out": "audio_out", "audio_output": "audio_out",
                "audio_outputs": "audio_out", "out": "audio_out",
                "midi_in": "midi_in", "midi_input": "midi_in",
                "midi_inputs": "midi_in",
                "midi_out": "midi_out", "midi_output": "midi_out",
                "midi_outputs": "midi_out",
            }
            if key in ("sidechain", "sidechain_input"):
                raise ValueError(
                    "Sidechain input lives on the device itself, not on a "
                    "Device.IO bus. Use set_device_sidechain for Compressor, "
                    "Gate and Auto Filter.")
            if key not in aliases:
                raise ValueError(
                    "Unknown bus '{0}'. Use audio_in, audio_out, midi_in "
                    "or midi_out.".format(bus))
            key = aliases[key]

            collection = None
            for label, coll in self._device_io_buses(device):
                if label == key:
                    collection = coll
                    break
            if collection is None:
                raise ValueError(
                    "'{0}' exposes no {1} buses. Only Max for Live devices "
                    "expose Device.IO routing.".format(device.name, key))

            buses = tuple(collection)
            bi = int(bus_index)
            if bi < 0 or bi >= len(buses):
                raise ValueError("{0} bus {1} out of range (0-{2}) on '{3}'".format(
                    key, bi, len(buses) - 1 if buses else 0, device.name))
            io_obj = buses[bi]

            changed = {}
            if routing_type is not None:
                if not hasattr(io_obj, "available_routing_types"):
                    raise ValueError(
                        "{0}[{1}] on '{2}' exposes no routing types".format(
                            key, bi, device.name))
                match = self._match_routing_option(
                    io_obj.available_routing_types, routing_type,
                    "{0}[{1}] routing_type".format(key, bi))
                io_obj.routing_type = match
                changed["routing_type"] = io_obj.routing_type.display_name
            if routing_channel is not None:
                if not hasattr(io_obj, "available_routing_channels"):
                    raise ValueError(
                        "{0}[{1}] on '{2}' exposes no routing channels".format(
                            key, bi, device.name))
                match = self._match_routing_option(
                    io_obj.available_routing_channels, routing_channel,
                    "{0}[{1}] routing_channel".format(key, bi))
                io_obj.routing_channel = match
                changed["routing_channel"] = io_obj.routing_channel.display_name

            if not changed:
                raise ValueError(
                    "Nothing to change: pass routing_type and/or routing_channel")

            return {
                "track": track.name,
                "device": device.name,
                "bus": key,
                "bus_index": bi,
                "changed": changed,
            }
        except Exception as e:
            self.log_message("Error setting device IO routing: " + str(e))
            raise

    def _looper_at(self, track_index, device_index=None):
        """Find a Looper on a track — by index, else the first one found."""
        track = self._track_at(track_index)
        devices = tuple(track.devices)
        if device_index is not None:
            if device_index < 0 or device_index >= len(devices):
                raise ValueError(
                    "device_index out of range (0-{0}) on '{1}'".format(
                        len(devices) - 1, track.name))
            looper = devices[device_index]
        else:
            looper = None
            for d in devices:
                if d.class_name == "Looper" or "looper" in d.name.lower():
                    looper = d
                    break
        if looper is None:
            raise ValueError("No Looper found on '{0}'".format(track.name))
        return track, looper

    def _looper_state(self, looper):
        """Snapshot of a Looper: transport state, size, tempo, settings."""
        info = {"device": looper.name}
        try:
            info["device_class"] = looper.class_name
        except Exception:
            pass
        try:
            for p in looper.parameters:
                if p.name != "State":
                    continue
                info["state_display"] = str(p)
                info["state_value"] = p.value
                if getattr(p, "is_quantized", False):
                    items = [str(v) for v in p.value_items]
                    info["state_options"] = items
                    idx = int(round(p.value))
                    if 0 <= idx < len(items):
                        info["state"] = items[idx]
                break
        except Exception:
            pass
        for attr in ("loop_length", "tempo", "overdub_after_record"):
            if not hasattr(looper, attr):
                continue
            try:
                info[attr] = self._describe_value(getattr(looper, attr))
            except Exception:
                pass
        if hasattr(looper, "record_length_list") and \
                hasattr(looper, "record_length_index"):
            try:
                names = [str(n) for n in looper.record_length_list]
                info["record_length_options"] = names
                idx = int(looper.record_length_index)
                info["record_length_index"] = idx
                if 0 <= idx < len(names):
                    info["record_length"] = names[idx]
            except Exception:
                pass
        info["available_actions"] = [
            a for a in ("record", "overdub", "play", "stop", "clear", "undo",
                        "double_length", "half_length", "double_speed",
                        "half_speed", "export_to_clip_slot")
            if hasattr(looper, a)]
        info["settable"] = [
            a for a in ("record_length", "overdub_after_record", "tempo")
            if hasattr(looper, a) or
            hasattr(looper, a + "_index")]
        return info

    def _control_looper(self, track_index, device_index=None, action="info",
                        scene_index=None):
        """Drive a Looper like a loop pedal — record, overdub, play, export."""
        try:
            track, looper = self._looper_at(track_index, device_index)

            if action == "info":
                result = self._looper_state(looper)
                result["track"] = track.name
                return result

            if not hasattr(looper, action):
                raise ValueError(
                    "Looper '{0}' has no action '{1}'. Available: {2}".format(
                        looper.name, action, ", ".join(
                            self._looper_state(looper)["available_actions"])))

            if action == "export_to_clip_slot":
                if scene_index is not None:
                    scenes = tuple(self._song.scenes)
                    if scene_index < 0 or scene_index >= len(scenes):
                        raise ValueError(
                            "scene_index out of range (0-{0})".format(
                                len(scenes) - 1))
                    try:
                        self._song.view.selected_track = track
                    except Exception as se:
                        self.log_message(
                            "Could not select looper track: " + str(se))
                    self._song.view.selected_scene = scenes[scene_index]
                looper.export_to_clip_slot()
                result = self._looper_state(looper)
                result["track"] = track.name
                result["action"] = action
                result["done"] = True
                result["exported_to_track"] = track.name
                if scene_index is not None:
                    result["exported_to_scene"] = scene_index
                return result

            getattr(looper, action)()
            result = self._looper_state(looper)
            result["track"] = track.name
            result["action"] = action
            result["done"] = True
            return result
        except Exception as e:
            self.log_message("Error controlling looper: " + str(e))
            raise

    def _configure_looper(self, track_index, device_index=None,
                          record_length=None, overdub_after_record=None,
                          tempo=None):
        """Set a Looper's record length (by name), overdub behaviour, tempo."""
        try:
            track, looper = self._looper_at(track_index, device_index)
            changed = {}
            unsupported = []
            read_only = []

            if record_length is not None:
                if not (hasattr(looper, "record_length_list") and
                        hasattr(looper, "record_length_index")):
                    unsupported.append("record_length")
                else:
                    names = [str(n) for n in looper.record_length_list]
                    wanted = str(record_length).strip().lower()
                    target = None
                    for i, n in enumerate(names):
                        if n.strip().lower() == wanted:
                            target = i
                            break
                    if target is None:
                        for i, n in enumerate(names):
                            if wanted in n.strip().lower():
                                target = i
                                break
                    if target is None:
                        raise ValueError(
                            "Unknown record_length '{0}'. Available: "
                            "{1}".format(record_length, ", ".join(names)))
                    looper.record_length_index = target
                    now = int(looper.record_length_index)
                    changed["record_length_index"] = now
                    if 0 <= now < len(names):
                        changed["record_length"] = names[now]
                    if now != target:
                        changed["record_length_requested"] = names[target]

            if overdub_after_record is not None:
                if not hasattr(looper, "overdub_after_record"):
                    unsupported.append("overdub_after_record")
                else:
                    try:
                        looper.overdub_after_record = bool(overdub_after_record)
                        changed["overdub_after_record"] = \
                            looper.overdub_after_record
                    except Exception as oe:
                        self.log_message(
                            "Looper overdub_after_record not writable: " +
                            str(oe))
                        read_only.append("overdub_after_record")

            if tempo is not None:
                if not hasattr(looper, "tempo"):
                    unsupported.append("tempo")
                else:
                    try:
                        looper.tempo = float(tempo)
                        changed["tempo"] = looper.tempo
                    except Exception as te:
                        self.log_message(
                            "Looper tempo not writable: " + str(te))
                        read_only.append("tempo")

            result = self._looper_state(looper)
            result["track"] = track.name
            result["changed"] = changed
            if read_only:
                result["read_only"] = read_only
            if unsupported:
                result["unsupported"] = unsupported
            return result
        except Exception as e:
            self.log_message("Error configuring looper: " + str(e))
            raise


    # ---------------------------------------------------------------
    # Rack family: RackDevice / Chain / DrumChain / DrumPad /
    # ChainMixerDevice.  A rack is the only place in Live where signal
    # topology, macro control and drum mapping all live on one object,
    # so these helpers all start from the same resolve step.
    #
    # Every LOM member touched here is hasattr/getattr guarded: a Drum
    # Rack has no chain_selector, an Instrument Rack has no drum_pads,
    # and older Live builds lack the variation API entirely.  Without
    # the guards those calls raise a bare AttributeError with no hint
    # of what actually went wrong.
    # ---------------------------------------------------------------

    def _rack_device(self, track_index, device_index, require_pads=False):
        """Resolve a device and confirm it really is a rack."""
        track, device = self._resolve_device(track_index, device_index)
        if not getattr(device, "can_have_chains", False):
            raise ValueError("'{0}' is not a rack".format(device.name))
        if require_pads and not getattr(device, "can_have_drum_pads", False):
            raise ValueError("'{0}' is not a Drum Rack".format(device.name))
        return track, device

    def _param_enum_pair(self, param):
        """Describe a quantized (enum) parameter as an index + name list.

        Quantized parameters in Live are enums: the float value is an index
        into value_items, and that ordering is NOT guaranteed across Live
        versions or device variants.  Always report and resolve by name.
        """
        out = {"index": None, "list": [], "name": None}
        try:
            out["list"] = [str(i) for i in param.value_items]
        except Exception:
            out["list"] = []
        try:
            out["index"] = int(round(param.value))
        except Exception:
            out["index"] = None
        idx = out["index"]
        if out["list"] and idx is not None and 0 <= idx < len(out["list"]):
            out["name"] = out["list"][idx]
        else:
            try:
                out["name"] = str(param)
            except Exception:
                out["name"] = None
        return out

    def _set_param_enum(self, param, wanted):
        """Set a quantized parameter from a name, a bool, or an index.

        Returns the resolved name.  Never assumes 0 = off / 1 = on: the
        value_items list is the authority, and only when the device does
        not expose one do we fall back to param.min / param.max.
        """
        try:
            items = [str(i) for i in param.value_items]
        except Exception:
            items = []

        if isinstance(wanted, bool):
            if items:
                target = "on" if wanted else "off"
                for i, item in enumerate(items):
                    if item.strip().lower() == target:
                        param.value = float(i)
                        return items[i]
            param.value = param.max if wanted else param.min
            return str(param)

        if isinstance(wanted, str):
            if not items:
                raise ValueError(
                    "'{0}' has no named values to match '{1}'".format(
                        param.name, wanted))
            for i, item in enumerate(items):
                if item.strip().lower() == wanted.strip().lower():
                    param.value = float(i)
                    return items[i]
            raise ValueError(
                "No value named '{0}' on '{1}'. Available: {2}".format(
                    wanted, param.name, ", ".join(items)))

        idx = int(wanted)
        if items:
            if idx < 0 or idx >= len(items):
                raise IndexError(
                    "Value index {0} out of range on '{1}' (0-{2}: {3})".format(
                        idx, param.name, len(items) - 1, ", ".join(items)))
            param.value = float(idx)
            return items[idx]
        param.value = float(idx)
        return str(param)

    def _drum_pad_at(self, device, note):
        """Find a DrumPad by its MIDI note (36 = C1 = bottom-left pad)."""
        if not getattr(device, "can_have_drum_pads", False):
            raise ValueError(
                "'{0}' is not a Drum Rack, so it has no pads".format(
                    device.name))
        target = int(note)
        for pad in device.drum_pads:
            if pad.note == target:
                return pad
        raise ValueError("No drum pad at note {0} on '{1}'".format(
            target, device.name))

    def _rack_chain_at(self, device, chain_index=None, chain_name=None,
                       pad_note=None, return_chain=False):
        """Resolve a single Chain inside a rack.

        pad_note selects a Drum Rack pad's chain, return_chain selects from
        the rack's internal return chains, otherwise the main chain list is
        used. chain_name wins over chain_index when both are given.
        """
        if pad_note is not None:
            pad = self._drum_pad_at(device, pad_note)
            chains = list(pad.chains)
            label = "pad {0} chain".format(pad.note)
            if not chains:
                raise ValueError("Drum pad {0} ('{1}') is empty".format(
                    pad.note, pad.name))
        elif return_chain:
            chains = list(getattr(device, "return_chains", []))
            label = "return chain"
        else:
            chains = list(device.chains)
            label = "chain"
        if not chains:
            raise ValueError("'{0}' has no {1}s".format(device.name, label))
        if chain_name is not None:
            for chain in chains:
                if chain.name == chain_name:
                    return chain, label
            raise ValueError(
                "No {0} named '{1}' in '{2}'. Available: {3}".format(
                    label, chain_name, device.name,
                    ", ".join(c.name for c in chains)))
        idx = 0 if chain_index is None else int(chain_index)
        if idx < 0 or idx >= len(chains):
            raise IndexError("{0} index {1} out of range (0-{2})".format(
                label.capitalize(), idx, len(chains) - 1))
        return chains[idx], label

    def _describe_chain(self, chain, index=None, with_devices=True):
        """Serialise a Chain / DrumChain (and its ChainMixerDevice)."""
        info = {"index": index, "name": chain.name}
        for attr in ("mute", "solo", "color_index", "out_note",
                     "choke_group"):
            try:
                info[attr] = getattr(chain, attr)
            except Exception:
                pass
        if with_devices:
            try:
                info["devices"] = [d.name for d in chain.devices]
            except Exception:
                info["devices"] = []
        mixer = getattr(chain, "mixer_device", None)
        if mixer is not None:
            try:
                info["volume"] = str(mixer.volume)
            except Exception:
                pass
            try:
                info["panning"] = str(mixer.panning)
            except Exception:
                pass
            try:
                act = self._param_enum_pair(mixer.chain_activator)
                info["chain_activator_index"] = act["index"]
                info["chain_activator_name"] = act["name"]
                info["chain_activator_list"] = act["list"]
            except Exception:
                pass
            try:
                info["sends"] = [str(s) for s in mixer.sends]
            except Exception:
                pass
        return info

    def _get_rack_map(self, track_index, device_index, include_devices=True,
                      pads="filled"):
        """One read of everything a rack exposes: macros, chains, pads.

        pads: 'filled' (pads holding something), 'visible' (the 16 currently
        on screen) or 'none'.
        """
        try:
            track, device = self._rack_device(track_index, device_index)
            info = {"track": track.name, "device": device.name,
                    "class_name": device.class_name}
            for attr in ("visible_macro_count", "has_macro_mappings",
                         "variation_count", "selected_variation_index",
                         "is_showing_chains", "can_show_chains",
                         "has_drum_pads"):
                try:
                    info[attr] = getattr(device, attr)
                except Exception:
                    pass
            try:
                info["macros_mapped"] = list(device.macros_mapped)
            except Exception:
                pass
            try:
                info["macros"] = [
                    {"index": i, "name": p.name, "value": p.value,
                     "display": str(p)}
                    for i, p in enumerate(device.parameters)
                    if "Macro" in p.name]
            except Exception:
                pass
            try:
                selector = device.chain_selector
                info["chain_selector"] = {"value": selector.value,
                                          "min": selector.min,
                                          "max": selector.max,
                                          "display": str(selector)}
            except Exception:
                pass
            try:
                info["chains"] = [
                    self._describe_chain(c, ci, include_devices)
                    for ci, c in enumerate(device.chains)]
            except Exception:
                info["chains"] = []
            try:
                info["return_chains"] = [
                    self._describe_chain(c, ci, include_devices)
                    for ci, c in enumerate(device.return_chains)]
            except Exception:
                info["return_chains"] = []
            info["chain_count"] = len(info.get("chains", []))

            if pads != "none" and getattr(device, "can_have_drum_pads", False):
                pad_objects = (device.visible_drum_pads if pads == "visible"
                               else device.drum_pads)
                pad_list = []
                for pad in pad_objects:
                    if pads == "filled" and not pad.chains:
                        continue
                    entry = {"note": pad.note, "name": pad.name}
                    for attr in ("mute", "solo", "choke_group", "out_note"):
                        try:
                            entry[attr] = getattr(pad, attr)
                        except Exception:
                            pass
                    # choke_group / out_note live on DrumChain in some Live
                    # builds, on DrumPad in others -- read whichever answers.
                    for chain in pad.chains:
                        for attr in ("choke_group", "out_note"):
                            if attr not in entry:
                                try:
                                    entry[attr] = getattr(chain, attr)
                                except Exception:
                                    pass
                    entry["devices"] = [d.name for c in pad.chains
                                        for d in c.devices]
                    pad_list.append(entry)
                info["pads"] = pad_list
                info["pad_filter"] = pads
            return info
        except Exception as e:
            self.log_message("Error getting rack map: " + str(e))
            raise

    def _manage_rack(self, track_index, device_index, action="info",
                     value=None, count=1, chain_index=None):
        """Inspect and control a rack: macros, chain selector, chain list.

        Supersedes the earlier _manage_rack. All previous actions still
        work; the variation actions now delegate to
        _manage_rack_variations so there is a single implementation.
        """
        try:
            track, device = self._rack_device(track_index, device_index)

            if action == "info":
                return self._get_rack_map(track_index, device_index)

            if action in ("add_macro", "remove_macro"):
                if not hasattr(device, action):
                    raise ValueError(
                        "'{0}' does not support {1} in this Live "
                        "version".format(device.name, action))
                wanted = max(1, int(count))
                applied = 0
                for _ in range(wanted):
                    try:
                        if action == "add_macro":
                            device.add_macro()
                        else:
                            device.remove_macro()
                        applied += 1
                    except Exception as inner:
                        self.log_message(
                            "Stopped {0} after {1}: {2}".format(
                                action, applied, str(inner)))
                        break
                return {"device": device.name, "action": action,
                        "applied": applied, "requested": wanted,
                        "visible_macro_count": getattr(
                            device, "visible_macro_count", None)}

            if action == "randomize_macros":
                if not hasattr(device, "randomize_macros"):
                    raise ValueError(
                        "'{0}' cannot randomize macros".format(device.name))
                device.randomize_macros()
                return {"device": device.name, "action": "randomize_macros",
                        "macros": [{"name": p.name, "display": str(p)}
                                   for p in device.parameters
                                   if "Macro" in p.name]}

            if action == "chain_selector":
                if value is None:
                    raise ValueError("chain_selector requires a value 0.0-1.0")
                selector = getattr(device, "chain_selector", None)
                if selector is None:
                    raise ValueError(
                        "'{0}' has no chain selector (Drum Racks do "
                        "not)".format(device.name))
                target = selector.min + (selector.max - selector.min) * \
                    max(0.0, min(1.0, float(value)))
                selector.value = target
                return {"device": device.name,
                        "chain_selector": selector.value,
                        "display": str(selector)}

            if action in ("show_chains", "hide_chains"):
                if not getattr(device, "can_show_chains", False):
                    raise ValueError(
                        "'{0}' cannot show its chains".format(device.name))
                device.is_showing_chains = (action == "show_chains")
                return {"device": device.name, "action": action,
                        "is_showing_chains": device.is_showing_chains}

            if action == "insert_chain":
                if not hasattr(device, "insert_chain"):
                    raise ValueError(
                        "'{0}' does not expose insert_chain in this Live "
                        "version".format(device.name))
                before = len(device.chains)
                pos = before if chain_index is None else int(chain_index)
                last_error = None
                inserted = False
                for args in ((pos,), ()):
                    try:
                        device.insert_chain(*args)
                        inserted = True
                        break
                    except Exception as inner:
                        last_error = inner
                if not inserted:
                    raise ValueError(
                        "insert_chain is not callable on '{0}': {1}".format(
                            device.name, str(last_error)))
                return {"device": device.name, "action": "insert_chain",
                        "chain_count": len(device.chains),
                        "was": before,
                        "chains": [c.name for c in device.chains]}

            if action in ("store_variation", "recall_variation",
                          "delete_variation", "recall_last_variation"):
                legacy = {"store_variation": "store",
                          "recall_variation": "recall",
                          "delete_variation": "delete",
                          "recall_last_variation": "recall_last"}
                return self._manage_rack_variations(
                    track_index, device_index, legacy[action],
                    None if value is None else int(value))

            raise ValueError("Unknown rack action '{0}'".format(action))
        except Exception as e:
            self.log_message("Error managing rack: " + str(e))
            raise

    def _manage_rack_variations(self, track_index, device_index,
                                action="list", variation_index=None):
        """Store / recall / delete a rack's macro variations (snapshots)."""
        try:
            track, device = self._rack_device(track_index, device_index)
            if not hasattr(device, "variation_count"):
                raise ValueError(
                    "'{0}' has no macro variations (needs Live 11 or "
                    "later)".format(device.name))

            def require(method):
                if not hasattr(device, method):
                    raise ValueError(
                        "'{0}' does not expose {1} in this Live "
                        "version".format(device.name, method))

            def state(extra=None):
                out = {"device": device.name,
                       "variation_count": device.variation_count,
                       "selected_variation_index": getattr(
                           device, "selected_variation_index", None)}
                if extra:
                    out.update(extra)
                return out

            if variation_index is not None and action in ("recall", "delete",
                                                          "select"):
                idx = int(variation_index)
                if idx < 0 or idx >= device.variation_count:
                    raise IndexError(
                        "Variation index {0} out of range (0-{1})".format(
                            idx, device.variation_count - 1))
                device.selected_variation_index = idx

            if action == "list":
                return state({"action": "list"})
            if action == "store":
                require("store_variation")
                device.store_variation()
                return state({"action": "store"})
            if action == "select":
                if variation_index is None:
                    raise ValueError("select requires a variation_index")
                return state({"action": "select"})
            if action == "recall":
                require("recall_selected_variation")
                device.recall_selected_variation()
                return state({"action": "recall"})
            if action == "recall_last":
                require("recall_last_used_variation")
                device.recall_last_used_variation()
                return state({"action": "recall_last"})
            if action == "delete":
                require("delete_selected_variation")
                device.delete_selected_variation()
                return state({"action": "delete"})
            raise ValueError(
                "Unknown variation action '{0}'".format(action))
        except Exception as e:
            self.log_message("Error managing rack variations: " + str(e))
            raise

    def _set_chain_state(self, track_index, device_index, chain_index=None,
                         chain_name=None, pad_note=None, return_chain=False,
                         name=None, color_index=None, mute=None, solo=None,
                         volume=None, panning=None, chain_activator=None,
                         send_index=None, send_value=None):
        """Set name / colour / mute / solo / mixer on one chain in a rack.

        Omitted values are left alone. volume, panning and send_value are
        normalized 0.0-1.0 across the parameter's own min..max, matching how
        sends and mixer extras behave elsewhere. chain_activator is a
        quantized (enum) parameter and is resolved by NAME, never by an
        assumed 0/1 ordering.
        """
        try:
            # pad_note reaches into device.drum_pads, which only exists on a
            # Drum Rack -- require it up front instead of raising a bare
            # AttributeError from inside the lookup.
            track, device = self._rack_device(
                track_index, device_index, require_pads=pad_note is not None)
            chain, label = self._rack_chain_at(
                device, chain_index, chain_name, pad_note, return_chain)
            changed = {}

            def set_param(param, val, key):
                target = param.min + (param.max - param.min) * \
                    max(0.0, min(1.0, float(val)))
                param.value = target
                changed[key] = str(param)

            if name is not None:
                chain.name = str(name)
                changed["name"] = chain.name
            if color_index is not None:
                chain.color_index = int(color_index)
                changed["color_index"] = chain.color_index
            if mute is not None:
                chain.mute = bool(mute)
                changed["mute"] = chain.mute
            if solo is not None:
                chain.solo = bool(solo)
                changed["solo"] = chain.solo

            if (volume is not None or panning is not None
                    or chain_activator is not None or send_value is not None):
                mixer = getattr(chain, "mixer_device", None)
                if mixer is None:
                    raise ValueError(
                        "Chain '{0}' has no mixer".format(chain.name))
                if volume is not None:
                    set_param(mixer.volume, volume, "volume")
                if panning is not None:
                    set_param(mixer.panning, panning, "panning")
                if chain_activator is not None:
                    activator = getattr(mixer, "chain_activator", None)
                    if activator is None:
                        raise ValueError(
                            "Chain '{0}' has no chain activator".format(
                                chain.name))
                    changed["chain_activator"] = self._set_param_enum(
                        activator, chain_activator)
                if send_value is not None:
                    if send_index is None:
                        raise ValueError(
                            "send_value requires a send_index")
                    sends = mixer.sends
                    si = int(send_index)
                    if si < 0 or si >= len(sends):
                        raise IndexError(
                            "Send index {0} out of range on chain "
                            "'{1}' (0-{2})".format(
                                si, chain.name, len(sends) - 1))
                    set_param(sends[si], send_value,
                              "send_{0}".format(si))

            return {"device": device.name, "target": label,
                    "chain": chain.name, "changed": changed}
        except Exception as e:
            self.log_message("Error setting chain state: " + str(e))
            raise

    def _manage_drum_pad(self, track_index, device_index, pad_note,
                         name=None, mute=None, solo=None, choke_group=None,
                         out_note=None, copy_to_note=None):
        """Set name / mute / solo / choke group / out note on a drum pad.

        choke_group and out_note are exposed on DrumPad in some Live builds
        and on the pad's DrumChain in others, so both are attempted.
        A choke group (1-16) makes pads cut each other off -- the open hat
        / closed hat trick.
        """
        try:
            track, device = self._rack_device(track_index, device_index,
                                              require_pads=True)
            pad = self._drum_pad_at(device, pad_note)
            changed = {}

            def set_on_pad_or_chains(attr, val, caster):
                applied = False
                try:
                    setattr(pad, attr, caster(val))
                    applied = True
                except Exception:
                    pass
                for chain in pad.chains:
                    try:
                        setattr(chain, attr, caster(val))
                        applied = True
                    except Exception:
                        pass
                if not applied:
                    raise ValueError(
                        "Pad {0} does not accept {1}".format(pad.note, attr))
                changed[attr] = caster(val)

            if name is not None:
                try:
                    pad.name = str(name)
                    changed["name"] = pad.name
                except Exception:
                    if not pad.chains:
                        raise ValueError(
                            "Pad {0} is empty, nothing to name".format(
                                pad.note))
                    pad.chains[0].name = str(name)
                    changed["chain_name"] = pad.chains[0].name
            if mute is not None:
                pad.mute = bool(mute)
                changed["mute"] = pad.mute
            if solo is not None:
                pad.solo = bool(solo)
                changed["solo"] = pad.solo
            if choke_group is not None:
                set_on_pad_or_chains("choke_group", choke_group, int)
            if out_note is not None:
                set_on_pad_or_chains("out_note", out_note, int)

            if copy_to_note is not None:
                if not hasattr(device, "copy_pad"):
                    raise ValueError(
                        "'{0}' does not expose copy_pad in this Live "
                        "version".format(device.name))
                dest = self._drum_pad_at(device, copy_to_note)
                last_error = None
                copied = False
                for args in ((pad.note, dest.note), (pad, dest)):
                    try:
                        device.copy_pad(args[0], args[1])
                        copied = True
                        break
                    except Exception as inner:
                        last_error = inner
                if not copied:
                    raise ValueError(
                        "copy_pad from {0} to {1} failed: {2}".format(
                            pad.note, dest.note, str(last_error)))
                changed["copied_to_note"] = dest.note

            return {"device": device.name, "note": pad.note,
                    "pad": pad.name, "changed": changed}
        except Exception as e:
            self.log_message("Error managing drum pad: " + str(e))
            raise

    def _simpler_enum_names(self, obj, prop, fallback):
        """Names for an int-valued Simpler/Sample property.

        Prefers a companion list published by Live (so we never assume an
        ordering Live could change), falls back to the static table.
        """
        for attr in SIMPLER_ENUM_LISTS.get(prop, ()):
            try:
                values = list(getattr(obj, attr))
            except Exception:
                continue
            names = []
            for v in values:
                text = None
                for name_attr in ("display_name", "name"):
                    try:
                        text = str(getattr(v, name_attr))
                        break
                    except Exception:
                        continue
                names.append(text if text is not None else str(v))
            if names:
                return tuple(n.strip().lower().replace(" ", "_")
                             .replace("-", "_") for n in names)
        return tuple(fallback)

    def _simpler_enum_name(self, obj, prop, fallback):
        """Bounds-checked name for obj.<prop>, or None.

        Bounds are checked in BOTH directions: Python's negative indexing would
        otherwise turn an unexpected -1 into a confident, wrong name.
        """
        try:
            index = int(getattr(obj, prop))
        except Exception:
            return None
        names = self._simpler_enum_names(obj, prop, fallback)
        if 0 <= index < len(names):
            return names[index]
        return None

    def _simpler_enum_report(self, obj, prop, fallback):
        """'2 (slicing)' when the name is known, otherwise just '2'."""
        value = getattr(obj, prop)
        name = self._simpler_enum_name(obj, prop, fallback)
        if name is None:
            return value
        return "{0} ({1})".format(value, name)

    def _simpler_at(self, track_index, device_index, chain_path=None):
        """Resolve a Simpler, optionally nested inside racks.

        chain_path is a dotted list of alternating 0-based chain/device
        indices, e.g. "0.0" means chains[0].devices[0] of the rack at
        device_index. Drum racks count too: a drum pad's chain is one of the
        rack's chains, so a Simpler under a pad is reachable the same way.
        """
        track = self._track_at(track_index)
        devices = tuple(track.devices)
        if device_index < 0 or device_index >= len(devices):
            raise IndexError(
                "Device index {0} out of range on track '{1}' (0-{2})".format(
                    device_index, track.name, len(devices) - 1 if devices else 0))
        device = devices[device_index]

        if chain_path:
            parts = [p for p in str(chain_path).split(".") if p != ""]
            if len(parts) % 2 != 0:
                raise ValueError(
                    "chain_path must be pairs of chain.device indices, got "
                    "'{0}'".format(chain_path))
            for i in range(0, len(parts), 2):
                ci = int(parts[i])
                di = int(parts[i + 1])
                if not getattr(device, "can_have_chains", False):
                    raise ValueError(
                        "'{0}' is not a rack, cannot follow chain_path "
                        "'{1}'".format(device.name, chain_path))
                chains = tuple(device.chains)
                if ci < 0 or ci >= len(chains):
                    raise IndexError(
                        "Chain index {0} out of range on '{1}' (0-{2})".format(
                            ci, device.name, len(chains) - 1 if chains else 0))
                chain = chains[ci]
                chain_devices = tuple(chain.devices)
                if di < 0 or di >= len(chain_devices):
                    raise IndexError(
                        "Device index {0} out of range in chain '{1}' "
                        "(0-{2})".format(di, chain.name,
                                         len(chain_devices) - 1
                                         if chain_devices else 0))
                device = chain_devices[di]

        # Identity gate: only Simpler carries BOTH of these. Sampler and the
        # rest of the instruments carry neither, so they fail here with a
        # readable message instead of an AttributeError deeper in.
        if not (hasattr(device, "playback_mode") and hasattr(device, "sample")):
            raise ValueError(
                "'{0}' ({1}) is not a Simpler. Sampler and the other "
                "instruments do not expose this API.".format(
                    device.name, getattr(device, "class_name", "?")))
        return track, device

    def _simpler_sample(self, device):
        """Return the Simpler's Sample, with the multisample case explained.

        Live returns None for `sample` whenever multi_sample_mode is on — the
        zones of a multisampled Simpler are not exposed to the API at all — and
        also before any file has been dropped in.
        """
        sample = device.sample
        if sample is None:
            if getattr(device, "multi_sample_mode", False):
                raise ValueError(
                    "'{0}' is in multi-sample mode, so Live exposes no single "
                    "Sample object. Load a one-shot/loop into a plain Simpler "
                    "to edit sample, slices or warping.".format(device.name))
            raise ValueError(
                "'{0}' has no sample loaded yet.".format(device.name))
        return sample

    def _require_member(self, obj, attr, label):
        """Fail early and readably when this Live build lacks a property."""
        if not hasattr(obj, attr):
            raise ValueError(
                "This Live version's {0} does not expose '{1}'.".format(
                    label, attr))

    def _resolve_simpler_enum(self, obj, prop, value, fallback, label):
        """Turn a name or an index into the int Live wants.

        Names are resolved against Live's own list when the object publishes
        one, and against the fallback table otherwise. A raw index always wins,
        so a build whose ordering differs from the table is still drivable.
        """
        if isinstance(value, bool):
            raise ValueError("{0} must be a name or an index".format(label))
        if isinstance(value, int):
            return int(value)
        if isinstance(value, float):
            if value != int(value):
                raise ValueError(
                    "{0} index must be a whole number, got {1}".format(
                        label, value))
            return int(value)
        names = self._simpler_enum_names(obj, prop, fallback)
        text = str(value).strip().lower().replace(" ", "_").replace("-", "_")
        if text.isdigit():
            return int(text)
        for i, name in enumerate(names):
            if name == text:
                return i
        partial = [i for i, name in enumerate(names) if text in name]
        if len(partial) == 1:
            return partial[0]
        raise ValueError(
            "Unknown {0} '{1}'. Use one of: {2} — or a raw index.".format(
                label, value, ", ".join(names)))

    def _sample_frames_at(self, sample, frames=None, beats=None,
                          label="position"):
        """Resolve a point in the sample to frames, from frames or beats."""
        if frames is not None:
            return int(frames)
        if beats is not None:
            return int(sample.beat_to_sample_time(float(beats)))
        raise ValueError("{0} needs a value in frames or in beats".format(label))

    def _describe_simpler(self, device):
        """Everything readable about a Simpler and its sample, in one dict."""
        info = {"device": device.name,
                "class_name": getattr(device, "class_name", None)}
        for attr in ("playback_mode", "slicing_playback_mode", "pad_slicing",
                     "multi_sample_mode", "voices", "retrigger",
                     "pitch_bend_range", "note_pitch_bend_range",
                     "playing_position", "playing_position_enabled",
                     "can_warp_as", "can_warp_half", "can_warp_double"):
            try:
                info[attr] = self._describe_value(getattr(device, attr))
            except Exception:
                pass
        # One try per name: an out-of-range value on one property must not
        # silently swallow the other's name.
        for prop, fallback in (
                ("playback_mode", SIMPLER_PLAYBACK_MODES),
                ("slicing_playback_mode", SIMPLER_SLICING_PLAYBACK_MODES)):
            try:
                name = self._simpler_enum_name(device, prop, fallback)
                if name is not None:
                    info[prop + "_name"] = name
            except Exception:
                pass

        sample = device.sample
        if sample is None:
            info["sample"] = None
            info["note"] = ("multi-sample mode — no Sample object"
                            if getattr(device, "multi_sample_mode", False)
                            else "no sample loaded")
            return info

        s = {}
        for attr in ("file_path", "length", "sample_rate", "start_marker",
                     "end_marker", "gain", "warping", "warp_mode",
                     "slicing_style", "slicing_beat_division",
                     "slicing_region_count", "slicing_sensitivity",
                     "beats_granulation_resolution", "beats_transient_envelope",
                     "beats_transient_loop_mode", "complex_pro_envelope",
                     "complex_pro_formants", "texture_flux",
                     "texture_grain_size", "tones_grain_size"):
            try:
                s[attr] = self._describe_value(getattr(sample, attr))
            except Exception:
                pass
        try:
            s["gain_display_string"] = sample.gain_display_string()
        except Exception:
            pass
        for prop, fallback in (("warp_mode", SAMPLE_WARP_MODES),
                               ("slicing_style", SAMPLE_SLICING_STYLES)):
            try:
                name = self._simpler_enum_name(sample, prop, fallback)
                if name is not None:
                    s[prop + "_name"] = name
            except Exception:
                pass
        try:
            slices = [int(x) for x in sample.slices]
            s["slice_count"] = len(slices)
            s["slices_frames"] = slices[:128]
            s["slices_beats"] = [round(sample.sample_to_beat_time(x), 4)
                                 for x in slices[:128]]
        except Exception:
            pass
        try:
            s["warp_markers"] = [{"beat_time": m.beat_time,
                                  "sample_time": m.sample_time}
                                 for m in sample.warp_markers][:64]
        except Exception:
            pass
        info["sample"] = s
        return info

    def _get_simpler_info(self, track_index, device_index, chain_path=None):
        """Report a Simpler's playback state, its sample and its slice grid.

        The read that has to happen before any chop: slice positions come back
        in both frames and beats, so the next call can speak whichever unit
        suits, and start/end markers show what portion is actually in play.
        """
        try:
            track, device = self._simpler_at(track_index, device_index,
                                             chain_path)
            info = self._describe_simpler(device)
            info["track"] = track.name
            return info
        except Exception as e:
            self.log_message("Error getting simpler info: " + str(e))
            raise

    def _set_simpler_playback(self, track_index, device_index, chain_path=None,
                              playback_mode=None, slicing_playback_mode=None,
                              pad_slicing=None, voices=None, retrigger=None,
                              pitch_bend_range=None, note_pitch_bend_range=None):
        """Set how a Simpler responds to notes. Omitted arguments are untouched.

        multi_sample_mode, playing_position and playing_position_enabled are
        read-only in Live and are reported by get_simpler_info instead.
        """
        try:
            track, device = self._simpler_at(track_index, device_index,
                                             chain_path)
            changed = {}

            if playback_mode is not None:
                index = self._resolve_simpler_enum(
                    device, "playback_mode", playback_mode,
                    SIMPLER_PLAYBACK_MODES, "playback_mode")
                device.playback_mode = index
                changed["playback_mode"] = self._simpler_enum_report(
                    device, "playback_mode", SIMPLER_PLAYBACK_MODES)

            if slicing_playback_mode is not None:
                self._require_member(device, "slicing_playback_mode", "Simpler")
                index = self._resolve_simpler_enum(
                    device, "slicing_playback_mode", slicing_playback_mode,
                    SIMPLER_SLICING_PLAYBACK_MODES, "slicing_playback_mode")
                device.slicing_playback_mode = index
                changed["slicing_playback_mode"] = self._simpler_enum_report(
                    device, "slicing_playback_mode",
                    SIMPLER_SLICING_PLAYBACK_MODES)

            if pad_slicing is not None:
                self._require_member(device, "pad_slicing", "Simpler")
                device.pad_slicing = bool(pad_slicing)
                changed["pad_slicing"] = device.pad_slicing

            if voices is not None:
                device.voices = int(voices)
                changed["voices"] = device.voices

            if retrigger is not None:
                device.retrigger = bool(retrigger)
                changed["retrigger"] = device.retrigger

            if pitch_bend_range is not None:
                device.pitch_bend_range = int(pitch_bend_range)
                changed["pitch_bend_range"] = device.pitch_bend_range

            if note_pitch_bend_range is not None:
                self._require_member(device, "note_pitch_bend_range", "Simpler")
                device.note_pitch_bend_range = int(note_pitch_bend_range)
                changed["note_pitch_bend_range"] = device.note_pitch_bend_range

            return {"track": track.name, "device": device.name,
                    "changed": changed,
                    "multi_sample_mode": getattr(device, "multi_sample_mode",
                                                 None)}
        except Exception as e:
            self.log_message("Error setting simpler playback: " + str(e))
            raise

    def _manage_simpler_sample(self, track_index, device_index, chain_path=None,
                               action=None, start_frames=None, start_beats=None,
                               end_frames=None, end_beats=None, gain=None,
                               warping=None, warp_mode=None, warp_as_beats=None):
        """Trim, gain, warp and reshape the sample loaded into a Simpler.

        Markers are sample frames in the API; beats are converted through
        Sample.beat_to_sample_time. gain is normalised 0.0-1.0 (0.4 reads as
        0.0 dB), not decibels. crop is destructive and permanent.
        """
        try:
            track, device = self._simpler_at(track_index, device_index,
                                             chain_path)
            sample = self._simpler_sample(device)
            changed = {}

            if warping is not None:
                sample.warping = bool(warping)
                changed["warping"] = sample.warping

            if warp_mode is not None:
                index = self._resolve_simpler_enum(
                    sample, "warp_mode", warp_mode, SAMPLE_WARP_MODES,
                    "warp_mode")
                sample.warp_mode = index
                changed["warp_mode"] = self._simpler_enum_report(
                    sample, "warp_mode", SAMPLE_WARP_MODES)

            if start_frames is not None or start_beats is not None:
                sample.start_marker = self._sample_frames_at(
                    sample, start_frames, start_beats, "start")
                changed["start_marker"] = sample.start_marker

            if end_frames is not None or end_beats is not None:
                sample.end_marker = self._sample_frames_at(
                    sample, end_frames, end_beats, "end")
                changed["end_marker"] = sample.end_marker

            if gain is not None:
                sample.gain = float(gain)
                changed["gain"] = sample.gain
                try:
                    changed["gain_display"] = sample.gain_display_string()
                except Exception:
                    pass

            if action:
                # getattr(..., True) so a build that simply does not publish
                # the can_* flag still gets to try the call and let Live judge.
                if action == "reverse":
                    self._require_member(device, "reverse", "Simpler")
                    device.reverse()
                elif action == "crop":
                    self._require_member(device, "crop", "Simpler")
                    device.crop()
                elif action == "guess_playback_length":
                    self._require_member(device, "guess_playback_length",
                                         "Simpler")
                    device.guess_playback_length()
                elif action == "warp_half":
                    self._require_member(device, "warp_half", "Simpler")
                    if not getattr(device, "can_warp_half", True):
                        raise ValueError(
                            "'{0}' cannot halve its warp length right "
                            "now".format(device.name))
                    device.warp_half()
                elif action == "warp_double":
                    self._require_member(device, "warp_double", "Simpler")
                    if not getattr(device, "can_warp_double", True):
                        raise ValueError(
                            "'{0}' cannot double its warp length right "
                            "now".format(device.name))
                    device.warp_double()
                elif action == "warp_as":
                    self._require_member(device, "warp_as", "Simpler")
                    if warp_as_beats is None:
                        raise ValueError("warp_as needs warp_as_beats")
                    if not getattr(device, "can_warp_as", True):
                        raise ValueError(
                            "'{0}' cannot be warped to a fixed length right "
                            "now".format(device.name))
                    device.warp_as(float(warp_as_beats))
                else:
                    raise ValueError(
                        "Unknown sample action '{0}'. Use reverse, crop, "
                        "guess_playback_length, warp_as, warp_half or "
                        "warp_double.".format(action))
                changed["action"] = action

            result = {"track": track.name, "device": device.name,
                      "changed": changed}
            # The sample object is replaced by crop/reverse, so re-read.
            fresh = device.sample
            if fresh is not None:
                for attr in ("start_marker", "end_marker", "length", "warping",
                             "warp_mode"):
                    try:
                        result[attr] = getattr(fresh, attr)
                    except Exception:
                        pass
                name = self._simpler_enum_name(fresh, "warp_mode",
                                               SAMPLE_WARP_MODES)
                if name is not None:
                    result["warp_mode_name"] = name
                try:
                    result["gain_display"] = fresh.gain_display_string()
                except Exception:
                    pass
            return result
        except Exception as e:
            self.log_message("Error managing simpler sample: " + str(e))
            raise

    def _manage_simpler_slices(self, track_index, device_index, chain_path=None,
                               action="info", at_frames=None, at_beats=None,
                               to_frames=None, to_beats=None,
                               slicing_style=None, beat_division=None,
                               region_count=None, sensitivity=None):
        """Chop a sample into slices — the loop-to-playable-rack move.

        Slice positions are sample frames in Live's API; beats are converted
        through Sample.beat_to_sample_time. insert/remove/move address a slice
        by its POSITION, never by an ordinal, which is why the info action
        reports both units.
        """
        try:
            track, device = self._simpler_at(track_index, device_index,
                                             chain_path)
            sample = self._simpler_sample(device)
            changed = {}

            if slicing_style is not None:
                self._require_member(sample, "slicing_style", "Sample")
                index = self._resolve_simpler_enum(
                    sample, "slicing_style", slicing_style,
                    SAMPLE_SLICING_STYLES, "slicing_style")
                sample.slicing_style = index
                changed["slicing_style"] = self._simpler_enum_report(
                    sample, "slicing_style", SAMPLE_SLICING_STYLES)

            if beat_division is not None:
                self._require_member(sample, "slicing_beat_division", "Sample")
                sample.slicing_beat_division = int(beat_division)
                changed["slicing_beat_division"] = sample.slicing_beat_division

            if region_count is not None:
                self._require_member(sample, "slicing_region_count", "Sample")
                sample.slicing_region_count = int(region_count)
                changed["slicing_region_count"] = sample.slicing_region_count

            if sensitivity is not None:
                self._require_member(sample, "slicing_sensitivity", "Sample")
                sample.slicing_sensitivity = float(sensitivity)
                changed["slicing_sensitivity"] = sample.slicing_sensitivity

            if action == "add":
                where = self._sample_frames_at(sample, at_frames, at_beats,
                                               "slice position")
                sample.insert_slice(where)
                changed["added_at_frames"] = where
            elif action == "remove":
                where = self._sample_frames_at(sample, at_frames, at_beats,
                                               "slice position")
                sample.remove_slice(where)
                changed["removed_at_frames"] = where
            elif action == "move":
                old = self._sample_frames_at(sample, at_frames, at_beats,
                                             "slice position")
                new = self._sample_frames_at(sample, to_frames, to_beats,
                                             "new slice position")
                sample.move_slice(old, new)
                changed["moved"] = "{0} -> {1}".format(old, new)
            elif action == "clear":
                sample.clear_slices()
                changed["cleared"] = True
            elif action == "reset":
                sample.reset_slices()
                changed["reset"] = True
            elif action != "info":
                raise ValueError(
                    "Unknown slice action '{0}'. Use info, add, remove, move, "
                    "clear or reset.".format(action))

            result = {
                "track": track.name,
                "device": device.name,
                "changed": changed,
                "playback_mode": getattr(device, "playback_mode", None),
                "pad_slicing": getattr(device, "pad_slicing", None),
                "slicing_style": getattr(sample, "slicing_style", None),
                "slicing_beat_division": getattr(
                    sample, "slicing_beat_division", None),
                "slicing_region_count": getattr(
                    sample, "slicing_region_count", None),
                "slicing_sensitivity": getattr(
                    sample, "slicing_sensitivity", None),
            }
            name = self._simpler_enum_name(device, "playback_mode",
                                           SIMPLER_PLAYBACK_MODES)
            if name is not None:
                result["playback_mode_name"] = name
            name = self._simpler_enum_name(sample, "slicing_style",
                                           SAMPLE_SLICING_STYLES)
            if name is not None:
                result["slicing_style_name"] = name
            try:
                slices = [int(x) for x in sample.slices]
                result["slice_count"] = len(slices)
                result["slices_frames"] = slices[:128]
                result["slices_beats"] = [
                    round(sample.sample_to_beat_time(x), 4)
                    for x in slices[:128]]
            except Exception:
                result["slice_count"] = 0
            return result
        except Exception as e:
            self.log_message("Error managing simpler slices: " + str(e))
            raise

    # Take lanes

    def _describe_take_lane(self, lane, index):
        """Summarise one take lane for the client."""
        info = {"index": index}
        for attr in ("name", "is_included_in_playback", "has_clips"):
            try:
                info[attr] = self._describe_value(getattr(lane, attr))
            except Exception:
                pass
        try:
            info["clip_count"] = len(tuple(lane.clips))
        except Exception:
            pass
        return info

    def _take_lanes_of(self, track):
        """Return the track's take lanes, or raise if this Live can't do them."""
        if not hasattr(track, "take_lanes"):
            raise ValueError(
                "Track '{0}' has no take_lanes (needs Live 11.1 or newer)".format(
                    track.name))
        return tuple(track.take_lanes)

    def _manage_take_lanes(self, track_index, action="info", lane_index=None,
                           name=None, include_in_playback=None):
        """Inspect, create and comp take lanes on a track.

        Actions: info, create, rename, update, use_take.
        'use_take' includes exactly one lane in playback and excludes the
        rest, which is how you audition take 1 against take 3.
        """
        try:
            track = self._track_at(track_index)
            lanes = self._take_lanes_of(track)

            if action == "info":
                pass
            elif action == "create":
                if not hasattr(track, "create_take_lane"):
                    raise ValueError(
                        "Track '{0}' cannot create take lanes".format(track.name))
                track.create_take_lane()
                lanes = self._take_lanes_of(track)
                if name and lanes:
                    try:
                        lanes[-1].name = name
                    except (AttributeError, RuntimeError) as e:
                        self.log_message("TakeLane.name assignment failed: " + str(e))
            elif action in ("rename", "update", "use_take"):
                if lane_index is None:
                    raise ValueError("lane_index is required for '{0}'".format(action))
                if lane_index < 0 or lane_index >= len(lanes):
                    raise IndexError(
                        "Take lane index out of range (track has {0})".format(len(lanes)))
                lane = lanes[lane_index]
                if action == "use_take":
                    for i, other in enumerate(lanes):
                        if not hasattr(other, "is_included_in_playback"):
                            continue
                        try:
                            other.is_included_in_playback = (i == lane_index)
                        except (AttributeError, RuntimeError) as e:
                            self.log_message(
                                "TakeLane.is_included_in_playback failed: " + str(e))
                else:
                    if name is not None:
                        try:
                            lane.name = name
                        except (AttributeError, RuntimeError) as e:
                            self.log_message(
                                "TakeLane.name assignment failed: " + str(e))
                    if include_in_playback is not None:
                        if not hasattr(lane, "is_included_in_playback"):
                            self.log_message(
                                "TakeLane has no is_included_in_playback here")
                        else:
                            try:
                                lane.is_included_in_playback = bool(include_in_playback)
                            except (AttributeError, RuntimeError) as e:
                                self.log_message(
                                    "TakeLane.is_included_in_playback failed: " + str(e))
            else:
                raise ValueError(
                    "Unknown action '{0}'. Use info, create, rename, "
                    "update or use_take".format(action))

            lanes = self._take_lanes_of(track)
            return {
                "track_name": track.name,
                "action": action,
                "take_lanes": [self._describe_take_lane(l, i)
                               for i, l in enumerate(lanes)],
            }
        except Exception as e:
            self.log_message("Error managing take lanes: " + str(e))
            raise

    # Cue points (locators)

    def _resolve_cue_point(self, cue_index=None, name=None):
        """Find a cue point by 0-based index or by exact/lowercase name."""
        cues = tuple(self._song.cue_points)
        if not cues:
            raise ValueError("This Set has no cue points")
        if cue_index is not None:
            if cue_index < 0 or cue_index >= len(cues):
                raise IndexError(
                    "Cue point index out of range (Set has {0})".format(len(cues)))
            return cues[cue_index]
        if name:
            for cp in cues:
                if cp.name.lower() == str(name).lower():
                    return cp
            available = [cp.name for cp in cues]
            raise ValueError("Cue point '{0}' not found. Available: {1}".format(
                name, ", ".join(available)))
        raise ValueError("Provide cue_index or name")

    def _edit_cue_point(self, cue_index=None, name=None, new_name=None,
                        time=None, jump=False):
        """Rename, move or jump to an existing locator.

        CuePoint.time is read-only in most Live builds; when the move is
        refused it is reported back instead of failing the whole call.
        """
        try:
            cp = self._resolve_cue_point(cue_index, name)
            result = {"name": cp.name, "time": cp.time}
            if new_name is not None:
                try:
                    cp.name = new_name
                    result["name"] = cp.name
                except (AttributeError, RuntimeError) as e:
                    self.log_message("CuePoint.name assignment failed: " + str(e))
                    result["rename_failed"] = str(e)
            if time is not None:
                try:
                    cp.time = float(time)
                    result["time"] = cp.time
                except (AttributeError, RuntimeError, TypeError) as e:
                    self.log_message("CuePoint.time assignment failed: " + str(e))
                    result["move_failed"] = (
                        "CuePoint.time is read-only here — delete this locator "
                        "and create a new one at the target position")
            if jump:
                if not hasattr(cp, "jump"):
                    result["jump_failed"] = (
                        "CuePoint.jump is unavailable in this Live build")
                else:
                    cp.jump()
                    result["jumped_to"] = self._song.current_song_time
            return result
        except Exception as e:
            self.log_message("Error editing cue point: " + str(e))
            raise

    # Groove pool

    def _groove_base_options(self, groove):
        """Names accepted for Groove.base, the current value, and resolvability.

        Returns (options, current, resolvable). ``resolvable`` is False when
        this Live build reports ``base`` as a bare number with no accompanying
        name list — in that case we refuse to guess an integer ordering.
        """
        options = []
        current = None
        try:
            current_value = groove.base
        except Exception:
            return options, current, False

        # Preferred: the _index/_list pair, when the build exposes one.
        if hasattr(groove, "base_list") and hasattr(groove, "base_index"):
            try:
                options = [str(n) for n in groove.base_list]
                idx = int(groove.base_index)
                if 0 <= idx < len(options):
                    current = options[idx]
                return options, current, True
            except Exception as e:
                self.log_message(
                    "Groove.base_list/base_index read failed: " + str(e))
                options = []

        # Fallback: a real enum object exposes named members of its own type.
        # A plain int/float carries no names, so nothing is resolvable there.
        cls = type(current_value)
        if not isinstance(current_value, (int, float)):
            for member in dir(cls):
                if member.startswith("_"):
                    continue
                try:
                    value = getattr(cls, member)
                except Exception:
                    continue
                if isinstance(value, cls):
                    options.append(member)
                    if value == current_value:
                        current = member
        if current is None:
            current = str(current_value)
        return options, current, bool(options)

    def _set_groove_base(self, groove, base):
        """Set Groove.base from a display name. Never guesses an index."""
        if hasattr(groove, "base_list") and hasattr(groove, "base_index"):
            names = [str(n) for n in groove.base_list]
            for i, n in enumerate(names):
                if n.lower() == str(base).lower():
                    groove.base_index = i
                    return names[i]
            raise ValueError("Base '{0}' not found. Available: {1}".format(
                base, ", ".join(names) or "(none reported)"))

        options, _, resolvable = self._groove_base_options(groove)
        if not resolvable:
            raise ValueError(
                "This Live build reports Groove.base as an unnamed value, so it "
                "cannot be set by name and this tool will not guess an index — "
                "change the base in Live's Groove Pool instead")
        current = groove.base
        cls = type(current)
        wanted = str(base).lower().replace(" ", "_")
        for member in options:
            if member.lower() == wanted:
                groove.base = getattr(cls, member)
                return member
        raise ValueError("Base '{0}' not found. Available: {1}".format(
            base, ", ".join(options)))

    def _describe_groove(self, groove, index):
        """Full read-out of one groove: base, and the four feel amounts."""
        info = {"index": index}
        try:
            info["name"] = groove.name
        except Exception:
            pass
        options, current, resolvable = self._groove_base_options(groove)
        info["base"] = current
        info["base_options"] = options
        info["base_settable"] = resolvable
        for attr in ("quantization_amount", "timing_amount", "random_amount",
                     "velocity_amount"):
            try:
                info[attr] = self._describe_value(getattr(groove, attr))
            except Exception:
                pass
        return info

    def _is_integer_like(self, value):
        """True for int/long but not bool or str — py2 and py3 safe."""
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        return hasattr(value, "__index__") and not hasattr(value, "lower")

    def _manage_groove_pool(self, groove=None, base=None,
                            quantization_amount=None, timing_amount=None,
                            random_amount=None, velocity_amount=None,
                            global_amount=None, new_name=None):
        """Read the groove pool, and edit one groove's feel in place.

        With no ``groove`` this is a report. With ``groove`` (name or 0-based
        index) the omitted amounts are left alone. The groove is resolved
        before anything is written, so a bad name changes nothing at all.
        """
        try:
            try:
                grooves = list(self._song.groove_pool.grooves)
            except Exception:
                return {"supported": False, "grooves": [],
                        "groove_amount": getattr(self._song, "groove_amount", None)}

            target = None
            target_index = None
            if groove is not None:
                if not grooves:
                    raise ValueError(
                        "Groove pool is empty. Drag a groove in from the browser, "
                        "or extract one from a clip, first")
                if self._is_integer_like(groove):
                    idx = int(groove)
                    if idx < 0 or idx >= len(grooves):
                        raise IndexError(
                            "Groove index out of range (pool has {0})".format(
                                len(grooves)))
                    target, target_index = grooves[idx], idx
                else:
                    for i, g in enumerate(grooves):
                        if g.name.lower() == str(groove).lower():
                            target, target_index = g, i
                            break
                if target is None:
                    raise ValueError("Groove '{0}' not found. Available: {1}".format(
                        groove, ", ".join([g.name for g in grooves])))

            # Only mutate once every argument has been validated.
            if global_amount is not None:
                if not hasattr(self._song, "groove_amount"):
                    self.log_message("Song has no groove_amount in this Live build")
                else:
                    self._song.groove_amount = max(
                        0.0, min(1.0, float(global_amount)))

            if target is None:
                return {
                    "supported": True,
                    "groove_amount": getattr(self._song, "groove_amount", None),
                    "grooves": [self._describe_groove(g, i)
                                for i, g in enumerate(grooves)],
                }

            changed = {}
            if base is not None:
                changed["base"] = self._set_groove_base(target, base)
            for attr, value in (("quantization_amount", quantization_amount),
                                ("timing_amount", timing_amount),
                                ("random_amount", random_amount),
                                ("velocity_amount", velocity_amount)):
                if value is None:
                    continue
                if not hasattr(target, attr):
                    self.log_message("Groove has no " + attr + " in this Live build")
                    continue
                try:
                    setattr(target, attr, float(value))
                    changed[attr] = getattr(target, attr)
                except (AttributeError, RuntimeError, TypeError) as e:
                    self.log_message(
                        "Groove." + attr + " assignment failed: " + str(e))
            if new_name is not None:
                try:
                    target.name = new_name
                    changed["name"] = target.name
                except (AttributeError, RuntimeError) as e:
                    self.log_message("Groove.name assignment failed: " + str(e))

            return {
                "supported": True,
                "groove_amount": getattr(self._song, "groove_amount", None),
                "changed": changed,
                "groove": self._describe_groove(target, target_index),
            }
        except Exception as e:
            self.log_message("Error managing groove pool: " + str(e))
            raise

    # Application, views and modal dialogs

    def _get_application_info(self):
        """Live's version, its main views, and any modal dialog that is up."""
        try:
            app = self.application()
            info = {}
            parts = []
            for getter, key in (("get_major_version", "major"),
                                ("get_minor_version", "minor"),
                                ("get_bugfix_version", "bugfix")):
                try:
                    value = getattr(app, getter)()
                    info[key] = value
                    parts.append(str(value))
                except Exception:
                    pass
            if parts:
                info["version"] = ".".join(parts)

            views = []
            try:
                for view_name in tuple(app.view.available_main_views()):
                    entry = {"name": view_name}
                    try:
                        entry["visible"] = app.view.is_view_visible(view_name)
                    except Exception:
                        pass
                    views.append(entry)
            except Exception as e:
                self.log_message("available_main_views failed: " + str(e))
            info["main_views"] = views
            try:
                info["focused_document_view"] = app.view.focused_document_view
            except Exception:
                pass

            dialog = {}
            for attr in ("open_dialog_count", "current_dialog_message",
                         "current_dialog_button_count"):
                try:
                    dialog[attr] = self._describe_value(getattr(app, attr))
                except Exception:
                    pass
            dialog["open"] = bool(dialog.get("open_dialog_count") or 0)
            dialog["can_press"] = hasattr(app, "press_current_dialog_button")
            info["dialog"] = dialog
            return info
        except Exception as e:
            self.log_message("Error getting application info: " + str(e))
            raise

    def _press_dialog_button(self, button_index=0):
        """Dismiss Live's current modal dialog by pressing one of its buttons."""
        try:
            app = self.application()
            count = 0
            try:
                count = int(app.open_dialog_count)
            except Exception:
                pass
            if not count:
                return {"dialog_open": False, "pressed": None,
                        "message": "No dialog is open"}
            if not hasattr(app, "press_current_dialog_button"):
                raise ValueError(
                    "This Live build has no "
                    "Application.press_current_dialog_button — dismiss the "
                    "dialog by hand")
            message = ""
            try:
                message = app.current_dialog_message
            except Exception:
                pass
            app.press_current_dialog_button(int(button_index))
            remaining = 0
            try:
                remaining = int(app.open_dialog_count)
            except Exception:
                pass
            return {"dialog_open": bool(remaining), "pressed": int(button_index),
                    "dismissed_message": message, "open_dialog_count": remaining}
        except Exception as e:
            self.log_message("Error pressing dialog button: " + str(e))
            raise

    def _require_view_member(self, view, member):
        """Raise a readable error when this build's View lacks a method."""
        if not hasattr(view, member):
            raise ValueError(
                "This Live build's Application.View has no '{0}'".format(member))

    def _control_live_view(self, action, view_name=None, direction="down",
                           modifier=False):
        """Show, hide, focus, toggle, scroll or zoom any named Live view."""
        try:
            app = self.application()
            view = app.view
            NAV = {"up": 0, "down": 1, "left": 2, "right": 3}
            if action in ("show", "hide", "focus", "toggle", "scroll", "zoom"):
                if not view_name:
                    raise ValueError("view_name is required for '{0}'".format(action))
            else:
                raise ValueError(
                    "Unknown action '{0}'. Use show, hide, focus, toggle, "
                    "scroll or zoom".format(action))

            available = []
            try:
                available = list(view.available_main_views())
            except Exception:
                pass
            if available and view_name not in available:
                for candidate in available:
                    if candidate.lower() == str(view_name).lower():
                        view_name = candidate
                        break

            if action == "show":
                self._require_view_member(view, "show_view")
                view.show_view(view_name)
            elif action == "hide":
                self._require_view_member(view, "hide_view")
                view.hide_view(view_name)
            elif action == "focus":
                self._require_view_member(view, "focus_view")
                view.focus_view(view_name)
            elif action == "toggle":
                self._require_view_member(view, "is_view_visible")
                self._require_view_member(view, "show_view")
                self._require_view_member(view, "hide_view")
                if view.is_view_visible(view_name):
                    view.hide_view(view_name)
                else:
                    view.show_view(view_name)
            elif action == "scroll":
                if direction not in NAV:
                    raise ValueError("direction must be up, down, left or right")
                self._require_view_member(view, "scroll_view")
                view.scroll_view(NAV[direction], view_name, bool(modifier))
            elif action == "zoom":
                if direction not in NAV:
                    raise ValueError("direction must be up, down, left or right")
                self._require_view_member(view, "zoom_view")
                view.zoom_view(NAV[direction], view_name, bool(modifier))

            result = {"action": action, "view": view_name}
            try:
                result["visible"] = view.is_view_visible(view_name)
            except Exception:
                pass
            try:
                result["focused_document_view"] = view.focused_document_view
            except Exception:
                pass
            return result
        except Exception as e:
            self.log_message("Error controlling Live view: " + str(e))
            raise

    # Tuning systems

    def _describe_tuning_system(self, tuning):
        """Name a TuningSystem object (or report 12-TET when there is none)."""
        if tuning is None:
            return {"name": "12-TET (default)", "custom": False}
        info = {"custom": True}
        for attr in ("name", "id"):
            try:
                info[attr] = self._describe_value(getattr(tuning, attr))
            except Exception:
                pass
        if "name" not in info:
            info["name"] = self._describe_value(tuning)
        return info

    def _collect_tuning_items(self, node, out, depth=0):
        """Walk a browser folder collecting loadable tuning items."""
        if depth > 6 or len(out) >= 256:
            return
        try:
            children = list(node.children)
        except Exception:
            children = []
        for child in children:
            if len(out) >= 256:
                return
            try:
                if getattr(child, "is_loadable", False):
                    out.append(child)
                else:
                    self._collect_tuning_items(child, out, depth + 1)
            except Exception:
                continue

    def _available_tuning_items(self):
        """Tuning files exposed by Live 12's browser, if this build has them."""
        app = self.application()
        browser = getattr(app, "browser", None)
        if browser is None:
            return []
        root = getattr(browser, "tunings", None)
        if root is None:
            return []
        items = []
        self._collect_tuning_items(root, items)
        return items

    def _manage_tuning_system(self, action="info", name=None):
        """Report, load or clear the Set's tuning system.

        Live types Song.tuning_system as an object, so it cannot be assigned a
        string — a custom tuning is applied by loading it from the browser,
        and cleared by assigning None (back to 12-TET).
        """
        try:
            if not hasattr(self._song, "tuning_system"):
                return {"supported": False,
                        "message": "This Live build has no Song.tuning_system"}
            items = []
            try:
                items = self._available_tuning_items()
            except Exception as e:
                self.log_message("Listing browser tunings failed: " + str(e))

            if action == "info":
                pass
            elif action == "clear":
                self._song.tuning_system = None
            elif action == "load":
                if not name:
                    raise ValueError("name is required to load a tuning system")
                if not items:
                    raise ValueError(
                        "No tunings in the browser — add .ascl/.scl files to the "
                        "User Library first")
                target = None
                for item in items:
                    if item.name.lower() == str(name).lower():
                        target = item
                        break
                if target is None:
                    for item in items:
                        if str(name).lower() in item.name.lower():
                            target = item
                            break
                if target is None:
                    raise ValueError("Tuning '{0}' not found. Available: {1}".format(
                        name, ", ".join([i.name for i in items[:40]])))
                browser = getattr(self.application(), "browser", None)
                if browser is None or not hasattr(browser, "load_item"):
                    raise ValueError(
                        "This Live build's browser cannot load items")
                browser.load_item(target)
            else:
                raise ValueError(
                    "Unknown action '{0}'. Use info, load or clear".format(action))

            return {
                "supported": True,
                "action": action,
                "current": self._describe_tuning_system(self._song.tuning_system),
                "available": [i.name for i in items[:64]],
            }
        except Exception as e:
            self.log_message("Error managing tuning system: " + str(e))
            raise

    # Generic Live Object Model access

    def _resolve_lom_path(self, path):
        """Resolve a dotted LOM path like 'tracks.3.devices.0' from song.

        Numeric segments index into collections; everything else is an
        attribute. 'song' or an empty path returns the song itself.
        """
        obj = self._song
        parts = [p for p in str(path).split(".") if p and p != "song"]
        for part in parts:
            if part.isdigit() or (part.startswith("-") and part[1:].isdigit()):
                obj = obj[int(part)]
            else:
                obj = getattr(obj, part)
        return obj

    def _describe_value(self, value, depth=0):
        """Turn a LOM value into something JSON can carry."""
        if isinstance(value, (int, float, bool, str)) or value is None:
            return value
        if isinstance(value, (list, tuple)) or hasattr(value, "__len__"):
            try:
                items = list(value)
            except Exception:
                return "<{0}>".format(type(value).__name__)
            if depth >= 1:
                return "<{0} items>".format(len(items))
            return [self._describe_value(v, depth + 1) for v in items[:64]]
        for attr in ("name", "display_name"):
            try:
                return "<{0} '{1}'>".format(type(value).__name__,
                                            getattr(value, attr))
            except Exception:
                continue
        return "<{0}>".format(type(value).__name__)

    def _set_device_sidechain(self, track_index, device_index, source_track,
                              channel=None):
        """Route a device's sidechain input from another track, by name.

        Live's Compressor, Gate and Auto Filter expose input_routing_type,
        which IS the sidechain source. (Glue Compressor does not expose
        routing at all, so it cannot be sidechained through the API.)
        Assigning it requires a RoutingType object from the device's own
        available_input_routing_types, not a string.
        """
        try:
            track = self._track_at(track_index)
            device = track.devices[device_index]
            if not hasattr(device, "available_input_routing_types"):
                raise ValueError(
                    "'{0}' exposes no input routing, so it cannot be "
                    "sidechained. Live's Compressor, Gate and Auto Filter "
                    "can; Glue Compressor cannot.".format(device.name))

            options = list(device.available_input_routing_types)
            wanted = str(source_track).strip().lower()
            match = None
            for opt in options:
                if opt.display_name.strip().lower() == wanted:
                    match = opt
                    break
            if match is None:
                partial = [o for o in options
                           if wanted in o.display_name.strip().lower()]
                if len(partial) == 1:
                    match = partial[0]
                elif len(partial) > 1:
                    raise ValueError("'{0}' is ambiguous: {1}".format(
                        source_track,
                        ", ".join(o.display_name for o in partial)))
            if match is None:
                raise ValueError("'{0}' not found. Available: {1}".format(
                    source_track,
                    ", ".join(o.display_name for o in options)))

            device.input_routing_type = match

            channel_set = None
            if channel is not None:
                chans = list(getattr(device, "available_input_routing_channels", []))
                cwanted = str(channel).strip().lower()
                for c in chans:
                    if cwanted in c.display_name.strip().lower():
                        device.input_routing_channel = c
                        channel_set = c.display_name
                        break

            return {"track": track.name, "device": device.name,
                    "sidechain_source": device.input_routing_type.display_name,
                    "channel": channel_set}
        except Exception as e:
            self.log_message("Error setting sidechain: " + str(e))
            raise

    def _call_lom(self, path, member, args=None, set_value=None,
                  has_set_value=False, set_from=None):
        """Read a property, set a property, or call a method anywhere in the LOM.

        The escape hatch: anything Live exposes is reachable without shipping
        new code first. Undocumented C++ signatures surface as readable
        errors listing the expected argument types, which is how a signature
        gets discovered rather than guessed.
        """
        try:
            obj = self._resolve_lom_path(path)
            if not member:
                return {"path": path,
                        "type": type(obj).__name__,
                        "value": self._describe_value(obj)}

            if not hasattr(obj, member):
                available = sorted(a for a in dir(obj) if not a.startswith("_"))
                raise AttributeError(
                    "'{0}' has no member '{1}'. Available: {2}".format(
                        type(obj).__name__, member, ", ".join(available[:80])))

            attr = getattr(obj, member)

            if callable(attr):
                call_args = list(args or [])
                result = attr(*call_args)
                return {"path": path, "member": member, "called_with": call_args,
                        "returned": self._describe_value(result)}

            if set_from:
                # Many LOM properties must be assigned an object taken from a
                # sibling collection (routing types, routing channels,
                # grooves). A string will be rejected by the C++ layer, so
                # resolve the value out of that collection by display name.
                options = list(getattr(obj, set_from))
                wanted = str(set_value).strip().lower()

                def label(o):
                    for a in ("display_name", "name"):
                        try:
                            return str(getattr(o, a))
                        except Exception:
                            continue
                    return str(o)

                match = None
                for o in options:
                    if label(o).strip().lower() == wanted:
                        match = o
                        break
                if match is None:
                    partial = [o for o in options
                               if wanted in label(o).strip().lower()]
                    if len(partial) == 1:
                        match = partial[0]
                    elif len(partial) > 1:
                        raise ValueError("'{0}' is ambiguous in {1}: {2}".format(
                            set_value, set_from,
                            ", ".join(label(o) for o in partial)))
                if match is None:
                    raise ValueError("'{0}' not found in {1}. Options: {2}".format(
                        set_value, set_from,
                        ", ".join(label(o) for o in options)))
                setattr(obj, member, match)
                return {"path": path, "member": member,
                        "set_from": set_from, "resolved": label(match),
                        "now": self._describe_value(getattr(obj, member))}

            if has_set_value:
                current = attr
                new = set_value
                if isinstance(current, bool):
                    new = bool(new)
                elif isinstance(current, int) and not isinstance(current, bool):
                    new = int(new)
                elif isinstance(current, float):
                    new = float(new)
                setattr(obj, member, new)
                return {"path": path, "member": member,
                        "was": self._describe_value(current),
                        "now": self._describe_value(getattr(obj, member))}

            return {"path": path, "member": member,
                    "value": self._describe_value(attr),
                    "is_callable": False}
        except Exception as e:
            self.log_message("Error in call_lom: " + str(e))
            raise

    # Metering, modulation, racks, looper

    def _get_meters(self):
        """Read output level meters for every track.

        Not a substitute for listening, but it makes level relationships
        measurable: which track is loudest, whether anything is clipping,
        whether a part is actually audible in the mix.
        """
        try:
            readings = []
            tracks = list(self._song.tracks) + list(self._song.return_tracks)
            for i, t in enumerate(tracks):
                entry = {"index": i, "name": t.name}
                for attr in ("output_meter_level", "output_meter_left",
                             "output_meter_right", "input_meter_level"):
                    try:
                        entry[attr] = round(getattr(t, attr), 4)
                    except Exception:
                        pass
                readings.append(entry)
            master = {"name": "Master"}
            for attr in ("output_meter_level", "output_meter_left",
                         "output_meter_right"):
                try:
                    master[attr] = round(
                        getattr(self._song.master_track, attr), 4)
                except Exception:
                    pass
            return {"is_playing": self._song.is_playing,
                    "tracks": readings, "master": master}
        except Exception as e:
            self.log_message("Error reading meters: " + str(e))
            raise

    def _get_modulation_targets(self, track_index, device_index):
        """List modulation sources and targets on a device that supports them.

        Wavetable exposes its modulation matrix through the API, so
        envelope-to-filter and LFO-to-pitch routings can be made
        programmatically rather than dragged by hand.
        """
        try:
            track = self._track_at(track_index)
            device = track.devices[device_index]
            info = {"device": device.name, "supports_modulation": False}

            if not hasattr(device, "visible_modulation_target_names"):
                info["note"] = (
                    "This device does not expose a modulation matrix. "
                    "Wavetable does; most others do not.")
                return info

            info["supports_modulation"] = True
            try:
                info["targets"] = list(device.visible_modulation_target_names)
            except Exception:
                info["targets"] = []

            sources = {}
            for attr in dir(device):
                if attr.endswith("_source_list") or attr.endswith("_list"):
                    if "mod_matrix" not in attr and "modulation" not in attr:
                        continue
                    try:
                        sources[attr] = list(getattr(device, attr))
                    except Exception:
                        pass
            info["source_lists"] = sources

            modulatable = []
            if hasattr(device, "is_parameter_modulatable"):
                for p in tuple(device.parameters):
                    try:
                        if device.is_parameter_modulatable(p):
                            modulatable.append(p.name)
                    except Exception:
                        pass
            info["modulatable_parameters"] = modulatable
            return info
        except Exception as e:
            self.log_message("Error reading modulation targets: " + str(e))
            raise

    def _set_device_modulation(self, track_index, device_index, target,
                               source, value):
        """Route a modulation source to a parameter and set its depth.

        target is a parameter name on the device (e.g. "Filter 1 Freq"),
        source is the modulation source name as reported by
        get_modulation_targets, and value is the depth, -1.0 to 1.0.
        """
        try:
            track = self._track_at(track_index)
            device = track.devices[device_index]

            if not hasattr(device, "set_modulation_value"):
                raise ValueError(
                    "Device '{0}' does not expose a modulation matrix".format(
                        device.name))

            param = None
            target_lower = str(target).strip().lower()
            for p in tuple(device.parameters):
                if p.name.strip().lower() == target_lower:
                    param = p
                    break
            if param is None:
                for p in tuple(device.parameters):
                    if target_lower in p.name.strip().lower():
                        param = p
                        break
            if param is None:
                raise ValueError("Parameter '{0}' not found on '{1}'".format(
                    target, device.name))

            if hasattr(device, "is_parameter_modulatable"):
                if not device.is_parameter_modulatable(param):
                    raise ValueError(
                        "'{0}' cannot be modulated".format(param.name))

            # The matrix is addressed by integer indices, not parameter
            # objects: set_modulation_value(target_index, source_index, value).
            # A parameter must be a visible target before it can be modulated.
            def target_names():
                try:
                    return [str(n) for n in device.visible_modulation_target_names]
                except Exception:
                    return []

            names = target_names()
            if param.name not in names:
                if hasattr(device, "add_parameter_to_modulation_matrix"):
                    device.add_parameter_to_modulation_matrix(param)
                    names = target_names()
            if param.name not in names:
                raise ValueError(
                    "Could not add '{0}' as a modulation target. Visible "
                    "targets: {1}".format(param.name, ", ".join(names)))
            target_index = names.index(param.name)

            try:
                source_index = int(source)
            except (TypeError, ValueError):
                raise ValueError(
                    "source must be an integer index into the device's "
                    "modulation sources (Wavetable: 0 = Env 2, 1 = Env 3, "
                    "2 = LFO 1, 3 = LFO 2). Got '{0}'.".format(source))

            depth = max(-1.0, min(1.0, float(value)))
            device.set_modulation_value(target_index, source_index, depth)

            applied = None
            try:
                applied = device.get_modulation_value(target_index, source_index)
            except Exception:
                pass

            return {"device": device.name, "parameter": param.name,
                    "target_index": target_index, "source_index": source_index,
                    "depth": depth, "readback": applied,
                    "visible_targets": names}
        except Exception as e:
            self.log_message("Error setting modulation: " + str(e))
            raise

    def _move_device(self, track_index, device_index, target_track_index=None,
                     position=0):
        """Move a device to a new position, on the same track or another.

        Device order is signal order, so this is how a chain gets corrected
        without deleting and reloading — e.g. moving an EQ in front of a
        compressor so low rumble stops triggering gain reduction.
        """
        try:
            track = self._track_at(track_index)
            devices = tuple(track.devices)
            if device_index < 0 or device_index >= len(devices):
                raise IndexError("Device index {0} out of range on '{1}'".format(
                    device_index, track.name))
            device = devices[device_index]
            target_track = (self._track_at(target_track_index)
                            if target_track_index is not None else track)
            self._song.move_device(device, target_track, int(position))
            return {"device": device.name,
                    "from_track": track.name,
                    "to_track": target_track.name,
                    "position": int(position),
                    "chain": [d.name for d in target_track.devices]}
        except Exception as e:
            self.log_message("Error moving device: " + str(e))
            raise



    def _set_song_scale(self, root_note=None, scale_name=None,
                        swing_amount=None, clip_trigger_quantization=None):
        """Set the Set's key/scale, global swing, and clip launch quantization.

        Live 12 tracks a Set-wide root note and scale that scale-aware
        devices and the MIDI editor follow.
        """
        try:
            changed = {}
            if root_note is not None:
                self._song.root_note = int(root_note)
                changed["root_note"] = self._song.root_note
            if scale_name is not None:
                self._song.scale_name = str(scale_name)
                changed["scale_name"] = self._song.scale_name
            if swing_amount is not None:
                self._song.swing_amount = max(0.0, min(1.0, float(swing_amount)))
                changed["swing_amount"] = self._song.swing_amount
            if clip_trigger_quantization is not None:
                self._song.clip_trigger_quantization = int(
                    clip_trigger_quantization)
                changed["clip_trigger_quantization"] = \
                    self._song.clip_trigger_quantization
            try:
                changed["scale_intervals"] = list(self._song.scale_intervals)
            except Exception:
                pass
            return {"changed": changed}
        except Exception as e:
            self.log_message("Error setting song scale: " + str(e))
            raise

    def _add_notes_extended(self, track_index, clip_index, notes, replace=False,
                            arrangement=False, clip_name=None):
        """Add MIDI notes with per-note probability and velocity deviation.

        The older set_notes API cannot express probability, which is what
        makes programmed patterns breathe — hats that land 80% of the time
        rather than every single loop.

        `add_new_notes` is additive: existing notes survive. Pass
        replace=True to clear the clip first.
        """
        try:
            import Live
            track, clip = self._resolve_clip(
                track_index, clip_index, arrangement, clip_name)

            if replace:
                try:
                    clip.remove_notes_extended(0, 128, 0.0, clip.length)
                except Exception:
                    pass

            specs = []
            for n in notes:
                spec = Live.Clip.MidiNoteSpecification(
                    pitch=int(n.get("pitch", 60)),
                    start_time=float(n.get("start_time", 0.0)),
                    duration=float(n.get("duration", 0.25)),
                    velocity=float(n.get("velocity", 100)),
                    mute=bool(n.get("mute", False)))
                for extra in ("probability", "velocity_deviation",
                              "release_velocity"):
                    if extra in n:
                        try:
                            setattr(spec, extra, float(n[extra]))
                        except Exception:
                            pass
                specs.append(spec)

            clip.add_new_notes(tuple(specs))
            return {"clip_name": clip.name, "notes_added": len(specs),
                    "replaced": bool(replace),
                    "arrangement": bool(arrangement)}
        except Exception as e:
            self.log_message("Error adding extended notes: " + str(e))
            raise

    # Introspection

    def _inspect_lom(self, target="song", track_index=None, clip_index=None,
                     device_index=None, scene_index=None, name_filter=""):
        """Report the real API surface of a live object in this Live version.

        The Live Object Model differs between versions and published
        documentation is incomplete, so the running instance is the only
        authoritative source. This resolves an object and reports its
        properties (with current values) and its methods.
        """
        try:
            obj = None
            label = target

            if target == "song":
                obj = self._song
            elif target == "track":
                obj = self._track_at(track_index or 0)
                label = "track '{0}'".format(obj.name)
            elif target == "clip":
                track = self._track_at(track_index or 0)
                slot = track.clip_slots[clip_index or 0]
                if not slot.has_clip:
                    raise ValueError("No clip at that slot")
                obj = slot.clip
                label = "clip '{0}'".format(obj.name)
            elif target == "clip_slot":
                track = self._track_at(track_index or 0)
                obj = track.clip_slots[clip_index or 0]
                label = "clip_slot"
            elif target == "device":
                track = self._track_at(track_index or 0)
                obj = track.devices[device_index or 0]
                label = "device '{0}'".format(obj.name)
            elif target == "scene":
                obj = self._song.scenes[scene_index or 0]
                label = "scene"
            elif target == "mixer":
                obj = self._track_at(track_index or 0).mixer_device
                label = "mixer_device"
            elif target == "master":
                obj = self._song.master_track
                label = "master_track"
            elif target == "view":
                obj = self._song.view
                label = "song.view"
            else:
                raise ValueError(
                    "Unknown target '{0}'. Use song, track, clip, clip_slot, "
                    "device, scene, mixer, master or view.".format(target))

            props = []
            methods = []
            for attr in sorted(dir(obj)):
                if attr.startswith("_"):
                    continue
                if name_filter and name_filter.lower() not in attr.lower():
                    continue
                try:
                    value = getattr(obj, attr)
                except Exception as e:
                    props.append({"name": attr, "value": "<error: {0}>".format(e)})
                    continue
                if callable(value):
                    methods.append(attr)
                    continue
                shown = value
                try:
                    if isinstance(value, (int, float, bool, str)) or value is None:
                        shown = value
                    elif hasattr(value, "__len__"):
                        shown = "<{0} items>".format(len(value))
                    else:
                        shown = "<{0}>".format(type(value).__name__)
                except Exception:
                    shown = "<unreadable>"
                props.append({"name": attr, "value": shown})

            return {"target": label, "properties": props, "methods": methods}
        except Exception as e:
            self.log_message("Error inspecting LOM: " + str(e))
            raise

    # Automation

    def _resolve_parameter(self, track, parameter_name, device_index=None):
        """Find an automatable parameter on a track, optionally within a device."""
        if not parameter_name:
            raise ValueError("parameter_name is required")
        target = parameter_name.strip().lower()
        mixer = track.mixer_device

        if device_index is None:
            aliases = {
                "volume": mixer.volume, "track volume": mixer.volume,
                "pan": mixer.panning, "panning": mixer.panning,
            }
            if target in aliases:
                return aliases[target], "mixer"
            for send in tuple(mixer.sends):
                if send.name.strip().lower() == target:
                    return send, "mixer send"

        devices = tuple(track.devices)
        if device_index is not None:
            if device_index < 0 or device_index >= len(devices):
                raise IndexError("Device index {0} out of range on '{1}'".format(
                    device_index, track.name))
            devices = (devices[device_index],)

        exact = []
        partial = []
        for device in devices:
            for p in tuple(device.parameters):
                # Live abbreviates p.name ("Flt 1 Freq") while original_name
                # keeps the UI spelling ("Filter 1 Freq"). Match either.
                candidates = [p.name.strip().lower()]
                try:
                    if p.original_name:
                        candidates.append(p.original_name.strip().lower())
                except Exception:
                    pass
                if target in candidates:
                    exact.append((p, device.name))
                elif any(target in c for c in candidates):
                    partial.append((p, device.name))
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise ValueError("'{0}' matches parameters on: {1}".format(
                parameter_name, ", ".join(d for _, d in exact)))
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            raise ValueError("'{0}' is ambiguous: {1}".format(
                parameter_name,
                ", ".join("{0} ({1})".format(p.name, d) for p, d in partial[:8])))
        raise ValueError("Parameter '{0}' not found on '{1}'".format(
            parameter_name, track.name))

    def _get_clip_automation(self, track_index, clip_index):
        """Report which parameters already have envelopes on a clip."""
        try:
            track = self._track_at(track_index)
            slot = track.clip_slots[clip_index]
            if not slot.has_clip:
                raise ValueError("No clip at slot {0}".format(clip_index))
            clip = slot.clip
            found = []
            candidates = [(track.mixer_device.volume, "mixer", "Volume"),
                          (track.mixer_device.panning, "mixer", "Pan")]
            for send in tuple(track.mixer_device.sends):
                candidates.append((send, "mixer", send.name))
            for device in tuple(track.devices):
                for p in tuple(device.parameters):
                    candidates.append((p, device.name, p.name))
            for param, owner, pname in candidates:
                try:
                    env = clip.automation_envelope(param)
                except Exception:
                    env = None
                if env is not None:
                    found.append({"device": owner, "parameter": pname})
            return {"clip_name": clip.name,
                    "has_envelopes": getattr(clip, "has_envelopes", None),
                    "automated": found}
        except Exception as e:
            self.log_message("Error reading clip automation: " + str(e))
            raise

    # --- Many commands over one connection --------------------------------

    def _batch(self, commands, stop_on_error=True):
        """Run a list of commands in order over a single connection.

        Every command is otherwise a full round trip: a socket exchange, and
        above that a separate tool call with model latency attached. Building
        one arrangement took roughly 260 of them, and that latency — not
        Live — is what made large edits impractical.

        Each entry is replayed through the normal `_process_command` path, so
        threading, validation and error handling are identical to sending it
        on its own. Only the round trips disappear. That is deliberate: a
        second dispatch path would be a second place for the three-way
        registration to rot.
        """
        if not isinstance(commands, (list, tuple)):
            raise ValueError("'commands' must be a list")

        results = []
        succeeded = 0
        failed = 0
        for index, entry in enumerate(commands):
            if not isinstance(entry, dict):
                raise ValueError(
                    "command {0} is not an object".format(index))
            name = entry.get("command") or entry.get("type")
            if not name:
                raise ValueError(
                    "command {0} has no 'command' name".format(index))
            if name == "batch":
                # Nesting would make a failure index meaningless and invites
                # unbounded recursion from a single malformed payload.
                raise ValueError("batch cannot contain another batch")

            record = {"index": index, "command": name}
            try:
                reply = self._process_command(
                    {"type": name, "params": entry.get("params") or {}})
            except Exception as exc:
                reply = {"status": "error", "message": str(exc)}

            if reply.get("status") == "error":
                failed += 1
                record["status"] = "error"
                record["message"] = reply.get("message", "unknown error")
                results.append(record)
                if stop_on_error:
                    return {"ran": index + 1, "total": len(commands),
                            "succeeded": succeeded, "failed": failed,
                            "stopped_early": True, "results": results}
            else:
                succeeded += 1
                record["status"] = "success"
                record["result"] = reply.get("result")
                results.append(record)

        return {"ran": len(commands), "total": len(commands),
                "succeeded": succeeded, "failed": failed,
                "stopped_early": False, "results": results}

    # --- Build identity --------------------------------------------------

    def _get_build_info(self):
        """Report which build of this script Live actually has loaded.

        Live reads a remote script once, at startup, so an edited repo and a
        running Live routinely disagree for hours. Every "the Live API cannot
        do that" that later turned out to be false was traced to asking a
        stale copy of this file, not to a real limit.
        """
        info = {"remote_script_build": BUILD_ID, "script_file": __file__}
        # Which Set is open. Live swaps documents without telling anyone, and
        # an afternoon went into "the arrangement is empty" that was really
        # "you are looking at the template". song.file_path answers it in one
        # line, so it belongs in the same place as the build check.
        for attr in ("name", "file_path"):
            try:
                info["set_" + attr] = getattr(self._song, attr)
            except Exception:
                pass
        try:
            app = self.application()
            parts = []
            for getter in ("get_major_version", "get_minor_version",
                           "get_bugfix_version"):
                try:
                    parts.append(str(getattr(app, getter)()))
                except Exception:
                    pass
            if parts:
                info["live_version"] = ".".join(parts)
        except Exception:
            pass
        return info

    # --- Arrangement automation, recorded off the transport ---------------
    #
    # Live refuses create_automation_envelope on an arrangement clip, and
    # duplicating a session clip into the arrangement strips its envelopes.
    # Both are true, and together they were read as "arrangement automation
    # is impossible". They are not the same claim: those are limits on CLIP
    # envelopes, and arrangement automation is TRACK automation.
    #
    # Live writes track automation the same way it does for a hardware fader
    # — arm arrangement record, roll the transport, move the parameter. Doing
    # exactly that from here produces automation Live owns and replays:
    # verified by DeviceParameter.automation_state going 0 -> 1 and the
    # parameter then reproducing the recorded shape untouched.

    def _auto_rec_state_default(self):
        return {"active": False, "status": "idle", "track": None,
                "parameter": None, "device": None, "from_beat": None,
                "to_beat": None, "position": None, "samples": 0,
                "ticks": 0, "rolling": False, "waiting": 0, "stalled": 0}

    @staticmethod
    def _interpolate_points(pts, t):
        """Linear value at beat t across sorted (time, value) pairs."""
        if t <= pts[0][0]:
            return pts[0][1]
        if t >= pts[-1][0]:
            return pts[-1][1]
        for i in range(1, len(pts)):
            t0, v0 = pts[i - 1]
            t1, v1 = pts[i]
            if t <= t1:
                if t1 <= t0:
                    return v1
                return v0 + (v1 - v0) * ((t - t0) / (t1 - t0))
        return pts[-1][1]

    def _finish_auto_rec(self, status="done"):
        """Stop the pass and put the transport back how it was found."""
        state = getattr(self, "_auto_rec", None)
        if not state:
            self._auto_rec = self._auto_rec_state_default()
            return self._auto_rec
        song = self._song
        restore = state.get("restore") or {}
        try:
            song.stop_playing()
        except Exception:
            pass
        try:
            song.record_mode = False
        except Exception:
            pass
        try:
            if "loop" in restore:
                song.loop = restore["loop"]
        except Exception:
            pass
        if restore.get("return_to_start", True):
            try:
                song.start_time = restore.get("start_time", 0.0)
            except Exception:
                pass
        # Arming a track makes Live's exclusive arm disarm the others, and
        # nothing reports it — so the whole arm map is captured and put back,
        # disarming everything first so exclusive arm cannot fight the restore.
        arm_map = restore.get("arm_map")
        if arm_map:
            tracks = tuple(song.tracks)
            for index, armed in arm_map:
                if index < len(tracks):
                    try:
                        tracks[index].arm = False
                    except Exception:
                        pass
            for index, armed in arm_map:
                if armed and index < len(tracks):
                    try:
                        tracks[index].arm = True
                    except Exception:
                        pass
        state["active"] = False
        state["status"] = status
        return state

    def _get_automation_record_status(self):
        state = getattr(self, "_auto_rec", None)
        if not state:
            return self._auto_rec_state_default()
        out = dict((k, v) for k, v in state.items() if k != "restore")
        if state.get("active"):
            try:
                out["position"] = self._song.current_song_time
            except Exception:
                pass
        # A finished bounce is only useful if the caller can find the file.
        # Resolved lazily rather than at stop time: Live finalises the
        # recording a moment after the transport stops, so reading it in
        # _finish_auto_rec would race and often come back empty.
        index = state.get("bounce_track_index")
        if index is not None and not state.get("active"):
            try:
                clips = tuple(self._song.tracks[index].arrangement_clips)
                if clips:
                    out["file_path"] = clips[-1].file_path
            except Exception:
                pass
        return out

    def _cancel_automation_record(self):
        state = getattr(self, "_auto_rec", None)
        if not state or not state.get("active"):
            return {"cancelled": False, "reason": "no recording in progress"}
        result = self._finish_auto_rec("cancelled")
        return {"cancelled": True,
                "track": result.get("track"),
                "parameter": result.get("parameter"),
                "stopped_at_beat": result.get("position")}

    def _record_arrangement_automation(self, track_index, parameter_name,
                                       points, device_index=None,
                                       return_to_start=True):
        """Write real arrangement automation by recording it in real time.

        points: [{"time": <absolute beat>, "value": 0.0-1.0}, ...] — at least
        two, because recording captures movement and a single value has no
        movement to capture. Values are interpolated against the TRUE
        playhead on every tick, so ramps come out smooth and a slow tick
        cannot make the shape drift out of time.

        Returns immediately: the pass runs on Live's own tick via
        schedule_message, so the UI thread is never blocked and the socket
        does not sit past its timeout. Poll get_automation_record_status.

        This overwrites existing automation for that parameter across the
        recorded range, exactly as a real record pass would.
        """
        state = getattr(self, "_auto_rec", None)
        if state and state.get("active"):
            raise RuntimeError(
                "Already recording automation for '{0}' on '{1}'. Wait for it "
                "or call cancel_automation_record.".format(
                    state.get("parameter"), state.get("track")))

        track = self._track_at(track_index)
        param, owner = self._resolve_parameter(
            track, parameter_name, device_index)

        pts = []
        for point in points:
            pts.append((float(point.get("time", 0.0)),
                        max(0.0, min(1.0, float(point.get("value", 0.0))))))
        if len(pts) < 2:
            raise ValueError(
                "Need at least 2 points — automation recording captures "
                "movement over time, so one value has nothing to record. "
                "For a static value just set the parameter.")
        pts.sort(key=lambda pair: pair[0])

        song = self._song
        start_beat, end_beat = pts[0][0], pts[-1][0]
        if end_beat <= start_beat:
            raise ValueError("All points share the same time; nothing to record")

        pmin, pmax = param.min, param.max
        restore = {"start_time": song.start_time,
                   "loop": song.loop,
                   "return_to_start": bool(return_to_start)}

        # Arrangement record captures ARMED TRACKS as well as parameter moves,
        # and recording across a range REPLACES whatever is arranged there.
        # An automation pass needs no armed track at all, so disarm everything
        # for the duration. Without this, writing a filter sweep over bars
        # 33-49 would silently punch out the clips on whichever track happened
        # to be armed — destroying arranged material as a side effect of
        # writing automation to an unrelated track.
        arm_map = []
        for index, other in enumerate(tuple(song.tracks)):
            try:
                arm_map.append((index, bool(other.arm)))
                if other.arm:
                    other.arm = False
            except Exception:
                pass
        restore["arm_map"] = arm_map

        # A loop would send the playhead back and re-record over the pass.
        song.loop = False
        song.start_time = start_beat
        song.record_mode = True
        song.start_playing()

        self._auto_rec = {
            "active": True, "status": "recording",
            "track": track.name, "parameter": param.name, "device": owner,
            "from_beat": start_beat, "to_beat": end_beat,
            "position": start_beat, "samples": 0, "ticks": 0,
            "rolling": False, "waiting": 0, "stalled": 0,
            "restore": restore,
        }

        # Ticks are ~100 ms. If the playhead somehow never reaches end_beat —
        # a loop brace switched on by hand mid-pass would do it — the chain
        # would reschedule itself forever, holding Live in record. Cap it at
        # a generous multiple of the expected duration.
        try:
            expected_ticks = (end_beat - start_beat) * 60.0 / song.tempo * 10.0
        except Exception:
            expected_ticks = 600.0
        max_ticks = int(expected_ticks * 3) + 100

        def step():
            current = getattr(self, "_auto_rec", None)
            if not current or not current.get("active"):
                return
            try:
                now = song.current_song_time
                previous = current["position"]
                current["position"] = now

                # Liveness is judged on the PLAYHEAD, never on is_playing.
                # is_playing keeps reading False for several ticks after
                # start_playing() while the change settles — not one tick, as
                # an earlier version assumed. That version read "not playing",
                # concluded the user had stopped, and ended the pass after two
                # ticks having written nothing, while reporting "done" with an
                # empty envelope. A silent no-op that looks like success is
                # the worst possible failure here, so the transport's own
                # position is the only signal trusted.
                if now > start_beat + 1e-6:
                    current["rolling"] = True
                if not current["rolling"]:
                    # A count-in delays the roll by up to 4 bars and Live
                    # reports it, so don't spend the patience budget waiting
                    # for something that is working as configured. This Set
                    # has count_in_duration set, which would otherwise have
                    # aborted a pass as "never_started" before the count
                    # finished.
                    counting_in = False
                    try:
                        counting_in = bool(song.is_counting_in)
                    except Exception:
                        pass
                    if not counting_in:
                        current["waiting"] += 1
                    if current["waiting"] > 40:          # ~4 s of ticks
                        self.log_message(
                            "automation record: transport never rolled")
                        self._finish_auto_rec("never_started")
                        return
                    self.schedule_message(1, step)
                    return

                # Once rolling, a playhead that stops advancing IS the stop.
                if now <= previous:
                    current["stalled"] += 1
                else:
                    current["stalled"] = 0
                stopped = current["stalled"] >= 3
                if now >= end_beat or stopped:
                    # Land exactly on the target. Without this the envelope
                    # ends at whatever the last tick interpolated, up to one
                    # tick short of the value that was asked for.
                    if not stopped:
                        try:
                            param.value = pmin + (pmax - pmin) * pts[-1][1]
                        except Exception:
                            pass
                    self._finish_auto_rec("done")
                    return
                current["ticks"] += 1
                if current["ticks"] > max_ticks:
                    self.log_message(
                        "automation record ran past its tick budget; stopping")
                    self._finish_auto_rec("timeout")
                    return
                param.value = pmin + (pmax - pmin) * self._interpolate_points(
                    pts, now)
                current["samples"] += 1
            except Exception as exc:
                self.log_message(
                    "automation record step failed: " + str(exc))
                self._finish_auto_rec("failed")
                return
            self.schedule_message(1, step)

        self.schedule_message(1, step)

        beats = end_beat - start_beat
        try:
            seconds = beats * 60.0 / song.tempo
        except Exception:
            seconds = None
        return {"started": True, "track": track.name,
                "parameter": param.name, "device": owner,
                "from_beat": start_beat, "to_beat": end_beat,
                "beats": beats, "estimated_seconds": seconds,
                "note": "Recording in real time. Poll "
                        "get_automation_record_status until status is 'done'."}

    def _insert_device(self, track_index, device_name, position=None):
        """Insert a Live device at a position, instead of append-then-move.

        Signature discovered from the C++ error rather than guessed:
        `insert_device(TString DeviceName, int DeviceIndex=-1)`. Note it
        takes a DEVICE NAME, not a browser URI — unlike load_browser_item.
        """
        track = self._track_at(track_index)
        before = [d.name for d in tuple(track.devices)]
        index = -1 if position is None else int(position)
        track.insert_device(str(device_name), index)
        after = [d.name for d in tuple(track.devices)]
        if len(after) == len(before):
            raise ValueError(
                "Live did not insert '{0}'. insert_device takes a device "
                "NAME as Live spells it (e.g. 'Reverb', 'EQ Eight'), not a "
                "browser URI — load_instrument_or_effect takes the URI. "
                "Chain is unchanged: {1}".format(device_name, after))
        return {"track": track.name, "inserted": device_name,
                "position": index, "chain": after}

    def _get_performance_report(self):
        """Per-track CPU load, so a heavy Set can be diagnosed not guessed.

        `Track.performance_impact` reads 0 while stopped — roll the transport
        first or every track looks free.
        """
        song = self._song
        rows = []
        for i, track in enumerate(tuple(song.tracks) + tuple(song.return_tracks)):
            try:
                rows.append({"index": i, "name": track.name,
                             "impact": round(track.performance_impact, 5),
                             "devices": len(track.devices),
                             "frozen": bool(getattr(track, "is_frozen", False))})
            except Exception:
                pass
        rows.sort(key=lambda r: r["impact"], reverse=True)
        return {"is_playing": song.is_playing, "tracks": rows,
                "total": round(sum(r["impact"] for r in rows), 5)}

    def _manage_song_data(self, action, key, value=None, track_index=None):
        """Read or write arbitrary data stored INSIDE the Live Set.

        Song and Track both expose get_data/set_data, which persist in the
        .als. That means notes, section plans or "what has already been
        bounced" travel with the project and survive restarts, instead of
        living in a sidecar file that goes stale the moment the Set is
        moved or renamed.

        Signature from the C++ error: get_data(key, default_value) — the
        default is REQUIRED, not optional.
        """
        target = (self._track_at(track_index)
                  if track_index is not None else self._song)
        where = getattr(target, "name", "song") if track_index is not None else "song"
        if action == "set":
            target.set_data(str(key), value)
            return {"action": "set", "scope": where, "key": key, "value": value}
        if action == "get":
            got = target.get_data(str(key), None)
            return {"action": "get", "scope": where, "key": key, "value": got}
        raise ValueError("action must be 'get' or 'set'")

    def _set_song_options(self, **options):
        """Global behaviour flags that were never wrapped.

        `exclusive_arm` is the one that matters: it is the mechanism behind
        arming a track silently disarming whatever was armed before, which
        has bitten this integration more than once.
        """
        song = self._song
        changed = {}
        for name in ("exclusive_arm", "exclusive_solo", "select_on_launch",
                     "tempo_follower_enabled", "is_ableton_link_enabled"):
            val = options.get(name)
            if val is not None:
                try:
                    setattr(song, name, bool(val))
                    changed[name] = {"requested": bool(val),
                                     "observed": getattr(song, name)}
                except Exception as exc:
                    changed[name] = {"error": str(exc)}
        count_in = options.get("count_in_duration")
        if count_in is not None:
            try:
                song.count_in_duration = int(count_in)
                changed["count_in_duration"] = song.count_in_duration
            except Exception as exc:
                changed["count_in_duration"] = {"error": str(exc)}
        current = {}
        for name in ("exclusive_arm", "exclusive_solo", "select_on_launch",
                     "tempo_follower_enabled", "is_ableton_link_enabled",
                     "count_in_duration"):
            try:
                current[name] = getattr(song, name)
            except Exception:
                pass
        return {"changed": changed, "current": current}

    def _delete_return_track(self, return_index):
        """Delete a return track by its position among the returns.

        The API could create return tracks but never remove them, purely
        because `delete_return_track` was never wrapped — it has been on
        Song the whole time.
        """
        song = self._song
        returns = tuple(song.return_tracks)
        if return_index < 0 or return_index >= len(returns):
            raise IndexError(
                "Return index {0} out of range (0-{1})".format(
                    return_index, len(returns) - 1))
        name = returns[return_index].name
        song.delete_return_track(return_index)
        return {"deleted": name, "remaining": len(song.return_tracks)}

    def _bounce_to_audio(self, from_beat, to_beat, source="Resampling",
                         name=None):
        """Render a range to an audio file, by resampling it in real time.

        Live exposes no render/export call, which was written up here as
        "rendering audio: not in the API". True of *export*, false of the
        goal: an audio track accepts `Resampling` (the main bus) or any
        individual track as its INPUT, so arming it and rolling the transport
        captures a real audio file on disk. Verified: bars 33-35 produced a
        843 KB 48 kHz stereo AIFF peaking at -8.5 dBFS.

        source="Resampling" bounces the full mix; source="<track name>"
        bounces that track alone, which is stem export, and doubles as a
        stand-in for freeze/flatten (resample the track, then disable the
        original).

        Real time: bouncing 32 bars takes 32 bars. Monitoring is forced Off,
        because monitoring a resampling track feeds the main bus back into
        itself.
        """
        state = getattr(self, "_auto_rec", None)
        if state and state.get("active"):
            raise RuntimeError(
                "A transport pass is already running ({0} on {1}).".format(
                    state.get("parameter") or "recording", state.get("track")))

        song = self._song
        song.create_audio_track(-1)
        track = song.tracks[len(song.tracks) - 1]
        track_index = len(song.tracks) - 1
        if name:
            try:
                track.name = name
            except Exception:
                pass

        # Resolve the input by display name against what Live actually
        # offers; the list depends on the audio interface and on which other
        # tracks exist, so it can never be assumed.
        wanted = str(source).strip().lower()
        chosen = None
        for routing in tuple(track.available_input_routing_types):
            if routing.display_name.strip().lower() == wanted:
                chosen = routing
                break
        if chosen is None:
            for routing in tuple(track.available_input_routing_types):
                if wanted in routing.display_name.strip().lower():
                    chosen = routing
                    break
        if chosen is None:
            available = [r.display_name
                         for r in tuple(track.available_input_routing_types)]
            song.delete_track(track_index)
            raise ValueError(
                "No input routing matching '{0}'. Available: {1}".format(
                    source, ", ".join(available)))
        track.input_routing_type = chosen

        # Monitoring must be Off or a resampling track re-feeds the main bus.
        try:
            track.current_monitoring_state = 2      # 2 == Off
        except Exception:
            pass

        result = self._record_over_range(track_index, from_beat, to_beat)
        self._auto_rec["bounce_track_index"] = track_index
        result["bounce_track"] = track.name
        result["bounce_track_index"] = track_index
        result["source"] = chosen.display_name
        return result

    def _record_over_range(self, track_index, from_beat, to_beat,
                           arm_track=True, return_to_start=True):
        """Record a track's live input into the arrangement over a bar range.

        Same transport pass as record_arrangement_automation, capturing audio
        or MIDI from the track's input instead of writing a parameter: arm the
        track, arm arrangement record, roll from `from_beat`, stop at
        `to_beat`. Punching a take over bars 33-49 becomes one call rather
        than a hand-timed record button.

        Whatever the track is set to monitor is what gets recorded — check
        input routing first if the result is silent.
        """
        state = getattr(self, "_auto_rec", None)
        if state and state.get("active"):
            raise RuntimeError(
                "A transport pass is already running ({0} on {1}). Wait for "
                "it or call cancel_automation_record.".format(
                    state.get("parameter") or "recording",
                    state.get("track")))

        track = self._track_at(track_index)
        from_beat = float(from_beat)
        to_beat = float(to_beat)
        if to_beat <= from_beat:
            raise ValueError("to_beat must be greater than from_beat")
        if arm_track and not getattr(track, "can_be_armed", False):
            raise ValueError(
                "'{0}' cannot be armed — group and return tracks have no "
                "input to record".format(track.name))

        song = self._song
        arm_map = []
        for index, other in enumerate(tuple(song.tracks)):
            try:
                arm_map.append((index, bool(other.arm)))
            except Exception:
                pass

        restore = {"start_time": song.start_time, "loop": song.loop,
                   "return_to_start": bool(return_to_start),
                   "arm_map": arm_map}

        # Disarm every other track first. Any track left armed also records,
        # replacing whatever is arranged on it across the same range — so
        # punching a vocal take over bars 33-49 would quietly destroy bars
        # 33-49 of an unrelated armed track. Exclusive arm usually does this,
        # but it is a preference and cannot be relied on.
        for other in tuple(song.tracks):
            try:
                if other.arm and other != track:
                    other.arm = False
            except Exception:
                pass
        if arm_track:
            track.arm = True
        song.loop = False
        song.start_time = from_beat
        song.record_mode = True
        song.start_playing()

        self._auto_rec = {
            "active": True, "status": "recording",
            "track": track.name, "parameter": "input (take)", "device": None,
            "from_beat": from_beat, "to_beat": to_beat,
            "position": from_beat, "samples": 0, "ticks": 0,
            "rolling": False, "waiting": 0, "stalled": 0,
            "restore": restore,
        }

        try:
            expected_ticks = (to_beat - from_beat) * 60.0 / song.tempo * 10.0
        except Exception:
            expected_ticks = 600.0
        max_ticks = int(expected_ticks * 3) + 100

        def step():
            current = getattr(self, "_auto_rec", None)
            if not current or not current.get("active"):
                return
            try:
                now = song.current_song_time
                previous = current["position"]
                current["position"] = now

                # Playhead-based liveness, same reasoning as the automation
                # recorder: is_playing lags start_playing() by several ticks,
                # and trusting it ended the pass instantly with nothing
                # recorded. A take that silently captures nothing is worse
                # than one that errors.
                if now > from_beat + 1e-6:
                    current["rolling"] = True
                if not current["rolling"]:
                    # A count-in delays the roll by up to 4 bars and Live
                    # reports it, so don't spend the patience budget waiting
                    # for something that is working as configured. This Set
                    # has count_in_duration set, which would otherwise have
                    # aborted a pass as "never_started" before the count
                    # finished.
                    counting_in = False
                    try:
                        counting_in = bool(song.is_counting_in)
                    except Exception:
                        pass
                    if not counting_in:
                        current["waiting"] += 1
                    if current["waiting"] > 40:          # ~4 s of ticks
                        self.log_message(
                            "take recording: transport never rolled")
                        self._finish_auto_rec("never_started")
                        return
                    self.schedule_message(1, step)
                    return

                if now <= previous:
                    current["stalled"] += 1
                else:
                    current["stalled"] = 0
                if now >= to_beat or current["stalled"] >= 3:
                    self._finish_auto_rec("done")
                    return
                current["ticks"] += 1
                if current["ticks"] > max_ticks:
                    self.log_message(
                        "take recording ran past its tick budget; stopping")
                    self._finish_auto_rec("timeout")
                    return
                current["samples"] += 1
            except Exception as exc:
                self.log_message("take recording step failed: " + str(exc))
                self._finish_auto_rec("failed")
                return
            self.schedule_message(1, step)

        self.schedule_message(1, step)

        beats = to_beat - from_beat
        try:
            seconds = beats * 60.0 / song.tempo
        except Exception:
            seconds = None
        return {"started": True, "track": track.name,
                "from_beat": from_beat, "to_beat": to_beat,
                "beats": beats, "estimated_seconds": seconds,
                "armed": bool(arm_track),
                "note": "Recording input in real time. Poll "
                        "get_automation_record_status until status is 'done'."}

    def _play_section(self, from_beat=0.0, to_beat=None, loop=False, play=True):
        """Start arrangement playback at an arbitrary point.

        `song.start_time` IS writable — an earlier note in this repo called it
        read-only and concluded that metering a specific section was
        impossible. It is not: start_playing() honours start_time, so any
        section can be auditioned and metered rather than always hearing the
        Set from bar 1.
        """
        song = self._song
        from_beat = float(from_beat)
        song.start_time = from_beat
        result = {"from_beat": from_beat}
        if to_beat is not None:
            to_beat = float(to_beat)
            if to_beat <= from_beat:
                raise ValueError("to_beat must be greater than from_beat")
            song.loop_start = from_beat
            song.loop_length = to_beat - from_beat
            result["to_beat"] = to_beat
        song.loop = bool(loop)
        result["loop"] = song.loop
        if play:
            song.start_playing()
        # is_playing read in the same tick as start_playing() still reports the
        # old value, so report the intent rather than a stale read.
        result["playing"] = bool(play)
        result["position"] = song.current_song_time
        return result

    def _write_arrangement_automation(self, track_index, parameter_name, points,
                                      device_index=None, clear_first=True,
                                      from_time=None, to_time=None):
        """Write the same envelope into every arrangement clip on a track.

        duplicate_clip_to_arrangement does NOT carry clip envelopes — a copy
        placed in the arrangement arrives with its automation stripped. So
        automation written to a session clip never reaches the arrangement,
        and has to be applied to the arrangement clips themselves.

        Point times are relative to each clip's own start, so one envelope
        shape is stamped onto every clip in the range.
        """
        try:
            track = self._track_at(track_index)
            clips = list(track.arrangement_clips)
            if not clips:
                raise ValueError("'{0}' has no arrangement clips".format(track.name))

            param, owner = self._resolve_parameter(
                track, parameter_name, device_index)

            written = 0
            skipped = 0
            pmin, pmax = param.min, param.max
            for clip in clips:
                if from_time is not None and clip.start_time < float(from_time):
                    skipped += 1
                    continue
                if to_time is not None and clip.start_time >= float(to_time):
                    skipped += 1
                    continue
                try:
                    env = clip.automation_envelope(param)
                except Exception:
                    env = None
                if env is None:
                    try:
                        env = clip.create_automation_envelope(param)
                    except Exception as exc:
                        raise RuntimeError(
                            "Live refuses CLIP envelopes on arrangement clips "
                            "({0}) — only session clips accept them, and "
                            "duplicating a session clip into the arrangement "
                            "strips its envelopes. That is a limit on clip "
                            "envelopes, NOT on arrangement automation: "
                            "arrangement automation is track automation, and "
                            "record_arrangement_automation writes it for real "
                            "by recording off the transport. Use that "
                            "instead.".format(exc))
                if env is None:
                    skipped += 1
                    continue
                if clear_first:
                    try:
                        env.clear()
                    except Exception:
                        pass
                for point in points:
                    t = float(point.get("time", 0.0))
                    length = float(point.get("length", 0.0))
                    raw = float(point.get("value", 0.0))
                    value = pmin + (pmax - pmin) * max(0.0, min(1.0, raw))
                    env.insert_step(t, length, value)
                written += 1

            return {"track": track.name, "parameter": param.name,
                    "device": owner, "clips_written": written,
                    "clips_skipped": skipped,
                    "points_per_clip": len(points)}
        except Exception as e:
            self.log_message("Error writing arrangement automation: " + str(e))
            raise

    def _write_clip_automation(self, track_index, clip_index, parameter_name,
                               points, device_index=None, clear_first=True):
        """Write real automation points into a clip envelope.

        points is a list of {time, value, length} where time and length are
        in beats from the clip start and value is normalized 0.0-1.0.
        Consecutive points with no gap produce a stepped envelope; supply
        many small steps to approximate a ramp.
        """
        try:
            track = self._track_at(track_index)
            slot = track.clip_slots[clip_index]
            if not slot.has_clip:
                raise ValueError("No clip at slot {0}".format(clip_index))
            clip = slot.clip

            param, owner = self._resolve_parameter(
                track, parameter_name, device_index)

            env = None
            try:
                env = clip.automation_envelope(param)
            except Exception:
                env = None
            if env is None:
                env = clip.create_automation_envelope(param)
            if env is None:
                raise ValueError(
                    "Could not create an envelope for '{0}'".format(param.name))

            if clear_first:
                try:
                    env.clear()
                except Exception:
                    pass

            written = 0
            pmin, pmax = param.min, param.max
            for point in points:
                time = float(point.get("time", 0.0))
                length = float(point.get("length", 0.0))
                raw = float(point.get("value", 0.0))
                value = pmin + (pmax - pmin) * max(0.0, min(1.0, raw))
                env.insert_step(time, length, value)
                written += 1

            return {"clip_name": clip.name, "parameter": param.name,
                    "device": owner, "points_written": written,
                    "range": [pmin, pmax]}
        except Exception as e:
            self.log_message("Error writing clip automation: " + str(e))
            raise

    # Clip launch and follow actions

    def _set_clip_launch(self, track_index, clip_index, launch_mode=None,
                         launch_quantization=None, legato=None,
                         velocity_amount=None):
        """Set a clip's launch behaviour."""
        try:
            track = self._track_at(track_index)
            slot = track.clip_slots[clip_index]
            if not slot.has_clip:
                raise ValueError("No clip at slot {0}".format(clip_index))
            clip = slot.clip
            changed = {}
            for attr, val, cast in (("launch_mode", launch_mode, int),
                                    ("launch_quantization", launch_quantization, int),
                                    ("legato", legato, bool),
                                    ("velocity_amount", velocity_amount, float)):
                if val is None:
                    continue
                if not hasattr(clip, attr):
                    changed[attr] = "<not supported in this Live version>"
                    continue
                setattr(clip, attr, cast(val))
                changed[attr] = getattr(clip, attr)
            return {"clip_name": clip.name, "changed": changed}
        except Exception as e:
            self.log_message("Error setting clip launch: " + str(e))
            raise

    def _set_clip_follow_action(self, track_index, clip_index, settings):
        """Set follow-action properties, adapting to this Live version.

        Follow actions were reworked in Live 12, so rather than assume a
        property set, this applies whichever of the requested attributes
        actually exist on the clip and reports the rest as unsupported. Call
        inspect_lom(target='clip', filter='follow') to see what is available.
        """
        try:
            track = self._track_at(track_index)
            slot = track.clip_slots[clip_index]
            if not slot.has_clip:
                raise ValueError("No clip at slot {0}".format(clip_index))
            clip = slot.clip

            available = sorted(
                a for a in dir(clip)
                if "follow" in a.lower() and not a.startswith("_"))

            if not settings:
                current = {}
                for attr in available:
                    try:
                        val = getattr(clip, attr)
                        if not callable(val):
                            current[attr] = val
                    except Exception:
                        pass
                return {"clip_name": clip.name, "available": available,
                        "current": current, "changed": {}}

            changed = {}
            unsupported = []
            for key, val in settings.items():
                if not hasattr(clip, key):
                    unsupported.append(key)
                    continue
                try:
                    existing = getattr(clip, key)
                    if isinstance(existing, bool):
                        val = bool(val)
                    elif isinstance(existing, int):
                        val = int(val)
                    elif isinstance(existing, float):
                        val = float(val)
                    setattr(clip, key, val)
                    changed[key] = getattr(clip, key)
                except Exception as e:
                    unsupported.append("{0} ({1})".format(key, e))

            return {"clip_name": clip.name, "available": available,
                    "changed": changed, "unsupported": unsupported}
        except Exception as e:
            self.log_message("Error setting follow action: " + str(e))
            raise

    # Warp markers

    def _manage_warp_markers(self, track_index, clip_index, action="list",
                             beat_time=None, sample_time=None, warp_mode=None,
                             warping=None):
        """List, add or remove warp markers on an audio clip, and set warp mode."""
        try:
            track = self._track_at(track_index)
            slot = track.clip_slots[clip_index]
            if not slot.has_clip:
                raise ValueError("No clip at slot {0}".format(clip_index))
            clip = slot.clip
            if clip.is_midi_clip:
                raise ValueError("'{0}' is a MIDI clip — warp markers are "
                                 "audio only".format(clip.name))

            result = {"clip_name": clip.name}

            if warping is not None:
                clip.warping = bool(warping)
                result["warping"] = clip.warping

            if warp_mode is not None:
                modes = list(getattr(clip, "available_warp_modes", []))
                clip.warp_mode = int(warp_mode)
                result["warp_mode"] = clip.warp_mode
                result["available_warp_modes"] = modes

            if action == "add":
                if beat_time is None:
                    raise ValueError("beat_time is required to add a warp marker")
                kwargs = {"beat_time": float(beat_time)}
                if sample_time is not None:
                    kwargs["sample_time"] = float(sample_time)
                clip.add_warp_marker(kwargs)
                result["added_at_beat"] = float(beat_time)
            elif action == "remove":
                if beat_time is None:
                    raise ValueError("beat_time is required to remove a warp marker")
                clip.remove_warp_marker(float(beat_time))
                result["removed_at_beat"] = float(beat_time)
            elif action not in ("list", "set"):
                raise ValueError("action must be list, add, remove or set")

            markers = []
            try:
                for m in clip.warp_markers:
                    markers.append({"beat_time": m.beat_time,
                                    "sample_time": m.sample_time})
            except Exception:
                pass
            result["warp_markers"] = markers[:64]
            result["marker_count"] = len(markers)
            return result
        except Exception as e:
            self.log_message("Error managing warp markers: " + str(e))
            raise

    # Routing, monitoring and crossfader

    def _get_routing_options(self, track_index):
        """List the routing types and channels available on a track."""
        try:
            track = self._track_at(track_index)

            def names(collection):
                try:
                    return [x.display_name for x in collection]
                except Exception:
                    return []

            def current(attr):
                try:
                    return getattr(track, attr).display_name
                except Exception:
                    return None

            monitoring = None
            try:
                monitoring = ["In", "Auto", "Off"][track.current_monitoring_state]
            except Exception:
                pass

            return {
                "track_name": track.name,
                "available_input_types": names(
                    getattr(track, "available_input_routing_types", [])),
                "available_input_channels": names(
                    getattr(track, "available_input_routing_channels", [])),
                "available_output_types": names(
                    getattr(track, "available_output_routing_types", [])),
                "available_output_channels": names(
                    getattr(track, "available_output_routing_channels", [])),
                "current_input_type": current("input_routing_type"),
                "current_input_channel": current("input_routing_channel"),
                "current_output_type": current("output_routing_type"),
                "current_output_channel": current("output_routing_channel"),
                "monitoring_state": monitoring,
            }
        except Exception as e:
            self.log_message("Error getting routing options: " + str(e))
            raise

    def _set_track_routing(self, track_index, input_type=None,
                           input_channel=None, output_type=None,
                           output_channel=None):
        """Set a track's input/output routing by display name.

        Names are matched case-insensitively; a unique substring is enough.
        Use get_routing_options first to see what this Set actually offers,
        since available routings depend on the audio interface and on which
        tracks exist.
        """
        try:
            track = self._track_at(track_index)
            changed = {}

            def apply(available_attr, target_attr, wanted):
                options = list(getattr(track, available_attr, []))
                if not options:
                    raise ValueError(
                        "No options available for {0}".format(available_attr))
                wanted_lower = str(wanted).lower()
                match = None
                for opt in options:
                    if opt.display_name.lower() == wanted_lower:
                        match = opt
                        break
                if match is None:
                    partial = [o for o in options
                               if wanted_lower in o.display_name.lower()]
                    if len(partial) == 1:
                        match = partial[0]
                    elif len(partial) > 1:
                        raise ValueError(
                            "'{0}' is ambiguous, matches: {1}".format(
                                wanted, ", ".join(
                                    o.display_name for o in partial)))
                if match is None:
                    raise ValueError(
                        "'{0}' not found. Available: {1}".format(
                            wanted, ", ".join(o.display_name for o in options)))
                setattr(track, target_attr, match)
                changed[target_attr] = match.display_name

            if input_type is not None:
                apply("available_input_routing_types",
                      "input_routing_type", input_type)
            if input_channel is not None:
                apply("available_input_routing_channels",
                      "input_routing_channel", input_channel)
            if output_type is not None:
                apply("available_output_routing_types",
                      "output_routing_type", output_type)
            if output_channel is not None:
                apply("available_output_routing_channels",
                      "output_routing_channel", output_channel)

            return {"track_name": track.name, "changed": changed}
        except Exception as e:
            self.log_message("Error setting track routing: " + str(e))
            raise

    def _set_track_monitoring(self, track_index, state):
        """Set input monitoring: 0 = In, 1 = Auto, 2 = Off."""
        try:
            track = self._track_at(track_index)
            state = int(state)
            if state not in (0, 1, 2):
                raise ValueError("Monitoring state must be 0 (In), 1 (Auto) or 2 (Off)")
            track.current_monitoring_state = state
            return {"track_name": track.name,
                    "monitoring_state": ["In", "Auto", "Off"][state]}
        except Exception as e:
            self.log_message("Error setting monitoring: " + str(e))
            raise

    def _set_crossfade_assign(self, track_index, assign):
        """Assign a track to a crossfader side: 0 = A, 1 = None, 2 = B."""
        try:
            track = self._track_at(track_index)
            assign = int(assign)
            if assign not in (0, 1, 2):
                raise ValueError("Assign must be 0 (A), 1 (None) or 2 (B)")
            track.mixer_device.crossfade_assign = assign
            return {"track_name": track.name,
                    "crossfade_assign": ["A", "None", "B"][assign]}
        except Exception as e:
            self.log_message("Error setting crossfade assign: " + str(e))
            raise

    def _set_crossfader(self, value):
        """Set the crossfader position. 0.0 = full A, 0.5 = centre, 1.0 = full B."""
        try:
            xf = self._song.master_track.mixer_device.crossfader
            target = xf.min + (xf.max - xf.min) * max(0.0, min(1.0, value))
            xf.value = target
            return {"crossfader": xf.value, "display_value": str(xf)}
        except Exception as e:
            self.log_message("Error setting crossfader: " + str(e))
            raise

    def _set_master_volume(self, volume):
        """Set the master track fader. 0.85 is unity gain."""
        try:
            param = self._song.master_track.mixer_device.volume
            target = param.min + (param.max - param.min) * max(0.0, min(1.0, volume))
            param.value = target
            return {"volume": param.value, "display_value": str(param)}
        except Exception as e:
            self.log_message("Error setting master volume: " + str(e))
            raise

    # Drum pads and samples

    def _load_sample_to_drum_pad(self, track_index, pad_note, uri):
        """Load a browser item (sample or device) onto one drum rack pad.

        pad_note is the MIDI note of the pad — 36 is C1, the usual kick slot.
        This is how a custom kit gets built: replace individual pads instead
        of loading a whole preset kit.
        """
        try:
            track = self._track_at(track_index)
            drum_rack = None
            for device in track.devices:
                if getattr(device, "can_have_drum_pads", False):
                    drum_rack = device
                    break
            if drum_rack is None:
                raise ValueError(
                    "No drum rack on track '{0}'".format(track.name))

            pad = None
            for candidate in drum_rack.drum_pads:
                if candidate.note == int(pad_note):
                    pad = candidate
                    break
            if pad is None:
                raise ValueError("No drum pad at note {0}".format(pad_note))

            app = self.application()
            item = self._find_browser_item_by_uri(app.browser, uri)
            if not item:
                raise ValueError("Browser item '{0}' not found".format(uri))

            self._song.view.selected_track = track
            drum_rack.view.selected_drum_pad = pad
            app.browser.load_item(item)

            return {"track_name": track.name, "pad_note": int(pad_note),
                    "pad_name": pad.name, "loaded": item.name}
        except Exception as e:
            self.log_message("Error loading sample to drum pad: " + str(e))
            raise

    # Groove

    def _get_grooves(self):
        """List grooves in the Set's groove pool."""
        try:
            grooves = []
            try:
                for i, g in enumerate(self._song.groove_pool.grooves):
                    grooves.append({"index": i, "name": g.name})
            except Exception:
                return {"supported": False, "grooves": [],
                        "groove_amount": getattr(self._song, "groove_amount", None)}
            return {"supported": True, "grooves": grooves,
                    "groove_amount": getattr(self._song, "groove_amount", None)}
        except Exception as e:
            self.log_message("Error listing grooves: " + str(e))
            raise

    def _set_groove_amount(self, value):
        """Set the global groove amount (0.0-1.0 scaled to Live's range)."""
        try:
            self._song.groove_amount = max(0.0, min(1.0, float(value))) * 1.0
            return {"groove_amount": self._song.groove_amount}
        except Exception as e:
            self.log_message("Error setting groove amount: " + str(e))
            raise

    def _apply_clip_groove(self, track_index, clip_index, groove_name):
        """Assign a groove from the groove pool to a clip, by name."""
        try:
            track = self._track_at(track_index)
            slot = track.clip_slots[clip_index]
            if not slot.has_clip:
                raise ValueError("No clip at slot {0}".format(clip_index))
            clip = slot.clip
            target = None
            for g in self._song.groove_pool.grooves:
                if g.name.lower() == str(groove_name).lower():
                    target = g
                    break
            if target is None:
                available = [g.name for g in self._song.groove_pool.grooves]
                raise ValueError(
                    "Groove '{0}' not found. Available: {1}".format(
                        groove_name, ", ".join(available) or "(pool is empty)"))
            clip.groove = target
            return {"clip_name": clip.name, "groove": target.name}
        except Exception as e:
            self.log_message("Error applying groove: " + str(e))
            raise

    # Transport, capture and undo

    def _set_transport_state(self, metronome=None, loop=None,
                             session_record=None, record_mode=None,
                             punch_in=None, punch_out=None):
        """Set transport toggles. Omitted values are left alone.

        The read-back can lag the write. `record_mode` in particular reports
        its OLD value if read in the same tick it was set, which reads as a
        failed write when the write actually succeeded — so report what was
        requested alongside what was observed rather than only the stale
        read.
        """
        try:
            changed = {}
            for attr, val in (("metronome", metronome), ("loop", loop),
                              ("session_record", session_record),
                              ("record_mode", record_mode),
                              ("punch_in", punch_in), ("punch_out", punch_out)):
                if val is not None:
                    wanted = bool(val)
                    setattr(self._song, attr, wanted)
                    observed = getattr(self._song, attr)
                    entry = {"requested": wanted, "observed": observed}
                    if observed != wanted:
                        entry["note"] = ("Live applies this on its next tick; "
                                         "this read-back is one tick early, "
                                         "not a failed write.")
                    changed[attr] = entry
            return {"changed": changed}
        except Exception as e:
            self.log_message("Error setting transport state: " + str(e))
            raise

    def _capture_midi(self):
        """Capture recently played MIDI into a clip — Live's Capture button."""
        try:
            self._song.capture_midi()
            return {"captured": True}
        except Exception as e:
            self.log_message("Error capturing MIDI: " + str(e))
            raise

    def _undo_redo(self, action):
        """Undo or redo the last operation in Live."""
        try:
            action = str(action).lower()
            if action == "undo":
                if not self._song.can_undo:
                    return {"action": "undo", "performed": False,
                            "reason": "nothing to undo"}
                self._song.undo()
            elif action == "redo":
                if not self._song.can_redo:
                    return {"action": "redo", "performed": False,
                            "reason": "nothing to redo"}
                self._song.redo()
            else:
                raise ValueError("Action must be 'undo' or 'redo'")
            return {"action": action, "performed": True}
        except Exception as e:
            self.log_message("Error in undo/redo: " + str(e))
            raise

    # View and scene management

    def _set_track_fold(self, track_index, folded):
        """Fold or unfold a group track."""
        try:
            track = self._track_at(track_index)
            if not track.is_foldable:
                raise ValueError(
                    "Track '{0}' is not a group track".format(track.name))
            track.fold_state = 1 if folded else 0
            return {"track_name": track.name, "folded": bool(folded)}
        except Exception as e:
            self.log_message("Error folding track: " + str(e))
            raise

    def _select_view_target(self, track_index=None, scene_index=None):
        """Select a track and/or scene in Live's UI."""
        try:
            selected = {}
            if track_index is not None:
                track = self._track_at(track_index)
                self._song.view.selected_track = track
                selected["track"] = track.name
            if scene_index is not None:
                if scene_index < 0 or scene_index >= len(self._song.scenes):
                    raise IndexError("Scene index out of range")
                scene = self._song.scenes[scene_index]
                self._song.view.selected_scene = scene
                selected["scene"] = scene.name or "(unnamed)"
            return {"selected": selected}
        except Exception as e:
            self.log_message("Error selecting view target: " + str(e))
            raise

    def _delete_scene(self, scene_index):
        """Delete a scene."""
        try:
            if len(self._song.scenes) <= 1:
                raise ValueError("Cannot delete the last remaining scene")
            if scene_index < 0 or scene_index >= len(self._song.scenes):
                raise IndexError("Scene index out of range")
            name = self._song.scenes[scene_index].name
            self._song.delete_scene(scene_index)
            return {"deleted": name or "(unnamed)",
                    "scene_count": len(self._song.scenes)}
        except Exception as e:
            self.log_message("Error deleting scene: " + str(e))
            raise

    def _duplicate_scene(self, scene_index):
        """Duplicate a scene, including all its clips."""
        try:
            if scene_index < 0 or scene_index >= len(self._song.scenes):
                raise IndexError("Scene index out of range")
            self._song.duplicate_scene(scene_index)
            new_index = scene_index + 1
            return {"index": new_index,
                    "name": self._song.scenes[new_index].name or "(unnamed)",
                    "scene_count": len(self._song.scenes)}
        except Exception as e:
            self.log_message("Error duplicating scene: " + str(e))
            raise

    def _set_scene_tempo(self, scene_index, tempo):
        """Set a per-scene tempo, or disable it by passing tempo = None.

        Launching that scene then changes the Set's tempo — the mechanism for
        a live set where songs run at different speeds.
        """
        try:
            if scene_index < 0 or scene_index >= len(self._song.scenes):
                raise IndexError("Scene index out of range")
            scene = self._song.scenes[scene_index]
            if tempo is None:
                scene.tempo_enabled = False
                return {"index": scene_index, "tempo_enabled": False}
            scene.tempo = float(tempo)
            scene.tempo_enabled = True
            return {"index": scene_index, "tempo": scene.tempo,
                    "tempo_enabled": True}
        except Exception as e:
            self.log_message("Error setting scene tempo: " + str(e))
            raise

    # Device helper methods

    def _addressable_tracks(self):
        """Session tracks followed by return tracks.

        Return tracks are addressed by indices >= len(song.tracks), so a set
        with 10 session tracks exposes returns A/B/C as indices 10/11/12.
        """
        return list(self._song.tracks) + list(self._song.return_tracks)

    def _track_at(self, track_index):
        """Resolve a 0-based index across session tracks and return tracks."""
        tracks = self._addressable_tracks()
        if track_index < 0 or track_index >= len(tracks):
            raise IndexError("Track index {0} out of range (0-{1})".format(
                track_index, len(tracks) - 1))
        return tracks[track_index]

    def _resolve_device(self, track_index, device_index, chain_index=None):
        """Resolve a device reference to (track, device) tuple.

        Parameters:
        - track_index: 0-based track index
        - device_index: 0-based device index on track (or within chain)
        - chain_index: optional 0-based chain index for rack devices

        Returns (track, device) tuple.
        Raises IndexError or ValueError on invalid references.
        """
        track = self._track_at(track_index)

        if device_index < 0 or device_index >= len(track.devices):
            raise IndexError("Device index {0} out of range on track '{1}' (0-{2})".format(
                device_index, track.name,
                len(track.devices) - 1 if track.devices else 0))

        device = track.devices[device_index]

        if chain_index is not None:
            # Navigate into rack chain
            if not device.can_have_chains:
                raise ValueError("Device '{0}' is not a rack and has no chains".format(device.name))
            chains = device.chains
            if chain_index < 0 or chain_index >= len(chains):
                raise IndexError("Chain index {0} out of range on '{1}' (0-{2})".format(
                    chain_index, device.name, len(chains) - 1))
            # Return the first device in the chain (or the chain's device list)
            # For now, return the chain's first device. If a nested device_index
            # is needed, callers can extend this.
            chain = chains[chain_index]
            if not chain.devices:
                raise ValueError("Chain '{0}' has no devices".format(chain.name))
            # Use device_index 0 within the chain for now (callers pass separate index)
            return track, device

        return track, device

    def _find_parameter(self, device, name=None, index=None):
        """Find a parameter on a device by name or index.

        Lookup order:
        1. Exact name match (case-sensitive)
        2. Case-insensitive exact match
        3. Case-insensitive partial match
        4. By 0-based index

        Returns (parameter, match_type) tuple where match_type is
        'exact', 'case_insensitive', 'partial', or 'index'.
        Raises ValueError if not found.
        """
        params = device.parameters

        if name is not None:
            # Step 1: exact match
            for p in params:
                if p.name == name:
                    return p, "exact"

            # Step 2: case-insensitive exact match
            name_lower = name.lower()
            for p in params:
                if p.name.lower() == name_lower:
                    return p, "case_insensitive"

            # Step 3: case-insensitive partial match
            for p in params:
                if name_lower in p.name.lower():
                    return p, "partial"

            raise ValueError("Parameter '{0}' not found on device '{1}'".format(
                name, device.name))

        if index is not None:
            if index < 0 or index >= len(params):
                raise IndexError("Parameter index {0} out of range on '{1}' (0-{2})".format(
                    index, device.name, len(params) - 1))
            return params[index], "index"

        raise ValueError("Either name or index must be provided")

    def _get_device_info(self, device):
        """Build a device info dict."""
        info = {
            "name": device.name,
            "class_name": device.class_name,
            "type": self._get_device_type(device),
            "is_active": device.is_active,
            "can_have_chains": device.can_have_chains,
            "can_have_drum_pads": device.can_have_drum_pads,
            "parameter_count": len(device.parameters),
        }
        try:
            info["class_display_name"] = device.class_display_name
        except Exception:
            info["class_display_name"] = device.class_name
        return info

    # General helper methods

    def _get_device_type(self, device):
        """Get the type of a device"""
        try:
            # Simple heuristic - in a real implementation you'd look at the device class
            if device.can_have_drum_pads:
                return "drum_machine"
            elif device.can_have_chains:
                return "rack"
            elif "instrument" in device.class_display_name.lower():
                return "instrument"
            elif "audio_effect" in device.class_name.lower():
                return "audio_effect"
            elif "midi_effect" in device.class_name.lower():
                return "midi_effect"
            else:
                return "unknown"
        except:
            return "unknown"
    
    def get_browser_tree(self, category_type="all"):
        """
        Get a simplified tree of browser categories.
        
        Args:
            category_type: Type of categories to get ('all', 'instruments', 'sounds', etc.)
            
        Returns:
            Dictionary with the browser tree structure
        """
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            # Check if browser is available
            if not hasattr(app, 'browser') or app.browser is None:
                raise RuntimeError("Browser is not available in the Live application")
            
            # Log available browser attributes to help diagnose issues
            browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith('_')]
            self.log_message("Available browser attributes: {0}".format(browser_attrs))
            
            result = {
                "type": category_type,
                "categories": [],
                "available_categories": browser_attrs,
                "total_folders": 0,
            }

            max_depth = 2
            max_children = 25

            def process_item(item, depth=0, parent_path="", name_override=None):
                if not item:
                    return None, 0

                name = name_override if name_override is not None else (
                    item.name if hasattr(item, 'name') else "Unknown"
                )
                children_iter = tuple(item.children) if hasattr(item, 'children') else ()
                is_folder = bool(children_iter)
                item_path = (parent_path + "/" + name) if parent_path else name

                node = {
                    "name": name,
                    "is_folder": is_folder,
                    "is_device": hasattr(item, 'is_device') and item.is_device,
                    "is_loadable": hasattr(item, 'is_loadable') and item.is_loadable,
                    "uri": item.uri if hasattr(item, 'uri') else None,
                    "path": item_path,
                    "children": [],
                    "has_more": False,
                }
                folder_count = 1 if is_folder else 0

                if is_folder and depth < max_depth:
                    visible = children_iter[:max_children]
                    if len(children_iter) > max_children:
                        node["has_more"] = True
                    for child in visible:
                        child_node, child_folders = process_item(child, depth + 1, item_path)
                        if child_node:
                            node["children"].append(child_node)
                            folder_count += child_folders
                elif is_folder and depth >= max_depth:
                    node["has_more"] = True

                return node, folder_count

            def _append_category(attr, display_name):
                try:
                    root_item = getattr(app.browser, attr, None)
                    if root_item is None:
                        return
                    node, folder_count = process_item(root_item, name_override=display_name)
                    if node is None:
                        return
                    result["categories"].append(node)
                    result["total_folders"] += folder_count
                except Exception as e:
                    self.log_message("Error processing {0}: {1}".format(attr, str(e)))

            named_categories = [
                ("instruments", "Instruments"),
                ("sounds", "Sounds"),
                ("drums", "Drums"),
                ("audio_effects", "Audio Effects"),
                ("midi_effects", "MIDI Effects"),
            ]
            for attr, display_name in named_categories:
                if (category_type == "all" or category_type == attr) and hasattr(app.browser, attr):
                    _append_category(attr, display_name)

            handled = {attr for attr, _ in named_categories}
            for attr in browser_attrs:
                if attr in handled:
                    continue
                if category_type != "all" and category_type != attr:
                    continue
                _append_category(attr, attr.capitalize())
            
            self.log_message("Browser tree generated for {0} with {1} root categories".format(
                category_type, len(result['categories'])))
            return result
            
        except Exception as e:
            self.log_message("Error getting browser tree: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
    
    def get_browser_items_at_path(self, path, limit=None, offset=0):
        """
        Get browser items at a specific path.

        Args:
            path: Path in the format "category/folder/subfolder"
                 where category is one of: instruments, sounds, drums, audio_effects, midi_effects
                 or any other available browser category
            limit: Maximum number of items to return (None = all). Prevents
                   large folders (e.g. sample packs with 500+ items) from
                   flooding the response.
            offset: Number of items to skip from the start (for pagination).

        Returns:
            Dictionary with items at the specified path. Includes
            `total_count` (items before slicing) and `returned` (after).
        """
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            # Check if browser is available
            if not hasattr(app, 'browser') or app.browser is None:
                raise RuntimeError("Browser is not available in the Live application")
            
            # Log available browser attributes to help diagnose issues
            browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith('_')]
            self.log_message("Available browser attributes: {0}".format(browser_attrs))
                
            # Parse the path
            path_parts = self._split_browser_path(path)
            if not path_parts:
                raise ValueError("Invalid path")
            
            # Determine the root category
            root_category = path_parts[0]
            current_item, resolved_root = self._resolve_browser_root_category(
                app.browser, root_category, browser_attrs
            )
            if current_item is None:
                # If we still haven't found the category, return available categories
                return {
                    "path": path,
                    "error": "Unknown or unavailable category: {0}".format(
                        self._normalize_browser_category_name(root_category)
                    ),
                    "available_categories": browser_attrs,
                    "items": []
                }

            # Keep path canonicalized to resolved root category
            path_parts[0] = resolved_root
            
            # Navigate through the path
            for i in range(1, len(path_parts)):
                part = path_parts[i]
                if not part:  # Skip empty parts
                    continue
                
                if not hasattr(current_item, 'children'):
                    return {
                        "path": path,
                        "error": "Item at '{0}' has no children".format('/'.join(path_parts[:i])),
                        "items": []
                    }
                
                found = False
                for child in current_item.children:
                    if hasattr(child, 'name') and child.name.lower() == part.lower():
                        current_item = child
                        found = True
                        break
                
                if not found:
                    return {
                        "path": path,
                        "error": "Path part '{0}' not found".format(part),
                        "items": []
                    }
            
            # Get items at the current path
            items = []
            if hasattr(current_item, 'children'):
                for child in current_item.children:
                    item_info = {
                        "name": child.name if hasattr(child, 'name') else "Unknown",
                        "is_folder": hasattr(child, 'children') and bool(child.children),
                        "is_device": hasattr(child, 'is_device') and child.is_device,
                        "is_loadable": hasattr(child, 'is_loadable') and child.is_loadable,
                        "uri": child.uri if hasattr(child, 'uri') else None
                    }
                    items.append(item_info)

            # Apply pagination (offset + limit). Negative/invalid values are
            # clamped silently so the Remote Script never crashes on a bad
            # caller; the MCP-server wrapper is where validation errors
            # should surface.
            total_count = len(items)
            try:
                off = max(0, int(offset or 0))
            except (TypeError, ValueError):
                off = 0
            if limit is None:
                sliced = items[off:]
            else:
                try:
                    lim = max(0, int(limit))
                except (TypeError, ValueError):
                    lim = 0
                sliced = items[off:off + lim]

            result = {
                "path": path,
                "name": current_item.name if hasattr(current_item, 'name') else "Unknown",
                "uri": current_item.uri if hasattr(current_item, 'uri') else None,
                "is_folder": hasattr(current_item, 'children') and bool(current_item.children),
                "is_device": hasattr(current_item, 'is_device') and current_item.is_device,
                "is_loadable": hasattr(current_item, 'is_loadable') and current_item.is_loadable,
                "items": sliced,
                "total_count": total_count,
                "returned": len(sliced),
                "offset": off,
                "limit": limit
            }

            self.log_message("Retrieved {0}/{1} items at path: {2} (offset={3}, limit={4})".format(
                len(sliced), total_count, path, off, limit))
            return result
            
        except Exception as e:
            self.log_message("Error getting browser items at path: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
