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

def create_instance(c_instance):
    """Create and return the AbletonMCP script instance"""
    return AbletonMCP(c_instance)

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
        
        # Cache the song reference for easier access
        self._song = self.song()
        
        # Start the socket server
        self.start_server()
        
        self.log_message("AbletonMCP initialized")
        
        # Show a message in Ableton
        self.show_message("AbletonMCP: Listening for commands on port " + str(DEFAULT_PORT))
    
    def disconnect(self):
        """Called when Ableton closes or the control surface is removed"""
        self.log_message("AbletonMCP disconnecting...")
        self.running = False
        
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
            elif command_type == "inspect_lom":
                response["result"] = self._inspect_lom(
                    params.get("target", "song"),
                    params.get("track_index", None),
                    params.get("clip_index", None),
                    params.get("device_index", None),
                    params.get("scene_index", None),
                    params.get("filter", ""))
            elif command_type == "get_meters":
                response["result"] = self._get_meters()
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
                        elif command_type == "control_looper":
                            result = self._control_looper(
                                params.get("track_index", 0),
                                params.get("device_index", None),
                                params.get("action", "info"))
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
                                params.get("replace", False))
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
                            result = self._delete_arrangement_clip(ti, ci, cn)
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
                            result = self._add_notes_to_arrangement_clip(ti, ci, notes)
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

    def _add_notes_to_arrangement_clip(self, track_index, clip_index, notes):
        """Add MIDI notes to an arrangement clip."""
        try:
            track, clip = self._resolve_arrangement_clip(track_index, clip_index)
            if not clip.is_midi_clip:
                raise ValueError("Clip is not a MIDI clip")
            live_notes = []
            for note in notes:
                pitch = note.get("pitch", 60)
                start_time = note.get("start_time", 0.0)
                duration = note.get("duration", 0.25)
                velocity = note.get("velocity", 100)
                mute = note.get("mute", False)
                live_notes.append((pitch, start_time, duration, velocity, mute))
            clip.set_notes(tuple(live_notes))
            return {"note_count": len(notes)}
        except Exception as e:
            self.log_message("Error adding notes to arrangement clip: " + str(e))
            raise

    def _delete_arrangement_clip(self, track_index, clip_index=None, clip_name=None):
        """Delete an arrangement clip."""
        try:
            track, clip = self._resolve_arrangement_clip(track_index, clip_index, clip_name)
            track.delete_clip(clip)
            return {"deleted": True}
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

    def _manage_rack(self, track_index, device_index, action="info", value=None):
        """Inspect and control a rack: macros, variations, chain selector."""
        try:
            track = self._track_at(track_index)
            device = track.devices[device_index]
            if not getattr(device, "can_have_chains", False):
                raise ValueError("'{0}' is not a rack".format(device.name))

            if action == "info":
                info = {"device": device.name}
                for attr in ("variation_count", "selected_variation_index",
                             "visible_macro_count", "has_macro_mappings",
                             "is_showing_chains"):
                    try:
                        info[attr] = getattr(device, attr)
                    except Exception:
                        pass
                try:
                    info["chains"] = [c.name for c in device.chains]
                except Exception:
                    pass
                try:
                    info["macros"] = [
                        {"name": p.name, "value": p.value}
                        for p in device.parameters if "Macro" in p.name]
                except Exception:
                    pass
                return info
            if action == "add_macro":
                device.add_macro()
                return {"device": device.name, "action": "add_macro",
                        "visible_macro_count": device.visible_macro_count}
            if action == "remove_macro":
                device.remove_macro()
                return {"device": device.name, "action": "remove_macro",
                        "visible_macro_count": device.visible_macro_count}
            if action == "randomize_macros":
                device.randomize_macros()
                return {"device": device.name, "action": "randomize_macros"}
            if action == "store_variation":
                device.store_variation()
                return {"device": device.name, "action": "store_variation",
                        "variation_count": device.variation_count}
            if action == "recall_variation":
                if value is not None:
                    device.selected_variation_index = int(value)
                device.recall_selected_variation()
                return {"device": device.name, "action": "recall_variation",
                        "selected_variation_index": device.selected_variation_index}
            if action == "delete_variation":
                device.delete_selected_variation()
                return {"device": device.name, "action": "delete_variation",
                        "variation_count": device.variation_count}
            if action == "chain_selector":
                if value is None:
                    raise ValueError("chain_selector requires a value")
                selector = device.chain_selector
                target = selector.min + (selector.max - selector.min) * \
                    max(0.0, min(1.0, float(value)))
                selector.value = target
                return {"device": device.name, "chain_selector": selector.value}
            raise ValueError("Unknown rack action '{0}'".format(action))
        except Exception as e:
            self.log_message("Error managing rack: " + str(e))
            raise

    def _control_looper(self, track_index, device_index=None, action="info"):
        """Control a Looper device — record, overdub, play, stop, clear, export."""
        try:
            track = self._track_at(track_index)
            devices = tuple(track.devices)
            looper = None
            if device_index is not None:
                looper = devices[device_index]
            else:
                for d in devices:
                    if d.class_name == "Looper" or "looper" in d.name.lower():
                        looper = d
                        break
            if looper is None:
                raise ValueError("No Looper found on '{0}'".format(track.name))

            if action == "info":
                info = {"device": looper.name}
                for attr in ("loop_length", "tempo", "record_length_index"):
                    try:
                        info[attr] = getattr(looper, attr)
                    except Exception:
                        pass
                info["available_actions"] = [
                    a for a in ("record", "overdub", "play", "stop", "clear",
                                "undo", "double_length", "half_length",
                                "double_speed", "half_speed",
                                "export_to_clip_slot")
                    if hasattr(looper, a)]
                return info

            if not hasattr(looper, action):
                raise ValueError(
                    "Looper has no action '{0}'. Available: {1}".format(
                        action, ", ".join(
                            a for a in dir(looper)
                            if not a.startswith("_") and callable(
                                getattr(looper, a, None)))))
            getattr(looper, action)()
            return {"device": looper.name, "action": action, "done": True}
        except Exception as e:
            self.log_message("Error controlling looper: " + str(e))
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

    def _add_notes_extended(self, track_index, clip_index, notes, replace=False):
        """Add MIDI notes with per-note probability and velocity deviation.

        The older set_notes API cannot express probability, which is what
        makes programmed patterns breathe — hats that land 80% of the time
        rather than every single loop.
        """
        try:
            import Live
            track = self._track_at(track_index)
            slot = track.clip_slots[clip_index]
            if not slot.has_clip:
                raise ValueError("No clip at slot {0}".format(clip_index))
            clip = slot.clip

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
                    "replaced": bool(replace)}
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
                pname = p.name.strip().lower()
                if pname == target:
                    exact.append((p, device.name))
                elif target in pname:
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
        """Set transport toggles. Omitted values are left alone."""
        try:
            changed = {}
            for attr, val in (("metronome", metronome), ("loop", loop),
                              ("session_record", session_record),
                              ("record_mode", record_mode),
                              ("punch_in", punch_in), ("punch_out", punch_out)):
                if val is not None:
                    setattr(self._song, attr, bool(val))
                    changed[attr] = getattr(self._song, attr)
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
