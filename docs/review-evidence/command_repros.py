#!/usr/bin/env python3
"""Offline defect reproductions for MCP-REVIEW-2026-09-03.md.

These assertions demonstrate defects in the reviewed source, not correct behavior.
They are evidence outside the test suite; after fixes, replace them with positive
regression tests. Live objects and scheduled ticks are mocks; no socket is opened.
"""
import importlib.util
import sys
sys.dont_write_bytecode = True
import types
import argparse
from pathlib import Path
from types import SimpleNamespace as NS

parser = argparse.ArgumentParser(description='Offline reproductions for the Ableton MCP command review; never connects to Live.')
parser.add_argument('source', nargs='?', default=str(Path(__file__).resolve().parents[2]), help='Remote-script source file, or repository root (default: current checkout).')
source_path = Path(parser.parse_args().source).resolve()
if source_path.is_dir():
    source_path /= 'AbletonMCP_Remote_Script/__init__.py'

framework = types.ModuleType('_Framework')
cs = types.ModuleType('_Framework.ControlSurface')
cs.ControlSurface = object
sys.modules['_Framework'] = framework
sys.modules['_Framework.ControlSurface'] = cs
spec = importlib.util.spec_from_file_location('remote_review', str(source_path))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class Track:
    def __init__(self, name, arm=False):
        self.name = name
        self.arm = arm
        self.can_be_armed = True
        self.arrangement_clips = []
        self.available_input_routing_types = []
        self.mixer_device = NS(track_activator=NS(value=1))

class Song:
    def __init__(self, tracks):
        self.tracks = tracks
        self.return_tracks = []
        self.start_time = 0.
        self.current_song_time = 0.
        self.loop = True
        self.record_mode = False
        self.tempo = 120.
        self.is_counting_in = False
        self.starts = 0
        self.stops = 0
    def start_playing(self): self.starts += 1
    def stop_playing(self): self.stops += 1
    def create_audio_track(self, index):
        # Match the exclusive-arm-on-track-creation behaviour recorded in docs.
        for t in self.tracks: t.arm = False
        t = Track('New Audio', True)
        t.available_input_routing_types = [NS(display_name=x.name) for x in self.tracks] + [NS(display_name='Resampling')]
        self.tracks.append(t)
    def delete_track(self, index): self.tracks.pop(index)

def setup(tracks=None):
    s = mod.AbletonMCP.__new__(mod.AbletonMCP)
    song = Song(tracks or [Track('Bass')])
    s.song = lambda: song
    s.log_message = lambda msg: None
    queue = []
    s.schedule_message = lambda delay, callback: queue.append(callback)
    return s, song, queue

s, song, queue = setup()
s._freeze_track(0, 0., 8.)
s._cancel_automation_record()
assert song.tracks[0].mixer_device.track_activator.value == 0
assert not song.tracks[1].arrangement_clips
print('REPRO: cancelling freeze muted source with no bounce clip')

s, song, queue = setup()
s._record_over_range(0, 0., 4.)
s._cancel_automation_record()
s._record_over_range(0, 100., 200.)
song.current_song_time = 100.1
queue.pop(0)()  # Callback from the cancelled 0..4 pass.
assert s._auto_rec['status'] == 'done'
assert s._auto_rec['to_beat'] == 200.
assert song.current_song_time < 200.
print('REPRO: stale callback marked new 100..200 pass done at 100.1')

s, song, queue = setup()
s._export_stems(0., 8., ['Bass'])
s._cancel_automation_record()
assert s._auto_rec['status'] == 'cancelled'
queue.pop(0)()
assert s._auto_rec['active'] and s._auto_rec['status'] == 'recording'
print('REPRO: cancelled stem preparation still started recording')

s, song, queue = setup([Track('Bass', True), Track('Lead')])
s._export_stems(0., 8., ['Bass', 'Lead'])
queue.pop(0)()
s._finish_auto_rec('done')
assert all(t.arm for t in song.tracks[2:])
assert song.tracks[0].arm
print('REPRO: finished stem export left both new recording tracks armed')

s, song, queue = setup([Track('Bass', True)])
s._bounce_to_audio(0., 8., 'Bass')
s._finish_auto_rec('done')
assert not song.tracks[0].arm
assert song.tracks[1].arm
print('REPRO: bounce lost original arm state and restored new track armed')

s, song, queue = setup()
s._record_over_range(0, 0., 8.)
try: s._freeze_track(0, 0., 8.)
except RuntimeError: pass
else: raise AssertionError('expected active guard error')
assert len(song.tracks) == 2
assert not song.tracks[0].arm
print('REPRO: rejected freeze during active pass created track and disarmed active recorder')

s, song, queue = setup()
try: s._bounce_to_audio(8., 0.)
except ValueError: pass
else: raise AssertionError('expected range error')
assert len(song.tracks) == 2
print('REPRO: invalid bounce range leaves new audio track behind')

live = types.ModuleType('Live')
live.Clip = NS(MidiNoteSpecification=lambda **kw: NS(**kw))
sys.modules['Live'] = live
class Clip:
    name = 'Existing MIDI'
    length = 8.
    def __init__(self, remove_fails=False):
        self.notes = ['original']
        self.remove_fails = remove_fails
    def remove_notes_extended(self, *args):
        if self.remove_fails: raise RuntimeError('remove not supported')
        self.notes.clear()
    def add_new_notes(self, notes): self.notes.extend(notes)
s, song, queue = setup()
clip = Clip()
s._resolve_clip = lambda *args: (song.tracks[0], clip)
try: s._add_notes_extended(0, 0, [{'pitch': 'invalid'}], replace=True)
except ValueError: pass
else: raise AssertionError('expected pitch validation error')
assert clip.notes == []
print('REPRO: invalid replacement pitch deleted all old notes before error')

clip = Clip(remove_fails=True)
s._resolve_clip = lambda *args: (song.tracks[0], clip)
r = s._add_notes_extended(0, 0, [{'pitch': 60}], replace=True)
assert r['replaced'] and len(clip.notes) == 2
print('REPRO: failed note removal silently appended notes and reported replaced=True')

class NoteClip:
    name = 'Humanize test'
    length = 8.
    is_midi_clip = True
    def __init__(self):
        self.notes = [NS(pitch=60, start_time=0., velocity=100., probability=1.), NS(pitch=62, start_time=1., velocity=100., probability=1.)]
    def get_notes_extended(self, *args): return self.notes
    def apply_note_modifications(self, notes): self.notes = notes
s, song, queue = setup()
clip = NoteClip()
s._resolve_clip = lambda *args: (song.tracks[0], clip)
s._modify_clip_notes(0, 0, humanize_ms=10.)
first_pass = clip.notes[1].start_time
s._modify_clip_notes(0, 0, humanize_ms=10.)
second_pass = clip.notes[1].start_time
assert abs(first_pass - 1.012) < 1e-9
assert abs(second_pass - 1.024) < 1e-9
print('REPRO: repeated identical humanize moved note 1.000 -> 1.012 -> 1.024 beats')
