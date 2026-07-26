"""Programmatic three-place registration check.

`docs/TOOLING-BACKLOG.md` has required this since two defects reached commits
by being "checked by eye". It was never automated, and a third defect of the
same family followed: `add_notes_to_arrangement_clip` was implemented on the
Live side, dispatched, and registered as mutating in BOTH command lists — but
had no `@mcp.tool` in server.py, so it was unreachable from the client. The
capability existed and could not be called.

These tests parse the two source files and assert the wiring holds, so the
next orphan fails CI instead of being discovered months later.

Deliberately source-parsing rather than import-based: the remote script imports
`Live`, which only exists inside Ableton.
"""

import ast
import os
import re

REPO = os.path.join(os.path.dirname(__file__), '..', '..')
REMOTE = os.path.join(REPO, 'AbletonMCP_Remote_Script', '__init__.py')
SERVER = os.path.join(REPO, 'MCP_Server', 'server.py')

# Commands that no tool sends, with the reason each is acceptable.
# Anything NOT listed here must be reachable from some @mcp.tool.
#
# Note this is only for commands genuinely never sent. A wire name that simply
# differs from its tool name (duplicate_to_arrangement ← duplicate_clip_to_-
# arrangement, load_browser_item ← load_instrument_or_effect, set_view ←
# set_ableton_view) does NOT belong here — those are sent, so they pass.
INTENTIONALLY_UNEXPOSED = {
    'add_notes_to_arrangement_clip':
        'kept for wire compatibility; delegates to _add_notes_extended. '
        'Exposed as add_notes_extended(arrangement=True).',

    # Legacy handlers superseded by a differently-named command that IS
    # exposed. Dead on the server side but harmless; left in place rather
    # than removed so any older client keeps working.
    'get_browser_items':
        'legacy; tools send get_browser_items_at_path',
    'get_browser_item':
        'legacy; tools send get_browser_items_at_path',
    'get_browser_categories':
        'legacy; tools send get_browser_tree',
    'load_instrument_or_effect':
        'legacy command name; the tool of the same name sends '
        'load_browser_item instead',
}


def _read(path):
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.read()


def _remote_source():
    return _read(REMOTE)


def _server_source():
    return _read(SERVER)


def _dispatch_branches(src):
    """Every command name with an `elif command_type == "x"` branch."""
    return set(re.findall(r'command_type\s*==\s*["\']([a-z0-9_]+)["\']', src))


def _mutating_list(src):
    """Command names inside the `command_type in [...]` membership test."""
    m = re.search(r'command_type\s+in\s+\[(.*?)\]', src, re.DOTALL)
    assert m, "could not locate the mutating-command list"
    return set(re.findall(r'["\']([a-z0-9_]+)["\']', m.group(1)))


def _sent_commands(src):
    """Every command name passed to send_command( in server.py."""
    return set(re.findall(
        r'send_command\(\s*["\']([a-z0-9_]+)["\']', src))


def _tool_names(src):
    """Function names decorated with @mcp.tool()."""
    tree = ast.parse(src)
    names = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                attr = getattr(target, 'attr', None)
                if attr == 'tool':
                    names.append(node.name)
    return names


class TestRemoteScriptWiring:
    """A command must be implemented, dispatched, and declared mutating."""

    def test_every_mutating_command_has_a_dispatch_branch(self):
        src = _remote_source()
        declared = _mutating_list(src)
        dispatched = _dispatch_branches(src)
        missing = sorted(declared - dispatched)
        assert not missing, (
            "declared mutating but never dispatched — these fail only at "
            "runtime: {0}".format(missing))

    def test_no_duplicate_handler_methods(self):
        """Class-level methods only — nested helpers may share a name."""
        src = _remote_source()
        defs = re.findall(r'^    def (_[a-z0-9_]+)\(', src, re.MULTILINE)
        dupes = sorted({n for n in defs if defs.count(n) > 1})
        assert not dupes, (
            "duplicate method definitions — the later silently wins: "
            "{0}".format(dupes))


