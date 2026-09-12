"""Latency reports preserve unknowns, source identity and bounded traversal."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS

import pytest


_PATH = Path(__file__).parents[2] / "AbletonMCP_Remote_Script" / "latency_report.py"
_SPEC = importlib.util.spec_from_file_location("latency_report", _PATH)
report = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(report)


class Device:
    def __init__(self, name="Effect", ms=0, samples=0, active=True, chains=None, returns=None):
        self.name, self.class_name = name, "AudioEffect"
        self._ms, self._samples = ms, samples
        self.is_active = active
        self.can_have_chains = chains is not None or returns is not None
        self.chains, self.return_chains = chains or [], returns or []

    @property
    def latency_in_ms(self):
        if isinstance(self._ms, Exception):
            raise self._ms
        return self._ms

    @property
    def latency_in_samples(self):
        if isinstance(self._samples, Exception):
            raise self._samples
        return self._samples


class Track:
    def __init__(self, handle, devices=None, parent=None, group=False, kind="track", monitoring=2):
        self.handle, self.name, self.kind = handle, handle.upper(), kind
        self.devices = devices or []
        self.is_foldable, self.is_grouped, self.group_track = group, parent is not None, parent
        self.current_monitoring_state, self.can_be_armed, self.arm = monitoring, not group, False
        self.input_routing_type, self.input_routing_channel = NS(display_name="Ext. In"), NS(display_name="1/2")
        self.output_routing_type, self.output_routing_channel = NS(display_name="Master"), NS(display_name="1/2")


class Remote:
    def __init__(self, tracks=None, returns=None, master=None):
        self._song = NS(tracks=tracks or [], return_tracks=returns or [],
                        master_track=master or Track("main", kind="master"), name="Test Set", file_path="/tmp/Test Set.als")
        self.session_id = "set-1"
        self._target_refs = {}
        self.calls = 0
        self.on_refresh = None

    def _get_edit_targets(self):
        self.calls += 1
        if self.on_refresh:
            self.on_refresh(self)
        tracks = self._song.tracks + self._song.return_tracks
        self._target_refs = {track.handle: track for track in tracks}
        return {"session_id": self.session_id, "revision": repr([(track.handle, track.name) for track in tracks]),
                "set_name": self._song.name, "set_file_path": self._song.file_path,
                "tracks": [{"track_handle": track.handle, "track_index": index + 1,
                            "name": track.name, "kind": track.kind} for index, track in enumerate(tracks)]}


def row(result, handle):
    return next(track for track in result["tracks"] if track["track_handle"] == handle)


def test_report_reads_device_latency_without_changing_monitoring_or_arming():
    vocal = Track("vocal", [Device("EQ", 0, 0)], monitoring=2)
    guitar = Track("guitar", [Device("Cabinet", 1.5, 66)], monitoring=1)
    returns = Track("room", [Device("Reverb", 10, 441)], kind="return")
    master = Track("main", [Device("Limiter", 128 * 1000 / 44100, 128)], kind="master")
    remote = Remote([vocal, guitar], [returns], master)
    result = report.get_report(remote, "set-1")
    assert result["status"] == "complete" and result["complete"] is True
    assert not result["truncated"] and not result["read_errors"]
    assert result["master"]["reported_device_chain_latency_samples"] == 128
    assert result["master"]["reported_device_chain_latency_ms"] == 128 * 1000 / 44100
    assert row(result, "vocal")["monitoring_state"] == "off"
    assert row(result, "guitar")["monitoring_state"] == "auto"
    assert row(result, "room")["monitoring_state"] is None
    assert row(result, "guitar")["input_routing_channel"] == "1/2"
    assert result["scope"]["includes_all_returns"] and result["scope"]["includes_master"]
    assert vocal.current_monitoring_state == 2 and guitar.current_monitoring_state == 1
    assert vocal.arm is guitar.arm is False
    assert all(item["value"] is None for item in result["manual_settings"])
    assert "No routing-path aggregate" in result["measurement_scope"]
    codes = {item["code"] for item in result["findings"]}
    assert "master_device_latency" in codes and "live_monitoring_enabled" in codes


def test_rack_children_and_parallel_return_chains_are_inventory_not_added_twice():
    inner = Device("Slow child", 100, 4410)
    rack = Device("Rack", 2, 88, chains=[NS(name="Parallel A", devices=[inner]),
                  NS(name="Parallel B", devices=[Device("Other child", 80, 3528)])],
                  returns=[NS(name="Rack return", devices=[Device("Delay", 500, 22050)])])
    target = Track("guitar", [rack, Device("Post rack", 3, 132)])
    result = report.get_report(Remote([target]), "set-1")
    actual = row(result, "guitar")
    assert result["complete"] and actual["reported_device_chain_latency_ms"] == 5
    assert actual["reported_device_chain_latency_samples"] == 220
    assert actual["devices"][0]["chains"][0]["devices"][0]["reported_latency_ms"] == 100
    assert actual["devices"][0]["return_chains"][0]["devices"][0]["reported_latency_ms"] == 500


def test_inactive_device_retains_reported_latency_and_is_flagged():
    device = Device("Bypassed lookahead", 4, 176, active=False)
    result = report.get_report(Remote([Track("vocal", [device])]), "set-1")
    assert row(result, "vocal")["reported_device_chain_latency_ms"] == 4
    finding = next(item for item in result["findings"] if item["code"] == "inactive_device_reports_latency")
    assert finding["device_name"] == "Bypassed lookahead"
    assert finding["reported_latency_ms"] == 4 and device.is_active is False


@pytest.mark.parametrize("value", [None, True, -1, float("nan"), float("inf"), "3", 10 ** 500,
                                   AttributeError("not exposed"), RuntimeError("object unavailable")])
def test_unknown_or_invalid_milliseconds_are_null_and_make_report_incomplete(value):
    result = report.get_report(Remote([Track("vocal", [Device(ms=value, samples=128)])]), "set-1")
    actual = row(result, "vocal")
    assert result["status"] == "incomplete" and result["complete"] is False
    assert actual["devices"][0]["reported_latency_ms"] is None
    assert actual["reported_device_chain_latency_ms"] is None
    assert actual["reported_device_chain_latency_samples"] == 128
    assert actual["devices"][0]["read_errors"]


@pytest.mark.parametrize("value", [None, True, -1, 1.5, float("nan"), float("inf"), "2"])
def test_unknown_or_fractional_samples_are_not_inferred_from_milliseconds(value):
    result = report.get_report(Remote([Track("vocal", [Device(ms=2, samples=value)])]), "set-1")
    actual = row(result, "vocal")
    assert actual["reported_device_chain_latency_ms"] == 2
    assert actual["reported_device_chain_latency_samples"] is None
    assert not result["complete"]


def test_missing_nested_latency_does_not_replace_valid_direct_rack_value():
    rack = Device("Rack", 3, 132, chains=[NS(name="Nested", devices=[Device(ms=None)])])
    result = report.get_report(Remote([Track("vocal", [rack])]), "set-1")
    assert not result["complete"]
    assert row(result, "vocal")["reported_device_chain_latency_ms"] == 3


def test_empty_device_chain_has_known_zero_without_inventing_manual_settings():
    result = report.get_report(Remote([Track("vocal")]), "set-1")
    assert result["complete"]
    assert row(result, "vocal")["reported_device_chain_latency_ms"] == 0
    assert row(result, "vocal")["reported_device_chain_latency_samples"] == 0
    assert all(item["value"] is None for item in result["manual_settings"])


def test_direct_sum_overflow_is_unknown_not_infinity():
    result = report.get_report(Remote([Track("vocal", [Device(ms=1e308), Device(ms=1e308)])]), "set-1")
    assert row(result, "vocal")["reported_device_chain_latency_ms"] is None
    assert not result["complete"]


def test_scope_adds_real_parent_groups_all_returns_and_master_without_unrelated_tracks():
    top = Track("top", [Device(ms=2)], group=True)
    group = Track("group", [Device(ms=3)], parent=top, group=True)
    vocal = Track("vocal", parent=group)
    unrelated = Track("unrelated", [Device(ms=5)])
    returns = Track("room", kind="return")
    requested = ["vocal"]
    result = report.get_report(Remote([top, group, vocal, unrelated], [returns]), "set-1", requested)
    assert set(result["scope"]["reported_track_handles"]) == {"vocal", "group", "top", "room"}
    assert requested == ["vocal"]
    assert row(result, "vocal")["group_track"] == {"track_handle": "group", "name": "GROUP"}
    assert row(result, "group")["monitoring_applicable"] is False
    assert row(result, "group")["arm"] is None
    assert row(result, "top")["focused"] is False
    finding = next(item for item in result["findings"] if item["code"] == "parent_group_reports_latency")
    assert finding["group_track_handle"] == "group" and finding["reported_latency_ms"] == 3


@pytest.mark.parametrize("handles", [[], "vocal", ["vocal", "vocal"], [None], [True], ["missing"]])
def test_invalid_or_stale_scope_is_rejected(handles):
    with pytest.raises(ValueError):
        report.get_report(Remote([Track("vocal")]), "set-1", handles)


@pytest.mark.parametrize("session", [None, "", True, "old-set"])
def test_stale_set_is_rejected_before_collecting_devices(session):
    with pytest.raises(ValueError):
        report.get_report(Remote([Track("vocal")]), session)


@pytest.mark.parametrize("change", ["set", "rename", "replace", "remove"])
def test_identity_changes_during_report_reject_mixed_snapshot(change):
    remote = Remote([Track("vocal")])
    def refresh(instance):
        if instance.calls != 2:
            return
        if change == "set":
            instance.session_id = "set-2"
        elif change == "rename":
            instance._song.tracks[0].name = "Renamed"
        elif change == "replace":
            instance._song.tracks[0] = Track("vocal")
        else:
            instance._song.tracks.clear()
    remote.on_refresh = refresh
    with pytest.raises(ValueError, match="changed"):
        report.get_report(remote, "set-1")


def test_deleted_parent_is_reported_unknown_not_assigned_by_name():
    target = Track("vocal", parent=Track("unavailable_group", group=True))
    result = report.get_report(Remote([target]), "set-1")
    assert not result["complete"] and row(result, "vocal")["group_track"] is None
    assert any(error["field"] == "group_track" for error in result["read_errors"])


@pytest.mark.parametrize("value", [None, True, -1, 3, "off"])
def test_unreadable_monitoring_is_null_and_never_written(value):
    target = Track("vocal", monitoring=value)
    result = report.get_report(Remote([target]), "set-1")
    assert row(result, "vocal")["monitoring_state"] is None and not result["complete"]
    assert target.current_monitoring_state is value


def test_routing_failure_is_reported_without_guessing_master_path():
    target = Track("vocal")
    del target.output_routing_type
    result = report.get_report(Remote([target]), "set-1")
    assert row(result, "vocal")["output_routing_type"] is None and not result["complete"]
    assert any(error["field"] == "output_routing_type" for error in row(result, "vocal")["read_errors"])


def test_device_cycles_and_depth_limits_are_bounded(monkeypatch):
    rack = Device("Cycle", chains=[NS(name="Loop", devices=[])])
    rack.chains[0].devices.append(rack)
    result = report.get_report(Remote([Track("vocal", [rack])]), "set-1")
    assert result["truncated"] and not result["complete"]
    assert result["limits"]["devices_read"] == 1
    monkeypatch.setattr(report, "MAX_DEPTH", 1)
    inner = Device("Inner", chains=[NS(name="Too deep", devices=[Device("Leaf")])])
    outer = Device("Outer", chains=[NS(name="Nested", devices=[inner])])
    result = report.get_report(Remote([Track("vocal", [outer])]), "set-1")
    assert result["truncated"] and not result["complete"]
    assert result["limits"]["devices_read"] == 2


class CountingIterable:
    def __init__(self, factory):
        self.factory, self.reads = factory, 0

    def __iter__(self):
        while True:
            self.reads += 1
            yield self.factory()


def test_global_device_budget_bounds_even_infinite_collections_and_preserves_main(monkeypatch):
    monkeypatch.setattr(report, "MAX_DEVICES", 3)
    devices = CountingIterable(lambda: Device(ms=2, samples=88))
    target = Track("vocal")
    target.devices = devices
    master = Track("main", [Device("Limiter", 1, 44)], kind="master")
    result = report.get_report(Remote([target], master=master), "set-1")
    assert devices.reads == 3  # Two remaining slots plus a truncation sentinel.
    assert result["limits"]["devices_read"] == 3 and result["truncated"]
    assert row(result, "vocal")["reported_device_chain_latency_ms"] is None
    assert result["master"]["reported_device_chain_latency_ms"] == 1


def test_global_chain_budget_and_per_device_cap_are_enforced(monkeypatch):
    monkeypatch.setattr(report, "MAX_TOTAL_CHAINS", 2)
    chains = CountingIterable(lambda: NS(name="Empty chain", devices=[]))
    rack = Device("Rack")
    rack.can_have_chains, rack.chains = True, chains
    result = report.get_report(Remote([Track("vocal", [rack])]), "set-1")
    assert chains.reads == 3 and result["limits"]["chains_read"] == 2
    assert result["truncated"] and not result["complete"]


def test_cycle_placeholders_also_consume_device_output_budget(monkeypatch):
    monkeypatch.setattr(report, "MAX_DEVICES", 3)
    rack = Device("Cycle", chains=[NS(name="First", devices=[]), NS(name="Second", devices=[])])
    for chain in rack.chains:
        chain.devices = [rack] * 100
    result = report.get_report(Remote([Track("vocal", [rack])]), "set-1")
    assert result["limits"]["devices_read"] == 1
    assert result["limits"]["device_nodes"] == 3
    assert len(list(report._walk_devices(row(result, "vocal")))) == 3
    assert result["truncated"] and not result["complete"]


def test_track_limit_is_explicit_and_still_reports_main(monkeypatch):
    monkeypatch.setattr(report, "MAX_TRACKS", 2)
    result = report.get_report(Remote([Track("one"), Track("two"), Track("three")]), "set-1")
    assert len(result["tracks"]) == 2 and result["truncated"] and not result["complete"]
    assert result["master"] is not None


def test_result_edits_do_not_modify_live_objects_or_requested_scope():
    target = Track("vocal", [Device("Effect", 1, 44)])
    scope = ["vocal"]
    result = report.get_report(Remote([target]), "set-1", scope)
    result["tracks"][0]["name"] = "Changed"
    result["tracks"][0]["devices"][0]["reported_latency_ms"] = 50
    result["scope"]["requested_track_handles"].append("other")
    assert target.name == "VOCAL" and target.devices[0].latency_in_ms == 1
    assert scope == ["vocal"]
