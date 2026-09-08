"""Safe previews and explicit snapshot comparison semantics."""

import copy
from unittest.mock import Mock

import pytest

from MCP_Server.workflow_tools import compare_snapshots, prepare_track_edit, register_workflow_tools


class Registry:
    def __init__(self): self.tools = {}
    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


@pytest.mark.parametrize("name,value,send", [
    ("volume", float("nan"), 0), ("volume", 1.1, 0), ("volume", True, 0),
    ("panning", -1.1, 0), ("arm", 1, 0), ("send", 0.5, 0),
    ("send", 0.5, True), ("name", "", 0), ("delete", True, 0),
])
def test_invalid_track_edits_fail_before_connection(name, value, send):
    with pytest.raises(ValueError):
        prepare_track_edit(name, value, send)


def test_preview_never_sends_a_write_and_apply_uses_handle():
    registry = Registry()
    connection = Mock()
    targets = {"session_id": "set1", "revision": "revision1", "tracks": [
        {"track_handle": "bass", "track_index": 3, "name": "Bass", "kind": "track"}]}
    connection.send_command.return_value = targets
    register_workflow_tools(registry, lambda: connection)
    edit = registry.tools["edit_track"]
    result = edit("set1", "bass", "volume", 0.6)
    assert result["status"] == "preview"
    assert connection.send_command.call_count == 1
    result = edit("set1", "bass", "volume", 0.6, preview=False)
    assert result["status"] == "applied"
    connection.send_command.assert_called_with("set_track_volume", {
        "volume": 0.6, "session_id": "set1", "track_handle": "bass"})


def test_stale_set_and_deleted_handle_cannot_apply():
    registry = Registry()
    connection = Mock()
    connection.send_command.return_value = {"session_id": "set2", "revision": "r", "tracks": []}
    register_workflow_tools(registry, lambda: connection)
    with pytest.raises(ValueError, match="Stale"):
        registry.tools["edit_track"]("set1", "bass", "mute", True, preview=False)
    with pytest.raises(ValueError, match="no longer exists"):
        registry.tools["edit_track"]("set2", "bass", "mute", True, preview=False)
    assert all(call.args[0] == "get_edit_targets" for call in connection.send_command.call_args_list)


def snapshot():
    return {"schema_version": 1, "session_id": "set1", "tempo": 120,
            "tracks": [{"track_handle": "bass", "name": "Bass", "track_index": 1,
                        "volume": 0.5, "read_errors": [], "clips_truncated": False}]}


def test_snapshot_diff_preserves_track_identity_across_rename():
    before = snapshot()
    after = copy.deepcopy(before)
    after["tracks"][0].update(name="Sub", volume=0.7)
    after["tempo"] = 125
    delta = compare_snapshots(before, after)
    assert delta["complete"]
    assert not delta["added"] and not delta["removed"]
    assert delta["changed"][0]["changes"]["volume"] == {"before": 0.5, "after": 0.7}
    assert delta["song_changes"]["tempo"] == {"before": 120, "after": 125}
    assert before["tracks"][0]["name"] == "Bass"


def test_diff_never_claims_completeness_for_truncated_or_failed_reads():
    before, after = snapshot(), snapshot()
    after["tracks"][0]["read_errors"] = ["clips unavailable"]
    assert not compare_snapshots(before, after)["complete"]
    after["session_id"] = "different"
    with pytest.raises(ValueError, match="different Set"):
        compare_snapshots(before, after)


def test_export_verification_does_not_read_file_while_recording(monkeypatch):
    registry = Registry()
    connection = Mock()
    connection.send_command.return_value = {"operation_id": "job", "active": True,
        "status": "recording", "outputs": [{"source": "Bass", "file_path": "/recording.wav"}]}
    register_workflow_tools(registry, lambda: connection)
    monkeypatch.setattr("MCP_Server.workflow_tools.analyze_file", lambda *args: pytest.fail("read active file"))
    result = registry.tools["verify_export_outputs"]()
    assert not result["all_files_measured"]
    assert result["outputs"][0]["analysis_status"] == "recording_active"


def test_export_verification_preserves_missing_and_failed_file_results(monkeypatch):
    registry = Registry()
    connection = Mock()
    connection.send_command.return_value = {"operation_id": "job", "active": False, "status": "done",
        "outputs": [{"source": "Bass", "file_path": None}, {"source": "Lead", "file_path": "/absent.wav"}]}
    register_workflow_tools(registry, lambda: connection)
    def fail(*args): raise FileNotFoundError("file absent")
    monkeypatch.setattr("MCP_Server.workflow_tools.analyze_file", fail)
    result = registry.tools["verify_export_outputs"]()
    assert [r["analysis_status"] for r in result["outputs"]] == ["missing_file", "error"]
    assert not result["all_files_measured"]


def test_latency_workflows_forward_stable_identities_and_poll_same_plan():
    registry, connection = Registry(), Mock()
    register_workflow_tools(registry, lambda: connection)
    registry.tools["get_latency_report"]("set1", ["voice"])
    connection.send_command.assert_called_with("get_latency_report", {
        "session_id": "set1", "track_handles": ["voice"]})
    registry.tools["configure_monitoring"]("preview", "set1", ["voice"], "direct")
    connection.send_command.assert_called_with("configure_monitoring", {
        "action": "preview", "session_id": "set1", "track_handles": ["voice"],
        "monitoring_path": "direct", "plan_id": ""})
    for action in ("apply", "restore"):
        registry.tools["configure_monitoring"](action, "set1", plan_id="retained")
        connection.send_command.assert_called_with("configure_monitoring", {
            "action": action, "session_id": "set1", "track_handles": None,
            "monitoring_path": None, "plan_id": "retained"})


def test_timing_analysis_has_no_live_connection(monkeypatch):
    registry = Registry()
    register_workflow_tools(registry, lambda: pytest.fail("offline analysis connected to Live"))
    analyzer = Mock(return_value={"status": "measured"})
    monkeypatch.setattr("MCP_Server.workflow_tools.analyze_timing", analyzer)
    assert registry.tools["analyze_recording_timing"]("/hits.wav", [.5, 1, 1.5], skip_initial=0) == {"status": "measured"}
    analyzer.assert_called_once_with("/hits.wav", [.5, 1, 1.5], 100., -40., 50., 1, 0)