class TestServerWiring:
    """Tools must send real commands, and commands must be reachable."""

    def test_no_tool_sends_an_unknown_command(self):
        remote = _remote_source()
        known = _dispatch_branches(remote)
        sent = _sent_commands(_server_source())
        unknown = sorted(sent - known)
        assert not unknown, (
            "server sends commands the remote script does not handle: "
            "{0}".format(unknown))

    def test_no_duplicate_tool_names(self):
        names = _tool_names(_server_source())
        dupes = sorted({n for n in names if names.count(n) > 1})
        assert not dupes, "duplicate @mcp.tool names: {0}".format(dupes)

    def test_every_command_is_reachable_from_some_tool(self):
        """The orphan check: a built capability with no way to call it.

        This is the test that `add_notes_to_arrangement_clip` failed.
        """
        remote = _remote_source()
        dispatched = _dispatch_branches(remote)
        sent = _sent_commands(_server_source())
        orphans = sorted(dispatched - sent - set(INTENTIONALLY_UNEXPOSED))
        assert not orphans, (
            "implemented and dispatched but no tool sends them, so they "
            "cannot be called: {0}\nEither add a tool or list the command in "
            "INTENTIONALLY_UNEXPOSED with a reason.".format(orphans))

    def test_unexposed_allowlist_has_no_stale_entries(self):
        """An allowlisted command that now has a tool should leave the list."""
        sent = _sent_commands(_server_source())
        stale = sorted(set(INTENTIONALLY_UNEXPOSED) & sent)
        assert not stale, (
            "these are listed as intentionally unexposed but a tool now sends "
            "them — remove them from the allowlist: {0}".format(stale))


class TestRecorderLiveness:
    """Every real-time pass must judge liveness on the PLAYHEAD.

    `song.is_playing` keeps reading False for several ticks after
    start_playing(). A recorder that treated that as "the user stopped" ended
    its pass after ~2 ticks having written nothing, and reported success. The
    fix was to watch `current_song_time` instead — and the risk now is that a
    fourth tick loop gets added later using the obvious-but-wrong signal.
    """

    def test_every_tick_loop_uses_playhead_stall_detection(self):
        src = _remote_source()
        loops = len(re.findall(r'^\s+def step\(\):', src, re.MULTILINE))
        stalled = len(re.findall(r'current\["stalled"\]', src))
        assert loops >= 3, "expected the recorder tick loops to be present"
        assert stalled >= loops, (
            "{0} tick loop(s) but only {1} reference(s) to the playhead-stall "
            "counter — a loop is judging liveness some other way, most likely "
            "on song.is_playing, which lags and silently truncates the "
            "pass".format(loops, stalled))

    def test_every_tick_loop_tolerates_a_count_in(self):
        """count_in_duration delays the roll by up to 4 bars."""
        src = _remote_source()
        loops = len(re.findall(r'^\s+def step\(\):', src, re.MULTILINE))
        assert len(re.findall(r'is_counting_in', src)) >= loops, (
            "a tick loop does not check is_counting_in, so a count-in will "
            "burn its patience budget and abort as 'never_started'")


