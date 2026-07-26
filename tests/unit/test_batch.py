"""Batch payload normalisation.

The index rule here is the dangerous part. `LIVE-API-FACTS.md` records that
writing to the wrong track is the failure mode that has corrupted work twice,
and a batch multiplies any off-by-one across every command in the list rather
than showing up once. So the rebasing gets tested directly.
"""

import pytest

from MCP_Server.server import _prepare_batch_commands


class TestIndexRebasing:
    def test_one_based_indices_become_zero_based(self):
        out = _prepare_batch_commands(
            [{"command": "set_track_volume",
              "params": {"track_index": 3, "volume": 0.8}}])
        assert out[0]["params"]["track_index"] == 2
        assert out[0]["params"]["volume"] == 0.8, "non-index params untouched"

    def test_zero_is_left_alone(self):
        """0 means all / by name / not specified — rebasing it to -1 would
        silently retarget the command."""
        out = _prepare_batch_commands(
            [{"command": "delete_track",
              "params": {"track_index": 0, "track_name": "SCRATCH"}}])
        assert out[0]["params"]["track_index"] == 0

    def test_negative_is_left_alone(self):
        """-1 means 'append at the end' for track creation."""
        out = _prepare_batch_commands(
            [{"command": "create_midi_track", "params": {"index": -1}}])
        assert out[0]["params"]["index"] == -1

    def test_every_known_index_field_is_rebased(self):
        out = _prepare_batch_commands([{
            "command": "whatever",
            "params": {"track_index": 1, "clip_index": 2, "device_index": 3,
                       "scene_index": 4, "chain_index": 5},
        }])
        assert out[0]["params"] == {
            "track_index": 0, "clip_index": 1, "device_index": 2,
            "scene_index": 3, "chain_index": 4}

    def test_booleans_are_never_rebased(self):
        """bool subclasses int, so a flag named like an index must not shift."""
        out = _prepare_batch_commands(
            [{"command": "x", "params": {"track_index": True}}])
        assert out[0]["params"]["track_index"] is True

    def test_rebasing_can_be_turned_off(self):
        out = _prepare_batch_commands(
            [{"command": "set_track_volume", "params": {"track_index": 3}}],
            indices_are_one_based=False)
        assert out[0]["params"]["track_index"] == 3

    def test_original_payload_is_not_mutated(self):
        original = {"command": "set_track_volume", "params": {"track_index": 3}}
        _prepare_batch_commands([original])
        assert original["params"]["track_index"] == 3


class TestCommandNames:
    def test_tool_names_that_differ_from_wire_names_are_translated(self):
        """Sending the tool name would fail with 'Unknown command'."""
        pairs = [("duplicate_clip_to_arrangement", "duplicate_to_arrangement"),
                 ("load_instrument_or_effect", "load_browser_item"),
                 ("set_ableton_view", "set_view")]
        for tool_name, wire_name in pairs:
            out = _prepare_batch_commands([{"command": tool_name}])
            assert out[0]["command"] == wire_name

    def test_ordinary_names_pass_through(self):
        out = _prepare_batch_commands([{"command": "set_tempo"}])
        assert out[0]["command"] == "set_tempo"

    def test_type_is_accepted_as_an_alias_for_command(self):
        out = _prepare_batch_commands([{"type": "set_tempo"}])
        assert out[0]["command"] == "set_tempo"

    def test_missing_name_is_rejected_with_its_position(self):
        with pytest.raises(ValueError, match="command 2"):
            _prepare_batch_commands(
                [{"command": "set_tempo"}, {"params": {"x": 1}}])

    def test_non_object_entry_is_rejected_with_its_position(self):
        with pytest.raises(ValueError, match="command 1"):
            _prepare_batch_commands(["set_tempo"])


class TestOrderAndShape:
    def test_order_is_preserved(self):
        out = _prepare_batch_commands(
            [{"command": "a"}, {"command": "b"}, {"command": "c"}])
        assert [c["command"] for c in out] == ["a", "b", "c"]

    def test_missing_params_becomes_an_empty_dict(self):
        out = _prepare_batch_commands([{"command": "stop_playback"}])
        assert out[0]["params"] == {}