class TestStemExportArmHandling:
    """Stem export arms many tracks at once and must restore the arm map.

    An earlier version tried to switch `exclusive_arm` off first. That was
    wrong twice over: the property is READ-ONLY on Song, and exclusive arm
    does not apply to arm writes through the API anyway — two resampling
    tracks armed via the API both read back True. Live's exclusive arm fires
    when a track is CREATED, which is a different thing entirely.
    """

    def test_export_stems_does_not_try_to_write_exclusive_arm(self):
        src = _remote_source()
        body = re.search(r'def _export_stems\(.*?\n(.*?)(?=\n    def )',
                         src, re.DOTALL)
        assert body, "_export_stems not found"
        assert 'song.exclusive_arm =' not in body.group(1), (
            "exclusive_arm has no setter — writing it raises and aborts the "
            "export before any stem is recorded")

    def test_export_stems_disarms_non_stem_tracks(self):
        """solo_arm=False keeps the stem tracks armed — and would also leave
        the user's armed track armed, so it records too. Observed once: a
        stray empty clip punched across the export range on FX / RISER."""
        src = _remote_source()
        body = re.search(r'def _export_stems\(.*?\n(.*?)(?=\n    def )',
                         src, re.DOTALL).group(1)
        assert 'stem_indices' in body and 'other.arm = False' in body, (
            "export_stems runs the pass with solo_arm=False, so it must "
            "disarm every non-stem track itself or they record too")

    def test_export_stems_restores_the_arm_map(self):
        src = _remote_source()
        body = re.search(r'def _export_stems\(.*?\n(.*?)(?=\n    def )',
                         src, re.DOTALL).group(1)
        assert 'pre_arm' in body and 'arm_map' in body, (
            "export_stems must capture arm state before arming stem tracks "
            "and restore it afterwards")

    def test_song_options_never_writes_a_read_only_property(self):
        """Probed: these four raise \"no setter\" on Song."""
        src = _remote_source()
        body = re.search(r'def _set_song_options\(.*?\n(.*?)(?=\n    def )',
                         src, re.DOTALL).group(1)
        for name in ('exclusive_arm', 'exclusive_solo', 'select_on_launch',
                     'count_in_duration'):
            assert 'setattr(song, "%s"' % name not in body
            assert 'song.%s =' % name not in body, (
                "%s is read-only; writing it raises" % name)


class TestBuildStamp:
    """The two halves must advertise the same build, or the handshake lies."""

    def test_build_ids_match(self):
        remote = re.search(
            r'^BUILD_ID\s*=\s*["\'](.+?)["\']', _remote_source(), re.MULTILINE)
        server = re.search(
            r'^SERVER_BUILD_ID\s*=\s*["\'](.+?)["\']', _server_source(),
            re.MULTILINE)
        assert remote, "remote script has no BUILD_ID"
        assert server, "server has no SERVER_BUILD_ID"
        assert remote.group(1) == server.group(1), (
            "BUILD_ID {0!r} != SERVER_BUILD_ID {1!r} — get_build_info would "
            "report a mismatch between two files that shipped together, "
            "hiding a real Live-vs-repo skew behind a false one.".format(
                remote.group(1), server.group(1)))


class TestArrangementNoteRouting:
    """The backlog item this batch closed: note tools reaching the arrangement."""

    def test_note_commands_accept_an_arrangement_flag(self):
        src = _remote_source()
        for handler in ('_get_clip_notes', '_modify_clip_notes',
                        '_remove_clip_notes', '_add_notes_extended'):
            m = re.search(
                r'def ' + handler + r'\((.*?)\):', src, re.DOTALL)
            assert m, "handler {0} not found".format(handler)
            assert 'arrangement' in m.group(1), (
                "{0} cannot target arrangement clips".format(handler))

    def test_note_handlers_route_through_the_unified_resolver(self):
        """They must not call _clip_at directly, or arrangement=True is ignored."""
        src = _remote_source()
        for handler in ('_get_clip_notes', '_modify_clip_notes',
                        '_remove_clip_notes', '_add_notes_extended'):
            body = re.search(
                r'def ' + handler + r'\(.*?\n(.*?)(?=\n    def )',
                src, re.DOTALL)
            assert body, "handler {0} body not found".format(handler)
            assert '_resolve_clip(' in body.group(1), (
                "{0} does not use _resolve_clip, so its arrangement flag "
                "would be silently ignored".format(handler))

    def test_remove_clip_notes_is_wired_end_to_end(self):
        remote = _remote_source()
        server = _server_source()
        assert 'remove_clip_notes' in _mutating_list(remote)
        assert 'remove_clip_notes' in _dispatch_branches(remote)
        assert 'def _remove_clip_notes' in remote
        assert 'remove_clip_notes' in _sent_commands(server)
        assert 'remove_clip_notes' in _tool_names(server)
